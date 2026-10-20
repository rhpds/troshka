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
