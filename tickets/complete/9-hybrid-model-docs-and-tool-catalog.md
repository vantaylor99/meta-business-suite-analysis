description: Documented the whole hybrid Meta setup for the next operator/agent — how reads can come from the direct client or a swappable MCP server, the full catalog of guarded write tools and the guardrail on each, and that we run as one operator today with multi-user login left for later. Docs-only.
files: AGENTS.md, docs/META_ACTION_WORKFLOW.md, docs/META_API_SETUP.md, README.md, src/meta_ads_analysis/control.py, tickets/backlog/wire-propose-budget-into-m-dispatch.md
difficulty: medium
----
## What shipped

The hybrid Meta integration is now documented to match shipped reality across four docs, with the
write catalog living as the single source of truth in **AGENTS.md → Hybrid Meta integration**
(read model · auth posture · 16-row write catalog · universal gate · review-vs-write gate), and
README / META_ACTION_WORKFLOW.md / META_API_SETUP.md cross-linking it rather than duplicating. A new
backlog ticket (`wire-propose-budget-into-m-dispatch`) was filed for the one genuine code gap found.

See the implement commit `daf2922` for the full doc diff. This review pass verified every load-bearing
claim against the code and fixed two minor doc-accuracy findings inline (below).

## Review findings

### Verified accurate (load-bearing claims, checked against code — all correct)

- **`propose-budget` dispatch gap** — verified both by inspection (`__main__.py` has no import or
  dispatch branch for `propose_budget_main`) **and empirically**: `python -m meta_ads_analysis
  propose-budget --help` → `Unknown command: propose_budget`. `propose_budget_main` exists
  (`cli.py:1668`) and is a declared console script (`pyproject.toml:53`). Documented honestly via
  AGENTS.md footnote ¹, META_ACTION_WORKFLOW.md invocation note, and README caveat; backlog ticket
  filed. Per the docs-only scope, no code wiring was added.
- **Console-script asymmetry** — confirmed `propose_creative_features` / `operator_brief` are
  reachable via `python -m` but absent from `[project.scripts]`; captured in the backlog ticket.
- **Write-catalog grounding/level flags** — spot-checked every row against source:
  `OP_LEVELS` / `SUPPORTED_OPS` / `TARGETING_OPS` / `GROUNDING_REQUIRED_OPS` (`control.py:43-73`),
  `CREATE_KINDS` / `PAUSED_KINDS` / `GROUNDING_REQUIRED_KINDS` (`authoring.py:60-68`,
  lookalike correctly *not* in `PAUSED_KINDS`), and the `requires_grounding` vs
  `requires_explicit_approval` split (`set_status` enable/pause + `set_daily_budget` + all authoring
  creates set `requires_grounding`; `set_creative_features` (`cli.py:1966`) and the whole rotation
  family (`rotation.py:348,573,792`) set only `requires_explicit_approval` — matching the
  "advisory only" / "approval-gated only" rows and the open `fix/rotation-apply-time-grounding-gate`).
- **Reader model** — `DEFAULT_MCP_TOOL_MAP` (`reader_provider.py:330`) maps exactly the 8 covered
  reads; the 6 uncovered (`list_custom_audiences`, `get_delivery_estimate`, `search_targeting`,
  `list_pixels`, `list_custom_conversions`, `iter_paginated`) are `None` and `_tool_for` raises a
  clear `NotImplementedError` naming the read. `reader_from_env` raises a clear `RuntimeError` when
  `mcp` is selected without a tool-executor (CLI path) — matches the docs.
- **MCP placeholder status** — `.mcp.json` has only `code-search` under `mcpServers`;
  `meta-ads-mcp-server@1.5.1` sits under non-launched `_candidateMcpServers` with a DISABLED/UNVETTED
  README. Matches the "unvetted placeholder" framing. Official OAuth server correctly described as a
  config-only, untested drop-in.
- **No delete/archive** — confirmed: no `delete_*` / `archive_*` methods in `meta_api.py`.
- **Review functions** — `review_action_plan` / `review_ops_plan` / `review_authoring_plan` /
  `review_rotation_plan` all exist (`review.py`); the per-pipeline key routing claim is accurate.
- **Budget caps** — `max_increase_percent` default 20 (`actions.py:449`); `MAX_BUDGET_DECREASE_PERCENT`
  50 — match the catalog.
- **README anchors** — slug for the AGENTS.md header `Hybrid Meta integration (read model · auth ·
  write catalog)` resolves to `hybrid-meta-integration-read-model--auth--write-catalog` under GitHub's
  slugifier (middot dropped, leaving the double hyphens the README link already uses); the in-README
  `#hybrid-meta-integration` anchors also match. Cross-file links to `../AGENTS.md` are file links, not
  anchors, so they cannot break.
- **Tests** — `.venv/bin/python -m pytest tests/ -q` → **271 passed** (before and after my edits).
- **Lint** — no linter is configured in this repo (`pyproject.toml` declares only pytest); nothing to
  run. Stated explicitly rather than silently skipped.

### Found and fixed inline (minor)

- **Stale `control.py` module docstring** (`control.py:6-12`) — it described the write vocabulary as
  just `set_status` / `set_daily_budget` / `rename` and claimed targeting "has its own guarded tools:
  rotation + advantage-audience disable", directly contradicting the `TARGETING_OPS`
  (`set_age_range` / `set_genders` / `set_geo_locations` / `set_placements`) now living *in*
  `control.py`. The implementer flagged this but deferred it under the implement-stage no-code rule;
  the review stage permits the inline fix. Rewrote the docstring to list the full vocabulary and
  correctly attribute creation → `authoring.py` and rotation/advantage-disable → `rotation.py`.
  Comment-only; 271 tests still pass and the module imports cleanly.
- **Ungated media-library uploads were under-documented** — `upload-video`
  (`MetaMarketingApiClient.upload_video`) and the ad-authoring `upload_image` are real account writes
  that bypass the propose→approve→validate→execute gate entirely, so the "every write is gated" framing
  in AGENTS.md and the README was categorically overbroad. Added an explicit caveat to the AGENTS.md
  catalog explaining the exception (uploads create an inert, unreferenced asset — no status/budget/
  delivery; nothing spends until a *gated* `create_*` references it) and a short parenthetical to the
  README pointing at it. The implementer had flagged this as a reviewer judgment call.

### Major findings filed as tickets

- None new. The two real code gaps already have tickets: the `propose-budget` dispatch gap →
  `tickets/backlog/wire-propose-budget-into-m-dispatch` (filed by the implement pass; verified
  accurate), and rotation's missing apply-time grounding gate →
  `tickets/fix/rotation-apply-time-grounding-gate` (pre-existing; correctly referenced by the docs).

### Considered, no action needed

- **Catalog density** (16 rows × 5 columns) — renders fine in GitHub markdown; acceptable for a
  reference table. Not split.
- **`set_creative_features` is in `GROUNDING_REQUIRED_OPS`** yet its builder sets only
  `requires_explicit_approval` — not a contradiction: the apply-time gate (`control.py:518`) fires only
  when the plan opts in via `requires_grounding`, which this builder deliberately does not. The catalog
  describes shipped builder behavior correctly.

## Result

Docs accurately reflect the shipped hybrid integration; two minor accuracy gaps fixed inline; one
code-comment docstring corrected; 271 tests pass; no new tickets required beyond those already filed.
