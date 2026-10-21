export interface ProviderImageOption {
  type: string; label: string; image_id: string; name: string; created: string; family?: string;
}

export async function discoverProviderImages(id: string, type: string): Promise<ProviderImageOption[] | null> {
  const endpoints: Record<string, string> = {
    ec2: "discover-images", gcp: "discover-images-gcp", azure: "discover-images-azure", ocpvirt: "discover-datasources",
  };
  const endpoint = endpoints[type];
  if (!endpoint) return [];
  const response = await fetch(`/api/v1/providers/${id}/${endpoint}`);
  if (!response.ok) return null;
  const data = await response.json();
  if (type === "ec2") return data.images || [];
  if (type === "gcp") return data.map((img: Record<string, string>) => ({
    type: img.source, label: `${img.name} (${img.source})`, image_id: img.self_link,
    name: img.family, created: img.creation_timestamp,
  }));
  if (type === "azure") return data.map((img: Record<string, string>) => ({
    type: img.source, label: `${img.name} (${img.source}) — ${img.version}`,
    image_id: img.urn, name: img.urn, created: "",
  }));
  return data.datasources.filter((ds: { ready: boolean }) => ds.ready).map((ds: { name: string }) => ({
    image_id: ds.name, label: ds.name, name: ds.name, created: "", type: "",
  }));
}

export function detectImageVersion(image: ProviderImageOption): string {
  const hay = `${image.label} ${image.name} ${image.image_id}`.toLowerCase();
  if (/fedora/.test(hay) || image.family === "fedora") return "fedora";
  if (/rhel.?10[.\-_ ]|(?:^|[\s\-_])10[-_.]lvm|lvm10\D/.test(hay)) return "10";
  if (/rhel.?9|9[-_.]lvm|lvm.?9\D/.test(hay)) return "9";
  return "";
}

export function filterProviderImages(images: ProviderImageOption[], filter: string, version: string, search: string): ProviderImageOption[] {
  return images.filter(image => {
    if (filter !== "all" && image.type !== filter) return false;
    if (version !== "all" && detectImageVersion(image) !== version) return false;
    return !search || [image.label, image.image_id, image.name || ""].some(value => value.toLowerCase().includes(search));
  });
}
