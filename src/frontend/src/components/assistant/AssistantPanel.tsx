"use client";
import { Alert, Button, Modal, ModalBody, ModalHeader, Spinner, TextArea } from "@patternfly/react-core";
import { useEffect, useRef, useState } from "react";
import { assistantRequest, sendAssistantMessage, type ChatMessage, type Conversation, type Evidence, type Grant } from "@/lib/assistant-client";
import ConsentControls from "./ConsentControls";
import EvidenceList from "./EvidenceList";
import AssistantMessage from "./AssistantMessage";

export function AssistantLauncher({ projectId }: { projectId: string }) {
  const [enabled, setEnabled] = useState(false);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let live = true;
    fetch("/api/v1/assistant/capabilities").then(r => r.ok ? r.json() : { enabled: false }).then((data: { enabled?: boolean }) => { if (live) setEnabled(data.enabled === true); }).catch(() => {});
    return () => { live = false; };
  }, []);
  if (!enabled) return null;
  return <>
    <Button variant="secondary" onClick={() => setOpen(true)}>Troubleshoot this project</Button>
    {open && <AssistantPanel key={projectId} projectId={projectId} onClose={() => setOpen(false)} />}
  </>;
}

export default function AssistantPanel({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const [id, setId] = useState<string | null>(null);
  const [grant, setGrant] = useState<Grant | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const created = useRef<Promise<{ id: string }> | null>(null);
  useEffect(() => {
    let live = true;
    created.current ??= assistantRequest<{ id: string }>("", "POST", { project_id: projectId });
    created.current.then(data => { if (live) setId(data.id); }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; controller.current?.abort(); };
  }, [projectId]);

  async function refresh() {
    if (!id) return;
    const data = await assistantRequest<Conversation>(`/${id}`);
    setGrant(data.grant); setMessages(data.messages); setEvidence(data.evidence);
  }

  async function action(operation: () => Promise<void>) {
    setError("");
    try { await operation(); } catch (e) { setError(e instanceof Error ? e.message : "Assistant unavailable"); }
  }

  async function submit() {
    if (!id || !question.trim() || busy) return;
    const text = question;
    const requestId = crypto.randomUUID();
    const abort = new AbortController(); controller.current = abort;
    setBusy(true); setStatus("Reviewing your question"); setQuestion(""); setError("");
    setMessages(previous => [...previous, { role: "user", text, status: "running", request_id: requestId }]);
    try {
      await sendAssistantMessage(id, text, requestId, abort.signal, event => {
        if (event.type === "status") setStatus(String(event.payload.message));
        if (event.type === "check_started") setStatus(`Checking ${String(event.payload.check).replaceAll("_", " ")}`);
        if (event.type === "check_finished") setEvidence(previous => [...previous, event.payload as unknown as Evidence]);
        if (event.type === "answer_delta") setMessages(previous => [...previous, { role: "assistant", text: String(event.payload.text), status: "done", request_id: requestId }]);
        if (event.type === "error") setError(String(event.payload.message));
      });
    } catch (e) {
      if (!abort.signal.aborted) setError(e instanceof Error ? e.message : "Connection interrupted");
    } finally { setBusy(false); setStatus(""); controller.current = null; }
  }

  return <Modal isOpen variant="medium" onClose={onClose} aria-labelledby="assistant-title">
    <ModalHeader title="Troubleshoot this project" labelId="assistant-title" />
    <ModalBody>
      <p>Project {projectId}. Diagnoses use saved configuration; unsaved canvas edits are excluded. Do not paste passwords, tokens or private keys.</p>
      {error && <Alert variant="warning" isInline title={error} />}
      <ConsentControls grant={grant} busy={busy || !id} onGrant={guest => void action(async () => {
        setGrant(await assistantRequest<Grant>(`/${id}/grant`, "POST", { guest_checks: guest }));
      })} onRevoke={() => void action(async () => {
        controller.current?.abort();
        try { await assistantRequest(`/${id}/grant`, "DELETE"); } finally { setGrant(null); }
      })} />
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBlock: 16 }}>
        {["Why is SSH unreachable?", "Why did installation fail?", "Why is cleanup pending?"].map(text => <Button key={text} variant="link" onClick={() => setQuestion(text)}>{text}</Button>)}
      </div>
      <section aria-label="Conversation" aria-live="polite">
        {messages.map((m, i) => <div key={`${m.request_id}-${m.role}-${i}`} style={{ marginBlock: 12 }}>
          <strong>{m.role === "user" ? "You" : "Troshka assistant"}</strong>
          {m.role === "assistant" ? <AssistantMessage text={m.text} /> :
            <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{m.text}</p>}
        </div>)}
      </section>
      <EvidenceList evidence={evidence} />
      <div role="status" aria-live="polite" aria-atomic="true" style={{ display: "flex", alignItems: "center", gap: 12, marginBlock: 16 }}>
        {busy && <span aria-hidden="true" style={{ display: "inline-flex" }}><Spinner size="md" /></span>}
        <span>{status}</span>
      </div>
      <TextArea aria-label="Question" value={question} onChange={(_, value) => setQuestion(value)} maxLength={2000} isDisabled={busy} />
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBlock: 16 }}>
        <Button onClick={() => void submit()} isDisabled={!id || busy || !question.trim()}>Ask</Button>
        {busy && <Button variant="secondary" onClick={() => void action(async () => { controller.current?.abort(); await assistantRequest(`/${id}/active-run`, "DELETE"); })}>Cancel</Button>}
        <Button variant="link" isDisabled={!id || busy} onClick={() => void action(refresh)}>Refresh history</Button>
        <Button variant="link" isDisabled={!messages.length || busy} onClick={() => void action(async () => {
          await navigator.clipboard.writeText(messages.filter(m => m.role === "assistant").map(m => m.text).join("\n\n") + "\n\n" + JSON.stringify(evidence, null, 2));
          setStatus("Support draft copied. Review it before sharing.");
        })}>Copy support draft</Button>
        <Button variant="link" isDanger isDisabled={!id || busy} onClick={() => void action(async () => { await assistantRequest(`/${id}`, "DELETE"); onClose(); })}>Delete conversation</Button>
      </div>
      <p>Use existing project controls to make changes. This assistant cannot repair hosts, stop VMs or delete resources.</p>
      <p><a href="/settings">Open Settings</a> · <a href={`/projects/${projectId}`}>Open saved project</a></p>
    </ModalBody>
  </Modal>;
}
