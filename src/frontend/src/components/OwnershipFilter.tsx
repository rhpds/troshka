"use client";

import React from "react";

export type OwnershipFilters = {
  mine: boolean;
  shared: boolean;
  others: boolean;
};

export const DEFAULT_OWNERSHIP_FILTERS: OwnershipFilters = {
  mine: true,
  shared: true,
  others: false,
};

export type OwnershipCategory = "mine" | "shared" | "others";

/** Return true if an item in the given ownership category should be shown. */
export function matchesOwnershipFilter(
  category: OwnershipCategory,
  filters: OwnershipFilters,
  isAdmin: boolean,
): boolean {
  if (category === "mine") return filters.mine;
  if (category === "shared") return filters.shared;
  return isAdmin && filters.others;
}

const labelStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  fontSize: 13,
  cursor: "pointer",
  whiteSpace: "nowrap",
};

type Props = {
  value: OwnershipFilters;
  onChange: (next: OwnershipFilters) => void;
  isAdmin: boolean;
  /** Projects have no shared category yet. */
  showShared?: boolean;
};

export default function OwnershipFilter({
  value,
  onChange,
  isAdmin,
  showShared = true,
}: Props) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
      <label style={labelStyle}>
        <input
          type="checkbox"
          checked={value.mine}
          onChange={(e) => onChange({ ...value, mine: e.target.checked })}
        />
        Mine
      </label>
      {showShared && (
        <label style={labelStyle}>
          <input
            type="checkbox"
            checked={value.shared}
            onChange={(e) => onChange({ ...value, shared: e.target.checked })}
          />
          Shared
        </label>
      )}
      {isAdmin && (
        <label style={labelStyle}>
          <input
            type="checkbox"
            checked={value.others}
            onChange={(e) => onChange({ ...value, others: e.target.checked })}
          />
          Other users
        </label>
      )}
    </div>
  );
}
