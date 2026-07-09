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


def cross_account_spend_summary(
    reader: "MetaReaderProvider",
    *,
    date_from: str,
    date_to: str,
    account_ids: list[str] | None = None,
    insight_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate spend/performance across every reachable account (or an explicit subset) in one call.

    Fans out **sequentially** over the target accounts — no new concurrency; the client's own ``429``
    retry handles rate limits. For each account it reads a single aggregated account-level insights row
    (``level="account"``, ``time_increment="all_days"``) for the window and extracts the requested
    additive metrics. Additive metrics are subtotaled **per currency** — never across currencies, so
    there is deliberately no grand total. A per-account failure (permission, exhausted retry, an
    unreadable explicit id) is recorded in ``errors`` and skipped; it never fails the whole call.

    When ``account_ids`` is omitted, targets and their metadata come from
    :func:`list_ad_accounts` (all reachable accounts). A discovery-level ``MetaApiError`` there
    (bad token / missing scope) propagates — a whole-call failure, distinct from a per-account one.
    When ``account_ids`` is given, each id is normalized (bare numeric or ``act_`` both work) and its
    metadata is fetched per id via ``reader.get_account`` inside the same per-account error path.

    See the module ticket for the returned shape; ``note="no accounts reachable"`` is present only
    when no ids were given and discovery found nothing.
    """
    fields = list(insight_fields) if insight_fields else list(DEFAULT_SUMMARY_INSIGHT_FIELDS)

    if account_ids is None:
        discovered = list_ad_accounts(reader)  # may raise MetaApiError -> whole-call failure
        reachable_count = len(discovered)
        # (ad_account_id, prefetched normalized metadata row)
        targets: list[tuple[str, dict[str, Any] | None]] = [
            (_ad_account_id_from_row(row), row) for row in discovered
        ]
    else:
        # De-duplicate after normalization (order-preserving) so a caller passing the same account
        # twice — including once bare and once as ``act_`` (``"1"`` and ``"act_1"``) — is fanned out
        # and subtotaled exactly once; summing an account twice would be wrong and has no valid use.
        seen: set[str] = set()
        normalized_ids: list[str] = []
        for raw in account_ids:
            norm = account_registry._normalize_ad_account_id(str(raw or "").strip())
            if norm not in seen:
                seen.add(norm)
                normalized_ids.append(norm)
        reachable_count = len(normalized_ids)
        targets = [(norm, None) for norm in normalized_ids]

    accounts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    totals_by_currency: dict[str, dict[str, Any]] = {}

    for ad_account_id, meta_row in targets:
        try:
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
        except MetaApiError as exc:
            # Central correctness requirement: one account's failure is recorded and skipped, never
            # fatal to the whole fan-out, and never contaminates another account's subtotal.
            errors.append({"ad_account_id": ad_account_id, "error": str(exc)})
            continue

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

    result: dict[str, Any] = {
        "date_from": date_from,
        "date_to": date_to,
        "account_count": len(targets),
        "reachable_count": reachable_count,
        "accounts": accounts,
        "totals_by_currency": totals_by_currency,
        "errors": errors,
    }
    if account_ids is None and reachable_count == 0:
        result["note"] = "no accounts reachable"
    return result
