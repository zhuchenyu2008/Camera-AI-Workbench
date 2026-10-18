import { useEffect, useRef } from "react";
import { hashKey, QueryClient, useQuery } from "@tanstack/react-query";
import { api, ApiError, type Args, type Op } from "./client";
import type { Operations } from "./generated/types";
import { serverNow } from "../shared/time";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10000,
      gcTime: 300000,
      refetchOnWindowFocus: false,
      retry: (count, e) =>
        count < 2 && (!(e instanceof ApiError) || e.status >= 500),
      retryDelay: (n, e) =>
        e instanceof ApiError && e.retryAt
          ? Math.max(0, e.retryAt - serverNow())
          : Math.min(1000 * 2 ** n, 8000),
    },
    mutations: { retry: false, gcTime: 0 },
  },
});
export function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return [...new Set(value)].sort();
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value)
        .filter(([, v]) => v !== undefined && v !== null && v !== "")
        .sort()
        .map(([k, v]) => [k, canonical(v)]),
    );
  return value;
}
type Poll = {
  refs: number;
  timer?: ReturnType<typeof setTimeout>;
  refresh: () => Promise<unknown>;
  interval: () => number | false;
  born: number;
};
const polls = new Map<string, Poll>();
function schedule(key: string, immediate = false) {
  const p = polls.get(key);
  if (!p) return;
  clearTimeout(p.timer);
  if (document.visibilityState === "hidden") return;
  const interval = p.interval();
  if (!interval) return;
  p.timer = setTimeout(
    async () => {
      try {
        await p.refresh();
      } finally {
        if (polls.get(key) === p) schedule(key);
      }
    },
    immediate ? 0 : interval,
  );
}
document.addEventListener("visibilitychange", () => {
  for (const key of polls.keys())
    schedule(key, document.visibilityState === "visible");
});
export function stopPolling() {
  for (const p of polls.values()) clearTimeout(p.timer);
  polls.clear();
}
export const pollingCount = () => polls.size;
export function useApi<K extends Op>(
  op: K,
  args: Args<K> = {} as Args<K>,
  options: {
    enabled?: boolean;
    poll?:
      | number
      | ((
          data: Operations[K]["response"] | undefined,
          age: number,
        ) => number | false);
    gcTime?: number;
  } = {},
) {
  const queryKey = [op, canonical(args)];
  const key = hashKey(queryKey);
  const result = useQuery<Operations[K]["response"], Error>({
    queryKey,
    queryFn: ({ signal }) => api(op, { ...args, signal }),
    enabled: options.enabled,
    gcTime: options.gcTime,
  });
  const ref = useRef({ result, options });
  ref.current = { result, options };
  useEffect(() => {
    if (!options.poll || options.enabled === false) return;
    let p = polls.get(key);
    if (p) p.refs++;
    else {
      p = {
        refs: 1,
        born: performance.now(),
        refresh: () =>
          queryClient.refetchQueries(
            { queryKey, exact: true },
            { cancelRefetch: false },
          ),
        interval: () =>
          typeof ref.current.options.poll === "function"
            ? ref.current.options.poll(
                queryClient.getQueryData<Operations[K]["response"]>(queryKey),
                performance.now() - p!.born,
              )
            : ref.current.options.poll || false,
      };
      polls.set(key, p);
      schedule(key);
    }
    return () => {
      if (--p.refs === 0) {
        clearTimeout(p.timer);
        polls.delete(key);
      }
    };
  }, [key, Boolean(options.poll), options.enabled]);
  return result;
}
export function invalidate(...ops: Op[]) {
  return queryClient.invalidateQueries({
    predicate: (q) => ops.includes(q.queryKey[0] as Op),
  });
}
export const recordOps: Op[] = [
  "getEvent",
  "listEvents",
  "getMinute",
  "listMinutes",
  "getTimeline",
  "getOverview",
  "getSummary",
  "listSummaries",
];
export function invalidateMedia() {
  window.dispatchEvent(new Event("media-invalidated"));
  return invalidate(
    "getEvidenceMetadata",
    "listMinuteEvidence",
    "getPreviewStatus",
  );
}
