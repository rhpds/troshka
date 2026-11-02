/** Prefer the newest stable release over any engineering/release candidate. */
export function pickDefaultOcpVersion(versions: string[]): string {
  if (!versions.length) return "";
  const cmp = (x: string, y: string) => x.localeCompare(y, "en", { numeric: true });
  const stable = versions.filter((v) => !v.includes("-") &&
    (Number.parseInt(v, 10) < 5 || /^\d+\.\d+\.\d+$/.test(v)));
  const pool = stable.length ? stable : versions;
  return [...pool].sort(cmp)[pool.length - 1];
}

export function templateVersionChoices(
  allowed: string[] | undefined,
  available: string[],
  isOkd: boolean,
): string[] {
  if (isOkd && allowed?.length) return allowed;
  if (!allowed?.length) return available;
  return available.filter((version) => allowed.some((v) =>
    version === v || (/^\d+\.\d+$/.test(v) && version.startsWith(`${v}.`)),
  ));
}

export const OCP_VERSION_PATTERN = /^\d+\.\d+(?:\.\d+(?:-(?:ec|rc)\.\d+)?)?$/;

export function formatTemplateVersionLabel(
  minor: string,
  isOkd: boolean,
  latest?: string,
): string {
  const major = Number.parseInt(minor.split(".")[0], 10) || 0;
  if (isOkd) return major >= 5 ? `${minor} (preview)` : minor;
  if (minor.includes("-") || (major >= 5 && /^\d+\.\d+$/.test(minor))) return `${minor} (dev preview)`;
  return latest && latest !== minor ? `${minor} (latest: ${latest})` : minor;
}
