description: A simple, written walkthrough (plus any config needed) so I can start the MCP server on my own machine, connect Claude to it, and actually try reading data and running a safe propose-approve-execute cycle — all against mocks, no live account.
prereq: mcp-read-tools, mcp-guarded-write-tools, mcp-guarded-write-authoring-rotation, mcp-local-approval-gate
files: docs/META_API_SETUP.md, docs/META_ACTION_WORKFLOW.md, README.md, .mcp.json
difficulty: easy
----
## Why

Once the server has read tools, guarded write tools, and a real approval gate, the user wants to
**launch it locally and play with it** — see how the connect → read → propose → approve → execute
loop feels through an MCP client before any hosting/Azure work. This ticket is the glue + docs that
make that a copy-paste experience, not a treasure hunt.

## Scope / what "done" looks like

- A single doc section ("Run the Meta MCP server locally") that walks through, in order:
  - install the `server` extra;
  - launch the server (HTTP, localhost) with the `direct`-reader + fake/mocks posture that makes **no
    live Meta calls**, and how to point it at a real token later when the user is ready;
  - the exact `.mcp.json` (or client connector) entry to connect Claude Code / Cowork to the local
    HTTP server, including where the role/confirmation header goes;
  - a scripted first session: call `server_info`, run one read tool, then walk one write end to end —
    `propose_*` → human confirm → `execute` → outcome verify.
- `.mcp.json`: promote the parked server entry to a real, launchable local entry (or document the
  exact client config), while leaving `code-search` untouched.
- Cross-links from `README.md` and `docs/META_ACTION_WORKFLOW.md` so the local-run path is discoverable
  next to the existing CLI workflow docs.

## Constraints

- **Default to mocks / no live calls** in the documented happy path; make going live an explicit,
  clearly-marked opt-in step (token with correct scope, sandbox account first) — consistent with the
  repo's build-safety rule and the "verify next-day spend = $0" pausing lesson.
- Docs must match shipped tool names/behavior from `mcp-read-tools` / `mcp-guarded-write-tools`; don't
  document aspirational tools.

## Edge cases & interactions

- What the user sees if they connect before launching the server, or with the wrong port → include a
  quick troubleshooting note.
- Make explicit that this is **single-operator, local** — multi-user auth, roles, and the Azure
  knowledge store are separate backlog work, so nobody mistakes the local rig for the production shape.
