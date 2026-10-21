import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import RunWorkloadModal from "../RunWorkloadModal";
import { useCanvasStore } from "@/stores/canvasStore";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("retains a requirement input and focus when a preceding row is removed", () => {
  useCanvasStore.setState({ nodes: [], clusters: [] });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => [] }));
  render(<RunWorkloadModal projectId="project" onClose={() => {}} onLaunched={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "+ Add requirement" }));
  fireEvent.click(screen.getByRole("button", { name: "+ Add requirement" }));
  const inputs = screen.getAllByPlaceholderText("git URL");
  fireEvent.change(inputs[1], { target: { value: "https://example.test/collection" } });
  inputs[1].focus();
  fireEvent.click(inputs[0].parentElement!.querySelector("button")!);
  expect(screen.getByPlaceholderText("git URL")).toBe(inputs[1]);
  expect(inputs[1]).toHaveFocus();
  expect(inputs[1]).toHaveValue("https://example.test/collection");
});
