description: Added a machine-checkable provenance format for the knowledge vault plus a `lint-vault` checker, so every recorded fact must declare its source, how to reproduce it, and when it was last confirmed — and stale live-account facts get flagged. Reviewed and shipped.
files: src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/__main__.py, src/meta_ads_analysis/config.py, pyproject.toml, knowledge/learnings.md, knowledge/README.md, knowledge/accounts/divine_designs/profile.md, tests/test_meta_ads_analysis.py
----
## What shipped

A provenance **format + linter + retrofit** for the knowledge vault, the knowledge-base counterpart
to the live `confidence.py` engine — sharing ONE vocabulary with it (pinned by tests).

- **`knowledge_provenance.py`** (pure: file text + regex, `today` injected — no Meta API, no clock in
  the rubric): `parse_learnings(text)` → `LearningEntry`/`EvidenceLine`; `lint(entries, today, reverify_days)`
  → errors (missing/invalid `src` tier, missing `Rot`/`Verified`, `metric:` without `verify:`,
  `src: external` without URL, untagged evidence) + `⏳ re-verify` warnings for aged `fast` facts
  (`evergreen` never aged); `lint_profile_baseline(...)` for the one `profile.md` baseline header;
  `render_report(...)` → report + exit code (1 on error, or any warn under `--strict`).
- `TIER_NAMES` derives from `confidence.EvidenceTier`; `BAND_EMOJIS` from `confidence.BAND_PRESENTATION`
  — no second scale; both equalities test-pinned.
- **config**: `KNOWLEDGE_REVERIFY_DAYS = 42` (deliberately > `CONFIDENCE_RECENCY_STALE_DAYS = 14`).
- **CLI**: `lint_vault_main()` in `cli.py`; `lint_vault` console script; `python -m meta_ads_analysis
  lint-vault` (alias `lint`). Flags `--path --profile --today --reverify-days --strict`.
- **Retrofit** (provenance metadata only — no claim/band/trend/number changed): every `learnings.md`
  entry got `Rot:`/`Verified:` + per-line `src:` tags; data-backed ROAS lines got `verify:`+`metric:`;
  `profile.md` baseline got a `Rot: fast · Verified: 2026-06-22` header; `README.md` documents the
  format + the `lint-vault` conventions.

## How it was validated

- `.venv/bin/python -m pytest tests/ -q` → **159 passed** (was 158; +1 regression test, −1 unused import).
- `python -m meta_ads_analysis lint-vault --today 2026-06-25` → `13 entries · 0 error(s) · 0 warning(s)`, exit 0.
- `--today 2026-12-01` → 0 errors, 4 ⏳ warns (3 fast learnings + profile baseline), evergreen unflagged.
- `lint` alias dispatches identically (hyphen→underscore normalization in `__main__.py`).

## Review findings

**Scope reviewed.** Full implement diff (`knowledge_provenance.py`, `cli.py`, `__main__.py`,
`config.py`, `pyproject.toml`, `learnings.md`, `README.md`, `profile.md`, tests). Aspect angles:
parser correctness/boundaries, lint completeness (false negatives + false positives), DRY (regex reuse
across `lint`/`lint_profile_baseline`), ONE-vocabulary coupling to `confidence.py`, CLI wiring,
determinism, doc cross-references, type safety, error/exit-code handling.

**Adversarial probes run.** Glyph-less evidence bullet; tag stranded on a `**Field:**`-looking
continuation; `lint`/`lint_vault` alias dispatch; both pin tests.

**Fixed inline (minor):**
- Removed an **unused `Finding` import** in `tests/test_meta_ads_analysis.py` (imported, never used).
- Added **`test_tag_on_field_label_continuation_fails_loudly_not_silently`** — guards the
  parser-boundary tradeoff the implement ticket flagged: when a `_( … )_` tag lands on a continuation
  line that opens with a bold *field label* (`**Note:**`), the block does NOT join. The new test pins
  the **failure direction** — this surfaces a loud `missing_tag` error, never a silent pass. (Inline
  emphasis like `**not**`, no colon, correctly does NOT end a block — exercised by `_CLEAN_VAULT`.)

**Observations — accepted, no fix (documented tradeoffs / downstream-owned):**
- **Glyph-less evidence bullets are silently dropped.** A bullet that forgets the `➕`/`➖` glyph
  (`- 2026-01-01 — fact …`) does not match the evidence opener, so it is skipped and never linted —
  a malformed line escapes the checker. This is conservative-by-design: entries also contain non-evidence
  `- ` sub-bullets under `**Apply:**`, and a looser "looks like evidence" heuristic would false-positive
  on them. The committed `learnings.md` is well-formed; acceptable.
- **`acct:` is part of the documented format but not lint-enforced** (only `src:` is). Intentional —
  `src` tier is the load-bearing grounding field; `acct:`/`—` is informational. Could be tightened
  later if account-attribution becomes load-bearing (e.g. the multi-account template).
- **External-line citation is presence-only.** Lint requires *a* URL on a `src: external` line but
  does not verify it resolves nor that the quoted text is accurate. The retrofit used the Meta Business
  Help Center **root** URL + an attributed quote that were **not** verified against the live page
  (read-only ticket). A human should confirm/replace the deep link + exact wording.
- **`verify:` is not asserted against the canonical `build_regenerating_query` grammar.** The
  reconstructed `account_metrics` windows (and `--breakdown` variants) are reproduce-it *pointers*,
  not byte-exact replays; semantic re-pull + drift detection is the downstream `vault-audit` job.
- **CLI defaults `--today` to `date.today()`.** The lint *rubric* itself stays clock-free (the design
  invariant that matters); only the CLI convenience default reads the clock. README's "never read from
  the clock" is true of the rubric — left as-is.
- **Static lint not run:** no `ruff`/`mypy` in `.venv` (exit 127); the project does not appear to gate
  on them. Tests are the enforced gate and pass.

**No major findings → no new tickets filed.** The downstream `vault-audit` / `audit-vault` work
(semantic drift re-pull, `Verified:` refresh on `--apply`) is already a tracked dependent ticket and
consumes `parse_learnings` built here; it is intentionally out of scope for this format/linter ticket.
