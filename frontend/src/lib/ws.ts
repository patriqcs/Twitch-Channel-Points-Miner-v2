import { useEffect, useRef, useState } from "react";

function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${path}`;
}

/**
 * Shared auto-reconnecting WebSocket effect. Delivers raw string messages and
 * fires `onOpen` on every (re)connect. Both public hooks below wrap this so a
 * reconnect-policy change only has to be made once.
 */
function useRawWs(
  path: string | null,
  onMessage: (raw: string) => void,
  onOpen?: () => void,
) {
  const cbRef = useRef(onMessage);
  cbRef.current = onMessage;
  const openRef = useRef(onOpen);
  openRef.current = onOpen;

  useEffect(() => {
    if (!path) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;

    const connect = () => {
      ws = new WebSocket(wsUrl(path));
      ws.onopen = () => openRef.current?.();
      ws.onmessage = (e) => cbRef.current(e.data as string);
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

/** Generic auto-reconnecting JSON WebSocket hook. */
export function useJsonWs<T>(path: string, onMessage: (data: T) => void) {
  const cbRef = useRef(onMessage);
  cbRef.current = onMessage;
  useRawWs(path, (raw) => {
    try {
      cbRef.current(JSON.parse(raw));
    } catch {
      /* ignore */
    }
  });
}

/** Tail a log file over WebSocket, keeping the last `max` lines. */
export function useLogTail(username: string | null, max = 500): string[] {
  const [lines, setLines] = useState<string[]>([]);
  const path = username ? `/ws/logs/${username}` : null;

  // Clear immediately when the selected account changes (or is cleared).
  useEffect(() => {
    setLines([]);
  }, [username]);

  useRawWs(
    path,
    (raw) =>
      setLines((prev) => {
        const next = [...prev, raw];
        return next.length > max ? next.slice(next.length - max) : next;
      }),
    // The backend re-sends the last ~200 lines on every new connection, so reset
    // on each (re)connect; otherwise a reconnect duplicates the whole tail block.
    () => setLines([]),
  );

  return lines;
}
