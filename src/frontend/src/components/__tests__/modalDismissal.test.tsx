import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ConfirmModal from "../ConfirmModal";
import RedeployOcpModal from "../RedeployOcpModal";
import DuplicateVMModal from "../canvas/DuplicateVMModal";

describe("modal dismissal", () => {
  it("gives duplicate UUID choices accessible names from nested label text", () => {
    render(<DuplicateVMModal vmName="test-vm" hasUuid={true} onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole("radio", { name: /Generate new UUID/ })).toBeChecked();
    const clone = screen.getByRole("radio", { name: /Clone UUID/ });
    fireEvent.click(clone);
    expect(clone).toBeChecked();
  });
  it.each(["confirm", "redeploy", "duplicate"] as const)(
    "%s dismisses only for a direct backdrop click",
    (kind) => {
      const onCancel = vi.fn();
      let modal;
      if (kind === "redeploy") {
        modal = <RedeployOcpModal clusters={[]} allReady={false} onChoose={vi.fn()} onCancel={onCancel} />;
      } else if (kind === "duplicate") {
        modal = <DuplicateVMModal vmName="test-vm" hasUuid={false} onConfirm={vi.fn()} onCancel={onCancel} />;
      } else {
        modal = <ConfirmModal message="Confirm this action" onConfirm={vi.fn()} onCancel={onCancel} />;
      }
      const { container } = render(modal);
      fireEvent.click(container.querySelector(".start-order-body")!);
      expect(onCancel).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Dismiss dialog" }));
      expect(onCancel).toHaveBeenCalledOnce();
    },
  );

  it("keeps cancel available to keyboard users", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<ConfirmModal message="Confirm this action" onConfirm={vi.fn()} onCancel={onCancel} />);
    screen.getByRole("button", { name: "Cancel" }).focus();
    await user.keyboard("{Enter}");
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("handles the native cancel event used for Escape", () => {
    const onCancel = vi.fn();
    render(<ConfirmModal message="Confirm this action" onConfirm={vi.fn()} onCancel={onCancel} />);
    const event = new Event("cancel", { bubbles: false, cancelable: true });
    fireEvent(screen.getByRole("dialog"), event);
    expect(onCancel).toHaveBeenCalledOnce();
    expect(event.defaultPrevented).toBe(true);
  });
});
