export interface ChangelogEntry {
  title: string;
  changes: string[];
}

/** Monthly headings and bullet entries are the supported CHANGELOG.md format. */
export function parseChangelog(markdown: string): ChangelogEntry[] {
  const entries: ChangelogEntry[] = [];
  for (const line of markdown.split(/\r?\n/)) {
    if (line.startsWith("## ")) {
      entries.push({ title: line.slice(3).trim(), changes: [] });
    } else if (line.startsWith("- ") && entries.length) {
      entries[entries.length - 1].changes.push(line.slice(2).trim());
    }
  }
  return entries;
}
