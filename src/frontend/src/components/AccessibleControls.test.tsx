import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AlertModal from "./AlertModal";
import TagEditor from "./TagEditor";
import LibraryPicker from "./canvas/LibraryPicker";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("accessible controls", () => {
  it("keeps alert content clicks inside the dialog and dismisses via Escape", () => {
    const close = vi.fn(); render(<AlertModal message="Details" onClose={close} />);
    fireEvent.click(screen.getByText("Details")); expect(close).not.toHaveBeenCalled();
    fireEvent(screen.getByRole("dialog", { name: "Notice" }), new Event("cancel", { bubbles: true, cancelable: true }));
    expect(close).toHaveBeenCalledOnce();
  });
  it("dismisses an alert through its backdrop button", () => {
    const close = vi.fn(); render(<AlertModal message="Details" onClose={close} />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss dialog" })); expect(close).toHaveBeenCalledOnce();
  });
  it("uses buttons to add and remove tags", () => {
    const add = vi.fn(), remove = vi.fn(); render(<TagEditor tags={["old"]} onAdd={add} onRemove={remove} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove tag old" })); expect(remove).toHaveBeenCalledWith("old");
    fireEvent.click(screen.getByRole("button", { name: "+ tag" }));
    fireEvent.change(screen.getByPlaceholderText("tag"), { target: { value: " New " } }); fireEvent.keyDown(screen.getByPlaceholderText("tag"), { key: "Enter" });
    expect(add).toHaveBeenCalledWith("new");
  });
  it("offers only ready disk images as selectable buttons", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => [
      { id: "ready", name: "Ready disk", format: "qcow2", size_bytes: 2.5 * 1024 ** 3, state: "ready" },
      { id: "iso", name: "Install ISO", format: "iso", size_bytes: 1, state: "ready" },
      { id: "pending", name: "Pending disk", format: "qcow2", size_bytes: 1, state: "importing" },
    ] }));
    const select = vi.fn(), close = vi.fn(); render(<LibraryPicker type="image" onSelect={select} onClose={close} />);
    const button = await screen.findByRole("button", { name: /Ready disk/ });
    expect(screen.queryByText("Install ISO")).not.toBeInTheDocument(); expect(screen.queryByText("Pending disk")).not.toBeInTheDocument();
    fireEvent.click(button);
    expect(select).toHaveBeenCalledWith({ id: "ready", name: "Ready disk", size_gb: 3, format: "qcow2" }); expect(close).toHaveBeenCalledOnce();
  });
});
