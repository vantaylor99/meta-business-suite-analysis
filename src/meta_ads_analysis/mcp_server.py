"""Custom Meta MCP server — thin entrypoint over the read library.

This is our own Meta MCP server: a process that starts, reports health, and can be connected
to from an MCP client over HTTP. Alongside the ``server_info`` health tool it exposes the live
Meta **read** surface — one tool per :data:`READ_TOOL_METHODS` entry (13 reads), each bound to a
shared :class:`~meta_ads_analysis.reader_provider.DirectMetaReader` — plus the guarded **write**
surface (``propose_* → preview_plan → execute_plan``). Every write travels through the same
propose → human-approve → validate → execute → verify pipeline as the CLI: a ``propose_*`` tool
grounds + reviews the op and persists a proposal, returning only a ``plan_id`` reference; ``execute_
plan`` is the *only* tool that writes, and it refuses a plan with zero approved ops. The write
lifecycle lives in :mod:`meta_ads_analysis.proposals` + the existing ``control`` builders; this
module only wires callables. Our tools carry the ``mcp__meta-suite__*`` prefix (distinct from the
deny-listed community ``mcp__meta-ads__*``) precisely because ours are gated.

The module is a **thin entrypoint over the existing library**: it embeds no Meta/business
logic and only imports and exposes package functions, so the CLI and the server stay two
frontends over one library. The ``mcp`` SDK import is guarded at module load (mirroring the
``requests``-missing pattern in :mod:`meta_ads_analysis.meta_api`) so a missing ``server``
extra produces an actionable ``SystemExit`` at use site, never a bare ``ImportError``; a missing
``META_ACCESS_TOKEN`` at startup likewise surfaces as an actionable ``SystemExit``.
"""

from __future__ import annotations

import argparse
import functools
import os
from collections.abc import Callable
from typing import Any

# Mirror the requests-missing pattern in meta_api.py: import guarded at module load,
# actionable SystemExit raised at use site — never a bare ImportError traceback.
try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ModuleNotFoundError:  # pragma: no cover - exercised only without the `server` extra
    FastMCP = None
    ToolError = None

from datetime import UTC, datetime

from . import __version__, account_registry, authoring, control, proposals, rotation
from .meta_api import MetaApiError, meta_api_version_from_env
from .reader_provider import (
    READ_METHODS,
    DirectMetaReader,
    MetaReaderProvider,
    reader_backend_from_env,
)

SERVER_NAME = "meta-ads-mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# The read surface we expose as MCP tools: every read in READ_METHODS **except** the raw
# ``iter_paginated`` escape hatch (a Graph-path/params primitive with no natural tool shape —
# the high-level reads drain pagination internally). This mirrors what ``MCPMetaReader`` omits.
READ_TOOL_METHODS: tuple[str, ...] = tuple(m for m in READ_METHODS if m != "iter_paginated")

# reader-method -> MCP tool name. Identity, because each tool is named **exactly** for its reader
# method (see the plan's decision 1: natural, self-describing names, not the community package's
# dialect). Shipped as a module constant so a future consumer wiring an ``MCPMetaReader`` at our
# server has the full name map for all 13 reads in one import.
SERVER_TOOL_MAP: dict[str, str] = {m: m for m in READ_TOOL_METHODS}

# Short human descriptions surfaced to the MCP client (and the calling LLM) per tool.
READ_TOOL_DESCRIPTIONS: dict[str, str] = {
    "fetch_insights": (
        "Fetch time-series ad insights (spend, results, ROAS, etc.) for an ad account over an "
        "ISO date range, at the given level and time increment."
    ),
    "fetch_ads": "List all ads in an ad account with the requested fields.",
    "list_campaigns": "List campaigns in an ad account, optionally filtered by effective status.",
    "get_campaign": "Fetch a single campaign's current state by id.",
    "list_adsets": "List ad sets in an ad account, optionally filtered by effective status.",
    "get_adset": "Fetch a single ad set's current state by id.",
    "get_ad": "Fetch a single ad's current state by id.",
    "list_custom_audiences": "List the custom audiences available in an ad account.",
    "get_account": (
        "Fetch account-level info (status, currency, spend cap, amount spent, funding)."
    ),
    "get_delivery_estimate": "Estimate audience reach/size for an ad set's current targeting.",
    "search_targeting": (
        "Search Meta's targeting catalog (interests, behaviors, demographics) by free-text query."
    ),
    "list_pixels": "List the ad pixels configured on an ad account.",
    "list_custom_conversions": "List the custom conversions configured on an ad account.",
}


def build_server_info() -> dict:
    """Pure, token-free health/info payload. Unit-testable without binding a socket.

    Reports the server identity, the configured Meta API version, the selected read backend,
    and whether live Meta calls are enabled. Uses the token-free ``*_from_env`` helpers, so it
    never touches ``META_ACCESS_TOKEN`` and never raises on a missing token or an unrecognized
    backend — it is a health probe, not a constructor.
    """
    return {
        "name": SERVER_NAME,
        "version": __version__,
        "meta_api_version": meta_api_version_from_env(),   # no token required
        "read_backend": reader_backend_from_env(),         # "direct" | "mcp" (verbatim-normalized)
        # Capability flag: this server now exposes live Meta **read** tools (see build_read_tools).
        # Independent of whether a token is present — build_server_info stays token-free.
        "live_calls_enabled": True,
        # This server now also exposes the guarded **write** surface (propose_* / preview_plan /
        # execute_plan — see build_write_tools). Every write is gated (propose → approve → validate →
        # execute → verify); execute refuses a plan with zero approved ops.
        "write_tools_enabled": True,
        # Approval is always required to execute a write. ``approval_configured`` is a token-free health
        # signal: True when an HMAC approval secret is set (HmacApprovalGate), False when it is absent or
        # misconfigured (fail-closed DeniedApprovalGate — execute refuses, reads still work). Non-raising:
        # a short/unreadable secret degrades to False here rather than breaking the health probe.
        "approval_required": True,
        "approval_configured": _approval_configured(),
    }


def _approval_configured() -> bool:
    """Whether an HMAC approval secret is resolvable (health signal for :func:`build_server_info`).

    Non-raising: a misconfigured secret (too short / unreadable file) raises inside
    :func:`proposals.approval_secret_from_env`, but ``server_info`` is a health probe, not a constructor,
    so we report ``False`` rather than propagating the error."""
    try:
        return proposals.approval_secret_from_env() is not None
    except ValueError:
        return False


def build_read_tools(reader: MetaReaderProvider) -> dict[str, Callable[..., Any]]:
    """Return ``{tool_name: callable}`` for every Meta **read**, bound to ``reader``.

    PURE: no FastMCP import, no socket, no token lookup — unit-testable with a ``FakeMetaReader``.
    Each callable is a thin, zero-translation wrapper over the same-named ``MetaReaderProvider``
    method: it mirrors that method's arguments and returns the identical dict/list the reader
    returns (an empty list stays ``[]``; an API failure lets ``MetaApiError`` propagate unchanged).

    The wrappers take **positional-or-keyword** params (no keyword-only split) with accurate
    annotations, because MCP arguments arrive as a flat object and FastMCP derives each tool's JSON
    schema from the wrapper's own signature. ``fields`` stays a ``list[str]`` (no comma-join) since
    the call goes straight into the reader; ``date_from``/``date_to`` stay separate ISO strings and
    ``search_type`` stays ``search_type`` — the natural surface, not the community dialect.
    """

    def fetch_insights(
        ad_account_id: str,
        fields: list[str],
        date_from: str,
        date_to: str,
        level: str = "ad",
        time_increment: int | str = 1,
        breakdowns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return reader.fetch_insights(
            ad_account_id,
            fields=fields,
            date_from=date_from,
            date_to=date_to,
            level=level,
            time_increment=time_increment,
            breakdowns=breakdowns,
        )

    def fetch_ads(ad_account_id: str, fields: list[str]) -> list[dict[str, Any]]:
        return reader.fetch_ads(ad_account_id, fields=fields)

    def list_campaigns(
        ad_account_id: str, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return reader.list_campaigns(ad_account_id, fields=fields, effective_status=effective_status)

    def get_campaign(campaign_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_campaign(campaign_id, fields=fields)

    def list_adsets(
        ad_account_id: str, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return reader.list_adsets(ad_account_id, fields=fields, effective_status=effective_status)

    def get_adset(adset_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_adset(adset_id, fields=fields)

    def get_ad(ad_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_ad(ad_id, fields=fields)

    def list_custom_audiences(ad_account_id: str, fields: list[str]) -> list[dict[str, Any]]:
        return reader.list_custom_audiences(ad_account_id, fields=fields)

    def get_account(ad_account_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_account(ad_account_id, fields=fields)

    def get_delivery_estimate(adset_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_delivery_estimate(adset_id, fields=fields)

    def search_targeting(
        query: str, search_type: str = "adinterest", limit: int = 25
    ) -> list[dict[str, Any]]:
        return reader.search_targeting(query=query, search_type=search_type, limit=limit)

    def list_pixels(ad_account_id: str, fields: list[str]) -> list[dict[str, Any]]:
        return reader.list_pixels(ad_account_id, fields=fields)

    def list_custom_conversions(ad_account_id: str, fields: list[str]) -> list[dict[str, Any]]:
        return reader.list_custom_conversions(ad_account_id, fields=fields)

    tools: dict[str, Callable[..., Any]] = {
        "fetch_insights": fetch_insights,
        "fetch_ads": fetch_ads,
        "list_campaigns": list_campaigns,
        "get_campaign": get_campaign,
        "list_adsets": list_adsets,
        "get_adset": get_adset,
        "get_ad": get_ad,
        "list_custom_audiences": list_custom_audiences,
        "get_account": get_account,
        "get_delivery_estimate": get_delivery_estimate,
        "search_targeting": search_targeting,
        "list_pixels": list_pixels,
        "list_custom_conversions": list_custom_conversions,
    }
    # Guard against a read added to READ_TOOL_METHODS but not wired here (or vice versa). The
    # parity test asserts the same, but failing loudly at construction beats a silently-missing tool.
    assert set(tools) == set(READ_TOOL_METHODS), (
        f"build_read_tools drifted from READ_TOOL_METHODS: "
        f"missing={set(READ_TOOL_METHODS) - set(tools)}, extra={set(tools) - set(READ_TOOL_METHODS)}"
    )
    return tools


# Short human descriptions for the guarded-write surface (surfaced to the MCP client / calling LLM).
WRITE_TOOL_DESCRIPTIONS: dict[str, str] = {
    "propose_set_status": (
        "Propose pausing (PAUSED) or enabling (ACTIVE) one ad / ad set / campaign. Grounded, reviewed, "
        "and persisted as a proposal; pausing the last active ad in a set also proposes pausing the set."
    ),
    "propose_set_daily_budget": (
        "Propose a daily-budget change for ONE ad set or campaign (exactly one). CBO is detected: an "
        "ad set under campaign-budget-optimization yields a campaign-level op, not an ad-set write."
    ),
    "propose_rename": "Propose renaming one ad / ad set / campaign.",
    "propose_set_creative": "Propose swapping an ad's creative to an existing creative id.",
    "propose_set_creative_features": (
        "Propose changing an ad's creative-enhancement enrollment (opt_in / opt_out feature lists)."
    ),
    "propose_set_age_range": "Propose an ad set's targeting age range (13 <= age_min <= age_max <= 65).",
    "propose_set_genders": "Propose an ad set's gender targeting ([] = all; 1 = male, 2 = female).",
    "propose_set_geo_locations": "Propose an ad set's geo-location targeting object.",
    "propose_set_placements": "Propose an ad set's placements (automatic, or explicit publisher platforms).",
    "propose_enable_ads": "Propose enabling currently-inactive ads (optionally filtered), each grounded on its own recent performance.",
    "propose_pause_ads": "Propose pausing active ads by filter and/or a ROAS-below-threshold rule.",
    # Authoring (create-only; every created spending entity is forced PAUSED).
    "propose_create_campaign": (
        "Propose creating a campaign (name + objective; created PAUSED). Net-new → no performance "
        "evidence → abstains, so an approved create is blocked at apply until a conscious override."
    ),
    "propose_create_adset": (
        "Propose creating an ad set under a campaign (created PAUSED). Pass the rest of the ad set body "
        "(optimization_goal, billing_event, targeting, daily_budget, …) in params. Net-new → abstains."
    ),
    "propose_create_ad": (
        "Propose creating an ad in an ad set from an existing creative id (created PAUSED). To recreate "
        "a proven ad's creative instead, use propose_duplicate_ad (grounded on the source's metric)."
    ),
    "propose_create_video_ad": (
        "Propose creating a video ad (created PAUSED) from an ALREADY-UPLOADED video_id (uploads are "
        "CLI-only). Net-new → abstains → approved create blocked until a conscious override."
    ),
    "propose_duplicate_ad": (
        "Propose duplicating an existing ad's creative into a target ad set (created PAUSED), grounded "
        "on the SOURCE ad's own recent metric — a proven winner is executable, an undelivered source abstains."
    ),
    "propose_lookalike": (
        "Propose creating a lookalike audience from a seed audience. An audience is inert (no status, "
        "never PAUSED, never spends) — a structural abstain that the apply-time gate allows."
    ),
    # Audience rotation (reversible targeting experiment) + Advantage-Audience disable.
    "propose_audience_rotation": (
        "Propose rotating each active ad set's included custom audience forward by offset (exclusions "
        "recomputed to preserve target-one/exclude-the-rest). Grounded on each ad set's fatigue signal."
    ),
    "propose_advantage_disable": (
        "Propose turning Advantage Audience OFF on each active ad set that has it enabled, preserving "
        "included/excluded audiences verbatim. Only ever disables automation — never enables it."
    ),
    "preview_plan": "Local, write-free dry run: show the request each APPROVED op in a proposal would send. No Meta write.",
    "execute_plan": (
        "Execute an approved proposal by plan_id — the ONLY tool that writes. Validates first, aborts on "
        "any validation failure, then applies approved ops and verifies the outcome. Refuses if nothing "
        "is approved or if the proposal was already executed."
    ),
}


def _resolve_account(account: str) -> tuple[str | None, str]:
    """Resolve an ``account`` argument into ``(account_slug, ad_account_id)``.

    Accepts a registry slug/name (grounding + policy use the slug) OR a raw ``act_<id>`` / numeric id
    (slug ``None``). A value that is neither is a clear ``ValueError`` (mapped to a tool error), so a
    typo never silently targets the wrong account.
    """
    text = str(account or "").strip()
    try:
        acct = account_registry.resolve_account(text, account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH)
        return account_registry.slugify_name(text), acct.ad_account_id
    except (FileNotFoundError, KeyError, ValueError):
        pass
    if text.startswith("act_") or text.isdigit():
        return None, account_registry._normalize_ad_account_id(text)
    raise ValueError(
        f"Unknown account {account!r}: not a slug/name in config/meta_ads_accounts.json and not an "
        "act_<id>. Pass a registry slug or an act_ id."
    )


def _proposal_summary(plan_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    """A review-ready digest the agent relays to a human: the ``plan_id`` reference + per-item status /
    confidence band / review verdict / note. Deliberately NOT the approvable plan body — the agent
    approves out-of-band and then calls ``execute_plan`` by id.

    Plan-type-aware via ``proposals.plan_items`` so authoring (``plan["ops"]`` keyed by ``op_id`` +
    ``kind``) and rotation (``plan["rotations"]`` / ``plan["items"]`` keyed by ``adset_id``) produce the
    **same** summary shape as the control ops — the field names are normalized (``op`` shows the op-type
    or the create ``kind``; ``id`` shows the entity id or the ad set id; ``note`` falls back to the
    rotation ``diff``)."""
    ops: list[dict[str, Any]] = []
    for item in proposals.plan_items(plan):
        conf = item.get("confidence") if isinstance(item.get("confidence"), dict) else {}
        review = item.get("review") if isinstance(item.get("review"), dict) else {}
        ops.append(
            {
                "op_id": item.get("op_id") or item.get("adset_id"),
                "op": item.get("op") or item.get("kind"),
                "level": item.get("level"),
                "id": item.get("id") or item.get("adset_id"),
                "status": item.get("status"),
                "confidence_band": conf.get("band"),
                "review_verdict": item.get("review_verdict") or review.get("verdict"),
                "note": item.get("note") or item.get("diff"),
            }
        )
    return {
        "plan_id": plan_id,
        "plan_type": plan.get("plan_type"),
        "intent": plan.get("intent"),
        "account_slug": plan.get("account_slug"),
        "ops": ops,
    }


def build_write_tools(
    reader: MetaReaderProvider, approval_gate: proposals.ApprovalGate
) -> dict[str, Callable[..., Any]]:
    """Return ``{tool_name: callable}`` for the full guarded write surface — control ops, authoring
    (create-only, PAUSED by default), and audience rotation / Advantage-Audience disable.

    PURE: no FastMCP import, no socket — unit-testable with a ``FakeMetaReader``/fake gate. Each
    ``propose_*`` wraps an existing library builder (``control`` / ``authoring`` / ``rotation``) that
    attaches evidence + a computed confidence band and runs the plan through the matching review pass
    (``review_ops_plan`` / ``review_authoring_plan`` / ``review_rotation_plan``), then persists the
    reviewed plan via :mod:`meta_ads_analysis.proposals` and returns a review-ready **summary** (a
    ``plan_id`` reference + per-item digest) — never an approvable body. ``execute_plan`` is the only
    writer; it loads by id, dispatches on ``plan_type`` (``proposals.PLAN_APPLIERS``), and builds its own
    write client lazily (never the reader's hidden client).

    Grounding note: the single-op ``set_status`` here abstains **structurally** (a direct operator
    instruction on a named entity, no metric) — the safety-PAUSE treatment the gate allows. The
    data-driven bulk paths ``propose_enable_ads`` / ``propose_pause_ads`` carry per-ad metric evidence
    (so ``propose_enable_ads`` enforces the cold-ad boundary), which a single explicit status change
    deliberately does not. Authoring net-new creates cite a zero sample → ``abstain`` (blocked at apply
    until a conscious override), while a ``propose_duplicate_ad`` grounds on the source ad's own metric
    and a ``propose_lookalike`` is a structural abstain (an audience is inert). Every created spending
    entity is forced PAUSED by ``authoring._build_create`` regardless of any review verdict. Media
    uploads (video/image) stay CLI-only and are NOT exposed here — the asset id is passed to
    ``propose_create_video_ad`` / ``propose_create_ad``.
    """

    def _finalize(plan: dict[str, Any]) -> dict[str, Any]:
        account_slug = plan.get("account_slug")
        run_date = plan.get("run_date") or datetime.now(UTC).date().isoformat()
        plan_id = proposals.save_proposal(plan, account_slug=account_slug, run_date=run_date)
        return _proposal_summary(plan_id, plan)

    def propose_set_status(
        account: str, id: str, level: str, status: str, run_date: str | None = None
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = control.build_single_op_plan(
            reader, ad_account_id, op="set_status", level=level, id=id,
            params={"status": str(status).upper()}, account_slug=account_slug, run_date=run_date,
        )
        # Pausing the last ACTIVE ad in a set leaves it live-but-not-delivering; propose the set pause too.
        if level == "ad" and str(status).upper() == "PAUSED":
            plan = control.append_last_active_ad_pause(reader, plan)
        return _finalize(plan)

    def propose_set_daily_budget(
        account: str,
        daily_budget_cents: int,
        adset_id: str | None = None,
        campaign_id: str | None = None,
        run_date: str | None = None,
        max_increase_percent: float | None = None,
        max_decrease_percent: float | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = control.build_budget_plan(
            reader, ad_account_id, new_daily_budget_cents=int(daily_budget_cents),
            adset_id=adset_id, campaign_id=campaign_id, account_slug=account_slug, run_date=run_date,
            max_increase_percent=max_increase_percent, max_decrease_percent=max_decrease_percent,
        )
        return _finalize(plan)

    def _single_op(account, *, op, level, id, params, run_date):
        account_slug, ad_account_id = _resolve_account(account)
        plan = control.build_single_op_plan(
            reader, ad_account_id, op=op, level=level, id=id, params=params,
            account_slug=account_slug, run_date=run_date,
        )
        return _finalize(plan)

    def propose_rename(account: str, id: str, level: str, name: str, run_date: str | None = None) -> dict[str, Any]:
        return _single_op(account, op="rename", level=level, id=id, params={"name": name}, run_date=run_date)

    def propose_set_creative(account: str, id: str, creative_id: str, run_date: str | None = None) -> dict[str, Any]:
        return _single_op(account, op="set_creative", level="ad", id=id, params={"creative_id": creative_id}, run_date=run_date)

    def propose_set_creative_features(
        account: str, id: str, opt_in: list[str] | None = None, opt_out: list[str] | None = None,
        run_date: str | None = None,
    ) -> dict[str, Any]:
        return _single_op(
            account, op="set_creative_features", level="ad", id=id,
            params={"opt_in": opt_in or [], "opt_out": opt_out or []}, run_date=run_date,
        )

    def propose_set_age_range(account: str, adset_id: str, age_min: int, age_max: int, run_date: str | None = None) -> dict[str, Any]:
        return _single_op(account, op="set_age_range", level="adset", id=adset_id,
                          params={"age_min": int(age_min), "age_max": int(age_max)}, run_date=run_date)

    def propose_set_genders(account: str, adset_id: str, genders: list[int], run_date: str | None = None) -> dict[str, Any]:
        return _single_op(account, op="set_genders", level="adset", id=adset_id,
                          params={"genders": list(genders or [])}, run_date=run_date)

    def propose_set_geo_locations(account: str, adset_id: str, geo_locations: dict[str, Any], run_date: str | None = None) -> dict[str, Any]:
        return _single_op(account, op="set_geo_locations", level="adset", id=adset_id,
                          params={"geo_locations": geo_locations}, run_date=run_date)

    def propose_set_placements(
        account: str, adset_id: str, automatic: bool = False,
        publisher_platforms: list[str] | None = None, run_date: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"automatic": bool(automatic)}
        if publisher_platforms is not None:
            params["publisher_platforms"] = list(publisher_platforms)
        return _single_op(account, op="set_placements", level="adset", id=adset_id, params=params, run_date=run_date)

    def propose_enable_ads(
        account: str, adset_ids: list[str] | None = None, name_contains: str | None = None,
        date_from: str | None = None, date_to: str | None = None, run_date: str | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = control.build_enable_ads_plan(
            reader, ad_account_id, account_slug=account_slug, adset_ids=adset_ids,
            name_contains=name_contains, date_from=date_from, date_to=date_to, run_date=run_date,
        )
        return _finalize(plan)

    def propose_pause_ads(
        account: str, adset_ids: list[str] | None = None, name_contains: str | None = None,
        roas_below: float | None = None, min_spend: float = 0.0,
        date_from: str | None = None, date_to: str | None = None, run_date: str | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = control.build_pause_plan(
            reader, ad_account_id, account_slug=account_slug, adset_ids=adset_ids,
            name_contains=name_contains, roas_below=roas_below, min_spend=min_spend,
            date_from=date_from, date_to=date_to, run_date=run_date,
        )
        return _finalize(plan)

    # --- Authoring (create-only; PAUSED by default). Each wraps an authoring builder that grounds the
    # create, runs review.review_authoring_plan, and — for spending creates — forces PAUSED regardless of
    # verdict. No delete/archive. Media uploads stay CLI-only: the video/image asset id is passed in. ---

    def propose_create_campaign(
        account: str, name: str, objective: str, special_ad_categories: list[str] | None = None,
        params: dict[str, Any] | None = None, run_date: str | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = authoring.build_create_campaign_plan(
            ad_account_id, name=name, objective=objective,
            special_ad_categories=special_ad_categories, params=params,
            account_slug=account_slug, run_date=run_date,
        )
        return _finalize(plan)

    def propose_create_adset(
        account: str, name: str, campaign_id: str,
        params: dict[str, Any] | None = None, run_date: str | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = authoring.build_create_adset_plan(
            ad_account_id, name=name, campaign_id=campaign_id, params=params,
            account_slug=account_slug, run_date=run_date,
        )
        return _finalize(plan)

    def propose_create_ad(
        account: str, name: str, adset_id: str, creative_id: str, run_date: str | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = authoring.build_create_ad_plan(
            ad_account_id, name=name, adset_id=adset_id, creative_id=creative_id,
            account_slug=account_slug, run_date=run_date,
        )
        return _finalize(plan)

    def propose_create_video_ad(
        account: str, name: str, adset_id: str, video_id: str, page_id: str, link: str,
        message: str | None = None, title: str | None = None, description: str | None = None,
        primary_texts: list[str] | None = None, headlines: list[str] | None = None,
        descriptions: list[str] | None = None, call_to_action_type: str = "SHOP_NOW",
        image_hash: str | None = None, image_url: str | None = None, run_date: str | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = authoring.build_video_ad_plan(
            ad_account_id, name=name, adset_id=adset_id, video_id=video_id, page_id=page_id, link=link,
            message=message, title=title, description=description, primary_texts=primary_texts,
            headlines=headlines, descriptions=descriptions, call_to_action_type=call_to_action_type,
            image_hash=image_hash, image_url=image_url, account_slug=account_slug, run_date=run_date,
        )
        return _finalize(plan)

    def propose_duplicate_ad(
        account: str, source_ad_id: str, target_adset_id: str, name: str | None = None,
        date_from: str | None = None, date_to: str | None = None, run_date: str | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = authoring.build_duplicate_ad_plan(
            reader, ad_account_id, source_ad_id=source_ad_id, target_adset_id=target_adset_id,
            name=name, account_slug=account_slug, date_from=date_from, date_to=date_to, run_date=run_date,
        )
        return _finalize(plan)

    def propose_lookalike(
        account: str, name: str, origin_audience_id: str, country: str, ratio: float,
        date_from: str | None = None, date_to: str | None = None, run_date: str | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        plan = authoring.build_lookalike_plan(
            ad_account_id, name=name, origin_audience_id=origin_audience_id, country=country,
            ratio=ratio, account_slug=account_slug, date_from=date_from, date_to=date_to, run_date=run_date,
        )
        return _finalize(plan)

    # --- Audience rotation (reversible) + Advantage-Audience disable. Read the account's ACTIVE ad sets
    # live, then build a reviewed plan (rotation grounds on each ad set's fatigue metric; disable is a
    # structural abstain). Bulk ad-set rename is intentionally OUT OF SCOPE — the ops `rename` (ticket 12)
    # already covers renames. ---

    def _active_adsets(ad_account_id: str) -> list[dict[str, Any]]:
        return reader.list_adsets(ad_account_id, fields=rotation.ADSET_FIELDS, effective_status=["ACTIVE"])

    def propose_audience_rotation(
        account: str, offset: int = 1, disable_advantage_audience: bool = False,
        date_from: str | None = None, date_to: str | None = None, run_date: str | None = None,
    ) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        adsets = _active_adsets(ad_account_id)
        policy = control.resolve_action_policy(account_slug)
        df, dt, recency_days, run_date_iso = control._resolve_grounding_window(date_from, date_to, run_date)
        # Each ad set's recent performance is the fatigue signal that grounds its swap (correlational).
        metrics_by_id = {
            str(m["id"]): m
            for m in control.fetch_entity_metrics(reader, ad_account_id, level="adset", date_from=df, date_to=dt)
        }
        plan = rotation.build_rotation_plan(
            adsets, account_slug=account_slug or "account", ad_account_id=ad_account_id,
            offset=int(offset), disable_advantage_audience=bool(disable_advantage_audience),
            metrics_by_id=metrics_by_id, goal=policy.get("primary_goal"), policy=policy,
            date_from=df, date_to=dt, recency_days=recency_days, run_date=run_date_iso,
        )
        return _finalize(plan)

    def propose_advantage_disable(account: str) -> dict[str, Any]:
        account_slug, ad_account_id = _resolve_account(account)
        adsets = _active_adsets(ad_account_id)
        plan = rotation.build_advantage_disable_plan(
            adsets, account_slug=account_slug or "account", ad_account_id=ad_account_id,
        )
        return _finalize(plan)

    def preview_plan(plan_id: str) -> dict[str, Any]:
        return proposals.preview_plan(plan_id, reader=reader)

    def execute_plan(plan_id: str) -> dict[str, Any]:
        return proposals.execute_plan(plan_id, approval_gate=approval_gate, reader=reader)

    tools: dict[str, Callable[..., Any]] = {
        "propose_set_status": propose_set_status,
        "propose_set_daily_budget": propose_set_daily_budget,
        "propose_rename": propose_rename,
        "propose_set_creative": propose_set_creative,
        "propose_set_creative_features": propose_set_creative_features,
        "propose_set_age_range": propose_set_age_range,
        "propose_set_genders": propose_set_genders,
        "propose_set_geo_locations": propose_set_geo_locations,
        "propose_set_placements": propose_set_placements,
        "propose_enable_ads": propose_enable_ads,
        "propose_pause_ads": propose_pause_ads,
        "propose_create_campaign": propose_create_campaign,
        "propose_create_adset": propose_create_adset,
        "propose_create_ad": propose_create_ad,
        "propose_create_video_ad": propose_create_video_ad,
        "propose_duplicate_ad": propose_duplicate_ad,
        "propose_lookalike": propose_lookalike,
        "propose_audience_rotation": propose_audience_rotation,
        "propose_advantage_disable": propose_advantage_disable,
        "preview_plan": preview_plan,
        "execute_plan": execute_plan,
    }
    return tools


def _wrap_tool_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool callable so an operator-actionable error becomes a clean FastMCP ``ToolError``.

    A bad token, an insufficient scope, or a transient Graph failure (``MetaApiError``), a malformed
    op / guardrail violation (``ValueError`` from ``validate_op`` / ``build_budget_plan``), or an
    approval-gate rejection (``ApprovalError``) all surface to the MCP client as a tool error — the
    server keeps serving instead of leaking an uncaught traceback. ``functools.wraps`` preserves
    ``func``'s signature so FastMCP still derives the correct JSON schema from it. (Read tools raise
    only ``MetaApiError`` in practice, so widening the catch does not change their behavior.)
    """

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (MetaApiError, ValueError, proposals.ApprovalError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapped


def build_server(host: str, port: int):
    """Construct the FastMCP server exposing ``server_info`` plus the live Meta read tools.

    Raises an actionable ``SystemExit`` if the ``mcp`` SDK (the ``server`` extra) is not
    installed, rather than surfacing a bare ``ImportError``.

    The reader is built **once** as a ``DirectMetaReader.from_env()`` and shared across all tool
    calls. We deliberately do NOT call ``reader_from_env``: with ``META_READER_BACKEND=mcp`` that
    would try to build an ``MCPMetaReader`` requiring an injected tool-executor — i.e. our server
    would recursively try to be its own MCP client. Our server *is* the direct reader an ``mcp``
    backend elsewhere points at; it must never recursively select an ``mcp`` backend. (``server_info``
    still reports ``reader_backend_from_env()`` verbatim as a health string — independent of this.)
    """
    if FastMCP is None:
        raise SystemExit(
            "The 'mcp' package is required to run the Meta MCP server. "
            "Install the server extra with `pip install -e .[server]`."
        )
    mcp = FastMCP(SERVER_NAME, host=host, port=port)

    @mcp.tool()
    def server_info() -> dict:
        """Report server identity, configured Meta API version, selected read backend, and whether
        live Meta calls are enabled."""
        return build_server_info()

    # One shared direct reader (NOT reader_from_env — see docstring). Live reads flow through it.
    # from_env() builds the client eagerly, so a missing META_ACCESS_TOKEN (or a missing `requests`)
    # raises MetaApiError here at startup. Convert it to an actionable SystemExit — mirroring the
    # SDK-missing branch above — so a mis-configured launch prints guidance instead of leaking a bare
    # MetaApiError traceback out of main() (which only wraps OSError).
    try:
        reader = DirectMetaReader.from_env()
    except MetaApiError as exc:
        raise SystemExit(
            f"Cannot start the Meta MCP server: {exc} "
            "Set META_ACCESS_TOKEN (a token with the ads_read scope) before launching."
        ) from exc
    for name, func in build_read_tools(reader).items():
        mcp.add_tool(
            _wrap_tool_errors(func),
            name=name,
            description=READ_TOOL_DESCRIPTIONS.get(name) or f"Meta read: {name}",
        )

    # Guarded write surface. The approval gate is selected from the environment: with META_APPROVAL_SECRET
    # set it is an HmacApprovalGate (execute verifies an out-of-band, human-produced HMAC signature over
    # the plan body — a secret the agent's tool surface never holds); with no secret it is
    # a fail-closed DeniedApprovalGate (execute refused with setup guidance; reads unaffected). The agent
    # can freely edit the proposal JSON but cannot forge a signature. See proposals.select_approval_gate_from_env.
    approval_gate = proposals.select_approval_gate_from_env()
    for name, func in build_write_tools(reader, approval_gate).items():
        mcp.add_tool(
            _wrap_tool_errors(func),
            name=name,
            description=WRITE_TOOL_DESCRIPTIONS.get(name) or f"Meta guarded write: {name}",
        )

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the custom Meta MCP server (HTTP).")
    parser.add_argument("--host", default=os.environ.get("MCP_SERVER_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_SERVER_PORT", DEFAULT_PORT)))
    args = parser.parse_args()
    mcp = build_server(args.host, args.port)
    try:
        mcp.run(transport="streamable-http")
    except OSError as exc:  # port in use / bad host bind
        raise SystemExit(f"Could not start MCP server on {args.host}:{args.port}: {exc}") from exc


if __name__ == "__main__":  # pragma: no cover - module-run convenience
    main()
