description: Audience rotation (the safe swap of which saved audiences an ad set targets) now records the facts and confidence behind each change and is run through the same automatic second-opinion check as every other account-changing action. Reviewed and shipped.
prereq:
files: src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/write_grounding.py, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What landed

Grounding (`evidence` + computed `confidence` + `review`) was attached to the rotation family and a
dedicated, key-aware review gate (`review.review_rotation_plan`) wired in. The rotation arithmetic,
the pre-write live re-read + drift guard, and the "Advantage off only / FORBIDDEN_FRAGMENTS" safety
are untouched. Full detail is in the implement commit (`5eff1b0`); the load-bearing pieces:

- `review.review_rotation_plan` dispatches `plan_type` → the correct item key
  (`audience_rotation`→`rotations`, `advantage_disable`→`items`, `adset_rename`→`renames`), never reads
  `plan["ops"]`, reuses the shared `review_recommendation` core + demote-only `_apply_op_verdict`, is
  idempotent, and never mutates its input.
- `rotation._attach_rotation_grounding` cites each ad set's own window performance at the
  `correlational` tier (caps at `medium` — fatigue is an inference, never `high` from a decline alone);
  no row → zero sample → abstain; `metrics_by_id is None` → structural abstain.
- `rotation._attach_advantage_disable_grounding` → structural abstain (named ad set, no sample);
  renames are exempt (passed through untouched, no fabricated band).
- CLI `propose_rotation_main` gained `--date-from`/`--date-to`, resolves the window via
  `control._resolve_grounding_window`, reads per-ad-set metrics via
  `control.fetch_entity_metrics(level="adset")`, and threads them into `build_rotation_plan`.
- Docs: `docs/META_ACTION_WORKFLOW.md` gained a rotation-grounding subsection and named
  `review_rotation_plan`.

## Review findings

**Diff reviewed first, with fresh eyes, before reading the handoff** (commit `5eff1b0`:
rotation.py / review.py / cli.py / docs / tests).

**Tests + build:** `.venv/bin/python -m pytest tests/ -q` → **268 passed** (267 from implement + 1
added this pass). Byte-compile of the three touched source modules is clean. **Lint/type-check:** the
repo configures no linter or type checker (`pyproject.toml` dev deps are pytest-only; no
ruff/mypy/pyright in `.venv` or config), so there is no lint step to run — pytest is the project's
gate, and it is green.

### What was checked

- **Correctness of the review dispatch** — `_rotation_items` keys on `plan_type`, falls back to first
  present known key, never touches `plan["ops"]`. Confirmed against the `_review_plan_ops` contract it
  mirrors. ✅
- **Band math + tier capping** — correlational ceiling caps at `medium`; causal-flag cap to `low`;
  band-earned recompute; thin/zero sample → `abstain`. Traced through `review_recommendation`,
  `attach_op_grounding`, `confidence.assess`. ✅
- **Idempotency + non-mutation** — verified in code (`_deepcopy_plan`, the `review`-present skip) and
  by the implementer's idempotence/non-mutation tests. ✅
- **Producer↔gate recency consistency** — the CLI feeds both the producer `recency_days` and the
  plan `run_date`/window from the same `_resolve_grounding_window`, and the gate re-derives recency
  from `run_date` + `evidence["window"]`; they agree by construction. ✅
- **Advantage-disable structural abstain + rename exemption** — `op_grounding_gap` allows a
  no-sample abstain; renames carry no `confidence` so the band-gated loop skips them. ✅
- **Apply-path safety** — `apply_rotation_plan` live-drift guard and Advantage-off-only invariant
  unchanged; the drift-precedence test confirms a confidently-grounded approved rotation still blocks
  on live drift. ✅

### Minor — fixed in this pass

- **Coverage gap (production-realistic path):** the implementer tested a *thin* sample but not the
  *empty-metrics* case, which is the realistic CLI shape (`fetch_entity_metrics` returns rows only for
  ad sets that delivered, so the metrics map can omit a proposed ad set). Added
  `test_rotation_adset_with_no_window_row_cites_zero_sample_and_abstains`: `metrics_by_id={}` → the ad
  set cites a **zero** sample → `abstain` → `review_verdict == "insufficient"`. Passes.

### Major — filed as a new ticket (not fixed inline)

- **Rotation grounding has no enforcement teeth.** Every rotation/disable item is built
  `status: "proposed"`, so the review's only enforcement mechanism (`_apply_op_verdict` demoting
  `approved`→`proposed`) is a structural no-op for the rotation family — the review attaches advisory
  metadata only. Control/authoring fail-closed at apply via `op_grounding_gap` when
  `guardrails.requires_grounding` is set; rotation sets neither that flag nor calls the gate, so an
  operator who manually approves an `abstain`-with-sample rotation (e.g. an ad set with no delivery in
  the window) has it executed — the exact case the rest of the system blocks. This is the
  implementer's flagged gap #1, and review confirms propose-time demotion is **not** sufficient
  (nothing is ever built `approved` to demote). Because resolving it means either changing the
  deliberately-frozen apply path *or* explicitly accepting advisory-only grounding — a design call with
  a (low, since rotation is reversible/Advantage-off-only) safety dimension — it is filed as
  `fix/rotation-apply-time-grounding-gate` rather than fixed inline. That ticket also notes the
  `build_rotation_plan` docstring's "demoted ... before it reaches the operator" wording overstates
  the no-op demotion and should be corrected under whichever option is chosen.

### Minor — noted, no change (acceptable as-is)

- **Call-time `from .control import _status_metric`** inside `_attach_rotation_grounding` — required
  (control imports rotation at module load, so a top-level import is circular); `authoring` reuses the
  same private symbol at top level. Acceptable.
- **`metric_name` differs between abstain paths** (`metrics_by_id is None` → `"audience_fatigue"`;
  with-metrics-no-row → goal metric). Harmless (both abstain); production always passes a dict so the
  goal-metric path is what ships. Cosmetic only.
- **CLI live `fetch_entity_metrics(level="adset")` wiring is not unit-tested** (only the injected-
  `metrics_by_id` builder path is). Thin glue mirroring the set_status CLI; left unpinned.
- **DRY:** `review_rotation_plan` duplicates the `_review_plan_ops` body except for the item iterator,
  and `rotation._num` duplicates `review._num`/`control._num`. Minor; not worth the coupling churn.
- **Reversibility is documented, not enforced** (results log captures the prior audience set). Out of
  scope; documentation-only.
