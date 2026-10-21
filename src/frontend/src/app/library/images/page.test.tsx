import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ImagesPage from "./page";
const confirm = vi.hoisted(() => vi.fn());
vi.mock("@patternfly/react-core", async (original) => ({
  ...await original<typeof import("@patternfly/react-core")>(),
  FileUpload: ({ onFileInputChange, onClearClick }: { onFileInputChange: (event: unknown, file: File) => void; onClearClick: () => void }) => <><input aria-label="Upload file" type="file" onChange={event => onFileInputChange(event, event.target.files![0])} /><button onClick={onClearClick}>Clear file</button></>,
}));
vi.mock("@/lib/confirm", () => ({ appConfirm: confirm }));
vi.mock("@/components/TagEditor", () => ({ default: ({ onAdd, onRemove }: { onAdd: (tag: string) => void; onRemove: (tag: string) => void }) => <><button onClick={() => onAdd("new")}>Add tag</button><button onClick={() => onRemove("old")}>Remove tag</button></> }));
const response = (data: unknown, status = 200) => ({ ok: status < 400, json: async () => data }) as Response;
let item: Record<string, unknown>;
let routes: Record<string, Response | Error>;
let calls: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks(); confirm.mockResolvedValue(true);
  item = { id: "image-1", name: "RHEL DVD", description: "Installer", type: "iso", format: "iso", size_bytes: 1024 ** 3, state: "ready", tags: { user_tags: ["old"] }, created_at: "2026-10-01", owned: true, owner_email: "alice@example.com" };
  routes = { "/api/v1/auth/me": response({ role: "admin" }), "POST /api/v1/library/": response({ id: "new" }) };
  calls = vi.fn(async (url: string, options?: RequestInit) => {
    const result = routes[`${options?.method || "GET"} ${url}`] || routes[url];
    if (result instanceof Error) throw result;
    if (result) return result;
    if (url.startsWith("/api/v1/library/") && !options?.method) return response([item]);
    return response({});
  });
  vi.stubGlobal("fetch", calls);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function loaded() { render(<ImagesPage />); await screen.findByRole("button", { name: "RHEL DVD" }); }
function click(name: string) { fireEvent.click(screen.getByRole("button", { name })); }
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
function body(method: string, path: string) {
  const call = calls.mock.calls.find(([url, opts]) => url === path && opts?.method === method);
  expect(call).toBeDefined(); return JSON.parse(call![1]!.body as string);
}
describe("image library", () => {
  it.each([
    ["success", ""], ["start", "Failed to start upload"], ["part", "Failed to get part URL"], ["xhr", "Part 1 failed (HTTP 500)"], ["complete", "Upload finalization failed"],
  ])("uploads file through multipart protocol: %s", async (failure, message) => {
    routes["POST /api/v1/library/new/upload-start"] = response({ upload_id: "upload-1" }, failure === "start" ? 400 : 200);
    routes["POST /api/v1/library/new/upload-part-url?upload_id=upload-1&part_number=1"] = response({ url: "https://s3.example/part" }, failure === "part" ? 400 : 200);
    routes["POST /api/v1/library/new/upload-complete"] = response({}, failure === "complete" ? 400 : 200);
    class UploadRequest {
      upload: { onprogress?: (event: { lengthComputable: boolean; loaded: number }) => void } = {};
      status = failure === "xhr" ? 500 : 200;
      onload?: () => void;
      open = vi.fn(); getResponseHeader = () => "etag-1";
      send() { this.upload.onprogress?.({ lengthComputable: true, loaded: 4 }); queueMicrotask(() => this.onload?.()); }
    }
    vi.stubGlobal("XMLHttpRequest", UploadRequest);
    await loaded(); click("+ Upload"); fireEvent.change(screen.getByLabelText("Upload file"), { target: { files: [new File(["data"], "disk.iso")] } });
    expect(screen.getByLabelText("Name")).toHaveValue("disk"); click("Upload");
    if (message) expect(await screen.findByText(message)).toBeInTheDocument();
    else {
      await waitFor(() => expect(screen.queryByLabelText("Upload file")).not.toBeInTheDocument());
      expect(body("POST", "/api/v1/library/new/upload-complete")).toEqual({ upload_id: "upload-1", parts: [{ part_number: 1, etag: "etag-1" }] });
    }
  });
  it("searches the API and filters ownership", async () => {
    await loaded(); fireEvent.change(screen.getByPlaceholderText("Search images..."), { target: { value: "RHEL 10" } });
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/library/?q=RHEL+10"));
    fireEvent.click(screen.getByLabelText("Mine"));
    expect(screen.getByText("No images match the selected ownership filters.")).toBeInTheDocument();
  });
  it.each([["downloading", "starting download…"], ["importing", "importing…"], ["uploading", "uploading…"], ["pending", "pending"], ["error", "error"], ["unknown", "unknown"]])("renders %s state", async (state, label) => {
    item.state = state; item.size_bytes = 0; await loaded(); expect(screen.getByText(label)).toBeInTheDocument();
  });
  it("shows multipart progress", async () => {
    item.state = "uploading_s3"; item.tags = { total_parts: 4, uploaded_parts: 3 }; await loaded(); expect(screen.getByText("uploading · 75%")).toBeInTheDocument();
  });
  it("renames on blur and cancels editing with escape", async () => {
    await loaded(); click("RHEL DVD"); const input = screen.getByDisplayValue("RHEL DVD");
    fireEvent.change(input, { target: { value: " Renamed " } }); fireEvent.blur(input);
    expect(body("PATCH", "/api/v1/library/image-1")).toEqual({ name: "Renamed" });
    click("RHEL DVD"); fireEvent.keyDown(screen.getByDisplayValue("RHEL DVD"), { key: "Escape" });
    expect(screen.getByRole("button", { name: "RHEL DVD" })).toBeInTheDocument();
  });
  it.each([true, false])("deletes with confirmation=%s", async (accepted) => {
    confirm.mockResolvedValue(accepted); await loaded(); click("Delete");
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(calls.mock.calls.some(([, opts]) => opts?.method === "DELETE")).toBe(accepted);
  });
  it("prevents edits and deletion of gold images", async () => {
    item.readonly = true; item.source = "central"; await loaded();
    expect(screen.getByRole("button", { name: "RHEL DVD" })).toBeDisabled(); expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  });
  it("updates default ISO and tags without discarding existing tags", async () => {
    await loaded(); fireEvent.click(screen.getByLabelText("OCP Default"));
    await waitFor(() => expect(body("PATCH", "/api/v1/library/image-1")).toEqual({ tags: { user_tags: ["old"], ocp_default_iso: true } }));
    click("Add tag"); click("Remove tag");
    await waitFor(() => expect(calls.mock.calls.filter(([, opts]) => opts?.method === "PATCH")).toHaveLength(3));
  });
  it("retries an errored URL import after saving the updated source", async () => {
    item.state = "error"; item.source_url = "https://old.example/disk";
    await loaded(); click("Edit URL"); change("Source URL", "https://new.example/disk"); click("Save & Retry");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(body("PATCH", "/api/v1/library/image-1")).toEqual({ source_url: "https://new.example/disk" });
    expect(body("POST", "/api/v1/library/image-1/import-url")).toEqual({ url: "https://new.example/disk" });
  });
  it("cancels an in-flight transfer", async () => {
    item.state = "importing"; await loaded(); click("Cancel");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/library/image-1/cancel", { method: "POST" }));
  });
  it("validates metadata and URL before creating an item", async () => {
    await loaded(); click("+ Upload"); click("Upload"); expect(screen.getByText("Name is required")).toBeInTheDocument();
    change("Name", "New image"); click("Upload"); expect(screen.getByText("Select a file")).toBeInTheDocument();
    click("Import from URL"); click("Upload"); expect(screen.getByText("Enter a URL")).toBeInTheDocument();
  });
  it.each([[200, 200, "Import started — download in progress on server"], [400, 200, "Failed to create item"], [200, 400, "Failed to start import"]])("imports URL with metadata status %s/import status %s", async (createStatus, importStatus, message) => {
    routes["POST /api/v1/library/"] = response({ id: "new" }, createStatus); routes["POST /api/v1/library/new/import-url"] = response({}, importStatus);
    await loaded(); click("+ Upload"); change("Name", "New image"); change("Type", "image"); change("Format", "raw"); change("Description (optional)", "disk"); click("Import from URL");
    fireEvent.change(screen.getByPlaceholderText("https://example.com/rhel-9.4-x86_64-dvd.iso"), { target: { value: "https://example.com/disk.raw" } }); click("Upload");
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(body("POST", "/api/v1/library/")).toMatchObject({ name: "New image", type: "image", format: "raw", description: "disk" });
  });
});
