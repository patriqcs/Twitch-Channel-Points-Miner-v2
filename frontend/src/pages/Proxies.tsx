import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Wifi } from "lucide-react";
import { api, type ProxyTestResult } from "@/lib/api";
import { Button, Card, Input, Modal } from "@/components/ui";

const EMPTY = { name: "", scheme: "socks5", host: "", port: "", username: "", password: "" };

export default function Proxies() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["proxies"] });
  const { data: proxies = [] } = useQuery({ queryKey: ["proxies"], queryFn: api.listProxies });

  const [showAdd, setShowAdd] = useState(false);
  const [form, setForm] = useState({ ...EMPTY });
  const [tests, setTests] = useState<Record<number, ProxyTestResult | "loading">>({});
  const [err, setErr] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      api.createProxy({
        name: form.name.trim(),
        scheme: form.scheme,
        host: form.host.trim(),
        port: Number(form.port),
        username: form.username.trim() || null,
        password: form.password || null,
      }),
    onSuccess: () => { setShowAdd(false); setForm({ ...EMPTY }); invalidate(); },
    onError: (e: Error) => setErr(e.message),
  });

  const runTest = async (id: number) => {
    setTests((t) => ({ ...t, [id]: "loading" }));
    try {
      const r = await api.testProxy(id);
      setTests((t) => ({ ...t, [id]: r }));
    } catch (e) {
      setTests((t) => ({ ...t, [id]: { ok: false, ip: null, latency_ms: null, error: (e as Error).message } }));
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Proxys</h1>
        <Button onClick={() => setShowAdd(true)}><Plus size={15} /> Proxy</Button>
      </div>

      <div className="space-y-3">
        {proxies.map((p) => {
          const t = tests[p.id];
          return (
            <Card key={p.id} className="flex flex-wrap items-center gap-3">
              <div className="min-w-[140px] flex-1">
                <div className="font-semibold">{p.name}</div>
                <div className="text-xs text-zinc-500">
                  {p.scheme}://{p.username ? `${p.username}@` : ""}{p.host}:{p.port}
                </div>
              </div>
              <div className="text-sm text-zinc-400">{p.account_count}/5 Accounts</div>
              <div className="min-w-[150px] text-xs">
                {t === "loading" && <span className="text-amber-400">teste…</span>}
                {t && t !== "loading" && t.ok && (
                  <span className="text-emerald-400">✅ {t.ip} · {t.latency_ms} ms</span>
                )}
                {t && t !== "loading" && !t.ok && (
                  <span className="text-red-400">❌ {t.error}</span>
                )}
              </div>
              <div className="flex gap-1.5">
                <Button size="sm" variant="outline" onClick={() => runTest(p.id)}>
                  <Wifi size={14} /> Test
                </Button>
                <Button size="sm" variant="danger"
                  onClick={async () => {
                    try {
                      if (confirm(`Proxy ${p.name} löschen?`)) { await api.deleteProxy(p.id); invalidate(); }
                    } catch (e) { setErr((e as Error).message); }
                  }}>
                  <Trash2 size={14} />
                </Button>
              </div>
            </Card>
          );
        })}
        {proxies.length === 0 && <Card className="text-zinc-400">Noch keine Proxys.</Card>}
      </div>

      {err && (
        <div className="rounded-md border border-red-700/40 bg-red-900/20 p-3 text-sm text-red-300"
          onClick={() => setErr(null)}>
          {err}
        </div>
      )}

      <Modal open={showAdd} onClose={() => setShowAdd(false)} title="Proxy hinzufügen">
        <div className="space-y-3">
          <Input placeholder="Name (z. B. Proxy DE 1)" value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <div className="flex gap-2">
            <select className="h-9 rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
              value={form.scheme} onChange={(e) => setForm({ ...form, scheme: e.target.value })}>
              {["socks5", "socks5h", "socks4", "http", "https"].map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <Input placeholder="Host" value={form.host}
              onChange={(e) => setForm({ ...form, host: e.target.value })} />
            <Input className="w-24" placeholder="Port" value={form.port}
              onChange={(e) => setForm({ ...form, port: e.target.value })} />
          </div>
          <div className="flex gap-2">
            <Input placeholder="User (optional)" value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })} />
            <Input placeholder="Passwort (optional)" type="password" value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </div>
          <Button className="w-full"
            disabled={!form.name.trim() || !form.host.trim() || !form.port || create.isPending}
            onClick={() => create.mutate()}>
            Anlegen
          </Button>
        </div>
      </Modal>
    </div>
  );
}
