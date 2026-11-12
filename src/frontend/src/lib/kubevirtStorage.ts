import { giB, integerText, Quantity, quotaFetch, Scope } from "./quotas";

/** Independent observation; never a policy limit, reservation, or billing input. */
export interface NativeStorageUsageData {
  scope: Scope; scope_id: string; metric: "rbd_allocated_extent_bytes";
  status: "complete" | "partial" | "stale" | "unavailable";
  allocated_bytes: Quantity | null; known_allocated_bytes: Quantity;
  provisioned_bytes: Quantity | null; observed_at: string | null;
  covered_projects: number; expected_projects: number; reason_codes: string[];
  enforcement_eligible: false; platform_bytes?: Quantity | null;
  unattributed_retained_projects?: number;
  unattributed_retained_known_bytes?: Quantity | null;
}

/** Round exact decimal GiB to hundredths without converting bytes to Number. */
export function roundedGiB(value: Quantity): string {
  const [whole, fraction = ""] = giB(value).split(".");
  const digits = fraction.padEnd(3, "0");
  const hundredths = BigInt(whole) * BigInt(100) + BigInt(digits.slice(0, 2)) + (digits[2] >= "5" ? BigInt(1) : BigInt(0));
  if (hundredths === BigInt(0) && BigInt(integerText(value)) > BigInt(0)) return "<0.01";
  const decimal = String(hundredths % BigInt(100)).padStart(2, "0").replace(/0+$/, "");
  return `${hundredths / BigInt(100)}${decimal ? `.${decimal}` : ""}`;
}

export async function fetchNativeStorage(scope: Scope, id: string): Promise<NativeStorageUsageData> {
  const data = await quotaFetch<NativeStorageUsageData>(`/quotas/${scope}/${encodeURIComponent(id)}/native-storage`);
  if (data.metric !== "rbd_allocated_extent_bytes" || data.enforcement_eligible !== false || !["complete", "partial", "stale", "unavailable"].includes(data.status)) throw new Error("Invalid native storage response");
  integerText(data.known_allocated_bytes);
  for (const amount of [data.allocated_bytes, data.provisioned_bytes, data.platform_bytes, data.unattributed_retained_known_bytes]) {
    if (amount != null) integerText(amount);
  }
  return data;
}
