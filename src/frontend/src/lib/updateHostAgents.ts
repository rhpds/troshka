interface AgentHost {
  id: string;
  agent_status: string;
  agent_version?: string;
}

// The POST only queues the update. Wait for each host to report its target
// version before letting the caller clear its loading state.
export async function updateHostAgents(hostIds: string[]): Promise<string[]> {
  const results = await Promise.allSettled(hostIds.map(async (id) => {
    const response = await fetch(`/api/v1/hosts/${id}/update-agent`, {
      method: "POST",
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error("Update request failed");
    const data = await response.json();
    if (!data.version) throw new Error("Missing target agent version");
    return { id, version: data.version as string };
  }));
  const failedIds = hostIds.filter((_, index) => results[index].status === "rejected");
  const pending = new Map<string, string>();
  for (const result of results) {
    if (result.status === "fulfilled") pending.set(result.value.id, result.value.version);
  }

  const deadline = Date.now() + 180000;
  while (pending.size > 0 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    try {
      const response = await fetch("/api/v1/hosts/", { signal: AbortSignal.timeout(5000) });
      if (!response.ok) continue;
      const hosts: AgentHost[] = await response.json();
      for (const host of hosts) {
        if (host.agent_status === "connected" && host.agent_version === pending.get(host.id)) {
          pending.delete(host.id);
        }
      }
    } catch {
      // A temporary outage during restart is not proof of completion.
    }
  }
  if (pending.size > 0) {
    throw new Error("Host agent update timed out — check Admin → Hosts for status.");
  }
  return failedIds;
}
