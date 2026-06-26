description: Write down how the whole hybrid setup works for the next operator and agent — how reads can come from our direct client or a swappable MCP server (with the optional official login-based server as a later drop-in), the full catalog of every safe write tool and the guardrails on each, and the note that we run as one operator today with multi-user login left for later.
prereq: community-mcp-read-server, enable-and-set-status-write, cbo-aware-budget-write, authoring-evidence-reconcile, audience-rotation-evidence-reconcile, write-config-registry-controls
files: AGENTS.md, docs/META_ACTION_WORKFLOW.md, docs/META_API_SETUP.md, README.md
difficulty: medium
----
## Why

Everything in the hybrid change has landed (provider-agnostic reads + community MCP server +
grounded reversible writes + CBO-aware budgets + authoring + rotation reconciled to evidence/
confidence/review). This ticket makes the documentation reflect that single reality, so an operator
or a fresh agent can understand the read model, the write tool catalog, and the auth posture without
reading source. It runs LAST because it documents the others.

## What to write

### AGENTS.md — read model + write catalog + auth note

- **Read model (hybrid):** reads flow through `MetaReaderProvider` (`DirectMetaReader` = current
  direct client, default; `MCPMetaReader` = community token-based Meta MCP read server, opt-in via
  `META_READER_BACKEND`). State the community server is wired as config NOW and works with the
  existing token; the **official Meta hosted OAuth MCP server is a documented drop-in** (no code
  change — same seam), NOT required now. If `community-mcp-read-server` shipped the server entry as a
  commented/unvetted placeholder, say so honestly — do not claim a live, vetted integration.
- **Auth posture:** single-operator with the current long-lived token is the supported path now;
  multi-user auth / OAuth is a **documented later concern, not built**. One clear paragraph.
- **Write tool catalog:** a table of every guarded write capability and its guardrails — the
  reversible controls (`set_status`/enable-pause, CBO-aware `set_daily_budget` +/-, targeting ops,
  audience rotation, advantage-disable) and authoring (`create_campaign`/`create_adset`/`create_ad`/
  `create_video_ad`/`create_lookalike`, all PAUSED except lookalike audiences which have no status).
  For each: level, reversible vs create-only, and the universal gate (propose→approve→validate_only→
  execute + audit log + PAUSED-by-default + FORBIDDEN_FRAGMENTS + Evidence + computed Confidence +
  review.py). Explicitly state **no delete/archive**.
- Reinforce the existing Guardrails bullets that now apply to ALL writes (not just the action plan):
  evidence+confidence required, abstain on thin data, adversarial review upstream of approval,
  PAUSED-by-default.

### docs/META_ACTION_WORKFLOW.md

- Update the end-to-end workflow so it covers all three write pipelines (action plan, control ops,
  authoring) plus rotation, each now carrying evidence/confidence and reviewed. A single diagram of
  the unified `propose → review → approve → validate_only → execute → audit` flow. Note that rotation
  plans use their own plan keys (`rotations`/`items`/`renames`) reviewed via `review_rotation_plan`,
  while ops/authoring use `review_ops_plan`/`review_authoring_plan` over `plan["ops"]`.

### docs/META_API_SETUP.md

- The community MCP server setup (package + version chosen or the placeholder if unvetted,
  `META_ACCESS_TOKEN` env, the `META_READER_BACKEND` toggle) and the official OAuth drop-in
  instructions. Cross-link the auth posture note.

### README.md

- A short "Hybrid Meta integration" subsection pointing to the above, and update any command list /
  capability summary that now understates the write surface.

## TODO

- AGENTS.md: add the hybrid read-model section, the auth-posture paragraph, and the write tool
  catalog table; refresh Guardrails to say evidence/confidence/review apply to all writes.
- META_ACTION_WORKFLOW.md: unify the workflow + diagram across action/ops/authoring/rotation.
- META_API_SETUP.md: community server + OAuth drop-in + reader-backend toggle.
- README.md: hybrid integration subsection + corrected capability summary.
- Verify every documented command/flag actually exists (grep `__main__.py` / `cli.py`); do not
  document a command that didn't ship.
- No code changes; if a doc claim can't be verified against shipped code, fix the doc to match
  reality (docs follow code), and note any genuine gap as a backlog ticket rather than documenting
  aspirational behavior.

## Edge cases & interactions

- **Docs must match shipped reality, not the plan** — read the final state of `reader_provider.py`,
  `.mcp.json`, `control.py`, `authoring.py`, `rotation.py`, `config.py`, and `__main__.py` before
  writing; treat all existing docs as stale until verified. A command or flag named in a doc but not
  in the code is a defect.
- **Community package specifics** — the exact package name/version and which reads it covers (and
  which fall back to `direct`) come from `community-mcp-read-server`'s handoff; pull them from there,
  don't invent. If it shipped as an unvetted placeholder, document it as such — not as live.
- **Don't over-promise OAuth** — the official server is a *documented* drop-in; be explicit that it
  is not wired/tested here, only that the seam supports it.
- **Single source of truth** — avoid duplicating the full write-catalog table in multiple files;
  put it in AGENTS.md and cross-link from the others to prevent drift.
- **Multi-user auth scope** — explicitly mark it out of scope/not built so a future reader doesn't
  assume it exists; reference where it would plug in (the reader seam + a future per-user token
  store).