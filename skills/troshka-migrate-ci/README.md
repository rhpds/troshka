# Using the Troshka CI migration skill

This AI skill helps catalog developers migrate an existing AgnosticV catalog
item (CI) to Troshka. It traces AgnosticD or AgnosticD-v2 and the related
workloads, assesses compatibility, and generates a template or tenant
configuration, Showroom, and a new AgnosticV CI candidate.

The skill is a set of instructions for your coding agent, not a command-line
converter. Its entry point is [SKILL.md](SKILL.md); keep the whole directory so
its references and assets remain available.

## What you need

- This Troshka checkout, including `skills/troshka-migrate-ci/`.
- Access to the source AgnosticV repository and the CI path/stage you want to
  migrate, such as `my-group/my-lab/dev.yaml`.
- Access to the referenced AgnosticD framework, workload collections, and any
  existing Showroom content. Local checkouts are useful; specify their revisions.
- The canonical `agnosticv` executable for resolving includes and overlays.
- An output location for candidate files and a location outside Git for runtime
  state, logs, and secret-bearing temporary data.

You can start an assessment without Troshka API or cluster credentials. Missing
source or tooling will be reported as unresolved. Live build/capture/testing
requires the appropriate deployment access later.

## Start with an assessment

Ask your agent to read the skill by its full path. For example, after adjusting
the paths and CI name:

```text
Use the skill at /path/to/troshka/skills/troshka-migrate-ci/SKILL.md.

Assess migration of my-group/my-lab/dev.yaml in /path/to/agnosticv,
using the currently checked-out revision. Troshka is at /path/to/troshka.
The framework and collection checkouts are available under /path/to/sources.

Identify whether this CI owns infrastructure or is a tenant on a shared cluster.
Trace its workloads and Showroom, and report feasibility with source evidence.
Save the report under /path/to/migration-output/my-lab.
Do not provision resources or modify the source CI.
```

If the skill is already installed and available in your agent's skill catalog,
you can invoke `$troshka-migrate-ci` instead of providing its full file path.
The repository package is not installed globally by creating or cloning it.

The assessment reports one of:

| Result | Meaning |
| --- | --- |
| `candidate` | Required mappings are substantiated; runtime validation is still needed |
| `partial` | Useful migration artifacts can be generated, but required work remains |
| `blocked` | A required dependency or platform capability cannot currently be supplied |

Expect an inventory of source revisions, workload dependencies, GUID and
certificate assumptions, ownership boundaries, Showroom changes, and blockers.
Dependencies needed for the lesson itself, such as AWS IAM, are called out
rather than silently removed.

**Current hardware limits:** CIs requiring GPUs or other specialized
instance-specific hardware cannot migrate to Troshka right now. The skill marks
that requirement as a blocker. An instance type used only for CPU/RAM sizing can
still map to ordinary VM resources.

Workloads that need nested virtualization must have `nested_virt: true` on each
affected VM in the generated template, including workers running nested guests.
Host/backend support must also be verified; the flag alone is not readiness
evidence. Tenant CIs require that capability in their existing cluster.

## Generate the migration candidate

After reviewing the assessment:

```text
Continue with the troshka-migrate-ci skill in generate mode for this CI.
Use the assessment in /path/to/migration-output/my-lab.

Generate the standalone Troshka template where applicable, a new AgnosticV CI
candidate, required workload adaptation diffs, and converted Showroom content.
If no Showroom exists, generate a minimal access and readiness guide.
Write candidates under /path/to/migration-output/my-lab/candidate.
Keep runtime state and logs outside Git.

Remove GUID-format and external certificate-issuance dependencies used only
for hosting. Preserve tenant uniqueness where resources are shared.
Report missing image, secret, content, and runtime access bindings explicitly.
Do not publish catalog changes or provision resources yet.
```

The agent validates the generated template against the selected Troshka schema
and resolves generated catalog configuration through AgnosticV where possible.
It distinguishes the standalone template from the catalog adapter's inputs.
An accepted YAML schema does not establish that images exist or workloads run.

## Dedicated environments: build, capture, and promote

For CIs that own their infrastructure, the intended progression is:

1. **Dev:** build from the template and run the assigned build-time workloads.
2. **Validate:** check the applications, credentials, Showroom, and TLS trust.
3. **Capture:** create a pattern from the validated project.
4. **Test:** restore that pattern into a separate project with fresh credentials.
5. **Prepare promotion:** generate test/prod and optional event YAML using the
   validated pattern, with explicit post-restore workloads where needed.

Authorize the live stage and target explicitly. Example follow-ups:

```text
Use build mode to order only the new dev CI through our existing catalog flow.
Use the approved candidate and record the resulting project and job references.
```

```text
Capture the validated development project, then validate that pattern in a
separate project with fresh credentials. Record the exact pattern identity and
check that Showroom links point at the restored project.
```

```text
Prepare promotion files for test, prod, and event using the tested artifact.
Show the stage differences and remaining checks before publication.
```

Live stages use existing supported tools and deployment interfaces. The skill
does not itself add platform APIs. It will report unavailable capabilities and
will not create publishable promotion files containing a fabricated pattern ID.

## Tenant-only CIs: reuse the existing cluster

A tenant CI consumes a provisioned or pool-supplied cluster. The skill preserves
the allocator, unique tenant/namespace identity, workload execution, scoped
credentials, and tenant cleanup. It does not deploy a cluster for each tenant or
capture the shared cluster as a pattern.

Its release artifacts are pinned workload/content revisions and allocation
configuration. Validation uses two independent tenant allocations and checks
that access and cleanup cannot affect the other tenant or the shared cluster.

**Troshka attachment support is not assumed.** If the selected Troshka version
cannot represent an external shared-cluster tenant and its access entry point,
the report identifies that platform gap and retains the native tenant flow as
an option. Generating tenant YAML does not implement a Troshka tenant-project API.

## Showroom and runtime values

Existing lessons are preserved and their access assumptions reviewed. Missing
Showroom content gets a minimal overview, verified access links/tabs, credential
guidance, and readiness checks; the skill does not invent lab exercises.

Runtime links and credentials must belong to the deployed project or tenant.
The generated content must not retain the development project's URLs or secrets.
If no content repository exists, the skill generates local content and reports
the binding/publication work still needed.

Some current native Showroom HTTPS proxy paths disable upstream certificate
verification. The skill checks the selected implementation and reports affected
paths as a limitation; generating YAML does not resolve that platform behavior.

## Resume and review outputs

```text
Resume this migration using the report and external state at the recorded paths.
Reconcile current source revisions, project/allocation state, and job status
before continuing. Tell me which previous checks need to be repeated.
```

Review the migration report, generated diffs, unresolved bindings, static check
results, and recorded live evidence. State/logs, kubeconfigs, decrypted variables,
tokens, and credentials belong outside Git.

For the detailed workflow, see [SKILL.md](SKILL.md). Maintainers can find the
offline exercise requests and acceptance rubric in [evals/cases.md](evals/cases.md).
The package has passed static and offline artifact checks; each real migrated CI
still needs its own live acceptance tests.
