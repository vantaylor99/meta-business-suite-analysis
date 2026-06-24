# Design Principles

## Core Philosophy

Tess is a thin orchestration layer. It imposes just enough structure (stages, priorities, a file format) to give AI agents a reliable pipeline, and nothing more. The ticket files themselves are the source of truth — no database, no API, no state beyond the filesystem.

### 1. Filesystem is the database

Tickets are markdown files in stage folders. Moving a ticket to the next stage means creating a new file in the next folder and deleting the old one. Git provides the audit trail. This makes the system inspectable with `ls`, debuggable with `cat`, and portable across any project.

### 2. Agents own transitions

The runner doesn't move tickets — agents do. An agent reads a ticket, does the work, creates the next-stage file(s), and deletes the source. The runner handles the git commit after the agent completes. This keeps commits out of interactive agent sessions while ensuring clean commit-per-ticket history when running the pipeline. Agents still have freedom to split tickets, adjust priorities, or redirect to `blocked/` when questions arise.

### 3. Selection is a pluggable policy

How much a single invocation does is a strategy choice, not a fixed law. The default `live` strategy re-discovers the board after every transition and works the current highest-priority ticket, so the follow-up work a stage generates (a review that files a fix, a plan that splits) is picked up and drained in the same run. The `batch` and `chase` strategies instead snapshot the ticket list at startup — tickets created during the run aren't picked up until the next invocation, which caps a run to one stage per ticket (batch) or one root chased to done (chase) and gives humans a clean review point between runs. Pick `batch`/`chase` when you want that frozen-per-run boundary; pick `live` (default) to clear the pipeline, including its own follow-ups, in one pass.

### 4. Human-in-the-loop between runs

Tess is non-interactive. Run it, review the commits, then run again. The human decides when to advance, can revert agent work, adjust priorities, or rewrite tickets between runs.

### 5. Minimal coupling to host project

Tess integrates into any project without imposing structure beyond a `tickets/` directory. It doesn't care about the host's language, framework, or build system.

### 6. Single source of truth

Tess lives in one repo. All consuming projects reference the same agent rules and runner script. Bug fixes and improvements propagate to every project.

---

## Integration Model

Tess integrates into a host project via git submodule (standard) or symlink (alternative). In both cases, `tess/` appears at the project root and an init script creates the `tickets/` scaffold. See [INSTALLATION.md](INSTALLATION.md) for the full design.

Tess provides three things to the host project:

1. **The `tickets/` directory scaffold** — stage subdirectories, `.gitignore` for logs
2. **Agent rules** — canonical rules in `tess/agent-rules/tickets.md`, surfaced to agents via stub files or symlinks in `tickets/`
3. **The runner script** — `node tess/scripts/run.mjs`, invoked from the project root

---

## Stage Definitions

The pipeline stages, adapted from the original optimystic system:

| Stage | Purpose | Output |
|---|---|---|
| `backlog` | Parked specs not ready to work yet | `plan/` (when promoted) |
| `fix` | Bug triage: reproduce, research, hypothesize | `implement/` ticket(s) |
| `plan` | Feature design: research, resolve questions | `implement/` ticket(s) |
| `implement` | Build, test, validate | `review/` ticket |
| `review` | Code quality, test coverage, docs | `complete/` ticket |
| `complete` | Archived summary | — |
| `blocked` | Parked — unresolved questions | Returns to any stage |

`backlog` is excluded from the runner's default stage set. Include it explicitly via `--stages backlog:<max>` to promote tickets when ready to work them.

---

## Traversal Strategies

The runner is split into two layers: a fixed pipeline (discover → topo-sort → agent invocation → commit) and a pluggable **strategy** that decides which ticket runs next. Strategies live in `tess/scripts/lib/strategies/` and share the same per-ticket runner (`lib/run-ticket.mjs`), so they cannot diverge on idle-timeout retries, in-progress state, or commit cadence — only on selection. `batch` and `chase` select from a snapshot frozen at startup; `live` re-runs discovery every iteration.

### `live` — continuously reassessed (default)

After every stage transition, live re-discovers the entire board from disk and re-applies the same priority policy `batch` uses — `--stages` order across stages, `prereq:` topo then sequence within a stage — picking the current highest-priority runnable ticket. Because it reads disk each iteration, tickets created mid-run compete for "what's next" immediately: a `review` that files a `fix` sees that fix (highest priority) resolved next; a `plan` that splits into `implement` tickets sees them ranked in at once. A ticket whose prereq is *behind but still advancing* is skipped only for the current pass and becomes selectable the instant its prereq moves forward, so a whole prereq chain can drain in one run. Errored/timed-out slugs are excluded for the rest of the run (resumed next run via their note); a per-slug transition cap and a global run cap backstop regress/respawn loops. Best for unattended runs that should clear the pipeline — including its own generated follow-ups — always working the most important thing next.

### `batch` — stage-major

The original behavior: drain every selected stage in `--stages` order, advancing each ticket by exactly one stage. Best for steady throughput and a clean review boundary per run; each run produces a stage-of-progress diff. The snapshot is captured once at startup and topo-sorted by `prereq:` within each stage; lower sequences come first.

### `chase` — ticket-major

Pick one root ticket and follow it through `plan → implement → review → complete` (or `fix → implement → review → complete`) in a single run, then move to the next root. Best for focused work on a single feature, or for keeping the in-flight set small.

**Successor lookup is by slug, not by filesystem diff.** After each stage transition, chase looks for the same slug in `NEXT_STAGE`, then in `blocked/` and `backlog/`. The diff approach was rejected because tess is intentionally tolerant of other agents (humans, sibling pipelines, parallel runners) modifying `tickets/` concurrently; attributing every new file to the agent we just ran would be wrong.

**Block / backlog cascade.** When a chain ends because the agent landed the slug in `blocked/` or `backlog/`, that slug is added to a per-run `deferred` set. Subsequent root tickets that list a deferred slug as `prereq:` are skipped — and the skipped root is itself added to `deferred`, so the cascade is transitive. This is the chase-equivalent of "don't bother with the work whose prerequisite just bounced."

**Splits.** Agents may split one ticket into multiple next-stage tickets. Chase follows the same-slug branch and leaves the siblings in place; they become roots in a future run. Trying to follow all splits in one chase would conflate "follow this idea" with "drain this layer," reintroducing the stage-major behavior chase was designed to avoid.

**Safety cap.** A single chain is bounded to 6 stage transitions to guard against regressive loops (e.g. an agent moving `implement` → `plan`). The natural pipeline tops out at 4–5 transitions.

### Why explicit strategies, not a knob

A single parameterized loop (`depth` = 1 means batch, `depth` = ∞ means chase) was considered and rejected. The modes differ in *intent* — "reassess and always work the top priority" (live) vs "advance everything once" (batch) vs "finish this one thing" (chase) — and in error recovery (chase needs the deferred-cascade rule; live needs the exclude-and-reassess rule; batch needs neither). They also differ in whether selection reads a frozen snapshot (batch/chase) or live disk state (live). A shared loop with mode flags would entangle these and make the subtlest behavior the default of every read; explicit strategies keep each contract obvious at the call site.

---

## Open Questions

### Q1: Should any `tickets/` subfolders be renamed for agile/kanban compatibility?

**Context:** The current stage names (`fix`, `plan`, `implement`, `review`, `complete`, `blocked`) are verb/action-oriented and describe *what happens* at each stage. Standard agile/kanban boards typically use state-oriented names like `backlog`, `todo`, `in-progress`, `done`.

**Current names — pros:**
- Descriptive of the work being done, not just the state
- Distinguish between bug work (`fix`) and feature work (`plan`), which have different workflows
- `implement` is clearer than `in-progress` about what the agent should do
- Already battle-tested across multiple projects

**Agile-standard names — pros:**
- Familiar to anyone coming from Jira, Trello, Linear, etc.
- Easier to map to existing team workflows
- `backlog` is a well-understood concept

**Possible hybrid:**
- Keep `fix` and `plan` as entry points (they represent different intake workflows)
- Rename `implement` → `build` or keep as-is?
- Keep `review`, `complete`, `blocked` (these already align with common usage)

**Recommendation:** Keep the current names. They are *imperative* — they tell the agent what to do, not just where the ticket sits. Agile/kanban names (`backlog`, `in-progress`, `done`) are designed for human boards tracking state; tess stages are designed for agents performing work. `implement` is a better instruction than `in-progress`. The only candidate for renaming would be `complete` → `done` for brevity, but `complete` already aligns with common usage and reads well in git logs (`task(complete): ...`). No rename needed.

**Decision:** Resolved — keep current naming (`fix`, `plan`, `implement`, `review`, `complete`, `blocked`).

---

### Q2: What integration method should tess use?

Moved to [INSTALLATION.md](INSTALLATION.md) — the design supports two methods:

- **Git submodule (standard)** — tess is a submodule at `project_root/tess/`. The init script creates real stub files in `tickets/` that reference `tess/agent-rules/tickets.md`. No symlinks needed — fully cross-platform.
- **Symlink (alternative)** — tess is cloned externally, a symlink at `project_root/tess` points to it. The init script symlinks `tickets/AGENTS.md` and `tickets/CLAUDE.md` to the canonical rules file.

A single init script (`node tess/scripts/init.mjs`) auto-detects which method is in use and does the right thing. Git subtree was evaluated and rejected — it copies tess into the project repo, violating the requirement that tess not be bundled.

See [INSTALLATION.md](INSTALLATION.md) for the full design, detection logic, and comparison.

**Decision:** Resolved — adopt dual installation mode (git submodule standard, symlink alternative). See [INSTALLATION.md](INSTALLATION.md).

---

### Q3: Should `fix` and `plan` (feature) be combined into a single intake stage?

**Context:** Both `fix/` and `plan/` serve as entry points that output `implement/` tickets. The question is whether they should be a single stage (e.g., `triage/` or `backlog/`) with a tag or metadata field distinguishing bugs from features.

**Keep separate — pros:**
- Different workflows: `fix` starts with reproduction, `plan` starts with design
- Agent instructions can be tailored per entry type
- Clear at a glance what kind of work is in the pipeline
- Priority can mean different things (P5 bug vs P5 feature)

**Combine — pros:**
- Simpler folder structure
- One fewer stage to reason about
- Some tickets blur the line (is "improve error handling" a fix or a feature?)
- Metadata field (`type: fix | feature`) could handle the distinction

**Consideration:** The agent rules in `AGENTS.md` currently give different instructions for fix vs plan stages. If combined, those instructions would need to branch on a metadata field, adding complexity to the agent prompt.

**Test-first parallel:** Both stages share a test-first pattern. `fix` starts by writing a regression test that exposes the bug — a failing test that proves the problem exists. `plan` could mirror this: start by writing a test against the proposed feature API that expresses the desired behavior before designing the implementation. This is TDD-flavored intake — the test *is* the spec. This parallel strengthens both stages (plan tickets with a concrete test are more actionable) but also highlights that the *intent* differs: fix tests prove what's broken, plan tests express what's desired. The agent instructions would read differently even if the mechanical pattern (write a test first) is the same.

**Recommendation:** Keep separate. The test-first parallel makes the stages more alike in *pattern* but their intent, agent instructions, and output character remain distinct. Combining them would require the agent to branch on a metadata field mid-prompt, adding complexity without reducing folder count meaningfully (you'd still have `implement/`, `review/`, `complete/`, `blocked/`). The fix/plan split is also immediately legible in `ls` — you can see at a glance whether the pipeline is bug-heavy or feature-heavy. Consider formalizing the test-first expectation for `plan` as well: "where feasible, begin with a test expressing the desired API/behavior."

**Decision:** Resolved — keep `fix/` and `plan/` as separate intake stages.

---

### Q4: Should the runner script live inside `tickets/` or be invoked from tess directly?

**Context:** In the current optimystic system, `run-tasks.mjs` lives inside `tasks/` and resolves paths relative to itself. Options for tess:

- **A. Copy/symlink the runner into `tickets/`** — Host project runs `node tickets/run.mjs`. Self-contained, familiar.
- **B. Run from tess directly** — Host project runs `node tess/scripts/run.mjs` (or a wrapper). Avoids duplication, always up to date.
- **C. npm/npx approach** — Publish tess to npm, run via `npx tess` or add as a dev dependency. Most "standard" but adds registry overhead.

**Recommendation:** Option B. The runner is tess's code, not the project's. In both installation methods (submodule and symlink), tess appears at `project_root/tess/`, so `node tess/scripts/run.mjs` works uniformly. The runner resolves `tickets/` relative to cwd (the project root) and reads rules from its own `agent-rules/` directory via `import.meta.url`. No copies, no symlinks of the runner itself, always up to date.

**Decision:** Resolved — runner lives at `tess/scripts/run.mjs`. The user can create their own symlink or shell wrapper in the project root if they want a shorter invocation (e.g., `./run` → `node tess/scripts/run.mjs`), but tess doesn't manage that.

---

### Q5: Should `AGENTS.md` be the only agent-rules file, or should there be per-stage rules?

**Context:** Currently a single `AGENTS.md` covers all stages. As instructions grow, a single file could become unwieldy. Alternative: per-stage rule files (`fix.md`, `plan.md`, etc.) that the runner composes into the prompt.

**Single file — pros:**
- Simple, one place to look
- Agents see the full pipeline context
- Easier to maintain

**Per-stage files — pros:**
- Focused instructions per stage
- Smaller prompt size (only relevant rules)
- Easier to evolve stages independently

**Recommendation:** Single file. The current `AGENTS.md` is ~30 lines — compact enough that splitting it would create more files to manage than lines saved. The runner already injects stage-specific context into the prompt (stage name, next stage), so the agent knows which section applies. If instructions grow substantially in the future, splitting is a non-breaking change (the runner can concatenate a base file with a per-stage file). Premature splitting adds indirection now for a problem that doesn't exist yet.

**Decision:** Resolved — single `AGENTS.md` for now. Tess will house the source file(s) in an `agent-rules/` directory (e.g., `tess/agent-rules/tickets.md`), which is what gets symlinked into the project as `tickets/AGENTS.md`. This keeps the architecture open to multiple rule files later (the runner could concatenate `agent-rules/base.md` + `agent-rules/fix.md` for a fix-stage ticket) without changing the consuming project's interface.

---

### Q6: What should the `tickets/.gitignore` contain?

**Context:** Logs (`.logs/`) should be ignored. Should anything else be? Prompt temp files are already cleaned up by the runner. Should `complete/` tickets be ignored or committed?

**Likely answer:** Only `.logs/` — completed tickets serve as project history and should be committed.

**Recommendation:** `.logs/` only. Completed tickets are lightweight and serve as a useful project history in version control. The runner already cleans up temp prompt files. No other generated artifacts need ignoring.

**Decision:** Resolved — `.logs/` only, matching current optimystic convention (`tasks/.logs/` is the sole tickets-related entry in optimystic's `.gitignore`).

---

### Q7: Should tess support project-local overrides of agent rules?

**Context:** Different projects may want to customize agent behavior (e.g., "always run `pnpm test` not `npm test`", or project-specific review criteria). Options:

- **A. No overrides** — Keep it simple; projects edit their own `AGENTS.md` copy
- **B. Merge/overlay** — Tess rules + a project-local `tickets/LOCAL_RULES.md` that gets appended
- **C. Template variables** — Agent rules use placeholders that init fills in

**Current behavior:** The runner explicitly reads `AGENTS.md` and injects it into the prompt — it does *not* rely on the agent discovering `AGENTS.md` by convention. In `buildPrompt()`, the runner reads `tasks/AGENTS.md`, reads the ticket file, and concatenates them into a single instruction file that gets passed to the agent via `--append-system-prompt-file` (Claude) or written to a temp file (Cursor/Augment). So the agent always sees the rules regardless of what convention filenames it respects.

This means `LOCAL_RULES.md` support is straightforward: the runner can read `tickets/LOCAL_RULES.md` (if it exists) and append it after the base rules in the same prompt. No changes to AGENTS.md itself are needed — the runner handles composition. There's no need for an "include" directive inside AGENTS.md.

**Recommendation:** Option B. The runner can trivially check for a `tickets/LOCAL_RULES.md` and append its contents to the prompt after the base rules. This is zero-config (if the file doesn't exist, nothing changes), requires no template parsing, and lets projects add things like "use `pnpm` not `npm`" or project-specific review criteria without forking tess's rules. The base `AGENTS.md` stays a symlink to tess (always up to date); local customization lives in a file the project owns.

**Decision:** Resolved — no built-in override mechanism for now. Users who need project-specific customization can handle it themselves (e.g., edit their own root AGENTS.md, add project-level agent rules, or create a wrapper script). If this becomes a friction point across projects, we'll revisit and add `LOCAL_RULES.md` support to the runner.

---

### Q8: Should priority be encoded in the filename, or is there a better method?

**Context:** The current convention uses a numeric prefix: `3-my-feature.md`. The runner parses this with a simple regex (`/^(\d+)-/`) to sort and filter tickets. Alternatives include frontmatter metadata, a separate manifest, or directory-based priority.

**Filename prefix (current) — pros:**
- Visible in `ls` and file explorers without opening the file
- Sortable at the filesystem level (`ls` groups by priority naturally)
- Trivial to parse — no YAML/frontmatter parser needed
- Easy to change — just rename the file
- Works with any tool (grep, find, shell globs like `5-*.md`)

**Filename prefix — cons:**
- Mixes metadata with the name — renaming to change priority can be confusing in git diffs
- Limited to a single numeric dimension (can't express urgency vs importance)
- Non-standard — most ticketing systems use fields, not filenames

**Frontmatter metadata — pros:**
- Richer: could encode priority, type, tags, assignee, etc.
- Filename stays purely descriptive
- Standard pattern in static-site generators, widely understood

**Frontmatter metadata — cons:**
- Requires parsing file contents just to sort/filter — slower for discovery
- Not visible without opening the file
- Adds parsing complexity to the runner
- The current metadata header (`description:`, `dependencies:`, `files:`) isn't YAML frontmatter (no `---` delimiters), so this would be a format change

**Other options:**
- **Directory-based** (`tickets/plan/p3/my-feature.md`) — adds nesting depth for little benefit
- **Manifest file** (`tickets/manifest.json`) — single point of failure, sync issues, defeats "filesystem is the database"

**Recommendation:** Keep the filename prefix. It's the right trade-off for a filesystem-based system: zero-cost discovery (no file reads needed to sort), visible at a glance, trivial to parse, easy to change. The con of "metadata in filenames" is real in systems with many metadata dimensions, but tess has exactly one: execution order. One dimension fits cleanly in a prefix. If richer metadata is ever needed, the in-file header (`description:`, `prereq:`, `files:`) can be extended without touching the filename convention.

**Decision:** Resolved — keep the numeric-in-filename convention (`3-my-feature.md`). See Q12 for the *semantics* of that number (priority → sequence) and Q13 for cross-ticket references.

---

### Q9: Do we need a `CLAUDE.md` symlink in addition to `AGENTS.md`?

**Context:** Different AI agents discover project-level instructions via different convention files:
- **Cursor** reads `AGENTS.md` (also `.cursor/rules/`)
- **Augment** reads `AGENTS.md`
- **Claude Code** reads `CLAUDE.md` — it does *not* read `AGENTS.md`

This matters in two scenarios:
1. **Runner-driven execution** — The runner reads `AGENTS.md` itself and injects its content into the agent prompt via `--append-system-prompt-file` or an instruction file. In this case, the convention filename doesn't matter because the runner handles delivery explicitly.
2. **Ad-hoc agent usage** — A developer opens the project and runs Claude Code manually (not through the runner). Claude won't discover `tickets/AGENTS.md` on its own; it would only discover `tickets/CLAUDE.md` or `CLAUDE.md` at the project root.

**Options:**
- **A. Tess creates both symlinks** — `tickets/AGENTS.md` and `tickets/CLAUDE.md` both point to the same rules file. Covers both agents' discovery conventions automatically.
- **B. User creates CLAUDE.md themselves** — Keep tess simple; users who use Claude Code add their own `CLAUDE.md` referencing the ticket rules.
- **C. Single canonical file, runner handles it** — Don't rely on convention-based discovery at all. The runner always injects rules explicitly. For ad-hoc use, the user is expected to know the rules exist.

**Recommendation:** Option A. It's trivial — one extra symlink in the init script. There's no cost, and it removes a gotcha for Claude Code users. The init script already creates symlinks; adding `CLAUDE.md → same target` is one line. Both symlinks can be gitignored alongside the others. For the project root, init also creates/appends a tess section to both `AGENTS.md` and `CLAUDE.md` (see [INSTALLATION.md](INSTALLATION.md)).

**Decision:** Resolved — create both `AGENTS.md` and `CLAUDE.md` symlinks in `tickets/`. The init script should define the list of convention filenames at the top of the file (e.g., `AGENT_RULE_NAMES = ['AGENTS.md', 'CLAUDE.md']`) so that adding future variants (if another agent tool introduces its own convention) is a single-line change rather than scattered logic.

---

### Q10: Is Node.js the right language for the runner, or would bash be better?

**Context:** The runner (`run-tasks.mjs` / `run.mjs`) is currently ~550 lines of Node.js. It handles task discovery, CLI arg parsing, process spawning, JSON stream parsing, log file management, and idle timeout detection.

**Node.js — pros:**
- JSON stream parsing is native and robust (the Claude and Cursor adapters parse newline-delimited JSON)
- Async I/O, process management, and timeout handling are well-supported
- Cross-platform (Windows compatibility if ever needed)
- The complexity is already managed — the script exists and works
- No external dependencies (uses only `node:fs`, `node:path`, `node:child_process`)

**Node.js — cons:**
- Requires Node.js installed (though this is near-universal for developers)
- ~550 lines is substantial for a "script"

**Bash — pros:**
- No runtime dependency beyond the shell
- File operations (`ls`, `mv`, `rm`) are native
- Simpler for pure file-shuffling tasks

**Bash — cons:**
- JSON stream parsing in bash is fragile (requires `jq`, regex hacks, or ignoring structured output)
- Process management with timeouts, idle detection, and tee-to-logfile is painful and error-prone
- Error handling is weak (set -e has well-known gotchas)
- The agent adapters format structured JSON streams — this is fundamentally not a bash-shaped problem
- Would likely need to shell out to `node` or `python` anyway for the JSON parsing

**Recommendation:** Stay with Node.js. The runner's core complexity — parsing agent JSON streams, managing child processes with idle timeouts, structured logging — is exactly what Node was built for and exactly what bash struggles with. The zero-dependency approach (only `node:` built-ins) keeps it lean. Node is a safe assumption for any developer using AI coding agents (the agent CLIs themselves often depend on it). Bash would be appropriate if the runner were just "move files between folders," but it's an orchestrator with real I/O concerns.

**Decision:** Resolved — keep Node.js implementation. Also implies the init script should be Node.js (not bash) for Windows compatibility.

---

### Q11: Do we need a detach/uninstall script?

**Context:** Appeus-2 has a `detach-appeus.sh` script that removes all symlinks. Tess creates artifacts in the host project — what does removal look like?

**What tess creates in the host project:**
- `tickets/` scaffold (directories + `.gitignore`)
- `tickets/AGENTS.md` and `tickets/CLAUDE.md` (stubs or symlinks)
- A `<!-- tess -->` section appended to root `AGENTS.md` and `CLAUDE.md`
- In symlink mode: a `tess` symlink and `.gitignore` entries

**What removal involves per method:**
- **Submodule:** `git submodule deinit -f tess && git rm tess && rm -rf .git/modules/tess` removes the submodule. The `tickets/` directory, stub files, and root AGENTS.md section remain — harmless but orphaned.
- **Symlink:** Remove `tess` symlink, `tickets/AGENTS.md` and `tickets/CLAUDE.md` symlinks, and `.gitignore` entries.

**Both methods:** Optionally remove the `<!-- tess -->` section from root `AGENTS.md`/`CLAUDE.md`. Optionally remove `tickets/` entirely (but it may contain tickets the user wants to keep).

**Recommendation:** Yes, provide a `scripts/detach.mjs` but keep it minimal. It should:
1. Remove tess-created files in `tickets/` (`AGENTS.md`, `CLAUDE.md`) — but only after verifying they're tess-created (check for `<!-- Generated by tess init -->` marker in stubs, or confirm they're symlinks pointing into tess). Skip with a warning if the file appears user-modified.
2. Remove the `<!-- tess -->` section from root convention files (using the marker for precise extraction)
3. In symlink mode: remove the `tess` symlink and clean tess-related `.gitignore` entries
4. In submodule mode: print the `git submodule` removal commands (don't run them — let the user control destructive git operations)
5. Never delete `tickets/` or its contents — that's the user's data

**Decision:** Resolved — implement `scripts/detach.mjs`.

---

### Q12: What does the numeric prefix mean — priority or sequence?

**Context:** The filename prefix (`3-my-feature.md`) originally encoded *priority* — higher = sooner — sorted descending. Over time this revealed two problems. First, priority naturally trends toward creeping inflation: when everything is P5, a new P6 slot gets invented. Second, humans reason about execution order as a queue, not a heap; "what comes next?" is a more natural question than "what is most important?".

**Options:**
- **A. Priority (descending)** — original semantics; higher number wins
- **B. Sequence (ascending)** — lower number wins; number answers "when does this run?"
- **C. Drop the number** — use only topological edges (`prereq:`) for ordering

**Recommendation:** Option B. Sequence scales better (append-only numbering, no inflation), reads as a to-do queue, composes cleanly with `prereq:` topo edges (prereq must have sequence ≤ dependent), and allows an optional prefix for tickets that don't care about placement. Option C alone was considered but rejected: prereqs capture *relative* order only; humans still want an absolute slot to answer "what's next."

**Decision:** Resolved — sequence semantics. Lower number runs sooner. The prefix is optional (unnumbered tickets follow numbered ones within a stage). `--max-sequence <n>` bounds the batch from the earliest-slot end. `tickets/.version` stamps the format so legacy v1 projects can be auto-migrated by inverting the numbering (`new = max + min - old`, preserving execution order) and rewriting headers (`dependencies:` → `prereq:`).

---

### Q13: How should cross-ticket references work, and do we need a `backlog/` stage?

**Context:** Tickets frequently reference each other ("this depends on that landing first"). The v1 header was `dependencies:` and referenced tickets by full filename including the priority prefix (`3-some-ticket.md`). This conflated three concerns:
1. The *name* of the ticket (what is it?)
2. Its *scheduling slot* (the numeric prefix, which can change)
3. Its *type of reference* (prereq? inspiration? external link?)

Separately, the pipeline had no good place to park speculative specs. `blocked/` is for "this has an unresolved question" — not "we'll get to this eventually." Ad-hoc tickets piled up in `plan/` with `priority: 1` as a back-door backlog.

**Recommendation:**
- **`prereq:` header** — rename `dependencies:` to `prereq:` to make the semantic clear: these tickets must *land first*. External libraries and unrelated modules don't belong here.
- **Slug-only references** — drop the numeric prefix when referencing other tickets. The sequence can change; the slug is the stable identity. `prereq: collection-api` not `prereq: 6-collection-api.md`.
- **Topological ordering** — the runner builds a DAG from `prereq:` edges within each stage, topo-sorts, and errors on cycles or on explicit sequence numbers that contradict a prereq edge. Agents and humans can both trust that `prereq:` enforces order.
- **`backlog/` stage** — add a parking-lot stage for specs that aren't ready. Not in the runner's default stage set; promote with `--stages backlog:<max>` when ready to work them. Agents may create backlog tickets when splitting work, so the pipeline has a "later" bucket that isn't a workflow dead-end like `blocked/`.

**Decision:** Resolved — `prereq:` replaces `dependencies:`; references use slug only; `backlog/` is a new stage (parked by default). All three changes ship together as format v2 (see Q12 for the version-file mechanism).
