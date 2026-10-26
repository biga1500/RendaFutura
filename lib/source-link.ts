export function sourceLink(value: unknown): { href: string; label: string } | null {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || !url.hostname || url.username || url.password) return null;
    return { href: url.href, label: url.hostname };
  } catch {
    return null;
  }
}
