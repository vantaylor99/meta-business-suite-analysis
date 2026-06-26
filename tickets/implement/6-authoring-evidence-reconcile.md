description: When the agent proposes creating a new campaign, ad set, or ad (always created switched-off), that proposal must now also carry the facts and confidence justifying why it's worth building, and pass the automatic second-opinion check — while still being created paused so it never spends on its own.
prereq: guarded-write-evidence-scaffold
files: src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/cli.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## Why

Authoring already exists and is in scope: `authoring.py` creates campaigns/ad sets/ads (and
video-ad/lookalike convenience builders), all forced **PAUSED**, with per-op approval, validate-only,
audit log, and the FORBIDDEN_FRAGMENTS / `_guard_params` block. LOCKED scope keeps authoring
(create-only, PAUSED-by-default, NO delete/archive). What's missing per the grounded-write mandate:
authoring ops carry **no `Evidence`/`Confidence`/`review`**. This ticket **reconciles** authoring
onto the scaffold — it does NOT rewrite authoring or change what gets created.

## What to build

### Ground authoring ops

For each `create_campaign` / `create_adset` / `create_ad` / `create_video_ad` / `create_lookalike`
op, attach grounding via the scaffold's `attach_op_grounding`:

- **Evidence**: the justification for creating the entity. For a duplicate/scale-out of a proven
  winner, the evidence is the source entity's metric over a window (e.g. "duplicating ad X which ran
  ROAS 4.2 on $1.2k / 60 purchases, window ..."). For a net-new creation with no prior data, evidence
  is necessarily absent → `abstain_confidence`. `entity_level` is the level being created; for a
  lookalike, the seed audience's basis.
- **Confidence**: computed via `confidence.assess` (or `abstain_confidence` when net-new) — never
  free-typed. A net-new campaign with no grounding lands at `abstain` → the gate marks it
  `insufficient` (non-executable). This is intended: creating PAUSED is fine, but auto-executing the
  *create* on no evidence should require a conscious operator override.
- Run authoring plans through `review.review_authoring_plan`; the gate stays demote-only and must
  **never touch PAUSED-by-default** (verify `authoring._build_create` still forces `status=PAUSED`
  for every kind in `PAUSED_KINDS` regardless of any review outcome).

### Preserve all existing authoring guarantees

- PAUSED-by-default (`PAUSED_KINDS`, the hardcoded `status=PAUSED`) unchanged. Note: `create_lookalike`
  is in `CREATE_KINDS` but NOT in `PAUSED_KINDS` (audiences have no status) — its grounding must still
  abstain on a thin seed; do not invent a status field for it.
- `_guard_params` / FORBIDDEN_FRAGMENTS unchanged (Meta-AI block stays).
- No delete/archive (out of scope, keep it that way).
- The grounding guard from the scaffold: an **approved** create op missing a confidence block is
  blocked (prevents hand-approving an ungrounded create).

## TODO

- Attach evidence/confidence to each authoring op via `attach_op_grounding`; net-new with no prior
  data → `abstain_confidence` with an explanatory factor.
- Add `review.review_authoring_plan` invocation in the authoring propose/brief path (or document that
  `apply_authoring_plan`'s grounding guard + operator brief invokes it).
- For duplicate-ad / lookalike builders, source the evidence from the seed entity's metrics (reads via
  reader provider).
- CLI proposers (`propose_duplicate_ad_main`, `propose_lookalike_main`, `propose_video_ad_main`,
  authoring create proposers) populate the evidence window.
- Tests (mock-only): duplicate of a proven winner carries computed confidence; net-new create
  abstains → insufficient/non-executable; review demotes an over-claimed duplicate; PAUSED-by-default
  still forced after review; approved-but-ungrounded create is blocked; FORBIDDEN_FRAGMENTS still
  blocks an Advantage+ param.
- Update `docs/META_ACTION_WORKFLOW.md` authoring section (evidence/confidence + PAUSED guarantee
  intact).
- `.venv/bin/python -m pytest tests/ -q` green.

## Edge cases & interactions

- **Net-new creation has no metric** — the common case for a brand-new campaign. Must abstain
  cleanly (not fabricate confidence) and the create stays PAUSED; executing the create then requires a
  conscious override, and going live is a separate `set_status ACTIVE` (the enable ticket). Test this
  whole chain conceptually with a net-new fixture.
- **Duplicate-ad evidence drift** — the source ad's metrics are read at propose time; if the source
  changed by execute, the evidence is stale but the create itself is unaffected (it copies creative).
  Document that evidence reflects propose-time justification, not a live precondition for the create.
- **Lookalike seed quality** — a lookalike's evidence is the seed audience size/quality, which the
  metric pipeline may not express as ROAS/conversions; likely abstains. Don't force a fabricated
  band; abstain with a factor. Remember lookalike is not in PAUSED_KINDS.
- **PAUSED-by-default is non-negotiable** — add an explicit test that no review verdict (including a
  `stands` on a high-confidence duplicate) ever sets a created entity to ACTIVE. The gate is demote-
  only and authoring hardcodes PAUSED; this test pins the invariant against future drift.
- **Audit log** — `write_authoring_results` must still capture op_id/kind/status/created_id; confirm
  the added evidence/confidence/review keys ride along without breaking serialization.
- **Idempotent review** — re-reviewing an authoring plan with existing review blocks is a no-op.