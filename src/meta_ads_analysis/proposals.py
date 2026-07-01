"""Proposal store + execute orchestration for the MCP guarded-write flow (pure library — no socket).

The custom MCP server (:mod:`meta_ads_analysis.mcp_server`) exposes ``propose_* → execute_plan`` write
tools. This module is the **shared machinery** those tools stand on, deliberately kept out of the server
entrypoint so it is unit-testable with no ``mcp`` SDK and no live Meta call:

- a **proposal store** — ``save_proposal`` persists a built+reviewed plan and returns a ``plan_id``
  *reference*; ``load_proposal`` resolves that id back to the persisted artifact. The agent is handed
  the id, never an approvable plan body — the anti-forgery seam :func:`execute_plan` relies on.
- an **approval seam** — :class:`ApprovalGate` (Protocol) with three implementations. The shipped local
  default (wired by ``mcp_server.build_server`` via :func:`select_approval_gate_from_env`) is
  :class:`HmacApprovalGate`: it verifies an HMAC-SHA256 signature over the plan's approved content,
  keyed by a secret (``META_APPROVAL_SECRET``) the agent's MCP tool surface never holds and produced
  out-of-band by the human-run ``approve_plan`` CLI (:func:`approve_proposal`). The agent may freely edit
  the persisted proposal JSON, but any edit to the approved set changes the recompute and fails the
  constant-time compare, and it cannot forge a matching signature without the secret. With **no** secret
  configured the seam fails **closed** via :class:`DeniedApprovalGate` (execute refused; reads
  unaffected) — the opposite of the old forgeable-open behavior. :class:`PlanStatusApprovalGate` stays as
  an explicit no-op used by the execute tests (which supply their own already-approved plans). Local
  limitation (accepted tradeoff): an actor that can read the server's environment or secret file could
  still forge — moving approval state server-side behind Entra ID is the ``mcp-role-based-access-tiers``
  backlog ticket, which drops in behind this same seam without touching :func:`execute_plan`.
- :func:`execute_plan` — the only entry point that writes. It loads by id (never a caller body),
  refuses a re-execute, consults the gate, then runs a **validate_only pass first** (real round-trip,
  nothing persisted); only if every approved op validates does it run the **execute pass**, write the
  audit artifact, and re-read each touched entity to verify the outcome landed.
- :func:`preview_plan` — a local, write-free dry run of what each approved op *would* send.

The dispatch map :data:`PLAN_APPLIERS` wires all four executable plan families — ``"ops"`` (control),
``"authoring"`` (PAUSED-by-default creates), ``"audience_rotation"``, and ``"advantage_disable"`` — each
to the existing library applier for its ``plan_type``. Two facts make the map correct rather than a bag
of lambdas: (1) the **reader-kwarg split** — ``apply_authoring_plan`` reads nothing at apply time (it
POSTs creates), so the authoring wrapper drops the ``reader`` kwarg, while ops/rotation appliers keep it
for their live re-reads; (2) the **items-key split** — a rotation plan carries **no** ``plan["ops"]``
(its approvable items live under ``"rotations"`` / ``"items"``), so :data:`PLAN_ITEMS_KEY` tells the
approval count and result serialization where to look. A ``plan_type`` that does not match its builder's
stamped type would silently apply/approve nothing — the map keys ARE those stamped types.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import authoring, control, rotation
from .config import DEFAULT_REPORTS_ROOT
from .meta_api import MetaApiError, client_from_env
from .reader_provider import MetaReaderProvider, as_reader
from .utils import ensure_dir, write_json

# Marker key stored on a persisted proposal once its execute pass has run. Its presence is the
# idempotency guard — a second execute_plan on the same id refuses (Meta writes are not transactional,
# so re-applying could double a budget change or re-toggle a status).
EXECUTION_KEY = "execution"


# --- Approval seam ----------------------------------------------------------


class ApprovalError(RuntimeError):
    """Raised by an :class:`ApprovalGate` when a plan is not approved for execution."""


@runtime_checkable
class ApprovalGate(Protocol):
    """The seam :func:`execute_plan` consults before applying. Mirrors the reader-provider seam so the
    ``mcp-local-approval-gate`` ticket can drop in an un-forgeable approver without touching execute."""

    def assert_approved(self, plan_id: str, plan: dict[str, Any]) -> None:
        """Raise :class:`ApprovalError` if ``plan`` is not approved for execution; return otherwise."""
        ...


class PlanStatusApprovalGate:
    """Default gate shipped this ticket: a **no-op** that relies entirely on the ``apply_*_plan``
    invariant that only ``status=="approved"`` ops are ever sent.

    Since no MCP tool in this ticket promotes an op to ``approved``, a freshly-proposed plan has zero
    approved ops and :func:`execute_plan` refuses before any write. This gate is **forgeable** by a
    local filesystem-write agent (it could hand-edit ``status`` in the persisted proposal); the
    ``mcp-local-approval-gate`` ticket replaces it, behind this same seam, with an un-forgeable source.
    """

    def assert_approved(self, plan_id: str, plan: dict[str, Any]) -> None:  # noqa: D401 - see class doc
        return None


# --- HMAC-signed approval (the shipped local, single-operator gate) ---------

# The plan key holding the human-produced approval block ({approved_at, signature, ...}). NEVER part of
# the signed payload (``plan_items`` reads neither this nor ``execution``), so the signature is not
# self-referential.
APPROVAL_KEY = "approval"

# Secret resolution + TTL env, mirroring the reader-provider ``*_from_env`` selection seam. The secret
# lives in the operator's approve-CLI shell AND the MCP server process; it is never returned by a tool,
# written into an artifact, or reachable through the agent's tool surface.
APPROVAL_SECRET_ENV = "META_APPROVAL_SECRET"
APPROVAL_SECRET_FILE_ENV = "META_APPROVAL_SECRET_FILE"
APPROVAL_TTL_ENV = "META_APPROVAL_TTL_SECONDS"
# A too-guessable secret is a misconfig, not a default — reject it loudly rather than pretend to protect.
MIN_APPROVAL_SECRET_LEN = 16
# Approvals age out: an approved plan left unexecuted for a day is stale (the account may have moved on).
DEFAULT_APPROVAL_TTL_SECONDS = 86400  # 24h; override via META_APPROVAL_TTL_SECONDS (empty/0 disables).

APPROVAL_ALGORITHM = "HMAC-SHA256"


def canonical_approval_payload(plan: dict[str, Any], approved_at: str) -> str:
    """Deterministic JSON over the plan's **approved** content — the single canonicalization used by
    BOTH the signer (:func:`approve_proposal`) and the verifier (:class:`HmacApprovalGate`). If the two
    sides canonicalized differently, every real approval would fail verification; keeping it in one
    function is the parity contract (mirroring the repo's CBO / review re-derivation parity patterns).

    Round-trip-stable: signing the in-memory plan and verifying the JSON-reloaded plan produce
    byte-identical payloads (all values originate from JSON, so ``sort_keys`` + compact separators +
    ``ensure_ascii`` are enough — no ``default=`` coercion needed).

    - Uses :func:`plan_items` (the plan-type-aware accessor), so it binds ops **and** rotation items — a
      rotation plan's approved items live under ``"rotations"`` / ``"items"``, not ``"ops"``.
    - Signs the **full approved item dicts**, so mutating any material field of an approved op (params,
      target ``id``, ``level``) — or stripping its ``confidence``/``evidence`` to dodge the separate
      write-grounding gate — changes the signature and is rejected.
    - ``plan_id`` binds the signature to this specific plan (blocks copying an ``approval`` block from
      plan A onto plan B — B's payload carries B's id, A's signature was over A's).
    - ``approved_at`` is inside the signed payload, so the agent cannot forward-date it to defeat the TTL.
    """
    approved = [it for it in plan_items(plan) if it.get("status") == control.APPROVED_STATUS]
    doc = {
        "plan_id": plan.get("plan_id"),
        "plan_type": plan.get("plan_type"),
        "approved_at": approved_at,
        "approved_items": approved,
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_approval_signature(plan: dict[str, Any], approved_at: str, secret: bytes) -> str:
    """HMAC-SHA256 hex digest over :func:`canonical_approval_payload`, keyed by ``secret``."""
    payload = canonical_approval_payload(plan, approved_at).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


class HmacApprovalGate:
    """Un-forgeable local approval. Verifies ``plan['approval'].signature`` against a fresh recompute of
    the HMAC over the plan's **current** approved items, keyed by a secret the agent never holds; the
    compare is constant-time.

    Failure modes it closes:

    - a freshly-proposed plan (no ``approval`` block) → "no human approval" :class:`ApprovalError`;
    - an agent that flips a status to ``approved`` but can't sign → recompute over the new approved set
      ≠ stored signature → mismatch;
    - a mutated / added / removed approved op, or a stripped ``confidence``/``evidence`` → mismatch;
    - a cross-plan replay (an ``approval`` block copied onto another plan) → ``plan_id`` differs → mismatch;
    - an approval older than the TTL → expired.

    Local limitation (accepted tradeoff, not a bug): on a single-user box an actor that can read the
    server process's environment or the secret file could forge. Moving approval state server-side behind
    Entra ID is exactly what the ``mcp-role-based-access-tiers`` backlog ticket does — dropping in behind
    this same :class:`ApprovalGate` seam without touching :func:`execute_plan`.
    """

    def __init__(
        self,
        secret: bytes,
        *,
        ttl_seconds: int | None = DEFAULT_APPROVAL_TTL_SECONDS,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if not secret:
            raise ValueError("HmacApprovalGate requires a non-empty secret.")
        self._secret = secret
        self._ttl_seconds = ttl_seconds
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    def assert_approved(self, plan_id: str, plan: dict[str, Any]) -> None:
        approval = plan.get(APPROVAL_KEY)
        if not isinstance(approval, dict) or not approval.get("signature"):
            raise ApprovalError(
                "no human approval on this plan — approve it out-of-band with "
                "`approve_plan --plan-id <id>` before executing."
            )
        approved_at = str(approval.get("approved_at") or "")
        expected = compute_approval_signature(plan, approved_at, self._secret)
        if not hmac.compare_digest(expected, str(approval.get("signature"))):
            raise ApprovalError(
                "approval signature does not match the plan's approved ops — the plan was modified after "
                "approval, or the approval is forged. Re-approve with `approve_plan`."
            )
        if self._ttl_seconds:  # 0 / None disables expiry
            age = self._approval_age_seconds(approved_at)
            if age is None:
                raise ApprovalError(
                    "approval timestamp is missing or unparseable — re-approve with `approve_plan`."
                )
            if age > self._ttl_seconds:
                raise ApprovalError(
                    f"approval expired ({int(age)}s old > {self._ttl_seconds}s TTL) — re-approve with "
                    "`approve_plan`."
                )

    def _approval_age_seconds(self, approved_at: str) -> float | None:
        try:
            ts = datetime.fromisoformat(approved_at)
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return (self._now_fn() - ts).total_seconds()


class DeniedApprovalGate:
    """Fail-**closed** gate selected when no approval secret is configured: every ``assert_approved``
    raises :class:`ApprovalError` naming :data:`APPROVAL_SECRET_ENV`. The deliberate opposite of the old
    forgeable-open default — with no secret, no write ever executes — while reads (never gated) keep
    working. Set ``META_APPROVAL_SECRET`` to switch to :class:`HmacApprovalGate`."""

    def assert_approved(self, plan_id: str, plan: dict[str, Any]) -> None:
        raise ApprovalError(
            f"approval is not configured: set {APPROVAL_SECRET_ENV} (a shared secret, "
            f">= {MIN_APPROVAL_SECRET_LEN} bytes) in the MCP server environment and approve plans "
            "out-of-band with `approve_plan`. Reads work without it; execute is refused until it is set "
            "(fail-closed)."
        )


def approval_secret_from_env() -> bytes | None:
    """Resolve the raw approval secret: :data:`APPROVAL_SECRET_ENV` first, else the file at
    :data:`APPROVAL_SECRET_FILE_ENV` (bytes, trailing newline stripped). ``None`` if neither is set.

    Raises a clear :class:`ValueError` if a configured secret is shorter than
    :data:`MIN_APPROVAL_SECRET_LEN` — a too-guessable secret is a misconfiguration, not a silent default.
    """
    raw = os.environ.get(APPROVAL_SECRET_ENV)
    secret: bytes | None = None
    if raw:
        secret = raw.encode("utf-8")
    else:
        file_path = os.environ.get(APPROVAL_SECRET_FILE_ENV)
        if file_path:
            try:
                secret = Path(file_path).read_bytes().rstrip(b"\r\n")
            except OSError as exc:
                raise ValueError(
                    f"{APPROVAL_SECRET_FILE_ENV}={file_path!r} could not be read: {exc}"
                ) from exc
    if secret is None:
        return None
    if len(secret) < MIN_APPROVAL_SECRET_LEN:
        raise ValueError(
            f"The configured approval secret is only {len(secret)} bytes; it must be at least "
            f"{MIN_APPROVAL_SECRET_LEN}. Generate one with "
            '`python -c "import secrets; print(secrets.token_hex(32))"`.'
        )
    return secret


def approval_ttl_from_env() -> int | None:
    """Approval TTL in seconds from :data:`APPROVAL_TTL_ENV`; :data:`DEFAULT_APPROVAL_TTL_SECONDS` when
    unset, and ``None`` (expiry disabled) for an empty value or ``0``. A non-integer is a clear error."""
    raw = os.environ.get(APPROVAL_TTL_ENV)
    if raw is None:
        return DEFAULT_APPROVAL_TTL_SECONDS
    raw = raw.strip()
    if raw in ("", "0"):
        return None
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{APPROVAL_TTL_ENV}={raw!r} must be an integer number of seconds (empty or 0 disables expiry)."
        ) from exc
    if seconds < 0:
        raise ValueError(f"{APPROVAL_TTL_ENV} must not be negative (got {seconds}).")
    return seconds or None


def select_approval_gate_from_env() -> ApprovalGate:
    """The single selection point for ``build_server``. A configured secret → :class:`HmacApprovalGate`
    (with the env TTL); no secret → :class:`DeniedApprovalGate` (fail-closed — execute refuses with setup
    guidance, reads still work)."""
    secret = approval_secret_from_env()
    if secret is None:
        return DeniedApprovalGate()
    return HmacApprovalGate(secret, ttl_seconds=approval_ttl_from_env())


# --- Proposal store ---------------------------------------------------------


def proposals_dir(
    account_slug: str | None, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT
) -> Path:
    """The proposals directory for an account/run-date: ``<reports_root>/<slug>/<run_date>/proposals``."""
    return Path(reports_root) / (account_slug or "account") / run_date / "proposals"


def _sanitize(text: str) -> str:
    """Filesystem-safe token for a ``plan_id`` (keep alnum / ``-`` / ``_``; collapse the rest to ``-``)."""
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(text)) or "x"


def save_proposal(
    plan: dict[str, Any],
    *,
    account_slug: str | None,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> str:
    """Persist ``plan`` under the proposals tree and return a unique ``plan_id`` **reference**.

    The id is ``f"{plan_type}-{intent}-{account_slug}-<UTC-timestamp>"`` (a collision appends a
    ``-<n>`` suffix). The plan is written verbatim with its own ``plan_id`` embedded, so
    :func:`load_proposal` round-trips it. The agent is handed only the id — never the approvable body.
    """
    plan_type = _sanitize(plan.get("plan_type") or "plan")
    intent = _sanitize(plan.get("intent") or "op")
    slug = _sanitize(account_slug or "account")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{plan_type}-{intent}-{slug}-{stamp}"
    directory = proposals_dir(account_slug, run_date, reports_root)
    ensure_dir(directory)
    plan_id = base
    suffix = 0
    while (directory / f"{plan_id}.json").exists():
        suffix += 1
        plan_id = f"{base}-{suffix}"
    stored = dict(plan)
    stored["plan_id"] = plan_id
    write_json(directory / f"{plan_id}.json", stored)
    return plan_id


def find_proposal_path(plan_id: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    """Resolve a ``plan_id`` to its persisted path under any ``*/*/proposals`` tree.

    Raises :class:`MetaApiError` (an operator-actionable error the server maps to a clean tool error)
    on a missing or ambiguous id, so a bad/forged/expired id never silently no-ops.
    """
    if not str(plan_id or "").strip():
        raise MetaApiError("plan_id is required.")
    root = Path(reports_root)
    matches = sorted(root.glob(f"*/*/proposals/{plan_id}.json"))
    if not matches:
        raise MetaApiError(
            f"No proposal found for plan_id {plan_id!r} under {root}/. Propose it first, then execute "
            "by the returned plan_id."
        )
    if len(matches) > 1:
        raise MetaApiError(f"Ambiguous plan_id {plan_id!r}: {len(matches)} proposals match.")
    return matches[0]


def load_proposal(plan_id: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> dict[str, Any]:
    """Load a persisted proposal by id. Raises a clear :class:`MetaApiError` if missing/unreadable."""
    path = find_proposal_path(plan_id, reports_root)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MetaApiError(f"Could not read proposal {plan_id!r} at {path}: {exc}") from exc


# --- Execute orchestration --------------------------------------------------


def _apply_ops(plan, client, *, execute, validate_only, reader):
    """Ops-plan applier: passes the reader through for the live re-reads control's ops path needs."""
    return control.apply_ops_plan(
        plan, client, execute=execute, validate_only=validate_only, reader=reader
    )


def _apply_authoring(plan, client, *, execute, validate_only, reader=None):
    """Authoring-plan applier. ``apply_authoring_plan`` reads nothing live at apply time (it POSTs
    creates), so it has **no** ``reader`` parameter — the accepted-and-dropped ``reader`` kwarg keeps a
    uniform applier signature without passing an argument the callee would reject with a ``TypeError``.
    Created entities are forced PAUSED inside ``authoring._build_create``, independent of this wiring."""
    return authoring.apply_authoring_plan(
        plan, client, execute=execute, validate_only=validate_only
    )


def _apply_rotation(plan, client, *, execute, validate_only, reader):
    """Audience-rotation applier: keeps ``reader`` for the pre-write live-targeting drift re-read."""
    return rotation.apply_rotation_plan(
        plan, client, execute=execute, validate_only=validate_only, reader=reader
    )


def _apply_advantage_disable(plan, client, *, execute, validate_only, reader):
    """Advantage-Audience-disable applier: keeps ``reader`` for the pre-write live re-read."""
    return rotation.apply_advantage_disable_plan(
        plan, client, execute=execute, validate_only=validate_only, reader=reader
    )


# plan_type -> applier. The wrapper absorbs the reader-kwarg split: ``apply_ops_plan`` /
# ``apply_rotation_plan`` / ``apply_advantage_disable_plan`` take a ``reader=`` kwarg for their live
# re-reads; ``apply_authoring_plan`` does not (authoring POSTs creates and reads nothing at apply time),
# so ``_apply_authoring`` accepts-and-drops the kwarg. Each key MUST equal the ``plan_type`` its builder
# stamps (``control`` → ``"ops"``, ``authoring`` → ``"authoring"``, rotation builders → their two keys),
# or execute_plan would find no applier and raise. (The parity check in the tests guards this.)
PLAN_APPLIERS: dict[str, Any] = {
    "ops": _apply_ops,
    "authoring": _apply_authoring,
    "audience_rotation": _apply_rotation,
    "advantage_disable": _apply_advantage_disable,
}

# plan_type -> the plan key holding its approvable items. Rotation plans carry NO ``plan["ops"]`` — their
# reviewable items live under their own keys — so the approval count and result serialization consult
# this map rather than assuming ``"ops"``. Routing a rotation plan through the ``ops`` key would count
# zero approved items and refuse a genuinely-approved plan (the #1 rotation failure mode).
PLAN_ITEMS_KEY: dict[str, str] = {
    "ops": "ops",
    "authoring": "ops",
    "audience_rotation": "rotations",
    "advantage_disable": "items",
}


def plan_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The list of approvable/reviewable items for ``plan``, keyed by its ``plan_type`` (defaulting to
    ``plan["ops"]``). Shared by the approval count, the proposal summary, and result serialization so
    all three agree on where a plan's items live."""
    key = PLAN_ITEMS_KEY.get(str(plan.get("plan_type")), "ops")
    return [it for it in (plan.get(key) or []) if isinstance(it, dict)]

# Substrings in a Meta error that signal a missing write scope (a read-only token). Kept in one place
# so both the validate-pass surfacing and any future pre-check map identically.
_SCOPE_ERROR_MARKERS = ("ads_management", "(#200)", "(#10)", "#10)", "permission")

SCOPE_ERROR_MESSAGE = (
    "The configured META_ACCESS_TOKEN lacks ads_management (writes need it; it looks read-only). "
    "Reads work; set an ads_management-scoped token to execute."
)


def _looks_like_scope_error(reason: str | None) -> bool:
    text = str(reason or "").lower()
    return any(marker.lower() in text for marker in _SCOPE_ERROR_MARKERS)


def scope_error_from_results(results: list) -> str | None:
    """Return :data:`SCOPE_ERROR_MESSAGE` if any validate/execute result reason signals a missing write
    scope, else ``None``. The mandatory validate pass doubles as the scope pre-flight (a read-only token
    fails ``validate_only`` with a Meta permissions error) — no extra ``/debug_token`` call needed."""
    for r in results:
        if getattr(r, "status", None) in ("validation_failed", "failed", "blocked") and _looks_like_scope_error(
            getattr(r, "reason", None)
        ):
            return SCOPE_ERROR_MESSAGE
    return None


def _result_to_dict(r) -> dict[str, Any]:
    return {
        "op_id": r.op_id,
        "status": r.status,
        "request": r.request,
        "response": r.response,
        "reason": r.reason,
    }


def _authoring_result_to_dict(r) -> dict[str, Any]:
    """Serialize an ``authoring.AuthoringResult`` (has ``kind`` + ``created_id``, no ``level``/``id``)."""
    return {
        "op_id": r.op_id,
        "kind": r.kind,
        "status": r.status,
        "created_id": r.created_id,
        "request": r.request,
        "response": r.response,
        "reason": r.reason,
    }


def _rotation_result_to_dict(r) -> dict[str, Any]:
    """Serialize a ``rotation.RotationResult`` (keyed by ``adset_id``, carries ``targeting``, no
    ``op_id``/``request`` — so the ops serializer would ``AttributeError`` on it)."""
    return {
        "adset_id": r.adset_id,
        "status": r.status,
        "targeting": r.targeting,
        "response": r.response,
        "reason": r.reason,
    }


def _serialize_results(
    plan: dict[str, Any], exec_results: list, verifications: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Serialize execute-pass results to plain dicts, plan-type-aware. Each result type carries a
    different attribute set (``OpResult``/``AuthoringResult`` key by ``op_id``; ``RotationResult`` keys
    by ``adset_id``), so both the serializer and the verification key are chosen by ``plan_type``. A
    matching outcome verification (built by :func:`_verify_outcomes`) is attached under ``verify``."""
    plan_type = str(plan.get("plan_type"))
    out: list[dict[str, Any]] = []
    for r in exec_results:
        if plan_type == "authoring":
            entry = _authoring_result_to_dict(r)
            key = str(r.op_id)
        elif plan_type in ("audience_rotation", "advantage_disable"):
            entry = _rotation_result_to_dict(r)
            key = str(r.adset_id)
        else:
            entry = _result_to_dict(r)
            key = str(r.op_id)
        if key in verifications:
            entry["verify"] = verifications[key]
        out.append(entry)
    return out


# Fields re-read to confirm a write's outcome. ``effective_status`` is the honest signal (``status`` is
# what we set; ``effective_status`` is what Meta actually reports after processing).
_VERIFY_FIELDS = ["id", "status", "effective_status"]

# authoring create kind -> the entity level to re-read for the created-then-verify PAUSED check.
# ``create_lookalike`` is absent on purpose: an audience has no status/effective_status (inert, never in
# ``authoring.PAUSED_KINDS``), so there is nothing to read back and no PAUSED to assert.
_AUTHORING_VERIFY_LEVEL: dict[str, str] = {
    "create_campaign": "campaign",
    "create_adset": "adset",
    "create_ad": "ad",
    "create_video_ad": "ad",
}


def _verify_outcomes(
    plan: dict[str, Any], exec_results: list, reader: MetaReaderProvider
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Re-read each executed entity's live state, dispatched by ``plan_type``. Each family verifies a
    different thing (ops → status landed + next-day-spend follow-up; authoring → created-and-PAUSED;
    rotation → the targeting write registered), and the result objects differ in shape, so a single
    ops-shaped loop would ``AttributeError`` on a rotation result. A re-read failure is always recorded
    (never raised) — the writes already landed and a failed *confirmation* read must not mask that."""
    plan_type = str(plan.get("plan_type"))
    if plan_type == "authoring":
        return _verify_authoring_outcomes(exec_results, reader)
    if plan_type in ("audience_rotation", "advantage_disable"):
        return _verify_rotation_outcomes(exec_results, reader)
    return _verify_ops_outcomes(plan, exec_results, reader)


def _verify_ops_outcomes(
    plan: dict[str, Any], exec_results: list, reader: MetaReaderProvider
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Ops outcome verify. Carries the pausing lesson: a ``set_status``→PAUSED that registered is
    necessary but NOT sufficient proof delivery stopped — same-day spend can still post. Each such op
    emits a structured ``verify_next_day_spend`` follow-up marker. A re-read failure is recorded per op
    (never raised — the writes already landed; a failed confirmation read must not mask that)."""
    ops_by_id = {
        str(op.get("op_id")): op for op in (plan.get("ops") or []) if isinstance(op, dict)
    }
    verifications: dict[str, dict[str, Any]] = {}
    follow_ups: list[dict[str, Any]] = []
    for r in exec_results:
        if r.status != control.EXECUTED_STATUS:
            continue
        op = ops_by_id.get(str(r.op_id)) or {}
        level = str(op.get("level") or "")
        node_id = str(op.get("id") or "")
        if not level or not node_id:
            continue
        try:
            live = control._get_entity(reader, level, node_id, _VERIFY_FIELDS)
            verifications[str(r.op_id)] = {
                "effective_status": live.get("effective_status"),
                "status": live.get("status"),
            }
        except MetaApiError as exc:
            verifications[str(r.op_id)] = {"verify_error": str(exc)}
        # PAUSED registering != delivery stopped: same-day spend can still post. Flag a next-day check.
        if op.get("op") == "set_status" and str((op.get("params") or {}).get("status") or "").upper() == "PAUSED":
            follow_ups.append(
                {
                    "type": "verify_next_day_spend",
                    "level": level,
                    "id": node_id,
                    "op_id": str(r.op_id),
                    "reason": (
                        "PAUSED write registered, but same-day spend cannot be confirmed $0 — verify "
                        "next-day spend = $0 to prove delivery actually stopped."
                    ),
                }
            )
    return verifications, follow_ups


def _verify_authoring_outcomes(
    exec_results: list, reader: MetaReaderProvider
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Create-then-verify: re-read each created entity's ``effective_status`` and confirm it is NOT
    ACTIVE. ``authoring._build_create`` forces PAUSED on every spending create, so a created entity that
    comes back ACTIVE is a red flag (a create that silently spends) — surfaced both in the per-op
    ``verify`` block and as a ``created_active`` follow-up marker. A created **lookalike** has no status
    (inert audience, not in ``_AUTHORING_VERIFY_LEVEL``) so it is skipped — asserting PAUSED on it would
    be wrong. The created id comes from the store's recorded ``created_id`` (not the op body)."""
    verifications: dict[str, dict[str, Any]] = {}
    follow_ups: list[dict[str, Any]] = []
    for r in exec_results:
        if r.status != authoring.CREATED_STATUS:  # only entities we actually created
            continue
        created_id = str(getattr(r, "created_id", "") or "")
        kind = str(getattr(r, "kind", "") or "")
        level = _AUTHORING_VERIFY_LEVEL.get(kind)
        if not created_id or level is None:
            continue  # inert audience (lookalike) or no id returned — nothing to read back
        try:
            live = control._get_entity(reader, level, created_id, _VERIFY_FIELDS)
        except MetaApiError as exc:
            verifications[str(r.op_id)] = {"created_id": created_id, "verify_error": str(exc)}
            continue
        effective = live.get("effective_status")
        entry: dict[str, Any] = {
            "created_id": created_id,
            "effective_status": effective,
            "status": live.get("status"),
        }
        # PAUSED-by-default: a spending create must never come back ACTIVE. (A paused entity anywhere in
        # the hierarchy reads PAUSED / CAMPAIGN_PAUSED / ADSET_PAUSED — never ACTIVE — so keying on
        # exactly "ACTIVE" avoids false positives from the paused-parent variants.)
        if str(effective or "").upper() == "ACTIVE":
            reason = (
                f"created {kind} {created_id} came back effective_status=ACTIVE — authoring forces "
                "PAUSED, so an ACTIVE create is a red flag (it may already be spending). Pause it."
            )
            entry["red_flag"] = reason
            follow_ups.append(
                {"type": "created_active", "kind": kind, "created_id": created_id, "reason": reason}
            )
        verifications[str(r.op_id)] = entry
    return verifications, follow_ups


def _verify_rotation_outcomes(
    exec_results: list, reader: MetaReaderProvider
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Rotation outcome verify: ``apply_rotation_plan`` already did the pre-write live-targeting drift
    re-read, so here we re-read each executed ad set once and record that the targeting write registered
    — the new included audiences and the (now-off, for a disable) ``advantage_audience`` flag. Keyed by
    ``adset_id`` to match the rotation result serialization; a re-read failure is recorded, never
    raised."""
    verifications: dict[str, dict[str, Any]] = {}
    for r in exec_results:
        if r.status != rotation.EXECUTED_STATUS:
            continue
        adset_id = str(getattr(r, "adset_id", "") or "")
        if not adset_id:
            continue
        try:
            live = reader.get_adset(adset_id, fields=rotation.ADSET_FIELDS)
        except MetaApiError as exc:
            verifications[adset_id] = {"verify_error": str(exc)}
            continue
        targeting = live.get("targeting") if isinstance(live.get("targeting"), dict) else {}
        verifications[adset_id] = {
            "effective_status": live.get("effective_status"),
            "advantage_audience": rotation.advantage_audience_enabled(targeting),
            "included": rotation._ids(rotation._audience_refs(targeting.get("custom_audiences"))),
        }
    return verifications, []


def _write_audit(
    plan: dict[str, Any], exec_results: list, account_slug: str, run_date: str, reports_root: Path
) -> Path | None:
    """Write the timestamped results log for this execute pass, dispatched by ``plan_type`` to the
    existing per-family writer (each embeds ``plan.get("plan_id")`` so the audit ties back to the
    proposal). Keeps the "every execute appends an audit trail" invariant uniform across all four
    families; an unrecognized ``plan_type`` writes nothing."""
    plan_type = str(plan.get("plan_type"))
    if plan_type == "ops":
        return control.write_ops_results(
            plan=plan, results=exec_results,
            output_path=control.default_ops_results_path(account_slug, run_date, reports_root),
            execute=True,
        )
    if plan_type == "authoring":
        return authoring.write_authoring_results(
            plan=plan, results=exec_results,
            output_path=authoring.default_authoring_results_path(account_slug, run_date, reports_root),
            execute=True,
        )
    if plan_type == "audience_rotation":
        return rotation.write_rotation_results(
            plan=plan, results=exec_results,
            output_path=rotation.default_rotation_results_path(account_slug, run_date, reports_root),
            execute=True,
        )
    if plan_type == "advantage_disable":
        return rotation.write_advantage_disable_results(
            plan=plan, results=exec_results,
            output_path=rotation.default_advantage_disable_results_path(account_slug, run_date, reports_root),
            execute=True,
        )
    return None


def _mark_executed(plan_id: str, reports_root: Path, *, audit_path: Path | None) -> None:
    """Stamp the persisted proposal as executed (the idempotency guard). Called only after the execute
    pass has run — a second :func:`execute_plan` then refuses rather than double-applying."""
    path = find_proposal_path(plan_id, reports_root)
    plan = load_proposal(plan_id, reports_root)
    plan[EXECUTION_KEY] = {
        "executed": True,
        "generated_at": control._now_iso(),
        "audit_path": str(audit_path) if audit_path else None,
    }
    write_json(path, plan)


def preview_plan(
    plan_id: str,
    *,
    reader: MetaReaderProvider,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> dict[str, Any]:
    """Local, **write-free** dry run: load the persisted proposal and report the request each approved
    item *would* send. No write is performed.

    For an **ops** plan this rebuilds the exact Graph request :func:`execute_plan` would send via
    ``control._build_request`` (the reader may do read-only re-reads — a budget cap, current targeting,
    the current creative). Authoring/rotation items are not ops-shaped (``control._build_request`` keys
    on ``op["op"]``, which they lack), so for those families the preview reports the item's stored
    intent (authoring ``params`` / the rotation ``diff`` + new audience ids) rather than re-deriving a
    request — still write-free and non-raising. A non-approved item reports no request; a build error is
    reported inline rather than raised."""
    reader = as_reader(reader)
    plan = load_proposal(plan_id, reports_root)
    plan_type = str(plan.get("plan_type"))
    ops_plan = plan_type in ("ops", "")
    previews: list[dict[str, Any]] = []
    for item in plan_items(plan):
        entry: dict[str, Any] = {
            "op_id": item.get("op_id") or item.get("adset_id"),
            "op": item.get("op") or item.get("kind"),
            "level": item.get("level"),
            "id": item.get("id") or item.get("adset_id"),
            "status": item.get("status"),
        }
        if item.get("status") != control.APPROVED_STATUS:
            entry["would_send"] = None
            entry["note"] = "not approved — would be skipped at execute (approval required)."
        elif ops_plan:
            try:
                entry["would_send"] = control._build_request(item, reader)
            except ValueError as exc:
                entry["would_send"] = None
                entry["error"] = str(exc)
        else:
            # Non-ops item: report the stored intent, not a re-derived Graph request.
            entry["would_send"] = {
                k: item.get(k)
                for k in ("params", "diff", "new_included", "new_excluded", "advantage_audience",
                          "disable_advantage_audience")
                if item.get(k) is not None
            } or None
        previews.append(entry)
    return {
        "plan_id": plan_id,
        "plan_type": plan.get("plan_type"),
        "intent": plan.get("intent"),
        "account_slug": plan.get("account_slug"),
        "ops": previews,
    }


def execute_plan(
    plan_id: str,
    *,
    approval_gate: ApprovalGate,
    reader: MetaReaderProvider,
    client=None,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> dict[str, Any]:
    """Execute an approved proposal, by **id**. This is the only entry point that writes.

    The signature deliberately takes a ``plan_id`` and **never** a plan body: it loads the persisted
    artifact so the caller cannot hand in a plan carrying forged ``status: approved`` ops. Flow:

    1. Load the persisted proposal by id.
    2. Idempotency: refuse if it was already executed (Meta writes are not transactional).
    3. Consult the approval gate (``build_server`` wires :class:`HmacApprovalGate`; a missing secret
       fails closed via :class:`DeniedApprovalGate`; the execute tests pass their own no-op gate).
    4. Refuse if zero items are approved — the core safety refusal for a freshly-proposed plan. The
       approvable items live under the plan-type's key (:func:`plan_items`), NOT always ``plan["ops"]``:
       a rotation plan's items are under ``"rotations"`` / ``"items"``.
    5. Build a write client lazily (never the reader's hidden client — writes keep an explicit client).
    6. **Validate pass** (real ``validate_only`` round-trip, nothing persisted). A read-only token
       surfaces here as a clear scope error. If any approved item fails validation, abort before writing.
    7. **Execute pass** (only reached when the whole validate pass is clean).
    8. Write the audit artifact, stamp the proposal executed, and re-read each entity to verify outcome.
    """
    reader = as_reader(reader)
    plan = load_proposal(plan_id, reports_root)  # (1) by id — never a caller-supplied body

    # (2) idempotency
    if (plan.get(EXECUTION_KEY) or {}).get("executed"):
        return {
            "refused": True,
            "executed": False,
            "plan_id": plan_id,
            "reason": "proposal already executed — refusing to re-apply (Meta writes are not transactional).",
        }

    # (3) approval gate — HmacApprovalGate in the running server (fail-closed DeniedApprovalGate with no
    # secret). Raises ApprovalError on an absent/forged/expired approval; the server maps that to a clean
    # ToolError. Runs BEFORE the step-4 zero-approved refusal, which stays as a second layer.
    approval_gate.assert_approved(plan_id, plan)

    # (4) core refusal: nothing approved -> nothing to send. plan_items() finds the approvable items
    # under the plan-type's key (rotation plans keep theirs under "rotations"/"items", not "ops").
    approved = [it for it in plan_items(plan) if it.get("status") == control.APPROVED_STATUS]
    if not approved:
        return {
            "refused": True,
            "executed": False,
            "plan_id": plan_id,
            "reason": "no approved ops — approval required (see approval gate).",
        }

    plan_type = plan.get("plan_type")
    applier = PLAN_APPLIERS.get(str(plan_type))
    if applier is None:
        raise MetaApiError(
            f"No applier registered for plan_type {plan_type!r}. Executable types: {sorted(PLAN_APPLIERS)}."
        )

    # (5) lazy write client — an explicit client for writes, distinct from the read path.
    if client is None:
        client = client_from_env()

    # (6) validate pass (real round-trip, validate_only=True; nothing persisted)
    validate_results = applier(plan, client, execute=False, validate_only=True, reader=reader)
    scope_msg = scope_error_from_results(validate_results)
    if scope_msg:
        # Surfaced as a MetaApiError so the server's _wrap_tool_errors maps it to a clean ToolError.
        raise MetaApiError(scope_msg)
    failed = [
        r for r in validate_results if r.status in ("validation_failed", "blocked", "failed")
    ]
    if failed:
        return {
            "executed": False,
            "validated": False,
            "plan_id": plan_id,
            "plan_type": plan_type,
            "intent": plan.get("intent"),
            "reason": "one or more approved ops failed validation — no writes performed.",
            "ops": _serialize_results(plan, validate_results, {}),
        }

    # (7) execute pass — reached only when the whole validate pass is clean
    exec_results = applier(plan, client, execute=True, validate_only=False, reader=reader)

    # (8) audit + idempotency stamp + outcome verification (all plan-type-aware)
    account_slug = plan.get("account_slug") or "account"
    run_date = plan.get("run_date") or datetime.now(UTC).date().isoformat()
    audit_path = _write_audit(plan, exec_results, account_slug, run_date, Path(reports_root))
    _mark_executed(plan_id, reports_root, audit_path=audit_path)

    verifications, follow_ups = _verify_outcomes(plan, exec_results, reader)
    ops_out = _serialize_results(plan, exec_results, verifications)

    return {
        "executed": True,
        "plan_id": plan_id,
        "plan_type": plan_type,
        "intent": plan.get("intent"),
        "audit_path": str(audit_path) if audit_path else None,
        "ops": ops_out,
        "follow_ups": follow_ups,
    }


# --- Out-of-band approval (the human-run `approve_plan` CLI stands on this) --


def approve_proposal(
    plan_id: str,
    *,
    secret: bytes,
    op_ids: list[str] | None = None,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Approve a persisted proposal **out-of-band** and HMAC-sign it so :class:`HmacApprovalGate` will
    let :func:`execute_plan` run it.

    Loads the proposal by id, flips the selected items to ``approved`` (all proposed items when
    ``op_ids`` is ``None``; items match by ``op_id`` **or** ``adset_id`` so rotation items are
    selectable), signs the approved content with ``secret``, writes ``plan['approval']``, persists, and
    returns the approval block. Refuses (:class:`MetaApiError`) if the plan was already executed —
    re-approving is meaningless (the idempotency guard would refuse the re-execute regardless).

    The signature is computed **after** the status flips, over the plan's now-approved items, so the
    gate's recompute over the reloaded plan reproduces it byte-for-byte (:func:`canonical_approval_payload`).
    """
    now_fn = now_fn or (lambda: datetime.now(UTC))
    path = find_proposal_path(plan_id, reports_root)
    plan = load_proposal(plan_id, reports_root)
    if (plan.get(EXECUTION_KEY) or {}).get("executed"):
        raise MetaApiError(
            f"proposal {plan_id!r} was already executed — refusing to (re-)approve it."
        )
    wanted = None if op_ids is None else {str(x) for x in op_ids}
    approved_count = 0
    for item in plan_items(plan):
        item_id = str(item.get("op_id") or item.get("adset_id") or "")
        if wanted is None or item_id in wanted:
            item["status"] = control.APPROVED_STATUS
            approved_count += 1
    if approved_count == 0:
        raise MetaApiError(
            f"no items in proposal {plan_id!r} matched the approval selection (op_ids={op_ids!r}); "
            "nothing approved."
        )
    approved_at = now_fn().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    approval = {
        "approved_at": approved_at,
        "approved_count": approved_count,
        "algorithm": APPROVAL_ALGORITHM,
        "signature": compute_approval_signature(plan, approved_at, secret),
    }
    plan[APPROVAL_KEY] = approval
    write_json(path, plan)
    return approval
