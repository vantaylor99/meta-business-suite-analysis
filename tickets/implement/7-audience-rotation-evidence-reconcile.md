description: The existing audience-rotation tool (which safely swaps which saved audiences an ad set targets) must now also carry the facts and confidence behind each rotation and pass the automatic second-opinion check — bringing it in line with every other account-changing action. No new rotation behavior is added.
prereq: guarded-write-evidence-scaffold
files: src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/cli.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## Why

Audience rotation is **already built** (`rotation.py`): `build_rotation_plan`, `compute_new_targeting`,
`apply_rotation_plan` (re-reads live targeting + validates no drift before each write),
`build_advantage_disable_plan` (turn Advantage Audience off only), and `build_rename_plan`. It's in
scope as a reversible control and is NOT to be rebuilt. The only gap vs the grounded-write mandate:
rotation plans carry no `Evidence`/`Confidence`/`review`. This is a **reconcile-only** ticket: attach
grounding and wire the review gate; change no rotation logic.

## CRITICAL — rotation plans use DIFFERENT keys than control ops (verified)

Do NOT route rotation through `review.review_ops_plan` — that wrapper iterates `plan["ops"]`, which
rotation does NOT produce. Verified plan shapes in `rotation.py`:
- `build_rotation_plan` → `plan["rotations"]` (each item has its own `status` proposed/approved).
- `build_advantage_disable_plan` → `plan["items"]` (each item has `status`).
- `build_rename_plan` → `plan["renames"]` (pure structural — name only).

You MUST add a dedicated `review.review_rotation_plan(plan, ...)` that dispatches on `plan_type`
(or on which key is present) and iterates the correct list, reusing `review_recommendation` +
the demote-only verdict applier per item. Reuse the gate's per-recommendation core; do NOT fork the
refutation logic. Rename plans (`renames`) are pure structural/no-metric — exempt them from grounding
(consistent with the scaffold's rename exemption), or attach a structural `abstain_confidence`; do
NOT fabricate a performance band for a rename.

## What to build

### Ground rotation items

Attach grounding to each rotation item (`plan["rotations"]`) via the scaffold's `attach_op_grounding`:
- **Evidence**: the fatigue/performance signal motivating the rotation (e.g. the ad set's recent
  metric decline over the window that justifies swapping audiences), with sample + regenerating
  query. `entity_level=adset`, `entity_id`/`entity_name`.
- **Confidence**: computed via `confidence.assess` (likely `correlational` tier — "audience fatigue"
  is an inference, not a direct observation, so the band caps appropriately) or `abstain_confidence`
  when the sample is thin. Never free-typed.
- The advantage-disable items (`plan["items"]`) are safety/structural ops (turning OFF Meta-AI
  audience automation); treat each like a no-metric structural op per the scaffold policy (abstain
  with an explanatory factor) rather than fabricating a performance band.
- Run rotation and advantage-disable plans through `review.review_rotation_plan` (the new
  key-aware wrapper). Rename plans pass through exempt/structural.

### Preserve rotation's existing safety

- The pre-write live re-read + drift validation in `apply_rotation_plan` is unchanged.
- `disable_advantage_audience` only turns automation OFF (never on); FORBIDDEN_FRAGMENTS interaction
  unchanged.

## TODO

- Add `review.review_rotation_plan` that dispatches on plan key (`rotations` / `items` / `renames`)
  and reuses `review_recommendation` + the demote-only applier per item; idempotent skip-guard.
- Attach evidence/confidence to `rotations` items via `attach_op_grounding` (correlational tier for
  fatigue inference; abstain on thin sample).
- Attach structural `abstain_confidence` to advantage-disable `items`; exempt `renames`.
- CLI `propose_rotation_main` populates the fatigue-window evidence (reads via reader provider).
- Tests (mock-only): a fatigued ad set rotation carries computed (correlational-capped) confidence;
  thin sample abstains → non-executable; review demotes an over-claimed rotation; advantage-disable
  item attaches a structural abstain; a rename plan passes through without a fabricated band; the
  existing drift-validation still blocks a rotation when live targeting changed since plan time.
- Update `docs/META_ACTION_WORKFLOW.md` rotation section (evidence/confidence + drift guard intact).
- `.venv/bin/python -m pytest tests/ -q` green.

## Edge cases & interactions

- **Wrong plan key is the #1 failure mode** — if `review_rotation_plan` iterates `plan["ops"]` it
  silently reviews nothing. Pin a test that a rotation plan's items actually receive review blocks.
- **Fatigue is correlational** — never let a rotation claim `high`/causal confidence from a decline
  alone; the `causal` review check must downgrade any cause-claim not grounded in an A/B. Test that a
  rotation rationale asserting "audience caused the drop" is downgraded.
- **Drift validation precedence** — grounding/review runs at propose; the live-targeting drift check
  runs at execute. If targeting drifted, the write is blocked regardless of confidence band. Confirm
  ordering: a high-confidence rotation still blocks on drift.
- **Advantage-disable has no performance metric** — must abstain with a clear structural factor, not
  a fabricated band; it's a safety toggle, and review must not refute it for "contradicting its
  metric" (it has none). Verify the gate skips/abstains rather than refutes.
- **Rename items have no metric and no spend impact** — exempt from grounding-required; do not block
  a rename for lacking confidence.
- **Audience rotation is reversible** — rotating back is itself another rotation; document that the
  audit log captures the prior audience set so a rotation can be reversed.
- **Idempotent review** — re-review of a rotation plan with existing review blocks is a no-op.