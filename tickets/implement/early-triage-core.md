description: Build the engine that judges a struggling brand-new ad against how similar past ads on the same account behaved when they were the same age, so we can tell a genuinely-bad ad apart from a slow-starting eventual winner.
prereq:
files: src/meta_ads_analysis/early_triage.py (new), src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/storage.py, src/meta_ads_analysis/actions.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## What this ticket builds

The **pure engine + data seam** for early-life ad triage. No CLI wiring, no monitor changes, no
follow-up writing — that is the sibling ticket `early-triage-monitor-integration` (which has this as
its `prereq:`). This ticket is everything that can be unit-tested against a fake history provider:

1. A **`HistoryProvider`** seam that yields per-ad daily time series for an account (so analog
   matching is testable with mocks; no live Meta).
2. A pure **analog-matching + recovery + verdict** module (`early_triage.py`).
3. A **confidence factory** for analog-grounded calls in `confidence.py`.

Everything here is **clock-free**: `as_of` (the run date) is passed in, never read from the system
clock — matching `monitor.py`'s discipline.

## Background / why

`monitor.classify_ad` today does the right thing for *thin* data by abstaining: an ad under the
`$100` significance floor returns `insufficient` (and is dropped from the watch report), and an ad
inside the `grace_days` (5) window returns `watch` (protected, abstain). That correctly protects
brand-new ads from premature kills — but it is **silent**: a genuinely-dead new ad and a slow-starting
future winner look identical and both just "keep running" with no scrutiny until day 5+.

The operator wants the constructive complement: when a *brand-new* ad (≈ day 1–3) is struggling, don't
silently abstain and don't auto-kill — instead grade it against **this account's own history of
comparable new ads at the same age**. If similar past ads that started equally badly later turned
around, keep it (and force a day-3 re-check). If comparable past ads at that age stayed bad — or there
is no comparable history — flag it as an early pause candidate through the normal guarded flow.

See the operator's words and the full intent in the source plan ticket history; the resolved design
is below.

## Resolved design decisions

These were the plan-stage open questions. They are **settled** here — the implementer builds to these,
not to a menu of options.

### Data source for analogs — the synced daily history (DuckDB), behind a provider seam

The richest "at the same age" source already exists: the normalized `ad_daily_metrics` rows. A single
synced snapshot's rows go back months (one `ingestion_run_date` carries daily `report_date` rows for
the whole account history — verified in `data/normalized/meta_ads/<slug>/<date>/ad_daily_metrics.csv`
and the DuckDB `ad_daily_metrics` table via `storage.fetch_run_rows`). So for every *past* ad we can
reconstruct its cumulative metrics **at age N days** and **after age N**, which is exactly what analog
matching and recovery detection need.

Define a provider protocol so the pure logic never touches DuckDB directly and tests inject a fake:

```python
@dataclass(slots=True)
class AdDailyPoint:
    report_date: date
    spend: float
    results: float          # goal-aware result count (see _select below)
    purchase_count: float
    purchase_value: float
    app_installs: float

@dataclass(slots=True)
class AdHistory:
    ad_id: str
    ad_name: str | None
    points: list[AdDailyPoint]      # sorted ascending by report_date, one per active day

    @property
    def first_seen(self) -> date: ...        # min report_date
    def age_on(self, as_of: date) -> int: ...  # (as_of - first_seen).days  (day 1 == age 0)

class HistoryProvider(Protocol):
    def ad_histories(self, account_slug: str) -> list[AdHistory]: ...
```

Ship one concrete implementation, `DuckDBHistoryProvider`, that reads the **latest** ingestion run's
rows for the account via `storage.fetch_run_rows` (latest `ingestion_run_date`) and groups them into
`AdHistory` objects. Keep all DuckDB/SQL in this provider; the matching logic takes `list[AdHistory]`.

### "Age" computation — deterministic, passed in

`age = (as_of - first_seen).days`. Day 1 of life is age 0; "day 3" is age 2. `as_of` is supplied by
the caller (the run date), never `date.today()`.

### Early-life window + decision age

- **Early-life window**: `age <= EARLY_LIFE_MAX_AGE` (default **2** → days 1–3). The triage only
  *applies* to ads this young; older ads stay with the existing monitor logic.
- **Decision age**: `age >= EARLY_LIFE_DECISION_AGE` (default **2**, i.e. by day 3) → the engine must
  return a **confident keep/kill**, never an indefinite abstain. (Day-3 forcing is consumed by the
  integration ticket; the engine exposes the verdict.)

### "Struggling" / early-performance bar — goal-aware

Reuse the account's existing floors/targets and the existing goal→metric selection
(`actions._select_action_metric`, keyed off `policy["primary_goal"]`):

- **ROAS/purchase accounts** (`primary_goal == "roas"` or ROAS present): struggling ⇔ life-to-date
  `blended_roas < pause_roas_floor` (the same floor `monitor._policy_floors` reads), **or** zero
  results on non-trivial spend.
- **Install-goal accounts** (`primary_goal == "maximize_in_app_subscriptions"`): struggling ⇔
  life-to-date `cost_per_app_install` worse (higher) than the account's target install cost, or zero
  installs on non-trivial spend. (Note the interaction with the install-goal grounding backlog items —
  see "Interactions" below; do not block on them, use whatever target the policy exposes today and
  fall back gracefully when absent.)

If the ad is **not** struggling, the engine returns a `not_struggling` verdict and the caller leaves
it to normal flow. The engine only does analog work for struggling early-life ads.

### "Comparable" analog definition + tolerance

The operator named three comparability features and called the **spend-to-result ratio at the same
age** the most robust. An `AdHistory` `h` is an **analog** of the triaged ad at age `N` iff:

- `h` reached at least age `N` (so we can slice it at the same age), and its age-`N` window exists;
- `h` was **also struggling at age N** (same goal-aware struggling test, applied to `h`'s
  cumulative-through-age-N metrics) — we compare against ads that *also started badly*, not all ads;
- **magnitude-comparable** at age `N` within a multiplicative tolerance
  `ANALOG_RATIO_TOLERANCE` (default **0.5**, i.e. 0.5×–2.0×):
  - **primary**: if both the triaged ad and `h` have ≥1 result at age N, their cost-per-result
    (spend/results) ratio is within tolerance;
  - **fallback (zero-result case — the common day-1 reality)**: when the triaged ad has ~0 results at
    age N, require `h` to also have ~0 results at age N **and** cumulative spend within tolerance.
    (A brand-new ad usually has 0 conversions; ratio is undefined, so spend-magnitude + zero-result is
    the match.)

Exclude the triaged ad itself (`ad_id` equality) and exclude ads still too young to have a recovery
window (see next).

### "Turned around" / recovery signal

Slice `h` after age N. `h` **recovered** iff its cumulative metrics over the later window
`[age N+1 .. min(h's last age, EARLY_LIFE_RECOVERY_HORIZON)]` cleared the account **target**:

- ROAS goal: cumulative `blended_roas >= target_roas` (the target `monitor._policy_floors` reads).
- install goal: cumulative `cost_per_app_install <= target install cost`.

`EARLY_LIFE_RECOVERY_HORIZON` default **7** (by day ~8). An analog that never reached the recovery
horizon (too short-lived to judge) is **excluded from the population entirely** — it is neither a
recovery nor a non-recovery (avoids counting an ad that was simply paused early as "stayed bad").

### Verdict from the analog population — survivorship-aware

Let `A` = number of qualifying analogs (matched **and** old enough to judge recovery), `R` = number
that recovered.

- `A < EARLY_LIFE_MIN_ANALOGS` (default **3**) → **`abstain_keep`**: not enough comparable history to
  make a confident early call. Keep the ad, defer to a day-3 re-check. This is the **"no analogs"
  fallback** — never a confident early kill on thin analog history.
- `A >= min` and `R / A >= EARLY_LIFE_RECOVERY_RATE` (default **0.33**) → **`keep_watch`**: comparable
  ads that started this badly recovered often enough; keep it, file the day-3 follow-up.
- `A >= min` and `R / A < rate` → **`pause_candidate`**: comparable ads at this age overwhelmingly
  stayed bad; surface as an early pause candidate (the integration ticket routes it through the
  guarded propose flow). Never a silent kill.

The survivorship guard is the **rate over the whole matched population**, not "≥1 lucky recovery" —
one ad that turned around inside a population of twenty that died is `R/A = 0.05`, well below the
keep threshold.

### Confidence — a new analog-grounded factory in `confidence.py`

Analog calls are **correlational** (cross-sectional comparison to *other* ads), so they cap at
**medium** — consistent with how scale-candidate/trajectory calls are tiered. The triaged ad's own
sample is below floor by definition, so do **not** route this through `assess` (its purchase/spend
floor would abstain everything). Instead add a sanctioned factory beside `abstain_confidence`, keeping
all `Confidence` construction inside `confidence.py` and the "band is never free-typed" invariant
(the band is computed deterministically from analog counts):

```python
def analog_confidence(*, analogs: int, recovered: int, min_analogs: int,
                      strong_analogs: int, factors: list[str]) -> Confidence:
    # tier = correlational (ceiling medium, set via grounding_strength)
    # data_band: abstain if analogs < min_analogs;
    #            medium if analogs >= strong_analogs;
    #            else low
    # combined = weaker axis (so it can never exceed medium)
```

`strong_analogs` default **6** (`EARLY_LIFE_STRONG_ANALOGS`). `abstain_keep` verdicts use the existing
`abstain_confidence` (data axis abstains). Every verdict carries an `Evidence` block describing the
analog basis (see below).

### Evidence block

The engine attaches an `Evidence` (same dataclass `monitor`/`actions` use) so the call survives the
grounding/review rules:

- `metric_name` / `metric_value` / `metric_display`: the triaged ad's life-to-date goal metric
  (ROAS or cost-per-install).
- `window`: `first_seen..as_of`.
- `sample_purchases` / `sample_spend`: the ad's own life-to-date sample (honestly thin).
- `entity_*`: the triaged ad.
- `regenerating_query`: `build_regenerating_query(account_slug, "ad", first_seen, as_of)`.

The analog basis itself (A, R, rate, age, the matched ad ids) goes in `factors` / a structured
`analog_basis` dict on the verdict, so the integration ticket can render "N analogs at age X started
this badly; R recovered (rate%)" and the reviewer can see why.

## Public surface (what the integration ticket calls)

```python
@dataclass(slots=True)
class EarlyTriageVerdict:
    verdict: str          # "not_struggling" | "abstain_keep" | "keep_watch" | "pause_candidate"
    age: int
    reasons: list[str]
    analog_basis: dict[str, Any]   # {"analogs": A, "recovered": R, "rate": .., "horizon": .., "matched_ids": [...]}
    confidence: dict[str, Any]     # confidence_to_dict(...)
    evidence: dict[str, Any]       # evidence_to_dict(...)

def triage_ad(
    *, ad_id: str, account_slug: str, as_of: date,
    histories: list[AdHistory], policy: dict[str, Any],
    roas_floor: float, roas_target: float,
    # plus the EARLY_LIFE_* knobs with config defaults
) -> EarlyTriageVerdict | None:   # None if ad not found / not early-life (age > max)
```

`triage_ad` takes `histories` (already fetched) so it stays pure; the provider fetch happens in the
integration ticket. Keep the goal-aware metric/floor selection consistent with
`actions._select_action_metric` and `monitor._policy_floors` — factor a shared helper if cleaner, but
do **not** duplicate threshold numbers.

## Config knobs (add to `config.py`, documented like the existing block)

- `EARLY_LIFE_MAX_AGE = 2`
- `EARLY_LIFE_DECISION_AGE = 2`
- `EARLY_LIFE_RECOVERY_HORIZON = 7`
- `EARLY_LIFE_MIN_ANALOGS = 3`
- `EARLY_LIFE_STRONG_ANALOGS = 6`
- `EARLY_LIFE_RECOVERY_RATE = 0.33`
- `ANALOG_RATIO_TOLERANCE = 0.5`

## Edge cases & interactions

- **Zero results at age N (the normal day-1 case):** ratio is undefined — must NOT divide by zero;
  fall to the spend-magnitude + zero-result match path. A struggling test that reads "0 results on
  non-trivial spend" must define "non-trivial" (reuse a fraction of `min_spend`, or any spend > 0 with
  a documented choice) so a $0.50 day-1 ad isn't force-graded.
- **No analogs at all** (new account / new creative direction): `A == 0 < min` → `abstain_keep`, never
  a confident kill. Covered by the verdict ladder; test it explicitly.
- **Survivorship bias:** verify `R/A` (population rate), not "any recovery". Add a test where 1 of 20
  analogs recovered → `pause_candidate`, and one where 4 of 6 recovered → `keep_watch`.
- **Analog too short-lived to judge recovery:** an analog that never reached the recovery horizon is
  excluded from `A` (neither recovered nor stayed-bad). Test that a population of paused-early ads does
  not masquerade as "stayed bad".
- **Triaged ad not in history / age > EARLY_LIFE_MAX_AGE:** `triage_ad` returns `None` (caller skips).
- **`as_of` before `first_seen`** (clock/data skew): age would be negative — clamp/guard and treat as
  age 0, or return None; pick one and test it.
- **Goal-aware:** ROAS path and install path must both be exercised; an install-goal account with no
  `target install cost` in policy must degrade gracefully (fall back to abstain rather than crash).
- **Confidence cap:** `analog_confidence` must never exceed `medium`, regardless of analog count
  (correlational ceiling). Pin with a test.
- **Determinism:** no `date.today()`, no `Math.random`; identical inputs → identical verdict. Sort
  matched ids for stable output.

## Key tests (mocks-only — a fake `HistoryProvider` / hand-built `AdHistory` lists; no live Meta)

- struggling day-1 ad, 5 zero-result analogs at age 0–1 of which 3 later cleared target → `keep_watch`,
  confidence `medium`, grounding `correlational`, evidence cites `first_seen..as_of`.
- same ad but 1 of 20 analogs recovered → `pause_candidate`, band ≤ `medium`, reasons name the rate.
- struggling ad, only 2 comparable analogs → `abstain_keep` (uses `abstain_confidence`).
- ad performing fine in early window → `not_struggling`, verdict short-circuits before analog work.
- install-goal account: cost-per-install struggling test + recovery against target install cost.
- analog older-but-not-recovered vs analog-too-short-to-judge produce different population counts.
- `analog_confidence` capped at medium even with 50 analogs all recovered.
- `age` computed purely from `as_of - first_seen`; passing a fixed `as_of` is fully deterministic.
- `DuckDBHistoryProvider` groups `fetch_run_rows` output into per-ad `AdHistory` (small fixture DB or
  monkeypatched `fetch_run_rows`); picks the latest `ingestion_run_date`.

## TODO

### Phase 1 — confidence factory
- [ ] Add `analog_confidence(...)` to `confidence.py` (correlational tier, data band from analog
      counts, capped at medium, all construction inside the module). Unit-test the cap + ladder.

### Phase 2 — config + types
- [ ] Add the `EARLY_LIFE_*` / `ANALOG_RATIO_TOLERANCE` constants to `config.py` with a documented
      comment block in the existing style.
- [ ] Add `AdDailyPoint`, `AdHistory`, `HistoryProvider` (Protocol), `EarlyTriageVerdict` to a new
      `src/meta_ads_analysis/early_triage.py`.

### Phase 3 — pure engine
- [ ] Implement age slicing (cumulative-through-age-N and after-age-N windows) on `AdHistory`.
- [ ] Implement the goal-aware struggling test (shared with / consistent with
      `actions._select_action_metric` + `monitor._policy_floors`; no duplicated numbers).
- [ ] Implement analog matching (primary ratio match + zero-result fallback, magnitude tolerance,
      exclusions) and recovery detection (target-clearing over the recovery window; exclude
      too-short-to-judge ads).
- [ ] Implement `triage_ad` returning `EarlyTriageVerdict` with the verdict ladder, evidence, and
      `analog_basis`.

### Phase 4 — provider
- [ ] Implement `DuckDBHistoryProvider` reading the latest ingestion run via `storage.fetch_run_rows`
      and grouping into `AdHistory` (all SQL stays here).

### Phase 5 — tests + checks
- [ ] Add the tests above to `tests/test_meta_ads_analysis.py`.
- [ ] Run the suite + type/lint checks; stream output with `tee`. Flag any pre-existing unrelated
      failure per the runner's `.pre-existing-error.md` protocol — do not chase it.
