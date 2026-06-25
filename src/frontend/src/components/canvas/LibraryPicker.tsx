"use client";

import React, { useEffect, useState } from "react";

interface LibraryItem {
  id: string;
  name: string;
  type: string;
  format: string;
  size_bytes: number;
  os_variant: string;
  state: string;
  created_at: string;
}

interface LibraryPickerProps {
  type: "iso" | "image";
  onSelect: (item: { id: string; name: string; size_gb: number; format: string }) => void;
  onClose: () => void;
}

export default function LibraryPicker({ type, onSelect, onClose }: LibraryPickerProps) {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const params = new URLSearchParams();
    if (type === "iso") {
      params.set("type", "iso");
    }
    if (search) params.set("q", search);
    fetch(`/api/v1/library/?${params.toString()}`)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => {
        let filtered = data.filter((i: LibraryItem) => i.state === "ready");
        if (type === "image") {
          filtered = filtered.filter((i: LibraryItem) => i.format !== "iso");
        }
        setItems(filtered);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [type, search]);

  const formatSize = (bytes: number) => {
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(0)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  };

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 2000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.6)",
    }} onClick={onClose}>
      <div style={{
        background: "var(--pf-t--global--background--color--primary--default)",
        border: "1px solid var(--pf-t--global--border--color--default)",
        borderRadius: 12, width: 500, maxHeight: "70vh",
        display: "flex", flexDirection: "column",
        boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
      }} onClick={(e) => e.stopPropagation()}>
        <div style={{ padding: "16px 20px", borderBottom: "1px solid var(--pf-t--global--border--color--default)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontWeight: 600, fontSize: 15 }}>
            Select {type === "iso" ? "ISO" : "Disk Image"}
          </span>
          <button onClick={onClose} style={{ background: "none", border: "none", color: "var(--pf-t--global--text--color--regular)", fontSize: 18, cursor: "pointer" }}>✕</button>
        </div>

        <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
          <input
            placeholder="Search..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{
              width: "100%", padding: "6px 10px", borderRadius: 6,
              border: "1px solid var(--pf-t--global--border--color--default)",
              background: "var(--pf-t--global--background--color--secondary--default)",
              color: "var(--pf-t--global--text--color--regular)", fontSize: 13,
            }}
          />
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: "8px 12px" }}>
          {loading && <p style={{ textAlign: "center", opacity: 0.5, padding: 20 }}>Loading...</p>}
          {!loading && items.length === 0 && (
            <p style={{ textAlign: "center", opacity: 0.5, padding: 20 }}>
              No {type === "iso" ? "ISOs" : "disk images"} found. Upload one from the Library page.
            </p>
          )}
          {items.map((item) => (
            <div
              key={item.id}
              style={{
                padding: "10px 12px", borderRadius: 8, cursor: "pointer",
                display: "flex", justifyContent: "space-between", alignItems: "center",
                marginBottom: 4,
              }}
              className="library-picker-item"
              onClick={() => {
                onSelect({
                  id: item.id,
                  name: item.name,
                  size_gb: Math.ceil(item.size_bytes / (1024 * 1024 * 1024)) || 1,
                  format: item.format,
                });
                onClose();
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.05)")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
            >
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span>{item.format === "iso" ? "💿" : "🛢"}</span>
                  <strong style={{ fontSize: 13 }}>{item.name}</strong>
                </div>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 2, marginLeft: 26 }}>
                  {formatSize(item.size_bytes)}
                </div>
              </div>
              <span style={{ fontSize: 11, color: "var(--pf-t--global--color--brand--default)" }}>Select</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
