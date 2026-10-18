const zone = "Asia/Shanghai";
let anchor = Date.now();
let monotonic = performance.now();
export function syncServerTime(value: string) {
  const stamp = Date.parse(value);
  if (Number.isFinite(stamp)) {
    anchor = stamp;
    monotonic = performance.now();
  }
}
export const serverNow = () => anchor + performance.now() - monotonic;
export function localDate(value: string | number = serverNow()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}
export function clock(
  value: string | number | null | undefined,
  seconds = false,
) {
  if (value == null || !Number.isFinite(new Date(value).getTime()))
    return "尚无记录";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: zone,
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
    hour12: false,
  }).format(new Date(value));
}
export function dateTime(value: string | number | null | undefined) {
  return value == null
    ? "尚无记录"
    : `${localDate(value)} ${clock(value, true)}（北京时间）`;
}
export function dateLabel(date: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: zone,
    month: "long",
    day: "numeric",
    weekday: "long",
  }).format(new Date(`${date}T12:00:00+08:00`));
}
export function dayRange(date: string, now = serverNow()) {
  const safe =
    /^\d{4}-\d{2}-\d{2}$/.test(date) &&
    Number.isFinite(Date.parse(`${date}T00:00:00+08:00`))
      ? date
      : localDate(now);
  const start = Date.parse(`${safe}T00:00:00+08:00`);
  const end = start + 86400000;
  return {
    from: new Date(start).toISOString(),
    to: new Date(Math.max(start + 1, Math.min(end, now))).toISOString(),
  };
}
export function beijingInput(value: string) {
  return new Date(
    `${value}${value.length === 16 ? ":00" : ""}+08:00`,
  ).toISOString();
}
export function toInput(value: string) {
  return `${localDate(value)}T${clock(value, true)}`;
}
export function duration(ms: number | undefined, estimated = true) {
  if (ms == null) return "未知";
  const sec = Math.floor(Math.max(0, ms) / 1000);
  const h = Math.floor(sec / 3600),
    m = Math.floor((sec % 3600) / 60),
    s = sec % 60;
  return `${estimated ? "约 " : ""}${h ? `${h} 小时${m ? ` ${m} 分钟` : ""}${s ? ` ${s} 秒` : ""}` : m ? `${m} 分钟${s ? ` ${s} 秒` : ""}` : `${s} 秒`}`;
}
export function compactDuration(ms: number) {
  const sec = Math.floor(ms / 1000);
  return sec >= 3600
    ? `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`
    : sec >= 60
      ? `${Math.floor(sec / 60)}m`
      : `${sec}s`;
}
export function bytes(value: unknown) {
  if (typeof value !== "number") return "未知";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const i =
    value > 0 ? Math.min(4, Math.floor(Math.log(value) / Math.log(1024))) : 0;
  return `${(value / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}
