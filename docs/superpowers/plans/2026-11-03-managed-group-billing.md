# Managed Group Billing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Let managers report on group members' usage and billing and delegate group management safely.

**Architecture:** Persist group membership and management separately from global roles. Centralize billing authorization and aggregate existing metering data in scoped report APIs; expose group administration and team billing through the existing frontend.

**Tech Stack:** FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, Next.js, React, PatternFly, pytest, Vitest.

**Spec:** docs/superpowers/specs/2026-11-03-managed-group-billing-design.md

## Global Constraints

- Current membership grants access to the member's available billing history.
- Removing a member or manager revokes access on subsequent requests.
- Ordinary membership alone does not grant access to other members' data.
- Operators gain no implicit team access.
- Do not modify project control, console, credential, rate-editing, statement-finalization, or user-administration permissions except sole-manager user-deletion protection.
- Managers can add/remove managers in their own group; never remove its last manager, including through user deletion. Serialize mutations using parent-row locks and recheck authorization after locking.
- Member assignment and group metadata/deletion remain site-admin-only.
- A manager is not automatically a member. Managers add existing users by exact email without directory browsing.
- Use UTC calendar-month boundaries and one shared as-of timestamp per report.
- Never add project-invoice totals to statement totals. Final statements override provisional calculations.
- Use python3, apply_patch for edits, no Co-Authored-By lines. Do not deploy or push.
- Long tasks must use background execution; if run_in_background is unavailable, use the exec tool's asynchronous session/yield mechanism without sleep loops.
- Keep this existing clean feature checkout; no new worktree needed.

### Task 1: Group persistence, access policy, and report APIs

**Files:**
- Create models/managed_group.py, schemas/managed_group.py, services/group_access.py, services/group_metering.py, api/groups.py, api/group_metering.py under src/backend/app.
- Modify src/backend/app/models/__init__.py, main.py, api/metering.py, api/users.py.
- Create src/backend/alembic/versions/d93b8a6c1042_add_managed_groups.py (parent c82589e4261c).
- Test src/backend/tests/test_groups_api.py, test_group_metering.py, test_managed_groups_migration.py; extend test_metering_api.py where appropriate.

**Interfaces:**
- GET /api/v1/groups returns [{id,name,description,member_count,manager_count}]; admin sees all, manager sees managed groups only.
- GET /api/v1/groups/{id} returns {id,name,description,members:[{id,email,display_name}],managers:[{id,email,display_name}]}.
- POST /api/v1/groups takes {name,description}; PATCH /groups/{id} takes same fields; DELETE /groups/{id} deletes group (admin only).
- PUT /groups/{id}/members/{user_id}, DELETE same path: admin-only membership assignment.
- POST /groups/{id}/managers takes {email}; DELETE /groups/{id}/managers/{user_id}: admin or current manager. Return updated group detail for assignments.
- GET /metering/groups/{id}/report?year=2026&month=11&owner_id=optional returns {group:{id,name},as_of,period_start,period_end,currency:"USD",total_usd,members:[{owner_id,owner_email,display_name,status:"final"|"provisional"|"unavailable",total_usd,by_project:[{project_id,project_name,subtotal,by_kind:[{kind,hours,qty_hours,unit_rate,subtotal}]}]}],active_projects:[{id,name,state,owner_id,owner_email,total_usd,line_items:{by_kind:[]}}]}.
- GET /metering/groups/{id}/invoices?owner_id=optional and /statements?owner_id=optional return existing summary shapes with all history for current members. UI filters dates locally on these bounded group documents. Invoice and statement detail use existing metering endpoints with the shared policy.
- Service policy functions: billing_owner_ids(db,user) returns a set including self for non-admin, None for admin; require_managed_group(db,user,group_id,lock=False) returns authorized group or 404. Implementations may add small helpers but preserve these contracts.
- Existing invoice/statement list endpoints accept own_only: bool=False; when true filter to caller even when caller can manage other users. This preserves personal billing presentation.

- [ ] Write access tests first using TestSession and get_current_user dependency overrides. Core checks:
```python
assert manager_client.get(f"/api/v1/groups/{other_group_id}").status_code == 404
assert manager_client.delete(f"/api/v1/groups/{group_id}/managers/{sole_manager_id}").status_code == 409
assert member_client.get(f"/api/v1/metering/invoices/{peer_invoice_id}").status_code == 403
```
Use actual existing TestClient override patterns rather than introducing three permanent clients. Include multiple managers, overlapping groups, revocation, ownerless documents, both project-metering paths, forbidden controls, admin-only membership, unknown email, duplicate assignments, user deletion invariant.
- [ ] Run new tests and confirm failure on missing feature.
- [ ] Implement additive models/migration, router registration, mutation locking, policy extension. Lock all affected groups in a consistent order during user deletion; reject sole-manager deletion. For report calculations batch load scoped projects/intervals/statements, preserve deleted-project draft contributions and replace living project entries by ID; prefer frozen final statements.
```python
if statement is not None and statement.status == "final":
    # Frozen totals and lines are authoritative, even if interval data remains.
    return statement.total_usd, statement.line_items
```
- [ ] Validate accounting tests for monthly clipping, open intervals, provisional drafts, final precedence, correct quantity units, empty/no data cases, duplicate suppression. Include a two-session PostgreSQL concurrency check when a disposable database is available; otherwise document static lock reasoning and the validation limitation.
- [ ] Test this migration upgrade/downgrade using a disposable SQLite connection and PostgreSQL DDL compilation; do not run the full historical migration chain on development DB.
- [ ] Run relevant backend tests, formatting/lint/type checks for changed files. Commit only task-owned files with absolute git paths. Report exact test commands/results and API contract deviations.

### Task 2: Group administration and team usage UI

**Files:**
- Create src/frontend/src/app/admin/groups/page.tsx and src/frontend/src/app/team-billing/page.tsx with small supporting components under src/frontend/src/components/groups/ and billing/ as needed.
- Create src/frontend/src/lib/group-billing.ts and focused .test.ts/.test.tsx files for new logic/pages.
- Modify src/frontend/src/app/layout.tsx for navigation and page labels; update docs/dev/subsystems.md.
- Modify src/frontend/src/app/metering/page.tsx to request own_only=true invoice/statement lists for non-admins after auth is known, preserving admin all-owner behavior. Update its existing tests for these URLs.

**Interfaces:** Consume Task 1 routes/shapes above. Personal metering remains intact. Add Groups under admin navigation and Team Usage & Billing for signed-in users; the latter handles no managed groups explicitly and provides manager controls in the selected group.

- [ ] Read frontend AGENTS.md and local Next.js docs for client components before editing.
- [ ] Write UI tests for group switching, errors, selection filters, exports, and manager assignment/removal including 409 errors. Add a pure CSV test:
```typescript
expect(csvCell('=SUM(A1)')).toBe("'=SUM(A1)");
expect(csvCell('a,b')).toBe('"a,b"');
```
- [ ] Implement admin create/edit/delete, membership selection from /users, manager exact-email add/remove. Keep ordinary managers on scoped group detail with no global user directory. Require confirmation for deletion/removal and show API error messages.
- [ ] Implement group/month/member selectors, current-members/history notice, provisional/final status, per-user/project/resource monthly usage, separate active lifetime spend, separate invoice and statement tables, detail expansion, and CSV downloads. Exports include owner/group/period/currency/unit/qty_hours/rate/cost. Separate document totals and line subtotals to prevent misleading summed CSV totals. Formula-escape user-controlled text, including leading whitespace/control characters before formula prefixes.
- [ ] Cancel/ignore stale fetches when scope changes; clear old details and exports immediately. On 403/404 discard unauthorized content. Surface incomplete data explicitly. Backend month is UTC; initialize default month from UTC time.
- [ ] Run new tests and existing metering tests, TypeScript check and changed-file ESLint. Fix relevant failures. Update operational docs with permission boundaries, last-manager behavior, current-members history semantics, and export format.
- [ ] Commit task-owned files and report test evidence. Whole-branch review follows both tasks; resolve findings before final report.
