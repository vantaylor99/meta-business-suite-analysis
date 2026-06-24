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
        time_increment: int | str = 1,
        breakdowns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "fields": ",".join(fields),
            "level": level,
            "time_increment": time_increment,
            "time_range": json.dumps({"since": date_from, "until": date_to}),
            "limit": 500,
        }
        if breakdowns:
            params["breakdowns"] = ",".join(breakdowns)
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

    def list_custom_audiences(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        """List the custom audiences available in the account (read-only)."""
        params = {"fields": ",".join(fields), "limit": 200}
        return list(self.iter_paginated(f"/{ad_account_id}/customaudiences", params=params))

    def get_account(self, ad_account_id: str, *, fields: list[str]) -> dict[str, Any]:
        """Fetch account-level info (status, currency, spend cap, amount spent, funding)."""
        params = {"fields": ",".join(fields), "access_token": self.access_token}
        return self._get_json(self._make_url(f"/{ad_account_id}"), params=params)

    def get_delivery_estimate(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]:
        """Estimated audience reach/size for an ad set's current targeting."""
        params = {"fields": ",".join(fields), "access_token": self.access_token}
        return self._get_json(self._make_url(f"/{adset_id}/delivery_estimate"), params=params)

    def search_targeting(self, *, query: str, search_type: str = "adinterest", limit: int = 25) -> list[dict[str, Any]]:
        """Search Meta's targeting catalog (default: detailed-targeting interests)."""
        params = {"type": search_type, "q": query, "limit": limit}
        return list(self.iter_paginated("/search", params=params))

    def list_pixels(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        params = {"fields": ",".join(fields), "limit": 100}
        return list(self.iter_paginated(f"/{ad_account_id}/adspixels", params=params))

    def list_custom_conversions(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        params = {"fields": ",".join(fields), "limit": 100}
        return list(self.iter_paginated(f"/{ad_account_id}/customconversions", params=params))

    def create_campaign(self, ad_account_id: str, *, params: dict[str, Any], validate_only: bool = False) -> dict[str, Any]:
        """Create a campaign (requires ``ads_management``)."""
        return self._post_json(
            self._make_url(f"/{ad_account_id}/campaigns"), data=self._encode_write_params(params, validate_only)
        )

    def create_adset(self, ad_account_id: str, *, params: dict[str, Any], validate_only: bool = False) -> dict[str, Any]:
        """Create an ad set (requires ``ads_management``)."""
        return self._post_json(
            self._make_url(f"/{ad_account_id}/adsets"), data=self._encode_write_params(params, validate_only)
        )

    def create_ad(self, ad_account_id: str, *, params: dict[str, Any], validate_only: bool = False) -> dict[str, Any]:
        """Create an ad (requires ``ads_management``)."""
        return self._post_json(
            self._make_url(f"/{ad_account_id}/ads"), data=self._encode_write_params(params, validate_only)
        )

    def create_custom_audience(
        self, ad_account_id: str, *, params: dict[str, Any], validate_only: bool = False
    ) -> dict[str, Any]:
        """Create a custom or lookalike audience (requires ``ads_management``)."""
        return self._post_json(
            self._make_url(f"/{ad_account_id}/customaudiences"), data=self._encode_write_params(params, validate_only)
        )

    def create_ad_creative(self, ad_account_id: str, *, params: dict[str, Any], validate_only: bool = False) -> dict[str, Any]:
        """Create an ad creative (e.g. an object_story_spec) reusable across ads."""
        return self._post_json(
            self._make_url(f"/{ad_account_id}/adcreatives"), data=self._encode_write_params(params, validate_only)
        )

    def upload_video(self, ad_account_id: str, *, file_path: str, name: str | None = None) -> dict[str, Any]:
        """Upload a video file to the ad account's media library. Returns {'id': <video_id>}.

        Simple (non-resumable) multipart upload — fine for typical ad videos. Meta then
        processes the video asynchronously; poll ``get_video`` for status before using it.
        """
        url = self._make_url(f"/{ad_account_id}/advideos")
        data: dict[str, Any] = {"access_token": self.access_token}
        if name:
            data["name"] = name
        with open(file_path, "rb") as handle:
            response = self.session.post(
                url, data=data, files={"source": handle}, timeout=max(self.timeout_seconds, 600)
            )
        return self._read_response(response, url)

    def get_video(self, video_id: str, *, fields: list[str]) -> dict[str, Any]:
        """Fetch a video's processing status/metadata (e.g. fields=['status'])."""
        params = {"fields": ",".join(fields), "access_token": self.access_token}
        return self._get_json(self._make_url(f"/{video_id}"), params=params)

    def upload_image(self, ad_account_id: str, *, file_path: str) -> dict[str, Any]:
        """Upload an image; returns the Graph API ``images`` map keyed by filename (with 'hash')."""
        url = self._make_url(f"/{ad_account_id}/adimages")
        with open(file_path, "rb") as handle:
            response = self.session.post(
                url, data={"access_token": self.access_token}, files={"filename": handle},
                timeout=max(self.timeout_seconds, 120),
            )
        return self._read_response(response, url)

    def _read_response(self, response: "requests.Response", url: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise MetaApiError(self._format_error(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetaApiError(f"Meta API returned non-JSON response from {url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise MetaApiError(f"Meta API returned an unexpected response shape from {url}.")
        return payload

    def list_campaigns(
        self,
        ad_account_id: str,
        *,
        fields: list[str],
        effective_status: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"fields": ",".join(fields), "limit": 200}
        if effective_status:
            params["effective_status"] = json.dumps(list(effective_status))
        return list(self.iter_paginated(f"/{ad_account_id}/campaigns", params=params))

    def get_campaign(self, campaign_id: str, *, fields: list[str]) -> dict[str, Any]:
        params = {"fields": ",".join(fields), "access_token": self.access_token}
        return self._get_json(self._make_url(f"/{campaign_id}"), params=params)

    def update_campaign(
        self,
        campaign_id: str,
        *,
        params: dict[str, Any],
        validate_only: bool = False,
    ) -> dict[str, Any]:
        """POST a campaign update (e.g. status, name). Requires ``ads_management``."""
        return self._post_json(
            self._make_url(f"/{campaign_id}"),
            data=self._encode_write_params(params, validate_only),
        )

    def get_ad(self, ad_id: str, *, fields: list[str]) -> dict[str, Any]:
        """Fetch a single ad's current state."""
        params = {"fields": ",".join(fields), "access_token": self.access_token}
        return self._get_json(self._make_url(f"/{ad_id}"), params=params)

    def update_ad(
        self,
        ad_id: str,
        *,
        params: dict[str, Any],
        validate_only: bool = False,
    ) -> dict[str, Any]:
        """POST an ad update (e.g. status). Requires an ``ads_management``-scoped token.

        When ``validate_only`` is set, Meta validates without persisting the change.
        """
        return self._post_json(
            self._make_url(f"/{ad_id}"),
            data=self._encode_write_params(params, validate_only),
        )

    def _encode_write_params(self, params: dict[str, Any], validate_only: bool) -> dict[str, Any]:
        encoded: dict[str, Any] = {"access_token": self.access_token}
        for key, value in params.items():
            encoded[key] = value if isinstance(value, str) else json.dumps(value)
        if validate_only:
            encoded["execution_options"] = json.dumps(["validate_only"])
        return encoded

    def update_adset(
        self,
        adset_id: str,
        *,
        params: dict[str, Any],
        validate_only: bool = False,
    ) -> dict[str, Any]:
        """POST an ad set update. Requires an ``ads_management``-scoped token.

        ``params`` values that are not plain strings are JSON-encoded, which is how
        the Graph API expects structured fields such as ``targeting``. When
        ``validate_only`` is set, Meta runs validation and returns the result without
        persisting any change (``execution_options=['validate_only']``).
        """
        return self._post_json(
            self._make_url(f"/{adset_id}"),
            data=self._encode_write_params(params, validate_only),
        )

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
