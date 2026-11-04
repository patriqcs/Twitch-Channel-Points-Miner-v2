import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type QuestConfig, type QuestLane, type QuestStatus } from "@/lib/api";
import { Button, Card, Input, Textarea } from "@/components/ui";
import { cn } from "@/lib/utils";

// Evolutionslinien (Twitch-setIDs). Stufe 0 = Starter-Badge (nur Besitz), danach Lanes.
const LINES: { key: string; title: string; stages: string[] }[] = [
  { key: "charmander", title: "Glumanda-Linie", stages: ["charmander", "charmeleon", "charizard"] },
  { key: "bulbasaur", title: "Bisasam-Linie", stages: ["bulbasaur", "ivysaur", "venusaur"] },
  { key: "squirtle", title: "Shiggy-Linie", stages: ["squirtle", "wartortle", "blastoise"] },
  { key: "pichu", title: "Pichu-Linie", stages: ["pichu", "pikachu"] },
  { key: "gastly", title: "Nebulak-Linie", stages: ["gastly", "haunter", "gengar"] },
];

const DE: Record<string, string> = {
  pichu: "Pichu", pikachu: "Pikachu", bulbasaur: "Bisasam", ivysaur: "Bisaknosp", venusaur: "Bisaflor",
  charmander: "Glumanda", charmeleon: "Glutexo", charizard: "Glurak", squirtle: "Shiggy",
  wartortle: "Schillok", blastoise: "Turtok", gastly: "Nebulak", haunter: "Alpollo", gengar: "Gengar",
};

function fmtTime(iso: string | null | undefined) {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function fmtIn(iso: string | null | undefined, now: number) {
  if (!iso) return "";
  const diff = Math.max(0, (new Date(iso).getTime() - now) / 1000);
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  return h > 0 ? `in ${h} h ${m} min` : `in ${m} min`;
}

function laneState(l: QuestLane, watching: boolean, now: number) {
  if (l.done) return { label: "freigeschaltet", tone: "text-emerald-400", icon: "✅" };
  if (!l.preconditions_met) return { label: "noch gesperrt", tone: "text-zinc-500", icon: "🔒" };
  if (l.blocked_until) return { label: `pausiert ${fmtIn(l.blocked_until, now)}`, tone: "text-amber-400", icon: "⏸" };
  if (watching) return { label: "läuft gerade", tone: "text-sky-400", icon: "👀" };
  if (l.day_complete) return { label: `heute voll · weiter ${fmtIn(l.ready_at, now)}`, tone: "text-amber-300", icon: "🌙" };
  if (l.selected) return { label: "ausgewählt", tone: "text-sky-300", icon: "🎯" };
  return { label: "wartet", tone: "text-zinc-400", icon: "·" };
}

function Stage({
  setId, image, lane, owned, watching, now, onSelect,
}: {
  setId: string; image: string | undefined; lane: QuestLane | undefined; owned: boolean;
  watching: boolean; now: number; onSelect: (lane: string) => void;
}) {
  const name = DE[setId] ?? setId;
  const st = lane ? laneState(lane, watching, now) : owned
    ? { label: "im Besitz", tone: "text-emerald-400", icon: "✅" }
    : { label: "nicht verfügbar", tone: "text-zinc-600", icon: "–" };
  const active = lane && !lane.done && (lane.selected || watching);
  const minutesPct = lane ? Math.min(100, Math.round((lane.minutes / Math.max(1, lane.required_minutes)) * 100)) : 0;
  return (
    <div
      className={cn(
        "flex w-44 shrink-0 flex-col items-center rounded-xl border p-3 text-center",
        active ? "border-sky-500/60 bg-sky-500/5" : "border-zinc-800 bg-zinc-950/40",
        !owned && !lane && "opacity-40"
      )}
    >
      <div className="relative h-20 w-20">
        {image ? (
          <img
            src={image}
            alt={name}
            className={cn("h-20 w-20 rounded-lg object-contain", !owned && !lane?.done && "grayscale")}
            loading="lazy"
          />
        ) : (
          <div className="flex h-20 w-20 items-center justify-center rounded-lg bg-zinc-800 text-3xl">❔</div>
        )}
        {watching && <span className="absolute -right-1 -top-1 h-3 w-3 animate-pulse rounded-full bg-sky-400" />}
      </div>
      <div className="mt-2 font-medium">{name}</div>
      <div className={cn("text-xs", st.tone)}>
        {st.icon} {st.label}
      </div>
      {lane && !lane.done && (
        <>
          <div className="mt-2 flex gap-1" title={`Tage: ${lane.grants}/${lane.repeat_times}`}>
            {Array.from({ length: lane.repeat_times }).map((_, i) => (
              <span
                key={i}
                className={cn(
                  "h-2.5 w-2.5 rounded-full",
                  i < lane.grants ? "bg-emerald-400" : i === lane.grants && lane.minutes > 0 ? "bg-sky-400" : "bg-zinc-700"
                )}
              />
            ))}
          </div>
          <div className="mt-1 text-[11px] text-zinc-400">
            Tag {Math.min(lane.grants + 1, lane.repeat_times)}/{lane.repeat_times} · {lane.minutes}/{lane.required_minutes} min
          </div>
          <div className="mt-1 h-1.5 w-full overflow-hidden rounded bg-zinc-800">
            <div className="h-full bg-sky-500" style={{ width: `${minutesPct}%` }} />
          </div>
          {!lane.selected && lane.preconditions_met && (
            <Button className="mt-2 h-7 px-2 text-xs" onClick={() => onSelect(lane.lane)}>
              Auswählen
            </Button>
          )}
        </>
      )}
    </div>
  );
}

export default function Quests() {
  const qc = useQueryClient();
  const [cfg, setCfg] = useState<QuestConfig | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const { data: loaded } = useQuery({ queryKey: ["quest-config"], queryFn: api.getQuestConfig });
  const { data: status } = useQuery<QuestStatus>({
    queryKey: ["quest-status"],
    queryFn: api.getQuestStatus,
    refetchInterval: 5000,
  });

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (loaded && !cfg) setCfg(loaded);
  }, [loaded, cfg]);

  if (!cfg) return <div className="text-zinc-400">Lädt…</div>;

  const set = <K extends keyof QuestConfig>(k: K, v: QuestConfig[K]) => setCfg({ ...cfg, [k]: v });

  const save = async () => {
    setSaving(true);
    try {
      const saved = await api.putQuestConfig({
        account: cfg.account,
        priority_raw: cfg.priority_raw,
        pool_raw: cfg.pool_raw,
        max_minutes: cfg.max_minutes,
        stall_minutes: cfg.stall_minutes,
      });
      setCfg(saved);
      setMsg("✅ Gespeichert");
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const toggleEnabled = async (enabled: boolean) => {
    const prev = cfg;
    setCfg({ ...cfg, enabled });
    try {
      const saved = await api.putQuestConfig({ enabled });
      setCfg(saved);
      qc.invalidateQueries({ queryKey: ["quest-status"] });
      setMsg(enabled ? "✅ Quests aktiviert" : "🛑 Quests deaktiviert");
    } catch (e) {
      setCfg(prev);
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const select = async (lane: string) => {
    try {
      await api.questSelect(lane);
      setMsg("✅ Lane gewechselt");
      qc.invalidateQueries({ queryKey: ["quest-status"] });
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const lanes = status?.lanes ?? [];
  const badges = status?.badges ?? [];
  const badgeBySet = new Map(badges.map((b) => [b.set_id, b]));
  const laneBySet = new Map(lanes.map((l) => [l.badge_set || l.name.toLowerCase(), l]));
  const watch = status?.watch ?? null;
  const d = status?.decision ?? null;
  const openLanes = lanes.filter((l) => !l.done && l.preconditions_met);
  const doneLanes = lanes.filter((l) => l.done);

  let headline = "Modul aus";
  if (cfg.enabled) {
    if (watch) headline = `👀 schaut ${watch.channel} (${watch.game ?? "?"}) für ${watch.lane_name_de} · ${watch.minutes_logged ?? watch.beacons} min gemeldet · ${Math.round((watch.bytes_kb ?? 0) / 1024)} MB`;
    else if (d?.action === "idle") headline = `🌙 Pause – alle Lanes für heute voll, weiter ${fmtIn(d.resume_at, now)} (${fmtTime(d.resume_at)})`;
    else if (d?.action === "done") headline = "🏁 Alles freigeschaltet – nichts mehr zu tun";
    else if (d?.action === "watch") headline = "🔎 suche Live-Kanal in einer zählenden Kategorie…";
    else headline = "⏳ lade Fortschritt…";
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Badges (Pokémon-Quests)</h1>
          <p className="text-sm text-zinc-400">
            20 Minuten pro Tag und Pokémon in einer zählenden Kategorie, 3 Tage je Stufe. Das Modul schaut für den
            Account einen passenden Live-Kanal, wechselt die Lane nach jedem vollen Tag und pausiert, wenn alles voll ist.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={cfg.enabled} onChange={(e) => toggleEnabled(e.target.checked)} />
          Modul aktiv
        </label>
      </div>

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm">
            <div className="font-medium">{headline}</div>
            <div className="text-xs text-zinc-400">
              Account {status?.account || cfg.account} · {openLanes.length} offene Stufen · {doneLanes.length} fertig
              {status?.last_refresh_age != null && ` · Stand vor ${Math.round(status.last_refresh_age)} s`}
            </div>
            {status?.last_error && <div className="mt-1 text-xs text-rose-400">⚠ {status.last_error}</div>}
          </div>
          <Button className="h-8 text-xs" onClick={() => api.questRefresh().then(() => qc.invalidateQueries({ queryKey: ["quest-status"] }))}>
            Jetzt aktualisieren
          </Button>
        </div>
      </Card>

      {LINES.map((line) => {
        const starterOwned = badgeBySet.has(line.stages[0]);
        const anyLane = line.stages.some((s) => laneBySet.has(s));
        if (!starterOwned && !anyLane) return null;
        return (
          <Card key={line.key}>
            <div className="mb-3 flex items-center justify-between">
              <div className="font-medium">{line.title}</div>
              <div className="text-xs text-zinc-500">
                {line.stages.filter((s) => badgeBySet.has(s)).length}/{line.stages.length} Badges
              </div>
            </div>
            <div className="flex items-center gap-2 overflow-x-auto pb-1">
              {line.stages.map((setId, i) => {
                const lane = laneBySet.get(setId);
                const owned = badgeBySet.has(setId) || !!lane?.done;
                const image = badgeBySet.get(setId)?.image || lane?.thumbnail;
                const watching = !!watch && !!lane && watch.lane === lane.lane && lane.selected;
                return (
                  <div key={setId} className="flex items-center gap-2">
                    {i > 0 && <span className="text-zinc-600">→</span>}
                    <Stage setId={setId} image={image} lane={lane} owned={owned} watching={watching} now={now} onSelect={select} />
                  </div>
                );
              })}
            </div>
          </Card>
        );
      })}

      {lanes.length === 0 && cfg.enabled && (
        <Card className="text-sm text-zinc-400">Noch kein Fortschritt geladen – Twitch-Inventar wird abgefragt…</Card>
      )}

      <Card>
        <div className="mb-3 font-medium">Einstellungen</div>
        <div className="grid gap-3 md:grid-cols-2">
          <label className="text-sm">
            <div className="mb-1 text-zinc-400">Account</div>
            <Input value={cfg.account} onChange={(e) => set("account", e.target.value)} />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="text-sm">
              <div className="mb-1 text-zinc-400">Max. Minuten je Lane/Tag</div>
              <Input type="number" value={cfg.max_minutes} onChange={(e) => set("max_minutes", Number(e.target.value))} />
            </label>
            <label className="text-sm">
              <div className="mb-1 text-zinc-400">Stillstand → Lane pausieren (min)</div>
              <Input type="number" value={cfg.stall_minutes} onChange={(e) => set("stall_minutes", Number(e.target.value))} />
            </label>
          </div>
          <label className="text-sm">
            <div className="mb-1 text-zinc-400">Reihenfolge der Pokémon (eine pro Zeile oder Komma; leer = angefangene zuerst)</div>
            <Textarea rows={4} value={cfg.priority_raw} onChange={(e) => set("priority_raw", e.target.value)} placeholder={"Glurak\nBisaflor\nTurtok\nPikachu"} />
          </label>
          <label className="text-sm">
            <div className="mb-1 text-zinc-400">Kanal-Pool (leer = Tarn-Kanal-Pool); genommen wird der erste Live-Kanal in einer zählenden Kategorie</div>
            <Textarea rows={4} value={cfg.pool_raw} onChange={(e) => set("pool_raw", e.target.value)} placeholder={"krokoboss\nmontanablack88"} />
          </label>
        </div>
        <div className="mt-3 flex items-center gap-3">
          <Button onClick={save} disabled={saving}>Speichern</Button>
          {msg && <span className="text-sm text-zinc-400">{msg}</span>}
        </div>
      </Card>

      <Card>
        <div className="mb-2 font-medium">Verlauf</div>
        <div className="max-h-64 space-y-0.5 overflow-y-auto font-mono text-xs text-zinc-400">
          {(status?.log ?? []).slice().reverse().map((l, i) => (
            <div key={i}>{l}</div>
          ))}
          {(status?.log ?? []).length === 0 && <div>–</div>}
        </div>
      </Card>
    </div>
  );
}
