import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/** GET `url` now and every `ms` (0 = once). Keeps the last good data while refreshing. */
export function usePoll<T = any>(url: string | null, ms = 0, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const load = useCallback(async () => {
    if (!url) return;
    try {
      const d = await api.get<T>(url);
      if (alive.current) {
        setData(d);
        setError(null);
      }
    } catch (e: any) {
      if (alive.current) setError(e.message || String(e));
    }
  }, [url]);
  useEffect(() => {
    alive.current = true;
    setData(null);
    load();
    if (!ms) return () => { alive.current = false; };
    const id = setInterval(() => { if (!document.hidden) load(); }, ms);
    return () => { alive.current = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, ms, ...deps]);
  return { data, error, reload: load };
}

/** Re-render every `ms` (for countdowns and "x seconds ago"). Returns now in seconds. */
export function useNow(ms = 1000) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

/** True while the viewport matches `query` (e.g. "(max-width: 640px)" for phones). */
export function useMedia(query: string) {
  const [on, setOn] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const f = () => setOn(mq.matches);
    f();
    mq.addEventListener("change", f);
    return () => mq.removeEventListener("change", f);
  }, [query]);
  return on;
}

/** Persist a small UI preference in localStorage (never trading state). */
export function usePref<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const s = localStorage.getItem("wl:" + key);
      return s === null ? initial : (JSON.parse(s) as T);
    } catch {
      return initial;
    }
  });
  const set = useCallback((x: T) => {
    setV(x);
    try { localStorage.setItem("wl:" + key, JSON.stringify(x)); } catch { /* private mode */ }
  }, [key]);
  return [v, set];
}
