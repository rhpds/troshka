import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import PatternPreviewModal from "../PatternPreviewModal";

afterEach(() => vi.unstubAllGlobals());

it("keeps the loading preview dismissible through its native dialog", () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  const onClose = vi.fn();
  render(<PatternPreviewModal patternId="pattern" patternName="Lab" onClose={onClose} />);
  expect(screen.getByText("Loading topology...")).toBeInTheDocument();
  fireEvent(screen.getByRole("dialog", { name: "Lab preview" }), new Event("cancel", { cancelable: true }));
  expect(onClose).toHaveBeenCalledOnce();
});

it("shows the empty state after a failed preview request and allows dismissal", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  const onClose = vi.fn();
  render(<PatternPreviewModal patternId="pattern" patternName="Lab" onClose={onClose} />);
  expect(await screen.findByText("No topology data")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss dialog" }));
  expect(onClose).toHaveBeenCalledOnce();
});
