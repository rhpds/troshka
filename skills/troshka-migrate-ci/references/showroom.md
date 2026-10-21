# Showroom conversion and creation

Showroom is required for dedicated environments and shared tenants. Discover its
content repository/ref, Antora configuration, supplemental variables, UI tabs,
terminals, proxies, deployment role, authentication, and user-info output. Inspect
the content at the actual selected ref rather than trusting a repository URL.

## Select one deployment owner

Troshka supports native Showroom configuration in infrastructure templates; some
catalog items instead use `agnosticd.showroom.vm_workload_showroom` on a bastion.
Choose one supported deployment path and document its lifecycle owner. Existing
content repositories do not force one path. Avoid deploying two Showrooms or
discarding authored content just to change hosting.

For shared tenants, retain the existing per-tenant Showroom hosting if it works.
Check whether it runs in the tenant namespace, on an allocated bastion, or through
an independent access service. Do not add a cluster or pretend a tenant-only
project is supported merely to host the entry point. Missing hosting/binding is
a platform gap. Each tenant's access must remain scoped to that tenant.

## Convert existing content

Preserve lesson order and substantive instructions. Review content/configuration
diffs for hardcoded project URLs, GUID-derived hostnames, cloud portals, SSH paths,
API endpoints, credentials, DNS, TLS/browser trust, and obsolete infrastructure.
Map verified access values to the deployed project or tenant at runtime using
supported mechanisms. Retain the provenance of adapted content and report author
decisions when a removed service changes the lesson.

Do not perform global URL replacement. An application may depend on redirects,
websockets, cookies, OAuth companion hosts, absolute paths, or SNI. Choose the
supported reverse-proxy/access arrangement and validate the whole login flow.
An HTTP 200 response alone does not prove a terminal or embedded console works.

## Generate a minimal Showroom if absent

Create a small content candidate following the actual selected Showroom/Antora
layout: component metadata, a navigation entry, and an overview page describing
verified services, how to open supported tabs, where authenticated runtime
credentials are supplied, and readiness checks. Do not invent exercises, accounts,
credentials, commands requiring unverified tools, or a published content URL.

If no content repository exists, produce the local content artifact and explain
the required content binding/publication step. Keep that candidate partial until
a supported source or delivery mechanism is supplied. Do not silently publish a
repository or point `content_repo` at an example URL that does not exist.

Template-native examples must use the target schema. The inspected schema accepts
this tab shape when `bastion`, `app`, and `mgmt` are verified topology names:

```yaml
showroom:
  enabled: true
  dns_network: mgmt
  tabs:
    - name: Terminal
      type: terminal
      vm: bastion
      network: mgmt
      ssh_user: lab-user
    - name: Application
      type: proxy
      vm: app
      network: mgmt
      proxy_path: /app/
      proxy_port: 8080
      proxy_tls: false
```

This is a partial configuration example, not a deploy-ready template. Bind a
real content source and supported runtime SSH credentials. Verify that the app
supports the selected path prefix before using `/app/`. Do not write passwords
to `ssh_pass` in Git or assume a secret-reference syntax exists in that field.
Cluster-terminal/console tabs need schema and implementation verification too.

## Runtime bindings and trust

Bake reusable content only. Derive external portal/Showroom/application URLs
from the actual deployment or tenant allocation; deliver access tokens and
credentials through its authenticated user-output mechanism. The inspected
provider role publishes `troshka_portal_url` and `troshka_showroom_url`; verify
that they refer to the new deployment and do not copy them into reusable content.

Do not insert cluster-admin kubeconfigs into tenant content or pattern disks.
Validate runtime password refresh and ensure the original credentials no longer
grant unintended access. Verify token scope and expiry where applicable.

The inspected native Showroom proxy code in
`src/backend/app/services/showroom_scaffold.py` and
`src/frontend/src/lib/showroomTabs.ts` emits `proxy_ssl_verify off` for some
HTTPS upstream paths. `proxy_tls: true` means HTTPS transport, not verified CA
trust. Reinspect the target revision and flag affected HTTPS paths as failing the
design's verification requirement unless a supported verified path is established.
Do not claim template generation fixes this platform limitation, and do not add
unsupported CA/proxy fields to hide it.

## Acceptance

Check content rendering, lesson links, terminal interaction, console login and
OAuth redirects, application behavior, and trust from the relevant clients.
Repeat in a fresh restored project, or two distinct tenant allocations. Confirm
links, identities, namespaces, and credentials belong to the intended environment.
No stale development URL may remain. Record untested paths as unverified.
