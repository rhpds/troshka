# COW Storage Quotas Implementation Plan

> **For agentic workers:** Use subagent-driven-development for implementation and independent review.

**Goal:** Enforce storage quotas against measured allocated bytes with ongoing growth reconciliation.
**Architecture:** Trusted agent storage inventory feeds a physical-storage adapter; immutable virtual capacities remain provider-validation data. Admission, reconciliation, durable stop enforcement and baseline readiness share the adapter.
**Tech Stack:** Python3 troshkad, FastAPI/SQLAlchemy, PostgreSQL, React/PatternFly.
**Spec:** docs/superpowers/specs/2026-11-11-cow-storage-quotas-design.md

## Global Constraints

Preserve all current uncommitted changes. Python3 only; apply_patch edits; no live
mutations, deploy, enablement, commits or hook bypass. Maintain operation → user →
organization → project lock order, original observation timestamps, shared Redis
identity, cleanup availability and exact currency. Keep functions complexity<=15.

## Task 1: End-to-end physical storage adapter

Files: troshkad storage telemetry and tests; backend quota_resources/admission/
operations/observations/enforcement/usage/baseline plus narrowly required schema,
config and migration changes; quota frontend units/summary; docs/dev/quotas.md.

This is one coupled integration task: changing the meaning of storage_bytes must
not be delivered without its admission, reconciliation and baseline consumers.

- [ ] Trace actual disk creation, storage roots/shared bases and current quota lifecycle.
- [ ] Write focused failing tests for real sparse allocated bytes, identity/dedup and missing evidence.
- [ ] Implement bounded read-only agent measurement with explicit completeness and pool headroom.
- [ ] Integrate fresh trusted evidence, physical admission and confirmed allocation growth; test generation and locking.
- [ ] Integrate ongoing storage exhaustion and pool protection without data deletion; test retries and stale observations.
- [ ] Version baseline semantics, update rollout instructions and UI labels/provisioned detail.
- [ ] Run affected agent/backend/frontend tests, TypeScript, Ruff and diff checks; report exact evidence and limitations.
- [ ] Independent whole-change review; consolidate any fixes and run scoped re-review.

Reports and pre-edit snapshots: .superpowers/sdd/2026-11-11-cow-storage-quotas/.
