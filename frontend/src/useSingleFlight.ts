import { useCallback, useEffect, useRef, useState } from "react";

export function useSingleFlight() {
  const lockedRef = useRef(false);
  const mountedRef = useRef(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const run = useCallback(async <T,>(fn: () => T | Promise<T>): Promise<T | undefined> => {
    if (lockedRef.current) return undefined;
    lockedRef.current = true;
    if (mountedRef.current) setBusy(true);
    try {
      return await fn();
    } finally {
      lockedRef.current = false;
      if (mountedRef.current) setBusy(false);
    }
  }, []);

  return { busy, run } as const;
}
