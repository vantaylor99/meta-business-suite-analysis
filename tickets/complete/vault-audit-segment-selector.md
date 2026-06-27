description: A stored vault number that was sliced by two dimensions at once (e.g. "Instagram, across all placements") couldn't be re-checked automatically; it can now, by letting the fact name its exact slice with a new `select:` field.
files: src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/cli.py, knowledge/learnings.md, knowledge/README.md, tests/test_meta_ads_analysis.py
----
## What shipped

`audit-vault` can now re-verify a `metric:` claim that is sliced by ≥2 breakdowns (e.g.
`ig_roas=3.63` under `publisher_platform,platform_position`). Such a claim previously matched
**many** fresh rows under the name-token heuristic → ambiguous → `could_not_audit`, so its
`lint-vault ⏳ re-verify` flag could never clear.

The fix adds an optional **`select:`** provenance field that names the exact breakdown slice.
`resolve_fresh_metric` resolves against it (full-value, case-insensitive) **before** the token
heuristic: zero matches → abstain; one → that cell; several → the author-specified blend. A
`lint-vault` **warn** (`select_recommended`) nudges authors of ≥2-breakdown metrics that lack a
selector. The token heuristic and every no-selector path are untouched.

See the commit `ticket(implement): vault-audit-segment-selector` (4a1696a) for the full
implementation rationale.

## Review findings

**Verdict: implementation is correct, well-tested, and backward-compatible. One minor robustness
trap found and fixed inline; no major findings, no new tickets filed.**

### Checked

- **Read the implement diff first, fresh.** Re-derived the resolution order in
  `resolve_fresh_metric`, the `_row_matches_selector` full-value/case-insensitive contract, the
  `_lint_evidence` warn gating, and the `_parse_evidence` selector parsing — all sound.
- **Safe-abstain invariant** (the project's "don't collapse missing into zeros" rule): zero-match,
  missing-key, and no-`segment` (account-level) rows all return `(None,None,None)`. Verified the
  selector branch never fabricates a value. ✔
- **Backward compat:** `selector=None` falls through to the unchanged token path; the
  single-breakdown `ig_roas=2.79` line and account-aggregate path are unaffected. Real-corpus
  `lint-vault` is `0 error · 0 warning`. ✔
- **`select_recommended` warn safety:** runs even when `verify_query` is `None`
  (`parse_verify_query(None)` → `breakdowns=[]` → no warn, no crash); skips `is_audit_line` ➖
  bullets; emits exactly one warn, never an error. ✔
- **Idempotency / safe-direction:** confirmed via the end-to-end tests — a selector-resolved
  contradiction logs a dated ➖ (which carries no `select:` and is `is_audit_line`-skipped) and a
  second `--apply` is byte-identical; a vanished IG segment leaves the file untouched. ✔
- **Type safety / resource cleanup:** no I/O or state added; `dict[str,str] | None` threaded
  cleanly through the one construction site and the one call site. ✔
- **Tests** (`pytest`): 343 passed. **Domain lint** (`lint-vault --today 2026-06-26`): 13 entries,
  0 error, 0 warning. The repo has no `ruff`/`mypy`/`pyright` config — `lint-vault` *is* the
  project's lint surface.

### Found & fixed inline (minor)

- **Silent multi-key truncation when a space follows the comma.** `_SELECT_RE` was
  `[A-Za-z0-9_=,.]+`, which stops at the first whitespace. So a naturally-spaced selector —
  `select: publisher_platform=instagram, platform_position=stories` — captured only
  `publisher_platform=instagram,` and silently parsed to `{publisher_platform: instagram}`. That is
  a **coarser slice than intended → a wrong blend rather than a safe abstain**, contradicting the
  project's safe-abstain philosophy, and `lint-vault` would not catch it. The README's
  `<key=value,…>` notation gave no spacing guidance, so an author would plausibly hit it.
  - Fix: capture now runs to the next tag field (`·`), tag end (`)`), or `;`/newline instead of to
    the first space, and `_parse_evidence` `.strip()`s each pair/key/value. Spacing is now tolerated
    and never drops a pair. As a bonus this also lifts the implementer's documented "selector values
    can't contain hyphens/whitespace" gap for the comma-separated multi-key case.
  - Guarded by three new assertions in `test_parse_evidence_selector_field`: spaced multi-key, a
    `select:` placed *before* another tag field (stops at `·`, doesn't swallow `acct:`), plus the
    pre-existing no-space / malformed / absent cases.
  - README updated: documents the multi-key pin form, that spaces are tolerated, and the
    full-value case-insensitive matching.

### Considered, no action (documented gaps confirmed acceptable)

- **Live re-verify is out-of-band.** All audit tests use fake rows; whether the real IG blend lands
  near 3.63 against live Meta needs a real `audit-vault --account divine_designs` run. Confirmed
  this is correctly out of scope for a unit-test pass — flagging, not a finding.
- **`ig_roas=2.79` (learnings.md) left selector-less on purpose** — single-breakdown, token
  heuristic resolves it; leaving it proves backward-compat. Agreed.
- **"Several matches = blend" with an explicit selector** is by design (the author's coarser slice),
  distinct from the no-selector "several → abstain". The reducer `_aggregate_value` re-derives ROAS
  from summed `purchase_value`/`spend` and abstains on missing value or zero spend — correct. Agreed.

### Not found (explicitly)

- No DRY/modularity issues — the selector branch reuses `_row_value`/`_aggregate_value`.
- No error-handling or type-safety gaps.
- No regression in the no-selector token path (covered by an explicit test).
