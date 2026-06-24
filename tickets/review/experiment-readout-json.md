description: Review the --json-output-path addition to the experiment readout subcommand so dashboards and automation can consume the result dict.
files: src/meta_ads_analysis/cli.py, tests/test_meta_ads_analysis.py, knowledge/learnings.md
difficulty: easy
----
## What was done

Added `--json-output-path` to the `readout` subparser in `experiment_main()` (cli.py ~line 1394).
When provided, the full `read_experiment` result dict is persisted as pretty-printed UTF-8 JSON
using the existing `write_json` / `ensure_dir` utilities, and a confirmation line is printed.
Flag omitted → behavior unchanged (stdout table only).

Updated `knowledge/learnings.md` with a one-line note under the `experiment` entry.

Added `test_readout_json_output_path` in `tests/test_meta_ads_analysis.py` which:
- calls `read_experiment` with a fake client (reuses `_ExpFakeClient`)
- writes via `ensure_dir` + `write_json` (same code path the CLI uses)
- round-trips via `json.loads` and asserts `verdict`, `control`, and `variant` are present and correct

All 79 tests pass (`python -m pytest -q`).

## Use cases for validation

1. **Flag omitted** — `experiment readout --account X --id Y` should print the table and exit cleanly with no file written.
2. **Flag with existing parent** — `experiment readout ... --json-output-path /tmp/out.json` writes the file, prints `Wrote readout JSON: /tmp/out.json`, and the file round-trips to a dict with the expected keys.
3. **Flag with missing parent** — `experiment readout ... --json-output-path /tmp/new-dir/out.json` creates the parent directory and writes the file without crashing.
4. **JSON content** — the written dict includes `verdict`, `control`, `variant`, `roas_lift_pct`, `conversion_rate_pvalue`, and `generated_at`.

## Known gaps / review focus

- The test calls `write_json`/`ensure_dir` directly rather than going through the CLI argument parser — end-to-end CLI invocation is not tested (would require mocking `client_from_env` and `resolve_ad_account_id`). Manual smoke test against the real CLI should be done before treating this as fully covered.
- No test for the "missing parent directory" branch specifically, though `ensure_dir` itself is well-tested elsewhere.
