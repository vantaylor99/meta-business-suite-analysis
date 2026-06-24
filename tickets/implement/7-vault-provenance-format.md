description: Make every fact in the committed knowledge vault declare where it came from, how to reproduce it, and when it was last confirmed — and add a checker that flags facts about a live account that have gone stale. The goal is that a wrong guess can't quietly harden into "fact" that future sessions trust.
prereq: confidence-core, grounding-rules-and-external-evidence
files: knowledge/README.md, knowledge/learnings.md, knowledge/accounts/divine_designs/profile.md, src/meta_ads_analysis/knowledge_provenance.py (new), src/meta_ads_analysis/cli.py, src/meta_ads_analysis/config.py, pyproject.toml, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Why

`knowledge/learnings.md` is the trust anchor — the agent reads it first and the ~25 MTC specialists
will inherit it as a template. Today an evidence line gestures at its source in freeform prose
(`_(direct API observation; divine_designs)_` vs `_(practitioner consensus…)_`). That is not
machine-checkable, carries no "last verified" date, and never ages out — so a 🟡 Medium inference
written once is treated as ground truth months later. This ticket makes provenance a **required,
consistent, regex-checkable tag**, adds a **last-verified date + volatility class** so live-account
facts age out, and ships a **`lint-vault`** checker that enforces it. It is the knowledge-base
counterpart to the live `confidence.py` engine — the two must speak **ONE** vocabulary.

This ticket is the format + checker + retrofit. The live "audit the vault against fresh metrics"
drift pass is the dependent `vault-audit` ticket (it reuses the parser built here).

## Shared vocabulary (do NOT invent a second scale)

Reuse `confidence.py` (the `confidence-core` ticket) verbatim:

- **Bands:** 🟢 High / 🟡 Medium / 🔴 Low / ⚪ abstain — the existing README rubric.
- **EvidenceTier (the provenance `src`):** `ab_experiment > direct_observation > correlational >
  external > model_inference`. The ticket's conceptual *observed / inferred / external* axis maps
  onto these: observed = {`direct_observation`, `ab_experiment`}, inferred = {`correlational`,
  `model_inference`}, external = `external`. Use the **tier names**, not a parallel 3-word set.
- **Regenerating query:** the exact string `confidence.build_regenerating_query` emits —
  `account_metrics --account <slug> --level <level> --date-from <from> --date-to <to>`.

## The provenance format (extend the README, don't replace it)

Every `➕`/`➖` evidence line gains a structured trailing tag (reads like today's parenthetical, just
parseable). Every entry gains a rot class + last-verified date. New entry template:

```
### <one-line claim>
**Confidence:** 🟡 Medium ↑  ·  **Domain:** platform | strategy | measurement
**Rot:** fast | evergreen  ·  **Verified:** YYYY-MM-DD
- ➕ YYYY-MM-DD — <supporting evidence> `verify: account_metrics --account <slug> --level <lvl> --date-from <f> --date-to <t>` _(src: direct_observation · acct: divine_designs · metric: blended_roas=3.74)_
- ➖ YYYY-MM-DD — <contradicting evidence> _(src: correlational · acct: divine_designs)_
**Apply:** <how to act on it>
**Would raise / lower:** <evidence that would move confidence>
```

Tag fields:
- `src: <tier>` — **required on every evidence line**; one of the five `EvidenceTier` names.
- `acct: <slug|—>` — the account the evidence came from, or `—` for account-agnostic facts.
- `metric: <name>=<value>` — **required when the evidence cites a live-account number** (e.g.
  `blended_roas=3.74`). Its presence marks the line *auditable* — `vault-audit` re-runs it.
- A line carrying `metric:` MUST also carry an inline `` `verify: account_metrics …` `` command
  (auditable ⇒ reproducible). External lines (`src: external`) MUST carry a URL + date + verbatim
  quote — that rule already lands in `grounding-rules-and-external-evidence` §2; just keep it
  consistent and let the lint require a URL on `src: external` lines.

Entry-level fields:
- `**Rot:** fast | evergreen` — volatility class.
  - `fast` = depends on *this live account's current numbers* or *current platform UI/policy state*
    (e.g. "Engaged Audience holds ~3.7 ROAS", "IG > FB on this account"). Subject to staleness.
  - `evergreen` = a **platform/API mechanic** that only changes if Meta changes the API (dev-mode-app
    blocker, `validate_only` honored, AA blocks audience edits) **or** a durable strategy principle
    ("lead with a hook"). **Never** auto-flagged on age — this is the "don't flag 'lead with a hook'
    as stale" guard from the parent ticket.
- `**Verified:** YYYY-MM-DD` — the date the claim was last confirmed (initially its first evidence
  date; `vault-audit --apply` refreshes it when it re-runs the claim).

## `lint-vault` — the checker (`src/meta_ads_analysis/knowledge_provenance.py` + a CLI)

A pure-ish module (file read + regex; no Meta API, no wall-clock inside the rubric — `today` is
passed in for determinism, mirroring `confidence.py`'s no-`datetime`-in-logic style):

- `parse_learnings(text) -> list[LearningEntry]` — structured records the dependent `vault-audit`
  ticket also consumes. Sketch (match repo dataclass + `from __future__ import annotations` style):

  ```python
  @dataclass(slots=True)
  class EvidenceLine:
      sign: str           # "+" | "-"
      date: str           # YYYY-MM-DD
      text: str
      tier: str           # one of EvidenceTier names
      account: str | None
      metric_name: str | None
      metric_value: float | None
      verify_query: str | None   # the account_metrics ... command
      url: str | None

  @dataclass(slots=True)
  class LearningEntry:
      claim: str
      band_emoji: str     # 🟢/🟡/🔴
      domain: str
      rot: str            # "fast" | "evergreen"
      verified: str | None
      evidence: list[EvidenceLine]
      lineno: int         # start line in the file — used by vault-audit for surgical edits
  ```

- `lint(entries, *, today, reverify_days) -> list[Finding]` with two severities:
  - **error** (fatal, drives a nonzero exit): missing/invalid `src` tier; missing `Rot`/`Verified`;
    a `metric:` line with no `verify:` command; a `src: external` line with no URL; an unparseable
    evidence line.
  - **warn** (informational, prints but does not fail by default): a `fast` entry whose `Verified`
    date is older than `reverify_days` before `today` → `⏳ re-verify`. `evergreen` entries are
    never age-flagged.
- CLI `lint_vault_main()`: scans `knowledge/learnings.md` (full enforcement). Flags include
  `--today YYYY-MM-DD` (default today), `--strict` (warnings also fail), and `--path` (override).
  Print a compact report (errors then ⏳ warnings); exit 1 on any error (CI-usable).
- Register the console script in `pyproject.toml` and the `python -m meta_ads_analysis lint-vault`
  subcommand mirroring an existing command's wiring (e.g. how `account_metrics`/`metrics_main` is
  registered — see `cli.py:813`).

`profile.md` is narrative, not the evidence format — keep its lint light: give the **Performance
baseline** section a `**Rot:** fast · **Verified:** 2026-06-22` header line so the staleness flag
covers the account's headline numbers, but do NOT reformat every prose bullet there. The lint may
scan `profile.md` for that single header's staleness only.

## Config

Add to `config.py` (do not change existing constants): `KNOWLEDGE_REVERIFY_DAYS = 42` (≈6 weeks —
matches the parent ticket's "six weeks later it's flagged re-verify" use case; deliberately longer
than `CONFIDENCE_RECENCY_STALE_DAYS = 14`, which governs *live-recommendation* recency, not vault
staleness). Reference, don't duplicate, the confidence floors.

## Retrofit existing knowledge (same run)

- `learnings.md`: add `Rot:`/`Verified:` to every entry and a `src:` tag to every evidence line,
  assigning tiers from the existing parentheticals — e.g. dev-mode-app blocker → `direct_observation`;
  the "Engaged audience may carry higher AOV/ROAS" cross-sectional, confounded entry →
  `correlational` (`fast`); the structural-reasoning audience/creative-confounding entry →
  `model_inference` (`evergreen`); the Jon Loomer / Meta-Help practitioner line → `external` (kept
  consistent with `grounding-rules-and-external-evidence`, which tags it confirm-via-A/B). Data-backed
  lines that cite a window get a `verify: account_metrics …` command + a `metric:` assertion. Mark
  platform/API mechanics and methodological principles `evergreen`; mark account-performance facts
  (Engaged ROAS, IG>FB, ROAS-by-band baseline) `fast`. **Do not change any claim's substance or
  band** — only add provenance.
- `profile.md`: add the baseline `Rot:`/`Verified:` header described above.
- `README.md`: update the entry template + "Confidence & evidence" section to document the tag, the
  rot class, and the last-verified date; add `lint-vault` (and forward-reference `audit-vault`) to
  the conventions / workflow so a new specialist learns the discipline. Keep additive to what
  `grounding-rules-and-external-evidence` already wrote — one vocabulary, no second scale.

## TODO

- [ ] Add `KNOWLEDGE_REVERIFY_DAYS = 42` to `config.py`.
- [ ] Create `knowledge_provenance.py` with `EvidenceLine`, `LearningEntry`, `parse_learnings`,
      `lint`, `Finding`, and the tier-name constant imported/shared with `confidence.EvidenceTier`
      (one source of truth — do not redefine the tier list).
- [ ] Add `lint_vault_main()` to `cli.py`; register console script + `python -m` subcommand.
- [ ] Retrofit `learnings.md` (every entry + every evidence line) and `profile.md` baseline header.
- [ ] Update `knowledge/README.md` template + conventions; cross-link `confidence.py`.
- [ ] Unit tests (below).
- [ ] `python -m pytest tests/ -q 2>&1 | tee /tmp/vault_fmt.log`, then run `lint-vault` on the real
      `learnings.md` and confirm zero errors (warnings OK).

## Key tests (TDD)

- `parse_learnings` on a fixture yields the right `LearningEntry`/`EvidenceLine` fields (tier,
  account, metric_name/value, verify_query, rot, verified, lineno).
- `lint` returns an **error** for: an evidence line with no `_( … )_` tag; `src: bogus_tier`; an
  entry missing `Rot`/`Verified`; a `metric:` line lacking a `verify:` command; a `src: external`
  line with no URL.
- Staleness: a `fast` entry with `Verified` 50 days before `--today` → `⏳ re-verify` **warn**; an
  `evergreen` entry 200 days old → **no** warning. `--strict` turns the warn into a failing finding.
- `lint_vault_main` exits nonzero when the file has a format error, zero when clean.
- Tier-name set in `knowledge_provenance` is identical to `confidence.EvidenceTier` (pin it so the
  two modules can't drift into two scales).
- After retrofit, `lint` over the real `learnings.md` returns **zero errors** (a meta-test that
  reads the committed file is acceptable here since it's deterministic).

## Edge cases & interactions

- **One vocabulary only.** `src` tiers must equal `confidence.EvidenceTier`; band emoji must match
  README + `confidence.py`. A test pins both so a refactor can't fork the scale.
- **Evergreen never ages out.** Platform mechanics and principles must not be staleness-flagged —
  test the dev-mode-app entry and a strategy principle stay un-warned at 200 days.
- **Missing inputs are errors, not silent passes.** A line with no tag, or an entry with no
  `Verified`, must fail the lint — never default to "assume fine."
- **Deterministic time.** `today`/`reverify_days` are passed in; no `datetime.now()` inside the
  rubric, so tests are stable and match the repo's existing no-clock test style.
- **Don't break the append-only narratives.** `decision-log.md` / `experiments.md` are dated prose,
  NOT subject to per-line provenance — the lint must not scan or fail on them.
- **Retrofit must not alter substance.** Only provenance metadata is added; every claim, band, and
  trend marker stays exactly as written. The reviewer will diff for unintended wording changes.
- **Regex robustness.** The tag parser must tolerate the existing `·`/`;` separators and extra prose
  before the trailing `_( … )_`; an evidence line can carry inline `code` (the verify command)
  before its tag without confusing the parser.
- **Read-only w.r.t. Meta.** This ticket touches no Meta API; it only reads/writes local markdown +
  ships a checker.
