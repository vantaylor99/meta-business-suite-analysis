Tickets flow forward through stages:

```
  backlog/ ─→ plan/ ─┐
                     ├─→ implement/ ──→ review/ ──→ complete/
              fix/ ──┘
                     ↕
                 blocked/
```

Each stage's job is to advance the ticket to the next stage. Tickets only move sideways into `blocked/` (and back out once unblocked); they never flow backward. In particular, `review/` is **after** `implement/` — a review ticket exists because code has already been written and now needs a code-review pass.

**Cross-stage gating is automatic.** If a `prereq:` slug sits anywhere earlier in the pipeline (including `blocked/` or `backlog/`), the runner defers the dependent this run and re-picks it once the chain clears. The runner also cascades: errored, deferred, or blocked-prereq slugs transitively defer their downstream. You never mirror this by hand. `prereq:` is a hint to the runner, not an instruction to you.

The tickets/ folder at the project root contains `backlog`, `fix`, `plan`, `implement`, `review`, `blocked`, and `complete` subfolders.  Each ticket is a markdown file inside one of these folders.

Filename convention: `<slug>.md`, optionally prefixed with a numeric **sequence** (integer or decimal) — `3-my-ticket.md` or `3.5-my-ticket.md`.  **Lower sequence runs sooner.**  The prefix is optional; unnumbered tickets (`my-ticket.md`) follow after all numbered ones in the same stage.  The sequence number is not part of the ticket's identity — when referencing another ticket, use only its slug (`my-ticket`), not the full filename.

You own the full stage transition.  When you are done:
  1. Create the next-stage output file(s) in the appropriate tickets/ subfolder.
     You may split one ticket into multiple next-stage tickets if warranted —
     give each a distinct slug and chain them with `prereq:` so the runner enforces topo order.
     Don't combine unrelated tickets. You may keep, add, or adjust the sequence prefix.
     Respect `prereq:` relationships: a prereq must have a sequence ≤ its dependent (or be
     unnumbered only if the dependent is also unnumbered) — the runner fails fast on conflicts.
  2. Delete the original source ticket file from its current stage folder.
     Delete only the file — leave the stage folder itself in place even when it
     ends up empty.

**Never sanitize the working tree.** Don't run `git checkout -- `, `git restore`, `git reset`, `git clean`, or `git stash`, and don't otherwise revert or discard changes you didn't make. The runner may be processing other tickets and a human may be promoting tickets concurrently — uncommitted board moves and in-flight edits in the tree are not yours to undo. Touch only the files your own ticket requires.

**`prereq:` is a hint, not an instruction to park.** Assume every `prereq:` ticket's work will land; design as if it has. The only reasons to deviate are the two `blocked/` categories below — neither is "an upstream tess ticket isn't done yet." Otherwise pick the best option, document the tradeoff in the next-stage ticket, and proceed.

Stages (overview — full rules for your active stage appear under "Active stage details" below):
- **backlog** — specs not yet ready to work; the human (or `--stages backlog:N`) promotes into plan/.
- **fix** — reproduce + research a bug; output implement/ ticket(s).
- **plan** — design a feature; output plan/ or implement/ ticket(s); park out-of-scope work in backlog/.
- **implement** — build it; ensure build + tests pass; output a review/ handoff that is honest about gaps (the reviewer treats your work as a starting point, not a finish line).
- **review** — adversarial pass over implement output: minor findings → fix inline; major → spawn new fix/plan/backlog ticket(s). Output complete/ with a `## Review findings` section.
- **blocked** — last-resort park: missing-prereq-code (implement only) or design question needing human sign-off.
- **complete** — archived summary of finished work, including review findings.

## Active stage details

<!-- stage:backlog -->
**Backlog** — specification tickets (like *plan*) that aren't ready to be worked yet.  Use this when splitting or scoping work: items the team will get to eventually but shouldn't enter the active pipeline.  Prefer `backlog/` over `blocked/` when the reason is "not now" rather than "unresolved question."  Not in the runner's default processing set — the human (or an explicit `--stages backlog:<max>` invocation) promotes these into `plan/` when ready.
<!-- /stage -->

<!-- stage:fix -->
**Fix** — for bugs.  Start with a reproducing test case, or a trace modality if the issue is intermittent.  Once reproduced and researched, form one or more hypotheses as to the cause and correction.  Output is one or more ticket file(s) in *implement/* (or blocked/backlog).  References should be made to key files and documentation.  TODO tasks should be at the bottom of the ticket file(s).  Split into multiple tickets if warranted.
<!-- /stage -->

<!-- stage:plan -->
**Plan** — specs for features and enhancements (not already designed/planned).  After research, output is one or more plan and implement/ tickets.  When you discover adjacent work that is out of scope for the current pass, park it in `backlog/` rather than growing the current ticket.  References should be made to key files and documentation.  TODO tasks should be at the bottom of the ticket file(s).  Don't switch to your agent's "planning mode" when working these tickets - that's too meta.  In the spirit of TDD, your plan may include bullets describing key tests that might come in later phases, and what the expected outputs should be.

**Resolve the design before you emit an implement ticket.**  Only hand off to `implement/` once no major question or open option remains: settle it with more research, or pick the best option and document the tradeoff in the ticket.  If a genuine question of consequence has no defensible default, route to `blocked/` for human sign-off — never emit an under-specified implement ticket and leave the call to the implementer.

**Enumerate the adversarial surface.**  Every implement ticket you produce should carry an `## Edge cases & interactions` section naming the boundary states, concurrent/forked access, partial-failure paths, and cross-subsystem interactions the implementer must cover and the reviewer will check.  A case you name here is a test written up front; a case you omit tends to return as a separate fix ticket.

**Size each ticket to one agent run.**  Split so each implement ticket is a single coherent change an agent can finish well inside the runner's idle-timeout window.  If a ticket would span several subsystems or carry multiple independent failure modes, break it into `prereq:`-chained tickets rather than one oversized ticket.
<!-- /stage -->

<!-- stage:implement -->
**Implement** — these tickets are ready for implementation (fix, build, update, ...whatever the ticket specifies).  If more than one agent would be useful, without stepping on toes, spawn sub-agents.  Be sure the build and tests pass when done.  Output is a distilled summary of the ticket, with emphasis on use cases for testing, validation and usage into the review/ folder.  Write the handoff honestly — the reviewer is instructed to treat your work as a starting point and your tests as a floor, so flag known gaps rather than papering over them.
<!-- /stage -->

<!-- stage:review -->
**Review** — adversarial pass over the completed implementation. The ticket will read as finished — find what it overlooked. **Read the implement-stage diff first**, with fresh eyes, before considering the handoff summary (find it via `git log --grep="ticket(implement): <slug>" -1 --format=%H` then `git show <hash>`). Scrutinize from every aspect angle (SPP, DRY, modular, scalable, maintainable, performant, resource cleanup, error handling, type safety). The implementer's tests are a *starting point* — cover happy path, edge cases, error paths, regressions, and interactions. Treat docs as out-of-date until you read every file the change touches — and the ones it *should* have touched — and confirm they reflect the new reality. Run lint + tests; they must pass. Disposition of findings: **minor** — fix in this pass; **major** — file new ticket(s). The output `complete/` ticket must include a `## Review findings` section listing what was checked, what was found, and what was done. Empty categories are fine — but say so *explicitly and with a reason*, not silently or "Looks good".
<!-- /stage -->

<!-- stage:blocked -->
**Blocked** — exclusively for what the runner cannot resolve on its own:
  (a) a design question of consequence requiring human sign-off, with no defensible default, OR
  (b) (implement stage only) a code prereq that is **not** a tess ticket and is not present (external upstream, stub primitive, premise mismatch).

Lead the file with one line stating which category and the exact thing that unblocks it.

Not blocked: a `prereq:` ticket still in plan/fix/implement (deferred automatically), a `prereq:` ticket already in `blocked/` (transitively deferred — leave yours where it is), uncertainty more research would resolve (do the research), or "we'll get to it later" (that's `backlog/`).
<!-- /stage -->

<!-- stage:complete -->
**Complete** — archived summary of finished work.  Contains briefly what was built, key files, testing notes, and usage information.
<!-- /stage -->

If the ticket contains a `<!-- resume-note -->` block, a prior agent run was interrupted before completion.  Read the referenced log file to understand what was already done, check the current codebase state for partial changes, and resume from where it left off.  If the prior run failed on a specific tool call or timed out, be careful not to just launch into the same situation.

## Pre-existing test failures

If the tests you run surface a failure that is plainly **not yours** — broken at HEAD before your edits, in a subsystem outside your diff, or otherwise clearly unrelated — do NOT try to chase it inside this ticket. Instead:

1. Write `tickets/.pre-existing-error.md` (overwrite if it already exists) containing:
   - the exact test command(s) you ran (and from which package, for monorepos),
   - the failing test name(s) and a short excerpt of the error output,
   - one sentence on why you believe it is pre-existing (e.g. "fails on `main` at the same SHA", "asserts against module X which this ticket never touches").
   - any steps you have done to disable or work-around the failure for the sake of completing your ticket
2. Finish your own ticket normally.

After your ticket commits, the runner reads `.pre-existing-error.md` and dispatches a triage agent that either fixes the failure or files a `tickets/backlog/` ticket. Don't second-guess that pass — your job is to flag the failure, not resolve it. Failures clearly caused by your own changes are not pre-existing; fix those before handing off.

## BUDGET_WARNING

If you receive a `BUDGET_WARNING` from the runner, the conversation has crossed its soft token budget and you should wrap up rather than continuing to investigate or implement:

- Once you wrap up what you are in the middle of, update the ticket to reflect your progress and learnings.
- If the work is too significant for one ticket, create additional ticket(s) in the **same stage** (not next) to decompose the work; use `prereq:` headers to determine the order.
- If the additional tickets replace the original ticket, delete the original.
- Exit cleanly and don't run more tests or run more tools after the ticket update/writes

## Efficiency tips:

- Use the `files:` header in tickets — it saves the next agent from re-discovering paths.
- Use the `prereq:` header to name other tickets (by slug, without sequence prefix) whose landing you depend on.  Omit sequence prefixes — they may change.
- When spawning sub-agents, give them specific file paths rather than asking them to explore.
- Use the appropriate section of AGENTS.md for the project layout — don't guess paths.
- Run tests and type checks during implement, not just during review.
- Long-running validation: runner kills if no output for 10 minutes (idle timeout).  If a command might run that long, **stream its output** (e.g. `yarn foo 2>&1 | tee /tmp/foo.log`) — never `> /tmp/foo.log 2>&1`, since silent redirection lets the idle timer expire and the run is lost.  If a command's wall-clock routinely exceeds ~10 minutes, it is **not agent-runnable**: skip it inside the ticket, document the deferral, and let a human or CI handle it out-of-band.
- **Never use `run_in_background: true` / `Monitor` / wait-for-notification patterns under tess.** Agent in `claude -p` mode - first `result` message ends the turn and runner will tree-kill agent. Validate in foreground with `tee`. To parallelize, chain in single shell pipeline.

For new tickets: put a new file into `fix/` or `plan/` (or `backlog/` if it's a future concern rather than active work) but focus on the **description, requirements, and specifications** of the issue or feature, expected behavior, use case, etc.  **Don't do planning, don't add TODO items, or get ahead**, unless you already possess key information that would be useful.  Think use cases, expectations, and specifications.

**The `description:` field is the plain-language summary — write it for a newcomer, not for yourself.** One sentence (two at most) that someone with *no prior context* can understand: what is wrong / what to build, and why, in human terms. It is the first — often only — thing skimmers, dashboards, and the next agent read. Keep symbol names, file paths, acronyms, commit SHAs, ticket slugs, and internal-mechanism detail **out** of it; all of that belongs in the body below the header fence. A multi-paragraph `description:` block dense with jargon is an anti-pattern — it makes the queue unreadable. If you can't say what the ticket is about in a plain sentence, you don't yet understand it well enough to file it.

Ticket file template:

----
description: <ONE plain-language sentence (two at most), jargon-free, understandable with no prior context — what the ticket is about and why. NOT a technical abstract; the detail goes in the body.>
prereq: <slugs of other tickets that must land first — comma-separated, no sequence prefix, no .md>
files: <list key files touched/relevant — saves the next agent significant discovery time>
difficulty: <optional; easy|medium|hard — how much horsepower the work needs. Default medium. Drives model/effort selection (e.g. hard → a stronger model); omit unless the work is unusually simple or hard.>
----
<timeless architecture description focused on prose, diagrams, and interfaces/types/schema>

<if implement: TODO list of tasks - avoid numbering of tasks, besides phases>
