# Follow-ups from project deletion validation

Recorded November 17, 2026. The user requested that work outside the agreed
deletion and early-failure recovery scope be recorded for later, without adding
it to the current implementation. These entries are not claims of completed
fixes. Remaining deletion acceptance and release gates stay in
`project-deletion-release-gate.md`.

## Capture restore verification uses conflicting deadlines

**Status:** Confirmed during capture testing; deferred by the user's explicit
direction to finish deletion rather than expand capture work. No deadline fix
is included.

After the reviewed capture-resume correction, both preserved dev attempts
created one ready CSI snapshot. Restore authorization then held. A read-only
reproduction on November 18 at 05:46 UTC used the configured 210-second scan
and 15-second command limits, but the snapshot observation adapter's independent
55-second inventory deadline expired during backend resolution. The subsequent
producer LIST raised `managed_observation_timeout` after 109.95 seconds overall.
A separate fresh source read matched the original source identity.

Review aligning the read-only adapter with the existing scan deadline in a
separate capture change, preserving per-request limits and authority checks.
The two failed API pattern records and their ongoing operator resources remain
preserved. Successful capture/export/restore is not a prerequisite for this
deletion release; any demonstrated effect on deletion safety must be assessed
explicitly. Unknown snapshot custody must continue to block unsafe cleanup.

## Capture recovery can duplicate a live job

**Status:** Confirmed; deferred, no fix included.

**Trigger:** Restart the API while a pattern capture is still executing on an
RQ worker. `_startup_resume_pattern_captures` in `src/backend/app/main.py`
enqueues every pattern whose state is `capturing`, without first distinguishing
a live capture from an abandoned one.

**Observed evidence:** During the dev candidate API rollout at approximately
2026-11-18 01:12 UTC, pattern `f253cde4-b5ef-4663-9ce7-adc1eb8f2958` already had
job `cca36cec-a5ea-4008-9f99-eb9a93612f42` running. Startup enqueued
`f86090fe-aa67-4d80-9c49-2e7a78fc9486`; both jobs subsequently appeared in the
started registry on separate workers. The capture request changed while both
jobs remained active. No completed duplicate export or data loss was established.

**Impact:** Duplicate capture execution and competing progress/request updates
are possible during an otherwise routine API restart. This is separate from
VM/VMI leftovers blocking namespace deletion.

**Suggested next step:** Give capture recovery an exact tracked execution
identity and resume only abandoned work. Cover API restart with a live capture,
worker loss, concurrent startup, and unknown tracking state. Preserve the
original restart/quiescence options and do not clear another job's tracking.
Review that design in its own change.

**Current operational constraint:** Avoid API restarts during an active capture
when possible. Preserve job tracking and inspect it before rollout; do not
force-kill workers or erase tracking to make a rollout appear idle.

## Observer lease can delay capture after a backend replacement

**Status:** Observed during dev acceptance; deferred, no fix included.

At 2026-11-18 03:50:03 UTC, the observer lease for disposable project
`0db5e15e-7630-4c2f-916a-6c3670994d00` had 1,945 seconds remaining.
The observer uses a 3,600-second lease without renewal, implying acquisition
around 03:22:29 UTC, before the current backend started at 03:40:57 UTC.
This strongly indicates a lease left by the previous backend. The current
snapshot checkpoint validated, but had no ACK or producer receipt; capture
had not created its snapshot. Lease contention is silently skipped.

**Current handling:** Preserve the active capture and let the lease expire
normally (estimated 04:22:29 UTC). Do not erase the lease, restart the API, or
submit a duplicate capture. Expiry is not evidence of successful recovery;
verify the checkpoint and capture afterward.

**Suggested next step:** Review observer lease ownership and replacement
recovery separately, including observability of contention. Preserve exclusion
of genuinely live observers; a shorter timeout alone is not a safety proof.

## Cleanup dispatcher can overlap long verification attempts

**Status:** Observed during deletion acceptance; scheduling correction deferred.

The pending-cleanup dispatcher uses a 60-second enqueue exclusion key. On
November 18, two reconciliations of the same disposable Delete each ran about
206 seconds before failing with `csi_identity_scan_limit`; subsequent automatic
attempts overlapped. Existing identity and transaction guards remain in place,
and no destructive action occurred during these failed observations. The direct
release blocker is the verification budget; overlapping work is a separate
efficiency/recovery concern, not evidence of duplicate deletion.

Review job-aware exclusion and recovery after worker loss separately. Preserve
legitimate retries and do not clear job tracking or extend an exclusion lease
without an owner/liveness design.

## Test harness warning cleanup

**Status:** Low priority; deferred, no runtime defect established.

The settled-root original-source replay emitted `PytestAssertRewriteWarning`
for `anyio` because the replay imported it before pytest plugin setup. Final
passing runs did not report that warning. Earlier broad validation also recorded
urllib3, shutdown logging, and Node localStorage warnings. Clean these up in a
test-tooling change rather than altering deletion behavior to suppress output.
Evidence remains in the native-deployment-origin task's validation reports.

## Operator debug logging includes sensitive project fields

**Status:** Observed during dev acceptance; deferred from deletion implementation.

Kopf DEBUG messages serialize full TroshkaProject objects, including inline
credential and guest access fields. Broad operator-log searches can therefore
expose those values. Do not copy raw debug object dumps into reports or issues;
use allowlisted event/error summaries. The local acceptance log is restricted to
its owner. Review logging levels/redaction and credential placement separately,
and assess the affected credentials without reproducing their values here.

## Preserved capture attempts block full release accounting

**Status:** Confirmed on November 18; bounded accounting recovery approved.

The preserved dev projects `430db3b3-e7a8-471c-94eb-8a7786bb66f9` and
`0db5e15e-7630-4c2f-916a-6c3670994d00` both report
`csi_snapshot_backend_unresolved` in their latest collection errors and inventory
blockers. Shared-domain accounting also reports failure. The full release health
check covers these enrolled projects, so finishing the separate disposable
deletion will not by itself satisfy that gate. No capture replay, accounting
recovery, evidence removal or gate exception has been performed.

The user approved accounting recovery for these two attempts, preserving their
data and failed records and running no new captures. The snapshot reader's
55-second inventory deadline can expire during intervening Ceph checks even
while the configured collection budget remains. A narrow correction passes the
existing absolute scan deadline to snapshot reads while retaining the five-second
per-request cap and ordinary inventory default. Live accounting recovery and
the full release gate still require verification; this is not a deployment claim.
The first read-only replay then reached the final root consistency check for both
fixtures: storage identity and graph verification passed, but controller retry
counters and completed execution IDs changed during the scan. A reviewed
comparison correction excludes only that bookkeeping, retaining active execution
IDs, operator/outcome identity, all producer/checkpoint fields and project spec.
Read-only evidence is retained in
`/private/tmp/troshka-v16-dev-health-details.json` and the native-deployment-origin
task ledger.

## Release-check interpretation of partial project coverage

**Status:** Observed during read-only diagnostics; deferred for separate review.

`scripts/collector_release_check.py:observe` derives project status from report
acceptance, reason codes, collection errors and inventory blockers. It does not
inspect each project's computed coverage. Report
`60913c0c-1d3d-4ae4-8286-d0a0d8b78f8b` has partial project coverage but is classified
as complete by that observer. The active-cleanup guard independently blocks the
current release, and deletion settlement independently rejects the incomplete
proof. Review whether the release checker also needs an explicit coverage check
for projects without active cleanup. No gate behavior was changed here.
