# Project troubleshooting assistant

The assistant is disabled by default. It provides diagnostic evidence and
guided self-service; it has no repair/lifecycle tools. Owner-only consent lasts
30 minutes. Guest-agent commands require the separate guest checkbox.

## Configure and validate in dev

Apply migration `b7a260112601` through the normal release migration workflow.
Run the backend with Redis available; assistant execution intentionally has no
in-memory coordination fallback.

Configure `TROSHKA_ASSISTANT__ENABLED=true`,
`TROSHKA_ASSISTANT__BASE_URL` (trusted HTTPS MaaS base URL),
`TROSHKA_ASSISTANT__MODEL` (explicit available model), and
`TROSHKA_ASSISTANT__API_KEY` through a local secret environment or Kubernetes
Secret. Do not paste keys into chats or commit them. Provision a dedicated key
for Troshka deployment. Defaults match production Parsec: endpoint
`https://maas-rhdp.apps.maas.redhatworkshops.io` and model `claude-sonnet-4-6`.
MaaS must support Anthropic `/v1/messages` tool use and
`/v1/messages/count_tokens`. There is no direct-public-provider fallback.

Helm values `assistant.enabled`, `baseUrl`, `model`, `existingSecret`, and
`secretKey` configure the backend. Provision the referenced Secret separately.
The chart never creates or generates a MaaS credential. No key is passed to the
frontend or worker fleet. Ansible/direct-manifest deployments must inject the
same backend environment variables through their normal secret management.

Before enabling for users, confirm the gateway's logging and retention policy
with its operator, validate model access using synthetic data, and set a spending
limit on the dedicated key. The app's retention policy does not control MaaS
retention. Changing configuration requires the normal backend restart/rollout;
there is no browser-based key editor.

## Diagnostics and limits

Project, workload and cleanup checks positively select safe fields. Error text
is classified into known categories; raw logs and unknown error strings are not
sent to the model. Host health uses existing host-poller observations with their
timestamps. KubeVirt VM reads verify the exact parent project identity and, when
available, its recorded namespace UID before accepting resource state.

Networking compares saved forwarding rules with VM state. An optional TCP probe
accepts only an allocated project EIP, a matching VM-targeted TCP forward, and a
literal global address. It runs from the backend, sends no credentials or payload,
and cannot prove laptop, ops-pod or guest reachability. Private/metadata/multicast
addresses and arbitrary URLs are rejected. A failed KubeVirt ownership check
fails the diagnostic closed rather than falling back to prefix-only matching.

Guest checks use only `ip -j address show`, `ip -j route show`, `ss -H -lnt`, or
`systemctl is-active sshd` through the guest agent. Results are parsed into
addresses, routes, ports or a fixed state vocabulary. No SSH, serial or VNC
fallback is attempted. Nested-cluster API inspection currently returns
`unsupported`; no topology-supplied kubeconfig or URL becomes a model capability.

Limits: one run per user and conversation; 20 turns/user/hour; 120 seconds/turn;
eight tool calls; 20 seconds/check; four concurrent blocking checks per process;
4 KiB/check; 24 KiB evidence/turn; 16,000 input tokens and 2,000 output tokens;
2,000 characters per user message. A timed-out underlying transport retains its
worker slot until it finishes, preventing unbounded abandoned threads.

SSE streams progress and checked evidence. Model answer text is buffered until
whole-message sanitization and a final access check, then emitted as one answer
event. This avoids partial credential masking and late-answer disclosure. Model
errors are sanitized; truncated model responses are not presented as complete.

## Privacy, cancellation and retention

Opening the panel creates a private conversation but grants no project access.
Without consent the model receives only the user's sanitized question and bundled
general guidance. Do not enter passwords or tokens: free-text pattern masking is
not a guarantee for every possible secret format.

Revoke persists immediately and cancels active work best-effort. Already-sent
MaaS bytes cannot be recalled. Old grant generations, their evidence and derived
answers are excluded from subsequent model context. Stored history is visible
only to its owner while that owner still owns the project and has account access.
Project deletion or ownership transfer blocks that access even if cleanup records
survive. Support drafts are copied locally; nothing is automatically sent.

Content/evidence expire after seven days and audit metadata after 30 days. A
shared Redis daily key admits one bounded cleanup sweep; expiry checks protect
API access even if physical cleanup is delayed. Owner deletion removes content
immediately while minimal audit metadata retains its normal expiry. Failed
retention logs contain no conversation text. Inspect those warnings operationally.

## Acceptance checklist

- Exercise wrong SSH port, missing forward, stopped VM and mismatched guest IP.
- Exercise an ops/bastion address conflict and source-dependent BMC reachability;
  unsupported probes must remain explicitly unknown.
- Exercise missing pull secret before OCP API timeout and missing NIC networkRef.
- Exercise cleanup_pending with incomplete settlement; do not promise zero charges.
- Revoke consent during a slow check and inference; ensure no late evidence or
  answer is published. Test ownership transfer and account expiry too.
- Confirm separate guest consent, unknown-tool rejection, and no raw secrets in
  model requests, transcripts, metrics or logs using synthetic canaries.
- Verify the configured gateway's count-token endpoint, tool calls and limits.
- Verify Redis contention/cancellation across backend processes, PostgreSQL
  migration upgrade/downgrade on a disposable database, and UI keyboard/mobile use.

## Validation record (2026-11-26)

### Dev deployment

`troshka-dev` on `ocpvdev01.dal13` is enabled on
`v2026.11.26-assistant-dev` (source `b8c65279`). Both Argo's Git revision and
ImageUpdater now target this named release rather than following main/latest.
Argo reported Synced/Healthy; backend, frontend, tunnel and active workers were
verified against the named image digests. Migration `b7a260112601` is applied.
The backend reads `troshka-assistant-maas-dev` (`api-key`), provisioned from the
existing Parsec credential at the user's request. No credential is in Git.

A temporary synthetic dev user/project passed authenticated API-to-MaaS diagnosis
of a missing pull secret and active-turn consent revocation. The fixture user,
project and conversation were removed, retaining only normal audit metadata.
Public-route browser testing requires OpenShift sign-in. Production was not
deployed. Dev is pinned to this release until a subsequent deployment deliberately
advances its Git revision and image updater together.

### Local validation

Local validation passed 50 backend tests (including real Redis cross-process
coordination), 84 frontend tests, TypeScript checking, Helm rendering, and a
disposable PostgreSQL migration upgrade/downgrade. A synthetic live check against
production Parsec's configured MaaS endpoint and model passed token counting,
tool use, and API-to-model diagnosis of a missing pull secret. Revoking consent
during a live turn prevented a late answer. The existing Parsec credential was
used only in process for this synthetic check; no credential was copied into
source or deployment configuration.

Follow-up local browser acceptance passed against the full Next.js UI and FastAPI
API on ports 3117/8217: project creation, opening without consent, unchecked guest
scope, explicit grant, live MaaS diagnosis with evidence, and revocation during
inference. Database verification showed the revoked request as interrupted with
no assistant response. At a 390-pixel viewport the dialog had no horizontal
overflow; keyboard navigation stayed within it, and Escape returned focus to the
launcher. The empty draft `Assistant local acceptance` remains for manual testing.

The worktree's ignored `config.local.yaml` enables the feature. The local backend
loads the existing Parsec credential in process via
`/private/tmp/troshka-assistant-local.py`; the key is not written to configuration.
This second backend runs with lifespan jobs disabled to avoid duplicate host
polling and lifecycle timers. The frontend uses Webpack because Turbopack rejects
the shared node_modules symlink. The normal app on ports 3100/8200 is unchanged.
The assistant migration is applied to the local PostgreSQL database. Background
retention is not running in this test backend; API expiry checks still apply.

Live KubeVirt/troshkad diagnostics and guest-agent checks on disposable projects
remain pending. Nested-cluster inspection and
ops/bastion-source probes explicitly return unsupported. No production deployment
or production user-project inspection was performed.

Automated model tests use fake MaaS responses and do not establish live provider
acceptance. Promote the same verified named release
after dev acceptance. Disable via configuration and normal rollout if necessary;
normal project controls remain independent of the assistant.
