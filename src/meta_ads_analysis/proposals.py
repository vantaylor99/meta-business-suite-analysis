"""Proposal store + execute orchestration for the MCP guarded-write flow (pure library — no socket).

The custom MCP server (:mod:`meta_ads_analysis.mcp_server`) exposes ``propose_* → execute_plan`` write
tools. This module is the **shared machinery** those tools stand on, deliberately kept out of the server
entrypoint so it is unit-testable with no ``mcp`` SDK and no live Meta call:

- a **proposal store** — ``save_proposal`` persists a built+reviewed plan and returns a ``plan_id``
  *reference*; ``load_proposal`` resolves that id back to the persisted artifact. The agent is handed
  the id, never an approvable plan body — the anti-forgery seam :func:`execute_plan` relies on.
- an **approval seam** — :class:`ApprovalGate` (Protocol) + the default :class:`PlanStatusApprovalGate`.
  The default is a no-op that leans on the ``apply_*_plan`` invariant (only ``status=="approved"`` ops
  are sent). Because **no tool in this ticket flips an op to approved**, a freshly-proposed plan has
  zero approved ops, so :func:`execute_plan` applies nothing and refuses. That default is **forgeable**
  by a local filesystem-write agent (it could hand-edit an op's ``status``); the ``mcp-local-approval-
  gate`` ticket replaces it, behind this same seam, with an un-forgeable source (out-of-band CLI stamp /
  confirmation token / HMAC over the plan).
- :func:`execute_plan` — the only entry point that writes. It loads by id (never a caller body),
  refuses a re-execute, consults the gate, then runs a **validate_only pass first** (real round-trip,
  nothing persisted); only if every approved op validates does it run the **execute pass**, write the
  audit artifact, and re-read each touched entity to verify the outcome landed.
- :func:`preview_plan` — a local, write-free dry run of what each approved op *would* send.

The dispatch map :data:`PLAN_APPLIERS` wires the ``"ops"`` plan type here; the authoring + rotation
branches register in the ``mcp-guarded-write-authoring-rotation`` follow-on ticket.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import control
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
    import json

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


# plan_type -> applier. The wrapper absorbs the signature split: ``apply_ops_plan`` /
# ``apply_rotation_plan`` / ``apply_advantage_disable_plan`` take a ``reader=`` kwarg; the authoring
# applier does not — so the authoring/rotation branches (next ticket) each adapt their own call here.
PLAN_APPLIERS: dict[str, Any] = {
    "ops": _apply_ops,
}

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


# Fields re-read to confirm a write's outcome. ``effective_status`` is the honest signal (``status`` is
# what we set; ``effective_status`` is what Meta actually reports after processing).
_VERIFY_FIELDS = ["id", "status", "effective_status"]


def _verify_outcomes(
    plan: dict[str, Any], exec_results: list, reader: MetaReaderProvider
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Re-read each executed entity's live state and build (per-op-verification, follow-up-markers).

    Carries the pausing lesson: a ``set_status``→PAUSED that registered is necessary but NOT sufficient
    proof delivery stopped — same-day spend can still post. Each such op emits a structured
    ``verify_next_day_spend`` follow-up marker. A re-read failure is recorded per op (never raised — the
    writes already landed; a failed confirmation read must not mask that)."""
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
    op *would* send (exactly what :func:`execute_plan` builds). No write is performed. The reader may
    do read-only re-reads (a budget cap, current targeting, the current creative) to build a request —
    a non-approved op reports no request, and a build error is reported inline rather than raised."""
    reader = as_reader(reader)
    plan = load_proposal(plan_id, reports_root)
    previews: list[dict[str, Any]] = []
    for op in plan.get("ops") or []:
        if not isinstance(op, dict):
            continue
        entry: dict[str, Any] = {
            "op_id": op.get("op_id"),
            "op": op.get("op"),
            "level": op.get("level"),
            "id": op.get("id"),
            "status": op.get("status"),
        }
        if op.get("status") != control.APPROVED_STATUS:
            entry["would_send"] = None
            entry["note"] = "not approved — would be skipped at execute (approval required)."
        else:
            try:
                entry["would_send"] = control._build_request(op, reader)
            except ValueError as exc:
                entry["would_send"] = None
                entry["error"] = str(exc)
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
    3. Consult the approval gate (default no-op — see :class:`PlanStatusApprovalGate`).
    4. Refuse if zero ops are approved — the core safety refusal for a freshly-proposed plan.
    5. Build a write client lazily (never the reader's hidden client — writes keep an explicit client).
    6. **Validate pass** (real ``validate_only`` round-trip, nothing persisted). A read-only token
       surfaces here as a clear scope error. If any approved op fails validation, abort before writing.
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

    # (3) approval gate (default no-op; ticket 13 swaps in an un-forgeable source behind this seam)
    approval_gate.assert_approved(plan_id, plan)

    # (4) core refusal: nothing approved -> nothing to send
    approved = [
        op
        for op in (plan.get("ops") or [])
        if isinstance(op, dict) and op.get("status") == control.APPROVED_STATUS
    ]
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
            "ops": [_result_to_dict(r) for r in validate_results],
        }

    # (7) execute pass — reached only when the whole validate pass is clean
    exec_results = applier(plan, client, execute=True, validate_only=False, reader=reader)

    # (8) audit + idempotency stamp + outcome verification
    account_slug = plan.get("account_slug") or "account"
    run_date = plan.get("run_date") or datetime.now(UTC).date().isoformat()
    audit_path: Path | None = None
    if str(plan_type) == "ops":
        audit_path = control.write_ops_results(
            plan=plan,
            results=exec_results,
            output_path=control.default_ops_results_path(account_slug, run_date, Path(reports_root)),
            execute=True,
        )
    _mark_executed(plan_id, reports_root, audit_path=audit_path)

    verifications, follow_ups = _verify_outcomes(plan, exec_results, reader)
    ops_out: list[dict[str, Any]] = []
    for r in exec_results:
        entry = _result_to_dict(r)
        if str(r.op_id) in verifications:
            entry["verify"] = verifications[str(r.op_id)]
        ops_out.append(entry)

    return {
        "executed": True,
        "plan_id": plan_id,
        "plan_type": plan_type,
        "intent": plan.get("intent"),
        "audit_path": str(audit_path) if audit_path else None,
        "ops": ops_out,
        "follow_ups": follow_ups,
    }
