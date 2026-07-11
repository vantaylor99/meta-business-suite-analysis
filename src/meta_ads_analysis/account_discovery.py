"""Ad-account discovery: normalize the ``/me/adaccounts`` reach into human-readable rows.

This is the one read that works **before any config exists** — it asks the shared env token
"which ad accounts can you reach?" and returns one normalized row per account, adding a
human-readable ``account_status_label`` alongside the raw ``account_status`` code so Cowork can
relay it in plain language.

**Where the transformation lives, and why here.** The reader seam
(:mod:`meta_ads_analysis.reader_provider`) is a strict byte-for-byte passthrough —
``DirectMetaReader`` adds nothing and a parity test enforces verbatim 1:1 delegation, so status-label
normalization cannot live on the reader. It also cannot live in ``mcp_server.build_read_tools``,
whose tools are asserted to return exactly what the reader returns. So normalization lives here: an
import-light library module (no FastMCP, no token lookup) that is unit-testable with a
``FakeMetaReader`` and reused by the follow-up cross-account aggregate tool.

Reads are intentionally open to every account the token can reach — there is **no registry gate**
here (plan decision, ``mcp-cross-account-read-tools``).
"""

from __future__ import annotations

import concurrent.futures
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import account_registry
from .config import (
    ATTENTION_CPC_DEGRADE_PCT,
    ATTENTION_CPR_DEGRADE_PCT,
    ATTENTION_CTR_DROP_PCT,
    ATTENTION_MIN_RESULTS_FLOOR,
    ATTENTION_MIN_SPEND,
    ATTENTION_PACING_VARIANCE_PCT,
    ATTENTION_SPEND_COLLAPSE_PCT,
    ATTENTION_SPEND_SPIKE_PCT,
    PACING_ON_TRACK_TOLERANCE_PCT,
    PACING_SHORTLIST_LIMIT,
)
from .currency import (
    FxTable,
    load_fx_table,
    minor_unit_exponent,
    minor_unit_exponent_is_known,
)
from .meta_api import MetaApiError
from .sync_api import (
    PURCHASE_KEYS,
    _find_metric,
    _infer_primary_result_action,
    _label_for_action,
    _metric_blob_list,
    _number,
)

if TYPE_CHECKING:  # typing only — keep this module import-light (no reader construction here)
    from .reader_provider import MetaReaderProvider

# Meta ad-account ``account_status`` codes → human labels. Unknown / missing codes → "UNKNOWN".
ACCOUNT_STATUS_LABELS: dict[int, str] = {
    1: "ACTIVE",
    2: "DISABLED",
    3: "UNSETTLED",
    7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT",
    9: "IN_GRACE_PERIOD",
    100: "PENDING_CLOSURE",
    101: "CLOSED",
    201: "ANY_ACTIVE",
    202: "ANY_CLOSED",
}

# Default fields requested from /me/adaccounts (plan decision A).
DEFAULT_AD_ACCOUNT_FIELDS: list[str] = [
    "account_id",
    "name",
    "account_status",
    "currency",
    "timezone_name",
    "amount_spent",
    "business",
]


def account_status_label(code: Any) -> str:
    """Human label for a Meta ``account_status`` code; ``"UNKNOWN"`` for anything unmapped/None.

    Never raises: a ``None``, an unexpected code, or a non-int value all fall through to
    ``"UNKNOWN"`` rather than a ``KeyError``/``TypeError``.
    """
    try:
        return ACCOUNT_STATUS_LABELS.get(int(code), "UNKNOWN")
    except (TypeError, ValueError):
        return "UNKNOWN"


def normalize_ad_account(row: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of a ``/me/adaccounts`` row with ``account_status_label`` added.

    The raw ``account_status`` code is preserved verbatim (never replaced). Rows missing the field
    entirely (Meta omits empty fields) still get a label — ``account_status_label`` handles the
    ``None`` case — so this never indexes a missing key.
    """
    normalized = dict(row)
    normalized["account_status_label"] = account_status_label(row.get("account_status"))
    return normalized


def list_ad_accounts(
    reader: "MetaReaderProvider", *, fields: list[str] | None = None
) -> list[dict[str, Any]]:
    """Discovery: every reachable ad account as a normalized row.

    Empty reach → ``[]`` (never an exception). A permission/Graph failure propagates as
    ``MetaApiError`` unchanged — the FastMCP tool layer maps it to an operator-readable ``ToolError``;
    this pure library does not swallow it.
    """
    rows = reader.list_ad_accounts(fields=fields or DEFAULT_AD_ACCOUNT_FIELDS)
    return [normalize_ad_account(row) for row in rows]


# Additive insight metrics summed per currency by :func:`cross_account_spend_summary`. ONLY additive
# metrics belong here: a ratio metric (cpc / ctr / roas) is meaningless when summed across accounts,
# so if one is ever added to the per-row output it MUST stay out of ``totals_by_currency``.
DEFAULT_SUMMARY_INSIGHT_FIELDS: list[str] = ["spend", "impressions", "clicks"]


def _parse_metric(value: Any) -> int | float:
    """Parse a Meta metric (usually a numeric string) into a number; ``0`` for missing/blank/garbage.

    Meta returns metrics as strings — ``spend`` with decimals (``"100.50"``), counts without
    (``"5000"``). Summing the raw strings would concatenate, so we parse first. A value carrying a
    decimal point or exponent parses to ``float``; a whole-number string parses to ``int``; anything
    unparseable (``None``, ``""``, non-numeric text) counts as ``0``. Mixing an ``int`` subtotal with a
    parsed ``float`` promotes to ``float`` naturally, so per-currency spend stays a float.
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return 0
    try:
        if "." in text or "e" in text or "E" in text:
            return float(text)
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return 0


def _ad_account_id_from_row(row: dict[str, Any]) -> str:
    """The ``act_<id>`` form for a normalized account row.

    ``/me/adaccounts`` rows carry ``id`` as ``act_<id>`` and ``account_id`` as the bare number; prefer
    ``id`` when present and fall back to ``account_id``, normalizing either through the registry helper
    so a bare numeric id becomes ``act_<id>``.
    """
    raw = row.get("id") or row.get("account_id") or ""
    return account_registry._normalize_ad_account_id(str(raw).strip())


# --------------------------------------------------------------------------- #
# The shared multi-account "scope" seam.
#
# Every multi-account tool resolves *which* accounts a request covers through
# :func:`resolve_scope`. Today that is either the whole reach (``account_ids=None`` ->
# ``list_ad_accounts``) or an explicit, de-duplicated list. When a real grouping layer
# (``mcp-role-based-access-tiers``) lands, it changes ONLY this function; callers keep
# reading ``.account_ids``. ``.metadata_by_id`` carries the per-account rows the discovery
# path already paid to fetch (from ``/me/adaccounts``), so the hot all-accounts path never
# has to re-fetch each account via ``get_account`` — see the ticket's read-doubling analysis.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedScope:
    """The set of accounts a multi-account request covers, plus any prefetched metadata.

    - ``account_ids``: normalized ``act_<id>`` forms, de-duplicated, order-preserving.
    - ``metadata_by_id``: prefetched ``/me/adaccounts`` rows keyed by normalized id when the
      scope was *discovered* (``account_ids=None``); ``{}`` for an explicit list (callers then
      fetch per-account metadata on demand). A future grouping layer with no prefetched rows
      simply returns ``{}`` and callers fall back to ``get_account``.
    - ``requested_all``: ``True`` iff the caller passed ``account_ids=None`` (whole reach).
    """

    account_ids: list[str]
    metadata_by_id: dict[str, dict[str, Any]]
    requested_all: bool


def resolve_scope(
    reader: "MetaReaderProvider", account_ids: list[str] | None = None
) -> ResolvedScope:
    """Resolve which accounts a request covers into a :class:`ResolvedScope`.

    ``account_ids=None`` -> discover the whole reach via :func:`list_ad_accounts` (one read;
    a discovery-level ``MetaApiError`` propagates as a whole-call failure, unchanged) and keep
    each row's metadata. An explicit list -> normalize each id (bare numeric or ``act_`` both
    work) and de-duplicate order-preserving (so ``["1", "act_1"]`` collapses to one) with no
    metadata prefetch.
    """
    if account_ids is None:
        discovered = list_ad_accounts(reader)  # may raise MetaApiError -> whole-call failure
        ids: list[str] = []
        metadata_by_id: dict[str, dict[str, Any]] = {}
        for row in discovered:
            ad_account_id = _ad_account_id_from_row(row)
            if ad_account_id not in metadata_by_id:
                ids.append(ad_account_id)
                metadata_by_id[ad_account_id] = row
        return ResolvedScope(account_ids=ids, metadata_by_id=metadata_by_id, requested_all=True)

    seen: set[str] = set()
    normalized_ids: list[str] = []
    for raw in account_ids:
        norm = account_registry._normalize_ad_account_id(str(raw or "").strip())
        if norm not in seen:
            seen.add(norm)
            normalized_ids.append(norm)
    return ResolvedScope(account_ids=normalized_ids, metadata_by_id={}, requested_all=False)


# --------------------------------------------------------------------------- #
# Bounded-concurrency fan-out.
#
# The reader is synchronous and reads are I/O-bound HTTP GETs whose socket wait releases the
# GIL, so threads give real parallelism here; the shared ``requests.Session`` is safe for
# concurrent GETs and the client's own 429/5xx back-off runs independently per worker thread.
# --------------------------------------------------------------------------- #

# Env var that tunes the fan-out worker pool for very large fleets. Default 8; clamped to
# [1, 32]. Token-free and never raises on a bad value (mirrors ``reader_backend_from_env``).
FANOUT_MAX_WORKERS_ENV = "META_FANOUT_MAX_WORKERS"
DEFAULT_FANOUT_MAX_WORKERS = 8
_FANOUT_MIN_WORKERS = 1
_FANOUT_MAX_WORKERS_CAP = 32


def fanout_max_workers_from_env() -> int:
    """Worker count from ``META_FANOUT_MAX_WORKERS`` (default 8), clamped to ``[1, 32]``.

    Token-free and construction-free. A missing var yields the default; a garbage value (not an
    int) also falls back to the default rather than raising — a health probe can read this safely.
    """
    raw = os.environ.get(FANOUT_MAX_WORKERS_ENV)
    if raw is None:
        return DEFAULT_FANOUT_MAX_WORKERS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_FANOUT_MAX_WORKERS
    return max(_FANOUT_MIN_WORKERS, min(value, _FANOUT_MAX_WORKERS_CAP))


def fan_out_accounts(
    read_one: Callable[[str], Any],
    account_ids: list[str],
    *,
    max_workers: int | None = None,
) -> list[tuple[str, Any | None, str | None]]:
    """Map ``read_one(ad_account_id)`` over ``account_ids`` with bounded concurrency.

    Returns one tuple per input id, **in input order** regardless of completion order:
    ``(ad_account_id, result_or_None, error_str_or_None)``. A per-account ``MetaApiError`` is
    caught and returned as its ``str`` in the third slot (result ``None``); any other exception
    propagates (a real bug must not be silently swallowed). An empty ``account_ids`` returns
    ``[]`` without constructing a pool (``ThreadPoolExecutor`` requires ``max_workers >= 1``).

    ``max_workers`` resolves to :func:`fanout_max_workers_from_env` when ``None``, then is
    clamped to ``min(resolved, len(account_ids))`` so we never spin more workers than accounts.
    """
    if not account_ids:
        return []

    resolved = fanout_max_workers_from_env() if max_workers is None else max_workers
    resolved = max(_FANOUT_MIN_WORKERS, min(resolved, len(account_ids)))

    results: list[tuple[str, Any | None, str | None]] = [
        (ad_account_id, None, None) for ad_account_id in account_ids
    ]

    def _worker(index: int, ad_account_id: str) -> None:
        # Runs on a worker thread. Only MetaApiError is caught -> per-account error slot; any
        # other exception propagates out of future.result() below (never swallowed). Writes to
        # ``results[index]`` are index-disjoint across threads, so no lock is needed.
        try:
            results[index] = (ad_account_id, read_one(ad_account_id), None)
        except MetaApiError as exc:
            results[index] = (ad_account_id, None, str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=resolved) as executor:
        futures = [
            executor.submit(_worker, index, ad_account_id)
            for index, ad_account_id in enumerate(account_ids)
        ]
        for future in concurrent.futures.as_completed(futures):
            future.result()  # re-raise any non-MetaApiError from a worker

    return results


def cross_account_spend_summary(
    reader: "MetaReaderProvider",
    *,
    date_from: str,
    date_to: str,
    account_ids: list[str] | None = None,
    insight_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate spend/performance across every reachable account (or an explicit subset) in one call.

    Fans out over the target accounts with **bounded concurrency** (:func:`fan_out_accounts`) —
    tunable via ``META_FANOUT_MAX_WORKERS`` (default 8); the client's own ``429`` retry handles
    rate limits independently inside each worker thread. For each account it reads a single
    aggregated account-level insights row (``level="account"``, ``time_increment="all_days"``) for
    the window and extracts the requested additive metrics. Additive metrics are subtotaled **per
    currency** — never across currencies, so there is deliberately no grand total. A per-account
    failure (permission, exhausted retry, an unreadable explicit id) is recorded in ``errors`` and
    skipped; it never fails the whole call.

    Scope is resolved through :func:`resolve_scope`. When ``account_ids`` is omitted, targets and
    their metadata come from :func:`list_ad_accounts` (all reachable accounts); a discovery-level
    ``MetaApiError`` there (bad token / missing scope) propagates — a whole-call failure, distinct
    from a per-account one. When ``account_ids`` is given, each id is normalized (bare numeric or
    ``act_`` both work) and its metadata is fetched per id via ``reader.get_account`` inside the
    same per-account error path.

    Output is deterministic regardless of which worker finishes first: ``accounts`` rows appear in
    scope order and per-currency subtotals are assembled on the main thread from the ordered
    fan-out results. See the module ticket for the returned shape; ``note="no accounts reachable"``
    is present only when no ids were given and discovery found nothing.
    """
    fields = list(insight_fields) if insight_fields else list(DEFAULT_SUMMARY_INSIGHT_FIELDS)

    scope = resolve_scope(reader, account_ids)  # discovery path may raise -> whole-call failure

    def read_one(ad_account_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        # Runs on a worker thread. Metadata comes from the prefetched discovery rows when present
        # (the hot all-accounts path never calls get_account); an explicit-list id fetches its own.
        meta_row = scope.metadata_by_id.get(ad_account_id)
        if meta_row is None:
            meta_row = normalize_ad_account(
                reader.get_account(ad_account_id, fields=DEFAULT_AD_ACCOUNT_FIELDS)
            )
        insight_rows = reader.fetch_insights(
            ad_account_id,
            fields=fields,
            date_from=date_from,
            date_to=date_to,
            level="account",
            time_increment="all_days",
        )
        return meta_row, insight_rows

    results = fan_out_accounts(read_one, scope.account_ids)

    accounts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    totals_by_currency: dict[str, dict[str, Any]] = {}

    # Main-thread assembly over the input-ordered fan-out results: identical output for identical
    # inputs no matter which worker completed first.
    for ad_account_id, payload, error in results:
        if error is not None:
            # Central correctness requirement: one account's failure is recorded and skipped, never
            # fatal to the whole fan-out, and never contaminates another account's subtotal.
            errors.append({"ad_account_id": ad_account_id, "error": error})
            continue

        meta_row, insight_rows = payload
        # all_days at account level yields one aggregated row; no delivery in range -> no row -> zeros.
        insight_row = insight_rows[0] if insight_rows else {}
        currency = meta_row.get("currency") or "UNKNOWN"

        account_entry: dict[str, Any] = {
            "ad_account_id": ad_account_id,
            "account_id": meta_row.get("account_id"),
            "name": meta_row.get("name"),
            "currency": currency,
            "account_status": meta_row.get("account_status"),
            "account_status_label": meta_row.get("account_status_label"),
        }
        subtotal = totals_by_currency.setdefault(
            currency, {**{field: 0 for field in fields}, "account_count": 0}
        )
        for field in fields:
            raw_metric = insight_row.get(field)
            value = _parse_metric(raw_metric)
            # Per-row reflects what Meta returned (omit a field Meta left blank/absent); the subtotal
            # counts a missing metric as 0 so the currency total is still complete.
            if raw_metric not in (None, ""):
                account_entry[field] = value
            subtotal[field] += value
        subtotal["account_count"] += 1
        accounts.append(account_entry)

    # reachable_count == account_count == resolved scope size (attempted); len(accounts) is the
    # succeeded count — the attempted-vs-succeeded distinction downstream callers rely on.
    result: dict[str, Any] = {
        "date_from": date_from,
        "date_to": date_to,
        "account_count": len(scope.account_ids),
        "reachable_count": len(scope.account_ids),
        "accounts": accounts,
        "totals_by_currency": totals_by_currency,
        "errors": errors,
    }
    if scope.requested_all and not scope.account_ids:
        result["note"] = "no accounts reachable"
    return result


# --------------------------------------------------------------------------- #
# Cross-account PERFORMANCE: efficiency metrics + currency normalization.
#
# Where cross_account_spend_summary returns only raw additive totals grouped by currency, this
# adds per-account *efficiency* metrics (cpm/cpc/ctr/cost_per_result/roas) recomputed from summed
# base components — never averaged across accounts (Simpson's-paradox-safe) — plus money metrics
# normalized to a single reporting currency via the static FX table (config/fx_rates.json).
# --------------------------------------------------------------------------- #

# Base metrics fetched per account for the performance read (account-level, all_days -> one row).
DEFAULT_PERFORMANCE_INSIGHT_FIELDS: list[str] = [
    "spend",
    "impressions",
    "clicks",
    "actions",
    "action_values",
]

# The money metrics that get a ``*_normalized`` twin in the reporting currency. ``ctr`` and ``roas``
# are currency-invariant ratios — they are NEVER normalized (no ``*_normalized`` key).
_NORMALIZED_MONEY_DERIVED: tuple[str, ...] = ("cpm", "cpc", "cost_per_result")


def compute_derived_metrics(base: dict[str, float | int | None]) -> dict[str, float]:
    """Recompute ratio efficiency metrics from summed/native base components.

    NEVER averages a ratio across accounts: every ratio is recomputed from its numerator and
    denominator, so the same helper is correct for a single account's native base, a per-currency
    summed base, or the summed normalized base (the single point that guarantees Simpson's-paradox
    safety). A metric whose denominator is zero, or whose needed component is missing (``None``), is
    **omitted** from the result — never emitted as ``inf`` / ``NaN`` / ``0``.

    Inputs consumed (any may be missing/``None``): ``spend``, ``impressions``, ``clicks``,
    ``results``, ``purchase_value``. Outputs (each present only when defined):
    ``cpm = spend/impressions*1000``, ``cpc = spend/clicks``, ``ctr = clicks/impressions*100``,
    ``cost_per_result = spend/results``, ``roas = purchase_value/spend``.
    """
    out: dict[str, float] = {}
    spend = base.get("spend")
    impressions = base.get("impressions")
    clicks = base.get("clicks")
    results = base.get("results")
    purchase_value = base.get("purchase_value")

    # impressions is the denominator for cpm and ctr; a zero/absent denominator drops both.
    if impressions:
        if spend is not None:
            out["cpm"] = spend / impressions * 1000
        if clicks is not None:
            out["ctr"] = clicks / impressions * 100
    if clicks and spend is not None:
        out["cpc"] = spend / clicks
    if results and spend is not None:
        out["cost_per_result"] = spend / results
    if spend and purchase_value is not None:
        out["roas"] = purchase_value / spend
    return out


def _as_count(value: float | None) -> int | float | None:
    """Present a count metric as ``int`` when it is a whole number, else the ``float`` unchanged.

    Money metrics (spend / purchase_value / the derived ratios) stay ``float``; only additive counts
    (impressions / clicks / results) run through this so the output mirrors Meta's integer counts.
    """
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


def _registry_by_ad_account_id() -> dict[str, account_registry.MetaAdsAccount]:
    """Best-effort ``{ad_account_id: MetaAdsAccount}`` map, reversed from the slug-keyed registry.

    The config registry (``config/meta_ads_accounts.json``) is gitignored and **absent in
    mock/unattended runs**, so a missing or invalid config yields an empty map rather than an error —
    consulting it must never break an otherwise-open read.
    """
    try:
        registry = account_registry.load_account_registry()
    except (FileNotFoundError, ValueError):
        return {}
    return {account.ad_account_id: account for account in registry.values()}


def _resolve_result_key(
    ad_account_id: str,
    actions: list[dict[str, Any]],
    registry_by_id: dict[str, account_registry.MetaAdsAccount],
) -> tuple[str | None, str | None]:
    """Resolve ``(primary_result_action_type, label)`` for an account: config first, else inference.

    Uses the account's configured ``primary_result_action_type`` when the account is in the registry;
    otherwise infers the key from the account's own ``actions`` blob (so the mock/no-config path still
    works). Returns ``(None, None)`` when neither yields a key — the caller then leaves ``results`` /
    ``result_label`` / ``cost_per_result`` absent rather than zero-filling a misleading ratio.
    """
    account = registry_by_id.get(ad_account_id)
    if account is not None and account.primary_result_action_type:
        key = account.primary_result_action_type
        return key, account.primary_result_label or _label_for_action(key)
    key = _infer_primary_result_action(actions)
    return key, (_label_for_action(key) if key else None)


def cross_account_performance(
    reader: "MetaReaderProvider",
    *,
    date_from: str,
    date_to: str,
    account_ids: list[str] | None = None,
    reporting_currency: str = "USD",
    level: str = "account",
    fx_table: FxTable | None = None,
) -> dict[str, Any]:
    """Per-account efficiency metrics + currency-normalized totals across the reachable accounts.

    Rides the same fan-out engine as :func:`cross_account_spend_summary` (``resolve_scope`` ->
    :func:`fan_out_accounts` -> main-thread assembly), so it inherits determinism (identical output
    regardless of worker completion order) and per-account partial-failure isolation. For each account
    it reads one aggregated account-level insights row (``spend, impressions, clicks, actions,
    action_values``; ``time_increment="all_days"``) and:

    - recomputes the efficiency metrics (``cpm``/``cpc``/``ctr``/``cost_per_result``/``roas``) from the
      account's own base components via :func:`compute_derived_metrics` — never an averaged ratio;
    - normalizes the money metrics (``spend``, ``cpm``, ``cpc``, ``cost_per_result``,
      ``purchase_value``) into ``reporting_currency`` (default USD) using the static FX table
      (``config/fx_rates.json``); ``ctr`` and ``roas`` are currency-invariant and get no twin.

    ``totals_by_currency`` subtotals the native base per currency and recomputes the derived metrics
    from those sums. ``normalized_total`` sums the normalized money base plus the currency-invariant
    counts across accounts that HAD an FX rate and recomputes the derived metrics in the reporting
    currency; accounts whose currency is absent from the FX table keep their native figures, record an
    ``errors`` entry, and are excluded from ``normalized_total`` (counted in ``excluded_no_fx``).

    Every aggregate block (each ``totals_by_currency`` subtotal and ``normalized_total``) also carries
    ``results_accounts`` / ``purchase_value_accounts`` — how many of its ``account_count`` accounts
    actually contributed ``results`` / ``purchase_value`` to the sum. This makes coverage legible: a
    portfolio ROAS built from 1 of 10 accounts reads differently from one built from all 10. The counts
    are always emitted (``0`` is meaningful — it explains why ``cost_per_result``/``roas`` are absent).

    ``level`` accepts only ``"account"`` for now (a future ticket can add campaign/adset roll-ups); any
    other value raises ``ValueError``. A ``reporting_currency`` absent from the FX table is a whole-call
    ``ValueError`` — nothing could be normalized. ``fx_table`` is injectable for tests; when ``None`` the
    committed table is loaded once before the fan-out (never per worker).
    """
    if level != "account":
        raise ValueError(
            f"cross_account_performance supports only level='account'; got {level!r}. "
            "Campaign/adset roll-ups are a future enhancement."
        )
    reporting = str(reporting_currency or "").strip().upper()
    table = fx_table if fx_table is not None else load_fx_table()
    if not table.has(reporting):
        raise ValueError(
            f"reporting_currency {reporting!r} has no rate in the FX table (as_of {table.as_of}); "
            "cannot normalize to it."
        )

    registry_by_id = _registry_by_ad_account_id()
    scope = resolve_scope(reader, account_ids)  # discovery path may raise -> whole-call failure

    def read_one(ad_account_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        meta_row = scope.metadata_by_id.get(ad_account_id)
        if meta_row is None:
            meta_row = normalize_ad_account(
                reader.get_account(ad_account_id, fields=DEFAULT_AD_ACCOUNT_FIELDS)
            )
        insight_rows = reader.fetch_insights(
            ad_account_id,
            fields=DEFAULT_PERFORMANCE_INSIGHT_FIELDS,
            date_from=date_from,
            date_to=date_to,
            level="account",
            time_increment="all_days",
        )
        return meta_row, insight_rows

    results = fan_out_accounts(read_one, scope.account_ids)

    accounts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    # Per-currency native accumulators; ``_results``/``_pv`` carry contributor counts so a metric no
    # account reported stays ABSENT (never a 0 that would fabricate a cost_per_result / roas).
    currency_acc: dict[str, dict[str, Any]] = {}
    # normalized_total accumulators (only accounts that had an FX rate contribute).
    norm = {
        "spend": 0.0,
        "impressions": 0.0,
        "clicks": 0.0,
        "results": 0.0,
        "results_contrib": 0,
        "purchase_value": 0.0,
        "pv_contrib": 0,
        "account_count": 0,
        "excluded_no_fx": 0,
    }

    for ad_account_id, payload, error in results:
        if error is not None:
            errors.append({"ad_account_id": ad_account_id, "error": error})
            continue

        meta_row, insight_rows = payload
        # account-level all_days yields one aggregated row; no delivery in range -> no row -> absent.
        insight_row = insight_rows[0] if insight_rows else {}
        currency = meta_row.get("currency") or "UNKNOWN"
        actions = _metric_blob_list(insight_row.get("actions"))
        action_values = _metric_blob_list(insight_row.get("action_values"))

        # Native base metrics as float | None (None = Meta returned nothing, distinct from a real 0).
        spend = _number(insight_row.get("spend"))
        impressions = _number(insight_row.get("impressions"))
        clicks = _number(insight_row.get("clicks"))
        result_key, result_label = _resolve_result_key(ad_account_id, actions, registry_by_id)
        results_value = _find_metric(actions, [result_key]) if result_key else None
        purchase_value = _find_metric(action_values, PURCHASE_KEYS)

        native_base = {
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "results": results_value,
            "purchase_value": purchase_value,
        }
        native_derived = compute_derived_metrics(native_base)

        row: dict[str, Any] = {
            "ad_account_id": ad_account_id,
            "account_id": meta_row.get("account_id"),
            "name": meta_row.get("name"),
            "currency": currency,
            "account_status": meta_row.get("account_status"),
            "account_status_label": meta_row.get("account_status_label"),
        }
        # Native base: omit a metric Meta left blank; counts as int, money as float.
        if spend is not None:
            row["spend"] = spend
        if impressions is not None:
            row["impressions"] = _as_count(impressions)
        if clicks is not None:
            row["clicks"] = _as_count(clicks)
        if results_value is not None:
            row["results"] = _as_count(results_value)
        if result_key:
            row["result_label"] = result_label
        if purchase_value is not None:
            row["purchase_value"] = purchase_value
        row.update(native_derived)  # cpm/cpc/ctr/cost_per_result/roas (absent omitted)

        # Per-currency native subtotal accumulation (every account, including no-FX ones).
        acc = currency_acc.setdefault(
            currency,
            {
                "spend": 0.0,
                "impressions": 0.0,
                "clicks": 0.0,
                "results": 0.0,
                "results_contrib": 0,
                "purchase_value": 0.0,
                "pv_contrib": 0,
                "account_count": 0,
            },
        )
        acc["spend"] += spend or 0.0
        acc["impressions"] += impressions or 0.0
        acc["clicks"] += clicks or 0.0
        if results_value is not None:
            acc["results"] += results_value
            acc["results_contrib"] += 1
        if purchase_value is not None:
            acc["purchase_value"] += purchase_value
            acc["pv_contrib"] += 1
        acc["account_count"] += 1

        # Currency normalization: money twins + normalized_total inclusion, or a no-FX error.
        if table.has(currency):
            spend_norm = (
                table.convert(spend, from_currency=currency, to_currency=reporting)
                if spend is not None
                else None
            )
            pv_norm = (
                table.convert(purchase_value, from_currency=currency, to_currency=reporting)
                if purchase_value is not None
                else None
            )
            # Recompute derived from the NORMALIZED base (single-point recompute); take money twins.
            norm_derived = compute_derived_metrics(
                {
                    "spend": spend_norm,
                    "impressions": impressions,
                    "clicks": clicks,
                    "results": results_value,
                    "purchase_value": pv_norm,
                }
            )
            if spend_norm is not None:
                row["spend_normalized"] = spend_norm
            for metric in _NORMALIZED_MONEY_DERIVED:
                if metric in norm_derived:
                    row[f"{metric}_normalized"] = norm_derived[metric]
            if pv_norm is not None:
                row["purchase_value_normalized"] = pv_norm

            norm["account_count"] += 1
            norm["spend"] += spend_norm or 0.0
            norm["impressions"] += impressions or 0.0
            norm["clicks"] += clicks or 0.0
            if results_value is not None:
                norm["results"] += results_value
                norm["results_contrib"] += 1
            if pv_norm is not None:
                norm["purchase_value"] += pv_norm
                norm["pv_contrib"] += 1
        else:
            errors.append(
                {
                    "ad_account_id": ad_account_id,
                    "error": f"no FX rate for currency '{currency}' (as_of {table.as_of})",
                }
            )
            norm["excluded_no_fx"] += 1

        accounts.append(row)

    totals_by_currency = {cur: _finalize_subtotal(acc) for cur, acc in currency_acc.items()}
    normalized_total = _finalize_normalized_total(norm, reporting)

    result: dict[str, Any] = {
        "date_from": date_from,
        "date_to": date_to,
        "level": level,
        "reporting_currency": reporting,
        "fx_as_of": table.as_of,
        "fx_note": table.note,
        "account_count": len(scope.account_ids),
        "reachable_count": len(scope.account_ids),
        "accounts": accounts,
        "totals_by_currency": totals_by_currency,
        "normalized_total": normalized_total,
        "errors": errors,
    }
    if scope.requested_all and not scope.account_ids:
        result["note"] = "no accounts reachable"
    return result


def _finalize_subtotal(acc: dict[str, Any]) -> dict[str, Any]:
    """Turn a per-currency native accumulator into the emitted subtotal (summed base + derived).

    ``results``/``purchase_value`` are emitted (and feed the derived recompute) only when at least one
    account in the group contributed them, so a group where nobody reported results/revenue simply
    lacks ``cost_per_result``/``roas`` rather than showing a fabricated zero.
    """
    base = {
        "spend": acc["spend"],
        "impressions": acc["impressions"],
        "clicks": acc["clicks"],
        "results": acc["results"] if acc["results_contrib"] else None,
        "purchase_value": acc["purchase_value"] if acc["pv_contrib"] else None,
    }
    out: dict[str, Any] = {
        "spend": acc["spend"],
        "impressions": _as_count(acc["impressions"]),
        "clicks": _as_count(acc["clicks"]),
    }
    if acc["results_contrib"]:
        out["results"] = _as_count(acc["results"])
    if acc["pv_contrib"]:
        out["purchase_value"] = acc["purchase_value"]
    out["account_count"] = acc["account_count"]
    out["results_accounts"] = acc["results_contrib"]
    out["purchase_value_accounts"] = acc["pv_contrib"]
    out.update(compute_derived_metrics(base))
    return out


def _finalize_normalized_total(norm: dict[str, Any], reporting: str) -> dict[str, Any]:
    """Turn the normalized accumulator into ``normalized_total`` (summed normalized base + derived).

    Present-but-empty when every account was excluded / none were reachable: zeroed base, no derived
    keys, and ``excluded_no_fx`` reflecting the count.
    """
    base = {
        "spend": norm["spend"],
        "impressions": norm["impressions"],
        "clicks": norm["clicks"],
        "results": norm["results"] if norm["results_contrib"] else None,
        "purchase_value": norm["purchase_value"] if norm["pv_contrib"] else None,
    }
    out: dict[str, Any] = {
        "reporting_currency": reporting,
        "spend": norm["spend"],
        "impressions": _as_count(norm["impressions"]),
        "clicks": _as_count(norm["clicks"]),
    }
    if norm["results_contrib"]:
        out["results"] = _as_count(norm["results"])
    if norm["pv_contrib"]:
        out["purchase_value"] = norm["purchase_value"]
    out["account_count"] = norm["account_count"]
    out["excluded_no_fx"] = norm["excluded_no_fx"]
    out["results_accounts"] = norm["results_contrib"]
    out["purchase_value_accounts"] = norm["pv_contrib"]
    out.update(compute_derived_metrics(base))
    return out


# --------------------------------------------------------------------------- #
# Single-account BENCHMARK: one account's efficiency as percentiles within a cohort.
#
# This is a *pure post-processor* over cross_account_performance — it re-reads nothing from Meta.
# It calls that tool once for the cohort (target included) and computes percentiles / quartiles over
# the per-account rows it already returned, so it inherits FX normalization, Simpson's-paradox-safe
# derived metrics, per-account partial-failure isolation, and the bounded-concurrency fan-out for
# free. The only new logic here is the percentile math and the per-metric assembly.
# --------------------------------------------------------------------------- #

# The efficiency metrics benchmarked, mapped to their directionality. VOLUME metrics (spend /
# impressions / clicks / results / purchase_value) are deliberately NOT benchmarked — "is my spend in
# a good percentile?" is ambiguous (volume, not efficiency). Percentiles are oriented so a HIGH
# percentile always means "good", for both directions: a low cost and a high quality ratio both land
# in a high percentile.
BENCHMARK_METRIC_DIRECTION: dict[str, str] = {
    "cpm": "lower_is_better",
    "cpc": "lower_is_better",
    "cost_per_result": "lower_is_better",
    "ctr": "higher_is_better",
    "roas": "higher_is_better",
}
# Money metrics are compared via each row's reporting-currency twin (``f"{metric}_normalized"``) so a
# USD account benchmarks correctly against a peer set in other currencies; ratio metrics are
# currency-invariant and compared on their native value.
_BENCHMARK_MONEY_METRICS: frozenset[str] = frozenset({"cpm", "cpc", "cost_per_result"})
_BENCHMARK_RATIO_METRICS: frozenset[str] = frozenset({"ctr", "roas"})

# Documented reliability floor: below this many peers with a valid value, a percentile is still
# computed but flagged ``unreliable`` (per metric) / ``too_small`` (whole cohort).
MIN_COHORT_FOR_PERCENTILE: int = 5


def quantiles(values: list[float], q: float | list[float]) -> float | list[float] | None:
    """Hand-rolled linear-interpolation quantile(s) over ``values`` (no numpy dependency).

    ``q`` may be a single quantile in ``[0, 1]`` (returns one ``float``) or a sequence of them
    (returns a list, one per ``q``). The estimator sorts ascending, computes ``rank = q * (n - 1)``
    and interpolates between the two straddling order statistics — e.g.
    ``quantiles([10, 20, 30, 40], 0.25) == 17.5``, ``median == 25.0``, ``0.75 == 32.5``.

    ``n == 1`` returns the single value for every ``q``; an empty ``values`` returns ``None`` (or a
    list of ``None`` for a list ``q``) — never raises.
    """
    single = isinstance(q, (int, float)) and not isinstance(q, bool)
    qs: list[float] = [float(q)] if single else [float(x) for x in q]  # type: ignore[arg-type]
    ordered = sorted(values)
    n = len(ordered)

    out: list[float | None] = []
    for quantile in qs:
        if n == 0:
            out.append(None)
        elif n == 1:
            out.append(ordered[0])
        else:
            rank = quantile * (n - 1)
            lo = int(math.floor(rank))
            hi = int(math.ceil(rank))
            if lo == hi:
                out.append(ordered[lo])
            else:
                out.append(ordered[lo] + (rank - lo) * (ordered[hi] - ordered[lo]))
    return out[0] if single else out


def percentile_rank(
    cohort_values: list[float], value: float, *, higher_is_better: bool
) -> float | None:
    """Percentile rank of ``value`` within ``cohort_values`` (which INCLUDES ``value`` itself).

    Oriented so a HIGH percentile always means "good": for ``higher_is_better`` a large value ranks
    high; for ``lower_is_better`` a small value ranks high. Uses the mid-rank convention so ties share
    a percentile::

        pr = 100 * (L + 0.5 * E) / N        N = len(cohort_values)
          higher_is_better:  L = count strictly LESS than value;    E = count EQUAL (incl. self)
          lower_is_better:   L = count strictly GREATER than value;  E = count EQUAL (incl. self)

    Guarantees a unique best in ``N == 10`` reads ``95.0``, a unique worst ``5.0``, an exact median
    ``~50``, and a two-way tie ``50.0`` each. Returns ``None`` when ``N < 1`` (nothing to rank).
    """
    n = len(cohort_values)
    if n < 1:
        return None
    equal = sum(1 for v in cohort_values if v == value)
    if higher_is_better:
        beaten = sum(1 for v in cohort_values if v < value)
    else:
        beaten = sum(1 for v in cohort_values if v > value)
    return 100.0 * (beaten + 0.5 * equal) / n


def _benchmark_verdict(pr: float) -> str:
    """Plain-language verdict from an (already good=high) percentile — a pure function of ``pr``."""
    if pr >= 75:
        return "better than most peers"
    if pr >= 50:
        return "above the cohort median"
    if pr >= 25:
        return "below the cohort median"
    return "worse than most peers"


def account_benchmark(
    reader: "MetaReaderProvider",
    *,
    account_id: str,
    date_from: str,
    date_to: str,
    cohort_ids: list[str] | None = None,
    reporting_currency: str = "USD",
    fx_table: FxTable | None = None,
) -> dict[str, Any]:
    """Rank ONE account's efficiency metrics as percentiles within a comparison cohort.

    The specialist-facing counterpart to :func:`cross_account_performance` (manager-facing ranking):
    same underlying metric rows, inverted point of view — one account vs. the field. It is a **pure
    post-processor**: it calls :func:`cross_account_performance` once for the cohort (target always
    included) and computes percentiles over the rows that call already returned, inheriting FX
    normalization, Simpson's-paradox-safe derived metrics, per-account failure isolation, and the
    determinism of the fan-out.

    Cohort resolution:

    - ``cohort_ids is None`` -> the whole reach (``account_ids=None``); the target is naturally
      included if reachable. If the target is NOT among the reachable rows, ``account`` is ``None`` and
      a ``note`` says it was not found.
    - ``cohort_ids`` given -> the union of that list and the target; the target is **force-added if
      absent** (the specialist's own account must be in its own comparison — a decision, not an error).

    Only the efficiency metrics in :data:`BENCHMARK_METRIC_DIRECTION` are benchmarked; volume metrics
    are not (a "good" spend percentile is ambiguous). Money metrics compare on each row's
    ``*_normalized`` twin (reporting currency); ratio metrics on their native value. Percentiles are
    oriented so a high percentile is always "good". A metric the target lacks, or with no peers, carries
    a ``reason`` instead of a percentile block; every metric key is always present. A cohort with fewer
    than :data:`MIN_COHORT_FOR_PERCENTILE` readable accounts still returns numbers but flags them
    ``unreliable`` / ``too_small``.

    An invalid ``reporting_currency`` (absent from the FX table) propagates as ``ValueError`` from
    :func:`cross_account_performance` — the same whole-call contract as the prereq. ``fx_table`` is a
    test-injection seam (not exposed to the LLM); when ``None`` the committed table is loaded once and
    passed through to the prereq so both sides share one table.
    """
    target_id = account_registry._normalize_ad_account_id(str(account_id or "").strip())
    # Load the FX table here (once) and pass it down so the same table validates reporting_currency,
    # normalizes the rows, AND tells us whether the TARGET's own currency has a rate.
    table = fx_table if fx_table is not None else load_fx_table()

    # Explicit cohort -> force-add the target (resolve_scope normalizes + dedups, so a redundant id is
    # safe). Whole-reach cohort -> None (the reachable target is included naturally).
    scope_ids = None if cohort_ids is None else [*cohort_ids, target_id]

    perf = cross_account_performance(
        reader,
        date_from=date_from,
        date_to=date_to,
        account_ids=scope_ids,
        reporting_currency=reporting_currency,
        fx_table=table,
    )

    rows: list[dict[str, Any]] = perf["accounts"]
    perf_errors: list[dict[str, Any]] = perf["errors"]
    target_row = next((r for r in rows if r.get("ad_account_id") == target_id), None)
    read_ok = len(rows)
    too_small = read_ok < MIN_COHORT_FOR_PERCENTILE

    result: dict[str, Any] = {
        "account_id": target_id,
        "date_from": date_from,
        "date_to": date_to,
        "reporting_currency": perf["reporting_currency"],
        "fx_as_of": perf["fx_as_of"],
        "fx_note": perf["fx_note"],
        "account": target_row,
        "cohort": {
            # accounts resolved into the cohort (attempted, incl. target).
            "count": perf["account_count"],
            # rows successfully read (a no-FX account still counts here — it has a native row).
            "read_ok": read_ok,
            # every prereq error (unreadable account / no-FX currency), surfaced not silent.
            "excluded": [
                {"ad_account_id": e.get("ad_account_id"), "reason": e.get("error")}
                for e in perf_errors
            ],
            "too_small": too_small,
            "min_for_percentile": MIN_COHORT_FOR_PERCENTILE,
        },
        "errors": perf_errors,
    }

    # Target could not be located among the read rows: distinguish unreadable (an error row exists) from
    # simply not-in-reach. Either way every metric key is still present, carrying a read-level reason.
    if target_row is None:
        if any(e.get("ad_account_id") == target_id for e in perf_errors):
            reason = "target account could not be read"
            result["note"] = f"target account {target_id} could not be read"
        else:
            reason = "target account not found in cohort"
            result["note"] = f"target account {target_id} not found in cohort"
        result["benchmarks"] = {
            metric: {"value": None, "direction": direction, "reason": reason}
            for metric, direction in BENCHMARK_METRIC_DIRECTION.items()
        }
        return result

    # Target present: benchmark each efficiency metric against the cohort's values for that metric.
    target_currency = target_row.get("currency") or "UNKNOWN"
    target_no_fx = not table.has(target_currency)

    benchmarks: dict[str, Any] = {}
    for metric, direction in BENCHMARK_METRIC_DIRECTION.items():
        is_money = metric in _BENCHMARK_MONEY_METRICS
        field = f"{metric}_normalized" if is_money else metric
        target_value = target_row.get(field)

        if target_value is None:
            # Money twin absent because the target is in a no-FX currency vs. Meta simply not
            # returning the metric — two distinct, both-honest reasons.
            if is_money and target_no_fx:
                reason = f"no FX rate for {target_currency}"
            else:
                reason = f"account missing {metric}"
            benchmarks[metric] = {"value": None, "direction": direction, "reason": reason}
            continue

        # A row lacking the needed field is excluded from THIS metric's cohort (per-metric N varies).
        cohort_values = [v for v in (r.get(field) for r in rows) if v is not None]
        cohort_n = len(cohort_values)
        if cohort_n < 2:
            benchmarks[metric] = {
                "value": target_value,
                "direction": direction,
                "reason": f"no peers with {metric} in cohort",
            }
            continue

        higher = direction == "higher_is_better"
        percentile = percentile_rank(cohort_values, target_value, higher_is_better=higher)
        p25, median, p75 = quantiles(cohort_values, [0.25, 0.5, 0.75])  # type: ignore[misc]
        # rank: 1 = best; ties share the count-of-strictly-better + 1.
        if higher:
            strictly_better = sum(1 for v in cohort_values if v > target_value)
        else:
            strictly_better = sum(1 for v in cohort_values if v < target_value)
        benchmarks[metric] = {
            "value": target_value,
            "direction": direction,
            "cohort_n": cohort_n,
            "percentile": percentile,
            "rank": strictly_better + 1,
            "rank_of": cohort_n,
            "median": median,
            "p25": p25,
            "p75": p75,
            "verdict": _benchmark_verdict(percentile),
            "unreliable": cohort_n < MIN_COHORT_FOR_PERCENTILE,
        }

    result["benchmarks"] = benchmarks
    if too_small:
        result["note"] = "cohort too small for a meaningful percentile"
    return result


# --------------------------------------------------------------------------- #
# ATTENTION SCAN: the handful of accounts that changed and need a human NOW.
#
# A *pure post-processor* over cross_account_performance — exactly the relationship account_benchmark
# has to that tool. It calls cross_account_performance TWICE over the SAME resolved scope (once per
# window), joins the two per-account metric rows by ad_account_id, and runs a pure flag-evaluation over
# each pair. So it inherits — for free — FX normalization, Simpson's-paradox-safe derived metrics
# (compute_derived_metrics never zero/inf-fills), per-account partial-failure isolation, and the
# deterministic bounded-concurrency fan-out. No new Meta read shape is introduced.
#
# SCOPE: budget-pacing (spend-to-date vs configured budget) is a DIFFERENT question over a DIFFERENT
# surface and is owned by the sibling `pacing_report` tool. By DEFAULT this tool never reads budget
# config; when called with `include_pacing=True` it calls `pacing_report` ONCE over the same scope and
# folds its per-account over/under verdict in as an opt-in `budget_pacing_off` flag (never re-reading
# budget config itself — reuse, no cycle: attention -> pacing -> cross_account_performance).
# Ad-level creative/disapproval problems need a per-account ad-level fan-out (heavy), so — like pacing —
# they are OFF by default and gated behind `include_ad_health=True`, which fans out ONLY into the ads of
# the accounts the cheap scan already flagged (never the full fleet). With both opt-ins off, every flag
# is zero extra reads beyond the two account-level fan-outs.
# --------------------------------------------------------------------------- #

# Severity rank: high(3) > medium(2) > low(1) > info(0). An account's severity is the max over its
# fired flags; ``flagged`` collects severity >= medium, ``informational`` the info-only accounts.
_SEVERITY_RANK: dict[str, int] = {"info": 0, "low": 1, "medium": 2, "high": 3}
_RANK_TO_SEVERITY: dict[int, str] = {rank: name for name, rank in _SEVERITY_RANK.items()}
_MEDIUM_RANK = _SEVERITY_RANK["medium"]

# account_status_label buckets for the account_status_alert flag (see ACCOUNT_STATUS_LABELS above).
_STATUS_ALERT_HIGH: frozenset[str] = frozenset({"DISABLED", "PENDING_CLOSURE", "CLOSED"})
_STATUS_ALERT_MEDIUM: frozenset[str] = frozenset(
    {"UNSETTLED", "PENDING_RISK_REVIEW", "PENDING_SETTLEMENT", "IN_GRACE_PERIOD"}
)

# Ad-level status vocabulary for the opt-in ad-health scan (``include_ad_health``). Defined LOCALLY —
# not imported from ``monitor`` — to keep this module import-light (see the module docstring; importing
# monitor drags in confidence/control/early_triage/meta_api). Keep ``_AD_DELIVERING`` in sync with its
# sibling definition ``monitor.DELIVERING``.
_AD_DELIVERING: frozenset[str] = frozenset({"ACTIVE", "IN_PROCESS"})  # mirrors monitor.DELIVERING
_AD_PAUSE_STATUSES: frozenset[str] = frozenset(
    {"PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "DELETED", "ARCHIVED"}
)
# An ACTIVE-configured ad counts as "not delivering" only when its effective_status is NONE of these:
# a delivering status, a deliberate pause (operator intent, not a stall), or DISAPPROVED (counted by
# its own high-severity flag, never double-counted here).
_AD_NOT_DELIVERING_EXCLUSIONS: frozenset[str] = (
    _AD_DELIVERING | _AD_PAUSE_STATUSES | frozenset({"DISAPPROVED"})
)
_AD_HEALTH_FIELDS: list[str] = ["id", "name", "status", "effective_status", "issues_info"]


@dataclass(frozen=True)
class AttentionThresholds:
    """Overridable thresholds for the attention scan.

    Defaults come from the ``ATTENTION_*`` constants in :mod:`config` so no magic numbers live in the
    engine. Injectable for tests; the MCP wrapper always uses :meth:`defaults` — this is a
    programmatic/test seam, exactly like ``fx_table`` on :func:`cross_account_performance`.

    - Percent knees (``*_pct``) are FRACTIONS: ``0.5`` == a 50% move, ``0.3`` == a 30% move.
    - ``min_spend_floor`` gates on a NORMALIZED (reporting-currency) spend figure so "$100 of spend"
      means the same across a USD and an MXN account (native fallback for a no-FX account).
    - ``min_results_floor`` is the cost-degradation significance floor: BOTH windows must clear it
      before a cost-per-result flag fires.
    - ``pacing_variance_pct`` is the "materially off-pace" knee for the opt-in ``budget_pacing_off``
      flag (only consulted when ``flag_accounts_needing_attention`` is called with
      ``include_pacing=True``). Larger than pacing's own 5% on_track tolerance — a small variance is
      not attention-worthy.
    - ``ad_health_min_count`` is the minimum ad count for an ad-level flag (``ads_disapproved`` /
      ``ads_not_delivering``) to fire in the opt-in ``include_ad_health`` scan. Defaults to ``1`` (any
      disapproved/stalled ad is worth surfacing on an already-flagged account) — a count, not a percent
      knee, so it is a plain int rather than an ``ATTENTION_*`` fraction constant.
    """

    spend_spike_pct: float
    spend_collapse_pct: float
    cost_per_result_degrade_pct: float
    cpc_degrade_pct: float
    ctr_drop_pct: float
    min_spend_floor: float
    min_results_floor: float
    pacing_variance_pct: float
    ad_health_min_count: int = 1

    @classmethod
    def defaults(cls) -> "AttentionThresholds":
        """The committed defaults from :mod:`config` (50% spend move / 30% efficiency degradation;
        25% budget-pacing variance knee; ad-health flags fire on any single bad ad)."""
        return cls(
            spend_spike_pct=ATTENTION_SPEND_SPIKE_PCT,
            spend_collapse_pct=ATTENTION_SPEND_COLLAPSE_PCT,
            cost_per_result_degrade_pct=ATTENTION_CPR_DEGRADE_PCT,
            cpc_degrade_pct=ATTENTION_CPC_DEGRADE_PCT,
            ctr_drop_pct=ATTENTION_CTR_DROP_PCT,
            min_spend_floor=ATTENTION_MIN_SPEND,
            min_results_floor=ATTENTION_MIN_RESULTS_FLOOR,
            pacing_variance_pct=ATTENTION_PACING_VARIANCE_PCT,
            ad_health_min_count=1,
        )


def prior_window(current_from: str, current_to: str) -> tuple[str, str]:
    """The immediately-preceding window of equal length.

    Pure and clock-free: parses ISO ``YYYY-MM-DD``, takes the inclusive length
    ``(to - from).days + 1``, and returns the equal-length span ending the day before ``current_from``::

        prior_window("2026-06-08", "2026-06-14")  ->  ("2026-06-01", "2026-06-07")   # 7 days

    Raises ``ValueError`` on unparseable dates (via :meth:`date.fromisoformat`) or ``from > to``.
    """
    start = date.fromisoformat(current_from)
    end = date.fromisoformat(current_to)
    if start > end:
        raise ValueError(
            f"current window from ({current_from}) is after to ({current_to}); cannot derive a "
            "prior window."
        )
    length = (end - start).days + 1
    baseline_to = start - timedelta(days=1)
    baseline_from = baseline_to - timedelta(days=length - 1)
    return baseline_from.isoformat(), baseline_to.isoformat()


def _spend_for_floor(row: dict[str, Any] | None) -> float | None:
    """The spend figure a floor is compared against: NORMALIZED preferred, native fallback (no-FX)."""
    if not row:
        return None
    value = row.get("spend_normalized")
    return value if value is not None else row.get("spend")


def _normalized_spend(row: dict[str, Any] | None) -> float | None:
    """Reporting-currency spend for the deterministic sort tiebreak (native fallback for a no-FX row)."""
    return _spend_for_floor(row)


def _at_or_above(value: float | None, floor: float) -> bool:
    """True iff ``value`` is present and clears ``floor`` (an absent metric never clears a floor)."""
    return value is not None and value >= floor


def _flag(
    name: str,
    severity: str,
    *,
    current: Any,
    baseline: Any,
    delta: float | None,
    delta_pct: float | None,
    detail: str,
) -> dict[str, Any]:
    """One fired flag. ``delta_pct`` is a FRACTION (0.6 == +60%); ``None`` when a % is undefined."""
    return {
        "name": name,
        "severity": severity,
        "current": current,
        "baseline": baseline,
        "delta": delta,
        "delta_pct": delta_pct,
        "detail": detail,
    }


def _account_status_flag(label: Any) -> dict[str, Any] | None:
    """The ``account_status_alert`` flag for an account-status label, or ``None`` when the account is
    in a healthy/unknown status. Baseline-independent — derived purely from the current row's label."""
    if label in _STATUS_ALERT_HIGH:
        severity = "high"
    elif label in _STATUS_ALERT_MEDIUM:
        severity = "medium"
    else:
        return None
    return _flag(
        "account_status_alert",
        severity,
        current=label,
        baseline=None,
        delta=None,
        delta_pct=None,
        detail=f"account status is {label}",
    )


def _budget_pacing_flag(
    pacing_entry: dict[str, Any], thresholds: AttentionThresholds
) -> dict[str, Any] | None:
    """The ``budget_pacing_off`` flag for one :func:`pacing_report` per-account entry, or ``None``.

    Baseline-independent (derived purely from the pacing verdict), so — like
    :func:`_account_status_flag` — it is appended by the orchestrator rather than produced inside the
    pure :func:`evaluate_attention_flags`. Pure and unit-testable with a hand-built pacing entry (no
    reader).

    Fires **only** when the entry's pacing ``status`` is ``over`` or ``under`` AND
    ``abs(variance_pct) >= thresholds.pacing_variance_pct`` (a larger knee than pacing's own 5%
    on_track tolerance — a tiny variance is not attention-worthy). Every other status
    (``on_track`` / ``no_budget_set`` / ``budget_not_projectable`` / ``account_inactive`` /
    ``not_started`` / ``budget_unread``) -> ``None``.

    Severity mirrors the ticket's mapping: ``over`` -> **high** (over-spend burns budget fast —
    urgent), ``under`` -> **medium** (under-delivery is a missed-pacing concern, less urgent).

    ``variance_pct`` is a same-currency ratio -> **FX-invariant**, used directly as ``delta_pct``.
    ``current`` / ``baseline`` use the normalized projected-spend / period-budget twins with a native
    fallback for a no-FX account, exactly as :func:`_flag` shapes the behavior flags.
    """
    status = pacing_entry.get("status")
    if status not in ("over", "under"):
        return None
    variance_pct = pacing_entry.get("variance_pct")
    if variance_pct is None or abs(variance_pct) < thresholds.pacing_variance_pct:
        return None

    projected = pacing_entry.get("projected_spend_normalized")
    if projected is None:
        projected = pacing_entry.get("projected_spend")
    budget = pacing_entry.get("period_budget_normalized")
    if budget is None:
        budget = pacing_entry.get("period_budget")
    delta = projected - budget if projected is not None and budget is not None else None

    severity = "high" if status == "over" else "medium"
    return _flag(
        "budget_pacing_off",
        severity,
        current=projected,
        baseline=budget,
        delta=delta,
        delta_pct=variance_pct,
        detail=f"projected to spend {abs(variance_pct) * 100:.0f}% {status} the period budget",
    )


def _ad_health_flags(
    ads: list[dict[str, Any]], thresholds: AttentionThresholds
) -> list[dict[str, Any]]:
    """PURE ad-level health flags for one account's ads (the opt-in ``include_ad_health`` scan).

    Fully unit-testable with hand-built ad dicts — no reader. Returns 0-2 flags, each in the
    :func:`_flag` shape with ``baseline/delta/delta_pct = None`` (ad health is a point-in-time count,
    not a window delta). A flag fires only when its count ``>= thresholds.ad_health_min_count``
    (default 1). Like :func:`_account_status_flag` / :func:`_budget_pacing_flag`, this is appended by
    the orchestrator rather than produced inside the pure two-row :func:`evaluate_attention_flags`.

    - ``ads_disapproved`` (**high**) — count of ads with ``effective_status == "DISAPPROVED"``: a
      blocked/policy ad, burning nothing but delivering nothing. Counted regardless of ``status``.
    - ``ads_not_delivering`` (**medium**) — count of ads the operator INTENDS to run
      (``status == "ACTIVE"``) whose ``effective_status`` is in none of
      :data:`_AD_NOT_DELIVERING_EXCLUSIONS` (a delivering status, a deliberate pause, or DISAPPROVED —
      already counted above and never double-counted). This residual-with-exclusions definition catches
      ``WITH_ISSUES`` / ``PENDING_REVIEW`` / ``PENDING_BILLING_INFO`` / ``PREAPPROVED`` and any future
      block status, while a paused ad (``PAUSED`` / ``CAMPAIGN_PAUSED`` / ``ADSET_PAUSED``) is treated
      as intentional, not a stall.
    """
    total = len(ads)
    disapproved = 0
    not_delivering = 0
    for ad in ads:
        effective_status = ad.get("effective_status")
        if effective_status == "DISAPPROVED":
            disapproved += 1
        elif (
            ad.get("status") == "ACTIVE"
            and effective_status not in _AD_NOT_DELIVERING_EXCLUSIONS
        ):
            not_delivering += 1

    min_count = thresholds.ad_health_min_count
    flags: list[dict[str, Any]] = []
    if disapproved >= min_count:
        flags.append(
            _flag(
                "ads_disapproved",
                "high",
                current=disapproved,
                baseline=None,
                delta=None,
                delta_pct=None,
                detail=f"{disapproved} of {total} ads are DISAPPROVED",
            )
        )
    if not_delivering >= min_count:
        flags.append(
            _flag(
                "ads_not_delivering",
                "medium",
                current=not_delivering,
                baseline=None,
                delta=None,
                delta_pct=None,
                detail=(
                    f"{not_delivering} of {total} ads are ACTIVE-configured but not delivering "
                    "(blocked/pending)"
                ),
            )
        )
    return flags


def evaluate_attention_flags(
    current_row: dict[str, Any] | None,
    baseline_row: dict[str, Any] | None,
    thresholds: AttentionThresholds,
) -> list[dict[str, Any]]:
    """PURE flag evaluation over two per-account metric rows (as emitted by
    :func:`cross_account_performance`).

    Fully unit-testable with hand-built dict fixtures — no reader. Returns the fired flags, each a
    ``{name, severity, current, baseline, delta, delta_pct, detail}`` dict.

    Key correctness rules baked in here:

    - ``compute_derived_metrics`` OMITS (never zero/inf-fills) any ratio whose denominator is 0 or
      whose component is absent, so a row simply *lacks* ``cost_per_result``/``cpc``/``ctr`` when
      undefined — a missing key means "cannot compute this flag," never 0.
    - Every ``/ baseline`` is guarded: a zero/absent/below-floor baseline yields ``insufficient_history``
      or ``newly_active`` (info), never an ``inf`` % spike.
    - Percent moves are computed on NATIVE figures (currency-invariant for one account across two
      windows); absolute floors compare on the NORMALIZED figure (native fallback for a no-FX account).
    - ``stalled_delivery`` fires only when the account reads ``ACTIVE``; a DISABLED/paused account with
      zero delivery surfaces via ``account_status_alert`` instead. (It cannot distinguish a deliberate
      all-ads pause on an ACTIVE account from a real stall — that needs ad-level reads, out of scope.)
    """
    cur = current_row or {}
    base = baseline_row

    flags: list[dict[str, Any]] = []

    # 1. account_status_alert — baseline-independent, fires even for a new/insufficient account.
    status_flag = _account_status_flag(cur.get("account_status_label"))
    if status_flag is not None:
        flags.append(status_flag)

    cur_floor_spend = _spend_for_floor(cur)
    base_floor_spend = _spend_for_floor(base)
    cur_native_spend = cur.get("spend")
    base_native_spend = base.get("spend") if base else None

    cur_spend_ok = _at_or_above(cur_floor_spend, thresholds.min_spend_floor)
    base_spend_ok = _at_or_above(base_floor_spend, thresholds.min_spend_floor)

    # 2. No usable baseline (absent row, or spend below the material floor): the account is either
    # newly active (material NOW, ~nothing before) or simply has too little history to compare. Either
    # way NO %-move / cost-degradation flag can fire (they would divide by a ~0 baseline).
    if base is None or not base_spend_ok:
        if cur_spend_ok:
            flags.append(
                _flag(
                    "newly_active",
                    "info",
                    current=cur_native_spend,
                    baseline=base_native_spend,
                    delta=None,
                    delta_pct=None,
                    detail="material spend now with little/no spend in the baseline window",
                )
            )
        else:
            flags.append(
                _flag(
                    "insufficient_history",
                    "info",
                    current=cur_native_spend,
                    baseline=base_native_spend,
                    delta=None,
                    delta_pct=None,
                    detail="baseline window has too little spend to compare against",
                )
            )
        return flags

    # 3. Baseline is usable (>= floor, so it was genuinely delivering). Compare the windows. Percent
    # moves use NATIVE spend (present whenever floor spend is), currency-invariant for one account.
    base_s = base_native_spend
    cur_s = cur_native_spend

    # spend_spike — BOTH windows material AND current up beyond the spike knee.
    if cur_spend_ok and base_s and cur_s is not None:
        move = (cur_s - base_s) / base_s
        if move >= thresholds.spend_spike_pct:
            severity = "high" if move >= 2 * thresholds.spend_spike_pct else "medium"
            flags.append(
                _flag(
                    "spend_spike",
                    severity,
                    current=cur_s,
                    baseline=base_s,
                    delta=cur_s - base_s,
                    delta_pct=move,
                    detail=f"spend up {move * 100:.0f}% vs. the baseline window",
                )
            )

    # spend_collapse — baseline material AND current down past the collapse knee (current may fall
    # below the floor; that is the collapse). NATIVE spend, absent current treated as 0.
    if base_s:
        cur_s0 = cur_s if cur_s is not None else 0.0
        move = (cur_s0 - base_s) / base_s
        if move <= -thresholds.spend_collapse_pct:
            flags.append(
                _flag(
                    "spend_collapse",
                    "high",
                    current=cur_s0,
                    baseline=base_s,
                    delta=cur_s0 - base_s,
                    delta_pct=move,
                    detail=f"spend down {abs(move) * 100:.0f}% vs. the baseline window",
                )
            )

    # stalled_delivery — an ACTIVE account that WAS delivering (baseline >= floor, guaranteed here) and
    # now has essentially no delivery (spend AND impressions ~0). Status gate is the key disambiguation
    # from a deliberately DISABLED account (which surfaces via account_status_alert).
    cur_impr0 = cur.get("impressions") or 0
    cur_spend0 = cur_s if cur_s is not None else 0.0
    if cur.get("account_status_label") == "ACTIVE" and cur_spend0 <= 0 and cur_impr0 <= 0:
        flags.append(
            _flag(
                "stalled_delivery",
                "high",
                current=cur_spend0,
                baseline=base_s,
                delta=cur_spend0 - base_s,
                delta_pct=-1.0,
                detail="account was delivering but has ~zero spend and impressions now",
            )
        )

    # cost_per_result_degraded — both rows have cost_per_result, BOTH windows cleared the results floor
    # (the low-volume noise guard), and cpr rose past the degrade knee (higher cpr == worse).
    cur_cpr = cur.get("cost_per_result")
    base_cpr = base.get("cost_per_result")
    if (
        cur_cpr is not None
        and base_cpr
        and _at_or_above(cur.get("results"), thresholds.min_results_floor)
        and _at_or_above(base.get("results"), thresholds.min_results_floor)
    ):
        move = (cur_cpr - base_cpr) / base_cpr
        if move >= thresholds.cost_per_result_degrade_pct:
            flags.append(
                _flag(
                    "cost_per_result_degraded",
                    "high",
                    current=cur_cpr,
                    baseline=base_cpr,
                    delta=cur_cpr - base_cpr,
                    delta_pct=move,
                    detail=f"cost per result up {move * 100:.0f}% vs. the baseline window",
                )
            )

    # cpc_degraded — both rows have cpc, both windows material (spend floor is the volume proxy), cpc up
    # past the degrade knee.
    cur_cpc = cur.get("cpc")
    base_cpc = base.get("cpc")
    if cur_cpc is not None and base_cpc and cur_spend_ok:
        move = (cur_cpc - base_cpc) / base_cpc
        if move >= thresholds.cpc_degrade_pct:
            flags.append(
                _flag(
                    "cpc_degraded",
                    "medium",
                    current=cur_cpc,
                    baseline=base_cpc,
                    delta=cur_cpc - base_cpc,
                    delta_pct=move,
                    detail=f"cost per click up {move * 100:.0f}% vs. the baseline window",
                )
            )

    # ctr_dropped — both rows have ctr, current down past the drop knee (a stalled account has no
    # current ctr — impressions denominator absent — so it never double-fires here).
    cur_ctr = cur.get("ctr")
    base_ctr = base.get("ctr")
    if cur_ctr is not None and base_ctr:
        move = (cur_ctr - base_ctr) / base_ctr
        if move <= -thresholds.ctr_drop_pct:
            flags.append(
                _flag(
                    "ctr_dropped",
                    "medium",
                    current=cur_ctr,
                    baseline=base_ctr,
                    delta=cur_ctr - base_ctr,
                    delta_pct=move,
                    detail=f"click-through rate down {abs(move) * 100:.0f}% vs. the baseline window",
                )
            )

    return flags


def _attention_account_entry(
    current_row: dict[str, Any],
    baseline_row: dict[str, Any] | None,
    flags: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble a flagged/informational account entry from its row + fired flags (severity = max)."""
    max_rank = max(_SEVERITY_RANK.get(f["severity"], 0) for f in flags)
    # Deterministic sort tiebreak: absolute normalized-spend delta between the windows (native
    # fallback), then ad_account_id — a stable total order so ties never reorder run-to-run.
    cur_norm = _normalized_spend(current_row) or 0.0
    base_norm = _normalized_spend(baseline_row) or 0.0
    return {
        "ad_account_id": current_row.get("ad_account_id"),
        "account_id": current_row.get("account_id"),
        "name": current_row.get("name"),
        "currency": current_row.get("currency"),
        "account_status_label": current_row.get("account_status_label"),
        "severity": _RANK_TO_SEVERITY[max_rank],
        "flags": flags,
        "_sort_rank": max_rank,
        "_sort_delta": abs(cur_norm - base_norm),
    }


def flag_accounts_needing_attention(
    reader: "MetaReaderProvider",
    *,
    current_from: str,
    current_to: str,
    account_ids: list[str] | None = None,
    baseline_from: str | None = None,
    baseline_to: str | None = None,
    reporting_currency: str = "USD",
    include_pacing: bool = False,
    include_ad_health: bool = False,
    thresholds: AttentionThresholds | None = None,
    fx_table: FxTable | None = None,
) -> dict[str, Any]:
    """Surface the handful of accounts that changed and need a human's attention right now.

    A **pure post-processor** over :func:`cross_account_performance`: it calls that tool TWICE over the
    same resolved scope — once for the current window, once for a prior baseline window of equal length
    — joins the per-account rows by ``ad_account_id``, and runs :func:`evaluate_attention_flags` over
    each pair. It inherits FX normalization, Simpson's-paradox-safe derived metrics, per-account
    partial-failure isolation, and the deterministic fan-out from the prereq; it introduces no new Meta
    read shape.

    **Baseline resolution.** Both ``baseline_from``/``baseline_to`` omitted -> :func:`prior_window` of
    the current window. Both given -> used verbatim (overlap with the current window is allowed but not
    corrected — the caller's explicit choice). Exactly one given -> ``ValueError`` (ambiguous).

    **Currency discipline.** The FX table is loaded once here and passed to BOTH reads so they
    normalize against one table; an invalid ``reporting_currency`` fails the whole call with the same
    ``ValueError`` contract as the prereq. Percent deltas are native (currency-invariant for one
    account); absolute spend floors compare on ``spend_normalized`` (native fallback for a no-FX
    account).

    **Opt-in budget pacing (``include_pacing``).** Off by default. When ``True``, :func:`pacing_report`
    is called ONCE over the SAME resolved scope / ``reporting_currency`` / shared ``fx_table``, pacing
    the **current** window (``date_from=current_from``, ``date_to=current_to``, ``as_of=current_to``).
    ``as_of=current_to`` makes the period *complete* (``elapsed_fraction == 1``) so
    ``projected_spend == spend_to_date`` and ``variance_pct`` is the realized actual-vs-budgeted
    variance for that window — a well-defined off-pace signal (an operator wanting month-pacing calls
    :func:`pacing_report` directly). Each account whose pacing status is ``over``/``under`` past
    ``thresholds.pacing_variance_pct`` gains a :func:`_budget_pacing_flag` (``budget_pacing_off``),
    which — because it can be the only fired flag — can promote a *clean* or *informational* account
    into ``flagged``. There is no cycle (attention -> pacing -> cross_account_performance; pacing never
    calls attention). An off-pace account unreadable in BOTH attention windows is not surfaced (the
    join skips it — attention is fundamentally a window-comparison tool); it appears only via errors.

    **Read cost (documented, not a bug).** With pacing off this issues ``2x`` the per-account insight
    reads of a single :func:`cross_account_performance` (one fan-out per window) — ~400 reads for a
    200-account scope; a hard regression guard. With ``include_pacing=True`` add pacing's own
    ``~1 + 4N`` (its ``cross_account_performance`` ``1 + N`` + a ``3N`` budget fan-out), of which the
    current-window insight read (``N``) duplicates attention's own current read — an accepted, documented
    duplicate (threading a shared perf into :func:`pacing_report` is a future optimization out of scope).

    **Opt-in ad health (``include_ad_health``).** Off by default. When ``True``, AFTER the flagged list
    is finalized (behavior + pacing flags, severity >= medium), a per-account ad enumeration fans out
    (:func:`fan_out_accounts`) into the ads of **only the flagged accounts** — informational/clean
    accounts are never ad-scanned. Each flagged account's ads yield 0-2 :func:`_ad_health_flags`:
    ``ads_disapproved`` (**high**, an ``effective_status == DISAPPROVED`` count) and
    ``ads_not_delivering`` (**medium**, ACTIVE-configured ads that are neither delivering nor
    deliberately paused nor disapproved). Attaching an ad-health flag can RAISE an account's severity
    (a medium-only account with disapproved ads -> high), so severity is recomputed on each touched
    entry and the flagged list is re-sorted with the existing deterministic key. ``ad_health_scanned_count``
    reports how many accounts were fanned into (present only when ``include_ad_health=True``).
    Per-account ad-enumeration failures isolate into ``errors`` tagged ``{"stage": "ad_health", …}``
    (never fatal to the whole call). **Documented limitation:** a window-over-window clean AND on-pace
    account with disapproved ads never becomes flagged, so it is never ad-scanned and its disapprovals
    stay hidden — the deliberate cost/completeness tradeoff (a full-fleet unconditional ad-disapproval
    scan is a possible future enhancement, out of scope here).

    **Read cost of ad health.** With ``include_ad_health=False`` (default): zero ad reads — a hard
    regression guard. With ``include_ad_health=True``: ``+ len(flagged)`` ad enumerations (each may
    paginate). Because the fan-out is gated on the flagged set (never the full scope), a fleet where
    only 3 of 200 accounts surface pays 3 ad enumerations, not 200.

    **Not in scope:** account-level health is covered by the ``account_status_alert`` flag at zero
    extra read cost. Budget pacing and ad health are both off by default; pass ``include_pacing=True``
    / ``include_ad_health=True`` to fold them in.
    """
    thr = thresholds if thresholds is not None else AttentionThresholds.defaults()

    # Baseline resolution — validate BEFORE any read so an ambiguous/invalid window fails fast.
    if baseline_from is None and baseline_to is None:
        resolved_from, resolved_to = prior_window(current_from, current_to)
    elif baseline_from is not None and baseline_to is not None:
        resolved_from, resolved_to = baseline_from, baseline_to
    else:
        raise ValueError(
            "baseline_from and baseline_to must be provided together (or both omitted to derive the "
            "immediately-preceding window of equal length)."
        )

    # Load the FX table once and share it across both reads so a single table validates
    # reporting_currency and normalizes both windows (an invalid currency raises inside the first call).
    table = fx_table if fx_table is not None else load_fx_table()

    current = cross_account_performance(
        reader,
        date_from=current_from,
        date_to=current_to,
        account_ids=account_ids,
        reporting_currency=reporting_currency,
        fx_table=table,
    )
    baseline = cross_account_performance(
        reader,
        date_from=resolved_from,
        date_to=resolved_to,
        account_ids=account_ids,
        reporting_currency=reporting_currency,
        fx_table=table,
    )

    cur_rows = {r["ad_account_id"]: r for r in current["accounts"]}
    base_rows = {r["ad_account_id"]: r for r in baseline["accounts"]}

    # Merge both reads' errors, tagging each with the window it came from. An account with NO row in a
    # window (a genuine read failure) cannot be compared -> excluded from flagging (it surfaces here).
    # A no-FX account carries a native row AND an errors entry: it is still readable/flagged (native
    # spend feeds its floor), and its FX-gap note is surfaced here too.
    errors: list[dict[str, Any]] = []
    for window, read in (("current", current), ("baseline", baseline)):
        for entry in read["errors"]:
            errors.append(
                {
                    "ad_account_id": entry.get("ad_account_id"),
                    "window": window,
                    "error": entry.get("error"),
                }
            )

    # Opt-in budget pacing. Pace the CURRENT window with as_of=current_to (elapsed_fraction == 1, so
    # the projection equals the realized spend) over the SAME scope / currency / shared fx_table. Only
    # over/under accounts that clear the pacing-variance knee yield a flag; pacing errors are merged
    # tagged stage:"pacing" (distinct from the window-tagged attention errors above).
    pacing_flag_by_id: dict[str, dict[str, Any]] = {}
    if include_pacing:
        pacing = pacing_report(
            reader,
            date_from=current_from,
            date_to=current_to,
            account_ids=account_ids,
            as_of=current_to,
            reporting_currency=reporting_currency,
            fx_table=table,
        )
        for entry in pacing["accounts"]:
            pacing_flag = _budget_pacing_flag(entry, thr)
            if pacing_flag is not None:
                pacing_flag_by_id[entry["ad_account_id"]] = pacing_flag
        for entry in pacing["errors"]:
            errors.append(
                {
                    "ad_account_id": entry.get("ad_account_id"),
                    "stage": "pacing",
                    "error": entry.get("error"),
                }
            )

    flagged: list[dict[str, Any]] = []
    informational: list[dict[str, Any]] = []
    clean_count = 0

    # Iterate in current-window scope order (deterministic); evaluate only accounts readable in BOTH
    # windows. The join/sort is order-deterministic, so identical inputs -> identical buckets and order.
    # Bucketing follows behavior-flag evaluation AND the pacing-flag append (two-phase): a pacing flag
    # can be the ONLY fired flag, promoting an otherwise-clean/informational account into flagged.
    for ad_account_id, current_row in cur_rows.items():
        baseline_row = base_rows.get(ad_account_id)
        if baseline_row is None:
            # Read-failed in the baseline window -> already surfaced in errors; cannot compare.
            continue
        flags = evaluate_attention_flags(current_row, baseline_row, thr)
        pacing_flag = pacing_flag_by_id.get(ad_account_id)
        if pacing_flag is not None:
            flags = [*flags, pacing_flag]
        if not flags:
            clean_count += 1
            continue
        entry = _attention_account_entry(current_row, baseline_row, flags)
        if entry["_sort_rank"] >= _MEDIUM_RANK:
            flagged.append(entry)
        else:
            informational.append(entry)

    # Opt-in ad-health scan. GATED on the finalized flagged set (never the full scope): fan out into
    # the ads of ONLY the flagged accounts, so a 200-account fleet where 3 surfaced pays 3 ad reads.
    # Ad-health flags only ever attach to already-flagged accounts, so no re-bucketing is needed — but
    # they CAN raise an entry's severity (medium -> high), so severity + the sort rank are recomputed
    # here, BEFORE the sort below, so the promotion is reflected deterministically. Per-account read
    # failures isolate into errors tagged stage:"ad_health" (never fatal), mirroring the pacing fan-out.
    ad_health_scanned_count = 0
    if include_ad_health and flagged:
        flagged_by_id = {e["ad_account_id"]: e for e in flagged}
        flagged_account_ids = list(flagged_by_id.keys())
        ad_health_scanned_count = len(flagged_account_ids)

        def read_ads(ad_account_id: str) -> list[dict[str, Any]]:
            # Materialize the lazy iterator: _ad_health_flags scans it more than once (len + loop).
            return list(
                reader.iter_paginated(
                    f"/{ad_account_id}/ads",
                    params={"fields": ",".join(_AD_HEALTH_FIELDS), "limit": 200},
                )
            )

        # Main-thread assembly over the input-ordered fan-out results -> deterministic regardless of
        # which worker finished first (same discipline as the account-level fan-outs above).
        for ad_account_id, ads, error in fan_out_accounts(read_ads, flagged_account_ids):
            if error is not None:
                errors.append(
                    {"stage": "ad_health", "ad_account_id": ad_account_id, "error": error}
                )
                continue
            health_flags = _ad_health_flags(ads, thr)
            if not health_flags:
                continue
            entry = flagged_by_id[ad_account_id]
            entry["flags"] = [*entry["flags"], *health_flags]
            max_rank = max(_SEVERITY_RANK.get(f["severity"], 0) for f in entry["flags"])
            entry["_sort_rank"] = max_rank
            entry["severity"] = _RANK_TO_SEVERITY[max_rank]

    # flagged: (severity desc, |normalized-spend delta| desc, ad_account_id asc) — a stable total order.
    flagged.sort(key=lambda e: (-e["_sort_rank"], -e["_sort_delta"], e["ad_account_id"] or ""))
    informational.sort(key=lambda e: (e["ad_account_id"] or ""))
    for entry in (*flagged, *informational):
        del entry["_sort_rank"]
        del entry["_sort_delta"]

    result: dict[str, Any] = {
        "current_window": {"date_from": current_from, "date_to": current_to},
        "baseline_window": {"date_from": resolved_from, "date_to": resolved_to},
        "reporting_currency": current["reporting_currency"],
        "fx_as_of": current["fx_as_of"],
        "fx_note": current["fx_note"],
        "account_count": current["account_count"],
        "reachable_count": current["reachable_count"],
        "flagged": flagged,
        "informational": informational,
        "clean_count": clean_count,
        "errors": errors,
    }
    # Cost legibility: how many accounts the ad-health fan-out issued a read for. Present ONLY when
    # the opt-in is on, so the default path stays byte-identical to the pre-ad-health output.
    if include_ad_health:
        result["ad_health_scanned_count"] = ad_health_scanned_count
    if current.get("note"):
        result["note"] = current["note"]
    return result


# --------------------------------------------------------------------------- #
# PACING REPORT: is each account on track to spend its budget for the period?
#
# Unlike account_benchmark / flag_accounts_needing_attention (pure post-processors that add NO new
# Meta read shape), pacing genuinely needs a SECOND data surface — the account's *budget config*
# (campaign/adset daily & lifetime budgets + the account spend cap). Spend-to-date comes from the
# existing insights read; budget config is a new per-account campaign+adset+account read. So this is a
# TWO-SOURCE JOIN, not a post-processor:
#
#   1. Spend-to-date + FX + scope: one cross_account_performance over [date_from, effective_as_of] —
#      inherits scope resolution, one insights row per account, native + normalized spend, currency,
#      account_status_label, the shared fx_table, and per-account error isolation, all for free.
#   2. Budget config: a SECOND fan_out_accounts over the accounts that read OK in step 1, each reading
#      list_campaigns + list_adsets (budget fields only) + get_account (spend_cap/amount_spent) and
#      computing the CBO-deduplicated ACTIVE daily-budget sum for that account.
#   3. Join + project + classify by ad_account_id -> a scope view + worst-pacer shortlists.
#
# READ COST (documented, accepted — same posture as the attention tool's 2x note): step 2 issues 3
# extra reads per readable account on top of cross_account_performance's 1 + N, so a scope of N
# accounts costs ~1 + 4N reads. A single combined per-account read is a future optimization, out of
# scope here.
# --------------------------------------------------------------------------- #

# Budget-only field lists for the step-2 config read. Deliberately NOT the heavier control.*_FIELDS
# (which carry targeting/objective) — pacing needs only the budget shape + status + parentage.
PACING_CAMPAIGN_FIELDS: list[str] = [
    "id",
    "effective_status",
    "daily_budget",
    "lifetime_budget",
    "start_time",
    "stop_time",
]
PACING_ADSET_FIELDS: list[str] = [
    "id",
    "campaign_id",
    "effective_status",
    "daily_budget",
    "lifetime_budget",
    "start_time",
    "stop_time",
]
# Cap fields fetched per account in the budget worker. The perf["accounts"] rows carry only
# account_id/name/currency/account_status[_label] (see the row built above), so the spend cap +
# lifetime spend must be fetched here rather than bloating DEFAULT_AD_ACCOUNT_FIELDS.
PACING_ACCOUNT_FIELDS: list[str] = ["currency", "spend_cap", "amount_spent"]

# Canonical status enum, ordered — the rollup's status_counts is initialised from this so every count
# is present (0 when unused) and the output is deterministic. The first five are classify_pacing's
# documented enum; ``budget_unread`` is the orchestration-only status for a step-2 read failure (kept
# distinct from a genuinely uncapped account so a failed read is never reported as "no budget set").
_PACING_STATUSES: tuple[str, ...] = (
    "over",
    "under",
    "on_track",
    "no_budget_set",
    "budget_not_projectable",
    "account_inactive",
    "not_started",
    "budget_unread",
)
_PACING_PROJECTABLE: frozenset[str] = frozenset({"over", "under", "on_track"})


def pacing_period(date_from: str, date_to: str, as_of: str) -> dict[str, Any]:
    """The clock-free pacing arithmetic for one period measured *through* ``as_of``.

    ``date_from..date_to`` is the FULL reporting period (inclusive, ``YYYY-MM-DD``); ``as_of`` is the
    day spend is measured through. This separates "the period we pace against" from "how far into it
    we are," so the only clock touch in the whole tool is :func:`pacing_report`'s ``as_of=None`` ->
    today default (this helper always takes an explicit ``as_of`` and is fully unit-testable).

    - ``effective_as_of`` = ``as_of`` clamped to ``[date_from - 1 day, date_to]``.
    - ``total_days`` = ``(date_to - date_from).days + 1``.
    - ``elapsed_days`` = ``(effective_as_of - date_from).days + 1`` clamped to ``[0, total_days]``.
    - ``elapsed_fraction`` = ``elapsed_days / total_days``.

    So ``as_of`` before the period -> ``elapsed_fraction == 0`` (not started, no divide-by-zero);
    ``as_of`` at/after ``date_to`` -> ``elapsed_fraction == 1`` (completed, projection == actual).
    Raises :class:`ValueError` on unparseable dates (via :meth:`date.fromisoformat`) or
    ``date_from > date_to``.
    """
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if start > end:
        raise ValueError(
            f"date_from ({date_from}) is after date_to ({date_to}); the reporting period is empty."
        )
    as_of_date = date.fromisoformat(as_of)

    floor = start - timedelta(days=1)
    if as_of_date < floor:
        effective = floor
    elif as_of_date > end:
        effective = end
    else:
        effective = as_of_date

    total_days = (end - start).days + 1
    elapsed_days = max(0, min((effective - start).days + 1, total_days))
    return {
        "total_days": total_days,
        "elapsed_days": elapsed_days,
        "elapsed_fraction": elapsed_days / total_days,
        "effective_as_of": effective.isoformat(),
    }


def project_spend(spend_to_date: float, elapsed_fraction: float) -> float | None:
    """Linear end-of-period projection: ``spend_to_date / elapsed_fraction``.

    Returns ``None`` when ``elapsed_fraction <= 0`` (the not-started guard against divide-by-zero) so
    the caller classifies the account ``not_started`` rather than projecting. A completed period
    (``elapsed_fraction == 1``) projects to exactly ``spend_to_date`` (actuals).
    """
    if elapsed_fraction <= 0:
        return None
    return spend_to_date / elapsed_fraction


def _minor_to_major(value: Any, currency: str = "USD") -> float | None:
    """Meta budget/cap minor units -> major currency units; ``None`` for missing/blank.

    The divisor is ISO-4217 **currency-aware**: ``value / 10 ** minor_unit_exponent(currency)`` — /100
    for the ~150 two-decimal currencies (USD/EUR/GBP/…), /1 for zero-decimal (JPY, KRW, …), /1000 for
    three-decimal (BHD, KWD, …). ``currency`` defaults to ``"USD"`` (2-decimal) so a bare call is
    unchanged. An unrecognized/blank/``"UNKNOWN"`` code falls through to the 2-decimal default; that
    fallback is an *assumption* surfaced by the caller (see :func:`minor_unit_exponent_is_known`), not
    a silent per-currency guess.
    """
    num = _number(value)
    if num is None:
        return None
    return num / (10 ** minor_unit_exponent(currency))


def summarize_account_budget(
    campaigns: list[dict[str, Any]], adsets: list[dict[str, Any]], currency: str = "USD"
) -> dict[str, Any]:
    """CBO-deduplicated ACTIVE budget for an account: ``{active_daily, lifetime_total,
    lifetime_entities}`` (major units).

    Native minor units in (Meta ``daily_budget`` / ``lifetime_budget``), native MAJOR units out —
    converted via the ISO-4217 currency-aware :func:`_minor_to_major`. ``currency`` defaults to
    ``"USD"`` (2-decimal) so a bare call is unchanged; the pacing loop threads the real per-account
    currency so zero-/3-decimal accounts (JPY, KWD, …) get the right divisor. Only
    ``effective_status == "ACTIVE"`` entities count — a paused campaign/adset does not
    deliver. Per **ACTIVE** campaign, the precedence (this is the double-counting guard):

    - campaign ``daily_budget > 0`` -> **CBO daily**: add the campaign daily to ``active_daily`` and
      **ignore its adsets** (their budgets are null under CBO; guarded anyway — naive summing would
      double-count here).
    - elif campaign ``lifetime_budget > 0`` -> **CBO lifetime**: add to ``lifetime_total``; ignore adsets.
    - else (**non-CBO** campaign) -> for each **ACTIVE** adset under it: adset ``daily_budget > 0`` ->
      ``active_daily``; elif adset ``lifetime_budget > 0`` -> ``lifetime_total``.

    Adsets whose parent campaign is not ACTIVE are ignored (the parent gates delivery). This mirrors
    :func:`control.classify_adset_budget`'s adset-daily-first-else-campaign shape rather than a
    contradictory rule.

    Lifetime budgets are additionally emitted as ``lifetime_entities`` — one entry per ACTIVE
    lifetime-owning entity, ``{"lifetime_budget": float (major units), "start_time": str | None,
    "stop_time": str | None}`` with the schedule strings verbatim from Meta — so the caller can
    prorate each pot across the overlap of its own schedule with the reporting window (see
    :func:`lifetime_pacing`). The lifetime entity is whichever level owns the budget under the CBO
    precedence above: the campaign for a CBO-lifetime campaign, the adset for a non-CBO adset-lifetime.
    ``lifetime_total`` (the raw Σ, still returned) drives the reported context figure; the schedules
    are what proration needs (different entities have different runs, so the sum alone is insufficient).
    """
    active_daily = 0.0
    lifetime_total = 0.0
    lifetime_entities: list[dict[str, Any]] = []

    # Group ACTIVE adsets by parent campaign id for O(1) lookup inside the non-CBO branch.
    active_adsets_by_campaign: dict[str, list[dict[str, Any]]] = {}
    for adset in adsets:
        if str(adset.get("effective_status") or "").upper() != "ACTIVE":
            continue
        campaign_id = str(adset.get("campaign_id") or "")
        active_adsets_by_campaign.setdefault(campaign_id, []).append(adset)

    for campaign in campaigns:
        if str(campaign.get("effective_status") or "").upper() != "ACTIVE":
            continue  # paused campaign -> no delivery; its adsets are gated off too.
        campaign_id = str(campaign.get("id") or "")
        camp_daily = _minor_to_major(campaign.get("daily_budget"), currency) or 0.0
        camp_lifetime = _minor_to_major(campaign.get("lifetime_budget"), currency) or 0.0

        if camp_daily > 0:
            active_daily += camp_daily  # CBO daily — ignore adsets (double-count guard).
            continue
        if camp_lifetime > 0:
            lifetime_total += camp_lifetime  # CBO lifetime — ignore adsets.
            lifetime_entities.append(
                {
                    "lifetime_budget": camp_lifetime,
                    "start_time": campaign.get("start_time"),
                    "stop_time": campaign.get("stop_time"),
                }
            )
            continue
        # Non-CBO campaign: budget lives on each ACTIVE adset (adset daily first, else adset lifetime).
        for adset in active_adsets_by_campaign.get(campaign_id, []):
            adset_daily = _minor_to_major(adset.get("daily_budget"), currency) or 0.0
            adset_lifetime = _minor_to_major(adset.get("lifetime_budget"), currency) or 0.0
            if adset_daily > 0:
                active_daily += adset_daily
            elif adset_lifetime > 0:
                lifetime_total += adset_lifetime
                lifetime_entities.append(
                    {
                        "lifetime_budget": adset_lifetime,
                        "start_time": adset.get("start_time"),
                        "stop_time": adset.get("stop_time"),
                    }
                )

    return {
        "active_daily": active_daily,
        "lifetime_total": lifetime_total,
        "lifetime_entities": lifetime_entities,
    }


def _overlap_days(a_start: date, a_end: date, b_start: date, b_end: date) -> int:
    """Inclusive-day overlap of ``[a_start, a_end]`` with ``[b_start, b_end]``; 0 if disjoint."""
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0, (end - start).days + 1)


def _parse_schedule_date(value: Any) -> date | None:
    """Leading ``YYYY-MM-DD`` of a Meta ISO time string -> :class:`date`; ``None`` if missing/blank/bad.

    Timezone-agnostic calendar days, consistent with the rest of the pacing arithmetic: we take
    ``str(value)[:10]`` and parse it, discarding any ``T…`` time/offset suffix Meta appends.
    """
    if value is None:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def lifetime_pacing(
    lifetime_entities: list[dict[str, Any]],
    *,
    date_from: str,
    date_to: str,
    effective_as_of: str,
) -> dict[str, Any]:
    """Prorate a set of lifetime-budget entities across the reporting window.

    Meta paces a lifetime budget over the entity's own ``start_time..stop_time`` schedule, not the
    arbitrary reporting window. To fold it into the period verdict we prorate each pot by the fraction
    of its schedule that falls inside the window: ``lifetime_i * overlap_i / schedule_total_i``, where
    the overlap is inclusive-day (mirroring :func:`pacing_period`'s ``(end - start).days + 1``).

    Returns aggregated major-unit figures over all *projectable* entities::

        {
          "period_budget":    float,  # Σ lifetime_i * overlap_full_i   / schedule_total_i
          "expected_to_date": float,  # Σ lifetime_i * overlap_todate_i / schedule_total_i
          "n_entities":       int,    # total lifetime entities considered
          "n_projectable":    int,    # entities with a valid schedule AND overlap_full > 0
        }

    where ``overlap_full`` is the overlap of the entity schedule with ``[date_from, date_to]`` and
    ``overlap_todate`` its overlap with ``[date_from, effective_as_of]``. An entity is non-projectable
    (contributes 0) when its lifetime budget is missing/<=0, either schedule bound is
    blank/unparseable (open-ended lifetime budgets and missing starts fall here — a data anomaly, since
    Meta requires an end time), ``stop_time <= start_time`` (bad data), or the schedule does not
    overlap the window at all. An empty / all-non-projectable input returns zeros — the caller then
    keeps ``budget_not_projectable``. All dates are explicit (clock-free), matching
    :func:`pacing_period`'s testability posture.
    """
    window_start = date.fromisoformat(date_from)
    window_end = date.fromisoformat(date_to)
    as_of_end = date.fromisoformat(effective_as_of)

    period_budget = 0.0
    expected_to_date = 0.0
    n_projectable = 0

    for entity in lifetime_entities:
        lifetime_budget = entity.get("lifetime_budget")
        if lifetime_budget is None or lifetime_budget <= 0:
            continue
        start = _parse_schedule_date(entity.get("start_time"))
        stop = _parse_schedule_date(entity.get("stop_time"))
        if start is None or stop is None:
            continue  # open-ended / missing bound -> non-projectable.
        schedule_total = (stop - start).days + 1
        if schedule_total <= 0:
            continue  # stop <= start -> bad data, non-projectable.
        overlap_full = _overlap_days(start, stop, window_start, window_end)
        if overlap_full <= 0:
            continue  # schedule wholly outside the window -> non-projectable.
        n_projectable += 1
        period_budget += lifetime_budget * overlap_full / schedule_total
        # overlap_todate clips against [date_from, effective_as_of]; when the window has not started
        # effective_as_of is date_from - 1, so this range is empty and the contribution is 0.
        overlap_todate = _overlap_days(start, stop, window_start, as_of_end)
        expected_to_date += lifetime_budget * overlap_todate / schedule_total

    return {
        "period_budget": period_budget,
        "expected_to_date": expected_to_date,
        "n_entities": len(lifetime_entities),
        "n_projectable": n_projectable,
    }


def classify_pacing(
    *,
    elapsed_fraction: float,
    account_status_label: str | None,
    active_daily_budget: float,
    lifetime_budget_total: float,
    spend_cap: float | None,
    period_budget: float,
    projected_spend: float | None,
    tolerance: float = PACING_ON_TRACK_TOLERANCE_PCT,
) -> dict[str, Any]:
    """Pure status + variance for one account, checked in the documented order.

    1. ``not_started`` — global ``elapsed_fraction <= 0`` (as_of before the period). No projection.
    2. ``account_inactive`` — ``account_status_label != "ACTIVE"`` (a paused account is not
       "under-pacing"). Excluded from the over/under math; spend-to-date is still reported.
    3. ``no_budget_set`` — no active daily budget, no lifetime budget, no spend cap (uncapped / free
       delivery). Excluded from over/under; reported explicitly (never counted as under-pacing).
    4. ``budget_not_projectable`` — a lifetime/cap-only account with no projectable schedule overlap
       (combined ``period_budget <= 0`` or no projection): a spend-cap-only account, or a lifetime-only
       account whose entities are all open-ended / non-overlapping / not-yet-started. Excluded from
       over/under. (A lifetime budget whose schedule *does* overlap the window is prorated by the caller
       and folded into ``period_budget``, so such an account gets a real over/under/on_track instead.)
    5. ``over`` / ``under`` / ``on_track`` — ``variance_pct = (projected_spend - period_budget) /
       period_budget``; ``over`` if ``> +tolerance``, ``under`` if ``< -tolerance``, else ``on_track``.

    ``variance_pct`` is a ratio of two same-currency figures -> **FX-invariant** (compute once from
    native; the normalized twins give the same value). Returned as ``None`` for statuses 1-4.
    """
    if elapsed_fraction <= 0:
        return {"status": "not_started", "variance_pct": None}
    if account_status_label != "ACTIVE":
        return {"status": "account_inactive", "variance_pct": None}

    has_cap = spend_cap is not None and spend_cap > 0
    if active_daily_budget <= 0 and lifetime_budget_total <= 0 and not has_cap:
        return {"status": "no_budget_set", "variance_pct": None}
    if period_budget <= 0 or projected_spend is None:
        # A lifetime/cap-only account with no projectable schedule overlap (or a defensively-zero
        # combined period budget) can't be projected. The caller passes the COMBINED daily + prorated
        # lifetime period_budget / projection, so dropping the old ``active_daily_budget <= 0`` clause
        # lets a projectable lifetime-only account through while a cap-only account (period_budget == 0)
        # still lands here. For a daily account period_budget = active_daily * total_days, so
        # period_budget > 0 <=> active_daily > 0 — daily outcomes are unchanged.
        return {"status": "budget_not_projectable", "variance_pct": None}

    variance_pct = (projected_spend - period_budget) / period_budget
    if variance_pct > tolerance:
        status = "over"
    elif variance_pct < -tolerance:
        status = "under"
    else:
        status = "on_track"
    return {"status": status, "variance_pct": variance_pct}


def _pacing_shortlist_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """The compact per-account row carried in the rollup's worst-pacer shortlists."""
    return {
        "ad_account_id": entry["ad_account_id"],
        "name": entry["name"],
        "variance_pct": entry["variance_pct"],
        "projected_spend_normalized": entry["projected_spend_normalized"],
        "period_budget_normalized": entry["period_budget_normalized"],
    }


def pacing_report(
    reader: "MetaReaderProvider",
    *,
    date_from: str,
    date_to: str,
    account_ids: list[str] | None = None,
    as_of: str | None = None,
    reporting_currency: str = "USD",
    fx_table: FxTable | None = None,
) -> dict[str, Any]:
    """Is each account on track to spend its configured budget for the reporting period?

    A **two-source join** (NOT a pure post-processor — budget config is not in the insights row):

    1. **Spend-to-date** — one :func:`cross_account_performance` over ``[date_from, effective_as_of]``
       resolves scope, reads one insights row per account, and yields native + normalized ``spend``,
       ``currency``, ``account_status_label``, the shared ``fx_table``, and per-account error
       isolation — all inherited.
    2. **Budget config** — a second :func:`fan_out_accounts` over the accounts that read OK in step 1,
       each reading ``list_campaigns`` + ``list_adsets`` (budget fields only) + ``get_account``
       (``spend_cap`` / ``amount_spent``) and computing the CBO-deduplicated ACTIVE daily-budget sum
       via :func:`summarize_account_budget`.
    3. **Join + project + classify** by ``ad_account_id``.

    **Dates (the three-date problem).** ``date_from..date_to`` is the full period (e.g. a month);
    ``as_of`` is the day spend is measured through (``None`` -> today, UTC — the only clock touch;
    tests always pass an explicit ``as_of``). :func:`pacing_period` clamps and derives
    ``elapsed_fraction``; :func:`project_spend` extrapolates ``spend_to_date / elapsed_fraction``.

    **Authoritative period budget = ACTIVE daily-budget sum (CBO-deduped) x total_days, plus prorated
    lifetime budgets.** The account spend cap is a *lifetime* ceiling, so it is reported as context,
    **never the denominator**. A **lifetime budget** is paced by Meta over the entity's own
    ``start_time..stop_time`` schedule, not this window; :func:`lifetime_pacing` prorates each pot by
    the inclusive-day overlap of that schedule with the window (``lifetime * overlap / schedule_total``)
    and the result folds additively into ``period_budget`` / expected-to-date, so a lifetime or mixed
    account earns a real over/under/on_track verdict. ``budget_not_projectable`` now means only a
    residual: an open-ended lifetime budget (no ``stop_time``), a schedule that does not overlap the
    window, or a spend-cap-only account — nothing with a projectable schedule falls here. Daily-only
    accounts keep the literal ``daily * total_days`` computation (byte-identical output).

    **Units.** Budget/cap/amount_spent are minor units; insights ``spend`` is major units.
    :func:`_minor_to_major`'s divisor is ISO-4217 currency-aware (``10 ** minor_unit_exponent`` —
    2/0/3-decimal, so JPY/KRW and BHD/KWD convert correctly, not 100x off). An **unrecognized** currency
    code assumes 2 decimals and that assumption is surfaced in the report ``note`` (never a silent
    guess). Currency discipline: a budget is only ever compared to spend in the SAME (native) currency
    per account; only the rollup uses normalized figures.

    **Read cost** ~``1 + 4N`` for an N-account scope (``cross_account_performance``'s ``1 + N`` plus 3
    per readable account) — documented, accepted; a single combined per-account read is a future
    optimization. Budget pacing lives HERE — :func:`flag_accounts_needing_attention` deliberately does
    not read budget config (see its ``NOTE``).

    **Errors.** Step-1 insight failures and no-FX accounts flow through
    :func:`cross_account_performance`'s ``errors`` verbatim (an account that failed step 1 gets no
    step-2 read). Step-2 budget failures are isolated per account into an ``errors`` entry tagged
    ``{"stage": "budget", …}`` and the account is reported ``status: "budget_unread"`` (distinct from a
    genuinely uncapped ``no_budget_set``). An invalid ``reporting_currency`` (absent from the FX table)
    is a whole-call ``ValueError`` inherited from the prereq; ``date_from > date_to`` raises before any
    read. ``fx_table`` is a test-only seam (not exposed to the LLM).
    """
    # Fail fast on an empty period and resolve the pacing arithmetic BEFORE any read. ``as_of=None``
    # -> today (UTC) is the single clock touch in this tool.
    effective_as_of_input = as_of or datetime.now(tz=timezone.utc).date().isoformat()
    period = pacing_period(date_from, date_to, effective_as_of_input)
    total_days = period["total_days"]
    elapsed_fraction = period["elapsed_fraction"]
    effective_as_of = period["effective_as_of"]

    reporting = str(reporting_currency or "").strip().upper()
    table = fx_table if fx_table is not None else load_fx_table()

    # Spend-to-date read window: never invert. When the period has not started (effective_as_of is the
    # day before date_from), read a single [date_from, date_from] window — the projection is suppressed
    # to None anyway, so the reported spend is immaterial. cross_account_performance validates the FX
    # table / reporting_currency (raising ValueError on an unknown currency — the inherited contract).
    read_to = date_from if elapsed_fraction <= 0 else effective_as_of
    perf = cross_account_performance(
        reader,
        date_from=date_from,
        date_to=read_to,
        account_ids=account_ids,
        reporting_currency=reporting,
        fx_table=table,
    )

    # Step 2: budget fan-out over the accounts that read OK in step 1 (in scope order). One worker per
    # account issues all three reads so the fan-out stays 3 reads/account and their failures isolate
    # together; an account that failed step 1 is absent here (never double-reported).
    perf_account_ids = [row["ad_account_id"] for row in perf["accounts"]]

    def read_budget(
        ad_account_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        campaigns = reader.list_campaigns(ad_account_id, fields=PACING_CAMPAIGN_FIELDS)
        adsets = reader.list_adsets(ad_account_id, fields=PACING_ADSET_FIELDS)
        account = reader.get_account(ad_account_id, fields=PACING_ACCOUNT_FIELDS)
        return campaigns, adsets, account

    budget_results = fan_out_accounts(read_budget, perf_account_ids)
    budget_by_id: dict[str, tuple[Any, Any, Any]] = {}
    errors: list[dict[str, Any]] = list(perf["errors"])  # step-1 errors verbatim (no-FX + read fails)
    for ad_account_id, payload, error in budget_results:
        if error is not None:
            errors.append({"stage": "budget", "ad_account_id": ad_account_id, "error": error})
            continue
        budget_by_id[ad_account_id] = payload

    accounts: list[dict[str, Any]] = []
    # Distinct currency codes whose minor-unit exponent we had to *assume* (fell through to the
    # 2-decimal default because the code is unrecognized). Surfaced in the report note so the
    # assumption is never silent. Collected only where a divisor is actually applied (budget read OK).
    assumed_currencies: set[str] = set()
    # Main-thread assembly over the scope-ordered perf rows -> deterministic output.
    for row in perf["accounts"]:
        ad_account_id = row["ad_account_id"]
        currency = row.get("currency") or "UNKNOWN"
        status_label = row.get("account_status_label")
        spend_native = row.get("spend")
        spend_to_date = spend_native if spend_native is not None else 0.0
        spend_to_date_norm = row.get("spend_normalized")  # None when the currency had no FX rate.

        base_entry: dict[str, Any] = {
            "ad_account_id": ad_account_id,
            "account_id": row.get("account_id"),
            "name": row.get("name"),
            "currency": currency,
            "account_status_label": status_label,
            "spend_to_date": spend_to_date,
            "spend_to_date_normalized": spend_to_date_norm,
        }

        payload = budget_by_id.get(ad_account_id)
        if payload is None:
            # Step-2 read failed -> distinct from a genuinely uncapped account. Budget-derived fields
            # are unknown (None); the account is excluded from the over/under math.
            accounts.append(
                {
                    **base_entry,
                    "period_budget": None,
                    "period_budget_normalized": None,
                    "elapsed_fraction": elapsed_fraction,
                    "projected_spend": None,
                    "projected_spend_normalized": None,
                    "status": "budget_unread",
                    "variance_pct": None,
                    "active_daily_budget": None,
                    "lifetime_budget_total": None,
                    "spend_cap": None,
                    "amount_spent": None,
                }
            )
            continue

        campaigns, adsets, account = payload
        # This account's minor-unit divisor rides on ``currency``; flag it if we're only assuming.
        if not minor_unit_exponent_is_known(currency):
            assumed_currencies.add(currency)
        budget = summarize_account_budget(campaigns, adsets, currency)
        active_daily = budget["active_daily"]
        lifetime_total = budget["lifetime_total"]
        spend_cap = _minor_to_major(account.get("spend_cap"), currency)
        if spend_cap is not None and spend_cap <= 0:
            spend_cap = None  # 0 / absent -> uncapped.
        amount_spent = _minor_to_major(account.get("amount_spent"), currency)

        # Prorate any ACTIVE lifetime budgets across the overlap of their own schedule with the window,
        # then fold them additively into the daily period budget / expected-to-date. Only accounts with
        # a projectable lifetime overlap use the combined form; daily-only (and lifetime-only-but-not-
        # projectable) accounts keep the LITERAL existing daily computation so their output stays
        # byte-identical (the combined ``spend * period_budget / expected`` form can differ in the last
        # ULP from ``spend / elapsed_fraction``).
        lifetime = lifetime_pacing(
            budget["lifetime_entities"],
            date_from=date_from,
            date_to=date_to,
            effective_as_of=effective_as_of,
        )
        daily_period_budget = active_daily * total_days
        if lifetime["period_budget"] > 0:
            period_budget = daily_period_budget + lifetime["period_budget"]
            expected_to_date = (
                daily_period_budget * elapsed_fraction + lifetime["expected_to_date"]
            )
            projected = (
                spend_to_date * period_budget / expected_to_date
                if expected_to_date > 0
                else None
            )
        else:
            period_budget = daily_period_budget  # byte-identical daily-only path.
            projected = project_spend(spend_to_date, elapsed_fraction)

        verdict = classify_pacing(
            elapsed_fraction=elapsed_fraction,
            account_status_label=status_label,
            active_daily_budget=active_daily,
            lifetime_budget_total=lifetime_total,
            spend_cap=spend_cap,
            period_budget=period_budget,
            projected_spend=projected,
        )

        # Normalized twins: convert native period budget + projection into the reporting currency; a
        # no-FX account keeps native figures only (None twins, already surfaced in step-1 errors).
        if table.has(currency):
            period_budget_norm = table.convert(
                period_budget, from_currency=currency, to_currency=reporting
            )
            projected_norm = (
                table.convert(projected, from_currency=currency, to_currency=reporting)
                if projected is not None
                else None
            )
        else:
            period_budget_norm = None
            projected_norm = None

        accounts.append(
            {
                **base_entry,
                "period_budget": period_budget,
                "period_budget_normalized": period_budget_norm,
                "elapsed_fraction": elapsed_fraction,
                "projected_spend": projected,
                "projected_spend_normalized": projected_norm,
                "status": verdict["status"],
                "variance_pct": verdict["variance_pct"],
                "active_daily_budget": active_daily,
                "lifetime_budget_total": lifetime_total,
                "spend_cap": spend_cap,
                "amount_spent": amount_spent,
            }
        )

    rollup = _build_pacing_rollup(accounts, reporting)

    result: dict[str, Any] = {
        "date_from": date_from,
        "date_to": date_to,
        "as_of": effective_as_of,
        "reporting_currency": perf["reporting_currency"],
        "fx_as_of": perf["fx_as_of"],
        "fx_note": perf["fx_note"],
        "total_days": total_days,
        "account_count": perf["account_count"],
        "accounts": accounts,
        "rollup": rollup,
        "errors": errors,
    }

    notes: list[str] = []
    if perf.get("note"):
        notes.append(perf["note"])
    if elapsed_fraction <= 0:
        notes.append(
            f"reporting period ({date_from}..{date_to}) has not started as of {effective_as_of}; "
            "every account is not_started and no projection is computed."
        )
    if assumed_currencies:
        codes = ", ".join(sorted(assumed_currencies))
        notes.append(
            f"assumed 2-decimal minor units for unrecognized currency codes: {codes}."
        )
    if notes:
        result["note"] = " ".join(notes)
    return result


def _build_pacing_rollup(accounts: list[dict[str, Any]], reporting: str) -> dict[str, Any]:
    """Roll per-account pacing entries up to a scope view + worst-pacer shortlists (deterministic).

    - ``total_period_budget_normalized`` / ``total_projected_normalized`` sum ONLY projectable
      (``over``/``under``/``on_track``) accounts that also had an FX rate (normalized twins present);
      ``overall_variance_pct`` is derived from those totals (``None`` when nothing qualified).
    - ``status_counts`` counts EVERY account by status (all enum keys present, 0 when unused).
    - ``worst_over_pacers`` / ``worst_under_pacers`` = projectable accounts sorted by ``variance_pct``
      desc / asc (native variance is FX-invariant, so no-FX accounts are eligible), tiebroken by
      ``ad_account_id`` asc, capped at ``PACING_SHORTLIST_LIMIT``.
    - ``excluded_from_rollup`` = accounts not contributing to the normalized totals.
    """
    status_counts: dict[str, int] = {status: 0 for status in _PACING_STATUSES}
    for entry in accounts:
        status = entry["status"]
        status_counts[status] = status_counts.get(status, 0) + 1

    projectable = [e for e in accounts if e["status"] in _PACING_PROJECTABLE]
    fx_projectable = [
        e
        for e in projectable
        if e["period_budget_normalized"] is not None
        and e["projected_spend_normalized"] is not None
    ]
    # Start at 0.0 so the totals are always float, even when nothing qualified (bare sum([]) is int 0).
    total_budget = sum((e["period_budget_normalized"] for e in fx_projectable), 0.0)
    total_projected = sum((e["projected_spend_normalized"] for e in fx_projectable), 0.0)
    overall_variance = (
        (total_projected - total_budget) / total_budget if total_budget > 0 else None
    )

    over_sorted = sorted(
        projectable, key=lambda e: (-e["variance_pct"], e["ad_account_id"] or "")
    )
    under_sorted = sorted(
        projectable, key=lambda e: (e["variance_pct"], e["ad_account_id"] or "")
    )

    return {
        "reporting_currency": reporting,
        "total_period_budget_normalized": total_budget,
        "total_projected_normalized": total_projected,
        "overall_variance_pct": overall_variance,
        "status_counts": status_counts,
        "worst_over_pacers": [
            _pacing_shortlist_entry(e) for e in over_sorted[:PACING_SHORTLIST_LIMIT]
        ],
        "worst_under_pacers": [
            _pacing_shortlist_entry(e) for e in under_sorted[:PACING_SHORTLIST_LIMIT]
        ],
        "excluded_from_rollup": len(accounts) - len(fx_projectable),
    }


# --------------------------------------------------------------------------- #
# RANK ACCOUNTS: sort the whole fleet by a single metric for a date range.
#
# A *pure post-processor* over cross_account_performance — exactly the same relationship
# account_benchmark and flag_accounts_needing_attention have to that tool. Calls it once,
# splits rows into rankable (have the metric value) vs unranked (missing value or no-FX twin
# for money metrics), sorts, assigns 1-based ranks (ties share strictly-better count + 1), and
# truncates to the requested limit. No new Meta read shape.
# --------------------------------------------------------------------------- #

# Maps every accepted metric name (including aliases) to its canonical internal field name.
RANK_METRIC_ALIASES: dict[str, str] = {
    "spend": "spend",
    "cpm": "cpm",
    "cpc": "cpc",
    "cost_per_result": "cost_per_result",
    "cpl": "cost_per_result",
    "cpa": "cost_per_result",
    "ctr": "ctr",
    "roas": "roas",
    "impressions": "impressions",
    "clicks": "clicks",
    "results": "results",
}

# Money metrics are ranked on their normalized twin (reporting_currency).
_RANK_MONEY_METRICS: frozenset[str] = frozenset({"spend", "cpm", "cpc", "cost_per_result"})


def rank_accounts(
    reader: "MetaReaderProvider",
    *,
    date_from: str,
    date_to: str,
    metric: str,
    order: str = "desc",
    limit: int = 10,
    account_ids: list[str] | None = None,
    reporting_currency: str = "USD",
    fx_table: FxTable | None = None,
) -> dict[str, Any]:
    """Rank every reachable ad account by a single efficiency or spend metric.

    A **pure post-processor** over :func:`cross_account_performance`: calls it once, splits the
    per-account rows into rankable (metric value present) vs unranked (missing value or no-FX
    normalized twin for money metrics), sorts, assigns 1-based ranks (ties share the
    strictly-better count + 1 convention, tiebroken by ``ad_account_id`` ascending for
    determinism), and truncates to ``limit``.

    ``metric`` is normalized to lowercase before lookup and resolved through
    :data:`RANK_METRIC_ALIASES` (so ``"cpl"`` and ``"cpa"`` resolve to ``"cost_per_result"``);
    the canonical name appears in the output. Money metrics (spend/CPM/CPC/cost_per_result) are
    ranked on each row's ``*_normalized`` twin so accounts in different currencies are directly
    comparable; ``value_native`` carries the native figure for reference. Ratio and count metrics
    (CTR/ROAS/impressions/clicks/results) are ranked natively (currency-invariant).

    An unknown ``reporting_currency`` or a whole-discovery failure propagate unchanged from the
    prereq. ``fx_table`` is a test-only injection seam — not exposed to the LLM.
    """
    canonical = RANK_METRIC_ALIASES.get(str(metric or "").lower())
    if canonical is None:
        valid = sorted(RANK_METRIC_ALIASES)
        raise ValueError(f"Unknown metric {metric!r}; valid names: {', '.join(valid)}")
    if order not in ("asc", "desc"):
        raise ValueError(f"order must be 'asc' or 'desc'; got {order!r}")
    if limit <= 0:
        raise ValueError(f"limit must be a positive integer; got {limit}")

    is_money = canonical in _RANK_MONEY_METRICS
    sort_field = f"{canonical}_normalized" if is_money else canonical

    perf = cross_account_performance(
        reader,
        date_from=date_from,
        date_to=date_to,
        account_ids=account_ids,
        reporting_currency=reporting_currency,
        fx_table=fx_table,
    )

    # Partition rows into rankable and unranked.
    rankable: list[tuple[float, str, dict[str, Any]]] = []
    unranked: list[dict[str, Any]] = []

    for row in perf["accounts"]:
        ad_account_id = row["ad_account_id"]
        sort_value = row.get(sort_field)
        if sort_value is not None:
            rankable.append((float(sort_value), ad_account_id, row))
        else:
            if is_money and canonical in row:
                # Native value present but no normalized twin → no FX rate for this currency.
                reason = f"no FX rate for {row.get('currency', 'UNKNOWN')}"
            else:
                reason = "metric unavailable"
            unranked.append({
                "ad_account_id": ad_account_id,
                "name": row.get("name"),
                "reason": reason,
            })

    # Sort: flip sort_value sign for desc so both directions use the same ascending tuple sort.
    # Tiebreak: ad_account_id ascending → stable, deterministic total order run-to-run.
    if order == "desc":
        rankable.sort(key=lambda t: (-t[0], t[1]))
    else:
        rankable.sort(key=lambda t: (t[0], t[1]))

    # Assign 1-based ranks: ties share strictly-better count + 1. Since the list is already sorted
    # by value, the first occurrence of a value is at index = count of strictly-better entries.
    ranked: list[dict[str, Any]] = []
    current_rank: int = 1
    prev_value: float | None = None
    for i, (sort_value, ad_account_id, row) in enumerate(rankable):
        if prev_value is None or sort_value != prev_value:
            current_rank = i + 1
        prev_value = sort_value

        entry: dict[str, Any] = {
            "rank": current_rank,
            "ad_account_id": ad_account_id,
            "account_id": row.get("account_id"),
            "name": row.get("name"),
            "currency": row.get("currency"),
            # Emit the row's original-typed value (int for counts, float for money-normalized),
            # not the float()-coerced sort key, so counts stay integers like everywhere else.
            "value": row.get(sort_field),
        }
        if is_money:
            entry["value_native"] = row.get(canonical)
        ranked.append(entry)

    ranked_total = len(ranked)

    return {
        "date_from": date_from,
        "date_to": date_to,
        "metric": canonical,
        "order": order,
        "limit": limit,
        "reporting_currency": perf["reporting_currency"],
        "fx_as_of": perf["fx_as_of"],
        "fx_note": perf["fx_note"],
        "account_count": perf["account_count"],
        "ranked": ranked[:limit],
        "ranked_total": ranked_total,
        "unranked": unranked,
        "errors": perf["errors"],
    }
