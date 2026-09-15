"use client";

import React, { Suspense, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { ClusterInstallLogPanel } from "@/components/canvas/ClusterInstallLogModal";

function InstallLogConsolePage() {
  const searchParams = useSearchParams();
  const projectId = searchParams.get("project") || "";
  const clusterKey = searchParams.get("cluster") || "";
  const clusterName = searchParams.get("name") || clusterKey;

  useEffect(() => {
    document.title = clusterName ? `${clusterName} — Status & Log` : "Install Status";
  }, [clusterName]);

  if (!projectId || !clusterKey) {
    return (
      <div
        style={{
          color: "var(--pf-t--global--text--color--regular)",
          padding: 20,
          background: "var(--pf-t--global--background--color--primary--default)",
          height: "100vh",
        }}
      >
        Missing <code>project</code> or <code>cluster</code> query parameter.
      </div>
    );
  }

  return (
    <ClusterInstallLogPanel
      projectId={projectId}
      clusterKey={clusterKey}
      clusterName={clusterName}
      standalone
    />
  );
}

export default function InstallLogConsoleWrapper() {
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
          Loading install status…
        </div>
      }
    >
      <InstallLogConsolePage />
    </Suspense>
  );
}
