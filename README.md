# Aircall → Airweave

A native Aircall connector that imports customer calls and available transcripts into Airweave's indexing pipeline.

**[Connector source](overlay/backend/airweave/platform/sources/aircall.py)** · **[Tests](overlay/backend/tests/unit/platform/sources/test_aircall.py)** · **[Example output](examples/searchable-calls.md)**

Imagine asking an agent: *“What did we promise this customer on their last call?”* This connector supplies the call metadata, agent notes and, when enabled and available, timestamped transcript passages for that context.

## What it does

- Imports finished calls, contact/agent names when present, notes and tags.
- Optionally imports post-call transcriptions and AI summaries.
- Produces native `AircallCallEntity` records with stable IDs, embeddable text and Airweave content hashes.
- Handles pagination, late transcripts and transient API errors; rejects malformed or repeated call records instead of silently accepting an incomplete full sync.

This is an independent prototype targeting Airweave's public backend at [`1ebe1af`](https://github.com/airweave-ai/airweave/tree/1ebe1af2dbfb90f3334410721e69997e4f02b320). The repository contains the integration patch and the eight changed files under `overlay/`. Apply the patch to a complete Airweave checkout to run it.

## Validation

**35 connector tests and 207 targeted tests passed locally**, after applying the patch to a clean copy of the pinned upstream backend. The checks cover real Airweave registries, source lifecycle validation, the HTTP wrapper, text preparation and content hashing, using fictional HTTP fixtures.

The [verification notes](docs/validation.md) and [recorded results](evidence/verification.json) describe the scope. A live Aircall account, database-backed sync, vector indexing, retrieval and the current hosted Airweave backend have not been validated.

## Apply and run the tests

Requires Git, Python 3.13 and the normal Airweave backend development environment. From this repository's root:

```sh
git clone https://github.com/airweave-ai/airweave.git airweave
git -C airweave checkout 1ebe1af2dbfb90f3334410721e69997e4f02b320
git -C airweave apply --check "$PWD/native-connector.patch"
git -C airweave apply "$PWD/native-connector.patch"
cd airweave/backend
poetry install
poetry run pytest -o addopts= -o log_cli=false tests/unit/platform/sources/test_aircall.py -q
```

The tests use `httpx.MockTransport`; no Aircall credentials, Docker or running backend services are needed. Package versions used for the recorded run are in [runtime-packages.txt](evidence/runtime-packages.txt).

To replay the fictional calls through the native connector and text builder, from `airweave/backend`:

```sh
poetry run python ../../replay-fixtures.py --output-dir ../../replay-output
```

This generates two call entities, their prepared text and their content hashes. [The example output](examples/searchable-calls.md) contains the transcript passage “Nous vous offrons un mois gratuit et nous démarrons lundi.” No search query or vector index is involved in that replay.

## Configure a connection

Once an Airweave instance runs the patched backend and workers, create a collection and use [this request body](examples/source-connection.json) with its `POST /source-connections` endpoint. Supply credentials for an authorized **Aircall** account through Airweave's normal credential flow.

| Setting | Default | Purpose |
| --- | --- | --- |
| `from_timestamp` | unset | Minimum call creation time, Unix seconds |
| `to_timestamp` | sync start time | Maximum call creation time, Unix seconds |
| `include_transcriptions` | `false` | Opt into available post-call transcripts |
| `include_summaries` | `false` | Opt into available AI summaries |

Full syncs revisit calls so delayed transcripts and updated notes can change the indexed content. Missing optional artifacts retain the call's other content; auth, permission and server failures remain visible. Expiring recording URLs are excluded from entities.

## Current scope

- Private-account Basic authentication. Public multi-company installation needs a partner OAuth application and its install flow.
- Full syncs; no webhooks or incremental cursor.
- An explicit error above Aircall's 10,000-match pagination cap. Narrow the configured time range for larger accounts.
- Collection/account access scope; no source-level per-agent ACL mapping.
- Source references use authenticated API URLs; browser dashboard deep links remain unverified.

The upstream public repository is archived. Maintainers need to review compatibility with their current backend and perform a controlled live validation before deploying this connector.

## References and license

[Aircall API](https://developers.aircall.io/api-references) · [Airweave connector guide](https://docs.airweave.ai/add-new-source)

MIT. The [license](LICENSE) retains Airweave's copyright notice. See [NOTICE](NOTICE) for source attribution.
