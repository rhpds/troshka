# Catalog Migration Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. Use inline execution unless the user authorizes delegation.

**Goal:** Deliver a portable AI skill that assesses an AgnosticV CI, generates an isolated Troshka migration candidate with Showroom, and guides validated template-to-pattern promotion.

**Architecture:** Package concise skill instructions with task-specific references and report templates. Reuse canonical AgnosticV merging, existing Troshka schemas, and the AgnosticD provider flow. The initial implementation generates and validates candidates; live build, capture, and promotion remain explicit resumable actions using existing tools.

**Tech Stack:** Markdown skill instructions, YAML/JSON artifacts, canonical AgnosticV CLI, existing Ansible and Troshka schema tooling.

**Spec:** `docs/superpowers/specs/2026-10-21-catalog-migration-skill-design.md` (approved by the user on 2026-10-21).

## Global Constraints

- Tenant amendment: classify dedicated, shared-cluster tenant, or hybrid before generating infrastructure. Tenant-only CIs reuse supplied cluster allocations; no per-tenant cluster build or shared-cluster pattern capture. Preserve unique tenant ownership, scope cleanup, and report missing Troshka attachment capabilities. Apply this amendment to discovery, conversion, Showroom, lifecycle, and evaluation tasks.

- Assessment covers both AgnosticD and AgnosticD-v2 and source providers including CNV, AWS, Azure, GCP, and IBM Cloud.
- This is not a promise of automatic conversion for every CI.
- Never persist decrypted secrets in reports or generated repository artifacts.
- Do not perform blind GUID substitution.
- Isolation is not a substitute for certificate validation.
- Showroom is a required output.
- Preserve the original CI during validation.
- Do not rebuild separately for each stage.
- Never interpret a request for feasibility as authorization to provision resources or publish a production catalog change.
- Always use `python3`; never add commit co-author trailers; use absolute paths when staging files.
- No backend changes, production deployments, external repository writes, or global skill installation are required for this implementation.

## File Structure

Create the distributable source package at `skills/troshka-migrate-ci/`:

| File | Responsibility |
| --- | --- |
| `SKILL.md` | Triggering, inputs, mode selection, core invariants, reference routing |
| `references/discovery.md` | Canonical merge, source tracing, legacy/v2 distinction, evidence |
| `references/conversion.md` | Infrastructure contract, isolation, workload phase ownership |
| `references/showroom.md` | Existing-content conversion and minimal Showroom generation |
| `references/lifecycle.md` | Dev build, capture, restore validation, promotion, resumption |
| `assets/migration-report.md` | Developer-facing assessment and candidate report template |
| `assets/migration-state.example.json` | Secret-free example of external resumable state |
| `evals/cases.md` | Realistic offline behavioral exercises and acceptance rubric |

The repository package is the versioned deliverable. Installation into a developer's skill directory is a subsequent explicit action. References must accept repository roots supplied by the developer; do not hardcode this workstation's paths into the distributable skill.

## Task 1: Discovery and feasibility assessment

**Files:** Create `SKILL.md`, `references/discovery.md`, `assets/migration-report.md`, and `evals/cases.md` under `skills/troshka-migrate-ci/`.

**Interfaces:** Consumes a source repository/root, CI path, stage, requested revision, and optional local dependency roots. Produces an assessment with resolved sources, dependency decisions, evidence, and blockers. Later tasks extend that report.

- [x] **Step 1:** Read the skill-creator guidance at `/Users/prutledg/.codex/skills/.system/skill-creator/SKILL.md`. Inspect `src/backend/app/services/workloads/agnosticv.py` and canonical CLI help. Confirm merge commands and source revision handling rather than reconstructing AgnosticV merge semantics.
- [x] **Step 2:** Add offline assessment exercises before writing instructions: (a) merged v2 OpenShift CI with a provider-dependent pre-workload; (b) legacy CI with a v2-incompatible playbook; (c) inaccessible role ref; (d) AWS IAM as lesson subject versus AWS VMs as hosting. Expected outcomes respectively include source tracing, explicit compatibility work, unresolved status, and retained-cloud versus replaceable-infra decisions.
- [x] **Step 3:** Write the skill entrypoint using this frontmatter:

```yaml
---
name: troshka-migrate-ci
description: Assess and migrate AgnosticV catalog items from AgnosticD or AgnosticD-v2 to Troshka templates and patterns, including workload compatibility and Showroom conversion.
---
```

Define modes `assess`, `generate`, `build`, `capture`, `validate-pattern`, and `prepare-promotion`. Default a feasibility request to assessment. Describe later modes as supported procedures, with runtime availability checked before execution.
- [x] **Step 4:** Write discovery guidance: resolve overlays, deployer/EE/SCM settings, requirements, roles, config playbooks, dynamic includes, Showroom sources, and lifecycle hooks. Record source revisions and path/line evidence without dumping merged secrets. Mark missing or unevaluated sources unresolved. Inspect all five provider families when present; avoid speculative provider-specific translation tables.
- [x] **Step 5:** Define the report interface: source manifest; `reusable`, `infra_mapping`, `workload_adaptation`, `external_required`, or `unresolved` per dependency; source evidence; replacement; validation; feasibility; blockers. Separate source assessment from executed validation.
- [x] **Step 6:** Run the four exercises in temporary output directories. Check findings against raw sources and the stated expected outcomes; refine instructions on demonstrated failures. Commit the completed assessment capability.

## Task 2: Template and catalog candidate generation

**Files:** Create `references/conversion.md`; extend `SKILL.md`, `assets/migration-report.md`, and `evals/cases.md`.

**Interfaces:** Consumes Task 1's assessment and current target schemas/configuration. Produces a template, proposed workload adaptations, a new CI candidate, and a phase ownership table, with unresolved requirements surfaced.

- [x] **Step 1:** Inspect `schemas/troshka-template.schema.json`, representative `src/backend/templates/` files, and current integration source. Local starting points are `/Users/prutledg/agnosticd-v2/ansible/configs/troshka/` and `/Users/prutledg/agnosticv/troshka/README.md`. Follow provider collection references to establish actual variable names and capture semantics. Existing checkouts may be stale: record their revisions and distinguish local evidence from current remote behavior.
- [x] **Step 2:** Add exercises for Linux inventory group preservation, GUID-derived password versus internal hostname, external ACME dependency, and a role that creates an external resource during destroy. Judge the generated mapping and lifecycle handling, not matching report wording.
- [x] **Step 3:** Write conversion guidance covering CPU/memory/image requirements, disks, networks, group/host variables, access transport, cluster outputs, bastion services, and readiness. Translate provider facts by semantic use. Replace internal GUID uniqueness and external certificate issuance; retain TLS verification and distribute trust.
- [x] **Step 4:** Require a workload phase table with role/source ref, build or post-restore phase, executor, inputs/outputs, replay behavior, and cleanup owner. Identify AAP versus Troshka ownership explicitly. Distinguish unsupported legacy execution from a supported candidate and report any required adaptation.
- [x] **Step 5:** Describe candidate generation using the target repository's actual include/stage conventions. Dev uses template mode. Test/prod/optional event use the validated pattern or pattern_workloads mode. Before capture, label promotion artifacts incomplete and non-publishable; do not insert a fake pattern ID or present a guessed version field as valid. Reuse shared configuration and register non-catalog template files as related files where required.
- [x] **Step 6:** Generate candidates in temporary directories from sanitized examples. Parse YAML and validate Troshka templates against the actual JSON schema using available repository tooling. Use canonical AgnosticV merging and existing static checks for CI candidates. Record missing tooling as unverified, and do not print merged secret values. Commit the conversion capability.

## Task 3: Required Showroom conversion or generation

**Files:** Create `references/showroom.md`; extend `SKILL.md`, the report template, and exercises.

**Interfaces:** Consumes verified template services, workload outputs, and existing Showroom sources when present. Produces converted or minimal Showroom content/configuration plus runtime access bindings and author-review findings.

- [x] **Step 1:** Inspect Showroom fields in `schemas/troshka-template.schema.json`, existing example templates, `src/frontend/src/lib/showroomTabs.ts`, `src/frontend/src/lib/showroomPortForwards.ts`, and the applicable backend Showroom implementation. Inspect AgnosticD-v2 `post_software.yml` for its access behavior. Establish supported variables and endpoints before authoring examples.
- [x] **Step 2:** Add paired exercises: existing Showroom with hardcoded source-project links and GUID-derived application URLs; no Showroom with one terminal and one verified application endpoint. Include a lesson step requiring a cloud console that migration removes.
- [x] **Step 3:** Write guidance to preserve authored lessons, adapt access bindings, identify obsolete instructional steps, and flag curriculum decisions. In the no-Showroom path, generate only an overview, verified access methods, credential guidance, and readiness checks.
- [x] **Step 4:** Require links and credentials to bind to the restored deployment using supported mechanisms. Keep development credentials and development-project URLs out of reusable content. Document which content is baked and which values are populated after restore.
- [x] **Step 5:** Execute both exercises offline; inspect content diffs, generated configuration, and available schema checks. Verify that removed cloud-console functionality is reported rather than replaced with invented instructions. Commit the Showroom capability.

## Task 4: Resumable build, capture, validation, and promotion procedures

**Files:** Create `references/lifecycle.md` and `assets/migration-state.example.json`; extend entrypoint, report, and exercises.

**Interfaces:** Consumes generated artifacts plus current authorization and runtime capabilities. Produces externally stored state, validation evidence, and stage configurations that reference the validated artifact.

- [x] **Step 1:** Inspect current pattern capture/restore APIs, schema models, provider collection, and `docs/dev/pattern-capture-v2-spec.md`. Document source locations for invocation discovery; avoid freezing speculative API requests into the skill. Verify whether pattern references are immutable; report any missing guarantee.
- [x] **Step 2:** Define the external state example with `schema_version`, source manifest, artifact fingerprints, completed stages, resource references, validation evidence paths, and blockers. Store it outside Git at a developer-selected location. Do not store secrets, decrypted variables, or transferable permission assertions. Resume by reconciling live resources and current session authorization.
- [x] **Step 3:** Write procedures for development build/readiness, pattern capture, separate-project restore with fresh credentials, and promotion of the same tested artifact. Cover supported start/stop, retry ordering, external cleanup, and destruction isolation. Require verification of runtime links and certificate trust from relevant clients, beyond simple HTTP status checks.
- [x] **Step 4:** Add exercises for stale source fingerprints, inaccessible live state, a captured Showroom URL pointing at dev, duplicate post-restore registration, and mutable pattern references. Expected results: invalidate affected stages, mark unknown, fail restore acceptance, fix/reject non-idempotent replay, and block unsubstantiated promotion respectively.
- [x] **Step 5:** Walk through each exercise with fabricated resource responses in a temporary workspace. No live build is needed to test decision handling. Record that procedural exercises do not establish live migration success. Commit lifecycle guidance.

## Task 5: Package validation and developer handoff

**Files:** Finalize all files under `skills/troshka-migrate-ci/`; update the design status to implemented only when its initial skill scope is delivered and validated.

**Interfaces:** Consumes the package and behavioral exercise results. Produces a reviewable portable skill and a concise account of tested behavior and live verification limitations.

- [x] **Step 1:** Run the skill validator:

```bash
python3 /Users/prutledg/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/prutledg/troshka/skills/troshka-migrate-ci
```

- [x] **Step 2:** Resolve each relative reference link and check package contents for workstation-specific assumptions, unfinished scaffolding, embedded credentials, unsupported API/schema claims, and accidental live-action defaults. Omit optional UI metadata unless needed.
- [x] **Step 3:** Re-run representative OpenShift, Linux VM, legacy AgnosticD, and cloud-dependent exercises, covering existing and missing Showroom. Compare actual artifacts and decisions with the rubric. Use inline evaluation unless the user authorizes independent agent evaluation.
- [x] **Step 4:** Check spec coverage: discovery → Task 1; compatibility/isolation/catalog generation → Task 2; Showroom → Task 3; lifecycle, phase replay, runtime access, stable artifact promotion, and resumability → Task 4; package and observable behavior → Task 5. Resolve omissions before claiming completion.
- [x] **Step 5:** Run `git diff --check`, review the entire package diff, stage absolute paths, and commit. Follow existing hook remediation if a push is later authorized. Deliver the package path, invocation example, validation results, and any source/runtime limitations. Do not install globally or publish catalog changes implicitly.

## Verification Boundaries

The skill is deliverable after package validation and successful offline behavioral exercises. A particular migrated CI is deliverable only after its real dev build and separate pattern deployment satisfy the design acceptance criteria. Keep those claims distinct in reports and handoff.

The exercise corpus may use sanitized temporary CI/role/content fixtures assembled from observed source shapes. Do not copy secrets, production provision data, or whole private catalogs into this repository. Do not add scripts that merely test instruction wording; add deterministic helpers only if actual repeated work demonstrates a need.

## Implementation Record — 2026-10-21

Delivered `skills/troshka-migrate-ci/` on `codex/catalog-migration-skill`.
Tenant-only requirements were added during execution: retain existing allocations,
preserve unique tenant ownership, skip shared-cluster capture, and report missing
Troshka attachment support. No platform attachment API was implemented.

Validation: skill frontmatter validator passed; 22 disposable artifact checks
passed against the current JSON schema and installed canonical AgnosticV CLI.
Checks cover accepted/invalid standalone fields, inventory groups, tab references,
dedicated catalog include/stage merging, and tenant merging without cluster build
or capture. The sanitized fixture has no production catalog schemas, so production
catalog acceptance remains unverified. All 16 exercises have an author-guided
source walkthrough; they are not independent model forward-tests.

Independent package review found one minor assessment/generation ambiguity, which
was corrected. No important findings remained. Evidence and generated sanitized
candidates are outside Git at `/private/tmp/troshka-migration-skill-work/`.
No live resources were provisioned, captured, modified, released, or published.

Execution adjustments: used a feature branch in the current checkout rather than
a second worktree; validated prose with source exercises/artifacts rather than
tests of instruction wording; packaged the related task files in one commit
rather than committing incomplete reference links between tasks. These choices
leave real model forward-testing and CI-specific live acceptance as explicit
limits. The skill is portable source, not a globally installed skill.
