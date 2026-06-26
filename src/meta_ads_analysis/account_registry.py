"""Account registry loading for multi-account Meta sync."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_ACCOUNTS_CONFIG_PATH
from .utils import slugify_name


@dataclass(slots=True)
class MetaAdsAccount:
    account_slug: str
    account_name: str
    ad_account_id: str
    timezone: str | None = None
    notes: str | None = None
    primary_result_action_type: str | None = None
    primary_result_label: str | None = None
    primary_metric: str | None = None
    secondary_metric: str | None = None
    secondary_metric_label: str | None = None
    roas_role: str | None = None
    analysis_notes: str | None = None
    action_policy: dict[str, object] | None = None
    # Per-account override of config.MAX_BUDGET_DECREASE_PERCENT; None means use the global default.
    max_budget_decrease_percent: float | None = None


def load_account_registry(
    config_path: Path = DEFAULT_ACCOUNTS_CONFIG_PATH,
) -> dict[str, MetaAdsAccount]:
    if not config_path.exists():
        raise FileNotFoundError(
            f"Meta account registry not found: {config_path}. Expected config/meta_ads_accounts.json"
        )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    raw_accounts = payload.get("accounts", payload)
    if not isinstance(raw_accounts, list):
        raise ValueError(
            "Account registry must contain an 'accounts' array or be a top-level array."
        )

    accounts: dict[str, MetaAdsAccount] = {}
    for item in raw_accounts:
        if not isinstance(item, dict):
            raise ValueError("Each account registry entry must be an object.")
        measurement_focus = item.get("measurement_focus")
        if measurement_focus is None:
            focus = {}
        elif isinstance(measurement_focus, dict):
            focus = measurement_focus
        else:
            raise ValueError("measurement_focus must be an object when provided.")
        action_policy = item.get("action_policy")
        if action_policy is None:
            policy = {}
        elif isinstance(action_policy, dict):
            policy = action_policy
        else:
            raise ValueError("action_policy must be an object when provided.")
        account_slug = slugify_name(str(item.get("account_slug") or item.get("account_name") or ""))
        if account_slug in accounts:
            raise ValueError(f"Duplicate account_slug in registry: {account_slug}")

        account_name = str(item.get("account_name") or "").strip()
        ad_account_id = _normalize_ad_account_id(str(item.get("ad_account_id") or "").strip())
        if not account_name:
            raise ValueError(f"Account entry '{account_slug}' is missing account_name.")
        if not ad_account_id:
            raise ValueError(f"Account entry '{account_slug}' is missing ad_account_id.")

        raw_decrease = policy.get("max_budget_decrease_percent")
        max_budget_decrease_percent = float(raw_decrease) if raw_decrease is not None else None

        accounts[account_slug] = MetaAdsAccount(
            account_slug=account_slug,
            account_name=account_name,
            ad_account_id=ad_account_id,
            timezone=_none_if_blank(item.get("timezone")),
            notes=_none_if_blank(item.get("notes")),
            primary_result_action_type=_none_if_blank(
                focus.get("primary_result_action_type", item.get("primary_result_action_type"))
            ),
            primary_result_label=_none_if_blank(
                focus.get("primary_result_label", item.get("primary_result_label"))
            ),
            primary_metric=_none_if_blank(focus.get("primary_metric"))
            or "results",
            secondary_metric=_none_if_blank(focus.get("secondary_metric")),
            secondary_metric_label=_none_if_blank(focus.get("secondary_metric_label")),
            roas_role=_none_if_blank(focus.get("roas_role")),
            analysis_notes=_none_if_blank(focus.get("analysis_notes")),
            action_policy=policy,
            max_budget_decrease_percent=max_budget_decrease_percent,
        )
    if not accounts:
        raise ValueError("Account registry is empty.")
    return accounts


def resolve_account(
    account_slug: str,
    config_path: Path = DEFAULT_ACCOUNTS_CONFIG_PATH,
) -> MetaAdsAccount:
    normalized_slug = slugify_name(account_slug)
    accounts = load_account_registry(config_path)
    if normalized_slug not in accounts:
        raise KeyError(
            f"Account '{normalized_slug}' was not found in {config_path}. "
            "Add it to config/meta_ads_accounts.json first."
        )
    return accounts[normalized_slug]


def _normalize_ad_account_id(value: str) -> str:
    if not value:
        return ""
    normalized = value.strip()
    if normalized.startswith("act_"):
        return normalized
    if normalized.isdigit():
        return f"act_{normalized}"
    return normalized


def _none_if_blank(value: object) -> str | None:
    raw = str(value or "").strip()
    return raw or None
