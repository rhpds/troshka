# Project troubleshooting assistant

Date: 2026-11-26

Status: Recommended v1 scope accepted by the user; implementation specification.

## Outcome and scope

Help a signed-in user diagnose their Troshka project and complete supported
self-service fixes. Use MaaS for reasoning and backend-controlled tools for
evidence gathering. Start with networking, installation failures, and deletion /
storage-settlement explanations. Show evidence, uncertainty, next steps, and a
verification procedure in every diagnostic answer.

V1 performs diagnostics and provides guidance. It does not start, stop, retry,
reconfigure, redeploy, delete, alter billing, remove finalizers, repair storage,
or modify hosts. Links to existing controls let users make those decisions in
the existing product workflows. A future action executor needs its own design
and per-action authorization; diagnostic consent never authorizes mutation.

No external messaging, automatic ticket creation, general shell tool, arbitrary
SQL, arbitrary HTTP fetch, or cross-user conversation search is included.

## Evidence informing priorities

Initial research inspected 22 relevant recent Codex troubleshooting conversations
and older turn pages, selected from local history covering approximately
October 27–November 26. This is not a complete census of screenshot contents or
every tool-output item. Automated reviews and release handoffs are not counted
as independent user incidents. Findings are historical reports, not fresh live
verification. Do not import raw transcripts or historical credentials.

| Reported symptom | Recorded findings | Required behavior |
| --- | --- | --- |
| External SSH timeout | Uppercase `-P` instead of `-p`; missing forward; stopped VM; target IP mismatch; ops-pod IP collision | Separate command mistakes from runtime networking; inspect configured and observed addresses |
| Redfish timeout from bastion | Endpoint responded from ops pod; bastion lacked a path to BMC network | Record probe source, target, port, and path; service health is not universal reachability |
| ACTIVE with OCP API unreachable | ISO generation failed on missing pull secret before OCP installation | Report infrastructure, guest, installation and workload state separately; locate first causal failure |
| Deployment hangs | Missing NIC networkRef; storage schema/configuration mismatch | Surface terminal resource conditions before generic timeouts |
| Workload wait times out | Selector missed a healthy operator; unexpected operator list | Distinguish workload logic failure from target-cluster health; avoid blind retries |
| Delete hangs after VM stop | Finalizers, missing ownership evidence, storage settlement | Explain the current cleanup blocker without weakening ownership or billing safeguards |
| Unexpected installed OCP version | Floating installer selected a different release | Compare requested version with observed version and evidence timestamp |
| Workshop tab blank / 502 | Backend unavailable or assets routed through wrong proxy | Trace service layers; HTML 200 alone is not application readiness |
| SSO failure prevents shutdown | User cannot reach project controls | Explain authenticated-bot limitation and offer an operator handoff draft |

Sources include conversations titled “Troubleshoot unreachable NAT IP”, “Review
attached file”, “Troubleshoot external SSH timeout”, “Find what happened”,
“Investigate failed OCP install jobs”, “Fix project deletion failures”, “Debug
failed delete in troshka”, and “Check deployed OCP version”, plus image-led
conversations. Some earlier diagnoses were corrected by later evidence; the
knowledge base must retain verified conclusions, not every assistant answer.

## Existing integration points

- `src/backend/app/api/projects.py`: owner/admin project access, progress,
  lifecycle controls, container logs, VM exec. Existing exec accepts arbitrary
  commands; do not expose it as an LLM tool. Project topology may contain secrets.
- `src/backend/app/api/workloads.py`: project authorization, workload runs and logs.
- `src/backend/app/services/troshkad_client.py`: host-agent transport and bounded
  health/state queries. Broad host-wide results need project filtering.
- `src/backend/app/services/providers/kubevirt.py`: provider connections and
  namespace resolution. Do not reuse cleanup helpers for inspection.
- `src/backend/app/models/native_project_cleanup.py`: persisted cleanup state and
  evidence; these records can outlive the Project row.
- `src/backend/app/core/config.py`: Dynaconf with `TROSHKA_` environment prefix.
- `src/frontend/src/app/projects/[id]/page.tsx`: project entry point.
- Parsec reference: `~/parsec/src/agent/client_factory.py` builds an Anthropic
  protocol client using a LiteLLM/MaaS endpoint and server-side API key.

Use current repository dependencies. The inspected frontend manifest uses
Next.js 16.3.6, React 19 and PatternFly 6; the older guide's Next.js 15 statement
must not drive implementation. Read installed Next.js documentation before code.

## Architecture

Project chat panel → authenticated chat API → bounded MaaS tool loop → consent
and authorization dispatcher → diagnostic adapters → sanitized evidence → MaaS
answer → streamed panel.

Use one asynchronous model loop, not a fleet of autonomous agents. The model
selects named checks; backend code selects resources and performs checks.
Adapters isolate libvirt/troshkad, hosting KubeVirt, and optional nested guest /
cluster inspection. Provider-specific missing capabilities return `unsupported`.

The browser never receives the MaaS key or host credentials. Use a dedicated
Troshka MaaS key, separate from Parsec. Configure the model explicitly; verify
tool-use and streaming compatibility against the selected gateway before rollout.
Do not infer model availability from Parsec's historical model names.

## Identity and consent

V1 is owner-only for project diagnostics, including when the requester happens to
be an administrator. An administrator's ordinary project visibility does not
authorize a support conversation about somebody else's project. Operator-assisted
delegation is a later capability. Public/portal links and API-key automation are
not chatbot diagnostic identities in v1. Use existing authenticated user sessions
and account/access-window checks; never derive user identity from model arguments.

General documentation questions need no project grant. A conversation can be
bound to at most one project; switching projects starts another conversation.
The initial grant lasts 30 minutes, without silent extension. It includes
project metadata, relevant diagnostic logs, runtime resources and filtered host
health. Guest inspection is an additional, unchecked scope. Inspecting nested
cluster resources requires that guest scope and existing application credentials.

Consent text:

> Allow this conversation to inspect this project's configuration, runtime
> status, relevant logs and supporting host diagnostics. Sanitized evidence will
> be sent to MaaS. This does not authorize changes.

Guest option:

> Also allow predefined checks inside this project's VMs and nested clusters.

Validate ownership, account access, conversation ownership, grant scope,
expiry, revocation and current resource bindings for every dispatch. Do not hold
a database transaction open during network calls. Check again before accepting a
result and before sending evidence to MaaS or returning it to the client.

Revocation cancels active inference and diagnostics best-effort and suppresses
late results. Bytes already sent to MaaS cannot be recalled. After expiry or
revocation, do not reuse project evidence or project-derived assistant messages
in later model requests. The owner may view retained history while still
authorized; continuation with project context requires a new explicit grant.
Ownership transfer immediately blocks old-owner transcript and evidence access.
Project deletion revokes diagnostic access; v1 does not independently authorize
reading surviving cleanup records after the Project row is gone.

Failed, deleting and cleanup_pending projects remain diagnosable while they
exist. Do not use helpers that require the project to be active or stopped.

## Tool contract and resource boundary

Every result has `evidence_id`, `check`, `status`, `observed_at`, `source`,
`project_id`, `resource_refs`, `facts`, `limitations`, and `truncated`.
Status is one of `ok`, `unknown`, `denied`, `unsupported`, `timeout`, `error`.
`ok` means successful observation, not necessarily a healthy resource.
Evidence older than 60 seconds is labeled stale for runtime-state claims.

| Tool | Inputs accepted from model | Backend behavior |
| --- | --- | --- |
| `get_project_summary` | none | Allowlisted topology, desired/runtime/install state, timers and version identifiers |
| `get_install_failure` | optional project cluster key | Earliest relevant error and bounded job/resource evidence; credential presence/validation status only |
| `get_cleanup_status` | none | Cleanup phase, observed remaining resources, blocker and accepted accounting status |
| `get_host_health` | none | Relevant assigned hosts' health/capacity summaries without tenant identities |
| `inspect_network_path` | VM ID, configured endpoint ID, source enum | Compare NICs, DHCP, routes, forwards, and bounded TCP/HTTP probes |
| `get_workload_failure` | run ID | Project-owned run status and bounded failing-task evidence |
| `inspect_guest` | VM ID, predefined check enum | Guest-scope-only addresses/routes/listener/service-status checks |
| `inspect_nested_cluster` | project cluster key, predefined check enum | Guest-scope-only version and selected operator health checks |
| `search_help` | bounded query | Approved, versioned troubleshooting documents only |

The dispatcher binds project ID and user ID; the model cannot supply them.
Resolve all VM/run/endpoint/cluster IDs against the authorized project. Derive
namespaces and actual resource identity through provider bindings, not namespace
prefix guessing alone. For multi-host projects derive all relevant assignments,
including networking hosts, rather than trusting only `Project.host_id`.

Network probes accept only server-resolved project endpoints and an allowlisted
probe type. Block cloud metadata/link-local, management endpoints, unapproved
egress, redirects and DNS rebinding. Pin validated resolved addresses. Use no
arbitrary URL or shell text. Distinguish app/backend, project gateway/ops, and
guest probe locations; a backend probe does not establish laptop reachability.
Read-only exec, if needed underneath an adapter, uses fixed commands and validated
arguments, bounded output and timeout. No shell metacharacter interpolation.

KubeVirt adapters read only project-owned objects and their relevant events.
They never list Secret values, host files, global pod logs, or unrelated projects.
Backend credentials may be stronger than model-visible capabilities: the
dispatcher is an enforced capability boundary, not a prompt convention.

## Data handling and knowledge

Construct safe DTOs from allowlisted fields. Never serialize entire Project,
Host, Provider, topology, Kubernetes objects or tool exceptions. Omit passwords,
private keys, bearer/session tokens, pull secrets, kubeconfigs, cloud-init/user
data, registry auth, public access tokens and credential-bearing URLs.

Log excerpts are a separate opt-in-by-grant data path with field suppression,
credential-pattern masking, control-character removal and a size cap. Prefer
parsed error categories over raw excerpts. If sanitization cannot confidently
produce a safe excerpt, return a reason and direct the user to the existing log
viewer. Redact user-entered known secret forms before storage and inference;
remind users not to paste credentials. No claim of universal free-text redaction.

Evidence and retrieved documents are untrusted data. Embedded instructions cannot
change tools or authorization. Tool schemas reject extra fields. Model output
uses escaped text and validated links; do not render raw HTML or execute content.
Only approved internal control links and documentation URLs are clickable.

Seed curated articles for SSH/forwards, DHCP collisions, BMC routing, OCP pull
secret failures, disconnected NICs, cleanup phases, and storage-charge semantics.
Each article includes an ID, applicable provider/release, symptoms, required
evidence, user steps, verification and escalation condition. Retrieve current
release-compatible articles; cite article IDs and evidence IDs. No automatic
learning from private chats. Later extensions cover workload selector failures,
requested/installed version mismatches and workshop proxy diagnostics.

## Persistence, API and runtime

New SQLAlchemy tables: `AssistantConversation`, `AssistantGrant`,
`AssistantMessage`, `AssistantEvidence`, `AssistantAuditEvent`.
Use existing UUID and JSONB conventions. Conversation stores owner, optional
project, creation/expiry timestamps. Grant stores scope, expiry and revocation.
Messages store sanitized text, role and evidence references. Evidence stores
only safe DTOs. Audit stores actor, resource IDs, check, timestamps and outcome,
not raw prompts/results. Avoid duplicating sensitive content in metrics/logs.

Conversation content/evidence expires after 7 days. Audit metadata expires after
30 days. Owner deletion removes content/evidence immediately and revokes grants;
minimal audit metadata remains until its expiry. Run a daily bounded retention
job using the application's existing scheduling/worker pattern. Access APIs
enforce expiry even if the retention job is delayed. Gateway-side retention is
an explicit deployment prerequisite, separately disclosed from app retention.

Proposed API under `/api/v1/assistant`:

- `POST /conversations` with optional project_id; authorize before creating.
- `GET /conversations/{id}` returns authorized, sanitized history.
- `POST /conversations/{id}/grant` with guest_checks boolean.
- `DELETE /conversations/{id}/grant` revokes all current grants and active work.
- `POST /conversations/{id}/messages` accepts text and client-generated request ID;
  returns an SSE stream via authenticated fetch.
- `DELETE /conversations/{id}/active-run` cancels active work, keeping history.
- `DELETE /conversations/{id}` deletes conversation content and revokes access.

Use events `status`, `check_started`, `check_finished`, `answer_delta`, `done`,
`error`; each carries a sequence ID and request ID. Persist final answers and
terminal errors; reconnection fetches saved history and never reruns a request
implicitly. Do not persist token-by-token partial text as a completed answer.
Enforce one active request per conversation with shared coordination, not
process-local locks. Reject duplicate active submissions with 409 and reference
the existing request. Return the existing terminal result for completed IDs.

Defaults: feature disabled; 1 concurrent run/user, 2,000 input characters,
8 tool calls/turn, 20 seconds/tool, 120 seconds/turn, 4 KiB/tool output,
24 KiB combined evidence, 16,000 model-input tokens, 2,000 output tokens,
20 turns/user/hour. Enforce token limits using the chosen model's tokenizer or
gateway counting capability before release. Apply provider-side spending limits
to the dedicated MaaS key. No unlimited retries: at most one transient model
retry within the deadline, and no replay of an already-issued tool call.

Use Redis for shared run admission, cancellation and rate limits. If Redis is
unavailable, fail chat execution closed with a clear 503 rather than falling
back to unbounded daemon-thread execution. Consent truth is persisted in the DB.
Run asynchronous model I/O and offload blocking adapter calls with bounded
concurrency. A disconnect cancels the active request; shutdown releases leases.
Lease expiry handles process crashes and marks an interrupted turn accordingly.

MaaS failure leaves diagnostics usable in the evidence panel and displays a
retryable error. No fabricated diagnosis. Distinguish missing credentials,
Forbidden, connectivity failure, unsupported checks and empty results.

## UI and answer behavior

Use a PatternFly panel on the project page with suggested questions for the three
initial categories. Show the bound project, active grant scopes and expiry,
Revoke access, Cancel, Delete conversation, and an expandable evidence list.
Guest scope is unchecked. Opening the panel or navigating to a project does not
grant consent. Draft unsaved canvas edits must be identified as unsaved; v1
diagnoses persisted configuration and clearly states that limitation.

Answer structure: observed facts with evidence references; likely cause and
remaining uncertainty; supported self-service steps with links; verification;
escalation when needed. Explicitly distinguish stopped compute from retained
storage charges and recorded resource deletion from confirmed settlement.
Never attribute an action to a person without an attributable audit record.

Provide a copyable, sanitized support draft containing symptom, resource IDs,
timestamps, checks, findings and unresolved blocker. The user chooses whether to
share it. No automatic Slack/email/ticket submission. Since a user with broken
SSO cannot reach the panel, show existing support guidance outside chatbot flows
where the application already supports it; do not add unauthenticated diagnostics.

## Acceptance and rollout

Required deterministic regressions: wrong SSH flag, absent forward, stopped VM,
DHCP/ops collision, BMC source-path mismatch, missing pull secret preceding API
timeout, disconnected NIC preceding deployment timeout, and cleanup with unknown
storage settlement. Assert that uncertainty remains explicit and that no case
dispatches a mutation. Include the corrected workload-selector diagnosis as a
later adapter acceptance case, not an assumption that every timeout is unhealthy.

Security tests must cover cross-project IDs, same-project foreign run/VM IDs,
ownership transfer, public links, account expiry, grant expiry/revocation during
checks and inference, guest scope, prompt injection, output escaping, redirect /
DNS / metadata egress, secret-containing fixtures, model unknown-tool requests,
duplicate requests, Redis loss and worker/process interruption.

Pilot on dev with synthetic secrets and disposable projects. Verify consent and
revocation in the UI, actual MaaS tool use/streaming, and each provider's supported
checks. Require zero authorization and secret-exposure failures. Human-reviewed
answers must identify the supported cause or explicitly report insufficient
evidence for every seeded case. Production enablement follows dev verification
with the same named release and a dedicated key; this design does not authorize
deployment. Feature disable cancels active runs and blocks new inference while
leaving normal project controls operational.

Track latency, tokens, tool/error counts, unsupported-check rates, user-reported
resolution and escalation rates without raw conversation text. Do not call a
case resolved merely because a response or diagnostic request succeeded.
