"use client";

import ModalBackdrop from "@/components/ModalBackdrop";
import { parseChangelog, type ChangelogEntry } from "@/lib/changelog";
import { useEffect, useState } from "react";

export default function AboutModal({ onClose }: Readonly<{ onClose: () => void }>) {
  const version = process.env.NEXT_PUBLIC_TROSHKA_VERSION || "Development build";
  const [entries, setEntries] = useState<ChangelogEntry[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  useEffect(() => {
    const controller = new AbortController();
    async function loadChangelog() {
      try {
        const response = await fetch("/api/changelog", { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error("Changelog request failed");
        const markdown = await response.text();
        if (controller.signal.aborted) return;
        setEntries(parseChangelog(markdown));
        setStatus("ready");
      } catch {
        if (!controller.signal.aborted) setStatus("error");
      }
    }
    void loadChangelog();
    return () => controller.abort();
  }, []);
  const isRelease = /^v\d[\w.-]*$/.test(version) && !/(-dirty|-\d+-g[0-9a-f]+)$/.test(version);

  return (
    <ModalBackdrop label="About Troshka" onClose={onClose} style={{ display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0, 0, 0, 0.6)" }}>
      <section className="about-modal">
        <header className="about-modal-header">
          <h1>About Troshka</h1>
          <button type="button" className="pf-v6-c-button pf-m-plain" onClick={onClose} aria-label="Close About">×</button>
        </header>
        <p>Version: {isRelease ? (
          <a href={`https://github.com/rhpds/troshka/releases/tag/${encodeURIComponent(version)}`} target="_blank" rel="noreferrer">{version}</a>
        ) : version}</p>
        <div>
          <p>Patrick T. Rutledge III &lt;<a href="mailto:prutledg@redhat.com">prutledg@redhat.com</a>&gt;</p>
          <p>Red Hat Demo Platform Team</p>
        </div>
        <h2>Changelog</h2>
        {status === "loading" && <p role="status">Loading release notes…</p>}
        {status === "error" && <p role="alert">Release notes could not be loaded. Close and reopen About to try again.</p>}
        {status === "ready" && entries.length === 0 && <p>No release notes are available for this build.</p>}
        {entries.map((entry) => (
          <details key={entry.title} className="about-release">
            <summary>{entry.title}</summary>
            <ul>{entry.changes.map((change) => <li key={change}>{change}</li>)}</ul>
          </details>
        ))}
      </section>
    </ModalBackdrop>
  );
}
