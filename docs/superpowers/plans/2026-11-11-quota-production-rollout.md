# Quota production rollout and migration

Authorized by the user: deploy to production and implement quota migration strategy.
Extends the approved organization quota and COW designs in `../specs/`.

1. Verify production revision, health, idle job registries without cleanup, and enforcement off.
2. Back up PostgreSQL to an explicitly approved private destination, restore in isolation,
   and rehearse additive schema upgrade to `c935e6fa0421` and rollback-only baseline import.
3. Package the baseline CLI in the backend image, add read-only schema/enforcement
   preflight, and document resumable phase gates and non-destructive rollback.
4. Resolve required repository checks; independently review migration tooling and
   release-check corrections. Commit tested feature/migration changes without bypasses.
5. Build immutable release images. Apply the tested additive schema before promoting
   application images. Keep enforcement disabled and preserve existing workloads.
6. Deploy backend/frontend/worker/tunnel, verify every replica and HTTP health, and
   capture production quota preflight/inventory reports without exposing credentials.
7. Only import live baselines with genuine maintenance/provider evidence. Unresolved
   history, unsupported resources, or missing physical evidence remain explicit blockers;
   do not fabricate zeros, delete resources, or enable an unready baseline.

Rollback uses previous application digests with the expanded schema retained; a
destructive downgrade is not part of this rollout. Private backups and reports stay
outside Git. Existing feature reviews remain the basis for unchanged code; new
tooling and check fixes receive scoped independent review.
