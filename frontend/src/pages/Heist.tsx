import { useEffect, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type HeistConfig } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";

const NUM_FIELDS: { key: keyof HeistConfig; label: string; hint: string }[] = [
  { key: "start_cooldown", label: "Start-Cooldown (s)", hint: "pro Account zwischen zwei !heist (Bot-Limit, i.d.R. 3600)" },
  { key: "spacing_min", label: "Spacing min (s)", hint: "min. Abstand zwischen zwei Openern" },
  { key: "spacing_max", label: "Spacing max (s)", hint: "max. Abstand zwischen zwei Openern" },
  { key: "join_delay_ms", label: "Join-Delay (ms)", hint: "Wartezeit vor !join nach Bot-Bestätigung" },
];

export default function Heist() {
  const [cfg, setCfg] = useState<HeistConfig | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const { data: loaded } = useQuery({ queryKey: ["heist-config"], queryFn: api.getHeistConfig });
  const { data: status } = useQuery({
    queryKey: ["heist-status"],
    queryFn: api.getHeistStatus,
    refetchInterval: 4000,
  });

  useEffect(() => {
    if (loaded && !cfg) setCfg(loaded);
  }, [loaded, cfg]);

  if (!cfg) return <div className="text-zinc-400">Lädt…</div>;

  const set = <K extends keyof HeistConfig>(k: K, v: HeistConfig[K]) =>
    setCfg({ ...cfg, [k]: v });

  const save = async () => {
    setSaving(true);
    try {
      const saved = await api.putHeistConfig(cfg);
      setCfg(saved);
      setMsg("✅ Gespeichert");
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const rt = status?.runtime;
  const onlineLabel = rt?.online === true ? "🟢 online" : rt?.online === false ? "⚪ offline" : "❓ unbekannt";

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Heist</h1>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={cfg.enabled} onChange={(e) => set("enabled", e.target.checked)} />
          Modul aktiviert
        </label>
      </div>

      {/* Live status */}
      <Card className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div><div className="text-zinc-500">Streamer</div><div>{onlineLabel}</div></div>
        <div><div className="text-zinc-500">Beobachter/Joiner</div>
          <div>{rt?.observer_connected ? `🟢 ${rt.observer_username}` : "—"}</div></div>
        <div><div className="text-zinc-500">Heist aktiv</div><div>{rt?.heist_active ? "🚨 ja" : "nein"}</div></div>
        <div><div className="text-zinc-500">Nächster Opener in</div>
          <div>{rt ? `${Math.round(rt.next_open_in)}s` : "—"}</div></div>
      </Card>

      {/* Roles overview */}
      <Card className="space-y-2 text-sm">
        <div className="font-semibold">Rollen (in „Accounts" umschaltbar)</div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <div className="text-zinc-500">Opener (!heist)</div>
            {status?.openers.length ? status.openers.map((o) => (
              <div key={o.id} className="flex items-center justify-between gap-2">
                <span>{o.username}{!o.logged_in && <span className="text-amber-400"> (kein Login)</span>}</span>
                <Button size="sm" variant="ghost" onClick={async () => {
                  setMsg(`⏳ Teste !heist mit ${o.username}…`);
                  try { const r = await api.heistTest(o.id); setMsg(r.ok ? `✅ ${o.username}: gesendet` : `❌ ${o.username}: fehlgeschlagen`); }
                  catch (e) { setMsg(`❌ ${(e as Error).message}`); }
                }}>Test</Button>
              </div>
            )) : <div className="text-zinc-600">keine</div>}
          </div>
          <div>
            <div className="text-zinc-500">Joiner (!join)</div>
            {status?.joiners.length ? status.joiners.map((j) => (
              <div key={j.id}>{j.username}{!j.logged_in && <span className="text-amber-400"> (kein Login)</span>}</div>
            )) : <div className="text-zinc-600">keine</div>}
          </div>
        </div>
      </Card>

      {/* Config */}
      <Card className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Streamer-Channel" hint="z.B. j4nkttv">
            <Input value={cfg.channel} onChange={(e) => set("channel", e.target.value)} placeholder="j4nkttv" />
          </Field>
          <Field label="Bot-Name" hint="z.B. j4nkbot">
            <Input value={cfg.bot} onChange={(e) => set("bot", e.target.value)} placeholder="j4nkbot" />
          </Field>
          <Field label="Start-Befehl" hint="öffnet einen Heist">
            <Input value={cfg.start_command} onChange={(e) => set("start_command", e.target.value)} placeholder="!heist" />
          </Field>
          <Field label="Join-Befehl" hint="tritt einem Heist bei">
            <Input value={cfg.join_command} onChange={(e) => set("join_command", e.target.value)} placeholder="!join" />
          </Field>
          <Field label="Trigger-Regex" hint="erkennt eine OFFENE Heist; leer = eingebauter Default">
            <Input value={cfg.trigger_regex} onChange={(e) => set("trigger_regex", e.target.value)} placeholder="Heist on .+spots left" />
          </Field>
          <Field label="End-Regex" hint="erkennt das ENDE eines Heists; leer = eingebauter Default">
            <Input value={cfg.end_regex} onChange={(e) => set("end_regex", e.target.value)} placeholder="took .+ from the !heist|No loot" />
          </Field>
          {NUM_FIELDS.map((f) => (
            <Field key={f.key} label={f.label} hint={f.hint}>
              <Input
                type="number"
                value={String(cfg[f.key] as number)}
                onChange={(e) => set(f.key, Number(e.target.value) as HeistConfig[typeof f.key])}
              />
            </Field>
          ))}
        </div>
        <Button onClick={save} disabled={saving}>Speichern</Button>
      </Card>

      {msg && (
        <div className="rounded-md border border-zinc-700 bg-zinc-900 p-3 text-sm" onClick={() => setMsg(null)}>
          {msg} <span className="text-zinc-500">(klicken zum Schließen)</span>
        </div>
      )}
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <div className="text-sm font-medium">{label}</div>
      {children}
      {hint && <div className="text-xs text-zinc-500">{hint}</div>}
    </label>
  );
}
