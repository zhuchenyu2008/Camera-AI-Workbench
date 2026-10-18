import { operations } from "./generated/operations";
import type { Operations, ErrorEnvelope } from "./generated/types";
import { clock, syncServerTime, serverNow } from "../shared/time";

export type Op = keyof Operations;
type QueryArgs<K extends Op> = Operations[K]["parameters"] extends {
  query: infer Q;
}
  ? {} extends Q
    ? { query?: Q }
    : { query: Q }
  : {};
export type Args<K extends Op> = Omit<
  Operations[K]["parameters"],
  "header" | "query"
> &
  QueryArgs<K> & {
    body?: Operations[K]["body"] | FormData;
    revision?: number;
    intent?: string;
    signal?: AbortSignal;
  };
let csrf = "";
export const setCsrf = (token: string) => {
  csrf = token;
};
export const sessionEvents = new EventTarget();
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId = "",
    public fields: ErrorEnvelope["error"]["field_errors"] = [],
    public retryAt: number | null = null,
    public details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}
export function sameOriginMedia(path: string) {
  const url = new URL(path, location.origin);
  if (
    url.origin !== location.origin ||
    !url.pathname.startsWith("/api/") ||
    url.username ||
    url.password
  )
    throw new ApiError(400, "UNSAFE_MEDIA", "媒体地址不属于受保护的同源接口");
  return url.pathname + url.search;
}
export function safeReturnTo(value: string | null) {
  if (
    !value ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    /[\\\r\n]/.test(value)
  )
    return "/";
  try {
    const u = new URL(value, location.origin);
    return u.origin === location.origin && u.pathname !== "/login"
      ? u.pathname + u.search
      : "/";
  } catch {
    return "/";
  }
}
export function apiUrl<K extends Op>(op: K, args: Args<K> = {} as Args<K>) {
  let path: string = operations[op].path;
  const a = args as {
    path?: Record<string, string>;
    query?: Record<string, unknown>;
  };
  for (const [key, value] of Object.entries(a.path ?? {}))
    path = path.replace(`{${key}}`, encodeURIComponent(value));
  if (path.includes("{")) throw new Error("接口路径缺少资源 ID");
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(a.query ?? {}).sort()) {
    if (value == null || value === "") continue;
    for (const v of Array.isArray(value) ? [...new Set(value)].sort() : [value])
      search.append(key, String(v));
  }
  return path + (search.size ? `?${search}` : "");
}
export async function checkedFetch(url: string, init: RequestInit = {}) {
  const response = await fetch(
    url === "/health/live" ? url : sameOriginMedia(url),
    {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
    },
  );
  if (!response.ok) {
    let body: Partial<ErrorEnvelope> = {};
    try {
      body = await response.json();
    } catch {
      /* Media proxies may return a plain error. */
    }
    if (body.meta?.server_time) syncServerTime(body.meta.server_time);
    const after = response.headers.get("Retry-After");
    const retryAt = after
      ? /^\d+$/.test(after)
        ? serverNow() + Number(after) * 1000
        : Date.parse(after)
      : null;
    const err = new ApiError(
      response.status,
      body.error?.code ?? `HTTP_${response.status}`,
      body.error?.message ?? "请求暂时无法完成",
      body.meta?.request_id ?? response.headers.get("X-Request-ID") ?? "",
      body.error?.field_errors,
      retryAt,
      body.error?.details,
    );
    if (
      response.status === 401 &&
      !(url === "/api/session" && init.method === "POST")
    ) {
      csrf = "";
      sessionEvents.dispatchEvent(new Event("expired"));
    }
    if (response.status === 403 && err.code === "CSRF_INVALID")
      sessionEvents.dispatchEvent(new Event("csrf"));
    throw err;
  }
  return response;
}
export async function api<K extends Op>(
  op: K,
  args: Args<K> = {} as Args<K>,
): Promise<Operations[K]["response"]> {
  const meta = operations[op];
  const headers = new Headers({ "X-Request-ID": `web_${crypto.randomUUID()}` });
  if (meta.method !== "GET" && op !== "login")
    headers.set("X-CSRF-Token", csrf);
  if (meta.idempotent) {
    if (!args.intent) throw new Error("写入操作缺少固定幂等意图");
    headers.set("Idempotency-Key", args.intent);
  }
  if (meta.versioned) {
    if (args.revision == null) throw new Error("修改操作缺少原始版本");
    headers.set("If-Match", `"rev-${args.revision}"`);
  }
  const form = args.body instanceof FormData;
  if (args.body !== undefined && !form)
    headers.set("Content-Type", "application/json");
  const timeout = AbortSignal.timeout(30000);
  const signal = args.signal
    ? AbortSignal.any([args.signal, timeout])
    : timeout;
  const response = await checkedFetch(apiUrl(op, args), {
    method: meta.method,
    headers,
    signal,
    body: form
      ? (args.body as FormData)
      : args.body === undefined
        ? undefined
        : JSON.stringify(args.body),
  });
  if (response.status === 204) return undefined as Operations[K]["response"];
  if (meta.binary) return (await response.blob()) as Operations[K]["response"];
  const body = await response.json();
  if (body.meta?.server_time) syncServerTime(body.meta.server_time);
  if ((op === "getSession" || op === "login") && body.data?.csrf_token)
    setCsrf(body.data.csrf_token);
  return body;
}
export function errorText(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 429)
      return `请求较多，请于 ${error.retryAt ? clock(error.retryAt, true) : "稍后"}（北京时间）再次操作。`;
    if (error.status === 412)
      return "其他会话已更新此记录。草稿已保留，请读取新版并核对差异。";
    if (error.status === 403 && error.code === "CSRF_INVALID")
      return "会话已重新核验，请再次提交；本次写入未自动重发。";
    return error.message;
  }
  return "暂时无法连接工作台，已有内容仍然保留。请检查连接后重试。";
}
