# Tess

*From Latin "tessera" — a ticket or token.*

Tess is a lightweight, agent-driven ticketing system for software projects. It provides a structured pipeline where AI coding agents (Claude, Cursor, Augment, Codex) process tickets through workflow stages — from triage through implementation and review to completion.

When using the Codex adapter, `codex-cli` must be version `0.112.0` or newer.

Tess lives as its own repository and integrates into any project, giving every repo the same ticket pipeline without duplicating code.

## How It Works

Tickets are markdown files organized into stage folders inside a project's `tickets/` directory. Each ticket file is named with an optional sequence prefix (`3-my-feature.md` — lower runs sooner) and contains a lightweight metadata header followed by architecture notes and TODO items. The sequence prefix is optional; unnumbered tickets follow after all numbered ones in a stage.

A runner script processes tickets one at a time, invoking an AI agent for each. The agent owns the full stage transition: it creates the next-stage file(s), deletes the source ticket, and commits. The runner chooses what to work next under one of three strategies — **live** (default; re-discover and re-prioritize the whole board after every transition, picking up tickets created mid-run), **batch** (snapshot at startup; drain stage-by-stage), or **chase** (snapshot at startup; follow one ticket through every stage before moving to the next). See [Strategies](#strategies) below.

```
tickets/
├── backlog/       # Parked specs — not yet ready to work
├── fix/           # Bug triage and reproduction
├── plan/          # Feature design and research
├── implement/     # Ready for implementation
├── review/        # Code review and validation
├── complete/      # Archived completed work
├── blocked/       # Parked — unresolved questions
├── AGENTS.md      # Points to tess agent rules
├── CLAUDE.md      # Points to tess agent rules
├── .version       # Ticket format version (managed by tess)
├── .logs/         # Agent execution logs (git-ignored)
└── .in-progress   # Current ticket state for resume (git-ignored)
```

## Quick Start

### 1. Install tess into your project

```bash
# Git submodule:
git submodule add https://github.com/gotchoices/tess.git tess
node tess/scripts/init.mjs

# Git subtree (works with git worktrees; submodules do not):
git subtree add --prefix=tess https://github.com/gotchoices/tess.git main --squash
node tess/scripts/init.mjs

# Symlink (tess cloned elsewhere):
node /path/to/tess/scripts/init.mjs
```

This creates the `tickets/` folder with stage subdirectories and connects tess's agent rules into your project.

### 2. Create a ticket

Drop a markdown file into `tickets/fix/`, `tickets/plan/`, or `tickets/backlog/`:

```
tickets/plan/3-user-auth.md
```

```markdown
description: Add JWT-based authentication
prereq: session-store, user-model
files: src/server.ts, src/middleware/auth.ts
----
Design a JWT auth flow with refresh tokens.

- Access tokens: short-lived (15min)
- Refresh tokens: long-lived, stored httpOnly
- Middleware to protect routes

TODO
- Define token schema and expiry strategy
- Implement login/refresh endpoints
- Add auth middleware
- Write integration tests
```

`prereq:` lists slugs of other tickets that must land (advance stage) first — no sequence prefix, no `.md` extension, since the sequence can change. The runner topologically sorts each stage to respect these edges and errors on cycles or sequence numbers that violate them.

**Cross-stage prereqs.** Prereqs are resolved across the whole pipeline, not just the current stage. The runner ranks stages as `backlog (0) < fix = plan (1) < implement (2) < review (3) < complete (4)` and treats a prereq as *satisfied* only when it sits in a strictly later rank than its dependent (same-stage ordering is enforced by topo sort). Practical effect:

- Prereq still in an earlier stage, in a peer-but-different stage (e.g. dependent in `plan/` with prereq still in `fix/`), or parked in `blocked/` → the dependent is **deferred** for this run and any sibling listing it as `prereq:` is deferred too. The cascade is transitive through the queue.
- Unresolved prereq slugs (not present anywhere in the pipeline) are assumed already complete and ignored.

Agents do **not** need to mirror this state by hand — `blocked/` is reserved for human sign-off and missing external code, never for "my prereq isn't done yet." See `agent-rules/tickets.md` for the agent-facing rule.

Pass `--skip-blocked` to pre-filter the snapshot: any ticket whose prereq chain transitively reaches a slug in `blocked/` is dropped before the run starts, so it never appears in the dry-run listing or the live banner. This is a stricter, upfront filter — the runtime cross-stage gate still handles the broader cases (prereq still in plan, peer-stage mismatch, etc.) by deferring at the moment of processing.

### 3. Run the pipeline

```bash
# See what would be processed
node tess/scripts/run.mjs --dry-run

# Process all tickets
node tess/scripts/run.mjs

# Only specific stages
node tess/scripts/run.mjs --stages fix,implement

# Cap each stage to its own max sequence (work only the earliest slots)
node tess/scripts/run.mjs --stages fix:15,plan:15,implement:12,review:10

# Include backlog for a promote-from-backlog pass (not in the default set)
node tess/scripts/run.mjs --stages backlog:15

# Use a different agent
node tess/scripts/run.mjs --agent cursor

# Chase a ticket through every stage before moving on
node tess/scripts/run.mjs --strategy chase
```

### Options

| Option | Default | Description |
|---|---|---|
| `--max-sequence <n>` | _unlimited_ | Default sequence ceiling for all stages (sequences can include decimals). Unnumbered tickets are skipped whenever this is finite. |
| `--stages <list>` | `fix,review,implement,plan` | Stages to process, with optional per-stage max (`implement:12,review:10`). The order is the cross-stage priority (earlier = higher). `backlog` is a valid target but excluded from the default set. |
| `--agent <name>` | `claude` | Agent adapter: `claude`, `cursor`, `auggie`, or `codex` |
| `--strategy <name>` | `live` | Selection strategy: `live`, `batch`, or `chase`. See [Strategies](#strategies). |
| `--max <n>` | _unlimited_ | Stop after processing at most n tickets (with `live`, caps stage transitions rather than snapshot size) |
| `--token-budget <n>` | _unset_ | Soft per-ticket context budget (claude only). When the running context size crosses *n* tokens, a one-shot `BUDGET_WARNING` is injected via a PreToolUse hook so the agent splits residual work into continuation tickets. See [Token Budget](#token-budget). |
| `--no-commit` | — | Skip automatic git commit after each ticket (also skips the migration commit) |
| `--skip-blocked` | — | Pre-filter the snapshot: drop any ticket whose prereq chain reaches a slug parked in `blocked/`. The runtime cross-stage prereq gate still applies to other misses. |
| `--refresh-index` | — | Run the local code indexer incrementally before each ticket. No-op if `tickets/.index/` does not exist. See [Local Code Search](#local-code-search-optional). |
| `--prune-completed-days <n>` | `30` | Remove completed tickets whose landing commit is older than *n* days. Runs once per run. See [Pruning Completed Tickets](#pruning-completed-tickets). |
| `--no-prune-completed` | — | Skip the stale-completed-ticket sweep entirely. |
| `--dry-run` | — | List tickets without invoking the agent |

### Init Options

| Option | Default | Description |
|---|---|---|
| `--ignore-stages` | — | Add ticket stage folders (fix/, plan/, etc.) to .gitignore |
| `--no-ignore-stages` | — | Keep ticket stage folders tracked in git |
| `--with-search` | — | Wire the MCP code-search server for the chosen agent |
| `--no-search` | — | Skip the MCP code search prompt |
| `--with-commit-hook` | — | Install a post-commit hook that refreshes the index after every commit |
| `--no-commit-hook` | — | Skip the commit-hook prompt |
| `--agent <name>` | `claude` | Target agent for `--with-search`: `claude`, `cursor`, `codex`, `auggie` |

When neither flag is passed, init will prompt interactively. The default is to **not** ignore stage folders. Use `--ignore-stages` when each developer maintains separate tickets that shouldn't be committed to the shared repo.

## Strategies

The runner picks the next ticket to work using a strategy. All three strategies share the same agent invocation, logging, and commit pipeline — they differ in how they choose the next ticket. `live` reassesses the board continuously; `batch` and `chase` traverse a snapshot frozen at startup.

### `live` (default)

After **every** stage transition, live re-discovers the entire ticket board from disk and re-applies the priority policy, then runs the current highest-priority ticket. The policy is the same one `batch` uses — cross-stage order from `--stages` (default `fix,review,implement,plan`: drive in-flight work toward done before opening new work), and within each stage prereqs before dependents then lower sequence first — but it is re-evaluated each iteration instead of once.

Because it reads disk every iteration, a ticket created mid-run is picked up the same run: a `review` that files a `fix` sees that fix jump to the front (fix is highest-priority) and resolved next; a `plan` that splits into several `implement` tickets sees them ranked in immediately. A ticket whose prereq is still *behind but advancing* is skipped only for the current pass and becomes selectable the moment its prereq moves forward — so an entire prereq chain can drain in one run.

A slug that errors or times out is excluded for the rest of the run (next run resumes it via its resume note); its dependents stay gated behind it. A per-slug transition cap (12) and a global run cap backstop an agent that regresses or re-spawns a ticket in a loop. `--max <n>` caps the number of transitions (not a snapshot length).

Best for: unattended runs that should clear the whole pipeline — including the follow-up work earlier stages generate — in a single invocation, always working the most important thing next.

### `batch`

Snapshot the ticket list at startup, then drain each stage in topo/sequence order: every snapshotted ticket advances exactly **one** stage per run, and tickets created during the run roll into the next run. The pipeline-wide order is `--stages` (default `fix,review,implement,plan`); within each stage, prereqs come before dependents and lower sequences come first.

Best for: steady, reviewable progress with a fixed, predictable batch per run. Each run produces a clean one-transition-per-ticket diff so you can inspect what each stage did before the next pass.

### `chase`

Pick one root ticket and follow it through **every** stage to `complete/` before moving to the next root. Ticket-major instead of stage-major.

After each stage transition, chase looks up the same slug in any forward-ranked stage (an agent is free to jump straight from `fix/` to `review/` when no separate implementation pass is needed), then in `blocked/` and `backlog/`. It does **not** rely on a filesystem diff — other agents may be modifying `tickets/` in parallel. If the slug landed somewhere past its current stage, the chase continues from there; if it landed in `blocked/` or `backlog/`, the chain ends and the slug is recorded as **deferred** for the rest of the run.

**Deferral cascade.** A slug enters the run's deferred set when the agent moves it to `blocked/` or `backlog/`, when the cross-stage prereq gate rejects it because a prereq is still behind, *or* when the agent errors on it. A queued root that lists a deferred slug as `prereq:` is skipped — and the skipped root is itself added to the deferred set, so the skip cascades transitively through the queue. The same cascade applies in `batch` mode. This prevents tess from charging into work whose prerequisite just bounced, hasn't caught up, or failed — without throwing away independent work elsewhere in the queue. Any agent errors collected during the run surface as a non-zero exit code once the runner finishes the rest of the snapshot.

**Splits.** If an agent splits one ticket into multiple next-stage tickets, chase follows the same-slug branch and leaves the siblings in place for the next run.

**Safety cap.** A single chain is bounded to 6 stage transitions, in case an agent regresses a ticket (e.g. `implement` → `plan`) and creates a loop. The natural pipeline tops out at 4 (`backlog → plan → implement → review → complete`).

Best for: focused work on a single feature, or when you want fewer parallel work-in-progress trails in git history.

```bash
# Default — live: reassess the board after every transition
node tess/scripts/run.mjs

# Snapshot at startup, drain stage by stage
node tess/scripts/run.mjs --strategy batch

# Follow each root ticket all the way through
node tess/scripts/run.mjs --strategy chase

# Live, but stop after 3 transitions
node tess/scripts/run.mjs --max 3
```

## Token Budget

A long-running ticket can outgrow the model's context window mid-task, leaving an interrupted commit that is awkward to resume from. The `--token-budget <n>` flag (claude only) gives you a soft cushion: the runner watches Claude's per-turn context size and, when the threshold is crossed, injects a one-shot `BUDGET_WARNING` through a PreToolUse hook. The agent's instructions (in `agent-rules/tickets.md`) tell it to stop investigating, capture remaining TODOs as continuation ticket(s) in the **same** stage, delete the source ticket, and exit cleanly.

```bash
# Suggested starting point — claude's context is 200k.
node tess/scripts/run.mjs --token-budget 160000
```

The warning is purely advisory; the agent stays in control. After the agent splits and the runner commits, behavior depends on strategy:

- **live** re-discovers the board and picks up the new same-stage continuations immediately, ranked against everything else.
- **chase** picks up the new same-stage continuations as part of the current chain (depth-first, before advancing the original slug forward).
- **batch** lets the continuations roll into the next run, preserving the snapshot-once-per-run guarantee.

The budget applies per ticket — every new ticket invocation starts from zero.

## Pruning Completed Tickets

`complete/` is an archive of finished work, and left alone it grows without bound. At the start of every run (before snapshotting), the runner removes completed tickets that landed more than 30 days ago and commits the deletion as `tess: prune <n> completed ticket(s) older than <d> days`.

Age is measured by each file's most-recent **git commit timestamp**, not its filesystem mtime — a checkout rewrites mtimes, but the commit date reflects when the ticket actually reached `complete/`. Untracked completed tickets (no commit history) are left alone since they can't be dated. Because pruning is a tracked deletion, anything removed stays recoverable from git history.

```bash
# Keep a 90-day archive instead of the default 30
node tess/scripts/run.mjs --prune-completed-days 90

# Turn the sweep off
node tess/scripts/run.mjs --no-prune-completed
```

`--dry-run` reports what the sweep would remove without deleting anything. The sweep also honors `--no-commit` (deletes the files but leaves the commit to you).

## Local Code Search (optional)

Tess can build a local vector index of the repository and expose it to the agent as an MCP `search_code` tool.  No API keys, no network calls after the first model download.

Three pieces, each independent:

1. **Indexer** — `node tess/scripts/index.mjs` walks `git ls-files`, chunks each file, embeds the chunks with a local code-aware embedding model (`jinaai/jina-embeddings-v2-base-code`, 768-dim, ~155MB quantized on first run), and stores vectors in `tickets/.index/index.db` (sqlite + sqlite-vec).  Incremental by content hash — re-running on a typical diff is sub-second.  If you have an existing index from the legacy MiniLM model, the indexer will refuse to open it and point you at `--rebuild`.
2. **MCP server** — `tess/scripts/mcp-search.mjs` is a stdio MCP server exposing `search_code`, `find_references`, and `read_chunk` against the same DB.  Started by the agent, dies with it; nothing runs in the background between invocations.
3. **Per-agent config** — `init` writes the right MCP config for the chosen agent (Claude `.mcp.json`, Cursor `.cursor/mcp.json`, codex sample TOML).

### Enable it

```bash
node tess/scripts/init.mjs --with-search --agent claude
```

That single command writes the MCP config, runs `npm install` inside `tess/`, builds the initial index (the first run downloads a ~155MB embedding model), and appends a `## Code search (tess)` section to your project's root `AGENTS.md` so any agent — not just the tess runner — is pointed at the index.  Re-running is safe and incremental.

### Keep it fresh

```bash
node tess/scripts/index.mjs                    # incremental refresh
node tess/scripts/index.mjs --watch            # debounced fs watcher
node tess/scripts/index.mjs --status           # row counts + last refresh
node tess/scripts/index.mjs --config           # show effective filter config
node tess/scripts/index.mjs --rebuild          # full rebuild
node tess/scripts/run.mjs --refresh-index ...  # refresh between every ticket
```

For hands-off freshness, pass `--with-commit-hook` to `init.mjs` (or accept the prompt).  This installs a `.git/hooks/post-commit` that fires the indexer in the background after every commit — the commit feels instant; the index trails by a second or two.  Remove the `# >>> tess search index >>>` block from the hook to disable.

All artifacts live under `tickets/.index/` (gitignored).  Full uninstall: delete that folder and remove the `code-search` entry from your agent's MCP config.

### Customize what gets indexed

By default the indexer skips `node_modules/`, `dist/`, `build/`, `.git/`, `tickets/`, `team/`, `docs/`, plus a handful of cache folders, and indexes a fixed list of source extensions. The `docs/` and `team/` defaults exist because long-form prose dominates the embedding signal vs. actual code, dragging down the rankings of real source matches.

To override either set, create `tickets/index-config.json`:

```json
{
  "exclude":    ["examples/", "vendor/"],
  "include":    ["docs/architecture/"],
  "extensions": [".graphql", ".proto"]
}
```

- `exclude` — additional directory prefixes to skip (joined with the defaults).
- `include` — re-include a path under an otherwise-excluded directory. Checked before `exclude`, so e.g. `docs/architecture/` lets you index that subtree while leaving the rest of `docs/` out.
- `extensions` — additional file extensions beyond the built-in source list (lowercase, leading dot optional).

All entries are directory-prefix matches (trailing `/` added if missing) — same semantics as the built-in excludes, applied to `git ls-files` output. Inspect the merged result any time with `node tess/scripts/index.mjs --config`. Edits take effect on the next refresh; no rebuild needed.

The config is also visible (and the only way to change it is by hand on disk) from the dashboard's Index page at `/index`.

### Query from the command line

`tess/scripts/search.mjs` is a thin CLI over the same index, sharing all ranking and formatting logic with the MCP server — useful for ad-hoc exploration without an agent in the loop.

```bash
# Semantic search (default mode)
node tess/scripts/search.mjs "where do we evict pages from the buffer pool"
node tess/scripts/search.mjs -k 5 --path "packages/lamina-substrate/%" "page eviction"

# Literal search; "|" ORs alternatives
node tess/scripts/search.mjs --refs "composeNewSlot|defaultComposeNewSlot"

# Read a line range (handy for expanding a snippet you just got back)
node tess/scripts/search.mjs --read packages/lamina/src/index.ts:120-160

# JSON output for piping into jq / scripts
node tess/scripts/search.mjs --json "page eviction" | jq '.matches[0].path'
```

The script has a shebang and is marked executable, so on Unix you can also invoke it directly (`./tess/scripts/search.mjs "..."`). After `npm install` inside `tess/`, the bin entry exposes it as `tess-search` (use `npx tess-search` from the project root, or symlink it onto your `PATH`). Exit codes: `0` on hits, `1` on no hits, `2` on usage / missing-index errors.

## Ticket Lifecycle

```
backlog/ ─→ plan/ ─┐
                   ├─→ implement/ ──→ review/ ──→ complete/
            fix/ ──┘
                   ↕
               blocked/
```

- **backlog** — Parked specifications that aren't ready to work yet (promoted to `plan/` when ready)
- **fix** — Reproduce a bug, research cause, output implementation ticket(s)
- **plan** — Design a feature, resolve questions, output implementation ticket(s)
- **implement** — Build it, ensure tests pass, output review ticket
- **review** — Inspect code quality, verify tests, update docs, output complete ticket
- **complete** — Archived summary of finished work
- **blocked** — Parked when there are unresolved questions or decisions

## Ticket Format

```markdown
description: <brief description>
prereq: <slugs of other tickets that must land first — comma-separated, no prefix, no .md>
files: <optional list of relevant files>
difficulty: <optional: easy | medium | hard — defaults to medium>
----
<Architecture description — prose, diagrams, interfaces/types>

<TODO list of sub-tasks, organized by phase if needed>
```

**Filename convention:** `<slug>.md` with an optional `<sequence>-` prefix where lower sequence runs sooner (integer or decimal, e.g. `3-my-feature.md` or `3.5-my-feature.md`). The sequence number is not part of the ticket's identity — reference tickets by slug only in `prereq:`.

**Difficulty (`easy` | `medium` | `hard`, default `medium`):** a portable, agent-agnostic estimate of how much horsepower a ticket needs. The runner maps it — together with the pipeline stage and per-agent config — to a concrete model and reasoning-effort. See [Model & Effort Selection](#model--effort-selection). Reserve `hard` for genuinely demanding work (it selects the strongest, most expensive model) and `easy` for mechanical changes.

## Model & Effort Selection

Tickets carry only a portable `difficulty:`; the concrete **model** and **reasoning-effort** are API-specific, so they live in a tess-level config (`tess/config/agents.json`) keyed per agent rather than on the ticket. Each agent adapter resolves them at invocation time, so the same `difficulty` notion works across `claude`, `codex`, `cursor`, etc. — each using its own model names and effort vocabulary.

The selection has a compact base rule plus sparse per-cell overrides:

- **Difficulty picks the model tier** — so the strongest model is reserved for the hardest tickets.
- **Stage picks the effort** — `implement` runs hottest, the rest a notch lower.
- **`overrides[stage][difficulty]`** pins a specific cell's model and/or effort when the base rule doesn't fit.

The resulting defaults for `claude` (in `scripts/lib/model-selection.mjs`, overridable by `config/agents.json`), shown as model · effort:

| stage | easy | medium | hard |
|---|---|---|---|
| fix / plan | `sonnet-4-6` · high | `opus-4-8` · high | `fable-5` · high |
| implement | `sonnet-4-6` · xhigh | `opus-4-8` · xhigh | `fable-5` · xhigh |
| review | **`opus-4-8` · medium** | `opus-4-8` · high | `fable-5` · high |

The `medium` column reproduces the historical effort profile (`xhigh` for `implement`, `high` elsewhere) while pinning the model explicitly instead of inheriting whatever was last configured interactively. `easy` saves cost with Sonnet; `hard` escalates to Fable 5. The bolded cell is an override: an `easy` review still runs on Opus (cheap models miss bugs) but at reduced effort.

`config/agents.json` is deep-merged over the built-in defaults, so a partial file only restates what it changes. A `null` or missing model/effort means "pass no flag — use the agent's own default," which is how every non-`claude` agent behaves until you add a block for it. Example — pin a cross-cutting cell and add a `codex` policy:

```json
{
  "claude": {
    "overrides": {
      "review": { "easy": { "model": "claude-opus-4-8", "effort": "medium" } }
    }
  },
  "codex": {
    "model": { "easy": "gpt-5-mini", "medium": "gpt-5", "hard": "gpt-5" },
    "effort": { "implement": "high", "default": "medium" }
  }
}
```

> **Note:** effort vocabularies are model-specific — a value valid for one model may be rejected by another. If a model in a given tier rejects an effort value, set a supported one for that stage in the config.

## Stopping the Runner

Create a `tickets/.stop` file to gracefully halt the runner between tickets:

```bash
touch tickets/.stop
```

The runner checks for this file before each ticket. When found, it finishes any in-progress commit, removes the stop file, and exits. The `.stop` file is git-ignored.

## Incomplete Run Recovery

The runner tracks which ticket is currently being processed in `tickets/.in-progress`. If a run is interrupted (disconnection, timeout, crash), the next run detects the incomplete state and prepends a resume note to the ticket file with:

- When and which agent last attempted the ticket
- A pointer to the prior run's log file
- Instructions to read the log, assess progress, and resume rather than restart

The agent sees this note as part of the ticket content and can read the log to understand what was already accomplished. The resume note is removed by the agent when it begins working.

The resume note is committed to the ticket file itself, so it carries across runs regardless of strategy. Under `batch` and `chase`, when the resumed ticket is present in the new run's snapshot it is also hoisted to the front of the queue so it runs first — even if it sits in a later stage than other queued tickets (in `chase`, it becomes the first root and is chased forward from its current stage). Under `live` there is no frozen queue to hoist within; the ticket is re-discovered with its note in place and selected by normal cross-stage priority.

If the incomplete ticket is no longer present (e.g., it was manually moved), the runner simply clears the stale state and proceeds normally.

### Idle-timeout retries

If the agent goes idle for too long (10 minutes with no output), the runner kills it and retries the same ticket once with a resume note pointing at the prior run's log. If the retry also times out, the runner commits a resume note to the ticket and moves on to the next one rather than aborting the whole batch — so an unattended run can finish the rest of the queue and you can pick up the timed-out ticket on the next invocation.

## Pre-existing Test Failure Triage

If the agent working a ticket runs tests and one fails in a way that is plainly unrelated to its own changes — broken at HEAD before its edits, in code it never touched — it writes `tickets/.pre-existing-error.md` summarising what it ran and what failed, then finishes its own ticket normally. The workflow rule for this lives in `agent-rules/tickets.md` (§ *Pre-existing test failures*).

After each ticket commits (and again once the run is wrapping up), the runner checks for that file. If present, it dispatches a triage agent — same adapter, focused prompt — instructed to either:

- reproduce the failure, fix the root cause, and let the runner commit; or
- file a `tickets/backlog/` ticket capturing the failing test and the evidence so a human can decide.

The runner deletes the report afterwards and commits any resulting changes as `tess: triage pre-existing test failure`. Triage respects `--token-budget` and `--no-commit`. The `.pre-existing-error.md` file is gitignored.

## Design Philosophy

- **Snapshot-based** — Ticket list captured once per run; newly created tickets wait for the next run
- **Agent-owned transitions** — The agent creates and deletes ticket files; the runner handles commits
- **Commit per ticket** — Clean git history for human review between runs
- **Sequence-driven** — Tickets processed lowest-sequence-first within each stage (optional prefix; unnumbered tickets trail numbered ones)
- **Prereq-aware** — `prereq:` edges topologically sort tickets within a stage and gate them across stages by pipeline rank; conflicts with explicit sequence numbers fail fast
- **Non-interactive** — Batch processing with human review between runs

## Ticket Format Migration

`tickets/.version` records the ticket format. Legacy format v1 used numeric prefixes to encode *priority* (higher = sooner) and a `dependencies:` header; the current format v2 uses *sequence* (lower = sooner) with a `prereq:` header and slug-only references.

The runner auto-migrates on first invocation against a v1 project: it inverts numbering (preserving execution order), renames `dependencies:` to `prereq:`, strips sequence prefixes from inter-ticket references, and commits the migration as its own commit. The migration is source-controlled — inspect the diff and revert if needed.

To run the migration explicitly (with a dry-run preview):

```bash
node tess/scripts/migrate.mjs --dry-run
node tess/scripts/migrate.mjs
```

## Web Dashboard

Tess includes a web dashboard for browsing the ticket pipeline, viewing tickets by stage, and reading ticket details.

### Running the Dashboard

```bash
cd tess/ui
npm install
npm run dev
```

The dashboard starts on `http://localhost:3004` by default.

### Cross-Linking

If a sibling system is detected (e.g., `teamos/` exists at the project root), the dashboard shows a link in the navigation bar. Both teamos and tess auto-detect each other and display reciprocal links. Override the project root with the `TESS_PROJECT_ROOT` environment variable:

```bash
TESS_PROJECT_ROOT=/path/to/project npm run dev
```

## Further Reading

- [docs/](docs/) — Design principles, installation architecture, and development status
