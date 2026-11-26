"use client";
import { Button, Checkbox } from "@patternfly/react-core";
import { useEffect, useState } from "react";
import type { Grant } from "@/lib/assistant-client";

export default function ConsentControls({ grant, busy, onGrant, onRevoke }: {
  grant: Grant | null; busy: boolean; onGrant: (guest: boolean) => void; onRevoke: () => void;
}) {
  const [guest, setGuest] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const active = grant && new Date(grant.expires_at).getTime() > now;
  if (active) return <section aria-label="Diagnostic access">
    <p>Project diagnostics allowed{grant.guest_checks ? ", including guest checks" : ""} until {new Date(grant.expires_at).toLocaleTimeString()}.</p>
    <Button variant="secondary" onClick={onRevoke}>Revoke access</Button>
  </section>;
  return <section aria-label="Diagnostic consent">
    <p>Allow this conversation to inspect this project&apos;s configuration, runtime status, relevant logs and supporting host diagnostics. Sanitized evidence will be sent to MaaS. This does not authorize changes.</p>
    {grant && <p>Diagnostic access expired. General help is still available.</p>}
    <Checkbox id="assistant-guest-consent" label="Also allow predefined checks inside this project's VMs and nested clusters" isChecked={guest} onChange={(_, checked) => setGuest(checked)} isDisabled={busy} />
    <Button onClick={() => onGrant(guest)} isDisabled={busy}>Allow diagnostics for 30 minutes</Button>
  </section>;
}
