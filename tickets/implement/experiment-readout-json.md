description: Let the A/B experiment readout optionally save its result as a JSON file, the same way the operator brief already can, so the numbers can feed dashboards and automation instead of only printing to the screen.
files: src/meta_ads_analysis/cli.py, src/meta_ads_analysis/experiment.py, tests/test_meta_ads_analysis.py, knowledge/learnings.md
difficulty: easy
----
`experiment readout` currently only prints a formatted table to stdout (see
`experiment_main()` in `src/meta_ads_analysis/cli.py`, the `args.action == "readout"`
branch). `read_experiment(...)` in `src/meta_ads_analysis/experiment.py` already returns a
complete result dict (control/variant arm summaries, `roas_lift_pct`,
`conversion_rate_pvalue`, `verdict`, `caveat`, `generated_at`). We just need a way to
persist that dict.

Add a `--json-output-path` option to the `readout` subparser, mirroring the existing
convention already used by the operator brief (`--json-output-path` near line 1633 of
`cli.py`, written via `write_json` from `meta_ads_analysis.utils`). When the flag is
given, write the full `read_experiment` result dict to that path as JSON (pretty-printed,
UTF-8) in addition to printing the normal human-readable table. When the flag is omitted,
behavior is unchanged (print only).

Keep it consistent with how the rest of the CLI does this:
- Reuse `write_json` from `.utils` (don't hand-roll `json.dump`).
- Print a confirmation line like `Wrote readout JSON: <path>` after writing.
- Create parent directories if needed (use `ensure_dir` on the parent, as other commands do).

## Edge cases & interactions
- Flag omitted → no file written, stdout output identical to today (don't regress the table).
- Parent directory doesn't exist → create it; don't crash.
- The result dict already includes a `generated_at` UTC timestamp from `read_experiment`,
  so the JSON is self-dating — don't add a second timestamp.
- Don't change `read_experiment`'s return shape or any other subcommand (`define`, `list`).
- `--json-output-path` is read-only persistence of a read-only readout — no Meta writes.

TODO
- Add `--json-output-path` to the `readout` subparser in `experiment_main()`.
- After computing the readout result `r`, if the path is set, `ensure_dir(parent)` then
  `write_json(path, r)` and print the confirmation line.
- Add a unit test in `tests/test_meta_ads_analysis.py` that calls the readout path with a
  fake client (reuse the `_ExpFakeClient` pattern already in the file) and asserts the JSON
  file is written and round-trips to a dict containing `verdict` and both arms. Run the full
  suite with `python -m pytest -q` and confirm it stays green.
- Add one line to the `experiment` entry in `knowledge/learnings.md`'s tooling list noting
  the optional `--json-output-path`.
