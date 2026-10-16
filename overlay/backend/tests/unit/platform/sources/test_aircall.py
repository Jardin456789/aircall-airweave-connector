"""Offline tests of the native connector, real HTTP wrapper, registries, and text builder."""

import base64
import copy
import json
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from airweave.domains.entities.registry import EntityDefinitionRegistry
from airweave.domains.sources.exceptions import (
    SourceAuthError,
    SourceEntityForbiddenError,
    SourceEntityNotFoundError,
    SourceError,
    SourceRateLimitError,
    SourceServerError,
)
from airweave.domains.sources.lifecycle import SourceLifecycleService
from airweave.domains.sources.registry import SourceRegistry
from airweave.domains.sources.service import SourceService
from airweave.domains.sources.token_providers.credential import DirectCredentialProvider
from airweave.domains.sources.token_providers.static import StaticTokenProvider
from airweave.domains.sync_pipeline.pipeline.hash_computer import HashComputer
from airweave.domains.sync_pipeline.pipeline.text_builder import TextualRepresentationBuilder
from airweave.platform.configs._base import Fields
from airweave.platform.configs.auth import AircallAuthConfig
from airweave.platform.configs.config import AircallConfig
from airweave.platform.entities.aircall import AircallCallEntity
from airweave.platform.http_client.airweave_client import AirweaveHttpClient
from airweave.platform.sources.aircall import AIRCALL_API_BASE, AircallSource
from airweave.schemas.source_connection import SourceConnectionCreate

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "aircall.json").read_text())


def page(calls, *, current=1, total=None, next_link=None):
    return {
        "calls": calls,
        "meta": {
            "total": len(calls) if total is None else total,
            "count": len(calls),
            "current_page": current,
            "per_page": 50,
            "next_page_link": next_link,
        },
    }


@pytest.fixture
async def make_source():
    clients = []

    async def factory(handler, config=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        wrapped = AirweaveHttpClient(
            wrapped_client=client,
            org_id=UUID(int=0),
            source_short_name="aircall",
            feature_flag_enabled=False,
        )
        auth = DirectCredentialProvider(
            AircallAuthConfig(api_id="fictional-id", api_token="fictional-token"),
            source_short_name="aircall",
        )
        return await AircallSource.create(
            auth=auth,
            logger=MagicMock(),
            http_client=wrapped,
            config=config or AircallConfig(include_transcriptions=True, include_summaries=True),
        )

    yield factory
    for client in clients:
        await client.aclose()


async def test_native_registry_contract_and_embeddable_schema():
    entities = EntityDefinitionRegistry()
    entities.build()
    auth_providers = MagicMock()
    auth_providers.list_all.return_value = []
    sources = SourceRegistry(auth_providers, entities)
    sources.build()
    entry = sources.get("aircall")
    assert entry.source_class_ref is AircallSource
    assert entry.auth_methods == ["direct"]
    assert entry.runtime_auth_all_fields == ["api_id", "api_token"]
    assert entry.output_entity_definitions == ["aircall_call_entity"]
    assert not entry.supports_continuous
    schema = entities.get("aircall_call_entity").entity_schema["properties"]
    assert schema["call_id"]["is_entity_id"] is True
    assert schema["content"]["embeddable"] is True
    context = MagicMock()
    context.organization.enabled_features = []
    catalog = await SourceService(sources, MagicMock(ENVIRONMENT="self-hosted")).get(
        "aircall", context
    )
    token = next(field for field in catalog.auth_fields.fields if field.name == "api_token")
    assert token.is_secret is True
    request = SourceConnectionCreate.model_validate(
        {
            "name": "Aircall customer calls",
            "short_name": "aircall",
            "readable_collection_id": "aircall-demo",
            "authentication": {
                "credentials": {"api_id": "fictional-id", "api_token": "fictional-token"}
            },
            "config": {"include_transcriptions": True, "include_summaries": False},
        }
    )
    assert request.authentication.credentials["api_id"] == "fictional-id"
    assert request.sync_immediately is True


async def test_pagination_and_actual_airweave_text_building(make_source):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/calls":
            if request.url.params.get("page", "1") == "1":
                body = page(
                    [FIXTURE["calls"][0], FIXTURE["calls"][2]],
                    total=3,
                    next_link="https://api.aircall.io/v1/calls?page=2",
                )
            else:
                body = page([FIXTURE["calls"][1]], current=2, total=3)
            return httpx.Response(200, json=body)
        if request.url.path == "/v1/calls/812/transcription":
            assert request.url.params["mode"] == "async"
            return httpx.Response(200, json={"transcription": FIXTURE["transcription"]})
        if request.url.path == "/v1/calls/812/summary":
            return httpx.Response(200, json={"summary": FIXTURE["summary"]})
        return httpx.Response(404, json={"error": "Not found"})

    source = await make_source(
        handler,
        AircallConfig(
            from_timestamp=1,
            to_timestamp=1792000000,
            include_transcriptions=True,
            include_summaries=True,
        ),
    )
    await source.validate()
    requests.clear()
    entities = [entity async for entity in source.generate_entities()]
    assert [entity.entity_id for entity in entities] == ["812", "813"]
    entity = entities[0]
    assert isinstance(entity, AircallCallEntity)
    assert entity.duration_seconds == 68 and entity.talk_time_seconds == 60
    assert entity.contact_name == "Atelier Exemple" and entity.agent_name == "Demo Agent"
    assert entity.created_at.tzinfo is timezone.utc
    assert entity.updated_at > entity.created_at
    assert entity.utterances[1]["start_time"] == 4.1
    assert entity.web_url == f"{AIRCALL_API_BASE}/calls/812"
    assert "signature=" not in entity.model_dump_json()
    converters = MagicMock()
    builder = TextualRepresentationBuilder(converter_registry=converters)
    context = MagicMock(source_short_name="aircall")
    runtime = MagicMock(entity_tracker=AsyncMock())
    prepared = await builder.build_for_batch(entities, context, runtime)
    assert [item.entity_id for item in prepared] == ["812", "813"]
    converters.for_extension.assert_not_called()
    converters.for_web.assert_not_called()
    text = prepared[0].textual_representation
    assert "Nous vous offrons un mois gratuit" in text
    assert "[4.1s] internal:" in text
    assert "Confirm the commercial offer." in text
    assert "one free month" in text
    assert entities[1].talk_time_seconds is None
    assert entities[1].contact_name is None
    assert entities[1].phone_number == "+12025550102"
    expected_auth = "Basic " + base64.b64encode(b"fictional-id:fictional-token").decode()
    assert all(request.headers["Authorization"] == expected_auth for request in requests)
    lists = [request for request in requests if request.url.path == "/v1/calls"]
    assert [request.url.params["page"] for request in lists] == ["1", "2"]
    assert all(request.url.params["from"] == "1" for request in lists)
    assert all(request.url.params["to"] == "1792000000" for request in lists)


async def test_late_transcript_is_picked_up_with_stable_identity(make_source):
    available = False

    def handler(request):
        if request.url.path == "/v1/calls":
            return httpx.Response(200, json=page([FIXTURE["calls"][0]]))
        if available:
            return httpx.Response(200, json={"transcription": FIXTURE["transcription"]})
        return httpx.Response(404)

    source = await make_source(
        handler, AircallConfig(include_transcriptions=True, include_summaries=False)
    )
    before = [entity async for entity in source.generate_entities()]
    available = True
    after = [entity async for entity in source.generate_entities()]
    again = [entity async for entity in source.generate_entities()]
    assert before[0].entity_id == after[0].entity_id
    assert before[0].utterances == []
    assert "un mois gratuit" in after[0].content
    assert after[0].model_dump() == again[0].model_dump()
    hasher = HashComputer()
    assert await hasher.compute_for_entity(before[0]) != await hasher.compute_for_entity(after[0])
    assert await hasher.compute_for_entity(after[0]) == await hasher.compute_for_entity(again[0])


async def test_metadata_only_mode_never_requests_ai_endpoints(make_source):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=page([FIXTURE["calls"][1]]))

    source = await make_source(
        handler, AircallConfig(include_transcriptions=False, include_summaries=False)
    )
    entities = [entity async for entity in source.generate_entities()]
    assert paths == ["/v1/calls"]
    assert entities[0].missed_call_reason == "agents_did_not_answer"


@pytest.mark.parametrize(
    "status,exception",
    [(401, SourceAuthError), (403, SourceEntityForbiddenError), (404, SourceEntityNotFoundError)],
)
async def test_calls_failures_are_not_empty_successes(make_source, status, exception):
    source = await make_source(lambda request: httpx.Response(status, json={"error": "test"}))
    with pytest.raises(exception):
        _ = [entity async for entity in source.generate_entities()]


async def test_optional_403_is_not_treated_as_missing_transcript(make_source):
    def handler(request):
        if request.url.path == "/v1/calls":
            return httpx.Response(200, json=page([FIXTURE["calls"][0]]))
        return httpx.Response(403, json={"error": "Company not verified"})

    source = await make_source(handler)
    with pytest.raises(SourceEntityForbiddenError):
        _ = [entity async for entity in source.generate_entities()]


@pytest.mark.parametrize("status", [429, 500])
async def test_transient_failures_retry_and_recover(make_source, monkeypatch, status):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status, headers={"Retry-After": "2"})
        return httpx.Response(200, json=page([]))

    sleep = AsyncMock()
    monkeypatch.setattr(AircallSource._get.retry, "sleep", sleep)
    source = await make_source(handler)
    await source.validate()
    assert attempts == 2
    assert sleep.await_count == 1
    assert float(sleep.await_args.args[0]) >= 2


async def test_exhausted_server_failure_propagates(make_source, monkeypatch):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    monkeypatch.setattr(AircallSource._get.retry, "sleep", AsyncMock())
    source = await make_source(handler)
    with pytest.raises(SourceServerError):
        await source.validate()
    assert attempts == 5


async def test_aircall_reset_header_is_respected(make_source, monkeypatch):
    source = await make_source(lambda request: httpx.Response(200, json={}))
    monkeypatch.setattr("airweave.platform.sources.aircall.time.time", lambda: 100.0)
    response = httpx.Response(429, headers={"X-AircallApi-Reset": "165"})
    with pytest.raises(SourceRateLimitError) as error:
        source._raise_for_status(response)
    assert error.value.retry_after == 65


@pytest.mark.parametrize(
    "body,message",
    [
        (
            {"calls": [], "meta": {"total": 10001, "current_page": 1, "next_page_link": None}},
            "10,000",
        ),
        (
            {"calls": [], "meta": {"current_page": 7, "total": 0, "next_page_link": None}},
            "unexpected page",
        ),
        (
            {"calls": [], "meta": {"current_page": 1, "total": 1, "next_page_link": "next"}},
            "cannot advance",
        ),
        ({"calls": []}, "invalid calls pagination"),
        (
            {"calls": [], "meta": {"current_page": 1, "total": 2, "next_page_link": None}},
            "before all calls",
        ),
        ({"calls": [], "meta": {"current_page": 1, "total": 0}}, "invalid calls pagination"),
    ],
)
async def test_incomplete_pagination_fails_loudly(make_source, body, message):
    source = await make_source(lambda request: httpx.Response(200, json=body))
    with pytest.raises(SourceError, match=message):
        _ = [entity async for entity in source.generate_entities()]


async def test_provider_next_link_never_receives_credentials(make_source):
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=page(
                    [FIXTURE["calls"][2]], total=2, next_link="https://elsewhere.invalid/collect"
                ),
            )
        other_call = {**FIXTURE["calls"][2], "id": 815}
        return httpx.Response(200, json=page([other_call], current=2, total=2))

    source = await make_source(handler)
    assert [entity async for entity in source.generate_entities()] == []
    assert hosts == ["api.aircall.io", "api.aircall.io"]


async def test_mismatched_transcript_call_id_is_rejected(make_source):
    transcript = copy.deepcopy(FIXTURE["transcription"])
    transcript["call_id"] = 999

    def handler(request):
        if request.url.path == "/v1/calls":
            return httpx.Response(200, json=page([FIXTURE["calls"][0]]))
        return httpx.Response(200, json={"transcription": transcript})

    source = await make_source(handler)
    with pytest.raises(SourceError, match="another call"):
        _ = [entity async for entity in source.generate_entities()]


@pytest.mark.parametrize("field,value", [("api_id", " "), ("api_token", "")])
def test_empty_credentials_are_rejected(field, value):
    credentials = {"api_id": "fictional-id", "api_token": "fictional-token", field: value}
    with pytest.raises(ValidationError):
        AircallAuthConfig(**credentials)


def test_invalid_time_range_is_rejected():
    with pytest.raises(ValidationError, match="from_timestamp"):
        AircallConfig(from_timestamp=20, to_timestamp=10)


@pytest.mark.parametrize(
    "auth",
    [
        StaticTokenProvider("fictional-token", source_short_name="aircall"),
        DirectCredentialProvider({"api_id": "fictional-id"}, source_short_name="aircall"),
    ],
)
async def test_unsupported_auth_fails_with_domain_error(auth):
    with pytest.raises(SourceError, match="Aircall requires"):
        await AircallSource.create(
            auth=auth, logger=MagicMock(), http_client=MagicMock(), config=AircallConfig()
        )


@pytest.mark.parametrize("same_page", [True, False])
async def test_duplicate_call_ids_cannot_complete_a_full_sync(make_source, same_page):
    call = FIXTURE["calls"][0]

    def handler(request):
        current = int(request.url.params["page"])
        calls = [call, call] if same_page else [call]
        next_link = "next" if not same_page and current == 1 else None
        return httpx.Response(200, json=page(calls, current=current, total=2, next_link=next_link))

    source = await make_source(
        handler, AircallConfig(include_transcriptions=False, include_summaries=False)
    )
    with pytest.raises(SourceError, match="duplicate call"):
        _ = [entity async for entity in source.generate_entities()]


async def test_untouched_creation_form_matches_backend_defaults(make_source):
    # SourceConfigView initializes booleans from field.default, which the current
    # ConfigField schema does not expose; the untouched form therefore submits false.
    fields = Fields.from_config_class(AircallConfig).fields
    payload = {field.name: False for field in fields if field.type == "boolean"}
    from_form = AircallConfig.model_validate(payload)
    assert from_form == AircallConfig()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=page([FIXTURE["calls"][0]]))

    source = await make_source(handler, from_form)
    assert len([entity async for entity in source.generate_entities()]) == 1
    assert paths == ["/v1/calls"]


async def test_malformed_call_object_fails_with_domain_error(make_source):
    source = await make_source(lambda request: httpx.Response(200, json=page(["invalid call"])))
    with pytest.raises(SourceError, match="invalid call"):
        _ = [entity async for entity in source.generate_entities()]


def test_summary_timestamp_without_offset_is_treated_as_utc():
    summary = {**FIXTURE["summary"], "created_at": "2026-10-14T11:00:00"}
    entity = AircallCallEntity.from_api(FIXTURE["calls"][0], summary=summary)
    assert entity.updated_at.tzinfo is timezone.utc


@pytest.mark.parametrize(
    "call",
    [
        {"id": 812, "direction": "outbound"},
        {"id": 812, "direction": "outbound", "status": "unexpected"},
        {"id": 812, "status": "done"},
    ],
)
async def test_missing_call_state_cannot_look_like_an_empty_sync(make_source, call):
    source = await make_source(lambda request: httpx.Response(200, json=page([call])))
    with pytest.raises(SourceError, match="invalid call state"):
        _ = [entity async for entity in source.generate_entities()]


async def test_redirect_does_not_forward_basic_credentials(make_source):
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://elsewhere.invalid/collect"})

    source = await make_source(handler)
    with pytest.raises(SourceError, match="redirect"):
        await source.validate()
    assert hosts == ["api.aircall.io"]


async def test_lifecycle_normalizes_api_credentials_and_validates_the_native_source(monkeypatch):
    entities = EntityDefinitionRegistry()
    entities.build()
    providers = MagicMock()
    providers.list_all.return_value = []
    registry = SourceRegistry(providers, entities)
    registry.build()
    service = SourceLifecycleService(
        registry, providers, MagicMock(), MagicMock(), MagicMock(), MagicMock()
    )
    original_client = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=page([]))

    class FixtureClient(original_client):
        def __init__(self):
            super().__init__(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("airweave.domains.sources.lifecycle.httpx.AsyncClient", FixtureClient)
    await service.validate(
        "aircall",
        {"api_id": "fictional-id", "api_token": "fictional-token"},
        {"include_transcriptions": True, "include_summaries": False},
    )
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/calls"
    expected = "Basic " + base64.b64encode(b"fictional-id:fictional-token").decode()
    assert requests[0].headers["Authorization"] == expected
