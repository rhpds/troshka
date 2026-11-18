import { formatApiDetail } from "./apiError";

/** A rejected delete must not remove local project state or navigate away. */
export async function requestProjectDeletion(url: string): Promise<string | undefined> {
  const response = await fetch(url, { method: "DELETE" });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(formatApiDetail(data?.detail, `Delete failed (HTTP ${response.status})`));
  }
  return data?.status;
}
