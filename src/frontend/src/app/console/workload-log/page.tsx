"use client";

import React, { Suspense, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { WorkloadRunLogPanel } from "@/components/canvas/WorkloadRunDetailModal";

function WorkloadLogConsolePage() {
  const searchParams = useSearchParams();
  const runId = searchParams.get("run") || "";
  const nameHint = searchParams.get("name") || "";

  useEffect(() => {
    document.title = nameHint ? `${nameHint} — Workload Log` : "Workload Log";
  }, [nameHint]);

  if (!runId) {
    return (
      <div
        style={{
          color: "var(--pf-t--global--text--color--regular)",
          padding: 20,
          background: "var(--pf-t--global--background--color--primary--default)",
          height: "100vh",
        }}
      >
        Missing <code>run</code> query parameter.
      </div>
    );
  }

  return <WorkloadRunLogPanel runId={runId} standalone />;
}

export default function WorkloadLogConsoleWrapper() {
  return (
    <Suspense
      fallback={
        <div
          style={{
            color: "var(--pf-t--global--text--color--regular)",
            padding: 20,
            background: "var(--pf-t--global--background--color--primary--default)",
            height: "100vh",
          }}
        >
          Loading workload log…
        </div>
      }
    >
      <WorkloadLogConsolePage />
    </Suspense>
  );
}
