import { useEffect, useState } from "react";
import { DATA_UPDATED, getJSON } from "../api";

// Fetches `path`; refetches silently when the live site publishes new data.
export function useApi<T>(path: string): { data?: T; error?: string } {
  const [state, setState] = useState<{ data?: T; error?: string }>({});
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const on = () => setTick((x) => x + 1);
    window.addEventListener(DATA_UPDATED, on);
    return () => window.removeEventListener(DATA_UPDATED, on);
  }, []);
  useEffect(() => {
    let alive = true;
    if (tick === 0) setState({});
    getJSON<T>(path).then((data) => alive && setState({ data }))
      .catch((e: Error) => alive && setState((s) => (s.data ? s : { error: e.message })));
    return () => { alive = false; };
  }, [path, tick]);
  return state;
}
