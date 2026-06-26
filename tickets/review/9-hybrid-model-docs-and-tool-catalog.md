description: Documented the whole hybrid Meta setup for the next operator/agent — how reads can come from the direct client or a swappable MCP server, the full catalog of guarded write tools and the guardrail on each, and that we run as one operator today with multi-user login left for later. Docs-only; no code changed.
files: AGENTS.md, docs/META_ACTION_WORKFLOW.md, docs/META_API_SETUP.md, README.md, tickets/backlog/wire-propose-budget-into-m-dispatch.md
difficulty: medium
----
## What shipped (docs only — zero code changes; 271 tests still pass, unchanged)

The whole hybrid integration is now documented to match **shipped reality** (every command/flag/op was
verified against `__main__.py` / `cli.py` / `pyproject.toml` / `control.py` / `authoring.py` /
`rotation.py` / `reader_provider.py` / `.mcp.json` before writing — existing docs were treated as stale
until verified).

### AGENTS.md — the single source of truth
Replaced the old "Read backend (direct vs MCP)" stub with **"Hybrid Meta integration (read model ·
auth · write catalog)"**:
- **Read model:** `MetaReaderProvider` seam, `DirectMetaReader` (default) vs `MCPMetaReader` (opt-in via
  `META_READER_BACKEND`), the 8 covered reads vs the 6 that fall back to `direct`, pagination drain.
- **Community MCP server documented as an UNVETTED PLACEHOLDER** parked under `_candidateMcpServers` in
  `.mcp.json` (not launched, not a live integration). Official OAuth server = **documented drop-in,
  not wired/tested**, only the seam supports it.
- **Auth posture paragraph:** single operator + long-lived `META_ACCESS_TOKEN` now; multi-user/OAuth is
  a documented later concern, **not built**; named the two plug-in points (reader seam swap + a future
  per-user token store).
- **Write tool catalog table** (16 rows): action plan, control ops, authoring, rotation — each with
  level, reversible-vs-create-only, and the per-capability guardrail. Explicit **no delete/archive**.
  Plus a "universal gate" paragraph (propose→approve→validate_only→execute + audit log +
  PAUSED-by-default + FORBIDDEN_FRAGMENTS + Evidence + computed Confidence + review.py) and a note on
  where the review gate sits vs the write gate.
- Refreshed a Guardrails bullet to say evidence/confidence/review apply to **all** writes, not just the
  action plan.

### docs/META_ACTION_WORKFLOW.md
- Added a **unified ASCII diagram** of `propose → review → approve → validate_only → execute → audit`
  covering all four pipelines, plus the explicit note that rotation plans use their own keys
  (`rotations`/`items`/`renames`) reviewed via `review_rotation_plan`, while ops/authoring use
  `review_ops_plan`/`review_authoring_plan` over `plan["ops"]`.
- Documented that apply-time grounding enforcement is **not uniform** (see findings).
- Cross-links to the AGENTS.md catalog instead of duplicating it.

### docs/META_API_SETUP.md
- Resolved the forward-references that said the auth note "lands with this ticket" → now point at the
  AGENTS.md auth posture. Corrected the `ads_management` line to list the full apply surface.

### README.md
- New **"Hybrid Meta integration"** section + corrected the intro capability summary (it understated
  the write surface). **Fixed a stale defect:** the old "the executor uses the installed `meta` CLI …"
  block (lines ~262) contradicted the code — writes go through `MetaMarketingApiClient.update_*`.

### tickets/backlog/wire-propose-budget-into-m-dispatch.md (new)
Filed for a genuine code gap surfaced below.

## Findings that shaped the docs (verify these — they are the load-bearing claims)

1. **`propose-budget` is unreachable via `python -m`** — a real defect, documented honestly, not hidden.
   `propose_budget_main` exists + is tested + is a `pyproject.toml` console script (`propose_budget`),
   but it is **missing from `__main__.py`** (import + dispatch), so `python -m meta_ads_analysis
   propose-budget` → "Unknown command". All docs use the `python -m` form. Documented as a console-script
   caveat in AGENTS.md (footnote ¹), META_ACTION_WORKFLOW.md (invocation note), and README; backlog
   ticket `wire-propose-budget-into-m-dispatch` filed. **Per ticket rules I made NO code change.**
2. **Grounding enforcement is non-uniform — documented as-is, not idealized.**
   - **Hard apply-time gate** (`requires_grounding` → `op_grounding_gap` blocks an approved-but-ungrounded
     write): `set_status` (enable/pause), `set_daily_budget`, authoring `create_*`.
   - **Advisory only** (carries evidence/confidence + runs review, but builders set
     `requires_explicit_approval` not `requires_grounding`, so no apply-time block): the **rotation
     family** (audience_rotation, advantage_disable). This matches the open `fix/rotation-apply-time-grounding-gate`
     ticket, which I reference rather than re-file. Rotation still has its apply-time **live-drift guard**.
   - **Approval-gated only, no grounding attached:** `set_creative_features` (the `propose-creative-features`
     builder sets only `requires_explicit_approval`).
   - **No grounded CLI proposer ships** for `set_creative`, the four targeting ops, or hand-authored
     `create_campaign`/`create_adset` — documented as "hand-authored (no CLI proposer)".
3. **No delete/archive anywhere** — confirmed (`control.py` docstring excludes them; no `delete_*`/
   `archive_*` methods in `meta_api.py`).

## How a reviewer should validate

- **Command existence:** `grep -nE "^def [a-z_]+_main" src/meta_ads_analysis/cli.py`, the dispatch chain
  in `__main__.py`, and `[project.scripts]` in `pyproject.toml`. Confirm every command named in the docs
  resolves, and that `propose-budget` is correctly flagged as console-script-only.
- **Catalog accuracy:** spot-check rows against `control.SUPPORTED_OPS` / `OP_LEVELS` /
  `GROUNDING_REQUIRED_OPS`, `authoring.CREATE_KINDS` / `PAUSED_KINDS` / `GROUNDING_REQUIRED_KINDS`, and the
  `requires_grounding` vs `requires_explicit_approval` flags in each `build_*_plan`
  (`grep -nE "requires_grounding|requires_explicit_approval" src/meta_ads_analysis/*.py`).
- **Reader claims:** the covered/uncovered read split against `reader_provider.DEFAULT_MCP_TOOL_MAP`, and
  the placeholder status against `.mcp.json` (`_candidateMcpServers`, only `code-search` under
  `mcpServers`).
- **Links/anchors:** the README → AGENTS.md anchor
  (`#hybrid-meta-integration-read-model--auth--write-catalog`) and the in-README `#hybrid-meta-integration`
  anchors render on GitHub (the AGENTS.md header contains `·` and parentheses — confirm GitHub's
  slugifier produces the anchor I used).
- **No duplication / drift:** the full catalog table lives only in AGENTS.md; the other three files
  cross-link it.
- `.venv/bin/python -m pytest tests/ -q` → **271 passed** (doc-only; unchanged from before).

## Known gaps / judgment calls the reviewer may want to revisit

- **`upload-video` / `intake-video` are not catalog rows.** `upload-video` *is* a Meta media-library
  write but sits **outside** the propose→approve gate; `intake-video` is local transcription, not a Meta
  write. I mentioned `upload-video` only parenthetically under `create_video_ad`. A reviewer may decide
  `upload-video` deserves its own row/caveat (it writes to the account, ungated).
- **`control.py` module docstring (lines ~10–12) is now mildly stale** — it says targeting "has its own
  guarded tools: rotation + advantage-audience disable," but `control.py` itself now carries
  `TARGETING_OPS` (`set_age_range`/`set_genders`/`set_geo_locations`/`set_placements`). It's a code
  comment, so I left it untouched (no-code-changes rule); flagging for a future code ticket.
- **Anchor-slug risk** (above) is the most likely cosmetic miss — worth a literal check on GitHub.
- The write catalog is wide (a 16-row, 5-column table); it renders fine in GitHub markdown but is dense.
  Acceptable for a reference table; flagging in case the reviewer prefers it split per pipeline.

## End
