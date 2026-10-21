/** Expected capture failures can be shown without exposing transport errors. */
export class CaptureError extends Error {}

const delay = (milliseconds: number) => new Promise<void>((resolve) => setTimeout(resolve, milliseconds));

export async function checkCaptureName(url: string, name: string, kind: "pattern" | "snapshot") {
  const response = await fetch(url);
  if (!response.ok) return;
  const items: unknown = await response.json();
  if (Array.isArray(items) && items.some((item) => item.name === name)) {
    throw new CaptureError(`You already have a ${kind} named "${name}"`);
  }
}

async function waitForState(url: string, states: string[], attempts: number, interval: number): Promise<boolean> {
  for (let attempt = 0; attempt < attempts; attempt++) {
    await delay(interval);
    const response = await fetch(url);
    if (!response.ok) continue;
    const data = await response.json();
    if (states.includes(data.state)) return true;
  }
  return false;
}

export async function stopForCapture(options: Readonly<{
  baseUrl: string;
  statusUrl: string;
  forceAction: string;
  stoppedStates: string[];
  forceDelay?: number;
  subject: string;
  onStatus: (status: string) => void;
}>) {
  options.onStatus("Graceful shutdown...");
  await fetch(`${options.baseUrl}/stop`, { method: "POST" });
  if (await waitForState(options.statusUrl, options.stoppedStates, 10, 3000)) return;
  options.onStatus("Force powering off...");
  await fetch(`${options.baseUrl}/${options.forceAction}`, { method: "POST" });
  if (options.forceDelay) await delay(options.forceDelay);
  if (!await waitForState(options.statusUrl, options.stoppedStates, 10, 2000)) {
    throw new CaptureError(`Timed out waiting for ${options.subject} to stop`);
  }
}

export async function waitForDiskCapture(snapshotId: string) {
  for (let attempt = 0; attempt < 120; attempt++) {
    await delay(3000);
    const response = await fetch(`/api/v1/library/${snapshotId}`);
    if (!response.ok) continue;
    const item = await response.json();
    if (item.state === "ready") return;
    if (item.state === "error") throw new CaptureError("Disk capture failed");
  }
  throw new CaptureError("Timed out waiting for disk capture");
}
