description: Review wiring of propose-budget into the python -m dispatcher and console-script parity for propose_creative_features and operator_brief.
files: src/meta_ads_analysis/__main__.py, pyproject.toml, AGENTS.md, README.md, docs/META_ACTION_WORKFLOW.md
----
## Summary

All three gaps from the implement ticket are closed:

1. **`propose_budget_main`** — added to the import block in `__main__.py` (between `operator_brief_main` and `propose_disable_advantage_main`), dispatch branch added for `{"propose_budget", "budget"}` (inserted between `apply_ops` and `propose_disable_advantage` blocks), and `propose-budget` added to both the usage string and the `Unknown command` error message.

2. **`propose_creative_features` / `operator_brief`** — both added as console-script entries in `pyproject.toml` (appended after `propose_video_ad`).

3. **Documentation caveats removed** — the footnote ¹ and its definition were stripped from `AGENTS.md`; the blockquote paragraph was removed from `README.md`; the `> **Invocation note:**` blockquote was removed from `docs/META_ACTION_WORKFLOW.md`.

## Smoke-test results

```
python3 -m meta_ads_analysis propose-budget --help  → exits 0, prints usage
python3 -m meta_ads_analysis budget --help          → exits 0, prints usage
```

## Use cases for review

- `python -m meta_ads_analysis propose-budget --help` — must print usage, not "Unknown command"
- `python -m meta_ads_analysis budget --help` — alias must also work
- `python -m meta_ads_analysis` with no args — usage string must include `propose-budget`
- `python -m meta_ads_analysis unknown-cmd` — error message must include `propose-budget`
- After `pip install -e .`: `propose_creative_features --help` and `operator_brief --help` must resolve
- AGENTS.md table row for `set_daily_budget` — no ¹ superscript, no footnote paragraph below
- README.md "One command caveat" blockquote — gone
- docs/META_ACTION_WORKFLOW.md "Invocation note" blockquote — gone

## Known gaps

- `pyproject.toml` changes for the two new console scripts only take effect after `pip install -e .`; the reviewer cannot verify them without reinstall. The `python -m` surface is verified above.
- No new tests were added; the change is pure wiring with no logic.
