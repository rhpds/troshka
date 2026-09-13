import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import { useVmStateSocket } from "@/hooks/useVmStateSocket";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  onopen: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: unknown) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  readyState = 1;
  url: string;
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
    setTimeout(() => this.onopen?.({}), 0);
  }
  send() {}
  close() {
    this.readyState = 3;
    this.onclose?.({});
  }
}

function Probe({ projectId }: { projectId: string }) {
  const ws = useVmStateSocket(projectId);
  return <div data-testid="wp">{ws.workloadProgress?.step ?? "none"}</div>;
}

describe("useVmStateSocket workload-progress", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
    // dev mode skips ws-token fetch; stub fetch defensively
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ token: "x" }) } as Response),
    ));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("surfaces workload-progress step", async () => {
    render(<Probe projectId="p1" />);
    await waitFor(() => expect(MockWebSocket.instances.length).toBeGreaterThan(0));
    const sock = MockWebSocket.instances[0];
    act(() => {
      sock.onmessage?.({
        data: JSON.stringify({ type: "workload-progress", progress: { step: "Install role", detail: "" } }),
      });
    });
    await waitFor(() => expect(screen.getByTestId("wp").textContent).toBe("Install role"));
  });
});
