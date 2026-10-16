# Validation and integration boundaries

The reviewed patch was applied to a clean export of Airweave's public backend at commit `1ebe1af2dbfb90f3334410721e69997e4f02b320`, using Python 3.13.2.

**35 Aircall cases and 207 targeted cases passed.** These are recorded local results. They are not a live provider or hosted-platform qualification.

## Coverage

| Area | What was exercised |
| --- | --- |
| Registration | Actual source/entity registries and source API metadata |
| Connection setup | Actual source lifecycle credential normalization and validation |
| HTTP behavior | Airweave's HTTP wrapper with `httpx.MockTransport`, Basic auth, redirects, rate limits, retry/recovery and propagated failures |
| Pagination | Multiple pages, repeated IDs, incomplete results, malformed states and the documented pagination cap |
| Call content | Answered/missed calls, absent contacts, notes, tags, available/missing transcripts and summaries |
| Late content | A delayed transcript changes the native content hash while retaining call identity |
| Pipeline | The actual text-builder batch and content hash implementation; unused converter dependencies are test doubles |
| Defaults | Explicit transcript/summary opt-in matches the existing creation form's unchecked booleans |

Two entirely fictional finished calls were replayed through the source and native text preparation. The [prepared text](../examples/searchable-calls.md) and [entities](../examples/entities.json) show the output. No audio converter, embedder, database, vector index or search service was invoked.

Ruff formatting/lint, patch application and whitespace checks pass. A targeted mypy run passes for the two new runtime modules, with imported modules skipped (`--follow-imports=skip --ignore-missing-imports`). This is not a full-backend type check.

The targeted pytest suite also covers upstream source lifecycle/config validation, source/entity registries, HTTP helpers, Fireflies, Freshdesk and text preparation. Remaining warnings originate in existing upstream Pydantic configuration and the Fireflies timestamp helper. Local filesystem paths and the test host name are sanitized in the reports.

## Reproduction

The [README](../README.md) gives the patch application and connector-test commands. The full targeted command is recorded in [verification.json](../evidence/verification.json), alongside the pinned upstream commit and check scope. The import/test dependency subset used locally is listed in [runtime-packages.txt](../evidence/runtime-packages.txt).

The eight reviewed source/test files are copied under `overlay/` for inspection. `native-connector.patch` is the integration artifact to apply; copying shared config or registry files over a newer upstream checkout could overwrite unrelated changes.

## Live qualification still needed

Maintainers should compare the patch with their current backend, validate a controlled Aircall account, sync authorized calls, and check real indexing/retrieval, updates and connection removal. The public source is archived; the current hosted implementation is unknown.

The current connector uses private-account Basic authentication and full syncs. Partner OAuth installation, webhooks, incremental cursors, automatic large-history partitioning and per-agent ACL mapping are outside this version. Collection permissions and the authorized Aircall account define the access scope. The documented source URL is an authenticated API resource, not a verified dashboard link.
