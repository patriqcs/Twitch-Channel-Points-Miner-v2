import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { api } from "@/lib/api";
import { Button, Card, Input, Textarea } from "@/components/ui";

export default function Settings() {
  const { data } = useQuery({ queryKey: ["streamers"], queryFn: api.getStreamers });
  const [raw, setRaw] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (data) setRaw(data.raw);
  }, [data]);

  const count = raw
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#")).length;

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-bold">Einstellungen</h1>

      <Card className="space-y-3">
        <div>
          <div className="font-semibold">Streamer-Liste (Farm)</div>
          <div className="text-sm text-zinc-400">
            Wird von <b>allen</b> Accounts gemeinsam geschaut und steuert das
            Stream-Gate (Accounts laufen nur, wenn hier ein Streamer live ist).
            Ein Streamer pro Zeile, <code className="px-1">#</code> für Kommentare.
            ({count} aktiv)
          </div>
        </div>
        <Textarea
          rows={8}
          value={raw}
          onChange={(e) => { setRaw(e.target.value); setSaved(false); }}
          placeholder={"streamer1\nstreamer2\nstreamer3"}
        />
        <div className="flex items-center gap-3">
          <Button
            onClick={async () => {
              await api.putStreamers(raw);
              setSaved(true);
            }}
          >
            <Save size={15} /> Speichern
          </Button>
          {saved && <span className="text-sm text-emerald-400">✅ Gespeichert</span>}
          <span className="text-xs text-zinc-500">
            Änderungen greifen beim nächsten Account-Neustart.
          </span>
        </div>
      </Card>

      <CoverCard />
    </div>
  );
}

function CoverCard() {
  const { data } = useQuery({ queryKey: ["cover"], queryFn: api.getCover });
  const [enabled, setEnabled] = useState(true);
  const [raw, setRaw] = useState("");
  const [count, setCount] = useState(3);
  const [maxCount, setMaxCount] = useState(8);
  const [offlinePresence, setOfflinePresence] = useState(2);
  const [offlineHours, setOfflineHours] = useState(3);
  const [maxOfflinePresence, setMaxOfflinePresence] = useState(5);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    if (data) {
      setEnabled(data.enabled);
      setRaw(data.raw);
      setCount(data.count);
      setMaxCount(data.max_count);
      setOfflinePresence(data.offline_presence);
      setOfflineHours(data.offline_hours);
      setMaxOfflinePresence(data.max_offline_presence);
    }
  }, [data]);

  const poolCount = raw.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#")).length;

  const save = async () => {
    try {
      const r = await api.putCover({
        enabled, raw, count,
        offline_presence: offlinePresence, offline_hours: offlineHours,
      });
      setRaw(r.raw);
      setCount(r.count);
      setOfflinePresence(r.offline_presence);
      setOfflineHours(r.offline_hours);
      setMsg("✅ Gespeichert");
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  return (
    <Card className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="font-semibold">Tarn-Kanäle (Anti-Bot)</div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          aktiviert
        </label>
      </div>
      <div className="text-sm text-zinc-400">
        Jeder Account beobachtet zusätzlich zu den Farm-Streamern eine <b>stabile,
        pro Account unterschiedliche</b> Auswahl aus diesem Pool großer deutscher
        Kanäle. Der Miner folgt und abonniert sie automatisch — so wirkt kein
        Account wie ein „schaut nur einen Kanal"-Bot. Das Stream-Gate bleibt
        unberührt (Accounts laufen weiter nur bei Farm-Streamer-Live), die
        Tarn-Kanäle diversifizieren nur innerhalb dieser Zeiten. ({poolCount} im Pool)
      </div>
      <div className="flex flex-wrap items-start gap-5">
        <label className="block space-y-1">
          <div className="text-sm font-medium">Kanäle pro Account</div>
          <Input
            type="number"
            min={0}
            max={maxCount}
            value={String(count)}
            onChange={(e) => setCount(Number(e.target.value))}
            className="w-24"
          />
          <div className="text-xs text-zinc-500">0–{maxCount} (Empfehlung 2–4)</div>
        </label>
        <label className="block space-y-1">
          <div className="text-sm font-medium">Offline-Präsenz (Accounts)</div>
          <Input
            type="number"
            min={0}
            max={maxOfflinePresence}
            value={String(offlinePresence)}
            onChange={(e) => setOfflinePresence(Number(e.target.value))}
            className="w-24"
          />
          <div className="text-xs text-zinc-500">0 = aus · 0–{maxOfflinePresence}</div>
        </label>
        <label className="block space-y-1">
          <div className="text-sm font-medium">Offline-Fenster (Std.)</div>
          <Input
            type="number"
            min={0}
            step={0.5}
            value={String(offlineHours)}
            onChange={(e) => setOfflineHours(Number(e.target.value))}
            className="w-24"
          />
          <div className="text-xs text-zinc-500">randomisiert ~½–voll</div>
        </label>
      </div>
      <div className="text-xs text-zinc-500">
        <b>Offline-Präsenz:</b> Wenn kein Farm-Streamer live ist, bleiben so viele
        Accounts (rotierend, nur etablierte) noch für ein zufälliges Fenster von
        ca. der Hälfte bis zur vollen eingestellten Stundenzahl online und schauen
        die Tarn-Kanäle — danach gehen auch die letzten aus. So wirken die Accounts
        wie echte Nutzer, die nach dem Stream noch etwas gucken (nicht 24/7).
      </div>
      <Textarea
        rows={8}
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder={"montanablack88\ntrymacs\npapaplatte"}
      />
      <div className="flex items-center gap-3">
        <Button variant="outline" onClick={save}>
          <Save size={15} /> Speichern
        </Button>
        {msg && (
          <span className="text-sm" onClick={() => setMsg(null)}>{msg}</span>
        )}
        <span className="text-xs text-zinc-500">
          Greift beim nächsten Account-Neustart.
        </span>
      </div>
    </Card>
  );
}
