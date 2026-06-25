"""Knowledge-vault provenance format + linter (pure: file text + regex, no Meta API, no clock).

`knowledge/learnings.md` is the trust anchor a new session reads first. This module makes the
provenance of every learning **machine-checkable** so a wrong guess can't quietly harden into a
"fact" that future sessions trust:

- ``parse_learnings(text)`` turns the markdown into structured :class:`LearningEntry` /
  :class:`EvidenceLine` records (also consumed by the dependent ``vault-audit`` drift pass).
- ``lint(entries, today=..., reverify_days=...)`` enforces the format and ages out *fast* facts.

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

from .config import KNOWLEDGE_REVERIFY_DAYS
from .confidence import BAND_PRESENTATION, Band, EvidenceTier

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
