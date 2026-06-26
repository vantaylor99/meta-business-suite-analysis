description: Wire the propose-budget command into the python -m dispatcher and make both entry-point surfaces (python -m and console scripts) list the same set of commands.
files: src/meta_ads_analysis/__main__.py, pyproject.toml
difficulty: easy
----
## Context

`propose_budget_main` (cli.py:1703) is registered as the `propose_budget` console script in
`pyproject.toml` but is absent from the `python -m meta_ads_analysis` dispatcher in `__main__.py`
(missing import, no dispatch branch, absent from usage/error strings).

Two other commands have the inverse gap: `propose_creative_features` and `operator_brief` are
reachable via `python -m` but have no console-script entry in `pyproject.toml`.

The policy: **both surfaces must list every command**. This ticket fixes all three gaps in one pass.

## TODO

- In `src/meta_ads_analysis/__main__.py`:
  - Add `propose_budget_main` to the import block (alphabetical order, after `propose_authoring` area — fits between `propose_audio`/`propose_b*` neighbors; current neighbors are `propose_disable_advantage_main` and `propose_duplicate_ad_main`, insert before `propose_disable_advantage_main`).
  - Add dispatch branch after `apply_ops` / before `propose_disable_advantage` block:
    ```python
    if command in {"propose_budget", "budget"}:
        propose_budget_main()
        return
    ```
  - Add `propose-budget` to the usage string (line 50–55 area) and to the `Unknown command` error message (line 172–181 area).

- In `pyproject.toml` `[project.scripts]`:
  - Add `propose_creative_features = "meta_ads_analysis.cli:propose_creative_features_main"`
  - Add `operator_brief = "meta_ads_analysis.cli:operator_brief_main"`

- Smoke-test:
  ```
  python -m meta_ads_analysis propose-budget --help
  python -m meta_ads_analysis budget --help
  ```
  Both must exit 0 and print usage (not "Unknown command").

- Trim the three documentation caveats that warn `python -m … propose-budget` fails:
  - `AGENTS.md` footnote ¹ (search for "propose-budget" or "propose_budget")
  - `META_ACTION_WORKFLOW.md` invocation note
  - `README.md` caveat

## Edge cases & interactions

- `command = sys.argv[1].replace("-", "_")` normalises `propose-budget` → `propose_budget` and
  `budget` → `budget`; both must be in the dispatch set.
- `pyproject.toml` changes only take effect after `pip install -e .`; smoke-test via `python -m`,
  which does not require reinstall.
- No change to `propose_budget_main` logic, arguments, or grounding — wiring only.
