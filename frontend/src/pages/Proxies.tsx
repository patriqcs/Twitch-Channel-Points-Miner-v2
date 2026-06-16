import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Upload, Wifi } from "lucide-react";
import { api, type ProxyImportResult, type ProxyTestResult } from "@/lib/api";
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

  const [showImport, setShowImport] = useState(false);
  const [importText, setImportText] = useState("");
  const [testOnImport, setTestOnImport] = useState(true);
  const [importResult, setImportResult] = useState<ProxyImportResult | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const importMut = useMutation({
    mutationFn: () => api.importProxies(importText, testOnImport),
    onSuccess: (r) => { setImportResult(r); setImportText(""); invalidate(); },
    onError: (e: Error) => setErr(e.message),
  });

  const onPickFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const text = await file.text();
    setImportText((prev) => (prev ? `${prev}\n${text}` : text));
    if (fileInput.current) fileInput.current.value = "";
  };

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

  const [testingAll, setTestingAll] = useState(false);
  const testAll = async () => {
    if (proxies.length === 0) return;
    setTestingAll(true);
    setTests(Object.fromEntries(proxies.map((p) => [p.id, "loading" as const])));
    try {
      const results = await api.testAllProxies();
      setTests(Object.fromEntries(results.map((r) => [r.id, r])));
    } catch (e) {
      setErr((e as Error).message);
      setTests({});
    } finally {
      setTestingAll(false);
    }
  };

  // ids of proxies whose last test failed
  const brokenIds = proxies
    .filter((p) => { const t = tests[p.id]; return t && t !== "loading" && !t.ok; })
    .map((p) => p.id);

  const deleteBroken = async () => {
    if (brokenIds.length === 0) return;
    if (!confirm(`${brokenIds.length} kaputte Proxys löschen?`)) return;
    try {
      const r = await api.bulkDeleteProxies(brokenIds);
      setErr(
        r.skipped_in_use
          ? `${r.deleted} gelöscht, ${r.skipped_in_use} übersprungen (einem Account zugewiesen).`
          : null
      );
      setTests((t) => {
        const next = { ...t };
        brokenIds.forEach((id) => delete next[id]);
        return next;
      });
      invalidate();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Proxys</h1>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={testingAll || proxies.length === 0} onClick={testAll}>
            <Wifi size={15} /> {testingAll ? "teste…" : "Alle testen"}
          </Button>
          <Button variant="danger" disabled={brokenIds.length === 0} onClick={deleteBroken}>
            <Trash2 size={15} /> Kaputte löschen{brokenIds.length ? ` (${brokenIds.length})` : ""}
          </Button>
          <Button variant="outline" onClick={() => { setImportResult(null); setShowImport(true); }}>
            <Upload size={15} /> Import .txt
          </Button>
          <Button onClick={() => setShowAdd(true)}><Plus size={15} /> Proxy</Button>
        </div>
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

      <Modal open={showImport} onClose={() => setShowImport(false)} title="Proxys importieren (.txt)">
        <div className="space-y-3">
          <p className="text-xs text-zinc-400">
            Eine Zeile pro Proxy, Format <code>scheme://host:port</code> (z. B.{" "}
            <code>socks5://1.2.3.4:1080</code> oder <code>http://user:pass@1.2.3.4:8080</code>).
            Leerzeilen und <code>#</code>-Kommentare werden ignoriert; Duplikate übersprungen.
          </p>
          <textarea
            className="h-44 w-full rounded-md border border-zinc-700 bg-zinc-950 p-2 font-mono text-xs"
            placeholder={"socks5://1.2.3.4:1080\nhttp://5.6.7.8:8080"}
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
          />
          <label className="flex items-center gap-2 text-sm text-zinc-300">
            <input type="checkbox" checked={testOnImport}
              onChange={(e) => setTestOnImport(e.target.checked)} />
            Vor dem Hinzufügen testen – nur funktionierende übernehmen
          </label>
          <div className="flex items-center gap-2">
            <input ref={fileInput} type="file" accept=".txt,text/plain"
              className="hidden" onChange={onPickFile} />
            <Button size="sm" variant="outline" onClick={() => fileInput.current?.click()}>
              <Upload size={14} /> Datei wählen
            </Button>
            <Button className="ml-auto"
              disabled={!importText.trim() || importMut.isPending}
              onClick={() => importMut.mutate()}>
              {importMut.isPending ? (testOnImport ? "teste & importiere…" : "importiere…") : "Importieren"}
            </Button>
          </div>

          {importResult && (
            <div className="space-y-2 rounded-md border border-zinc-700 bg-zinc-900/50 p-3 text-sm">
              <div>
                <span className="text-emerald-400">✅ {importResult.added} hinzugefügt</span>
                {" · "}
                <span className={importResult.skipped_offline ? "text-amber-400" : "text-zinc-400"}>
                  {importResult.skipped_offline} offline
                </span>
                {" · "}
                <span className="text-zinc-400">{importResult.skipped_duplicate} Duplikate</span>
                {" · "}
                <span className={importResult.failed ? "text-red-400" : "text-zinc-400"}>
                  {importResult.failed} fehlerhaft
                </span>
              </div>
              {importResult.errors.length > 0 && (
                <ul className="max-h-28 space-y-0.5 overflow-y-auto text-xs text-red-300">
                  {importResult.errors.map((e) => (
                    <li key={e.line}>Zeile {e.line}: {e.value} — {e.error}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </Modal>
    </div>
  );
}
