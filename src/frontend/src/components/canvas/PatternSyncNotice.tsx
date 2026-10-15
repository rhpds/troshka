import { Label } from "@patternfly/react-core";

export interface PatternCentralSync {
  state: string;
  error?: string | null;
  synced_disks?: number;
  total_disks?: number;
}

export function PatternAvailability({ sync }: { sync?: PatternCentralSync | null }) {
  if (sync?.state === "error") return <Label color="red">central copy failed</Label>;
  if (sync && sync.state !== "synced") return <Label color="orange">copying to central…</Label>;
  return <Label color="green">ready</Label>;
}

export default function PatternSyncNotice({ sync }: { sync?: PatternCentralSync | null }) {
  if (sync?.state !== "error") return null;
  return (
    <div role="alert" style={{ marginBottom: 12, color: "var(--pf-t--global--text--color--status--danger--default)" }}>
      Captured on the source cluster, but the central copy failed. Deployments on other clusters may be unavailable.
      {sync.error && <div style={{ fontSize: 12, marginTop: 4 }}>{sync.error}</div>}
    </div>
  );
}
