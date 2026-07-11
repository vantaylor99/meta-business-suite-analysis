description: Verify the three cross-account tools correctly accept a pre-fetched performance snapshot instead of re-fetching it, and that the new unit tests genuinely prove the read savings and the safety guards.
prereq:
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/reader_provider.py
difficulty: medium
----
## What this ticket was

A **precomputed-perf injection seam** on three cross-account tools so a caller (the portfolio digest)
that already holds a `cross_account_performance` result for the window can hand it in and skip the
internal re-fetch. Purely **additive / backward-compatible**: with the new kwarg omitted, each function
is byte-identical to before, and the kwarg is **NOT** exposed to the LLM (`build_discovery_tools`
wrappers do not add it — internal/test-style seam, like the existing `fx_table` param).

**Important context (unchanged from the implement handoff):** the three seams had *already landed
inline* via the dependent `2-portfolio-digest` ticket (commit `7dc29ce`). This run's job was to (a)
**verify** the landed seam faithfully matches the spec, and (b) add the **dedicated seam unit tests**
that the ticket called out as the still-valuable remaining work. Both are done. **No production code was
changed this run** — the seams were verified as-is and only tests were added.

## Seam verification (done — all three match the spec)

All three live in `src/meta_ads_analysis/account_discovery.py`:

- **`grade_accounts_against_goals(..., precomputed_perf=None)`** (`account_discovery.py:3133`,
  branch at `:3197`). When injected, it bypasses BOTH the internal `cross_account_performance` fetch
  AND the `empty_default` early-return; scope becomes exactly `precomputed_perf["accounts"]`;
  `registry_by_id` still built the same way, so an injected account absent from config grades
  `no_goal_configured`. Reads only native `row.get(metric)` / `row.get("spend")` (never `*_normalized`)
  — confirmed at `:3252` / `:3267`.
- **`flag_accounts_needing_attention(..., precomputed_current_perf=None)`** (`:1650`, branch at
  `:1744`). When injected, used verbatim as `current`; **baseline is still fetched** (`:1763`).
  Reporting-currency mismatch → `ValueError` at `:1747`, raised **before** the baseline read.
  `include_pacing` path untouched (out of scope; digest sets `include_pacing=False`).
- **`pacing_report(..., precomputed_perf=None)`** (`:2302`, branch at `:2381`). Used as step-1 `perf`
  ONLY when `elapsed_fraction > 0`; when `elapsed_fraction <= 0` the injection is **ignored** and the
  existing `[date_from, date_from]` self-read runs. Reporting-currency mismatch → `ValueError` at
  `:2386`. Step-2 budget fan-out unchanged.

MCP wrappers confirmed clean — `mcp_server.py:615/641/682` do **not** forward the new kwargs.

## Tests added (8, `tests/test_meta_ads_analysis.py`)

Grouped next to each existing suite. All use `FakeMetaReader` (MOCKS ONLY; `.calls` records every read)
and the existing helpers (`_grade_reader`, `_grade_account`, `_attention_reader`, `_pacing_reader`,
`_pc_camp`, `_fx`, `_PACING_KW`). New `_grade_seam_fixture()` shares the grade setup.

Grade seam (after the grade suite):
- `test_grade_precomputed_perf_zero_reads_identical` — injected perf → `dead_reader.calls == []`
  (zero `fetch_insights` / `list_ad_accounts` / `get_account`) AND output `== ` the self-fetching run;
  also asserts the injected non-configured `act_9` grades `no_goal_configured`.
- `test_grade_precomputed_perf_non_usd_parity` — inject a perf normalized to **EUR**; output `== ` the
  USD self-fetch grade (proves grade reads only native metrics).
- `test_grade_precomputed_empty_perf_no_crash` — empty injected perf → empty accounts, zeroed counts,
  no pause candidates, **errors passthrough** verbatim, zero reads.

Flag seam (after the flag suite):
- `test_flag_precomputed_current_perf_baseline_only_fanout` — injected current perf → `fetch_insights`
  fires for the **baseline window only** (exactly N reads, one fan-out) and output `== ` the
  non-injected run.
- `test_flag_precomputed_current_perf_currency_mismatch_raises` — EUR-injected vs USD-call →
  `ValueError`, `reader.calls == []` (fails before any read).

Pacing seam (after the pacing suite):
- `test_pacing_precomputed_perf_skips_step1_fetch` — injected perf → NO `fetch_insights`, NO
  `list_ad_accounts`; exactly the `3N` budget reads (`list_campaigns`/`list_adsets`/`get_account`); output
  `== ` the self-fetching run.
- `test_pacing_precomputed_perf_currency_mismatch_raises` — EUR vs USD (elapsed>0) → `ValueError`,
  zero reads.
- `test_pacing_precomputed_perf_ignored_when_not_started` — `as_of` before `date_from`
  (`elapsed_fraction <= 0`) → a **bogus ghost account** in the injected perf never surfaces, `act_1`
  from the self-read does, status `not_started`, self-read window not inverted.

## Validation run

- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` → **681 passed** (log at `/tmp/perf-seam.log`).
- The 8 new tests: `pytest -k precomputed` → 8 passed. The grade/flag/pacing/digest/perf suites all pass
  **unchanged** (backward compat holds).
- `py_compile` clean on both files. **No ruff/mypy/pyright is installed or configured in this repo**
  (no `[tool.ruff]`/`[tool.mypy]` in `pyproject.toml`, none in `.venv`, no lint command in AGENTS.md), so
  the ticket's "ruff/type checks" step is **not runnable here** — flagged for CI/human, not a gap I can
  close in-sandbox.

## For the reviewer — where to push

The tests are a floor, not a finish line. Worth an adversarial look:

- **The `injected == reference` equality assertions** (tests 1, 2, 4, 6 above by rough count) lean on
  grade/flag/pacing output being deterministic and, for grade, **FX-independent**. That holds because
  the injected and reference runs traverse identical code over identical native inputs. If you distrust
  full-dict `==` on floats, note the floats come from the *same* arithmetic on both sides (not a
  recompute), so they are bit-identical — but confirm that reasoning survives your scrutiny.
- **Digest calling-convention vs the flag test.** The digest passes `flag(..., account_ids=scope_ids)`
  (explicit scope), so its baseline read does **not** call `list_ad_accounts`. My flag test omits
  `account_ids`, so the baseline read *does* resolve scope via `list_ad_accounts` — I therefore assert
  on `fetch_insights` counts only, not `list_ad_accounts`. That's faithful to the seam contract but is a
  slightly different path than the digest exercises; the digest's own tests
  (`test_portfolio_digest_*`, esp. `_default_read_count_grade_reuses_shared_perf` and
  `_currency_normalizes_to_eur_and_no_fx_isolated`, ~`:16355`/`:16527`) cover the explicit-scope path.
- **Untested corners I chose not to add (judge whether they matter):**
  - flag's injected-current **errors passthrough** (no-FX / per-account read-fail entries flowing from
    the injected perf into the merged `errors` tagged `window="current"`) is not directly asserted for
    flag (it *is* for grade via the empty-perf test). Low risk — flag treats the injected perf exactly
    like a self-fetched one — but not locked by a test.
  - flag/pacing with an injected perf containing a **no-FX account** (native-metric fallback) — relies
    on the same native-read path the non-injected tests already cover, but not re-proven through the
    seam.
  - pacing's currency-mismatch guard is only reachable on `elapsed_fraction > 0`; the `<= 0` branch
    deliberately skips it (tested that the perf is ignored, but not that a *mismatched* currency is
    silently accepted-because-ignored on that branch — arguably a non-issue since nothing is read).
- **Scope-mismatch (grade).** The spec says an injected perf whose accounts differ from what
  `account_ids` would resolve → injected accounts win (intentional). Not independently tested beyond the
  `act_9`-absent-from-config case; if you want the "injected id that `account_ids` would *drop*" case
  locked, that's a candidate addition.

None of these rise to a `fix/`-ticket in my judgment — they're coverage deltas on an already-green,
backward-compatible additive seam. If the reviewer disagrees on any, spin a small `fix/` ticket rather
than blocking.
