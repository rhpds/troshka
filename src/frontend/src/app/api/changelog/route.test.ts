// @vitest-environment node
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./route";

let root: string;
let frontend: string;
beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), "troshka-changelog-"));
  frontend = path.join(root, "src/frontend");
  await mkdir(frontend, { recursive: true });
  vi.spyOn(process, "cwd").mockReturnValue(frontend);
});
afterEach(async () => {
  vi.restoreAllMocks();
  await rm(root, { recursive: true, force: true });
});

describe("runtime changelog", () => {
  it("reads local edits on successive requests without restarting", async () => {
    const filename = path.join(root, "CHANGELOG.md");
    await writeFile(filename, "## November 2026\n- Before");
    const before = await GET();
    expect(before.status).toBe(200);
    expect(before.headers.get("Cache-Control")).toBe("no-store");
    expect(await before.text()).toContain("Before");
    await writeFile(filename, "## November 2026\n- After");
    expect(await (await GET()).text()).toContain("After");
  });

  it("prefers the file packaged at the container working directory", async () => {
    await writeFile(path.join(root, "CHANGELOG.md"), "Repository notes");
    await writeFile(path.join(frontend, "CHANGELOG.md"), "Packaged notes");
    expect(await (await GET()).text()).toBe("Packaged notes");
  });

  it("returns an uncached error without exposing filesystem paths", async () => {
    const response = await GET();
    expect(response.status).toBe(503);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(await response.text()).toBe("Release notes are temporarily unavailable.");
  });
});
