/** Prefer latest stable (major < 5); OCP 5.x stays selectable but not the default. */
export function pickDefaultOcpVersion(versions: string[]): string {
  if (!versions.length) return "";
  const parse = (v: string): [number, number] => {
    const [a, b] = v.split(".");
    return [parseInt(a, 10) || 0, parseInt(b, 10) || 0];
  };
  const cmp = (x: string, y: string) => {
    const [xa, xb] = parse(x);
    const [ya, yb] = parse(y);
    return xa !== ya ? xa - ya : xb - yb;
  };
  const stable = versions.filter((v) => parse(v)[0] < 5);
  const pool = stable.length ? stable : versions;
  return [...pool].sort(cmp)[pool.length - 1];
}

export function formatTemplateVersionLabel(
  minor: string,
  isOkd: boolean,
  latest?: string,
): string {
  const major = parseInt(minor.split(".")[0], 10) || 0;
  if (isOkd) return major >= 5 ? `${minor} (preview)` : minor;
  if (major >= 5) return `${minor} (dev preview)`;
  return latest ? `${minor} (latest: ${latest})` : minor;
}
