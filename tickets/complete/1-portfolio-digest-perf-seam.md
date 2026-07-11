description: Verified the three cross-account tools correctly accept a pre-fetched performance snapshot instead of re-fetching it, and strengthened the unit tests that prove the read savings and safety guards.
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py, src/meta_ads_analysis/mcp_server.py
----
## What this ticket was

A **precomputed-perf injection seam** on three cross-account tools
(`grade_accounts_against_goals`, `flag_accounts_needing_attention`, `pacing_report`) so
`portfolio_digest` can hand in a `cross_account_performance` result it already holds for the window and
skip the internal re-fetch. Purely **additive / backward-compatible**; the kwarg is **NOT** exposed to
the LLM (like the existing `fx_table` seam). The three seams landed inline via the `2-portfolio-digest`
ticket (commit `7dc29ce`); the implement run (`bcdb161`) added 8 dedicated seam unit tests and verified
the seam matches spec. This review pass re-verified the seam independently, added one more test locking a
load-bearing invariant, and confirmed lint/tests.

## Review findings

### Seam correctness (checked — no defects)
Read all three seam branches line-by-line against the spec, with fresh eyes on the implement diff first:

- **grade** (`account_discovery.py:3197`) — injection bypasses BOTH the internal fetch AND the
  `empty_default` early-return; scope becomes exactly `precomputed_perf["accounts"]`; grading reads only
  **native** `row.get(metric)` / `row.get("spend")` (`:3252`/`:3267`), never `*_normalized` — so the
  output is FX-independent. An injected account absent from config grades `no_goal_configured`. Confirmed.
- **flag** (`:1744`) — injected perf used verbatim as `current`; baseline **still fetched** (`:1763`);
  currency-mismatch → `ValueError` at `:1747` **before** any read. Confirmed.
- **pacing** (`:2381`) — injection used as step-1 `perf` ONLY when `elapsed_fraction > 0`; ignored (self-
  read runs) when `<= 0`; currency-mismatch → `ValueError` at `:2386`. Step-2 budget fan-out unchanged.
  Confirmed.

### LLM-exposure safety (checked — holds)
`grep` confirms `precomputed_perf` / `precomputed_current_perf` appear **only** in
`account_discovery.py` (zero refs in `mcp_server.py`). The three MCP wrappers
(`mcp_server.py:615/641/682`) use **explicit named parameters** (no `**kwargs` splat) and omit the seam
kwargs entirely — the LLM-facing signature cannot pass them. The only callers are the digest itself
(`account_discovery.py:3483/3507/3545`). Safety property fully verified.

### Digest wiring (checked — correct)
`portfolio_digest` threads the single shared perf into all three seams, passes matching
`reporting_currency` + shared `fx_table` (so the guards never fire spuriously), and correctly skips
each sub-tool's inherited current-window errors when merging (`:3489`/`:3519`/`:3556`) to avoid
triple-counting the shared perf's per-account errors.

### Tests (checked — meaningful, non-tautological; one gap closed)
Ran the 8 implement-added tests (`pytest -k precomputed` → 8 passed) and read each. They are genuine:
the "zero reads" tests inject into a `FakeMetaReader()` that raises on any read and assert `.calls == []`;
the `injected == reference` equality tests are sound because both sides run identical arithmetic over
identical native inputs (the reference self-fetch and the injected run share the same fake data), so the
floats are bit-identical rather than independently recomputed. The mismatch-guard tests assert
`reader.calls == []`, proving fail-fast before any read.

- **Minor (fixed inline): one load-bearing invariant was only implicitly covered.** The flag seam tags
  the **injected** current-perf's errors with `window="current"`, which is exactly what lets the digest
  skip them (`account_discovery.py:3519`) and avoid double-counting — but the digest tests would *not*
  catch a mistagging here (the shared-perf error is tagged `section="performance"` independently, so
  `perf_no_fx` still finds it). Added `test_flag_precomputed_current_perf_errors_tagged_current`: builds
  a **real** `cross_account_performance` result (all `fx_as_of`/`account_count` keys the seam consumes)
  whose one account failed the current read, injects it, and asserts the error surfaces tagged
  `window="current"` (not `baseline`), while a genuinely-new baseline read failure surfaces tagged
  `window="baseline"`. This also incidentally documents that an injected perf must be a **complete**
  perf result, not a minimal stub (the seam reads `fx_as_of`/`fx_note`/`account_count`/`reachable_count`
  from it verbatim) — the digest always passes a real one, so no production change is warranted.

- **Coverage deltas left intentionally (not major, no fix ticket).** The implement handoff enumerated
  several untested corners; I judged each **low-risk and not worth blocking**: pacing's `<= 0` branch
  silently-accepting a mismatched currency (nothing is read on that branch, so it is inert); flag/pacing
  with an injected no-FX account (same native-read path the non-injected suites already cover); and the
  grade "injected id that `account_ids` would drop" case (the `act_9`-absent-from-config case already
  proves the injected scope wins). None change behavior of the additive, backward-compatible seam.

### Lint / type checks (not runnable here — flagged, not a gap)
No `ruff`/`mypy`/`pyright` is installed or configured (no `[tool.ruff]`/`[tool.mypy]`/`[tool.pyright]`
in `pyproject.toml`, none in `.venv`, no lint command in AGENTS.md). `py_compile` is clean. The ticket's
"ruff/type checks" step is not closable in-sandbox — left to CI/human.

### Validation
- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` → **682 passed** (681 pre-existing + the
  1 test added this pass). No regressions.
- `pytest -k precomputed` → 8 passed; `pytest -k errors_tagged_current` → 1 passed.

## Disposition
No major findings; no new `fix/`/`plan`/`backlog` tickets spawned. One minor test added inline. The
seam is a faithful, LLM-safe, backward-compatible additive change with a strengthened test floor.
