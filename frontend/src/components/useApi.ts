import { useEffect, useState } from "react";
import { getJSON } from "../api";

export function useApi<T>(path: string): { data?: T; error?: string } {
  const [state, setState] = useState<{ data?: T; error?: string }>({});
  useEffect(() => {
    let alive = true;
    setState({});
    getJSON<T>(path).then((data) => alive && setState({ data }))
      .catch((e: Error) => alive && setState({ error: e.message }));
    return () => { alive = false; };
  }, [path]);
  return state;
}
