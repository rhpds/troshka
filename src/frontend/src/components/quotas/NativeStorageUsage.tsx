"use client";
import { useEffect, useRef, useState } from "react";
import { fetchNativeStorage, NativeStorageUsageData, roundedGiB } from "@/lib/kubevirtStorage";
import { integerText, isQuotaRevocation, Quantity, Scope } from "@/lib/quotas";
import styles from "./quotas.module.css";

interface Props { scope: Scope; id: string; onDenied?: () => void; isRevoked?: () => boolean }

/** Key the inner view so changing scope clears amounts before the next paint. */
export default function NativeStorageUsage(props: Props) {
  return <NativeStorageView key={`${props.scope}:${props.id}`} {...props} />;
}

function NativeStorageView({ scope, id, onDenied, isRevoked }: Props) {
  const [data, setData] = useState<NativeStorageUsageData | null>(null);
  const [error, setError] = useState("");
  const callbacks = useRef({ onDenied, isRevoked });
  useEffect(() => { callbacks.current = { onDenied, isRevoked }; }, [onDenied, isRevoked]);
  useEffect(() => {
    let live = true, request = 0, denied = false;
    const blocked = () => denied || callbacks.current.isRevoked?.();
    async function load() {
      if (blocked()) return;
      const current = ++request;
      try {
        const result = await fetchNativeStorage(scope, id);
        if (live && current === request && !blocked()) { setData(result); setError(""); }
      } catch (error) {
        if (!live || blocked()) return;
        // A denial always invalidates the view, even from an older poll.
        if (isQuotaRevocation(error)) {
          denied = true; setData(null); setError("Native storage access revoked"); callbacks.current.onDenied?.();
        } else if (current === request) { setData(null); setError("Native storage report unavailable"); }
      }
    }
    void load();
    const timer = setInterval(() => void load(), 15000);
    return () => { live = false; clearInterval(timer); };
  }, [scope, id]);
  return <section className={styles.nativeStorage} aria-label="Native storage reporting">
    <div className={styles.heading}><h3>Allocated RBD storage</h3>{data && <span className={styles.badge}>{data.status}</span>}</div>
    {error ? <p role="status">{error}</p> : data ? <NativeAmounts data={data} /> : <p role="status">Loading native storage report…</p>}
    <p className={styles.muted}>Reporting only; native quota enforcement unavailable. Billing uses provisioned capacity.</p>
  </section>;
}

function Bytes({ value }: { value: Quantity }) {
  return <span title={`${integerText(value)} bytes`} aria-label={`${integerText(value)} bytes`}>{roundedGiB(value)} GiB</span>;
}

function hasVerifiedMeasurement(data: NativeStorageUsageData): boolean {
  if (data.status === "unavailable") return false;
  if (data.status === "complete" && data.allocated_bytes !== null) return true;
  if (BigInt(integerText(data.known_allocated_bytes)) > BigInt(0) || data.covered_projects > 0) return true;
  // The API preserves every project completeness blocker, while covered_projects
  // counts only fresh complete projects. Age alone can therefore hide a measured
  // zero; an arbitrary scan timestamp or incomplete zero cannot establish one.
  return data.status === "stale" && data.expected_projects > 0 && data.reason_codes.length === 1 && data.reason_codes[0] === "stale_evidence";
}

function NativeAmounts({ data }: { data: NativeStorageUsageData }) {
  if (data.expected_projects === 0) return <p className={styles.muted}>No native projects in this scope</p>;
  const complete = data.status === "complete" && data.allocated_bytes !== null;
  const measured = hasVerifiedMeasurement(data);
  const labels = { complete: "Scope total", partial: "Measured subset", stale: "Last measured", unavailable: "No verified measurement" };
  return <>
    <div className={styles.nativeSummary}>
      <div>{!complete && <p className={styles.muted}>Scope total unavailable</p>}
        <p className={styles.muted}>{measured ? labels[data.status] : "No verified measurement"}</p>
        <p className={styles.nativeAmount}>{measured ? <Bytes value={complete ? data.allocated_bytes! : data.known_allocated_bytes} /> : "Unavailable"}</p>
      </div>
      <div className={styles.muted}>
        <p>Provisioned PVC capacity: {data.provisioned_bytes === null ? "Unavailable" : <Bytes value={data.provisioned_bytes} />}</p>
        <p>Native project coverage: {data.covered_projects} of {data.expected_projects}</p>
        <p>{data.observed_at ? <>Source observed <time dateTime={data.observed_at}>{new Date(data.observed_at).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC")}</time></> : "Source observation time unavailable"}</p>
      </div>
    </div>
    <CoverageDetails data={data} />
  </>;
}

function CoverageDetails({ data }: { data: NativeStorageUsageData }) {
  return <details className={styles.nativeDetails}><summary>Coverage details</summary>
      <p>Allocated image and retained snapshot extents, excluding replication and raw device consumption. Shared platform cache is excluded from scope totals. Missing evidence is never counted as zero.</p>
      {data.reason_codes.length > 0 && <p>Coverage reasons: {data.reason_codes.map(reason => reason.replaceAll("_", " ")).join(", ")}</p>}
      {data.platform_bytes !== undefined && <p>Admin diagnostic — platform allocation across selected collection domains: {data.platform_bytes === null ? "Unavailable" : <Bytes value={data.platform_bytes} />}. This is not attributed to this scope.</p>}
      {data.unattributed_retained_projects !== undefined && <p>Admin diagnostic — unresolved retained projects: {data.unattributed_retained_projects}. Known retained allocation: {data.unattributed_retained_known_bytes == null ? "Unavailable" : <Bytes value={data.unattributed_retained_known_bytes} />}. This is not attributed to this scope.</p>}
    </details>;
}
