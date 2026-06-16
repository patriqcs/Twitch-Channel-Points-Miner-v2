import { useEffect, useRef, useState } from "react";

function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${path}`;
}

/** Generic auto-reconnecting JSON WebSocket hook. */
export function useJsonWs<T>(path: string, onMessage: (data: T) => void) {
  const cbRef = useRef(onMessage);
  cbRef.current = onMessage;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;

    const connect = () => {
      ws = new WebSocket(wsUrl(path));
      ws.onmessage = (e) => {
        try {
          cbRef.current(JSON.parse(e.data));
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        if (!closed) retry = setTimeout(connect, 2000);
      };
    };
    connect();

    return () => {
      closed = true;
      clearTimeout(retry);
      ws?.close();
    };
  }, [path]);
}

/** Tail a log file over WebSocket, keeping the last `max` lines. */
export function useLogTail(username: string | null, max = 500): string[] {
  const [lines, setLines] = useState<string[]>([]);

  useEffect(() => {
    setLines([]);
    if (!username) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;

    const connect = () => {
      ws = new WebSocket(wsUrl(`/ws/logs/${username}`));
      ws.onmessage = (e) =>
        setLines((prev) => {
          const next = [...prev, e.data as string];
          return next.length > max ? next.slice(next.length - max) : next;
        });
      ws.onclose = () => {
        if (!closed) retry = setTimeout(connect, 2000);
      };
    };
    connect();

    return () => {
      closed = true;
      clearTimeout(retry);
      ws?.close();
    };
  }, [username, max]);

  return lines;
}
