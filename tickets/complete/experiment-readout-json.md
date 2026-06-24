description: The A/B experiment readout can now optionally save its full result as a JSON file (via --json-output-path) so dashboards and automation can consume it, not just the on-screen table.
files: src/meta_ads_analysis/cli.py, tests/test_meta_ads_analysis.py, knowledge/learnings.md
difficulty: easy
----
## Summary

`experiment readout` gained an optional `--json-output-path` flag. When set, the full
`read_experiment` result dict is persisted as pretty-printed UTF-8 JSON via the shared
`ensure_dir` + `write_json` utilities, with a `Wrote readout JSON: <path>` confirmation line.
When omitted, behavior is unchanged (stdout table only). The implementation matches the
operator-brief convention already used elsewhere in `cli.py`.

## Review findings

**Implementation (cli.py ~line 1437) — correct.**
- `Path`, `ensure_dir`, `write_json` are all module-level imports in `cli.py` (lines 8, 100) —
  no NameError risk. Verified.
- The flag is added only to the `readout` subparser, and `args.json_output_path` is read only
  inside the `elif args.action == "readout":` branch — no AttributeError for `define`/`list`.
  Verified.
- `write_json` uses a `with` context manager (resource cleanup ✓) and pretty-prints with
  `indent=2` + trailing newline. Parent-dir creation and missing-dir handling are covered by
  `ensure_dir`. Error handling matches the rest of the CLI (no bespoke try/except — an
  unwritable path raises, consistent with sibling commands).
- **DRY (minor, not changed):** `ensure_dir(out.parent)` is redundant — `write_json` already
  calls `ensure_dir(path.parent)` internally. Left in place because it mirrors the established
  codebase pattern (e.g. `cli.py:969`) and the implement ticket explicitly requested it;
  changing it would diverge from convention for no functional gain.

**Tests — gap found and fixed (minor, fixed inline).**
- The implementer's `test_readout_json_output_path` called `ensure_dir`/`write_json` directly
  rather than going through the CLI, so it gave false confidence: a broken `experiment_main`
  branch (typo'd attr, wrong dict, missing import) would not have been caught. This was the
  stated "known gap."
- Added two true end-to-end tests that drive `experiment_main()` via `monkeypatch.setattr(sys,
  "argv", ...)` (the existing pattern used by `sync_meta_api_main` tests), stubbing
  `cli.resolve_ad_account_id` and `meta_api.client_from_env` and defining a real experiment in a
  temp `EXPERIMENTS_ROOT`:
  - `test_experiment_readout_cli_writes_json` — exercises the actual write branch, asserts the
    file is created **in a non-existent parent dir** (covers the missing-parent branch the
    implementer flagged as untested), the confirmation line is printed, and the JSON round-trips
    with `verdict`/both arms/`roas_lift_pct`/`conversion_rate_pvalue`/`generated_at`.
  - `test_experiment_readout_cli_no_json_path_writes_nothing` — flag omitted: table still prints,
    no confirmation line, no `readout.json` written (regression guard for the default path).
- Verdict asserted by substring (`"SIGNIFICANT"`/`"variant"`) rather than an exact string, to
  match the existing convention and stay robust.

**Docs — checked, accurate.**
- `knowledge/learnings.md` `experiment` entry updated with a one-line note on the flag. ✓
- `knowledge/accounts/divine_designs/experiments.md` references `experiment readout` in a
  narrative runbook; intentionally **not** changed — it's a play description, not a flag
  reference, and an optional output flag doesn't belong there.
- No other docs, README, or help text reference the command.

**Aspect sweep (SPP / scalable / maintainable / performant / type safety):** no issues. The
change is a single guarded, self-contained branch reusing shared utilities; result dict shape
and other subcommands are untouched.

**Lint:** N/A — repo has no ruff/flake8/mypy configured (`pyproject.toml` dev deps = pytest only).

**Tests run:** `python -m pytest -q` → **81 passed** (was 79; +2 new e2e tests). Green.

No major findings; no follow-up tickets filed.
