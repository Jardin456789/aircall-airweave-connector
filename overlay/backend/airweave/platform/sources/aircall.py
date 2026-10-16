"""Native Aircall connector using Airweave's v2 source contract.

Imports calls and available post-call text with a private account's Basic auth.
Public multi-customer OAuth installation and webhooks are outside this version.
API reference: https://developers.aircall.io/api-references
"""

from __future__ import annotations

import time
from typing import Any, AsyncGenerator, Iterator

import httpx
from tenacity import retry, stop_after_attempt

from airweave.core.logging import ContextualLogger
from airweave.core.shared_models import RateLimitLevel
from airweave.domains.browse_tree.types import NodeSelectionData
from airweave.domains.sources.exceptions import SourceError, SourceRateLimitError
from airweave.domains.sources.token_providers.credential import DirectCredentialProvider
from airweave.domains.sources.token_providers.protocol import SourceAuthProvider
from airweave.domains.storage.file_service import FileService
from airweave.domains.syncs.cursors.cursor import SyncCursor
from airweave.platform.configs.auth import AircallAuthConfig
from airweave.platform.configs.config import AircallConfig
from airweave.platform.decorators import source
from airweave.platform.entities.aircall import AircallCallEntity
from airweave.platform.http_client.airweave_client import AirweaveHttpClient
from airweave.platform.sources._base import BaseSource
from airweave.platform.sources.http_helpers import raise_for_status
from airweave.platform.sources.retry_helpers import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from airweave.schemas.source_connection import AuthenticationMethod

AIRCALL_API_BASE = "https://api.aircall.io/v1"
PAGE_SIZE = 50
MAX_CALLS = 10_000


@source(
    name="Aircall",
    short_name="aircall",
    auth_methods=[AuthenticationMethod.DIRECT],
    auth_config_class=AircallAuthConfig,
    config_class=AircallConfig,
    labels=["Telephony", "CRM", "Transcription"],
    supports_continuous=False,
    rate_limit_level=RateLimitLevel.ORG,
)
class AircallSource(BaseSource):
    """Sync finished calls, notes, tags, transcripts, and summaries from Aircall.

    Full re-syncs revisit calls so late transcripts are picked up. API errors fail
    the sync rather than masquerading as an empty collection.
    """

    @classmethod
    async def create(
        cls,
        *,
        auth: SourceAuthProvider,
        logger: ContextualLogger,
        http_client: AirweaveHttpClient,
        config: AircallConfig,
    ) -> AircallSource:
        """Construct a source with typed, Airweave-managed account credentials."""
        if not isinstance(auth, DirectCredentialProvider) or not isinstance(
            auth.credentials, AircallAuthConfig
        ):
            raise SourceError(
                "Aircall requires API ID and API token credentials for a private account.",
                source_short_name="aircall",
            )
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._basic_auth = httpx.BasicAuth(auth.credentials.api_id, auth.credentials.api_token)
        instance._config = config
        return instance

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, optional: bool = False
    ) -> dict[str, Any] | None:
        """GET a fixed Aircall resource; only optional 404 means unavailable content."""
        response = await self.http_client.get(
            f"{AIRCALL_API_BASE}{path}",
            auth=self._basic_auth,
            headers={"Accept": "application/json"},
            params=params or {},
            timeout=30.0,
            follow_redirects=False,
        )
        if optional and response.status_code == 404:
            return None
        self._raise_for_status(response)
        data = response.json()
        if not isinstance(data, dict):
            raise SourceError(
                "Aircall returned a non-object response.", source_short_name="aircall"
            )
        return data

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Respect Aircall's reset timestamp when Retry-After is absent."""
        if response.status_code == 429 and not response.headers.get("Retry-After"):
            try:
                reset = float(response.headers["X-AircallApi-Reset"])
            except (KeyError, ValueError):
                pass
            else:
                raise SourceRateLimitError(
                    retry_after=max(1.0, reset - time.time()), source_short_name="aircall"
                )
        raise_for_status(
            response, source_short_name="aircall", token_provider_kind=self.auth.provider_kind
        )

    async def _calls(self) -> AsyncGenerator[dict[str, Any], None]:
        """Paginate without following external links or silently exceeding 10,000 calls."""
        params: dict[str, Any] = {
            "per_page": PAGE_SIZE,
            "page": 1,
            "order": "asc",
            "fetch_contact": "true",
        }
        if self._config.from_timestamp is not None:
            params["from"] = self._config.from_timestamp
        if self._config.to_timestamp is not None:
            params["to"] = self._config.to_timestamp
        else:
            params["to"] = int(time.time())
        received = 0
        seen: set[int] = set()
        while True:
            data = await self._get("/calls", params)
            meta, calls = self._parse_page(data)
            if meta.get("current_page") != params["page"]:
                raise SourceError(
                    "Aircall returned an unexpected page.", source_short_name="aircall"
                )
            for call in self._unique_calls(calls, seen):
                yield call
            received += len(calls)
            if not meta.get("next_page_link"):
                if received != meta["total"]:
                    raise SourceError(
                        "Aircall pagination ended before all calls were retrieved.",
                        source_short_name="aircall",
                    )
                break
            if not calls or params["page"] * PAGE_SIZE >= MAX_CALLS:
                raise SourceError(
                    "Aircall pagination cannot advance safely.", source_short_name="aircall"
                )
            # Reconstruct requests to the fixed API origin; never forward credentials to a URL.
            params["page"] += 1

    @staticmethod
    def _unique_calls(calls: list[dict[str, Any]], seen: set[int]) -> Iterator[dict[str, Any]]:
        """Reject malformed or repeated IDs instead of reporting a partial full sync as complete."""
        for call in calls:
            if not isinstance(call, dict) or type(call.get("id")) is not int or call["id"] <= 0:
                raise SourceError("Aircall returned an invalid call.", source_short_name="aircall")
            if call.get("status") not in {"initial", "answered", "done"} or call.get(
                "direction"
            ) not in {"inbound", "outbound"}:
                raise SourceError(
                    "Aircall returned an invalid call state.", source_short_name="aircall"
                )
            if call["id"] in seen:
                raise SourceError(
                    "Aircall returned a duplicate call ID during pagination.",
                    source_short_name="aircall",
                )
            seen.add(call["id"])
            yield call

    @staticmethod
    def _parse_page(data: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Validate the pagination contract before treating an import as complete."""
        if (
            not data
            or not isinstance(data.get("calls"), list)
            or not isinstance(data.get("meta"), dict)
        ):
            raise SourceError(
                "Aircall returned invalid calls pagination.", source_short_name="aircall"
            )
        meta = data["meta"]
        if type(meta.get("total")) is not int or meta["total"] < 0 or "next_page_link" not in meta:
            raise SourceError(
                "Aircall returned invalid calls pagination.", source_short_name="aircall"
            )
        if meta["total"] > MAX_CALLS:
            raise SourceError(
                "Aircall pagination is limited to 10,000 calls. Narrow the configured "
                "from_timestamp/to_timestamp range and retry.",
                source_short_name="aircall",
            )
        return meta, data["calls"]

    async def _optional_content(self, call_id: int, kind: str) -> dict[str, Any] | None:
        """Retrieve optional text without hiding permission or account failures."""
        params = {"mode": "async"} if kind == "transcription" else None
        data = await self._get(f"/calls/{call_id}/{kind}", params, optional=True)
        if data is None:
            return None
        if kind not in data:
            raise SourceError(
                f"Aircall returned invalid {kind} content.", source_short_name="aircall"
            )
        content = data[kind]
        if content is None:
            return None
        if not isinstance(content, dict):
            raise SourceError(
                f"Aircall returned invalid {kind} content.", source_short_name="aircall"
            )
        if content.get("call_id") != call_id:
            raise SourceError(
                f"Aircall {kind} belongs to another call.", source_short_name="aircall"
            )
        return content

    async def generate_entities(
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[AircallCallEntity, None]:
        """Yield native entities for completed calls in the configured full-sync scope."""
        async for call in self._calls():
            if call.get("status") != "done":
                continue
            call_id = call["id"]
            transcription = (
                await self._optional_content(call_id, "transcription")
                if self._config.include_transcriptions
                else None
            )
            summary = (
                await self._optional_content(call_id, "summary")
                if self._config.include_summaries
                else None
            )
            yield AircallCallEntity.from_api(call, transcription, summary)

    async def validate(self) -> None:
        """Validate credentials and call-read permissions with a minimal request."""
        await self._get("/calls", {"per_page": 1})
