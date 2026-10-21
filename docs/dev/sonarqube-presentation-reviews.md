# Accepted presentation conditionals

Review date: 2026-10-20. Source revision: `58708617` (before the local cleanup).

These individual `typescript:S3358` findings are accepted maintainability choices,
not false positives. Each expression is confined to JSX presentation, has no calls
or side effects, and selects only literal colors, icons, labels, or ARIA sort
values. Keeping a short status-to-presentation choice beside its rendered element
is reasonable here. This does not accept conditionals that select credentials,
network targets, deployment parameters, or other operational data, and does not
disable the rule. Revisit if a listed expression grows or acquires side effects.

| Issue | Source | Reviewed expression |
| --- | --- | --- |
| `b4488185-ec13-4fba-a443-008b520ce4b2` | `src/frontend/src/app/projects/page.tsx:1472` | `p.ocp_status === "ready" ? "rgba(74,222,128,0.15)" : p.ocp_status === "error" ? "rgba(248,113,113,0.15)" : "rgba(251,191,36,0.15)"` |
| `fc100d55-e42f-455e-bbd6-4fa2f34a3f03` | `src/frontend/src/app/projects/page.tsx:1473` | `p.ocp_status === "ready" ? "#4ade80" : p.ocp_status === "error" ? "#f87171" : "#fbbf24"` |
| `ed3a05d1-665d-4138-8f86-77f4409dbfa3` | `src/frontend/src/components/canvas/Palette.tsx:1218` | `disabled ? item.type === "project-ceph" ? "One Ceph Storage per project" : "One showroom per project" : undefined` |
| `ded87956-f196-408e-ae79-2beb9f9314b3` | `src/frontend/src/components/canvas/Palette.tsx:1071` | `ocpHealth.phase === "ready" ? "#4ade80" : ocpHealth.phase === "error" &#124;&#124; ocpHealth.phase === "timeout" ? "#f87171" : ocpHealth.phase === "warning" ? "#fbbf24" : "var(--pf-t--global--text--color--regular)"` |
| `e20eaf5b-6f1a-47f2-9ce7-39ce84c101a8` | `src/frontend/src/components/canvas/Palette.tsx:1071` | `ocpHealth.phase === "ready" ? "#4ade80" : ocpHealth.phase === "error" &#124;&#124; ocpHealth.phase === "timeout" ? "#f87171" : ocpHealth.phase === "warning" ? "#fbbf24" : "var(--pf-t--global--text--color--regular)"` |
| `221c9e85-e8d7-4bff-926f-a95a2f5f572b` | `src/frontend/src/components/canvas/Palette.tsx:1083` | `isGood ? "#4ade80" : isBad ? "#f87171" : "#fbbf24"` |
| `87ead00f-5dd2-48ac-af2b-72b9757831db` | `src/frontend/src/app/metering/page.tsx:369` | `active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"` |
| `c4f07d0b-9040-421f-8df1-11d7db3cf867` | `src/frontend/src/app/metering/page.tsx:372` | `active ? (sort.dir === "asc" ? " ↑" : " ↓") : ""` |
| `3e7f067c-b82c-4156-98de-1312b9d38300` | `src/frontend/src/app/projects/[id]/page.tsx:1399` | `ocpStatus === "ready" ? "rgba(74,222,128,0.15)" : ocpStatus === "error" ? "rgba(248,113,113,0.15)" : "rgba(251,191,36,0.15)"` |
| `c573a99a-8067-4c56-ac55-7f3bdcfb29e1` | `src/frontend/src/app/projects/[id]/page.tsx:1405` | `ocpStatus === "ready" ? "#4ade80" : ocpStatus === "error" ? "#f87171" : "#fbbf24"` |
| `a153d7f5-59aa-42aa-b4a2-a40a63e4c247` | `src/frontend/src/app/projects/[id]/page.tsx:1418` | `liveSpend.budget_stopped ? "#ef4444" : liveSpend.budget_warned ? "#fbbf24" : "#86efac"` |
| `12070c54-1341-4e9f-a907-affc0d195f9b` | `src/frontend/src/app/projects/[id]/page.tsx:1419` | `liveSpend.budget_stopped ? "rgba(239,68,68,0.12)" : liveSpend.budget_warned ? "rgba(251,191,36,0.12)" : "rgba(34,197,94,0.12)"` |
| `6d04440f-2574-487f-96bd-c7a6fbafad5a` | `src/frontend/src/app/projects/[id]/page.tsx:1441` | `timerUrgency === "critical" ? "#ef4444" : timerUrgency === "warning" ? "#fbbf24" : "#94a3b8"` |
| `acaa0b7b-b8c8-4b21-af53-da3da3fa1113` | `src/frontend/src/app/projects/[id]/page.tsx:1442` | `timerUrgency === "critical" ? "rgba(239,68,68,0.12)" : timerUrgency === "warning" ? "rgba(251,191,36,0.12)" : "rgba(148,163,184,0.08)"` |
| `221fc180-2a42-4d6e-b112-bae0219c4827` | `src/frontend/src/components/canvas/nodes/ClusterNode.tsx:152` | `selected ? "var(--troshka-accent)" : hasError ? "var(--troshka-red, #ef4444)" : "var(--troshka-border, #4b5563)"` |
| `7e66eb8b-97a9-4394-b832-70769e81cfb6` | `src/frontend/src/app/admin/hosts/page.tsx:1057` | `h.state === "stopped" ? "Powering on..." : h.state === "paused" &#124;&#124; h.state === "starting" ? "Resuming..." : "Powering off..."` |
| `82ca1f79-11e2-4116-8977-04854286ae58` | `src/frontend/src/app/admin/hosts/page.tsx:755` | `ps.type === "ec2" ? "#fb923c" : ps.type === "gcp" ? "#60a5fa" : ps.type === "azure" ? "#22d3ee" : ps.type === "kubevirt" ? "#a78bfa" : ps.type === "ocpvirt" ? "#f472b6" : "#9ca3af"` |
| `84d0c777-4b65-4a91-b23d-6acc9d78bb09` | `src/frontend/src/app/admin/hosts/page.tsx:755` | `ps.type === "ec2" ? "#fb923c" : ps.type === "gcp" ? "#60a5fa" : ps.type === "azure" ? "#22d3ee" : ps.type === "kubevirt" ? "#a78bfa" : ps.type === "ocpvirt" ? "#f472b6" : "#9ca3af"` |
| `9500abb9-41f5-4923-9127-29e81ed79176` | `src/frontend/src/app/admin/hosts/page.tsx:755` | `ps.type === "ec2" ? "#fb923c" : ps.type === "gcp" ? "#60a5fa" : ps.type === "azure" ? "#22d3ee" : ps.type === "kubevirt" ? "#a78bfa" : ps.type === "ocpvirt" ? "#f472b6" : "#9ca3af"` |
| `cf6eba03-a880-46cb-9a8e-d89cc979c95b` | `src/frontend/src/app/admin/hosts/page.tsx:755` | `ps.type === "ec2" ? "#fb923c" : ps.type === "gcp" ? "#60a5fa" : ps.type === "azure" ? "#22d3ee" : ps.type === "kubevirt" ? "#a78bfa" : ps.type === "ocpvirt" ? "#f472b6" : "#9ca3af"` |
| `94ea756e-4b64-449a-b366-1d79ae776aa5` | `src/frontend/src/components/canvas/ClusterInstallLogModal.tsx:913` | `restarting ? "Restarting install…" : loading ? "Loading install log…" : "No install log yet."` |
| `f34b65b1-0e33-48d2-9d67-f01116000dce` | `src/frontend/src/components/canvas/nodes/ContainerNode.tsx:84` | `isShowroom ? "📖" : isPod ? "🫛" : "📦"` |
| `43e6d55c-b3f4-4162-b40e-12088fd81dee` | `src/frontend/src/app/layout.tsx:541` | `updateBusy ? "Update in progress…" : updateStatus?.mode === "dev" ? "Restart backend" : "Apply update"` |
| `dcd3d151-681d-4ab4-864f-9a736fd9d4ca` | `src/frontend/src/app/layout.tsx:402` | `user.role === "admin" ? "rgba(108,99,255,0.2)" : user.role === "operator" ? "rgba(251,191,36,0.2)" : "rgba(148,163,184,0.2)"` |
| `6f6a658a-3bd8-456e-a0ee-75dda3d8b24a` | `src/frontend/src/app/layout.tsx:403` | `user.role === "admin" ? "#a78bfa" : user.role === "operator" ? "#fbbf24" : "#94a3b8"` |
