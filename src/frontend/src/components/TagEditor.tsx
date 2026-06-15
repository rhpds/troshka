"use client";

import React, { useState } from "react";

interface TagEditorProps {
  tags: string[];
  onAdd: (tag: string) => void;
  onRemove: (tag: string) => void;
}

export default function TagEditor({ tags, onAdd, onRemove }: TagEditorProps) {
  const [adding, setAdding] = useState(false);
  const [value, setValue] = useState("");

  const handleAdd = () => {
    const trimmed = value.trim().toLowerCase();
    if (trimmed && !tags.includes(trimmed)) {
      onAdd(trimmed);
    }
    setValue("");
    setAdding(false);
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
      {tags.map((tag) => (
        <span
          key={tag}
          style={{
            fontSize: 11, padding: "1px 6px", borderRadius: 4,
            background: "rgba(99,140,255,0.15)", color: "#8bb4ff",
            display: "inline-flex", alignItems: "center", gap: 4,
          }}
        >
          {tag}
          <span
            onClick={(e) => { e.stopPropagation(); onRemove(tag); }}
            style={{ cursor: "pointer", opacity: 0.6, fontSize: 10, lineHeight: 1 }}
            title="Remove tag"
          >x</span>
        </span>
      ))}
      {adding ? (
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onBlur={handleAdd}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleAdd();
            if (e.key === "Escape") { setValue(""); setAdding(false); }
          }}
          onClick={(e) => e.stopPropagation()}
          placeholder="tag"
          style={{
            fontSize: 11, padding: "1px 6px", borderRadius: 4, width: 80,
            border: "1px solid var(--pf-t--global--border--color--default)",
            background: "var(--pf-t--global--background--color--primary--default)",
            color: "var(--pf-t--global--text--color--regular)",
          }}
        />
      ) : (
        <span
          onClick={(e) => { e.stopPropagation(); setAdding(true); }}
          style={{
            fontSize: 11, padding: "1px 6px", borderRadius: 4, cursor: "pointer",
            border: "1px dashed var(--pf-t--global--border--color--default)",
            color: "var(--pf-t--global--text--color--subtle)", opacity: 0.6,
          }}
          title="Add tag"
        >+ tag</span>
      )}
    </div>
  );
}
