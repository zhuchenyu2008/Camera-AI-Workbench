import type { ScheduleRule } from "../api/generated/types";
import { localDate } from "./time";
const minute = (value: string) =>
  Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5));
export function mergedWeek(rules: ScheduleRule[]) {
  const parts: [number, number][] = [];
  for (const r of rules)
    for (const d of r.weekdays) {
      const start = (d - 1) * 1440 + minute(r.start),
        end = (d - 1) * 1440 + minute(r.end) + (r.end_day_offset ?? 0) * 1440;
      if (end > 10080) {
        parts.push([start, 10080], [0, end - 10080]);
      } else parts.push([start, end]);
    }
  parts.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const [start, end] of parts) {
    const prev = merged.at(-1);
    if (prev && start <= prev[1]) prev[1] = Math.max(prev[1], end);
    else merged.push([start, end]);
  }
  return merged;
}
export function defaultOverrideEnd(rules: ScheduleRule[], now: number) {
  const date = localDate(now),
    today = Date.parse(`${date}T00:00:00+08:00`),
    weekday = (new Date(`${date}T12:00:00+08:00`).getUTCDay() + 6) % 7;
  const monday = today - weekday * 86400000;
  const future = mergedWeek(rules)
    .flatMap(([a, b]) => [a, b])
    .flatMap((m) => [monday + m * 60000, monday + (m + 10080) * 60000])
    .filter((v) => v > now)
    .sort((a, b) => a - b)[0];
  return Math.min(now + 86400000, future ?? now + 3600000);
}
export function scheduleLabel([start, end]: [number, number]) {
  const days = ["一", "二", "三", "四", "五", "六", "日"];
  const part = (m: number) =>
    `周${days[Math.floor(m / 1440) % 7]} ${String(Math.floor((m % 1440) / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  return `${part(start)}—${end === 10080 ? "次周一 00:00" : part(end)}`;
}
