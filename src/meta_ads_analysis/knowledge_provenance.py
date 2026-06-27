"""Knowledge-vault provenance format + linter (pure: file text + regex, no Meta API, no clock).

`knowledge/learnings.md` is the trust anchor a new session reads first. This module makes the
provenance of every learning **machine-checkable** so a wrong guess can't quietly harden into a
"fact" that future sessions trust:

- ``parse_learnings(text)`` turns the markdown into structured :class:`LearningEntry` /
  :class:`EvidenceLine` records (also consumed by the ``audit-vault`` drift pass below).
- ``lint(entries, today=..., reverify_days=...)`` enforces the format and ages out *fast* facts.
- The **audit engine** (``select_auditable`` → ``audit_claim`` → ``plan_edits`` →
  ``apply_entry_edits``) re-checks a stored ``metric:`` against a *fresh* live value and, when reality
  has drifted, **surfaces the contradiction loudly, lowers the band one level, and logs a dated
  ``➖``** — it never silently keeps a stale belief and **never deletes an entry** (a human decides
  deletion). This is the pure half; the Meta pull that feeds it ``FreshSample`` values lives in
  :mod:`cli` (the only module that touches the API). It is deliberately the same vocabulary as the
  live engine: drift verdicts decrement :class:`confidence.Band` (not a local emoji list) and the
  data floor that protects a true fact from a noisy week is :func:`confidence.data_strength`.

This is the knowledge-base counterpart to the live :mod:`confidence` engine, and the two speak
**ONE** vocabulary — the provenance ``src`` tiers ARE ``confidence.EvidenceTier`` (re-exported here,
never redefined) and the band emojis ARE ``confidence.BAND_PRESENTATION`` (pinned by a test). The
conceptual *observed / inferred / external* axis maps onto the tiers: observed =
{``direct_observation``, ``ab_experiment``}, inferred = {``correlational``, ``model_inference``},
external = ``external``.

Determinism, mirroring :mod:`confidence`: there is **no** ``datetime.now()`` inside the rubric —
``today`` is passed in, so staleness math is stable and matches the repo's no-clock test style.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .config import (
    CONFIDENCE_CONVERSIONS_FLOOR,
    KNOWLEDGE_DRIFT_PCT,
    KNOWLEDGE_REVERIFY_DAYS,
    MIN_WASTE_SPEND,
)
from .confidence import BAND_PRESENTATION, Band, EvidenceTier, data_strength

# ONE vocabulary — the provenance `src` tiers are exactly confidence.EvidenceTier. Do NOT fork a
# second list; a test pins this equality so the two modules can't drift into two scales.
TIER_NAMES: frozenset[str] = frozenset(t.name for t in EvidenceTier)

# Band emojis recognized in an entry header — derived from confidence.BAND_PRESENTATION (same scale,
# never a parallel set). ⚪ abstain is not a written-down learning band, so only the three real bands.
BAND_EMOJIS: frozenset[str] = frozenset(
    BAND_PRESENTATION[b]["emoji"] for b in (Band.high, Band.medium, Band.low)
)

# Volatility classes. `fast` ages out (account numbers / current platform UI state); `evergreen`
# never does (platform/API mechanics, durable strategy principles).
ROT_FAST = "fast"
ROT_EVERGREEN = "evergreen"
ROT_CLASSES: frozenset[str] = frozenset({ROT_FAST, ROT_EVERGREEN})

# --- line grammar -----------------------------------------------------------------------------

_ENTRY_RE = re.compile(r"^###\s+(?P<claim>.+?)\s*$")
_SECTION_RE = re.compile(r"^##\s+")
_CONFIDENCE_RE = re.compile(r"\*\*Confidence:\*\*")
_DOMAIN_RE = re.compile(r"\*\*Domain:\*\*\s*(?P<domain>[A-Za-z][A-Za-z ()/|]*?)\s*(?:·|$)")
_ROT_RE = re.compile(r"\*\*Rot:\*\*\s*(?P<rot>fast|evergreen)", re.IGNORECASE)
_VERIFIED_RE = re.compile(r"\*\*Verified:\*\*\s*(?P<date>\d{4}-\d{2}-\d{2})")

# An evidence line opens with a +/- glyph then a date; the em dash after the date is optional.
_EVIDENCE_START_RE = re.compile(
    r"^-\s*(?P<sign>➕|➖)\s*(?P<date>\d{4}-\d{2}-\d{2})\s*(?:—|-)?\s*(?P<rest>.*)$"
)

# A bold *field label* such as `**Apply:**` / `**Would raise / lower:**` — a colon-terminated bold
# span at the START of a line. Distinguishes a real header from inline emphasis (`**not**`,
# `**capped 🔴 Low**`) that can legitimately wrap onto a continuation line.
_FIELD_LABEL_RE = re.compile(r"^\*\*[^*]+:\*\*")

# The trailing structured tag: the LAST `_( … )_` on the (joined) line. Greedy to the final `)_`.
_TAG_RE = re.compile(r"_\((?P<tag>.+)\)_\s*$")

# Tag fields — searched anywhere inside the tag, tolerating `·`/`;`/`,` separators and extra prose.
_SRC_RE = re.compile(r"\bsrc:\s*(?P<src>[A-Za-z_]+)")
_ACCT_RE = re.compile(r"\bacct:\s*(?P<acct>[^·;,)\s]+)")
_METRIC_RE = re.compile(r"\bmetric:\s*(?P<name>[A-Za-z0-9_.]+)\s*=\s*(?P<value>[-+]?\d*\.?\d+)")

# A `select:` slice — comma-separated key=value pairs naming the exact breakdown cells this metric
# summarizes (so a two-dimension breakdown row resolves exactly instead of by name-token overlap).
# Capture runs to the next tag field (`·`), tag end (`)`), or a stray `;`/newline — NOT to the first
# space — so the commas *inside* the selector survive AND incidental whitespace (`a=b, c=d`) is
# tolerated; `_parse_evidence` strips each pair/key/value, so spacing never silently drops a pair.
_SELECT_RE = re.compile(r"\bselect:\s*(?P<select>[^·;)\n]+)")

# The inline reproduce-it command (`verify: account_metrics …`) and any URL, anywhere on the line.
_VERIFY_RE = re.compile(r"`?verify:\s*(?P<cmd>account_metrics[^`\n]*)`?")
_URL_RE = re.compile(r"https?://\S+")


@dataclass(slots=True)
class EvidenceLine:
    """One ``➕``/``➖`` evidence line with its parsed provenance tag."""

    sign: str  # "+" | "-"
    date: str  # YYYY-MM-DD
    text: str  # the evidence prose, tag stripped
    tier: str | None  # one of EvidenceTier names (None if the tag had no `src:`)
    account: str | None  # slug, "—" for account-agnostic, or None if absent
    metric_name: str | None
    metric_value: float | None
    verify_query: str | None  # the `account_metrics …` command that reproduces metric_value
    url: str | None
    lineno: int  # start line in the file
    has_tag: bool  # did the line carry a trailing `_( … )_` tag at all?
    # The breakdown slice this metric summarizes (`select: publisher_platform=instagram` → its
    # cells). None = no selector → audit-vault falls back to the name-token heuristic.
    metric_selector: dict[str, str] | None = None


@dataclass(slots=True)
class LearningEntry:
    """One ``### claim`` learning block with its header fields and evidence."""

    claim: str
    band_emoji: str | None  # 🟢/🟡/🔴
    domain: str | None
    rot: str | None  # "fast" | "evergreen"
    verified: str | None  # YYYY-MM-DD
    evidence: list[EvidenceLine]
    lineno: int  # start line of the `### claim` header — used by vault-audit for surgical edits


@dataclass(slots=True)
class Finding:
    """A single lint result. ``severity`` is ``"error"`` (fatal) or ``"warn"`` (informational)."""

    severity: str
    code: str
    message: str
    lineno: int
    claim: str | None = None


# --- parsing ----------------------------------------------------------------------------------


def _first_band_emoji(text: str) -> str | None:
    for ch in text:
        if ch in BAND_EMOJIS:
            return ch
    return None


def _is_continuation(line: str) -> bool:
    """True if ``line`` is a wrapped continuation of the evidence block above it: indented,
    non-empty, and not the start of a new bullet / header / bold field."""
    if not line.strip():
        return False
    if not (line.startswith(" ") or line.startswith("\t")):
        return False
    stripped = line.strip()
    if stripped.startswith(("- ", "###", "## ", "➕", "➖", "| ")):
        return False
    # A bold *field label* (`**Apply:**`) ends the block; inline emphasis (`**not**`) does not.
    if _FIELD_LABEL_RE.match(stripped):
        return False
    return True


def _parse_evidence(joined: str, lineno: int) -> EvidenceLine:
    """Parse one (already line-joined) evidence block into an :class:`EvidenceLine`."""
    m = _EVIDENCE_START_RE.match(joined)
    assert m is not None  # only called when the opener already matched
    sign = "+" if m.group("sign") == "➕" else "-"
    rest = m.group("rest")

    tag_m = _TAG_RE.search(rest)
    has_tag = tag_m is not None
    tag = tag_m.group("tag").strip() if tag_m else ""
    text = (rest[: tag_m.start()] if tag_m else rest).strip()

    tier = account = metric_name = None
    metric_value: float | None = None
    metric_selector: dict[str, str] | None = None
    if tag:
        if sm := _SRC_RE.search(tag):
            tier = sm.group("src")
        if am := _ACCT_RE.search(tag):
            account = am.group("acct")
        if mm := _METRIC_RE.search(tag):
            metric_name = mm.group("name")
            try:
                metric_value = float(mm.group("value"))
            except ValueError:
                metric_value = None
        if selm := _SELECT_RE.search(tag):
            pairs: dict[str, str] = {}
            for piece in selm.group("select").split(","):
                if "=" in piece:
                    k, v = (part.strip() for part in piece.split("=", 1))
                    if k and v:
                        pairs[k] = v
            metric_selector = pairs or None  # malformed / empty → None → token-heuristic fallback

    verify_query = vm.group("cmd").strip() if (vm := _VERIFY_RE.search(rest)) else None
    url = um.group(0).rstrip(".,);") if (um := _URL_RE.search(rest)) else None

    return EvidenceLine(
        sign=sign,
        date=m.group("date"),
        text=text,
        tier=tier,
        account=account,
        metric_name=metric_name,
        metric_value=metric_value,
        verify_query=verify_query,
        url=url,
        lineno=lineno,
        has_tag=has_tag,
        metric_selector=metric_selector,
    )


def parse_learnings(text: str) -> list[LearningEntry]:
    """Parse ``learnings.md`` text into structured :class:`LearningEntry` records.

    Only ``### claim`` blocks become entries; the prose intro, ``## section`` headers, the
    "Tooling capabilities" bullet list, and markdown tables are ignored. An evidence block may wrap
    across several physical lines — they are re-joined so the trailing ``_( … )_`` tag parses.
    """
    lines = text.splitlines()
    n = len(lines)
    entries: list[LearningEntry] = []
    current: LearningEntry | None = None
    i = 0
    while i < n:
        raw = lines[i]
        lineno = i + 1

        if m := _ENTRY_RE.match(raw):
            current = LearningEntry(
                claim=m.group("claim").strip(),
                band_emoji=None,
                domain=None,
                rot=None,
                verified=None,
                evidence=[],
                lineno=lineno,
            )
            entries.append(current)
            i += 1
            continue

        if current is None:
            i += 1
            continue

        # A new `## section` closes the current entry's scope.
        if _SECTION_RE.match(raw):
            current = None
            i += 1
            continue

        if _CONFIDENCE_RE.search(raw):
            current.band_emoji = _first_band_emoji(raw)
            if dm := _DOMAIN_RE.search(raw):
                current.domain = dm.group("domain").strip()
            i += 1
            continue

        if "**Rot:**" in raw or "**Verified:**" in raw:
            if (rm := _ROT_RE.search(raw)) and current.rot is None:
                current.rot = rm.group("rot").lower()
            if (vm := _VERIFIED_RE.search(raw)) and current.verified is None:
                current.verified = vm.group("date")
            i += 1
            continue

        if _EVIDENCE_START_RE.match(raw):
            parts = [raw]
            j = i + 1
            while j < n and _is_continuation(lines[j]):
                parts.append(lines[j])
                j += 1
            joined = " ".join(p.strip() for p in parts)
            current.evidence.append(_parse_evidence(joined, lineno))
            i = j
            continue

        i += 1

    return entries


# --- linting ----------------------------------------------------------------------------------


def _days_between(today: date, iso: str) -> int | None:
    try:
        return (today - date.fromisoformat(iso)).days
    except ValueError:
        return None


def _lint_evidence(ev: EvidenceLine, entry: LearningEntry, findings: list[Finding]) -> None:
    if not ev.has_tag:
        findings.append(
            Finding(
                "error",
                "missing_tag",
                f"evidence line ({ev.sign}{ev.date}) has no trailing _( … )_ provenance tag",
                ev.lineno,
                entry.claim,
            )
        )
        return

    if ev.tier is None:
        findings.append(
            Finding(
                "error",
                "missing_src",
                f"evidence tag ({ev.sign}{ev.date}) is missing a `src:` tier",
                ev.lineno,
                entry.claim,
            )
        )
    elif ev.tier not in TIER_NAMES:
        findings.append(
            Finding(
                "error",
                "invalid_src",
                f"invalid src tier {ev.tier!r}; expected one of {sorted(TIER_NAMES)}",
                ev.lineno,
                entry.claim,
            )
        )

    if ev.metric_name and not ev.verify_query:
        findings.append(
            Finding(
                "error",
                "metric_without_verify",
                f"metric: {ev.metric_name} is asserted but the line carries no "
                "`verify: account_metrics …` command (auditable ⇒ reproducible)",
                ev.lineno,
                entry.claim,
            )
        )

    # A metric sliced by ≥2 breakdowns with no `select:` is the exact class the audit's name-token
    # heuristic can't resolve (every IG cell matches `{instagram}` → ambiguous → could_not_audit).
    # Nudge (warn, never error — keeps pre-existing entries lint-clean); skip our own ➖ audit lines.
    if ev.metric_name and ev.metric_selector is None and not is_audit_line(ev):
        breakdowns = parse_verify_query(ev.verify_query)["breakdowns"]
        if len(breakdowns) >= 2:
            findings.append(
                Finding(
                    "warn",
                    "select_recommended",
                    f"metric: {ev.metric_name} is sliced by {len(breakdowns)} breakdowns "
                    "but carries no `select:` field; audit-vault cannot resolve it without one",
                    ev.lineno,
                    entry.claim,
                )
            )

    if ev.tier == EvidenceTier.external.name and not ev.url:
        findings.append(
            Finding(
                "error",
                "external_without_url",
                "src: external line carries no URL citation (external claims need a link)",
                ev.lineno,
                entry.claim,
            )
        )


def lint(
    entries: list[LearningEntry],
    *,
    today: date,
    reverify_days: int = KNOWLEDGE_REVERIFY_DAYS,
) -> list[Finding]:
    """Enforce the provenance format and age out stale *fast* facts.

    **error** (fatal — drives a nonzero CLI exit): a missing/invalid ``src`` tier, a missing
    ``Rot``/``Verified`` header, a ``metric:`` line with no ``verify:`` command, a ``src: external``
    line with no URL, or an evidence line with no tag at all. **warn** (informational): a ``fast``
    entry whose ``Verified`` date is older than ``reverify_days`` before ``today`` → ``⏳ re-verify``.
    ``evergreen`` entries are NEVER age-flagged.
    """
    findings: list[Finding] = []
    for entry in entries:
        if entry.rot not in ROT_CLASSES:
            findings.append(
                Finding(
                    "error",
                    "missing_rot",
                    f"entry has no valid **Rot:** (fast|evergreen); got {entry.rot!r}",
                    entry.lineno,
                    entry.claim,
                )
            )
        if not entry.verified:
            findings.append(
                Finding(
                    "error",
                    "missing_verified",
                    "entry has no **Verified:** date",
                    entry.lineno,
                    entry.claim,
                )
            )

        for ev in entry.evidence:
            _lint_evidence(ev, entry, findings)

        # Staleness — only `fast` entries age out; `evergreen` never does.
        if entry.rot == ROT_FAST and entry.verified:
            age = _days_between(today, entry.verified)
            if age is not None and age > reverify_days:
                findings.append(
                    Finding(
                        "warn",
                        "reverify",
                        f"⏳ re-verify: fast claim last verified {entry.verified} "
                        f"({age}d ago > {reverify_days}d)",
                        entry.lineno,
                        entry.claim,
                    )
                )

    return findings


def lint_profile_baseline(
    text: str,
    *,
    today: date,
    reverify_days: int = KNOWLEDGE_REVERIFY_DAYS,
    label: str = "profile.md",
) -> list[Finding]:
    """Light staleness check for a narrative ``profile.md``: find the single
    ``**Rot:** fast · **Verified:** YYYY-MM-DD`` baseline header and warn if a ``fast`` baseline is
    older than ``reverify_days``. ``profile.md`` is prose, not the evidence format — so this only
    age-flags that one header and never raises errors on its bullets.
    """
    findings: list[Finding] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if "**Verified:**" not in line or "**Rot:**" not in line:
            continue
        rot = rm.group("rot").lower() if (rm := _ROT_RE.search(line)) else None
        verified = vm.group("date") if (vm := _VERIFIED_RE.search(line)) else None
        if rot == ROT_FAST and verified:
            age = _days_between(today, verified)
            if age is not None and age > reverify_days:
                findings.append(
                    Finding(
                        "warn",
                        "reverify",
                        f"⏳ re-verify: {label} baseline last verified {verified} "
                        f"({age}d ago > {reverify_days}d)",
                        idx,
                        "Performance baseline",
                    )
                )
        break  # only the first/headline baseline header is in scope
    return findings


def render_report(
    findings: list[Finding],
    *,
    entries_count: int,
    strict: bool = False,
) -> tuple[str, int]:
    """Render a compact report (errors first, then ⏳ warnings) and the CLI exit code.

    Exit 1 on any error, or — when ``strict`` — on any warning too; else 0.
    """
    errors = [f for f in findings if f.severity == "error"]
    warns = [f for f in findings if f.severity == "warn"]
    out: list[str] = []
    for f in errors:
        claim = f" [{f.claim}]" if f.claim else ""
        out.append(f"ERROR L{f.lineno}{claim}: {f.message}")
    for f in warns:
        claim = f" [{f.claim}]" if f.claim else ""
        out.append(f"{f.message} (L{f.lineno}{claim})")
    out.append(
        f"lint-vault: {entries_count} entries · {len(errors)} error(s) · {len(warns)} warning(s)"
    )
    exit_code = 1 if (errors or (strict and warns)) else 0
    return "\n".join(out), exit_code


# --- audit (drift re-check) -------------------------------------------------------------------
#
# `lint-vault` enforces the *format* and ages out fast facts. `audit-vault` closes the loop: it
# re-pulls each `metric:` line's value over a FRESH trailing window and, when reality has drifted,
# lowers the band + logs a dated `➖`. The diff/verdict and the markdown mutation below are PURE
# (no Meta, no clock) so they unit-test with a fake metrics provider; cli.py owns the live pull.

# The verdict an audited claim earns. `refuted` is the strong form of `contradicted` (a policy
# threshold the claim sat on the other side of was crossed); both append a `➖` and lower the band,
# `refuted` drops to 🔴 Low + `(contested)`. The two abstain verdicts never touch the band — a noisy
# fresh window (`insufficient_fresh_data`) or a vanished entity/metric (`could_not_audit`) must not
# refute a real fact (AGENTS.md: "don't collapse missing into zeros").
AUDIT_CONFIRMED = "confirmed"
AUDIT_CONTRADICTED = "contradicted"
AUDIT_REFUTED = "refuted"
AUDIT_INSUFFICIENT = "insufficient_fresh_data"
AUDIT_COULD_NOT = "could_not_audit"

# A contradiction event lowers the band; the two abstain verdicts leave it untouched.
_AUDIT_DRIFTED: frozenset[str] = frozenset({AUDIT_CONTRADICTED, AUDIT_REFUTED})

# Reverse of BAND_PRESENTATION for the three written-down bands — emoji → Band. Derived from the
# single source so the audit's band decrement stays ONE vocabulary with the live engine.
_EMOJI_TO_BAND: dict[str, Band] = {
    BAND_PRESENTATION[b]["emoji"]: b for b in (Band.high, Band.medium, Band.low)
}

# The dated `➖` audit bullets all open with this marker; selection skips them (an audit line must
# not itself become an audit target) and idempotency keys off it.
_AUDIT_MARKER = "vault audit:"

# In-line band token on a `**Confidence:**` header, e.g. "🟢 High" — rewritten as a pair so the
# emoji and label never disagree (never "🟡 High").
_BAND_INLINE_RE = re.compile(r"(🟢|🟡|🔴)\s*(High|Medium|Low)")
_VERIFIED_LINE_RE = re.compile(r"(\*\*Verified:\*\*\s*)\d{4}-\d{2}-\d{2}")

# Pull the level / breakdown / window out of a stored `account_metrics …` verify command.
_VQ_LEVEL_RE = re.compile(r"--level\s+(?P<level>account|campaign|adset|ad)")
_VQ_BREAKDOWN_RE = re.compile(r"--breakdown\s+(?P<breakdown>[A-Za-z0-9_,]+)")
_VQ_FROM_RE = re.compile(r"--date-from\s+(?P<d>\d{4}-\d{2}-\d{2})")
_VQ_TO_RE = re.compile(r"--date-to\s+(?P<d>\d{4}-\d{2}-\d{2})")


@dataclass(slots=True)
class FreshSample:
    """The fresh live numbers a stored claim is re-checked against. Produced by cli.py's metrics
    provider (the only Meta-touching code) and consumed by the pure :func:`audit_claim`.

    ``value`` is the resolved fresh metric (e.g. ROAS) or ``None`` when it could not be resolved
    (entity vanished from the rows, named metric absent, or value missing for the window → audit
    abstains rather than scoring a fabricated 0). ``purchases``/``spend`` size the fresh window so
    :func:`confidence.data_strength` can decide whether the pull even clears the significance floor.
    ``window`` is the fresh trailing window actually pulled (``YYYY-MM-DD..YYYY-MM-DD``), surfaced in
    the report and the logged ``➖``."""

    value: float | None
    purchases: float | None
    spend: float | None
    window: str


@dataclass(slots=True)
class AuditOutcome:
    """One audited claim's verdict + the inputs behind it (so a reader can reproduce the call)."""

    entry: LearningEntry
    evidence: EvidenceLine
    verdict: str
    stored_value: float | None
    fresh: FreshSample
    crossed_threshold: str | None  # "target_roas"/"pause_roas_floor" when a boundary cross refuted
    new_band_emoji: str | None  # the band this entry would move to (None = unchanged)
    contested: bool  # refuted → mark the band (contested)
    factors: list[str]  # human-readable "why this verdict"


@dataclass(slots=True)
class EntryEdit:
    """A surgical, idempotent edit to ONE entry, keyed off its ``### header`` line number. The
    mutation only ever touches this entry's band emoji + ``Verified:`` line and inserts bullets into
    its evidence log — never the claim text, never another entry, never the whole file."""

    entry_lineno: int
    new_band_emoji: str | None
    contested: bool
    set_verified: str | None
    insert_bullets: list[str]


def is_audit_line(ev: EvidenceLine) -> bool:
    """True if ``ev`` is a ``➖`` line this audit wrote (opens with the ``vault audit:`` marker).
    Such lines carry their own ``metric:``+``verify:`` but must never be re-audited."""
    return ev.text.lstrip().lower().startswith(_AUDIT_MARKER)


def select_auditable(
    entries: list[LearningEntry], *, account_slug: str
) -> list[tuple[LearningEntry, EvidenceLine]]:
    """The (entry, evidence) pairs worth auditing for ``account_slug``: a data-backed, account-scoped
    claim. Skips ``evergreen`` entries (platform mechanics don't rot on numbers), lines with no
    ``metric:``/``verify:``, lines for other accounts, and this audit's own ``➖`` lines."""
    needle = f"--account {account_slug}"
    out: list[tuple[LearningEntry, EvidenceLine]] = []
    for entry in entries:
        if entry.rot == ROT_EVERGREEN:
            continue
        for ev in entry.evidence:
            if ev.metric_name is None or ev.metric_value is None or not ev.verify_query:
                continue
            if is_audit_line(ev):
                continue
            if needle not in ev.verify_query:
                continue
            out.append((entry, ev))
    return out


def parse_verify_query(cmd: str | None) -> dict[str, object]:
    """Extract ``level`` / ``breakdowns`` / ``date_from`` / ``date_to`` from a stored
    ``account_metrics …`` verify command (pure string parsing — no Meta). Missing parts come back
    ``None`` / ``[]`` so the caller can fall back to defaults."""
    cmd = cmd or ""
    level_m = _VQ_LEVEL_RE.search(cmd)
    bd_m = _VQ_BREAKDOWN_RE.search(cmd)
    from_m = _VQ_FROM_RE.search(cmd)
    to_m = _VQ_TO_RE.search(cmd)
    breakdowns = (
        [b for b in bd_m.group("breakdown").split(",") if b] if bd_m else []
    )
    return {
        "level": level_m.group("level") if level_m else None,
        "breakdowns": breakdowns,
        "date_from": from_m.group("d") if from_m else None,
        "date_to": to_m.group("d") if to_m else None,
    }


def _crosses(stored: float, fresh: float, threshold: float) -> bool:
    """True if ``stored`` and ``fresh`` sit on opposite sides of ``threshold`` (a decision flip)."""
    return (stored >= threshold) != (fresh >= threshold)


def classify_drift(
    *,
    stored_value: float | None,
    fresh: FreshSample,
    target_roas: float | None,
    pause_roas_floor: float | None,
    spend_floor: float = MIN_WASTE_SPEND,
    conversions_floor: float = CONFIDENCE_CONVERSIONS_FLOOR,
    drift_pct: float = KNOWLEDGE_DRIFT_PCT,
) -> tuple[str, str | None, list[str]]:
    """The pure verdict: compare a stored value to a fresh sample. Returns
    ``(verdict, crossed_threshold, factors)``.

    Order of guards (each protects a true fact from a false refutation):
    1. ``could_not_audit`` — the fresh value couldn't be resolved (``None``) or the stored value is
       absent/zero (no relative change is computable).
    2. ``insufficient_fresh_data`` — the fresh sample is below the significance floor
       (:func:`confidence.data_strength` abstains): a noisy week must not refute a real fact.
    3. ``refuted`` — the fresh value crossed ``target_roas`` or ``pause_roas_floor`` (a decision
       flip) — strong contradiction regardless of magnitude.
    4. ``contradicted`` — relative change ≥ ``drift_pct`` (the 25% band absorbs ROAS noise).
    5. ``confirmed`` — fresh ≈ stored.
    """
    if stored_value is None or stored_value == 0 or fresh.value is None:
        why = "fresh value could not be resolved" if (fresh.value is None) else (
            "stored value missing or zero — relative drift not computable"
        )
        return AUDIT_COULD_NOT, None, [why]

    data_band, data_factors = data_strength(
        sample_purchases=fresh.purchases,
        sample_spend=fresh.spend,
        spend_floor=spend_floor,
        conversions_floor=conversions_floor,
        recency_days=0,  # a fresh trailing window ends at --as-of by construction
    )
    if data_band == Band.abstain:
        return AUDIT_INSUFFICIENT, None, list(data_factors)

    for name, threshold in (("target_roas", target_roas), ("pause_roas_floor", pause_roas_floor)):
        if threshold is not None and _crosses(stored_value, fresh.value, threshold):
            return (
                AUDIT_REFUTED,
                name,
                [
                    f"fresh {fresh.value:.2f} crossed {name} ({threshold:g}) from stored "
                    f"{stored_value:.2f} — decision flip, refuted"
                ],
            )

    rel = abs(fresh.value - stored_value) / abs(stored_value)
    if rel >= drift_pct:
        return (
            AUDIT_CONTRADICTED,
            None,
            [f"fresh {fresh.value:.2f} vs stored {stored_value:.2f} — {rel:.1%} drift ≥ {drift_pct:.0%}"],
        )
    return (
        AUDIT_CONFIRMED,
        None,
        [f"fresh {fresh.value:.2f} ≈ stored {stored_value:.2f} — {rel:.1%} drift < {drift_pct:.0%}"],
    )


def lower_band_emoji(emoji: str | None) -> str | None:
    """One band step down (🟢→🟡→🔴, floored at 🔴) via :class:`confidence.Band` ordering — never a
    local emoji ladder. ``None`` / unrecognized emoji → ``None`` (leave the header alone)."""
    band = _EMOJI_TO_BAND.get(emoji or "")
    if band is None:
        return None
    lowered = Band(max(Band.low, Band(band - 1)))
    return BAND_PRESENTATION[lowered]["emoji"]


def audit_claim(
    entry: LearningEntry,
    evidence: EvidenceLine,
    fresh: FreshSample,
    *,
    target_roas: float | None,
    pause_roas_floor: float | None,
    spend_floor: float = MIN_WASTE_SPEND,
    conversions_floor: float = CONFIDENCE_CONVERSIONS_FLOOR,
    drift_pct: float = KNOWLEDGE_DRIFT_PCT,
) -> AuditOutcome:
    """Run :func:`classify_drift` for one claim and resolve the band move it implies. ``refuted`` →
    🔴 Low + ``(contested)``; ``contradicted`` → one level down; everything else leaves the band."""
    verdict, crossed, factors = classify_drift(
        stored_value=evidence.metric_value,
        fresh=fresh,
        target_roas=target_roas,
        pause_roas_floor=pause_roas_floor,
        spend_floor=spend_floor,
        conversions_floor=conversions_floor,
        drift_pct=drift_pct,
    )
    new_band_emoji: str | None = None
    contested = False
    if verdict == AUDIT_REFUTED:
        new_band_emoji = BAND_PRESENTATION[Band.low]["emoji"]
        contested = True
    elif verdict == AUDIT_CONTRADICTED:
        new_band_emoji = lower_band_emoji(entry.band_emoji)
    return AuditOutcome(
        entry=entry,
        evidence=evidence,
        verdict=verdict,
        stored_value=evidence.metric_value,
        fresh=fresh,
        crossed_threshold=crossed,
        new_band_emoji=new_band_emoji,
        contested=contested,
        factors=factors,
    )


def build_audit_bullet(
    outcome: AuditOutcome,
    *,
    as_of: str,
    account_slug: str,
    verify_query: str,
) -> str:
    """The dated ``➖`` evidence line to append for a drifted claim. It carries its own
    ``metric:``+``verify:`` (so it survives ``lint-vault``) and opens with the ``vault audit:`` marker
    (so it is never itself re-audited). ``verify_query`` should reproduce the *fresh* value."""
    metric = outcome.evidence.metric_name
    fresh = outcome.fresh.value
    stored = outcome.stored_value
    return (
        f"- ➖ {as_of} — {_AUDIT_MARKER} {metric} now {fresh:.2f} vs stored "
        f"{stored:.2f} over {outcome.fresh.window} `verify: {verify_query}` "
        f"_(src: {EvidenceTier.direct_observation.name} · acct: {account_slug} · "
        f"metric: {metric}={fresh:.2f})_"
    )


def plan_edits(
    outcomes: list[AuditOutcome],
    *,
    as_of: str,
    account_slug: str,
    fresh_verify_for: dict[int, str] | None = None,
) -> list[EntryEdit]:
    """Turn audit outcomes into per-entry surgical edits, idempotently.

    One :class:`EntryEdit` per entry (outcomes are grouped by ``entry.lineno``). For each entry:

    - **Idempotency:** an outcome whose dated ``vault audit:`` line for this ``as_of`` + ``metric``
      already exists in the entry is treated as already-applied — it contributes no new bullet and no
      band decrement, so re-running ``--apply`` on the same ``--as-of`` is a no-op.
    - **Band:** at most ONE step per run. A *new* refutation pins to 🔴 ``(contested)``; otherwise a
      *new* contradiction lowers one level. Abstain verdicts never move the band.
    - **Verified:** refreshed to ``as_of`` whenever the claim was actually checked (confirmed /
      contradicted / refuted) — this is how a ``lint-vault ⏳ re-verify`` flag clears. An abstain
      (insufficient / could-not-audit) does NOT refresh it (nothing was confirmed).

    ``fresh_verify_for`` maps an evidence ``lineno`` to the reproduce-the-fresh-value command for its
    ``➖`` bullet; a missing entry falls back to the stored ``verify_query``.
    """
    fresh_verify_for = fresh_verify_for or {}
    by_entry: dict[int, list[AuditOutcome]] = {}
    for o in outcomes:
        by_entry.setdefault(o.entry.lineno, []).append(o)

    edits: list[EntryEdit] = []
    for lineno, group in by_entry.items():
        entry = group[0].entry
        already: set[str] = {
            f"{e.date}|{e.metric_name}"
            for e in entry.evidence
            if is_audit_line(e) and e.metric_name is not None
        }

        new_band_emoji: str | None = None
        contested = False
        bullets: list[str] = []
        checked = False  # any non-abstain verdict → Verified is fair to refresh

        for o in group:
            if o.verdict in (AUDIT_CONFIRMED, *_AUDIT_DRIFTED):
                checked = True
            if o.verdict not in _AUDIT_DRIFTED:
                continue
            key = f"{as_of}|{o.evidence.metric_name}"
            if key in already:
                continue  # this exact audit already logged — don't double-count
            verify_query = fresh_verify_for.get(o.evidence.lineno) or (o.evidence.verify_query or "")
            bullets.append(
                build_audit_bullet(
                    o, as_of=as_of, account_slug=account_slug, verify_query=verify_query
                )
            )
            if o.verdict == AUDIT_REFUTED:
                new_band_emoji = BAND_PRESENTATION[Band.low]["emoji"]
                contested = True
            elif new_band_emoji is None and not contested:
                lowered = lower_band_emoji(entry.band_emoji)
                if lowered is not None:
                    new_band_emoji = lowered

        set_verified = as_of if checked else None
        if new_band_emoji is None and not contested and not bullets and set_verified is None:
            continue
        edits.append(
            EntryEdit(
                entry_lineno=lineno,
                new_band_emoji=new_band_emoji,
                contested=contested,
                set_verified=set_verified,
                insert_bullets=bullets,
            )
        )
    return edits


def _entry_span(lines: list[str], header_idx: int) -> int:
    """End (exclusive, 0-indexed) of the entry whose ``### header`` is at ``header_idx``: the next
    ``### ``/``## `` line, or EOF."""
    i = header_idx + 1
    n = len(lines)
    while i < n:
        if lines[i].startswith("### ") or lines[i].startswith("## "):
            break
        i += 1
    return i


def _apply_one_edit(lines: list[str], edit: EntryEdit) -> None:
    """Apply a single :class:`EntryEdit` to ``lines`` in place. Re-locates the band / Verified /
    evidence lines by scanning the entry's CURRENT span (never byte offsets) so concurrent edits
    elsewhere in the file aren't clobbered."""
    start = edit.entry_lineno - 1  # entry_lineno is 1-indexed at the `### header`
    if start < 0 or start >= len(lines) or not lines[start].startswith("### "):
        return  # entry moved/vanished under us — skip rather than corrupt
    end = _entry_span(lines, start)

    # Band emoji (+ optional contested marker) on the **Confidence:** line.
    if edit.new_band_emoji is not None or edit.contested:
        for i in range(start, end):
            if "**Confidence:**" not in lines[i]:
                continue
            line = lines[i]
            if edit.new_band_emoji is not None:
                band = _EMOJI_TO_BAND[edit.new_band_emoji]
                label = BAND_PRESENTATION[band]["label"]
                line = _BAND_INLINE_RE.sub(f"{edit.new_band_emoji} {label}", line, count=1)
            if edit.contested and "(contested)" not in line:
                line = _BAND_INLINE_RE.sub(r"\g<0> (contested)", line, count=1)
            lines[i] = line
            break

    # Verified date.
    if edit.set_verified is not None:
        for i in range(start, end):
            if "**Verified:**" in lines[i]:
                lines[i] = _VERIFIED_LINE_RE.sub(rf"\g<1>{edit.set_verified}", lines[i], count=1)
                break

    # Insert bullets after the last evidence block (before **Apply:** etc.).
    if edit.insert_bullets:
        last_ev = None
        for i in range(start, end):
            if _EVIDENCE_START_RE.match(lines[i]):
                last_ev = i
        if last_ev is None:
            insert_at = end  # no evidence log yet — append at the entry's end
        else:
            insert_at = last_ev + 1
            while insert_at < end and _is_continuation(lines[insert_at]):
                insert_at += 1
        lines[insert_at:insert_at] = edit.insert_bullets


def apply_entry_edits(text: str, edits: list[EntryEdit]) -> str:
    """Apply ``edits`` to ``text`` and return the new text. Edits run bottom-up (descending
    ``entry_lineno``) so an insertion never shifts the line numbers of a not-yet-edited entry above
    it. With no edits the input is returned byte-for-byte (report-only ⇒ zero file changes)."""
    if not edits:
        return text
    lines = text.splitlines()
    had_final_newline = text.endswith("\n")

    for edit in sorted(edits, key=lambda e: e.entry_lineno, reverse=True):
        _apply_one_edit(lines, edit)

    out = "\n".join(lines)
    if had_final_newline:
        out += "\n"
    return out


# Icons for the report. Drift is loud (⚠️); abstains are quiet; confirmed is a check.
_VERDICT_ICON = {
    AUDIT_CONFIRMED: "✅",
    AUDIT_CONTRADICTED: "⚠️",
    AUDIT_REFUTED: "⚠️",
    AUDIT_INSUFFICIENT: "•",
    AUDIT_COULD_NOT: "•",
}


def render_audit_report(
    outcomes: list[AuditOutcome], *, account_slug: str, as_of: str, apply_mode: bool
) -> tuple[str, dict[str, int]]:
    """Render the always-printed audit report and return ``(text, counts)``. Contradictions/
    refutations are called out loudly (⚠️); the summary line tallies every verdict."""
    counts = {
        AUDIT_CONFIRMED: 0,
        AUDIT_CONTRADICTED: 0,
        AUDIT_REFUTED: 0,
        AUDIT_INSUFFICIENT: 0,
        AUDIT_COULD_NOT: 0,
    }
    mode = "apply" if apply_mode else "report-only"
    out = [f"audit-vault {account_slug} — {len(outcomes)} auditable claim(s) as of {as_of} [{mode}]"]
    for o in outcomes:
        counts[o.verdict] = counts.get(o.verdict, 0) + 1
        icon = _VERDICT_ICON.get(o.verdict, "•")
        stored = "n/a" if o.stored_value is None else f"{o.stored_value:.2f}"
        fresh = "n/a" if o.fresh.value is None else f"{o.fresh.value:.2f}"
        loud = "  ⚠️ CONTRADICTION" if o.verdict in _AUDIT_DRIFTED else ""
        out.append(
            f"{icon} [{o.verdict}] {o.entry.claim}{loud}\n"
            f"      {o.evidence.metric_name}: stored {stored} vs fresh {fresh} "
            f"over {o.fresh.window or 'n/a'}"
        )
        for factor in o.factors:
            out.append(f"        - {factor}")
    out.append(
        "audit-vault: "
        f"{counts[AUDIT_CONFIRMED]} confirmed · "
        f"{counts[AUDIT_CONTRADICTED] + counts[AUDIT_REFUTED]} contradicted "
        f"({counts[AUDIT_REFUTED]} refuted) · "
        f"{counts[AUDIT_INSUFFICIENT]} insufficient-fresh-data · "
        f"{counts[AUDIT_COULD_NOT]} could-not-audit"
    )
    return "\n".join(out), counts
