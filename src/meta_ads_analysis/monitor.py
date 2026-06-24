"""Runaway / outlier watch scanner (read-only, flag-only).

Catches ads that are spending while performing badly — WITHOUT killing promising ads too early.
A cheap deterministic triage classifies each delivering ad as urgent / underperforming / watch /
ok using context-aware guards; the agent then reasons over the flagged few case-by-case and pauses
(if warranted) through the normal guarded flow. This module never writes to the account.

Protective by design:
- **Significance floor**: ignore ads that haven't spent enough to judge (protects brand-new ads).
- **Recently-created-or-changed grace**: an ad created/edited within `grace_days` is "learning" →
  classified `watch`, NEVER `urgent`. Using `updated_time` means a just-re-enabled/edited ad (the
  mid-relearn case) is protected automatically, no decision-log parsing needed.
- **Account goal anchored**: ROAS floor / target come from the account's action policy.

Persistence: a watchlist tracks how many consecutive scans an ad has been flagged, so a *consistent*
underperformer is distinguishable from a one-day blip.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import account_registry
from .config import DEFAULT_REPORTS_ROOT
from .control import fetch_entity_metrics
from .meta_api import MetaMarketingApiClient
from .utils import ensure_dir, write_json

AD_META_FIELDS = ["id", "name", "status", "effective_status", "adset_id", "created_time", "updated_time"]
DELIVERING = {"ACTIVE", "IN_PROCESS"}


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def classify_ad(
    *,
    spend: float,
    roas: float | None,
    results: float | None,
    days_since_change: int | None,
    accelerating: bool,
    min_spend: float,
    grace_days: int,
    roas_floor: float,
    roas_target: float,
) -> dict[str, Any]:
    """Pure classification of one ad. Returns classification + reasons + $ at risk."""
    if spend < min_spend:
        return {"classification": "insufficient", "dollars_at_risk": 0.0,
                "reasons": [f"only ${spend:.0f} spent (< ${min_spend:.0f} significance floor) — too early to judge"]}
    r = roas if roas is not None else 0.0
    dollars_at_risk = round(spend * max(0.0, 1.0 - (r / roas_target)), 2)
    reasons: list[str] = []
    protected = days_since_change is not None and days_since_change < grace_days
    if protected:
        reasons.append(f"created/changed {days_since_change}d ago (< {grace_days}d) — learning, protected from kill")
        if r < roas_floor:
            reasons.append(f"ROAS {r:.2f} is below floor {roas_floor} but it's too young to judge")
        return {"classification": "watch", "dollars_at_risk": dollars_at_risk, "reasons": reasons}
    if r < roas_floor:
        reasons.append(f"ROAS {r:.2f} < pause floor {roas_floor} on ${spend:.0f}")
        if not results:
            reasons.append("~0 results")
        if accelerating:
            reasons.append("spend accelerating vs its recent average")
        return {"classification": "urgent", "dollars_at_risk": dollars_at_risk, "reasons": reasons}
    if r < roas_target:
        reasons.append(f"ROAS {r:.2f} below target {roas_target} (above floor {roas_floor})")
        return {"classification": "underperforming", "dollars_at_risk": dollars_at_risk, "reasons": reasons}
    return {"classification": "ok", "dollars_at_risk": dollars_at_risk, "reasons": []}


def _policy_floors(account_slug: str) -> tuple[float, float]:
    try:
        policy = account_registry.resolve_account(account_slug).action_policy or {}
    except Exception:
        policy = {}
    floor = policy.get("pause_roas_floor") or 1.5
    target = policy.get("target_roas") or policy.get("scale_roas_floor") or 3.0
    return float(floor), float(target)


def build_watch_report(
    client: MetaMarketingApiClient,
    ad_account_id: str,
    *,
    account_slug: str,
    as_of: date,
    window_days: int = 7,
    recent_days: int = 3,
    min_spend: float = 100.0,
    grace_days: int = 5,
    roas_floor: float | None = None,
    roas_target: float | None = None,
    prior_watchlist: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if roas_floor is None or roas_target is None:
        pf, pt = _policy_floors(account_slug)
        roas_floor = roas_floor if roas_floor is not None else pf
        roas_target = roas_target if roas_target is not None else pt
    win_from = (as_of - timedelta(days=window_days - 1)).isoformat()
    rec_from = (as_of - timedelta(days=recent_days - 1)).isoformat()
    to = as_of.isoformat()

    window = {str(m["id"]): m for m in fetch_entity_metrics(client, ad_account_id, level="ad", date_from=win_from, date_to=to)}
    recent = {str(m["id"]): m for m in fetch_entity_metrics(client, ad_account_id, level="ad", date_from=rec_from, date_to=to)}
    meta = {
        str(a.get("id")): a
        for a in client.iter_paginated(f"/{ad_account_id}/ads", params={"fields": ",".join(AD_META_FIELDS), "limit": 200})
    }

    prior = (prior_watchlist or {}).get("ads", {})
    rows: list[dict[str, Any]] = []
    new_watchlist: dict[str, Any] = {}
    for ad_id, m in window.items():
        info = meta.get(ad_id, {})
        if info.get("effective_status") not in DELIVERING:
            continue  # not currently delivering -> not a runaway
        spend = m.get("spend") or 0.0
        changed = _as_date(info.get("updated_time")) or _as_date(info.get("created_time"))
        days_since_change = (as_of - changed).days if changed else None
        win_daily = spend / window_days
        rec_daily = (recent.get(ad_id, {}).get("spend") or 0.0) / recent_days
        accelerating = win_daily > 0 and rec_daily >= 1.3 * win_daily
        verdict = classify_ad(
            spend=spend, roas=m.get("roas"), results=m.get("purchases"),
            days_since_change=days_since_change, accelerating=accelerating,
            min_spend=min_spend, grace_days=grace_days, roas_floor=roas_floor, roas_target=roas_target,
        )
        cls = verdict["classification"]
        if cls in ("insufficient", "ok"):
            continue
        flaggable = cls in ("urgent", "underperforming")
        prior_entry = prior.get(ad_id, {})
        times = (prior_entry.get("times_flagged", 0) + 1) if flaggable else prior_entry.get("times_flagged", 0)
        row = {
            "ad_id": ad_id, "ad_name": info.get("name"), "adset_id": info.get("adset_id"),
            "classification": cls, "spend": round(spend, 2), "roas": m.get("roas"),
            "purchases": m.get("purchases"), "dollars_at_risk": verdict["dollars_at_risk"],
            "days_since_change": days_since_change, "accelerating": accelerating,
            "times_flagged": times, "reasons": verdict["reasons"],
        }
        rows.append(row)
        if flaggable:
            new_watchlist[ad_id] = {
                "ad_name": info.get("name"),
                "first_flagged": prior_entry.get("first_flagged", to),
                "last_flagged": to, "times_flagged": times,
            }

    order = {"urgent": 0, "underperforming": 1, "watch": 2}
    rows.sort(key=lambda r: (order.get(r["classification"], 9), -r["dollars_at_risk"]))
    return {
        "schema_version": 1,
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "as_of": to,
        "window": f"{win_from}..{to}",
        "params": {"min_spend": min_spend, "grace_days": grace_days, "roas_floor": roas_floor, "roas_target": roas_target},
        "rows": rows,
        "watchlist": {"generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "ads": new_watchlist},
    }


def default_watch_report_path(account_slug: str, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / run_date / "watch_report.json"


def watchlist_path(account_slug: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / "watchlist.json"


def load_watchlist(account_slug: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> dict[str, Any]:
    path = watchlist_path(account_slug, reports_root)
    if path.exists():
        import json
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def save_watchlist(account_slug: str, watchlist: dict[str, Any], reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    path = watchlist_path(account_slug, reports_root)
    ensure_dir(path.parent)
    write_json(path, watchlist)
    return path
