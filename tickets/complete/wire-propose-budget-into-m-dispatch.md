description: Make the budget proposer reachable through both command surfaces, and bring the two entry-point lists into full parity so every command works whether you call it via `python -m` or as an installed console script.
files: src/meta_ads_analysis/__main__.py, pyproject.toml, AGENTS.md, README.md, docs/META_ACTION_WORKFLOW.md
----
## Summary

`propose_budget_main` was a registered console script but absent from the `python -m
meta_ads_analysis` dispatcher; conversely `propose_creative_features` / `operator_brief` were
reachable via `python -m` but had no console-script entry. This ticket closed all three gaps and
removed the now-stale documentation caveats that warned `python -m … propose-budget` fails.

Implemented in `40a6e2f`:

- `__main__.py`: `propose_budget_main` imported; dispatch branch `{"propose_budget", "budget"}`
  added; `propose-budget` added to the usage string and the `Unknown command` error message.
- `pyproject.toml`: `propose_creative_features` and `operator_brief` added under `[project.scripts]`.
- Caveat removed from `AGENTS.md` (footnote ¹ + definition), `README.md` (blockquote),
  `docs/META_ACTION_WORKFLOW.md` (invocation note).

## Review findings

**Verdict: ship as-is. No major findings, no new tickets filed.** Pure wiring; correct and complete.

### What was checked

- **Dispatch wiring (`__main__.py`)** — import present (line 31, between `operator_brief_main` and
  `propose_disable_advantage_main` as the handoff claims); dispatch branch at lines 154–156; usage
  string (line 55) and `Unknown command` message (line 183) both list `propose-budget`. ✅
- **`command.replace("-", "_")` normalization** — both `propose-budget` → `propose_budget` and the
  `budget` alias resolve; both are in the dispatch set. Verified empirically below. ✅
- **Smoke tests (run in `.venv`)** —
  - `python -m meta_ads_analysis propose-budget --help` → exit 0, prints usage. ✅
  - `python -m meta_ads_analysis budget --help` → exit 0, prints usage. ✅
  - no-args usage string contains `propose-budget`. ✅
  - `python -m meta_ads_analysis bogus-cmd` error contains `propose-budget`. ✅
- **Console-script parity (the implement handoff's "Known gap" — could not verify without
  reinstall)** — **closed this pass.** The venv was installed *before* these entries existed, so the
  scripts were missing from `.venv/bin`. Ran `pip install -e .`; all three (`propose_budget`,
  `propose_creative_features`, `operator_brief`) now resolve on PATH and `--help` exits 0. (Editable
  reinstall only regenerates gitignored `.venv` entry points — no working-tree change.) ✅
- **Full surface parity (beyond the three named commands)** — programmatically diffed the `_main`
  functions exposed by `[project.scripts]` vs. the `__main__.py` import/dispatch block: **37 == 37,
  empty symmetric difference.** No command is reachable on only one surface. ✅
- **`cli.py` targets exist** — `propose_budget_main` (1703), `propose_creative_features_main` (2046),
  `operator_brief_main` (2144). ✅
- **Doc cleanliness** — grepped the whole repo: no lingering "not wired" / "Unknown command" /
  "invocation note" / "one command caveat" caveats in **active** docs; the footnote `¹` is gone from
  `AGENTS.md` and the surrounding table (line 295 → 297) reads cleanly with no orphan. Remaining
  matches are confined to `tickets/complete/` (historical archives — correctly left untouched) and an
  unrelated OAuth-seam "not wired here" note in `META_API_SETUP.md`. ✅
- **Tests** — `python -m pytest -q` → **343 passed in 0.48s.** ✅
- **Compile / lint** — `py_compile` clean on `__main__.py`. No lint tooling is configured in the
  project (no ruff/flake8/black/mypy; the only `lint` reference is the unrelated `lint_vault`
  console script), so there is nothing further to run. ✅

### Findings by category

- **Correctness / bugs** — none.
- **DRY / modularity** — none. The dispatch follows the existing flat `if command in {...}` idiom;
  matching the surrounding style is correct here.
- **Type safety / error handling** — none; unchanged surface, `propose_budget_main` logic untouched.
- **Resource cleanup / performance / scalability** — N/A; no resources or hot paths involved.
- **Tests** — no new tests added. **Accepted (minor, not filed):** the change is declarative wiring
  (import + dispatch dict entry + pyproject entry-point strings + doc deletions) with no branching
  logic; the four `python -m` smoke assertions plus the parity diff above cover it. A regression test
  asserting "every console script's `_main` is dispatch-reachable" would have value but is a separate
  hardening concern, not a gap in this ticket's scope.

### Disposition

All findings minor or none; nothing fixed inline (nothing needed fixing) and no major findings, so
no new fix/plan/backlog tickets filed.
