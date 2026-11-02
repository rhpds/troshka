"use client";

import { useEffect, useId, useState, type CSSProperties } from "react";

type Option = { value: string; label: string };

export default function OcpVersionSelect({ value, options, onChange, disabled, className, style, label = "OCP Version" }: Readonly<{
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
  style?: CSSProperties;
  label?: string;
}>) {
  const id = useId();
  const [query, setQuery] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const matches = options.filter((option) => option.value.toLowerCase().startsWith((query || "").trim().toLowerCase()));
  const expanded = open && !disabled;
  const choose = (option: Option) => {
    onChange(option.value);
    setQuery(null);
    setOpen(false);
  };

  useEffect(() => {
    if (expanded) document.getElementById(`${id}-${active}`)?.scrollIntoView?.({ block: "nearest" });
  }, [active, expanded, id]);

  return <div style={{ position: "relative", width: "100%" }}>
    <input
      role="combobox"
      aria-label={label}
      aria-autocomplete="list"
      aria-expanded={expanded}
      aria-controls={`${id}-list`}
      aria-activedescendant={expanded && matches.length ? `${id}-${active}` : undefined}
      autoComplete="off"
      placeholder="Type a version to filter…"
      value={query ?? value}
      disabled={disabled}
      className={className}
      style={style}
      onFocus={(event) => { setOpen(true); setActive(0); event.target.select(); }}
      onChange={(event) => { setQuery(event.target.value); setOpen(true); setActive(0); }}
      onBlur={() => { setOpen(false); setQuery(null); }}
      onKeyDown={(event) => {
        if (event.key === "Escape") { event.stopPropagation(); setOpen(false); setQuery(null); }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          setOpen(true);
          setActive(Math.max(0, Math.min(matches.length - 1, active + (event.key === "ArrowDown" ? 1 : -1))));
        }
        if (event.key === "Enter" && expanded) {
          event.preventDefault(); event.stopPropagation();
          if (matches[active]) choose(matches[active]);
        }
      }}
    />
    {expanded && <div id={`${id}-list`} role="listbox" aria-label={`${label} releases`} style={{
      position: "absolute", top: "100%", left: 0, right: 0, zIndex: 100,
      maxHeight: 240, overflowY: "auto", borderRadius: 4,
      border: "1px solid var(--pf-t--global--border--color--default)",
      background: "var(--pf-t--global--background--color--primary--default)",
      boxShadow: "0 4px 12px rgba(0,0,0,0.25)",
    }}>
      {matches.length === 0 && <div role="status" style={{ padding: 10, fontSize: 12 }}>No matching releases</div>}
      {matches.map((option, index) => <div
        key={option.value}
        id={`${id}-${index}`}
        role="option"
        aria-selected={option.value === value}
        onMouseDown={(event) => event.preventDefault()}
        onMouseEnter={() => setActive(index)}
        onClick={() => choose(option)}
        style={{ padding: "8px 10px", cursor: "pointer", fontSize: 12,
          background: index === active ? "var(--pf-t--global--background--color--secondary--default)" : undefined }}
      >{option.label}</div>)}
    </div>}
  </div>;
}
