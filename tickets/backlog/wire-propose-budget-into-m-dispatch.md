description: The command to propose a budget change can't be run the same way as every other action command — it only works as an installed script, not through the usual `python -m` shortcut, so anyone following the docs hits an "unknown command" error.
files: src/meta_ads_analysis/__main__.py, src/meta_ads_analysis/cli.py, pyproject.toml
difficulty: easy
----
## Why

`propose_budget_main` exists and is tested (`src/meta_ads_analysis/cli.py:1668`) and is registered as a
console script in `pyproject.toml` (`propose_budget = "...:propose_budget_main"`), so it runs after
`pip install -e .` as `propose_budget ...`. **But it was never added to the `python -m
meta_ads_analysis` dispatcher** (`__main__.py`): it is missing from both the import block and the
`if command in {...}` chain. As a result:

```
$ python -m meta_ads_analysis propose-budget --help
Unknown command: propose_budget. Use `ingest`, `report`, ...
```

Every other write command (`propose-enable-ads`, `propose-pause-ads`, `apply-ops`,
`propose-duplicate-ad`, …) is reachable via `python -m`, and all the docs use that form, so this is a
discoverability/consistency defect, not a missing capability. The budget capability itself
(`control.build_budget_plan` + `apply_ops_plan`) shipped and works.

Surfaced by the `hybrid-model-docs-and-tool-catalog` documentation pass, which documented the gap
honestly (AGENTS.md write catalog footnote, META_ACTION_WORKFLOW.md invocation note, README caveat)
rather than papering over it. Those docs should be simplified once this lands.

## What to do

- Add `propose_budget_main` to the import list in `src/meta_ads_analysis/__main__.py`.
- Add a dispatch branch, e.g. `if command in {"propose_budget", "budget"}: propose_budget_main(); return`.
- Add `propose-budget` to the usage string and the `Unknown command` help text in `__main__.py`.
- Smoke-test `python -m meta_ads_analysis propose-budget --help`.
- While here, optionally reconcile the inverse asymmetry: `propose_creative_features` and
  `operator_brief` are reachable via `python -m` but are **not** in `pyproject.toml`'s `[project.scripts]`
  (so they have no console script). Decide whether both surfaces should list every command; at minimum
  document the intended policy.
- Once landed, trim the three doc caveats (AGENTS.md footnote ¹, META_ACTION_WORKFLOW.md invocation
  note, README caveat) that currently warn `python -m ... propose-budget` fails.

## Out of scope

No change to the budget logic, grounding, or gate — only command wiring.
