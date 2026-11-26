import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AssistantMessage from "./AssistantMessage";

describe("assistant response formatting", () => {
  it("renders headings, emphasis, lists, tables and literal command placeholders", () => {
    const { container } = render(<AssistantMessage text={'## SSH guidance\n\n**Check these:**\n\n- VM running\n- Forward configured\n\n| Check | Verify |\n| --- | --- |\n| Port | Use `ssh -p <port>` |\n\n```sh\nssh -p 2222 user@host\n```'} />);
    expect(screen.getByRole("heading", { name: "SSH guidance" })).toBeTruthy();
    expect(container.querySelector("strong")?.textContent).toBe("Check these:");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("columnheader", { name: "Verify" })).toBeTruthy();
    expect(screen.getByText("ssh -p <port>").tagName).toBe("CODE");
    expect(container.querySelector("pre code")?.textContent).toContain("ssh -p 2222");
  });

  it("does not create active HTML, images, or model-provided links", () => {
    const { container } = render(<AssistantMessage text={'<script>alert(1)</script>\n\n<img src="https://example.test/track" onerror="alert(1)">\n\n![tracking](https://example.test/pixel)\n\n[Settings](javascript:alert%281%29) and [External](https://example.test)\n\n**Safe text**'} />);
    expect(container.querySelector("script,img,a,iframe,input")).toBeNull();
    expect(screen.getByText("Safe text")).toBeTruthy();
    expect(screen.getByText("External")).toBeTruthy();
  });
});
