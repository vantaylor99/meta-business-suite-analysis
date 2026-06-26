"""Shared grounding layer for the op / authoring write plans (pure — no I/O, no clock, no network).

The action plan (``actions.py``) already attaches an ``evidence`` block + a **computed** ``confidence``
band to every recommendation-bearing action via ``actions.evaluate_action_confidence`` /
``actions._attach_confidence``. The ``control.py`` ops pipeline and the ``authoring.py`` pipeline
produce write plans (``plan["ops"]``) that historically carried no such grounding. This module is the
**shared scaffolding** so those pipelines (and the per-capability tickets layered on them) can attach
grounding uniformly instead of each re-implementing the assess / abstain / serialize dance.

Why a separate module (and not ``confidence.py`` or ``actions.py``):

- ``confidence.py`` is the *pure scoring engine* and the single source of the band. It deliberately
  knows nothing about op-plan dict shape; keeping it that way preserves the "one place computes a
  band" invariant. This helper *mutates op-plan dicts* — a write-plan-shape concern — so it sits one
  layer above ``confidence.py`` and imports from it.
- ``actions.py`` pulls in the Meta API client and is action-plan-specific. Importing its
  ``_attach_confidence`` into ``control``/``authoring`` would couple the write layers to the action
  layer (and to the network). Instead both write layers import THIS small pure module, so
  ``control.py`` and ``authoring.py`` stay decoupled from each other and from ``actions.py``.

The hard invariant is the same one ``confidence.py`` enforces: **a band is never free-typed.** The
only paths to a band are :func:`confidence.assess` (from deterministic sample/recency/tier inputs) and
:func:`confidence.abstain_confidence` (the explicit absence-of-a-score). When a caller cannot supply a
sample, this helper routes through ``abstain_confidence`` — it never defaults to ``low``/``medium``.
"""

from __future__ import annotations

from typing import Any

from .confidence import (
    Evidence,
    EvidenceTier,
    abstain_confidence,
    assess,
    confidence_to_dict,
    detect_causal_language,
    evidence_to_dict,
)

# Band name (stored on the confidence block) that marks the explicit absence of a score.
ABSTAIN_BAND = "abstain"


def _empty_evidence_dict() -> dict[str, Any]:
    """The serialized shape of "no evidence" — every :func:`evidence_to_dict` key present but empty,
    so a downstream reader (the review gate, a renderer) sees the same keys whether or not a sample
    was supplied. Sample fields are ``None`` so ``sample_cited`` reads False at the gate."""
    return {
        "metric_name": "",
        "metric_value": None,
        "metric_display": "",
        "window": "",
        "sample_purchases": None,
        "sample_spend": None,
        "entity_level": "",
        "entity_id": None,
        "entity_name": None,
        "regenerating_query": None,
    }


def attach_op_grounding(
    op: dict[str, Any],
    *,
    evidence: Evidence | None,
    tier: EvidenceTier | str,
    spend_floor: float,
    conversions_floor: float,
    recency_days: int | None,
    causal_text: str | None = None,
) -> None:
    """Attach a serialized ``evidence`` + **computed** ``confidence`` block to a write op IN PLACE.

    The band is computed by :func:`confidence.assess` when a sample is present, and by
    :func:`confidence.abstain_confidence` when ``evidence`` is ``None`` or carries no sample
    (``sample_purchases`` and ``sample_spend`` both ``None``) — never free-typed. ``assess`` itself
    abstains when a present sample is below the significance floor, so a thin sample yields an
    ``abstain`` band rather than a fabricated ``low``/``medium``.

    ``recency_days`` is passed in by the caller (the live-state read that derives it happens in the
    impure caller, never here), keeping this module clock-free. ``causal_text`` is the causal-language
    probe — an accidentally causal rationale downgrades grounding, mirroring the action plan.
    """
    causal_claim = detect_causal_language(causal_text)
    has_sample = evidence is not None and (
        evidence.sample_purchases is not None or evidence.sample_spend is not None
    )
    if has_sample:
        conf = assess(
            evidence=evidence,
            tier=tier,
            spend_floor=spend_floor,
            conversions_floor=conversions_floor,
            recency_days=recency_days,
            causal_text=causal_text,
        )
        op["evidence"] = evidence_to_dict(evidence)
    else:
        reason = (
            "no sample supplied for this op — abstaining rather than fabricating a band"
            if evidence is None
            else "no purchases/spend sample for this op — abstaining rather than fabricating a band"
        )
        conf = abstain_confidence(
            tier=tier,
            factors=[reason],
            would_raise="supply a purchases/spend sample over the significance floor",
            causal_claim=causal_claim,
        )
        op["evidence"] = _empty_evidence_dict() if evidence is None else evidence_to_dict(evidence)
    op["confidence"] = confidence_to_dict(conf)


def op_grounding_gap(
    confidence: Any,
    evidence: Any,
) -> str | None:
    """Return a block reason if a *grounding-required* approved write is not adequately grounded,
    else ``None``. Pure — the caller (``apply_ops_plan`` / ``apply_authoring_plan``) decides which ops
    are grounding-required and only consults this for those.

    Two gaps fail closed:

    - **No confidence block** (missing or blank ``band``) → an ungrounded write; the hole a hand-edited
      plan could otherwise sneak through. Reason: "approved write missing required evidence/confidence."
    - **``abstain`` band WITH a cited sample** → the op tried to ground on data but the sample is below
      the significance floor; acting on it would be a confident call on thin data. Reason names the
      abstain.

    A **structural** ``abstain`` (band ``abstain`` but NO sample cited — e.g. a safety PAUSE or other
    no-metric op) is allowed: it is an honest, deliberate abstention, not a thin-data overclaim, so
    blocking it would needlessly break PAUSED-by-default safety writes.
    """
    if not isinstance(confidence, dict) or not confidence.get("band"):
        return "approved write missing required evidence/confidence."
    if str(confidence.get("band")) == ABSTAIN_BAND:
        ev = evidence if isinstance(evidence, dict) else {}
        sample_cited = ev.get("sample_purchases") is not None or ev.get("sample_spend") is not None
        if sample_cited:
            return (
                "approved write rests on insufficient data (abstain band) — keep running; do not "
                "execute until the sample clears the significance floor."
            )
    return None
