"use client";

import { useEffect, useId, useState } from "react";
import { Person } from "@/lib/group-billing";

export default function UserPicker({ users, value, onChange, label, disabled }: Readonly<{
  users: Person[];
  value: string;
  onChange: (person: Person | null) => void;
  label: string;
  disabled?: boolean;
}>) {
  const id = useId();
  const [query, setQuery] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const needle = (query || "").trim().toLowerCase();
  const matches = users.filter(person => `${person.display_name || ""} ${person.email}`.toLowerCase().includes(needle));
  const selected = users.find(person => person.id === value);
  const expanded = open && !disabled;
  const activeIndex = Math.min(active, Math.max(0, matches.length - 1));
  useEffect(() => {
    if (expanded) document.getElementById(`${id}-${activeIndex}`)?.scrollIntoView?.({ block: "nearest" });
  }, [expanded, id, activeIndex]);
  function choose(person: Person) {
    onChange(person);
    setQuery(null);
    setOpen(false);
  }
  return <div style={{ display: "inline-block", position: "relative", minWidth: 300, maxWidth: "100%" }}>
    <label htmlFor={id}>{label}</label>
    <input id={id} role="combobox" aria-autocomplete="list" aria-expanded={expanded}
      aria-controls={`${id}-list`} aria-activedescendant={expanded && matches.length ? `${id}-${activeIndex}` : undefined}
      autoComplete="off" placeholder="Type a name or email…" disabled={disabled}
      style={{ width: "100%" }} value={query ?? selected?.email ?? ""}
      onFocus={() => { setOpen(true); setActive(0); }}
      onBlur={() => setOpen(false)}
      onChange={event => { setQuery(event.target.value); onChange(null); setActive(0); setOpen(true); }}
      onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); setOpen(false); }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          setOpen(true);
          setActive(open ? Math.max(0, Math.min(matches.length - 1, activeIndex + (event.key === "ArrowDown" ? 1 : -1))) : 0);
        }
        if (event.key === "Enter" && expanded) {
          event.preventDefault();
          if (matches[activeIndex]) choose(matches[activeIndex]);
        }
      }}
    />
    {expanded && <div id={`${id}-list`} role="listbox" aria-label={`${label} results`} style={{
      position: "absolute", top: "100%", left: 0, right: 0, zIndex: 100,
      maxHeight: 240, overflowY: "auto", border: "1px solid var(--pf-t--global--border--color--default)",
      background: "var(--pf-t--global--background--color--primary--default)",
    }}>
      {!matches.length && <div role="status" style={{ padding: 8 }}>No matching users</div>}
      {matches.map((person, index) => <div key={person.id} id={`${id}-${index}`} role="option"
        aria-selected={person.id === value} onMouseDown={event => event.preventDefault()}
        onMouseEnter={() => setActive(index)} onClick={() => choose(person)}
        style={{ padding: 8, cursor: "pointer", background: index === activeIndex ? "var(--pf-t--global--background--color--secondary--default)" : undefined }}>
        {person.display_name ? `${person.display_name} — ${person.email}` : person.email}
      </div>)}
    </div>}
  </div>;
}
