# Status — Tess Development Progress

## Phase 1: Planning and Design

- [x] Examine existing optimystic/tasks system
- [x] Examine appeus-2 integration pattern for reference
- [x] Create initial README.md
- [x] Create DESIGN.md with principles and open questions
- [x] Create INSTALLATION.md — dual-mode installation design
- [x] Create STATUS.md (this file)
- [x] Create docs/README.md — docs vs usage documentation split
- [x] Resolve Q1: Stage folder naming — keep current names
- [x] Resolve Q2: Dual installation mode (submodule standard, symlink alternative)
- [x] Resolve Q3: Keep fix/plan separate
- [x] Resolve Q4: Runner at tess/scripts/run.mjs; user can wrapper/symlink
- [x] Resolve Q5: Single AGENTS.md for now; agent-rules/ folder in tess keeps architecture open
- [x] Resolve Q6: .gitignore — `.logs/` only
- [x] Resolve Q7: No built-in overrides; user handles customization; revisit if needed
- [x] Resolve Q8: Keep priority-in-filename convention
- [x] Resolve Q9: Create both AGENTS.md and CLAUDE.md symlinks; configurable variant list
- [x] Resolve Q10: Keep Node.js for runner and init script
- [x] Resolve Q11: Implement detach script (scripts/detach.mjs)
- [x] Resolve Sub-Q: Version detection — git submodule handles it; runner prints commit hash in banner

## Phase 2: Core Package

- [x] Adopt and adapt `run-tasks.mjs` → `run.mjs`
- [x] Adopt and adapt `AGENTS.md` → `agent-rules/tickets.md`
- [x] Create `scripts/init.mjs` (project initialization — Node.js, cross-platform)
- [x] Create `agent-rules/root.md` (tess section for project root convention files)
- [x] Define `tickets/` scaffold structure (in init.mjs)
- [x] Define `.gitignore` for tickets (in init.mjs)
- [x] Verify runner source (reported bugs were subagent transcription errors, source is clean)
- [x] Agent adapters — adopted as-is from working optimystic source; revisit if tests fail
- [x] Create `scripts/detach.mjs` (tess removal — Node.js, cross-platform)

## Phase 3: Testing and Validation

- [ ] Test init script on a clean project
- [ ] Test runner with Claude adapter
- [ ] Test runner with Cursor adapter
- [ ] Test full ticket lifecycle (plan → implement → review → complete)
- [ ] Test blocked workflow
- [ ] Validate commit message format

## Phase 4: Documentation

- [ ] Finalize README.md with accurate install/usage instructions
- [ ] Write agent rules documentation
- [ ] Document ticket file format with examples
- [ ] Document runner CLI and options

## Phase 5: Publish and Integrate

- [ ] Initialize git repo for tess
- [ ] Publish / make available
- [ ] Integrate into optimystic
- [ ] Integrate into sereus
- [ ] Integrate into fret
- [ ] Integrate into remaining projects
- [ ] Remove old `tasks/` systems from integrated projects

## Phase 6: Ticket format v2 (sequence + prereq + backlog)

- [x] Resolve Q12: Numeric prefix semantics — sequence (ascending, optional)
- [x] Resolve Q13: `prereq:` header, slug-only references, `backlog/` stage
- [x] Add `backlog/` to `init.mjs` stage scaffold
- [x] Write `scripts/migrate.mjs` (auto-invert numbering, rename `dependencies:` → `prereq:`, strip prefixes from references, stamp `tickets/.version`)
- [x] Update `scripts/run.mjs`: sequence-asc ordering, optional prefix, `--max-sequence`, topological sort with cycle/sequence-conflict detection, backlog excluded from `PENDING_STAGES`, auto-migration on startup
- [x] Update `agent-rules/tickets.md` for v2 language
- [x] Update README, DESIGN.md, INSTALLATION.md for v2 layout
- [x] Update `ui/` dashboard (types, api-plugin, TicketCard, TicketDetail, StageView, Pipeline) — sequence + prereq + backlog; ascending sort; side-rail backlog
- [ ] Migrate existing tess-consuming projects (lamina, …) and verify commit/test lanes remain green
