"""Grounded-recommendation confidence engine (pure, no I/O, no clock, no network).

Every operator-facing recommendation must carry (a) the **evidence** behind it and (b) a
**computed confidence band**. This module is the single source of both, so the whole repo speaks
ONE confidence language and the scoring logic lives in exactly one place.

The hard invariant: **a band is never a number the model free-types.** It is computed from
deterministic inputs (sample size, recency, evidence tier, significance) via a transparent rubric.
A caller that cannot supply those inputs gets `abstain` — never a guessed score. There is no public
parameter anywhere in this module that accepts a pre-baked band/score; the only path to a band is
through the deterministic inputs (missing sample → below floor → abstain).

Two independent axes, and **the weaker one governs**:

- *Data strength* — do we have enough recent, significant data to trust the number?
- *Grounding strength* — how causal is the evidence? An A/B experiment grounds a causal claim; a
  cross-sectional correlation does not, no matter how large the sample. Grounding therefore CAPS
  data strength: a huge-n correlational causal claim cannot read High.

The 🟢/🟡/🔴/⚪ vocabulary is deliberately identical to the human rubric in `knowledge/README.md`
so the documented rubric and this computed rubric stay one language (pinned by a test).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from .config import CONFIDENCE_RECENCY_STALE_DAYS


class Band(IntEnum):
    """Ordered confidence band. Integer order matters: ``min()`` = the weaker axis governs, and
    ``abstain`` is the floor so combining anything with ``abstain`` yields ``abstain``."""

    abstain = 0
    low = 1
    medium = 2
    high = 3


class EvidenceTier(IntEnum):
    """How causal the evidence is, lowest→highest. Order matters for ceilings."""

    model_inference = 0
    external = 1
    correlational = 2
    direct_observation = 3
    ab_experiment = 4


# Highest grounding band each tier can REACH (the ceiling; data strength can land lower).
_TIER_CEILING: dict[EvidenceTier, Band] = {
    EvidenceTier.ab_experiment: Band.high,
    EvidenceTier.direct_observation: Band.high,
    EvidenceTier.correlational: Band.medium,
    EvidenceTier.external: Band.low,
    EvidenceTier.model_inference: Band.low,
}

# Presentation strings — MUST match knowledge/README.md verbatim (pinned by test). Do NOT invent a
# second scale. Note: en dash (–) in ranges, em dash (—) in the abstain label.
BAND_PRESENTATION: dict[Band, dict[str, str]] = {
    Band.high: {"emoji": "🟢", "label": "High", "range": "~80–100%"},
    Band.medium: {"emoji": "🟡", "label": "Medium", "range": "~50–80%"},
    Band.low: {"emoji": "🔴", "label": "Low", "range": "<50%"},
    Band.abstain: {"emoji": "⚪", "label": "Insufficient data — abstain", "range": "—"},
}

# Causal-language detector. Word-boundary aware, case-insensitive. Used to flag a recommendation
# that asserts CAUSE from non-experimental data (which then downgrades grounding).
_CAUSAL_RE = re.compile(
    r"\b(?:because|causes?|caused|causing|drives?|drove|driving|"
    r"due\s+to|leads?\s+to|led\s+to|results?\s+in|resulted\s+in|"
    r"thanks\s+to|responsible\s+for)\b",
    re.IGNORECASE,
)


def _coerce_tier(tier: EvidenceTier | str) -> EvidenceTier:
    if isinstance(tier, EvidenceTier):
        return tier
    try:
        return EvidenceTier[str(tier)]
    except KeyError as exc:
        valid = ", ".join(t.name for t in EvidenceTier)
        raise ValueError(f"Unknown evidence tier {tier!r}; expected one of: {valid}") from exc


def _fmt_conversions(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{int(value)}" if float(value).is_integer() else f"{value:g}"


def _fmt_spend(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.0f}"


@dataclass(slots=True)
class Evidence:
    """The deterministic facts behind a recommendation, so an operator can reproduce the number."""

    metric_name: str
    metric_value: float | None
    metric_display: str
    window: str
    sample_purchases: float | None
    sample_spend: float | None
    entity_level: str  # ad | adset | campaign | account
    entity_id: str | None
    entity_name: str | None
    regenerating_query: str | None  # the account_metrics command that reproduces metric_value


@dataclass(slots=True)
class Confidence:
    """A computed confidence verdict. Produced ONLY by :func:`assess` from deterministic inputs."""

    band: Band  # combined verdict (the weaker of the two axes)
    data_band: Band
    grounding_band: Band
    grounding_tier: str
    factors: list[str]  # why this band — shown to the operator
    would_raise: str
    would_lower: str
    causal_flag: bool


def build_regenerating_query(
    account_slug: str | None,
    level: str | None,
    date_from: str | None,
    date_to: str | None,
) -> str | None:
    """Return the exact ``account_metrics`` command that reproduces a metric, or ``None`` if any
    input is missing (never fabricate a query). Matches the ``account_metrics`` entry point in
    pyproject.toml (``meta_ads_analysis.cli:metrics_main``)."""
    if not (account_slug and level and date_from and date_to):
        return None
    return (
        f"account_metrics --account {account_slug} --level {level} "
        f"--date-from {date_from} --date-to {date_to}"
    )


def detect_causal_language(text: str | None) -> bool:
    """True if prose asserts a cause ("because", "drives", "due to", ...). Case-insensitive,
    word-boundary aware. Missing/empty text → False."""
    if not text:
        return False
    return bool(_CAUSAL_RE.search(text))


def data_strength(
    *,
    sample_purchases: float | None,
    sample_spend: float | None,
    spend_floor: float,
    conversions_floor: float,
    recency_days: int | None,
    pvalue: float | None = None,
    stale_days: int = CONFIDENCE_RECENCY_STALE_DAYS,
) -> tuple[Band, list[str]]:
    """Band from how much recent, significant data backs the metric.

    Below the floor (NEITHER spend nor conversions floor cleared) → ``abstain``: we do not report a
    low percentage, we abstain. Above the floor, start from sample size, then round DOWN for a stale
    window and, when a ``pvalue`` is supplied for a comparative claim, cap at medium unless p<0.05.
    Every ambiguous/missing input rounds DOWN — the anti-fabrication invariant.
    """
    purchases = sample_purchases if sample_purchases is not None else 0.0
    spend = sample_spend if sample_spend is not None else 0.0

    cleared_conversions = purchases >= conversions_floor
    cleared_spend = spend >= spend_floor

    if not cleared_conversions and not cleared_spend:
        return Band.abstain, [
            f"below significance floor: {_fmt_conversions(sample_purchases)} conversions "
            f"< {conversions_floor:g} and {_fmt_spend(sample_spend)} spend "
            f"< ${spend_floor:,.0f} — insufficient data, abstain"
        ]

    factors: list[str] = []

    # Base band from sample size. Conversions are the statistically meaningful signal; spend alone
    # (conversions below floor) is weak data on the outcome, so it caps at low.
    if cleared_conversions:
        if purchases >= 4 * conversions_floor:
            base = Band.high
        else:
            base = Band.medium
        factors.append(
            f"sample: {_fmt_conversions(sample_purchases)} conversions / "
            f"{_fmt_spend(sample_spend)} spend (over floor)"
        )
    else:
        base = Band.low
        factors.append(
            f"sample: {_fmt_spend(sample_spend)} spend cleared but only "
            f"{_fmt_conversions(sample_purchases)} conversions (< {conversions_floor:g}) — thin on conversions"
        )

    band = base

    # Recency: a stale (or unknown-recency) window rounds the band down one level, floored at low.
    if recency_days is None:
        band = max(Band.low, Band(band - 1))
        factors.append("recency unknown — rounded down")
    elif recency_days > stale_days:
        band = max(Band.low, Band(band - 1))
        factors.append(f"stale window ({recency_days}d since end > {stale_days}d) — rounded down")
    else:
        factors.append(f"recent window ({recency_days}d since end)")

    # Significance (only when a pvalue is supplied for a comparative claim).
    if pvalue is not None:
        if pvalue < 0.05:
            factors.append(f"significant (p={pvalue:g}) — supports higher")
        else:
            band = min(band, Band.medium)
            factors.append(f"not significant (p={pvalue:g}) — capped at medium")

    return band, factors


def grounding_strength(
    tier: EvidenceTier | str,
    *,
    causal_claim: bool,
) -> tuple[Band, list[str]]:
    """Ceiling band from the evidence tier, downgraded one level if a non-experimental claim asserts
    cause. An A/B experiment IS the causal evidence, so it is never downgraded by the causal guard."""
    resolved = _coerce_tier(tier)
    band = _TIER_CEILING[resolved]
    factors = [f"evidence tier: {resolved.name} (ceiling {BAND_PRESENTATION[band]['label']})"]

    if causal_claim and resolved is not EvidenceTier.ab_experiment:
        band = max(Band.low, Band(band - 1))
        factors.append("correlational — confirm via A/B")

    return band, factors


def combine_bands(data: Band, grounding: Band) -> Band:
    """The weaker axis governs. Because ``abstain`` is the integer floor, if either axis is
    ``abstain`` the result is ``abstain`` — and grounding can CAP a strong sample."""
    return min(data, grounding)


def assess(
    *,
    evidence: Evidence,
    tier: EvidenceTier | str,
    spend_floor: float,
    conversions_floor: float,
    recency_days: int | None,
    pvalue: float | None = None,
    causal_text: str | None = None,
) -> Confidence:
    """Compute a :class:`Confidence` from deterministic inputs. There is no parameter that accepts a
    pre-baked band/score: the only way to a band is through the inputs below. A caller that cannot
    supply sample data passes ``None`` sample values, which drive ``abstain`` naturally."""
    resolved_tier = _coerce_tier(tier)
    causal_flag = detect_causal_language(causal_text)

    data_band, data_factors = data_strength(
        sample_purchases=evidence.sample_purchases,
        sample_spend=evidence.sample_spend,
        spend_floor=spend_floor,
        conversions_floor=conversions_floor,
        recency_days=recency_days,
        pvalue=pvalue,
    )
    grounding_band, grounding_factors = grounding_strength(resolved_tier, causal_claim=causal_flag)
    band = combine_bands(data_band, grounding_band)

    factors = list(data_factors) + list(grounding_factors)
    if causal_flag:
        factors.append(
            "causal claim backed by A/B experiment"
            if resolved_tier is EvidenceTier.ab_experiment
            else "asserts cause from non-experimental data"
        )

    return Confidence(
        band=band,
        data_band=data_band,
        grounding_band=grounding_band,
        grounding_tier=resolved_tier.name,
        factors=factors,
        would_raise="more purchases / a more recent window / a completed A/B",
        would_lower="smaller sample / a stale or contradicting window / a refuting A/B",
        causal_flag=causal_flag,
    )


def abstain_confidence(
    *,
    tier: EvidenceTier | str,
    factors: list[str],
    would_raise: str,
    causal_claim: bool = False,
) -> Confidence:
    """A sanctioned, explicit refusal-to-score verdict for a caller whose own domain gate (a
    significance floor, a still-learning grace window) deems the data untrustworthy in a way the
    sample-size rubric in :func:`assess` cannot express — e.g. a well-funded but too-young ad, whose
    sample would otherwise clear the floor.

    The data axis is pinned to ``abstain`` — the integer floor, NOT a fabricated number — and the
    grounding axis is the tier's honest ceiling; the weaker axis governs (:func:`combine_bands`), so
    the verdict abstains. This keeps every :class:`Confidence` construction inside this module while
    preserving the invariant that a band is never a value the caller supplies: ``abstain`` is the
    *absence* of a score, the only band reachable without deterministic sample inputs.
    """
    grounding, _ = grounding_strength(tier, causal_claim=causal_claim)
    return Confidence(
        band=combine_bands(Band.abstain, grounding),
        data_band=Band.abstain,
        grounding_band=grounding,
        grounding_tier=_coerce_tier(tier).name,
        factors=list(factors),
        would_raise=would_raise,
        would_lower="",
        causal_flag=causal_claim,
    )


def render_confidence_line(conf: Confidence, *, max_factors: int = 3) -> str:
    """Compact one-line confidence renderer (emoji + label + range + top factors). Presentation
    only — the structured data lives on :class:`Confidence`."""
    pres = BAND_PRESENTATION[conf.band]
    head = f"{pres['emoji']} {pres['label']}"
    if pres["range"] != "—":
        head += f" ({pres['range']})"
    if conf.factors:
        head += " — " + "; ".join(conf.factors[:max_factors])
    return head


def evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    """Serialize :class:`Evidence` to the JSON shape stored on an action (e.g. action_plan.json)."""
    return {
        "metric_name": evidence.metric_name,
        "metric_value": evidence.metric_value,
        "metric_display": evidence.metric_display,
        "window": evidence.window,
        "sample_purchases": evidence.sample_purchases,
        "sample_spend": evidence.sample_spend,
        "entity_level": evidence.entity_level,
        "entity_id": evidence.entity_id,
        "entity_name": evidence.entity_name,
        "regenerating_query": evidence.regenerating_query,
    }


def confidence_to_dict(conf: Confidence) -> dict[str, Any]:
    """Serialize :class:`Confidence` to JSON. Bands are stored as their lowercase name
    (``"high"``/``"medium"``/``"low"``/``"abstain"``), never as a number."""
    return {
        "band": conf.band.name,
        "data_band": conf.data_band.name,
        "grounding_band": conf.grounding_band.name,
        "grounding_tier": conf.grounding_tier,
        "factors": list(conf.factors),
        "would_raise": conf.would_raise,
        "would_lower": conf.would_lower,
        "causal_flag": conf.causal_flag,
    }


def evidence_from_dict(data: dict[str, Any]) -> Evidence:
    """Rebuild :class:`Evidence` from :func:`evidence_to_dict` output (for downstream renderers)."""
    return Evidence(
        metric_name=data.get("metric_name", ""),
        metric_value=data.get("metric_value"),
        metric_display=data.get("metric_display", ""),
        window=data.get("window", ""),
        sample_purchases=data.get("sample_purchases"),
        sample_spend=data.get("sample_spend"),
        entity_level=data.get("entity_level", ""),
        entity_id=data.get("entity_id"),
        entity_name=data.get("entity_name"),
        regenerating_query=data.get("regenerating_query"),
    )


def confidence_from_dict(data: dict[str, Any]) -> Confidence:
    """Rebuild :class:`Confidence` from :func:`confidence_to_dict` output for rendering ONLY. This is
    a deserializer for an already-computed verdict, not a scoring path — :func:`assess` remains the
    only way to *compute* a band from data."""
    return Confidence(
        band=Band[data["band"]],
        data_band=Band[data["data_band"]],
        grounding_band=Band[data["grounding_band"]],
        grounding_tier=data.get("grounding_tier", ""),
        factors=list(data.get("factors") or []),
        would_raise=data.get("would_raise", ""),
        would_lower=data.get("would_lower", ""),
        causal_flag=bool(data.get("causal_flag")),
    )


def render_evidence_line(evidence: Evidence, *, include_regen: bool = True) -> str:
    """Compact one-line evidence renderer (metric · window · sample · entity · regen query).

    Pass ``include_regen=False`` to drop the trailing ``regen:`` query — for callers (e.g. the
    operator brief) that surface the reproduce-it command on its own clearly-labeled line and would
    otherwise print it twice."""
    parts = [evidence.metric_display or evidence.metric_name, f"window {evidence.window}"]
    parts.append(
        f"n={_fmt_conversions(evidence.sample_purchases)} conversions / "
        f"{_fmt_spend(evidence.sample_spend)} spend"
    )
    if evidence.entity_id or evidence.entity_name:
        label = f"{evidence.entity_level}:{evidence.entity_id or '?'}"
        if evidence.entity_name:
            label += f" '{evidence.entity_name}'"
        parts.append(label)
    if include_regen and evidence.regenerating_query:
        parts.append(f"regen: {evidence.regenerating_query}")
    return " · ".join(parts)
