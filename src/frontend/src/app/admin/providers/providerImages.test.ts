import { afterEach, describe, expect, it, vi } from "vitest";
import { detectImageVersion, discoverProviderImages, filterProviderImages, type ProviderImageOption } from "./providerImages";

afterEach(() => vi.unstubAllGlobals());
const option = (name: string, type = "BYOS"): ProviderImageOption => ({ name, type, label: name, image_id: name, created: "" });

describe("provider image discovery", () => {
  it.each([
    ["ec2", "discover-images", { images: [option("rhel-9")] }, "rhel-9"],
    ["gcp", "discover-images-gcp", [{ source: "PAYG", name: "rhel-9", self_link: "projects/image", family: "rhel", creation_timestamp: "2026-01-01" }], "projects/image"],
    ["azure", "discover-images-azure", [{ source: "PAYG", name: "rhel-9", version: "latest", urn: "RedHat:rhel:9:latest" }], "RedHat:rhel:9:latest"],
    ["ocpvirt", "discover-datasources", { datasources: [{ ready: false, name: "not-ready" }, { ready: true, name: "rhel-9" }] }, "rhel-9"],
  ])("maps %s images and calls its discovery endpoint", async (type, endpoint, data, id) => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => data });
    vi.stubGlobal("fetch", fetch);
    const result = await discoverProviderImages("provider", type);
    expect(fetch).toHaveBeenCalledWith(`/api/v1/providers/provider/${endpoint}`);
    expect(result).toHaveLength(1);
    expect(result?.[0].image_id).toBe(id);
  });
  it("distinguishes empty results from a failed response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({ ok: false }).mockResolvedValueOnce({ ok: true, json: async () => ({}) }));
    expect(await discoverProviderImages("p", "ec2")).toBeNull();
    expect(await discoverProviderImages("p", "ec2")).toEqual([]);
  });
  it("does not query unsupported provider types", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    expect(await discoverProviderImages("p", "s3")).toEqual([]);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("image filtering", () => {
  it.each([
    ["Fedora 42", "fedora"], ["rhel-10.2", "10"], ["10-lvm", "10"],
    ["lvm10-gold", "10"], ["rhel-9", "9"], ["9-lvm", "9"], ["lvm-9-gold", "9"], ["custom", ""],
  ])("detects %s as version %s", (name, version) => {
    expect(detectImageVersion(option(name))).toBe(version);
  });
  it("uses Fedora family metadata even when the label is opaque", () => {
    expect(detectImageVersion({ ...option("opaque"), family: "fedora" })).toBe("fedora");
  });
  it("combines source, version, and case-insensitive field matching", () => {
    const images = [option("RHEL-9-Gold"), option("RHEL-10.2-Gold"), option("RHEL-9-Payg", "PAYG")];
    expect(filterProviderImages(images, "BYOS", "9", "gold")).toEqual([images[0]]);
    expect(filterProviderImages(images, "all", "all", "")).toEqual(images);
    expect(filterProviderImages(images, "all", "all", "missing")).toEqual([]);
    const opaque = { ...option("internal"), label: "Friendly", image_id: "image-id" };
    for (const search of ["friendly", "image-id", "internal"]) expect(filterProviderImages([opaque], "all", "all", search)).toEqual([opaque]);
  });
});
