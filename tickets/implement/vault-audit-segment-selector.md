description: When the vault re-checks a stored number that was sliced by two or more dimensions at once (for example "Instagram, across all placements"), it can't tell which live rows to compare against and gives up. Let a fact name its exact slice so the re-check can find it.
prereq:
files: src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/cli.py, knowledge/learnings.md, knowledge/README.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Problem (recap)

`audit-vault` resolves a stored `metric:` claim to fresh live numbers by **token-matching** the
metric name against each fresh row's breakdown segment value(s) / entity name
(`resolve_fresh_metric` in `cli.py`, via `_metric_identifier_tokens` / `_row_identifier_text`). A
metric name resolves only when its discriminating tokens match **exactly one** fresh row; zero or
several matches abstain (`could_not_audit`) — by design it never guesses.

One real, already-stored claim falls through this today:

```
knowledge/learnings.md:194
  metric: ig_roas=3.63
  verify: … --breakdown publisher_platform,platform_position
```

The two-dimension breakdown returns many `instagram × {feed, stories, reels, …}` rows. The token
`{instagram}` matches **all** of them → ambiguous → `could_not_audit`. So a `lint-vault ⏳ re-verify`
flag on this line can never clear via `audit-vault`.

## Design decision (resolved — do NOT re-litigate)

**Add an explicit `select:` field to the provenance tag** naming the segment slice the claim
summarizes, and have `resolve_fresh_metric` resolve against it exactly instead of by token overlap.

**Critical correction to the ticket's original sketch.** Re-reading learnings.md:187–194, the claim
text is "Instagram 3.63 vs Facebook 2.55", and the per-cell numbers it lists are *IG Stories 4.33,
IG Reels 3.30, IG feed 3.25*. So **`3.63` is the Instagram platform-level blend across all
positions — NOT a single `platform_position` cell.** The ticket's example
(`…,platform_position=feed`) would wrongly map it to IG feed (3.25).

Therefore the selector must support **selecting a *subset* of rows and blending them**, not just
"exactly one row":

- selector matches **zero** rows  → `(None, None, None)` → `could_not_audit` (renamed/vanished
  segment — the safe-direction abstain is preserved).
- selector matches **exactly one** row → that row via `_row_value` (keeps the roas-or-derived path
  for rows that carry `roas` but no `purchase_value`).
- selector matches **several** rows → `_aggregate_value(matches)` — the **intentional, author-
  specified** blend (e.g. `select: publisher_platform=instagram` under a `publisher_platform,
  platform_position` breakdown blends all IG cells → the platform-level 3.63).

This is not "guessing on ambiguity" — with an explicit selector, multiple matches is the author's
intent (a coarser slice under a finer breakdown). The token-heuristic's "several → abstain" rule
stays in force only for the **no-selector** path (unchanged, fully backward-compatible).

### Tag syntax

A new optional field inside the existing trailing `_( … )_` tag, alongside `src:`/`acct:`/`metric:`:

```
_(src: correlational · acct: divine_designs · metric: ig_roas=3.63 · select: publisher_platform=instagram)_
```

- Value is comma-separated `key=value` pairs: `select: key1=val1,key2=val2`.
- Chosen over the `metric: … @ …` inline form because it parses with the same independent-field
  approach already used for `src:`/`acct:`/`metric:` (search-anywhere-in-tag), and commas inside the
  value don't collide with the tag's `·` field separator (the field regex stops at whitespace, and
  selector pairs contain no spaces).

### Parsing grammar

Add next to `_METRIC_RE` (knowledge_provenance.py ~line 85):

```python
# A `select:` slice — comma-separated key=value pairs naming the exact breakdown cells this metric
# summarizes (so a two-dimension breakdown row resolves exactly instead of by name-token overlap).
# Value chars are [A-Za-z0-9_=,] so it stops at whitespace / the `·` field separator, not at the
# commas *inside* the selector.
_SELECT_RE = re.compile(r"\bselect:\s*(?P<select>[A-Za-z0-9_=,.]+)")
```

`EvidenceLine` gains `metric_selector: dict[str, str] | None`. In `_parse_evidence`, after the
`_METRIC_RE` block:

```python
if sm := _SELECT_RE.search(tag):
    pairs = {}
    for piece in sm.group("select").split(","):
        if "=" in piece:
            k, v = piece.split("=", 1)
            if k and v:
                pairs[k] = v
    metric_selector = pairs or None     # malformed / empty → None → token-heuristic fallback
```

Default the field to `None` everywhere it is constructed (the dataclass + any test fixtures that
build `EvidenceLine` directly, if any — grep first).

### Resolution (cli.py)

Add a small predicate near `_row_identifier_text`:

```python
def _row_matches_selector(row: dict[str, object], selector: dict[str, str]) -> bool:
    """True if EVERY selector key/value is present in the row's `segment` dict (full-value, case-
    insensitive — not token overlap). A missing key → no match (safe abstain)."""
    seg = row.get("segment")
    if not isinstance(seg, dict):
        return False
    return all(str(seg.get(k, "")).lower() == v.lower() for k, v in selector.items())
```

Give `resolve_fresh_metric` a new keyword `selector: dict[str, str] | None = None` and branch it
**first** (before the account-aggregate and token paths), so existing no-selector claims are
untouched:

```python
if not rows:
    return None, None, None
if selector:
    matches = [r for r in rows if _row_matches_selector(r, selector)]
    if not matches:
        return None, None, None            # vanished/renamed segment → could_not_audit
    if len(matches) == 1:
        return _row_value(matches[0])       # single cell — keep roas-or-derived path
    return _aggregate_value(matches)        # author-specified slice — blend the subset
if not breakdowns and level == "account":
    return _aggregate_value(rows)
# … existing token-heuristic path unchanged …
```

In `run_vault_audit` (cli.py ~line 1135), thread the parsed selector through:

```python
value, purchases, spend = resolve_fresh_metric(
    rows, level=level, breakdowns=breakdowns,
    metric_name=ev.metric_name, selector=ev.metric_selector,
)
```

Update the `resolve_fresh_metric` docstring to describe the three selector outcomes and that the
token path is the no-selector fallback.

### lint-vault nudge (warn, never error)

In `_lint_evidence` (knowledge_provenance.py ~line 318), after the `metric_without_verify` check,
add a **warn** when a metric line is sliced by ≥2 breakdowns but carries no selector — the exact
class that the token heuristic can't resolve. Skip audit lines (`is_audit_line(ev)`).

```python
if ev.metric_name and ev.metric_selector is None and not is_audit_line(ev):
    breakdowns = parse_verify_query(ev.verify_query)["breakdowns"]
    if len(breakdowns) >= 2:
        findings.append(Finding(
            "warn", "select_recommended",
            f"metric: {ev.metric_name} is sliced by {len(breakdowns)} breakdowns "
            "but carries no `select:` field; audit-vault cannot resolve it without one",
            ev.lineno, entry.claim,
        ))
```

`parse_verify_query` / `is_audit_line` are defined later in the same module — fine, they resolve at
call time. Warn-not-error keeps existing entries lint-clean (the real-learnings test asserts no
**errors**, allows warns — tests/test_meta_ads_analysis.py:5249).

### Migrate the one broken real claim

Edit knowledge/learnings.md:194 to add the selector to the `ig_roas=3.63` tag:

```
_(src: correlational · acct: divine_designs · metric: ig_roas=3.63 · select: publisher_platform=instagram)_
```

(Use `publisher_platform=instagram` only — the platform-level blend — NOT a `platform_position`
value; see the design correction above.) Leave the single-breakdown `ig_roas=2.79` line (185–186)
**as-is** to prove backward-compat; mention in the review handoff that it could optionally gain
`select: publisher_platform=instagram` for consistency but does not need it.

### Docs

Update knowledge/README.md's provenance-tag documentation to list the optional `select:` field next
to `metric:` with the one-line "names the exact breakdown slice so a multi-dimension metric
re-verifies automatically; subset of keys → platform-level blend" explanation.

## Edge cases & interactions

- **Subset-blend vs single-cell** — `select: publisher_platform=instagram` under a two-dim breakdown
  → multiple rows → `_aggregate_value` blend; `select: publisher_platform=instagram,platform_position=stories`
  → one row → `_row_value`. Test both.
- **Zero matches (vanished/renamed segment)** → `(None, None, None)` → `could_not_audit`; band
  unchanged, no ➖. The safe-direction invariant must hold — assert it.
- **Selector key absent from the row's `segment` dict** → `seg.get(k,"")` ≠ value → no match (do
  not crash, do not partial-match).
- **Case-insensitive full-value match** — `Instagram` selector value vs `instagram` segment value
  resolves; substring (`insta`) must NOT match (full value only, unlike the token path).
- **Malformed / empty selector** (`select: publisher_platform` with no `=`, or `select:` empty) →
  `metric_selector` is `None` → falls back to the token heuristic (no crash, no silent wrong match).
- **Account-level pull with a selector** (rows have no `segment` dict) → `_row_matches_selector`
  returns False for all → zero matches → abstain. Harmless.
- **No-selector backward-compat** — the existing account-aggregate path and every existing
  `resolve_fresh_metric` test (tests/test_meta_ads_analysis.py:5409+) must pass byte-for-byte
  unchanged; the new `selector` kwarg defaults to `None`.
- **Idempotency** — the ➖ `vault audit:` bullets the audit writes carry `metric:` but no `select:`;
  they are skipped by `is_audit_line`, so a re-run on the same `--as-of` stays byte-identical even
  for selector-resolved claims. Confirm with a second-apply assertion.
- **Multi-metric / multi-selector in one entry** — two evidence bullets, each its own `EvidenceLine`,
  parse independently; a `select:` on one line must not bleed into the other.
- **Stray `select:` with no `metric:`** — `select_auditable` already skips lines without a
  `metric_name`, so it is inert; no new handling needed.
- **Real-corpus lint stays clean** — after the learnings.md edit, `lint(parse_learnings(real))`
  must still have zero **errors** (warns allowed); the migrated line must NOT trigger
  `select_recommended` (it now has a selector).

## Out of scope

Verdict math, band decrement, `➖` logging format, and the read-only/report-only guarantees are
unchanged. Do not migrate the single-breakdown `ig_roas=2.79` line. Do not change the token
heuristic for no-selector claims.

## TODO

### Phase 1 — parse the selector
- [ ] Add `_SELECT_RE` next to `_METRIC_RE` in knowledge_provenance.py.
- [ ] Add `metric_selector: dict[str, str] | None` to `EvidenceLine`; default `None` at every
      construction site (grep for `EvidenceLine(`).
- [ ] Parse it in `_parse_evidence` (comma-split → `k=v` dict; malformed/empty → `None`).
- [ ] Unit tests: tag with `select:` → dict; multi-key; malformed `=`-less → `None`; absent → `None`.

### Phase 2 — resolve against it
- [ ] Add `_row_matches_selector` in cli.py.
- [ ] Add the `selector` kwarg + selector-first branch to `resolve_fresh_metric`; update docstring.
- [ ] Thread `selector=ev.metric_selector` through `run_vault_audit`.
- [ ] Unit tests: single-cell (one key+value) → `_row_value`; subset blend (`publisher_platform=instagram`
      over two-dim rows) → blended roas; zero match → `(None,None,None)`; case-insensitive; selector
      ignored when `None` (existing tests unchanged).

### Phase 3 — lint nudge + migration + docs
- [ ] Add the `select_recommended` **warn** in `_lint_evidence` (≥2 breakdowns, metric, no selector).
- [ ] Test: two-breakdown metric line w/o selector → one `select_recommended` warn (no error); with
      selector → no warn.
- [ ] Edit knowledge/learnings.md:194 to add `· select: publisher_platform=instagram`.
- [ ] Document `select:` in knowledge/README.md.
- [ ] Confirm the real-learnings lint test (tests/...:5249) still passes (zero errors).

### Phase 4 — validate
- [ ] `python -m pytest tests/test_meta_ads_analysis.py -q 2>&1 | tee /tmp/vault-sel.log` (stream;
      never silent-redirect). All existing audit/resolve/lint tests green plus the new ones.
- [ ] Hand off to review/: note the live-data re-verify of `ig_roas=3.63` (that the IG blend really
      lands near 3.63) happens out-of-band via a real `audit-vault` run — tests use fake rows only.
