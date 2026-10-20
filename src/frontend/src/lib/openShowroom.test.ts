import { afterEach, describe, expect, it, vi } from "vitest";
import { openShowroom } from "./openShowroom";

afterEach(() => vi.restoreAllMocks());

describe("openShowroom", () => {
  function mockTab() {
    const tab = {
      opener: {},
      document: document.implementation.createHTMLDocument(),
      location: { replace: vi.fn() },
      close: vi.fn(),
    };
    vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    return tab;
  }

  it("fetches the latest deployment on every click before navigating", async () => {
    const tab = mockTab();
    const fetcher = vi.spyOn(globalThis, "fetch");
    for (const token of ["first", "rotated"]) {
      fetcher.mockResolvedValueOnce(new Response(JSON.stringify({
        deployed_topology: {
          _showroom_url: "https://showroom.example.com/?token=old",
          _showroom_access_token: token,
        },
      })));
      await openShowroom("project-id");
      expect(tab.location.replace).toHaveBeenLastCalledWith(
        `https://showroom.example.com/?token=${token}`,
      );
    }
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenCalledWith("/api/v1/projects/project-id", { cache: "no-store" });
    expect(tab.opener).toBeNull();
  });

  it.each([503, 200])("closes the tab without stale navigation when no URL is available (%s)", async (status) => {
    const tab = mockTab();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status }));
    await expect(openShowroom("project-id")).rejects.toThrow();
    expect(tab.close).toHaveBeenCalledOnce();
    expect(tab.location.replace).not.toHaveBeenCalled();
  });

  it("reports blocked popups", async () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    await expect(openShowroom("project-id")).rejects.toThrow("Allow popups");
  });
});
