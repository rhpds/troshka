import { readFile } from "node:fs/promises";
import path from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const headers = {
  "Content-Type": "text/plain; charset=utf-8",
  "Cache-Control": "no-store",
};

export async function GET() {
  // Container images carry the file at their root; local dev uses the repo root.
  const paths = [
    path.resolve(process.cwd(), "CHANGELOG.md"),
    path.resolve(process.cwd(), "../../CHANGELOG.md"),
  ];
  for (const filename of paths) {
    try {
      return new Response(await readFile(filename, "utf8"), { headers });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") break;
    }
  }
  return new Response("Release notes are temporarily unavailable.", { status: 503, headers });
}
