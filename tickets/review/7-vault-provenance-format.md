description: Review the new machine-checkable provenance format for the knowledge vault and its `lint-vault` checker, which forces every recorded fact to declare its source, how to reproduce it, and when it was last confirmed — and flags live-account facts that have gone stale.
prereq: confidence-core, grounding-rules-and-external-evidence
files: src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/__main__.py, src/meta_ads_analysis/config.py, pyproject.toml, knowledge/learnings.md, knowledge/README.md, knowledge/accounts/divine_designs/profile.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What landed

A provenance **format + checker + retrofit** for the knowledge vault, the knowledge-base counterpart
to the live `confidence.py` engine. It speaks **ONE** vocabulary with that engine (a test pins it).

- **`src/meta_ads_analysis/knowledge_provenance.py`** (new, pure — file text + regex, no Meta API,
  no clock in the rubric; `today` is passed in like `confidence.py`):
  - `parse_learnings(text) -> list[LearningEntry]` — parses `### claim` blocks into
    `LearningEntry`/`EvidenceLine` dataclasses (the dependent `vault-audit` ticket reuses this).
    Re-joins multi-physical-line evidence so the trailing `_( … )_` tag parses; ignores the prose
    intro, `## section` headers, the "Tooling capabilities" bullet list, and markdown tables.
  - `lint(entries, *, today, reverify_days) -> list[Finding]` — **errors** (fatal): missing/invalid
    `src` tier, missing `Rot`/`Verified`, a `metric:` line with no `verify:` command, a
    `src: external` line with no URL, an untagged evidence line. **warns** (informational):
    `⏳ re-verify` for a `fast` entry older than `reverify_days`; `evergreen` is never age-flagged.
  - `lint_profile_baseline(...)` — light staleness check for the one `**Rot:**/**Verified:**`
    header on `profile.md`'s Performance-baseline section (never errors on prose bullets).
  - `render_report(...)` — compact report (errors first, then ⏳ warns) + exit code (1 on any error,
    or any warn under `--strict`).
  - `TIER_NAMES` is `frozenset(t.name for t in confidence.EvidenceTier)`; `BAND_EMOJIS` derives from
    `confidence.BAND_PRESENTATION` — **no second scale**, both pinned by tests.
- **`config.py`**: `KNOWLEDGE_REVERIFY_DAYS = 42` (≈6 weeks; deliberately > `CONFIDENCE_RECENCY_STALE_DAYS=14`).
- **CLI**: `lint_vault_main()` in `cli.py`; `lint_vault` console script in `pyproject.toml`;
  `python -m meta_ads_analysis lint-vault` (alias `lint`) in `__main__.py`. Flags: `--path`,
  `--profile`, `--today`, `--reverify-days`, `--strict`.
- **Retrofit** (provenance metadata ONLY — no claim/band/trend/number changed; verified by word-diff):
  - `learnings.md`: every entry got `Rot:`/`Verified:`; every evidence line got a `src:` tag;
    data-backed account-ROAS lines got `verify:` + `metric:`. Tier assignments: dev-mode/AA/format
    mechanics → `direct_observation` (`evergreen`); structural-confounding principle →
    `model_inference` (`evergreen`); Engaged-ROAS and IG>FB → `correlational` (`fast`); practitioner
    line → `external` (`fast`).
  - `profile.md`: Performance-baseline `Rot: fast · Verified: 2026-06-22` header.
  - `README.md`: new entry template + a "Provenance format" subsection + a `lint-vault` conventions
    bullet (forward-references `audit-vault`).

## How to validate

```
.venv/bin/python -m pytest tests/ -q                       # 158 passed
PYTHONPATH=src .venv/bin/python -m meta_ads_analysis lint-vault --today 2026-06-25
#   → "lint-vault: 13 entries · 0 error(s) · 0 warning(s)", exit 0
PYTHONPATH=src .venv/bin/python -m meta_ads_analysis lint-vault --today 2026-12-01
#   → 0 errors, 4 ⏳ warns (3 fast learnings + profile baseline); evergreen entries unflagged
```

Key tests (all in `tests/test_meta_ads_analysis.py`, search "Knowledge-vault provenance"):
`test_parse_learnings_extracts_structured_fields`, the five `test_lint_errors_*`,
`test_lint_staleness_flags_fast_but_never_evergreen`, `test_render_report_strict_*`,
`test_lint_vault_main_exits_{zero_when_clean,nonzero_on_format_error}`,
`test_lint_vault_main_strict_fails_on_stale_fast`,
`test_provenance_tier_names_are_exactly_confidence_evidence_tier`,
`test_provenance_band_emojis_match_confidence_presentation`,
`test_real_learnings_md_lints_with_zero_errors`, `test_real_profile_baseline_header_is_present_and_fresh`.

## Known gaps / things to scrutinize (treat the tests as a floor)

- **External-line citation is soft.** The retrofit had to add a URL to the Jon-Loomer/Meta-Help
  `external` line (lint requires one). I used the Meta Business Help Center **root**
  `https://www.facebook.com/business/help/` (guaranteed to resolve) plus an attributed verbatim
  quote ("advertisers saw a 4% decrease in median cost per result"). **The exact deep link and the
  precise wording of that quote were not verified against the live page** (this ticket is read-only
  w.r.t. the network). A human should confirm/replace the deep link + quote. The lint only checks
  that *a* URL is present — not that it resolves, nor that the quote is accurate. Consider whether
  that's strong enough for the `external` discipline.
- **`verify:` date windows are reconstructed from prose.** The `account_metrics` commands on the
  Engaged-ROAS and IG>FB lines were back-computed from "30d ending 2026-06-22" / "120d" phrasing, so
  the date bounds may be off by a day, and the breakdown variants append `--breakdown …` beyond the
  canonical `confidence.build_regenerating_query` shape. They are reproduce-it *pointers*, not
  byte-exact replays; `vault-audit` (the dependent ticket) is what re-runs them against fresh data.
  Reviewer: decide whether lint should additionally assert `verify:` matches the canonical query grammar.
- **Parser boundary rule.** An evidence block ends at a line that (after strip) starts with `- `,
  `###`, `## `, `| `, `➕`/`➖`, or a **bold field label** matching `^\*\*[^*]+:\*\*` (e.g. `**Apply:**`).
  Inline emphasis (`**not**`, `**capped 🔴 Low**`) does NOT end a block — this was a real bug found
  during dev (the external line's tag wraps onto a `**not**`-leading line). Worth a skeptical look:
  is there a learnings shape that defeats the join? (e.g. an evidence line whose continuation
  legitimately begins with a `**Word:**`-looking token.)
- **Profile scan is single-account.** `lint_vault_main` defaults `--profile` to the divine_designs
  path. Fine today (one account); when the ~25-specialist template lands this needs to iterate
  accounts. Not wired to fail if the profile is missing (it's skipped).
- **Lint is structural, not semantic.** It checks presence/validity of tags, not whether a `src:`
  tier is *honest* for the evidence, nor whether `metric:` values still match live data. That
  semantic drift pass is deliberately the downstream `vault-audit` ticket.
- **Meta-test pins `today=2026-06-25`** so `test_real_learnings_md_lints_with_zero_errors` won't
  flake as the calendar advances (it asserts errors only). If you change the real file's `Verified`
  dates, that test still only cares about errors.

## Downstream (not part of this ticket)

`vault-audit` / `audit-vault` — re-pull each `metric:` line's `verify:` query against fresh metrics,
flag drift, and refresh `Verified:` on `--apply`. It consumes `parse_learnings` built here.
