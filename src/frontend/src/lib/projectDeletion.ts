import { formatApiDetail } from "./apiError";

/** Backend keeps the Project row until namespace absence; treat both as in-flight delete. */
export function isAsyncProjectDeletion(status?: string | null): boolean {
  return status === "deleting" || status === "cleanup_pending";
}

/** A rejected delete must not remove local project state or navigate away. */
export async function requestProjectDeletion(url: string): Promise<string | undefined> {
  const response = await fetch(url, { method: "DELETE" });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(formatApiDetail(data?.detail, `Delete failed (HTTP ${response.status})`));
  }
  return data?.status;
}
