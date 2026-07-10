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
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import account_registry
from .currency import FxTable, load_fx_table
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
    out.update(compute_derived_metrics(base))
    return out
