import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import PatternSyncNotice, { PatternAvailability } from "./PatternSyncNotice";

describe("pattern replication feedback", () => {
  it("shows the copy error instead of an unconditional ready label", () => {
    const sync = { state: "error", error: "Multipart completion failed" };
    render(<><PatternAvailability sync={sync} /><PatternSyncNotice sync={sync} /></>);
    expect(screen.queryByText("ready")).toBeNull();
    expect(screen.getByText("central copy failed")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("Multipart completion failed");
    expect(screen.getByRole("alert").textContent).toContain("source cluster");
  });

  it("shows copy in progress until all disks are synced", () => {
    render(<PatternAvailability sync={{ state: "syncing" }} />);
    expect(screen.getByText("copying to central…")).toBeTruthy();
    expect(screen.queryByText("ready")).toBeNull();
  });

  it("shows ready after the central copy succeeds", () => {
    render(<PatternAvailability sync={{ state: "synced" }} />);
    expect(screen.getByText("ready")).toBeTruthy();
  });
});
