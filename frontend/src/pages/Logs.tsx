import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useLogTail } from "@/lib/ws";
import { Card } from "@/components/ui";

export default function Logs() {
  const { data: accounts = [] } = useQuery({ queryKey: ["accounts"], queryFn: api.listAccounts });
  const [selected, setSelected] = useState<string | null>(null);
  const lines = useLogTail(selected);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!selected && accounts.length) setSelected(accounts[0].username);
  }, [accounts, selected]);

  useEffect(() => {
    boxRef.current?.scrollTo(0, boxRef.current.scrollHeight);
  }, [lines]);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-bold">Logs</h1>
        <select
          className="h-9 rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
          value={selected ?? ""}
          onChange={(e) => setSelected(e.target.value)}
        >
          {accounts.map((a) => (
            <option key={a.id} value={a.username}>{a.username}</option>
          ))}
        </select>
      </div>

      <Card className="p-0">
        <div
          ref={boxRef}
          className="h-[70vh] overflow-auto p-3 font-mono text-xs leading-relaxed"
        >
          {lines.length === 0 ? (
            <div className="text-zinc-500">
              {selected ? "Warte auf Log-Ausgabe…" : "Kein Account ausgewählt."}
            </div>
          ) : (
            lines.map((l, i) => (
              <div key={i} className="whitespace-pre-wrap break-all text-zinc-300">
                {l}
              </div>
            ))
          )}
        </div>
      </Card>
    </div>
  );
}
