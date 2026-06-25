description: The action plan's pause/scale recommendations now each carry the facts behind them and a computed trust band, and a too-thin ad is returned as "not enough data yet — keep running" instead of a confident pause or scale.
prereq:
files: src/meta_ads_analysis/actions.py, src/meta_ads_analysis/analyze.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/confidence.py, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped

Wired the `confidence.py` engine into the action plan and the narrative recommendations:

- `actions.py`: `evaluate_action_confidence(...)` + `_attach_confidence(...)` attach structured
  `evidence` + computed `confidence` blocks to `pause_ad` / `increase_adset_budget` /
  `consider_scale_budget` / `refresh_creative`. The executable pause/budget paths apply an
  **abstention guard** (`_abstain_action`): a below-floor sample flips to a non-executable
  `verdict: "insufficient_data"` "keep running" recommendation, never a confident pause/scale.
- `analyze.py`: prose recommendations now carry inline metric/window/sample/spend facts
  (`_recommendation_facts`) and trajectory facts (`_trajectory_facts`).
- `config.py`: added `CONFIDENCE_CONVERSIONS_FLOOR = 25`.
- `confidence.py`: added pure (de)serializers `evidence_to_dict` / `confidence_to_dict` /
  `evidence_from_dict` / `confidence_from_dict` (bands serialize as lowercase name, never a number).

See the implement-stage commit `c173a8c` for the full diff and the implementer's decision log.

## Review findings

Adversarial pass over commit `c173a8c`. Read every touched source file plus the report→plan data
path, the docs the change should have touched, and the consumers of the changed action shape.

### Verified correct (checked, no change needed)

- **Data-shape match (the highest-risk item).** The action plan is built from the *serialized*
  report payload (`_serialize_ad_summary`), not the in-memory summary. Confirmed the serialized
  finding dicts (`budget_waste` / `fatigue_findings` / `scaling_candidates`) carry every field
  `evaluate_action_confidence` reads: `first_seen` / `last_seen` (isoformat strings),
  `total_purchase_count`, `total_spend`, `blended_roas`, `cost_per_app_install`, `ad_id`, `ad_name`,
  `days_active`. No silent `None`-everywhere → no spurious blanket abstain.
- **Band re-derived from source** for a pause (120 purchases / $2400, recent, direct_observation →
  `high`) and an abstain (3 purchases / $40 → `abstain`), independent of the tests. Matches.
- **Abstain → write path is closed.** An abstained action is `executable: False`; `apply_action_plan`
  skips non-executable actions *before* the approval check (`actions.py:345`), so it can never reach
  `build_api_operation` / the Meta write — even if a human flips `status` to `approved`. Re-approving
  an abstained action into an execute requires manually editing `executable` in the JSON too, i.e. a
  deliberate override, not the normal status-only approval flow. Guard holds.
- **Metric selection mirrors `_should_pause_ad` / `_qualifies_for_budget_increase`** for both goals
  (ROAS for `roas`, cost-per-install for `maximize_in_app_subscriptions`), and recency is computed
  from `run_date` (not wall clock) via `_recency_days`. Confirmed.
- **Trajectory prose keys.** `_trajectory_facts` reads `metric` / `percent_change`, which the
  highlight dict provides via `**comparison` (`analyze.py:507`) — the facts populate, not silently
  blank.
- **Backward compatibility.** The structured `evidence` block replaces the old ad-hoc score dict on
  pause/scale/refresh; grep of `src/` confirms no consumer reads the old sub-keys. The new `verdict`
  key is informational and not consumed by the apply path. `pause_ad` executable path (rationale /
  params / executable / approval_required) is unchanged — confidence/evidence are additive.

### Fixed inline (minor)

- **Doc was stale.** `docs/META_ACTION_WORKFLOW.md` documented the approval model but not the new
  `evidence` / `confidence` blocks or the abstention behavior — a meaningful new behavior (an
  executable pause can now be auto-downgraded to non-executable "insufficient data — keep running").
  Added an **"Evidence and Confidence"** section describing both blocks and the significance-floor
  abstention. No other doc (`ARCHITECTURE.md`, `AGENTS.md`) describes the action-plan shape, so none
  else needed touching.

### Filed as new ticket (major / design question)

- **`tickets/backlog/confidence-install-goal-significance.md`** — for install-goal accounts
  (`pollen_sense`), `evaluate_action_confidence` always grounds significance on
  `total_purchase_count`, which those accounts structurally don't generate. Result: an install-goal
  recommendation backed by real volume (verified: 80 installs / $250 spend) is **capped at `low`**
  with factor "thin on conversions," and the 80 installs are ignored; the `evidence` block also shows
  a cost-per-install metric against a purchases sample (internal mismatch). It is conservative
  (under-confidence, never over-confidence) and the abstention guard cannot wrongly fire on a real
  high-waste pause, so it is **not a safety hole** — but it makes confidence meaningless for ~half
  the managed accounts. Deferred (not fixed inline) because *which* conversion should ground an
  install account (subscription results vs app installs) is a product decision, and the cleaner fix
  may rename `Evidence.sample_purchases` → `sample_conversions` in the reviewed/accepted
  `confidence-core`. The implementer did not flag this.

### Considered, intentionally left as-is

- `consider_scale_budget` / `refresh_creative` are **not** abstention-guarded — correct, they are
  non-executable manual actions, so an `abstain` band there is purely informational.
- `_build_budget_increase_action` and `_manual_action` still build the legacy `evidence` dict that
  `_attach_confidence` then overwrites — dead-ish work, but `_manual_action`'s dict is still the live
  evidence for `review_waste_without_ad_id` (which is intentionally not given a structured block, no
  entity to ground). Not worth the churn/risk to refactor.
- `evidence_from_dict` / `confidence_from_dict` are unused until the downstream
  `confidence-operator-brief` ticket, but are pure, tested (round-trip), and the natural home for the
  JSON contract. Acceptable.
- `refresh_creative` grounding tier = `correlational` (judgement call; non-load-bearing). Left.

### Tests / lint

- `.venv/bin/python -m pytest tests/ -q` → **106 passed**. No lint tooling is configured in the repo
  (no ruff/mypy/flake8 in `.venv`, `pyproject.toml`, or any CI config); pytest is the only
  validation gate. The review added no code changes (doc + new backlog ticket only), so the 106
  green tests from implement still hold.
- Test coverage assessment: happy path (high-confidence executable pause), edge (43-purchase
  medium-knee, zero-sample boundary), error/abstain path, grounding-cap + causal flag, backward-compat,
  prose facts, and serializer round-trip are all covered. Gap noted but not blocking: no test exercises
  the degrading/improving **trajectory** prose line (only the waste line), and no test pins the
  install-goal behavior (now tracked by the backlog ticket above).
