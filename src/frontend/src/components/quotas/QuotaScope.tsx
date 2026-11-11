"use client";
import { useEffect, useRef, useState } from "react";
import { Alert, Button } from "@patternfly/react-core";
import { fetchUsage, isQuotaRevocation, QuotaUsageData, Scope } from "@/lib/quotas";
import QuotaUsage from "./QuotaUsage";
import QuotaEditor from "./QuotaEditor";
import styles from "./quotas.module.css";
import ModalBackdrop from "@/components/ModalBackdrop";

/** Mount with a scope/id key so navigation cannot retain another scope's draft. */
export default function QuotaScope({ scope, id, onDenied, onUsage, dialog }: { scope: Scope; id: string; onDenied?: () => void; onUsage?: (usage: QuotaUsageData | null) => void; dialog?: { isOpen: boolean; onClose: () => void } }) {
  const [usage, setUsage] = useState<QuotaUsageData | null>(null), [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const callbacks = useRef({ onDenied, onUsage });
  const revoked = useRef(false);
  useEffect(() => { callbacks.current = { onDenied, onUsage }; }, [onDenied, onUsage]);
  useEffect(() => {
    let live = true;
    let request = 0;
    async function load() {
      if (revoked.current) return;
      const current = ++request;
      try { const result = await fetchUsage(scope, id); if (live && current === request && !revoked.current) { setUsage(result); setError(""); callbacks.current.onUsage?.(result); } }
      catch (e) { if (live && current === request) { setUsage(null); callbacks.current.onUsage?.(null); setError((e as Error).message); if (isQuotaRevocation(e)) { revoked.current = true; callbacks.current.onDenied?.(); } } }
    }
    void load(); const timer = setInterval(() => void load(), 15000);
    return () => { live = false; clearInterval(timer); };
  }, [scope, id, revision]);
  // Keep permission polling active even when project quota details are closed.
  if (dialog && !dialog.isOpen) return null;
  const content = error ? <Alert isInline variant="warning" title={`Quota usage unavailable: ${error}`} />
    : !usage ? <p>Loading quota usage…</p>
    : <div className={styles.scope}><QuotaUsage usage={usage} /><details className={styles.details}><summary>Quota limits</summary><QuotaEditor key={`${scope}:${id}:${usage.policy_revision}`} usage={usage} onSaved={() => setRevision(v => v + 1)} onDenied={() => { revoked.current = true; setUsage(null); callbacks.current.onUsage?.(null); setError("Quota access revoked"); callbacks.current.onDenied?.(); }} /></details><Button variant="link" isInline onClick={() => setRevision(v => v + 1)}>Refresh quotas</Button></div>;
  if (!dialog) return content;
  return <ModalBackdrop label="Project quotas" className="start-order-overlay" onClose={dialog.onClose}>
    <div className={`start-order-modal ${styles.modal}`}>
      <div className="start-order-header"><h2>Project quotas</h2><button type="button" aria-label="Close project quotas" onClick={dialog.onClose}>✕</button></div>
      <div className={`start-order-body ${styles.modalBody}`}>{content}</div>
      <div className="start-order-footer"><Button variant="secondary" onClick={dialog.onClose}>Done</Button></div>
    </div>
  </ModalBackdrop>;
}
