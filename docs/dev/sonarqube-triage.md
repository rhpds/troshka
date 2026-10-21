# Reviewed SonarQube security findings

## Mutation-safe dictionary snapshots, 2026-10-20

Two `python:S7504` findings are false positives. These `list()` calls are
snapshots required because the loop body removes entries from the dictionary.
Iterating the dictionaries directly would raise `RuntimeError` after mutation.

| Finding | Call site | Evidence |
| --- | --- | --- |
| `c9a6fcc6-b915-4121-b63d-a9c72ed89ad8` | `ops_pod_heal._strip_ops_pod_cni_status_annotations` | The loop calls `annotations.pop`. `test_ops_pod_heal.py` verifies removal of stale OVN/CNI annotations while preserving requested networks. |
| `d598e43f-77a4-464e-a295-80830096dab2` | `MultiplexSession._shutdown` | `_close_stream` pops entries from `_streams`. `test_tunnel_session_paths.py::test_shutdown_closes_every_stream_while_removing_registry_entries` verifies that multiple registered streams close and both registries empty. |

These decisions apply to the specific call sites below, not to the rules
globally. Revisit them if the authentication or source-selection behavior changes.

## Agent API tunnel: S5527 and S4830

[`open_troshkad_tunnel_socket`](../../src/backend/app/services/ocp/api_tunnel.py)
connects to agents with self-signed certificates. Identity is established by the
enrolled SHA-256 certificate fingerprint, rather than a public CA or hostname.
`_get_pool` rejects hosts without an enrolled fingerprint.
`_open_pinned_agent_socket` rejects missing or mismatched peer certificates,
closes the connection on failure, and returns only after successful comparison.
The caller sends its bearer token only after this check.

The disabled CA and hostname checks are therefore intentional, and these two
rule reports are false positives for this pinned connection. Tests in
[`test_api_tunnel.py`](../../src/backend/tests/test_api_tunnel.py) cover missing
and mismatched certificates, a matching certificate, handshake cleanup, and
withholding credentials from an unverified peer.

## CDI transfer-size probe: S7608

[`_object_size`](../../src/backend/app/services/disk_transfer_progress.py)
uses the endpoint, bucket, and object selected by CDI to obtain a byte count for
optional progress reporting. Sources can include external/public buckets and
non-AWS S3-compatible servers. There is no application-owned AWS account that
the source is required to belong to, and this probe neither writes objects nor
uses their contents to establish trust.

Requiring `ExpectedBucketOwner` here would invent an ownership constraint that
the importer does not have and would not work for the supported non-AWS
endpoints. This report is a false positive for this metadata-only probe.
[`test_disk_transfer_progress.py`](../../src/backend/tests/test_disk_transfer_progress.py)
covers source selection, credential use, bounded requests, and optional failure.

## Full-backlog review, 2026-10-20

The main-branch inventory at revision `8de7832f` contained 1,592 unresolved
issues across all code, not just the earlier new-code scope. The subsequent
dependency commit did not change these source call sites. Inventorying a rule
does not establish that every finding under that rule is a false positive.

The following 17 individual findings have evidence supporting false-positive
review. These decisions do not disable their rules.

| Finding | Rule | Call site and evidence |
| --- | --- | --- |
| `35d81a69-235d-4810-8845-573132817f98` | S6853 | `DuplicateVMModal.tsx`: the enclosing label supplies the radio's accessible name from nested “Generate new UUID” text. The rendered-role test verifies its name and selection. |
| `4ff89dd1-cccf-44b0-b3e5-0fcf55aea8df` | S6853 | `DuplicateVMModal.tsx`: the second radio is named by nested “Clone UUID” text. The same test finds it by role and name, clicks it, and verifies selection. |
| `241f15dc-12cb-488a-bb40-ce03b372ecac` | S6551 | `apiError.ts`, `formatApiDetail`: arrays and objects return before the final `String(detail)`. Objects use JSON serialization, covered by a regression test. |
| `e618864c-9480-431b-a07e-f16bd88f6f49` | S6551 | `canvasStore.ts`, `_formatDiffValue`: nulls, arrays, and objects return before `String(v)`, so the reported plain-object conversion cannot occur at this call. |
| `8a7a90ab-0544-4a6b-b497-4185192e85b4` | S8707 | `troshkad.py`, `main` → `load_config`: the config path is an administrator-controlled startup argument, not HTTP input. The service unit invokes the daemon directly; there is no restricted-directory CLI contract or LLM tool interface here. Existing config-loader tests exercise explicit paths. Revisit if an untrusted caller gains control of startup arguments. |
| `7d0351cf-ed29-4c14-b043-dab1c5c4ca9c` | S1313 | Projects page: `192.168.100.50` is an input placeholder; submission uses the separately controlled field value. |
| `ecd0336f-919f-40b5-9f8b-fab6360841e2` | S1313 | DNS providers page: `10.0.0.53` is a placeholder, not a configured DNS server. |
| `7a59c7a1-e5c7-44a3-832c-4825bdcce667` | S5332 | Properties panel: the HTTP iPXE URL is placeholder text, not a request or fallback value. |
| `aaa2a641-a006-42c3-ac6e-add79509ae2e` | S5332 | Properties panel: the HTTP UEFI URL is placeholder text, not a request or fallback value. |
| `a5cad1d9-6ddd-47b8-b6d1-82bfa8cd60a5` | S1313 | `clusterBmc.ts`: the private CIDR is the editable default for a new virtual BMC network; IP allocation prefers the configured network CIDR. It is not a fixed external service endpoint. |
| `82bb9f51-a400-491d-abc7-9cbb4f639e83` | S1313 | Projects page: the private BMC address initializes an editable form field and is submitted as lab configuration, not used as an application trust anchor. |
| `f73f6f23-d1ce-48a2-a3b4-fbe8a42b63b8` | S1313 | `Canvas.tsx`: the private CIDR seeds a new editable virtual network; existing network ranges influence the selected subnet. It is not a fixed external endpoint. |
| `2d5147f3-a824-435b-9eca-c9d2d74326ac` | S2245 | Properties panel: random suffix identifies a canvas external-IP entry; it is not a credential or authorization token. |
| `e1d2af69-c382-47c9-9e04-5ccb63a755cd` | S2245 | `ExternalIpsPanel.tsx`: random suffix identifies a canvas external-IP entry; secrecy is not required. |
| `6bd0a712-1ab1-4870-b0a7-10b419d827f2` | S2245 | `canvasStore.ts`, `generateMac`: random bytes generate a virtual NIC MAC, not an authentication secret. Collision handling is a separate reliability concern. |
| `5c6d740a-e312-4021-9610-e5735576bb4c` | S2245 | `canvasStore.ts`: random suffix identifies a duplicated topology edge; authorization does not depend on its unpredictability. |
| `71e9582c-f7c6-4773-a052-39ba57acdb7a` | S2245 | `canvasStore.ts`: random suffix identifies another duplicated topology edge; it is not a credential. |

### Additional individual reviews

| Finding | Decision | Evidence |
| --- | --- | --- |
| `d7fa1f4c-2809-4436-ba25-83e9c9d234df`, `f10a2577-0eab-4733-b6c5-8b495ca3a985` (after extraction) | Accepted, S7758 | Showroom `shortHash` deliberately hashes UTF-16 code units with signed 32-bit overflow to preserve existing public route names. Replacing `charCodeAt` with code-point iteration changes identifiers for astral characters and can break previously generated URLs. Compatibility vectors cover ASCII, overflow, accented text, and astral text. This acceptance is specific to the persisted route-name algorithm, not general text processing. |
| `b1fa0327-8f2b-4d10-a2ee-486b0c72fd75` | False positive, S2245 | The extracted `duplicatedEdge` helper uses a random suffix only to distinguish canvas edge IDs. It creates no credential, authorization token, or security boundary. This is the same reviewed topology-ID use after extraction into a helper. |
| `ffaad1f4-7664-4b02-a349-1b133e51b2df` | False positive, S1313 | The DNS-provider input's `10.0.0.53` is placeholder text; its value and submission come from the separately controlled `nsServer` state. No request is sent to the placeholder. |
| `8a7439a8-08c4-4541-8d7a-f5293244591c` | False positive, S5332 | The iPXE URL is an input placeholder. The control uses `data.ipxeScriptUrl` and writes the user's entered value; the placeholder is not a default or network request. |
| `76dfe9aa-12d4-4fac-abfc-1c0d1d587e44` | False positive, S5332 | The UEFI boot URL is an input placeholder. The control uses `data.uefiBootUrl` and writes the user's entered value; the placeholder is not a default or network request. |
| `849a901d-c790-4115-8ae6-514441cc9173` | False positive, S3516 | `_ensure_cpu_host_model` deep-copies Kopf's immutable event body, then conditionally mutates the copied `spec.cpuHostModel` from the project's status. Returning the same local variable on early and normal exits does not return the same value: its content depends on the input and API response. Keeping the detached body also prevents changes to Kopf's event snapshot. |
| `881a4249-18f3-415b-b977-d006726bb9c0` | Accepted, S5778 | `test_scoped_key_cannot_open_websocket` deliberately observes rejection during either the WebSocket handshake or the first receive. Both are valid times for the server's authorization close to surface through TestClient. The assertion checks the exact authorization close code 4003, so an unrelated disconnect cannot satisfy this test. Setup remains outside the exception block. |
| `b5a4611f-485e-4c32-abb0-109c0bc564ed` | Accepted, Docker S6505 | The terminal image installs version-pinned Wetty 2.5.0 with native node-pty dependencies. Its install scripts compile required native modules; disabling them produces a terminal that fails at startup. The Dockerfile explicitly documents this requirement and removes build compilers afterward. Running dependency lifecycle scripts remains a build supply-chain risk, so this is an intentional acceptance rather than a false positive. Revisit when a vetted prebuilt terminal artifact replaces the native build. |

### Findings retained for remediation

- The projects-page password generator used `Math.random` for a real console/BMC
  password. It now reuses `generateImportPassword`; the form submission test
  verifies the password comes from cryptographic random bytes.
- The 252 reports of labels without an associated control and 156 missing
  button-type reports are not candidates for blanket dismissal. The inspected
  call sites use native HTML elements and genuinely lack the reported attribute
  or association.
- The other two nested-label reports require separate rendered verification.
- Most S6551 reports involve `Record<string, unknown>` values without an object
  guard; the two guarded call sites above do not justify dismissing that group.
- Complexity, nested ternaries, and style preferences remain actionable debt.
  Choosing to tolerate them would be an acceptance decision, not a false positive.
- Test exception reports need per-call inspection; setup helpers inside a
  `pytest.raises` block can fail before the operation under test.
- The S3 probe's fixed signing region was removed. Its client now honors the
  SDK's configured region rather than overriding it with `us-east-1`.
  Regression tests construct real SDK clients for two configured regions while
  mocking the metadata request; custom endpoint and credential handling remain
  unchanged.
