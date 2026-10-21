/** Trim one delimiter without repeatedly retrying a trailing regular expression. */
export function trimEdgeCharacter(value: string, character: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && value[start] === character) start++;
  while (end > start && value[end - 1] === character) end--;
  return value.slice(start, end);
}

/** Split a numbered name, dropping one optional separator before the digits. */
export function numericNameSuffix(value: string): { prefix: string; digits: string } | null {
  let start = value.length;
  while (start > 0 && value[start - 1] >= "0" && value[start - 1] <= "9") start--;
  if (start === value.length) return null;
  const separator = value[start - 1];
  return {
    prefix: value.slice(0, separator === "-" || separator === "_" ? start - 1 : start),
    digits: value.slice(start),
  };
}
