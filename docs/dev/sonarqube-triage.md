# Reviewed SonarQube security findings

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
- The S3 probe's fixed signing region remains open: support for arbitrary AWS
  source regions needs investigation rather than dismissal based solely on
  compatibility with non-AWS endpoints.
