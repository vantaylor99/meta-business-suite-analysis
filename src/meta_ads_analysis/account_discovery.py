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
from .meta_api import MetaApiError

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
