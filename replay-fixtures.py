"""Replay fictional HTTP fixtures from a patched Airweave backend directory.

Uses the repository's unit-test settings and httpx.MockTransport. No provider,
file converter, embedder, vector database, or search service is contacted.
"""

import argparse
import asyncio
import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx


def _handler(
    fixture: dict[str, Any], requests: list[dict[str, str]]
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a local HTTP fixture handler that records no authentication headers."""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append({"method": request.method, "url": str(request.url)})
        if request.url.path == "/v1/calls":
            return httpx.Response(
                200,
                json={
                    "calls": fixture["calls"],
                    "meta": {
                        "total": 3,
                        "current_page": 1,
                        "count": 3,
                        "per_page": 50,
                        "next_page_link": None,
                    },
                },
            )
        for kind in ("transcription", "summary"):
            if request.url.path == f"/v1/calls/812/{kind}":
                return httpx.Response(200, json={kind: fixture[kind]})
        return httpx.Response(404)

    return handle


async def _metadata() -> str:
    """Serialize metadata using the actual registries and source service."""
    from airweave.core.config import settings
    from airweave.domains.entities.registry import EntityDefinitionRegistry
    from airweave.domains.sources.registry import SourceRegistry
    from airweave.domains.sources.service import SourceService

    entities = EntityDefinitionRegistry()
    entities.build()
    providers = MagicMock()
    providers.list_all.return_value = []
    sources = SourceRegistry(providers, entities)
    sources.build()
    context = MagicMock()
    context.organization.enabled_features = []
    metadata = await SourceService(sources, settings).get("aircall", context)
    return metadata.model_dump_json(indent=2)


def _write_outputs(
    out: Path,
    entities: list[Any],
    hashes: dict[str, str | None],
    requests: list[dict[str, str]],
    metadata: str,
) -> None:
    """Write observable fixture outputs to an explicitly selected directory."""
    out.mkdir(parents=True, exist_ok=True)
    records = [entity.model_dump(mode="json") for entity in entities]
    (out / "example-entities.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    text = (
        "# Fictional calls processed by the actual Airweave text builder\n\n"
        "Offline HTTP fixtures only. This is text prepared for indexing; "
        "no vector database or search query was executed.\n\n"
    )
    text += "\n\n---\n\n".join(entity.textual_representation for entity in entities)
    (out / "example-searchable-text.md").write_text(text + "\n", encoding="utf-8")
    (out / "example-source-metadata.json").write_text(metadata + "\n", encoding="utf-8")
    replay = {
        "transport": "httpx.MockTransport; no network",
        "native_http_client": True,
        "native_source_registries": True,
        "native_text_builder_batch": True,
        "native_hash_computer": True,
        "file_or_audio_conversion_invoked": False,
        "produced_entity_ids": [entity.entity_id for entity in entities],
        "content_hashes": hashes,
        "fictional_requests": requests,
        "live_aircall": False,
        "vector_index": False,
    }
    (out / "offline-replay.json").write_text(json.dumps(replay, indent=2) + "\n", encoding="utf-8")


async def _replay(backend: Path, out: Path) -> None:
    """Run the native source, text preparation, and change-detection hash stages."""
    from airweave.domains.sources.token_providers.credential import DirectCredentialProvider
    from airweave.domains.sync_pipeline.pipeline.hash_computer import HashComputer
    from airweave.domains.sync_pipeline.pipeline.text_builder import TextualRepresentationBuilder
    from airweave.platform.configs.auth import AircallAuthConfig
    from airweave.platform.configs.config import AircallConfig
    from airweave.platform.http_client.airweave_client import AirweaveHttpClient
    from airweave.platform.sources.aircall import AircallSource

    fixture = json.loads(
        (backend / "tests/unit/platform/sources/fixtures/aircall.json").read_text(encoding="utf-8")
    )
    requests: list[dict[str, str]] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(fixture, requests))
    ) as client:
        source = await AircallSource.create(
            auth=DirectCredentialProvider(
                AircallAuthConfig(api_id="fictional-id", api_token="fictional-token"),
                source_short_name="aircall",
            ),
            logger=MagicMock(),
            http_client=AirweaveHttpClient(
                client, UUID(int=0), "aircall", feature_flag_enabled=False
            ),
            config=AircallConfig(include_transcriptions=True, include_summaries=True),
        )
        entities = [entity async for entity in source.generate_entities()]
    converters = MagicMock()
    builder = TextualRepresentationBuilder(converter_registry=converters)
    context = MagicMock(source_short_name="aircall")
    runtime = MagicMock(entity_tracker=AsyncMock())
    entities = await builder.build_for_batch(entities, context, runtime)
    if [entity.entity_id for entity in entities] != ["812", "813"] or converters.mock_calls:
        raise RuntimeError("Unexpected native entity output or a file converter was invoked.")
    if "un mois gratuit" not in entities[0].textual_representation:
        raise RuntimeError("The expected transcript passage was lost during text preparation.")
    hasher = HashComputer()
    hashes = {entity.entity_id: await hasher.compute_for_entity(entity) for entity in entities}
    _write_outputs(out, entities, hashes, requests, await _metadata())
    print(f"Offline replay: {len(entities)} native entities prepared for indexing. Output: {out}")


def main() -> None:
    """Parse CLI arguments before bootstrapping the patched backend's test settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    backend = Path.cwd()
    if not (backend / "airweave/platform/sources/aircall.py").is_file():
        parser.error("Run from the patched Airweave backend directory.")
    sys.path.insert(0, str(backend))
    runpy.run_path(str(backend / "conftest.py"))
    asyncio.run(_replay(backend, args.output_dir))


if __name__ == "__main__":
    main()
