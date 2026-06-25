"""A/B experiment harness: define a test, then read it out with a significance check.

The point is to turn opinions ("enhance_cta probably helps", "this copy is better") into
*evidence*. You declare an experiment — a hypothesis, the ONE variable being changed, the control
vs variant entity ids, and a window — then `readout` pulls both arms' live metrics, compares ROAS,
and runs a two-proportion significance test on conversion rate so we know whether a difference is
real or noise (with a "needs more data" gate so we don't call it early).

Setting up the two arms reuses existing tools (e.g. `propose-duplicate-ad` to clone an ad, then
`set_creative_features` / `set_placements` to flip the one variable). For a perfectly clean audience
split (no overlap) Meta's native split-test/Experiments product is more rigorous; this harness is a
pragmatic, in-repo A/B that's directional-to-solid depending on how the arms are isolated.

Experiment definitions are committed under `knowledge/accounts/<slug>/experiments/<id>.json` so the
record travels with the repo.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .confidence import (
    Evidence,
    EvidenceTier,
    assess,
    build_regenerating_query,
    confidence_to_dict,
    evidence_to_dict,
)
from .control import fetch_entity_metrics
from .meta_api import MetaMarketingApiClient
from .utils import ensure_dir, slugify_name

EXPERIMENTS_ROOT = PROJECT_ROOT / "knowledge" / "accounts"


@dataclass(slots=True)
class Experiment:
    id: str
    account: str
    hypothesis: str
    variable: str
    level: str  # ad | adset | campaign
    control_ids: list[str]
    variant_ids: list[str]
    metric: str
    start_date: str
    planned_days: int
    status: str
    notes: str
    created: str


def experiments_dir(account: str) -> Path:
    return EXPERIMENTS_ROOT / slugify_name(account) / "experiments"


def define_experiment(
    *, account: str, exp_id: str, hypothesis: str, variable: str, level: str,
    control_ids: list[str], variant_ids: list[str], metric: str = "roas",
    start_date: str, planned_days: int = 14, notes: str = "", created: str,
) -> Path:
    if level not in {"ad", "adset", "campaign"}:
        raise ValueError("level must be ad, adset, or campaign.")
    if not control_ids or not variant_ids:
        raise ValueError("Need at least one control id and one variant id.")
    exp = Experiment(
        id=exp_id, account=slugify_name(account), hypothesis=hypothesis, variable=variable,
        level=level, control_ids=control_ids, variant_ids=variant_ids, metric=metric,
        start_date=start_date, planned_days=planned_days, status="active", notes=notes, created=created,
    )
    out = experiments_dir(account) / f"{exp_id}.json"
    ensure_dir(out.parent)
    out.write_text(json.dumps(asdict(exp), indent=2), encoding="utf-8")
    return out


def load_experiment(account: str, exp_id: str) -> Experiment:
    path = experiments_dir(account) / f"{exp_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Experiment not found: {path}")
    return Experiment(**json.loads(path.read_text(encoding="utf-8")))


def list_experiments(account: str) -> list[Experiment]:
    base = experiments_dir(account)
    if not base.exists():
        return []
    return [Experiment(**json.loads(p.read_text(encoding="utf-8"))) for p in sorted(base.glob("*.json"))]


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def two_proportion_pvalue(x1: float, n1: float, x2: float, n2: float) -> float | None:
    """Two-sided p-value for difference in conversion rates (x successes of n trials per arm)."""
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = x1 / n1, x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    if pooled <= 0 or pooled >= 1:
        return None
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    z = (p1 - p2) / se
    return round(2 * (1 - _phi(abs(z))), 4)


def _summarize_arm(metrics_by_id: dict[str, dict[str, Any]], ids: list[str]) -> dict[str, Any]:
    spend = value = purchases = impressions = 0.0
    for i in ids:
        m = metrics_by_id.get(str(i))
        if not m:
            continue
        spend += m.get("spend") or 0.0
        value += m.get("purchase_value") or 0.0
        purchases += m.get("purchases") or 0.0
        impressions += m.get("impressions") or 0.0
    return {
        "spend": round(spend, 2), "purchase_value": round(value, 2),
        "purchases": int(purchases), "impressions": int(impressions),
        "roas": round(value / spend, 2) if spend else None,
        "cvr": round(purchases / impressions, 6) if impressions else None,
        "cpa": round(spend / purchases, 2) if purchases else None,
    }


def read_experiment(
    client: MetaMarketingApiClient, ad_account_id: str, exp: Experiment, *, as_of: date,
    min_conversions: int = 25,
) -> dict[str, Any]:
    """Pull both arms over the test window and compare ROAS + conversion-rate significance."""
    metrics = {
        str(m["id"]): m
        for m in fetch_entity_metrics(client, ad_account_id, level=exp.level, date_from=exp.start_date, date_to=as_of.isoformat())
    }
    control = _summarize_arm(metrics, exp.control_ids)
    variant = _summarize_arm(metrics, exp.variant_ids)
    p_value = two_proportion_pvalue(variant["purchases"], variant["impressions"], control["purchases"], control["impressions"])

    roas_lift = None
    if control["roas"] and variant["roas"] is not None:
        roas_lift = round((variant["roas"] / control["roas"] - 1) * 100, 1)

    enough = control["purchases"] >= min_conversions and variant["purchases"] >= min_conversions
    if not enough:
        verdict = (f"INSUFFICIENT DATA — need >= {min_conversions} purchases per arm "
                   f"(control {control['purchases']}, variant {variant['purchases']}). Keep running.")
    elif p_value is None:
        verdict = "INSUFFICIENT DATA — could not compute significance."
    elif p_value < 0.05:
        better = "variant" if (variant["roas"] or 0) >= (control["roas"] or 0) else "control"
        verdict = (f"SIGNIFICANT conversion-rate difference (p={p_value}); **{better}** has higher ROAS "
                   f"(variant {variant['roas']} vs control {control['roas']}).")
    else:
        verdict = (f"NO significant difference yet (p={p_value}); ROAS variant {variant['roas']} "
                   f"vs control {control['roas']}. Keep running or call it a tie.")

    # Confidence in the shared vocabulary. The A/B is the top grounding tier (``ab_experiment``), so a
    # significant, well-powered readout can reach 🟢 High — it is NOT capped the way a correlational
    # claim is. Significance rests on conversions, so the data band is driven by per-arm purchases
    # (the weaker arm governs) + the p-value; spend is kept off the data axis. Below the
    # ``min_conversions`` gate the sample is below the conversion floor → ⚪ abstain, while the human
    # ``verdict`` string (INSUFFICIENT DATA) is preserved unchanged.
    sample_purchases = min(control["purchases"], variant["purchases"])
    evidence = Evidence(
        metric_name="roas_lift_pct", metric_value=roas_lift,
        metric_display=f"ROAS lift {roas_lift:+.1f}%" if roas_lift is not None else "ROAS lift n/a",
        window=f"{exp.start_date}..{as_of.isoformat()}",
        sample_purchases=sample_purchases,
        sample_spend=None,  # the A/B's significance rests on conversions, not spend
        entity_level=exp.level,
        entity_id=",".join(exp.control_ids + exp.variant_ids) or None, entity_name=exp.id,
        regenerating_query=build_regenerating_query(exp.account, exp.level, exp.start_date, as_of.isoformat()),
    )
    confidence = assess(
        evidence=evidence, tier=EvidenceTier.ab_experiment,
        # spend is not the experiment's significance axis; sample_spend=None + a positive floor keeps
        # it off the data band, so the conversion floor (min_conversions) alone governs abstention.
        spend_floor=1.0, conversions_floor=float(min_conversions),
        recency_days=0,  # window ends at as_of (deterministic, clock-free)
        pvalue=p_value if enough else None,
        causal_text=None,  # the A/B IS the causal instrument — not an unsupported causal claim to flag
    )
    return {
        "experiment_id": exp.id, "hypothesis": exp.hypothesis, "variable": exp.variable,
        "window": f"{exp.start_date}..{as_of.isoformat()}", "level": exp.level,
        "control": control, "variant": variant,
        "roas_lift_pct": roas_lift, "conversion_rate_pvalue": p_value, "min_conversions": min_conversions,
        "verdict": verdict,
        "confidence": confidence_to_dict(confidence), "evidence": evidence_to_dict(evidence),
        "caveat": ("Significance is on conversion-rate (purchases/impressions); ROAS also depends on "
                   "value variance. If both arms share an ad set they compete (overlap) — cleanest is "
                   "matched separate ad sets or Meta's native split test."),
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
