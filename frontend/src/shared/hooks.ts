import { useCallback, useEffect, useRef, useState } from "react";
import { useBlocker } from "react-router-dom";
import { ApiError } from "../api/client";
export function useVisible() {
  const [visible, set] = useState(document.visibilityState !== "hidden");
  useEffect(() => {
    const fn = () => set(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", fn);
    return () => document.removeEventListener("visibilitychange", fn);
  }, []);
  return visible;
}
// A retry after a lost response retains the same body, revision and idempotency key.
// A new form value is a new intent; mutations are never automatically replayed.
export function useAction<T>() {
  const [pending, setPending] = useState(false),
    [error, setError] = useState<unknown>(null),
    [result, setResult] = useState<T>();
  const active = useRef(false),
    intent = useRef<{ signature: string; key: string } | undefined>(undefined);
  const run = useCallback(
    async (signature: unknown, fn: (key: string) => Promise<T>) => {
      if (active.current) return;
      const value = JSON.stringify(signature);
      if (intent.current?.signature !== value)
        intent.current = { signature: value, key: crypto.randomUUID() };
      active.current = true;
      setPending(true);
      setError(null);
      try {
        const data = await fn(intent.current.key);
        setResult(data);
        intent.current = undefined;
        return data;
      } catch (e) {
        setError(e);
        if (e instanceof ApiError && e.status < 500 && e.status !== 429)
          intent.current = undefined;
        return undefined;
      } finally {
        active.current = false;
        setPending(false);
      }
    },
    [],
  );
  return {
    pending,
    error,
    result,
    run,
    setError,
    clear: () => {
      setError(null);
      setResult(undefined);
    },
  };
}
export function useUnsaved(dirty: boolean) {
  const allowed = useRef(false),
    dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const blocker = useBlocker(() => dirtyRef.current && !allowed.current);
  useEffect(() => {
    const fn = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", fn);
    return () => window.removeEventListener("beforeunload", fn);
  }, [dirty]);
  return {
    ...blocker,
    allowNavigation: () => {
      allowed.current = true;
    },
  };
}
