"""Meta Marketing API client.

Reporting sync is read-only. The ad set targeting methods (``list_adsets``,
``get_adset``, ``update_adset``) are the only write-capable surface and require
an access token with the ``ads_management`` permission. Read-only insights sync
only needs ``ads_read``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - exercised only in minimal local envs
    requests = None

from .config import (
    DEFAULT_GRAPH_API_ROOT,
    DEFAULT_META_API_TIMEOUT_SECONDS,
    DEFAULT_META_API_VERSION,
)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class MetaApiError(RuntimeError):
    """Raised when the Meta API returns an operator-actionable error."""


def client_from_env(api_version: str | None = None) -> "MetaMarketingApiClient":
    """Build a client from META_ACCESS_TOKEN / META_API_VERSION environment variables."""
    access_token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    effective_version = api_version or os.environ.get("META_API_VERSION") or DEFAULT_META_API_VERSION
    return MetaMarketingApiClient(access_token=access_token, api_version=effective_version)


class MetaMarketingApiClient:
    def __init__(
        self,
        access_token: str,
        api_version: str = DEFAULT_META_API_VERSION,
        *,
        session: requests.Session | None = None,
        graph_api_root: str = DEFAULT_GRAPH_API_ROOT,
        timeout_seconds: int = DEFAULT_META_API_TIMEOUT_SECONDS,
        max_retries: int = 3,
    ) -> None:
        if not access_token.strip():
            raise MetaApiError("META_ACCESS_TOKEN is required for Meta API sync.")
        self.access_token = access_token
        self.api_version = api_version
        if session is not None:
            self.session = session
        elif requests is not None:
            self.session = requests.Session()
        else:
            raise MetaApiError(
                "The 'requests' package is required for live Meta API sync. "
                "Install project dependencies with `pip install -e .[dev]`."
            )
        self.graph_api_root = graph_api_root.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def fetch_insights(
        self,
        ad_account_id: str,
        *,
        fields: list[str],
        date_from: str,
        date_to: str,
        level: str = "ad",
        time_increment: int = 1,
    ) -> list[dict[str, Any]]:
        params = {
            "fields": ",".join(fields),
            "level": level,
            "time_increment": time_increment,
            "time_range": json.dumps({"since": date_from, "until": date_to}),
            "limit": 500,
        }
        return list(self.iter_paginated(f"/{ad_account_id}/insights", params=params))

    def fetch_ads(
        self,
        ad_account_id: str,
        *,
        fields: list[str],
    ) -> list[dict[str, Any]]:
        params = {
            "fields": ",".join(fields),
            "limit": 500,
        }
        return list(self.iter_paginated(f"/{ad_account_id}/ads", params=params))

    def list_adsets(
        self,
        ad_account_id: str,
        *,
        fields: list[str],
        effective_status: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Enumerate ad sets in an account, optionally filtered by effective status."""
        params: dict[str, Any] = {
            "fields": ",".join(fields),
            "limit": 200,
        }
        if effective_status:
            params["effective_status"] = json.dumps(list(effective_status))
        return list(self.iter_paginated(f"/{ad_account_id}/adsets", params=params))

    def get_adset(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]:
        """Fetch a single ad set's current state (used to re-read just before a write)."""
        params = {"fields": ",".join(fields), "access_token": self.access_token}
        return self._get_json(self._make_url(f"/{adset_id}"), params=params)

    def get_ad(self, ad_id: str, *, fields: list[str]) -> dict[str, Any]:
        """Fetch a single ad's current state."""
        params = {"fields": ",".join(fields), "access_token": self.access_token}
        return self._get_json(self._make_url(f"/{ad_id}"), params=params)

    def update_ad(self, ad_id: str, *, params: dict[str, Any]) -> dict[str, Any]:
        """POST an ad update (e.g. status). Requires an ``ads_management``-scoped token."""
        encoded: dict[str, Any] = {"access_token": self.access_token}
        for key, value in params.items():
            encoded[key] = value if isinstance(value, str) else json.dumps(value)
        return self._post_json(self._make_url(f"/{ad_id}"), data=encoded)

    def update_adset(self, adset_id: str, *, params: dict[str, Any]) -> dict[str, Any]:
        """POST an ad set update. Requires an ``ads_management``-scoped token.

        ``params`` values that are not plain strings are JSON-encoded, which is how
        the Graph API expects structured fields such as ``targeting``.
        """
        encoded: dict[str, Any] = {"access_token": self.access_token}
        for key, value in params.items():
            encoded[key] = value if isinstance(value, str) else json.dumps(value)
        return self._post_json(self._make_url(f"/{adset_id}"), data=encoded)

    def iter_paginated(
        self,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        next_url = self._make_url(path_or_url)
        next_params = dict(params or {})
        next_params.setdefault("access_token", self.access_token)

        while next_url:
            payload = self._get_json(next_url, params=next_params)
            data = payload.get("data")
            if not isinstance(data, list):
                raise MetaApiError("Meta API response did not contain a data array.")
            for item in data:
                if isinstance(item, dict):
                    yield item
            next_url = payload.get("paging", {}).get("next")
            next_params = None

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout_seconds)
            except Exception as exc:
                last_error = str(exc)
                if attempt >= self.max_retries:
                    break
                time.sleep(2**attempt)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                time.sleep(2**attempt)
                continue

            if response.status_code >= 400:
                raise MetaApiError(self._format_error(response))

            try:
                payload = response.json()
            except ValueError as exc:
                raise MetaApiError(f"Meta API returned non-JSON response from {url}: {exc}") from exc
            if not isinstance(payload, dict):
                raise MetaApiError(f"Meta API returned an unexpected response shape from {url}.")
            return payload

        raise MetaApiError(f"Meta API request failed after retries: {last_error or url}")

    def _post_json(self, url: str, data: dict[str, Any]) -> dict[str, Any]:
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(url, data=data, timeout=self.timeout_seconds)
            except Exception as exc:
                last_error = str(exc)
                if attempt >= self.max_retries:
                    break
                time.sleep(2**attempt)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                time.sleep(2**attempt)
                continue

            if response.status_code >= 400:
                raise MetaApiError(self._format_error(response))

            try:
                payload = response.json()
            except ValueError as exc:
                raise MetaApiError(f"Meta API returned non-JSON response from {url}: {exc}") from exc
            if not isinstance(payload, dict):
                raise MetaApiError(f"Meta API returned an unexpected response shape from {url}.")
            return payload

        raise MetaApiError(f"Meta API request failed after retries: {last_error or url}")

    def _make_url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        path = path_or_url.lstrip("/")
        return f"{self.graph_api_root}/{self.api_version}/{path}"

    def _format_error(self, response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = error.get("message") or response.text or "Unknown Meta API error"
        code = error.get("code")
        subcode = error.get("error_subcode")
        pieces = [f"Meta API request failed with HTTP {response.status_code}: {message}"]
        if code is not None:
            pieces.append(f"(code={code})")
        if subcode is not None:
            pieces.append(f"(subcode={subcode})")
        return " ".join(pieces)
