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
