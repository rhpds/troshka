import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AboutModal from "./AboutModal";
import { parseChangelog } from "@/lib/changelog";

afterEach(() => { cleanup(); vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe("About Troshka", () => {
  it("shows the build release, attribution, and collapsed release notes", async () => {
    vi.stubEnv("NEXT_PUBLIC_TROSHKA_VERSION", "v2026.11.14-214005");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("# Changelog\n\n## November 2026\n- New About dialog\n\n## October 2026\n- Storage fixes")));
    const close = vi.fn();
    render(<AboutModal onClose={close} />);
    expect(screen.getByRole("dialog", { name: "About Troshka" })).toBeVisible();
    expect(screen.getByRole("link", { name: "v2026.11.14-214005" })).toHaveAttribute("href", "https://github.com/rhpds/troshka/releases/tag/v2026.11.14-214005");
    expect(screen.getByText(/Patrick T. Rutledge III/)).toBeVisible();
    expect(screen.getByText("Red Hat Demo Platform Team")).toBeVisible();
    const release = (await screen.findByText("November 2026")).closest("details")!;
    expect(release).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("November 2026"));
    expect(release).toHaveAttribute("open");
    fireEvent.click(screen.getByRole("button", { name: "Close About" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("handles development builds and missing notes without a false release link", async () => {
    vi.stubEnv("NEXT_PUBLIC_TROSHKA_VERSION", "v2026.11.14-214005-dirty");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("")));
    const close = vi.fn();
    render(<AboutModal onClose={close} />);
    expect(screen.queryByRole("link", { name: /v2026/ })).not.toBeInTheDocument();
    expect(await screen.findByText(/No release notes/)).toBeVisible();
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { bubbles: true, cancelable: true }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("fetches updated notes on reopen without caching", async () => {
    const fetchNotes = vi.fn()
      .mockResolvedValueOnce(new Response("## November 2026\n- Original notes"))
      .mockResolvedValueOnce(new Response("## November 2026\n- Updated notes"));
    vi.stubGlobal("fetch", fetchNotes);
    const first = render(<AboutModal onClose={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading release notes");
    await screen.findByText("Original notes");
    first.unmount();
    render(<AboutModal onClose={vi.fn()} />);
    await screen.findByText("Updated notes");
    expect(screen.queryByText("Original notes")).not.toBeInTheDocument();
    expect(fetchNotes).toHaveBeenCalledTimes(2);
    expect(fetchNotes).toHaveBeenLastCalledWith("/api/changelog", expect.objectContaining({ cache: "no-store" }));
  });

  it("keeps About usable when notes cannot be loaded", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("Unavailable", { status: 503 })));
    const close = vi.fn();
    render(<AboutModal onClose={close} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Release notes could not be loaded");
    fireEvent.click(screen.getByRole("button", { name: "Close About" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("parses release bullets with Windows newlines and ignores preamble text", () => {
    expect(parseChangelog("# Changelog\r\n- Preamble\r\n## November 2026\r\n\r\n- A fix\r\n## October 2026\r\n- Older fix")).toEqual([
      { title: "November 2026", changes: ["A fix"] },
      { title: "October 2026", changes: ["Older fix"] },
    ]);
  });
});
