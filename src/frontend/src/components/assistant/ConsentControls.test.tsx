import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ConsentControls from "./ConsentControls";

describe("diagnostic consent", () => {
  it("does not grant access on render and leaves guest checks unchecked", () => {
    const onGrant = vi.fn();
    render(<ConsentControls grant={null} busy={false} onGrant={onGrant} onRevoke={() => {}} />);
    expect(onGrant).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Allow diagnostics for 30 minutes" }));
    expect(onGrant).toHaveBeenCalledWith(false);
  });

  it("keeps revocation available while a request is running", () => {
    const revoke = vi.fn();
    render(<ConsentControls grant={{ id: "grant", guest_checks: false, expires_at: new Date(Date.now() + 60000).toISOString() }} busy onGrant={() => {}} onRevoke={revoke} />);
    fireEvent.click(screen.getByRole("button", { name: "Revoke access" }));
    expect(revoke).toHaveBeenCalledOnce();
  });
});
