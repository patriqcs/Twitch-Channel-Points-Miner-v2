import { clsx, type ClassValue } from "clsx";

export function cn(...inputs: ClassValue[]) {
  return clsx(inputs);
}

export function fmtNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  return n.toLocaleString("de-DE");
}

/** Parse a backend timestamp. SQLite drops the timezone, so the API sends naive
 *  UTC (no offset) — treat a missing tz as UTC, otherwise the browser reads it as
 *  local time and shows it ~2h off. */
export function parseTs(iso: string): Date {
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  return new Date(hasTz ? iso : iso + "Z");
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  return parseTs(iso).toLocaleString("de-DE");
}
