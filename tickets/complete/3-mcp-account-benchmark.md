description: A new tool that shows how one ad account compares to its peers — e.g. "is this account's cost-per-lead good or bad?" — by ranking it as a percentile within a comparison group, so a bare number becomes interpretable ("72nd percentile — better than most peers").
prereq: mcp-cross-account-performance
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----

## What shipped

The `account_benchmark` discovery tool — the specialist-facing counterpart to
`cross_account_performance`. It ranks ONE account's efficiency metrics (CPM, CPC, cost-per-result, CTR,
ROAS) as **percentiles within a cohort** of peers, oriented so a high percentile always means "good"
for both cost and quality metrics. It is a pure post-processor over `cross_account_performance` (calls
it once for the cohort, target always included, computes percentiles/quartiles over the returned rows),
inheriting FX normalization, Simpson's-paradox-safe derived metrics, per-account failure isolation, the
`errors` list, and the deterministic bounded-concurrency fan-out. The MCP discovery set is now four
tools.

See the implement commit (`ticket(implement): mcp-account-benchmark`, `d89474c`) for the full landing
summary; this file records the review pass over that work.

## Review findings

Adversarial pass over the implement diff, read fresh before the handoff summary. Scrutinized SPP/DRY,
directionality correctness, FX value-source, error/edge paths, type safety, docs, and determinism.

### Verdict: solid — no correctness bugs, no major findings, no new tickets filed.

**Correctness (checked, clean).** Re-derived the percentile/quartile/verdict math by hand and against
the tests. `percentile_rank` mid-rank orientation is correct in both directions (a low cost and a high
ratio both land high); `quantiles` linear interpolation matches the pinned worked example; `rank`
(competition ranking) and `rank_of` are consistent with the cohort used. The ticket's must-have — a
high percentile always means good for both a cost metric and a quality metric within the *same* target
row — is enforced by `test_account_benchmark_direction_cost_vs_quality`.

**FX / value-source (checked, clean).** Money metrics rank on each row's `*_normalized` twin (reporting
currency), ratio metrics on the native value; verified via
`test_account_benchmark_ranks_on_normalized_money` (an MXN peer normalizing to the cheapest USD
correctly reorders the field). Single FX-table load shared with the prereq; a no-FX reporting currency
propagates `ValueError` from the prereq (same whole-call contract).

**Error / edge paths (checked, clean).** Unreadable target → `account: None` + per-metric read-failure
reason + prereq error passed through; no-FX target → money reasons, ratios still computed; no-FX cohort
member → dropped from money cohort only, surfaced in `excluded` + `errors`; target missing a metric →
`{value: null, reason}`, no percentile, every metric key still present; tiny cohort → numbers still
returned but flagged `too_small`/`unreliable`. All covered by existing tests.

**Edge cases probed manually (not previously tested).** Ran a scratch harness confirming: empty
explicit `cohort_ids=[]` degenerates to a correct target-only cohort ("no peers"); a duplicate target
in `cohort_ids` de-dups (no double count); an `UNKNOWN`-currency target falls back to "no FX rate for
UNKNOWN" on money metrics while ratios still benchmark; and repeated runs are byte-identical
(determinism). All behaved correctly.

**Minor — fixed inline (this pass).** Added two regression tests pinning the two benign-but-previously
untested paths above so a future refactor can't silently break them:
- `test_account_benchmark_empty_cohort_ids_degenerates_to_target_only`
- `test_account_benchmark_unknown_currency_target_money_reason`

Determinism at the benchmark layer was verified manually but *not* added as a test — it is fully
inherited from the prereq's fan-out (which has its own reordering test) and the percentile math runs
single-threaded over the already-ordered `accounts` list, so a benchmark-layer determinism test would
be redundant. Noted here explicitly rather than left silent.

**Known gaps from the handoff — reviewed, all accepted as-is (none rise to major).**
- `cohort.excluded` mirrors the target's own error when the target is unreadable / no-FX. Deliberate
  transparency (also in `errors`); the target is only partially "excluded" (money metrics, not ratios),
  but filtering it out could hide a real target-read failure. Left as-is.
- `percentile_rank` uses exact float `==` for the equal-count term. Mathematically correct; genuine
  float ties in Meta-derived values are vanishingly rare (the target itself always contributes the one
  guaranteed equal). Not a bug.
- `rank` is competition ranking, not documented in the output shape — field names `rank`/`rank_of` are
  self-explanatory. Left as-is.
- Quartiles (`p25`/`median`/`p75`) include the target in the distribution — intentional ("the honest
  distribution a human reads" contains the target). Left as-is.

**Docs (checked, current).** `README.md` and `docs/META_API_SETUP.md` both describe the new tool and
say "four discovery tools"; grepped for stale "three discovery tools" references — none remain (other
"three" hits are unrelated). Docstrings on `account_benchmark`, `quantiles`, `percentile_rank`, and the
module constants match the implemented behavior.

**Lint (not applicable).** `ruff`/`mypy` are not installed in this repo and there is no lint config
(`.ruff.toml`/`ruff.toml`/`mypy.ini`/pyproject entries all absent) — confirmed directly, matching the
prereq's handoff. Validation is import + pytest.

## Validation run during review

- `.venv/bin/python -c "import meta_ads_analysis.account_discovery, meta_ads_analysis.mcp_server"` → ok.
- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q` → **534 passed** (up from 532 with the
  two added regression tests).
- Focused: `-k "account_benchmark or percentile_rank or quantiles"` → 14 passed.
- Python is `.venv/bin/python` (3.14); system `python` is absent. No live Meta path (mocks only, per
  the ticket).
