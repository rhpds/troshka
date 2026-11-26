export type Evidence = {
  evidence_id: string; check: string; status: string; observed_at: string;
  source: string; facts: Record<string, unknown>; limitations: string[]; truncated: boolean;
};
export type Grant = { id: string; expires_at: string; guest_checks: boolean };
export type ChatMessage = { role: string; text: string; status: string; request_id: string };
export type Conversation = { id: string; grant: Grant | null; messages: ChatMessage[]; evidence: Evidence[] };
export type AssistantEvent = { sequence: number; request_id: string; type: string; payload: Record<string, unknown> };

const base = "/api/v1/assistant/conversations";

export async function assistantRequest<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    method, credentials: "same-origin", headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : "Assistant request failed");
  }
  return response.status === 204 ? undefined as T : response.json();
}

export async function readEvents(stream: ReadableStream<Uint8Array>, onEvent: (event: AssistantEvent) => void) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      if (buffer.length > 262144) throw new Error("Assistant event exceeded size limit");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = frame.split("\n").filter(line => line.startsWith("data: ")).map(line => line.slice(6)).join("\n");
        if (data) onEvent(JSON.parse(data));
      }
      if (done) {
        if (buffer.trim()) throw new Error("Assistant stream ended unexpectedly");
        break;
      }
    }
  } finally { reader.releaseLock(); }
}

export async function sendAssistantMessage(id: string, text: string, requestId: string, signal: AbortSignal, onEvent: (event: AssistantEvent) => void) {
  const response = await fetch(`${base}/${id}/messages`, {
    method: "POST", credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, request_id: requestId }),
  });
  if (!response.ok || !response.body) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : "Assistant unavailable");
  }
  let terminal = false;
  await readEvents(response.body, event => {
    if (event.type === "done" || event.type === "error") terminal = true;
    onEvent(event);
  });
  if (!terminal) throw new Error("Assistant connection interrupted; refresh history before retrying");
}
