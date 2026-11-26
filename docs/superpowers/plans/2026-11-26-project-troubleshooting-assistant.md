# Project Troubleshooting Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, inline in the current session. Do not spawn agents without user authorization. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver consent-controlled, MaaS-backed project diagnostics and guided self-service for networking, installation failures and cleanup explanations.

**Architecture:** An authenticated FastAPI SSE endpoint runs one bounded model tool loop. A server-side dispatcher rechecks identity and consent and resolves project resources before invoking typed diagnostic adapters. A PatternFly project panel displays guidance and sanitized evidence without executing repairs.

**Tech Stack:** Existing Python 3 / FastAPI / SQLAlchemy / Alembic / Dynaconf / Redis stack; Anthropic-protocol asynchronous MaaS client; existing Next.js / React / PatternFly frontend and Vitest tests.

**Spec:** [Project troubleshooting assistant design](../specs/2026-11-26-project-troubleshooting-assistant-design.md)

## Global Constraints

- V1 performs diagnostics and provides guidance.
- Diagnostic consent never authorizes mutation.
- V1 is owner-only for project diagnostics, including when the requester happens to be an administrator.
- The initial grant lasts 30 minutes, without silent extension.
- Guest inspection is an additional, unchecked scope.
- Conversation content/evidence expires after 7 days. Audit metadata expires after 30 days.
- Defaults: feature disabled; 1 concurrent run/user, 2,000 input characters, 8 tool calls/turn, 20 seconds/tool, 120 seconds/turn, 4 KiB/tool output, 24 KiB combined evidence, 16,000 model-input tokens, 2,000 output tokens, 20 turns/user/hour.
- If Redis is unavailable, fail chat execution closed with a clear 503.
- Always use `python3`; backend commands use `src/backend/venv/bin/python3`.
- Do not edit with sed, add co-author trailers, or push/deploy as part of implementation.
- Use background execution for long tests/builds with the available tool's background/session facility; no sleep-poll loops.
- Read installed Next.js documentation before frontend implementation; use the existing dependency versions.
- Keep each function's cognitive complexity at or below 15.
- Keep root CHANGELOG.md updated with the user-visible feature under the current month.

## File boundaries

Create backend `app/services/assistant/` modules: `contracts.py` (strict DTOs),
`policy.py` (authorization), `projection.py` (safe data), `dispatcher.py` (tool
registry), `project_checks.py` (persisted evidence), `host_checks.py` (host adapter),
`kubevirt_checks.py` (hosting-cluster adapter), `guest_checks.py` (fixed guest
checks), `knowledge.py` (curated documents), `model.py` (MaaS transport),
`runtime.py` (bounded loop), `runs.py` (shared admission/cancellation), and
`retention.py` (expired content cleanup). Keep files focused; do not extend the
large projects.py with chatbot orchestration.

Create `app/models/assistant.py`, `app/schemas/assistant.py`,
`app/api/assistant.py`, a generated Alembic revision and `tests/assistant/`.
Register models/router in their existing registries. Add feature configuration to
`src/backend/config/config.yaml`; declare the chosen compatible MaaS SDK in the
repository's existing dependency manifest and verify the gateway before pinning.

Create frontend `src/components/assistant/{AssistantPanel,ConsentControls,
EvidenceList}.tsx` and `src/lib/assistant-client.ts`, with colocated tests.
Integrate only the entry point in `src/app/projects/[id]/page.tsx`.
Create curated articles in `docs/troubleshooting/` and deploy documentation in
`docs/dev/troubleshooting-assistant.md`.

All backend paths above are relative to `src/backend/`; frontend paths are
relative to `src/frontend/`.

## Task 1: Persistent identity, grants and policy

**Files:** models/assistant.py, schemas/assistant.py, services/assistant/contracts.py,
services/assistant/policy.py, models/__init__.py, generated Alembic revision,
tests/assistant/test_policy.py, tests/assistant/test_retention.py.

**Interfaces:** Define `AssistantScope` as `Literal["project", "guest"]`;
`AuthorizedContext` contains actor_id, conversation_id, project_id, grant_id and
grant_expires_at. Produce `authorize(db, actor, conversation_id, scope, now) ->
AuthorizedContext`. `actor` is the authenticated User model. The returned context
is not durable authorization: dispatcher rechecks it before publishing results.

- [ ] Add fixtures for users A/B, an A-owned project, an A-owned conversation and
  a project-only grant. Use repository DB fixtures and UTC timestamps.
- [ ] Write policy tests using these concrete cases:

```python
@pytest.mark.parametrize("case", [
    "foreign_conversation", "foreign_project", "admin_not_owner",
    "transferred_project", "expired_grant", "revoked_grant",
    "expired_account", "guest_without_scope", "deleted_project",
])
def test_diagnostic_authorization_denies_outside_grant(policy_case, case):
    db, actor, conversation_id, scope, now = policy_case(case)
    with pytest.raises(HTTPException):
        authorize(db, actor, conversation_id, scope, now)
```

  `policy_case` builds each named case by changing only the corresponding fixture
  condition. Also test failed/deleting/cleanup_pending states remain accessible
  to the owner with valid consent and no active/stopped restriction.
- [ ] Run `./venv/bin/python3 -m pytest tests/assistant/test_policy.py -q` from
  src/backend and verify the new tests fail before implementation.
- [ ] Add the five models from the spec, UUID FKs matching existing conventions,
  ownership indexes, expiry fields and unique `(conversation_id, request_id)`
  request identity on the persisted user-message/run record. Store terminal
  status there so retries survive process restart. Persist grant scope/revocation.
- [ ] Implement `authorize` using exact user and project ownership, account access
  checks, scope and UTC expiry. Do not accept public-token/API-key identities.
- [ ] Generate migration, inspect upgrade/downgrade, and test on disposable
  PostgreSQL plus the repository SQLite test environment. Do not migrate prod.
- [ ] Run policy tests; review FK behavior on owner/project deletion and ensure
  surviving cleanup rows cannot confer access. Commit only task files when ready.

## Task 2: Safe evidence contract and dispatch boundary

**Files:** contracts.py, projection.py, dispatcher.py,
tests/assistant/test_projection.py, tests/assistant/test_dispatcher.py.

**Interfaces:** `ToolRequest(name: str, arguments: dict)`;
`Evidence(evidence_id, check, status, observed_at, source, project_id,
resource_refs, facts, limitations, truncated)`;
`async dispatch(db_factory, actor_id, conversation_id, request: ToolRequest) -> Evidence`.
`db_factory` creates short-lived SQLAlchemy sessions. `SafeProjectSummary` and
each tool-specific facts schema prohibit extra fields. Unknown tools are rejected.

- [ ] Write canary tests that place distinct secrets in topology cloud-init,
  passwords, pull secrets, provider config, public tokens and nested logs. Assert
  none appear in serialized evidence, exceptions, audit events or log capture.

```python
def test_project_projection_omits_credentials(project_with_canaries):
    project, canaries = project_with_canaries
    encoded = project_summary(project).model_dump_json()
    assert all(value not in encoded for value in canaries)
    assert "public_token" not in encoded
```

  Produce `project_summary(project) -> SafeProjectSummary` in projection.py.
- [ ] Write dispatcher tests for arbitrary shell/URL fields, foreign resource IDs,
  unknown tool names, guest scope, revocation during adapter execution, revoked
  results, denied access and oversized results. Use an adapter barrier to revoke
  before completion; assert the late result never reaches persistence/model.
- [ ] Implement a static tool registry with per-tool Pydantic input models,
  forbidden extra fields, required scopes and bounded adapters. Reauthorize in
  fresh DB sessions before dispatch, after completion and before publication.
- [ ] Implement explicit safe DTO construction, bounded error/log extraction and
  fail-closed suppression of unsafe excerpts. Include safe reasons for redaction.
  Return timed-out/unsupported/error results as such, never empty healthy lists.
- [ ] Run both test modules with backend pytest; inspect all egress serialization
  paths and commit the capability boundary independently of MaaS integration.

## Task 3: Initial project, installation and cleanup checks

**Files:** project_checks.py, host_checks.py, kubevirt_checks.py,
tests/assistant/test_project_checks.py, tests/assistant/test_provider_checks.py.

**Interfaces:** Register `get_project_summary`, `get_install_failure`,
`get_cleanup_status`, `get_host_health`, and `get_workload_failure` with dispatch.
Adapters accept AuthorizedContext plus validated tool arguments and return
Evidence; internal provider clients never escape the adapter.

- [ ] Add recorded, sanitized fixtures for missing pull secret followed by API
  timeout; disconnected NIC followed by deploy timeout; cleanup blocked by a
  finalizer; namespace gone with storage settlement unknown; stopped VM with
  retained disks. Assert evidence preserves the earliest causal error and reports
  infrastructure/installation/guest state in distinct fields.
- [ ] Read progress and workload code from existing API/services; call reusable
  read functions or extract narrowly scoped read helpers without changing their
  existing authorization or lifecycle behavior. Do not call HTTP route functions
  internally with fabricated admin users.
- [ ] Implement cleanup reads from persisted state plus bounded resource
  observations. Never invoke reconcile, collection enqueue, ownership enrollment
  or finalizer removal. Do not infer charge cessation from namespace absence.
- [ ] Implement host adapters for both KubeVirt and troshkad with current
  project host assignments and filtered health/capacity facts. Reject namespace
  UID/resource ownership mismatches and distinguish Forbidden from absence.
- [ ] Verify provider tests assert no writes: mock clients reject create, patch,
  delete, job dispatch or unrestricted exec. Assert other tenants' fixtures never
  enter results. Run both test modules and commit when all pass.

## Task 4: Bounded networking and optional guest diagnostics

**Files:** host_checks.py, kubevirt_checks.py, guest_checks.py, dispatcher.py,
tests/assistant/test_network_checks.py, tests/assistant/test_guest_checks.py.

**Interfaces:** Register `inspect_network_path`, `inspect_guest` and
`inspect_nested_cluster`. Define NetworkPathArgs with vm_id, endpoint_id and
source (`backend`, `gateway`, `ops`, `guest`); GuestArgs with vm_id and check
(`addresses`, `routes`, `listeners`, `service_status`); NestedArgs with
cluster_key and check (`version`, `operator_health`). Resource endpoints and
service names come from project configuration and curated check definitions.

- [ ] Write cases for wrong SSH port, missing forwarding rule, VM stopped,
  configured/observed IP mismatch, ops address collision, healthy BMC from ops
  but unreachable from guest. Assert probe source is always recorded.
- [ ] Write egress cases for metadata/link-local addresses, unrelated private
  addresses, DNS answer changes, redirects, user-supplied URL/shell text and
  cross-project VM IDs. Assert no connection or exec occurs for rejected inputs.
- [ ] Implement server-side endpoint resolution and project network membership
  checks, pinning approved addresses and disabling redirects. Use fixed TCP and
  HTTP probe definitions with deadlines and response-size limits. Do not collect
  arbitrary response bodies or credential-bearing headers.
- [ ] Implement fixed guest commands and structured nested-cluster reads using
  application-held credentials. Guest/nested checks require guest scope. Fail
  unsupported transports clearly rather than spawning general-purpose shells.
- [ ] Test repeated checks do not alter network state or trigger workloads. Run
  both modules, inspect the exact permitted commands and commit the adapters.

## Task 5: Curated help and answer contract

**Files:** knowledge.py, docs/troubleshooting/*.md,
tests/assistant/test_knowledge.py, tests/assistant/fixtures/*.json.

**Interfaces:** `search_help(query: str, release: str, provider: str) -> list[HelpArticle]`;
HelpArticle has id, release/provider applicability, symptoms, required_evidence,
steps, verification and escalation. Use a small local index first, with no vector
database or external transcript ingestion. Define allowed documentation paths.

- [ ] Create seven articles: SSH/forwarding, DHCP collision, BMC routing, OCP
  credential failure, disconnected NIC, cleanup phases, and storage charges.
  Use current docs/code for product instructions; historical diagnosis alone is
  not proof a workaround is appropriate for the running release.
- [ ] Add fixture expectations for article applicability, unsupported release,
  bounded results, article IDs and no private identifiers from original chats.
- [ ] Implement query limits and metadata filtering. Return no matching article
  explicitly; never silently use incompatible release instructions.
- [ ] Define the system answer contract: observations/evidence IDs, cause with
  uncertainty, self-service control links, verification and escalation. Treat
  logs/articles as untrusted data; unknown evidence references cannot be rendered
  as authoritative citations. Support drafts contain safe evidence only.
- [ ] Run knowledge tests and manually review every article's mutation wording:
  v1 links to existing controls, not model-generated recovery shell commands.
  Commit curated help and its provenance metadata.

## Task 6: MaaS client and bounded run lifecycle

**Files:** model.py, runtime.py, runs.py, config/config.yaml, requirements.txt,
tests/assistant/test_runtime.py, tests/assistant/test_runs.py.

**Interfaces:** `async stream_answer(actor_id, conversation_id, request_id, text)`
yields AssistantEvent; AssistantEvent has sequence, request_id, type and safe
payload. `ModelClient` wraps async gateway calls and token counting. `RunLease`
encapsulates atomic Redis admission, renewal, cancellation and expiry. Runtime
uses dispatch and search_help; it never exposes a generic interpreter tool.

- [ ] Write fake-model tests for tool-call → evidence → cited answer; unknown
  tools; invalid schemas; injected log instructions; token/tool/time limits;
  revoked scope; safe gateway errors; duplicate IDs; crash recovery; and Redis loss.

```python
@pytest.mark.asyncio
async def test_revocation_suppresses_late_evidence(runtime_case):
    case = runtime_case("revoke_during_check")
    events = [event async for event in case.run()]
    assert not case.model.received_project_evidence
    assert all(event.type != "answer_delta" for event in events)
    assert events[-1].type == "error"
```

  `runtime_case` builds the fake model, adapter barrier, DB grant and Redis fixture;
  test revoke after inference submission separately and assert subsequent deltas
  are suppressed while recording that previously sent context is not recallable.
- [ ] Add disabled-by-default assistant configuration and environment-only key
  injection. Verify a compatible Anthropic-protocol SDK using Parsec as reference;
  use configured endpoint/model with no fallback to public providers or credentials.
- [ ] Implement shared user/conversation admission, rate limiting, lease expiry,
  cancellation and persisted request terminal state. Never replay tool execution
  on reconnect. Enforce the exact defaults in Global Constraints.
- [ ] Build model history only from currently authorized context. Expired or
  revoked grants exclude project-derived messages and evidence. Recount inputs
  after each tool result; trim safely without dropping the policy instructions.
- [ ] Stream validated answer deltas, with bounded transient model retry only;
  persist completed answers/terminal errors. Preserve safe evidence on MaaS
  failure so users can inspect it without an invented summary.
- [ ] Run runtime/run tests with fake MaaS and real disposable Redis; exercise
  multi-process admission. Commit once cancellation and duplicate semantics pass.

## Task 7: Authenticated API and retention

**Files:** api/assistant.py, main.py, schemas/assistant.py, retention.py,
tests/assistant/test_api.py, tests/assistant/test_retention.py.

**Interfaces:** Implement exactly the seven API operations in the spec. Use
existing session identity, CSRF/origin protections and access-window checks.
`purge_expired(db, now, batch_size=100) -> int` deletes expired content/evidence
and audit metadata according to their different retention periods.

- [ ] Add endpoint tests for owner isolation, unauthenticated/public-token/API-key
  requests, disabled feature, foreign project binding, oversized input, 409
  duplicate-active request, retained completed request and 503 Redis unavailable.
- [ ] Add disconnect and shutdown tests: cancel MaaS/adapters, suppress late
  results, close sessions and release/expire run leases. Ensure SSE uses no
  credential-bearing query parameters and no proxy buffering/cache.
- [ ] Implement the routes with short DB sessions and async streaming. DB failure
  during policy checks fails closed. Use safe error codes rather than exception
  strings. Deletion immediately revokes active access before deleting content.
- [ ] Integrate daily bounded retention with the existing application scheduler /
  worker pattern after inspecting its registration in main.py. Ensure one shared
  scheduled job, not one purge task per web replica. API expiry enforcement is
  independent of successful physical deletion.
- [ ] Test 7-day content and 30-day audit expiration, explicit owner deletion,
  removed project, and ownership transfer. Run API/retention tests; commit.

## Task 8: Project panel and consent UX

**Files:** frontend components/assistant/*.tsx, lib/assistant-client.ts,
app/projects/[id]/page.tsx and colocated Vitest files.

**Interfaces:** `AssistantPanel({projectId, onClose})`; ConsentControls shows
grant state, expiry, unchecked guest option and revoke; EvidenceList consumes
Evidence DTOs. Client uses raw authenticated fetch and parses the defined SSE
events incrementally, including split UTF-8/event boundaries.

- [ ] Read frontend AGENTS.md, relevant installed Next.js docs and existing
  project-page test patterns. Use current PatternFly components and client state.
- [ ] Write UI tests: panel opening sends no grant; guest unchecked; correct
  consent wording; expired/revoked scope; cancel; project switch; safe evidence
  links; stream split chunks; disconnected request; unavailable MaaS and Redis.
- [ ] Implement the panel with suggested initial questions, scope/expiry display,
  evidence expansion, sanitized support-copy action and ordinary control links.
  Do not render raw HTML. Restrict links to approved product/documentation paths.
- [ ] Add “Troubleshoot this project” to the project page and applicable failure
  displays. Hide unavailable feature gracefully. State that unsaved topology
  changes are not included. Preserve the canvas and existing control behavior.
- [ ] Run `npm test -- src/components/assistant src/lib/assistant-client.test.ts
  'src/app/projects/[id]/page.test.tsx'` and `npx tsc --noEmit` from frontend.
  Verify keyboard access, focus return, narrow viewport and live grant revocation
  in a browser against dev; do not merely assert the panel renders. Commit.

## Task 9: End-to-end evidence, documentation and release readiness

**Files:** tests/assistant/test_acceptance.py, frontend assistant tests,
docs/dev/troubleshooting-assistant.md, CHANGELOG.md and deployment templates
identified from the current deployment guide.

- [ ] Replay the eight initial diagnostic fixtures through API → fake MaaS →
  dispatcher → answer. Assert approved cause or insufficient-evidence result,
  correct evidence references and no mutation calls for every case.
- [ ] Run the full assistant suite once, then relevant existing project/workload
  authorization regressions and the frontend checks. Do not run the entire
  backend suite repeatedly without a change or failure justifying it.
- [ ] In dev only, verify real MaaS streaming, tool calls and token counting with
  a dedicated key and synthetic secrets. Verify hosting KubeVirt and troshkad
  checks on disposable projects plus guest consent. Record supported/unsupported
  checks, latency, limits and evidence freshness. No production project access.
- [ ] Document configuration, key injection, gateway retention prerequisite,
  fixed tools, limits, retention job, disabled feature behavior and operator
  support handoff. Add the user-visible November changelog bullet only with the
  implementation, not with these planning documents.
- [ ] Verify feature disable cancels active runs without affecting existing
  project controls. Review security cases and ensure raw prompts/credentials
  appear in neither logs nor metrics. Record exact test outcomes and limitations.
- [ ] Commit the complete intended implementation set with no co-author lines.
  Present release readiness and dev evidence. Do not push or deploy production
  without authorization; promotion must use the same verified named release.

## Plan self-review

### Execution status — 2026-11-26

Implementation is present on `codex/project-troubleshooting-assistant`. See
`docs/dev/troubleshooting-assistant.md` for verified checks and remaining release
gates. The checkboxes above remain the original acceptance checklist, not a claim
that all live acceptance steps have run. Production deployment is not authorized.

Implementation choices: reuse existing httpx for the Anthropic MaaS protocol;
buffer answer text until sanitization and final authorization while streaming
progress/evidence; bundle curated guidance in the runtime module. Production
Parsec's endpoint/model are the defaults. Nested-cluster and ops-source probes
remain explicitly unsupported. Local browser acceptance subsequently passed,
including narrow viewport, keyboard focus return and active-turn revocation;
disposable provider acceptance remains pending. Synthetic live API/MaaS,
Redis concurrency and PostgreSQL migration
checks passed. No user-project or production-host mutation was performed.

The tasks cover consent and identity (1–2), provider evidence (3–4), curated help
(5), model/run limits (6), API/retention (7), user experience (8), and acceptance /
rollout documentation (9). No automatic repair, public diagnostics, shared-host
administration or cross-user learning is part of any task. Definition of success
is tested diagnostic usefulness and isolation, not HTTP 200 or an LLM response.
