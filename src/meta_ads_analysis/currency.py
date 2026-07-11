"""Static FX normalization for cross-account performance reporting.

Cross-account efficiency reads (:func:`meta_ads_analysis.account_discovery.cross_account_performance`)
compare accounts that bill in different currencies. To add money metrics across those accounts we
first convert each native amount into a single **reporting currency** (default USD). The conversion
rates come from a **static table checked into the repo** (``config/fx_rates.json``) — never from a
network / Meta FX call. That is a deliberate product decision: no network means mock and unattended
runs stay deterministic, and the numbers are explicitly labelled "approximate, not live" so no
consumer mistakes them for billing-grade rates.

The table maps ``currency code -> multiplier that converts one native unit into the base (USD)``::

    usd_amount        = native_amount * rates[native]
    amount_reporting  = native_amount * rates[native] / rates[reporting]

Both the native and the reporting currency must be present in the table, otherwise
:meth:`FxTable.convert` returns ``None`` (never a guess, never a silent pass-through of unlike
currencies). ``as_of`` is required so the tool can surface the rate vintage; a table missing it (or
with an empty / non-numeric / non-positive rate) is a load-time :class:`ValueError` — bad data, not a
runtime absence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

# Committed, static FX table. Unlike ``config/meta_ads_accounts.json`` (gitignored, absent in
# mock/unattended runs) this file MUST be committed — it is required for currency normalization.
DEFAULT_FX_TABLE_PATH: Path = PROJECT_ROOT / "config" / "fx_rates.json"


@dataclass(frozen=True)
class FxTable:
    """A loaded, validated static FX table.

    ``rates`` maps an **upper-cased** currency code to the multiplier that converts one native unit
    of that currency into ``base`` (USD in the committed table). Currency lookups are therefore
    case-insensitive.
    """

    as_of: str
    base: str
    note: str | None
    rates: dict[str, float]  # currency (upper-cased) -> rate-to-base

    def has(self, currency: str) -> bool:
        """True iff ``currency`` (any case) has a rate in the table."""
        return bool(currency) and str(currency).strip().upper() in self.rates

    def convert(
        self, amount: float, *, from_currency: str, to_currency: str
    ) -> float | None:
        """Convert a native ``amount`` from one currency into another.

        Returns ``None`` — never a guess — when **either** currency is absent from the table, so the
        caller can route that account's normalized fields to *absent* and record an error while still
        returning native figures. Formula: ``amount * rates[from] / rates[to]``.
        """
        src = str(from_currency).strip().upper() if from_currency else ""
        dst = str(to_currency).strip().upper() if to_currency else ""
        from_rate = self.rates.get(src)
        to_rate = self.rates.get(dst)
        if from_rate is None or to_rate is None:
            return None
        return amount * from_rate / to_rate


def load_fx_table(path: Path | None = None) -> FxTable:
    """Load and validate the static FX table at ``path`` (default :data:`DEFAULT_FX_TABLE_PATH`).

    Raises :class:`ValueError` with an actionable message when the file is missing, is not a JSON
    object, lacks ``as_of``, lacks a non-empty ``rates`` object, or carries a non-numeric / zero /
    negative rate (all bad data, distinct from a currency simply being absent at convert time).
    Currency codes are upper-cased on load so lookups are case-insensitive.
    """
    resolved = path or DEFAULT_FX_TABLE_PATH
    if not resolved.exists():
        raise ValueError(
            f"FX rate table not found: {resolved}. Expected a committed config/fx_rates.json "
            "with an 'as_of' date and a 'rates' object."
        )

    try:
        payload: Any = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"FX rate table {resolved} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"FX rate table {resolved} must be a JSON object.")

    as_of = payload.get("as_of")
    if not isinstance(as_of, str) or not as_of.strip():
        raise ValueError(
            f"FX rate table {resolved} is missing a non-empty 'as_of' date (required so the tool "
            "can surface the rate vintage)."
        )

    raw_rates = payload.get("rates")
    if not isinstance(raw_rates, dict) or not raw_rates:
        raise ValueError(
            f"FX rate table {resolved} must contain a non-empty 'rates' object mapping currency "
            "codes to rate-to-base multipliers."
        )

    rates: dict[str, float] = {}
    for currency, raw_rate in raw_rates.items():
        # Reject bool up front: bool is an int subclass, so isinstance(True, (int, float)) is True.
        if isinstance(raw_rate, bool) or not isinstance(raw_rate, (int, float)):
            raise ValueError(
                f"FX rate for '{currency}' in {resolved} must be a number, got {raw_rate!r}."
            )
        if raw_rate <= 0:
            raise ValueError(
                f"FX rate for '{currency}' in {resolved} must be positive, got {raw_rate!r}."
            )
        rates[str(currency).strip().upper()] = float(raw_rate)

    base = str(payload.get("base") or "USD").strip().upper()
    note = payload.get("note")
    note_str = str(note) if isinstance(note, str) and note.strip() else None
    return FxTable(as_of=as_of.strip(), base=base, note=note_str, rates=rates)


# --- ISO-4217 minor-unit exponents ------------------------------------------------------------
#
# Meta returns budget / spend-cap / amount-spent fields in the account currency's **minor unit**
# (e.g. USD cents). Converting a minor-unit integer into major units is ``value / 10 ** exponent``.
# The exponent is a stable, public, finite ISO-4217 fact — unlike the FX *rates* (which drift over
# time and therefore live in the committed ``config/fx_rates.json``), the number of minor-unit
# digits does not change, so it lives here as a Python constant: no network, no file I/O, no cache,
# deterministic under mock/unattended runs. This is the exponent analogue of
# :data:`meta_ads_analysis.account_discovery.ACCOUNT_STATUS_LABELS`.

#: The ISO-4217 default minor-unit exponent — correct for the ~150 two-decimal currencies (USD, EUR,
#: GBP, …). Everything NOT listed in :data:`CURRENCY_MINOR_UNIT_EXPONENTS` uses this.
DEFAULT_MINOR_UNIT_EXPONENT = 2

#: ISO-4217 minor-unit exceptions: currency code -> exponent. Only the NON-2-decimal currencies are
#: stored; any code absent here defaults to :data:`DEFAULT_MINOR_UNIT_EXPONENT`. Keeping this complete
#: matters — a stray missing entry silently reintroduces the wrong divisor for that currency.
CURRENCY_MINOR_UNIT_EXPONENTS: dict[str, int] = {
    # zero-decimal (minor unit == major unit; divisor 10**0 == 1)
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0,
    "PYG": 0, "RWF": 0, "UGX": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    # three-decimal (divisor 10**3 == 1000)
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    # four-decimal (divisor 10**4 == 10000)
    "CLF": 4, "UYW": 4,
}

#: Common 2-decimal currency codes we explicitly recognize. Everything here uses the 2-decimal
#: default, so listing a code changes no arithmetic — it only distinguishes "known 2-decimal" from
#: "unrecognized code, exponent *assumed* 2-decimal" (see :func:`minor_unit_exponent_is_known`, used
#: by the pacing report's assumption note). This tuple MUST stay a superset of every code in
#: ``config/fx_rates.json`` so an FX-supported account is never flagged as an assumption.
_KNOWN_TWO_DECIMAL_CURRENCIES: tuple[str, ...] = (
    # config/fx_rates.json codes (MUST remain a subset — guarded by a test):
    "USD", "EUR", "GBP", "BRL", "MXN", "CAD", "AUD",
    # other common Meta-supported 2-decimal currencies:
    "AED", "ARS", "BDT", "BOB", "CHF", "CNY", "COP", "CRC", "CZK", "DKK", "DZD", "EGP",
    "GTQ", "HKD", "HNL", "HUF", "IDR", "ILS", "INR", "KES", "LKR", "MOP", "MYR", "NGN",
    "NIO", "NOK", "NZD", "PEN", "PHP", "PKR", "PLN", "QAR", "RON", "RUB", "SAR", "SEK",
    "SGD", "THB", "TRY", "TWD", "UAH", "UYU", "VES", "ZAR",
)

#: Every currency code whose minor-unit exponent we recognize for sure: the ISO exceptions above
#: plus the explicitly-listed common 2-decimal codes. A code outside this set falls through to the
#: 2-decimal default, but that fallback is an *assumption* and is surfaced rather than silently made.
KNOWN_MINOR_UNIT_CURRENCIES: frozenset[str] = frozenset(CURRENCY_MINOR_UNIT_EXPONENTS) | frozenset(
    _KNOWN_TWO_DECIMAL_CURRENCIES
)


def minor_unit_exponent(currency: str) -> int:
    """ISO-4217 minor-unit exponent for a currency code (case-insensitive); never raises.

    Returns :data:`DEFAULT_MINOR_UNIT_EXPONENT` (2) for any code not in
    :data:`CURRENCY_MINOR_UNIT_EXPONENTS` — including blank / ``None`` / ``"UNKNOWN"`` — because 2 is
    both the ISO default and the correct answer for the ~150 two-decimal currencies. Intended as the
    exponent of 10 in the minor->major divisor: ``value / 10 ** minor_unit_exponent(currency)``.
    """
    code = str(currency).strip().upper() if currency else ""
    return CURRENCY_MINOR_UNIT_EXPONENTS.get(code, DEFAULT_MINOR_UNIT_EXPONENT)


def minor_unit_exponent_is_known(currency: str) -> bool:
    """True iff ``currency`` (case-insensitive) is a code whose minor-unit exponent we recognize.

    Distinguishes "we know this currency is 2-decimal" from "unrecognized code, exponent assumed
    2-decimal" so a caller can surface the assumption instead of guessing silently. A blank / ``None``
    / ``"UNKNOWN"`` code is NOT known.
    """
    code = str(currency).strip().upper() if currency else ""
    return code in KNOWN_MINOR_UNIT_CURRENCIES
