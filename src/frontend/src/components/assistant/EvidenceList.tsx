"use client";
import type { Evidence } from "@/lib/assistant-client";

export default function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  return <section aria-label="Diagnostic evidence">
    {evidence.map(item => <details key={item.evidence_id}>
      <summary>{item.check.replaceAll("_", " ")} — {item.status}</summary>
      <p>{item.source} · {new Date(item.observed_at).toLocaleString()}</p>
      <p>Evidence: {item.evidence_id}</p>
      {Date.now() - new Date(item.observed_at).getTime() > 60000 && <p>Observation is over one minute old; runtime state may have changed.</p>}
      <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(item.facts, null, 2)}</pre>
      {item.limitations.map((text, i) => <p key={i}>{text}</p>)}
      {item.truncated && <p>Result was limited in size.</p>}
    </details>)}
  </section>;
}
