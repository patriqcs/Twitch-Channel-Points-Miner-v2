import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type PredictionConfig, type PredictionOutcome } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";

const fmt = (n: number | null | undefined) =>
  n == null ? "—" : n.toLocaleString("de-DE");

const OUTCOME_STYLES: Record<string, { border: string; selected: string; text: string }> = {
  BLUE: { border: "border-blue-500/40", selected: "border-blue-400 ring-2 ring-blue-500/50 bg-blue-500/10", text: "text-blue-300" },
  PINK: { border: "border-pink-500/40", selected: "border-pink-400 ring-2 ring-pink-500/50 bg-pink-500/10", text: "text-pink-300" },
};

const RUN_STATUS: Record<string, string> = {
  waiting: "⏳ wartet",
  betting: "🎲 setzt…",
  ok: "✅ gesetzt",
  skipped: "⏭️ übersprungen",
  failed: "❌ fehlgeschlagen",
  tos_blocked: "🔒 AGB nötig",
};

export default function Predictions() {
  const qc = useQueryClient();
  const [cfg, setCfg] = useState<PredictionConfig | null>(null);
  const [channel, setChannel] = useState("");
  const [armed, setArmed] = useState(false); // erst nach "Laden" wird gepollt
  const [selected, setSelected] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const { data: loadedCfg } = useQuery({
    queryKey: ["prediction-config"],
    queryFn: api.getPredictionConfig,
  });
  useEffect(() => {
    if (loadedCfg && !cfg) {
      setCfg(loadedCfg);
      if (!channel) setChannel(loadedCfg.channel);
    }
  }, [loadedCfg, cfg, channel]);

  const activeQ = useQuery({
    queryKey: ["prediction-active", channel],
    queryFn: () => api.getActivePrediction(channel),
    enabled: armed && !!channel.trim(),
    refetchInterval: 7000,
    retry: false,
  });
  const balancesQ = useQuery({
    queryKey: ["prediction-balances", channel],
    queryFn: () => api.getPredictionBalances(channel),
    enabled: armed && !!channel.trim(),
    staleTime: Infinity, // Punktestände nur auf Knopfdruck aktualisieren (N GQL-Calls)
    retry: false,
  });
  const runQ = useQuery({
    queryKey: ["prediction-run"],
    queryFn: api.getPredictionRun,
    refetchInterval: (q) => (q.state.data && !q.state.data.done ? 1500 : 5000),
  });

  // 1s-Ticker für den Countdown bis zur Sperre
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const event = activeQ.data?.event ?? null;
  const locksIn = event?.locks_in != null
    ? Math.max(0, event.locks_in - (now - activeQ.dataUpdatedAt) / 1000)
    : null;
  const isOpen = event?.status === "ACTIVE" && (locksIn == null || locksIn > 3);
  const totalPool = (event?.outcomes ?? []).reduce((s, o) => s + o.total_points, 0);

  // Auswahl zurücksetzen, wenn eine andere Wette geladen wird
  useEffect(() => {
    if (event && selected && !event.outcomes.some((o) => o.id === selected)) {
      setSelected(null);
    }
  }, [event, selected]);

  const run = runQ.data ?? null;
  const runRunning = !!run && !run.done;

  const load = async () => {
    const ch = channel.trim().toLowerCase();
    if (!ch) return;
    setChannel(ch);
    setArmed(true);
    setMsg(null);
    try {
      await api.putPredictionConfig({ channel: ch }); // Kanal merken
    } catch {
      /* nur Komfort — Laden geht trotzdem */
    }
    qc.invalidateQueries({ queryKey: ["prediction-active", ch] });
    qc.invalidateQueries({ queryKey: ["prediction-balances", ch] });
  };

  const saveCfg = async () => {
    if (!cfg) return;
    try {
      const saved = await api.putPredictionConfig({
        exclude: cfg.exclude,
        spacing_min: cfg.spacing_min,
        spacing_max: cfg.spacing_max,
      });
      setCfg(saved);
      setMsg("✅ Einstellungen gespeichert");
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const placeBets = async () => {
    if (!event || !selected) return;
    const outcome = event.outcomes.find((o) => o.id === selected);
    const eligible = (balancesQ.data?.accounts ?? activeQ.data?.accounts ?? []).filter(
      (a) => a.logged_in
    );
    const total = balancesQ.data?.total_balance;
    const ok = window.confirm(
      `Wirklich mit ${eligible.length} Accounts ALLE Kanalpunkte` +
        (total != null ? ` (~${fmt(total)} gesamt)` : "") +
        ` auf „${outcome?.title}" setzen?\n\nWette: ${event.title}\nDas kann nicht rückgängig gemacht werden!`
    );
    if (!ok) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.startPredictionBet({
        channel,
        event_id: event.id,
        outcome_id: selected,
      });
      setMsg(`🎲 Runde gestartet: ${r.accounts} Accounts setzen auf „${r.outcome}"`);
      qc.invalidateQueries({ queryKey: ["prediction-run"] });
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const cancelRun = async () => {
    try {
      await api.cancelPredictionRun();
      qc.invalidateQueries({ queryKey: ["prediction-run"] });
      setMsg("🛑 Runde abgebrochen (bereits gesetzte Wetten bleiben bestehen)");
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const okCount = run?.results.filter((r) => r.status === "ok").length ?? 0;
  const runTotal = run?.results.reduce((s, r) => s + (r.points ?? 0), 0) ?? 0;
  const tosBlocked = run?.results.filter((r) => r.status === "tos_blocked") ?? [];

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Wetten</h1>
        {armed && (
          <Button variant="outline" size="sm" onClick={() => activeQ.refetch()}>
            Aktualisieren
          </Button>
        )}
      </div>

      {/* Kanal + Laden */}
      <Card className="space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Streamer-Channel" hint="Kanal mit der aktiven Kanalwette">
            <Input
              value={channel}
              onChange={(e) => setChannel(e.target.value)}
              placeholder="j4nkttv"
              onKeyDown={(e) => e.key === "Enter" && load()}
              className="w-56"
            />
          </Field>
          <Button onClick={load} disabled={!channel.trim() || activeQ.isFetching}>
            {activeQ.isFetching ? "Lädt…" : "Wette laden"}
          </Button>
        </div>
        {activeQ.error && (
          <div className="text-sm text-red-400">❌ {(activeQ.error as Error).message}</div>
        )}
      </Card>

      {/* Aktive Wette */}
      {armed && activeQ.data && (
        <Card className="space-y-4">
          {event ? (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="text-lg font-semibold">{event.title}</div>
                  <div className="text-sm text-zinc-500">
                    {activeQ.data.display_name} · Pool: {fmt(totalPool)} Punkte
                  </div>
                </div>
                <div className="text-right text-sm">
                  {event.status === "ACTIVE" ? (
                    <>
                      <div className="text-emerald-400">🟢 offen</div>
                      {locksIn != null && (
                        <div className="tabular-nums text-zinc-400">
                          sperrt in {fmtSecs(locksIn)}
                        </div>
                      )}
                    </>
                  ) : (
                    <div className="text-amber-400">🔒 {event.status}</div>
                  )}
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {event.outcomes.map((o) => (
                  <OutcomeCard
                    key={o.id}
                    outcome={o}
                    totalPool={totalPool}
                    selected={selected === o.id}
                    disabled={!isOpen || runRunning}
                    onClick={() => setSelected(selected === o.id ? null : o.id)}
                  />
                ))}
              </div>

              <div className="flex flex-wrap items-center gap-3">
                <Button
                  variant="danger"
                  disabled={!isOpen || !selected || busy || runRunning}
                  onClick={placeBets}
                  title={
                    !isOpen
                      ? "Wette ist nicht (mehr) offen"
                      : !selected
                        ? "erst ein Ergebnis auswählen"
                        : undefined
                  }
                >
                  {busy
                    ? "Starte…"
                    : `🎲 ALL-IN: alle Punkte auf „${
                        event.outcomes.find((o) => o.id === selected)?.title ?? "…"
                      }"`}
                </Button>
                <span className="text-xs text-zinc-500">
                  setzt mit allen Accounts außer: {cfg?.exclude || "—"}
                </span>
              </div>
            </>
          ) : (
            <div className="text-zinc-400">
              Keine aktive Kanalwette auf{" "}
              <span className="font-medium">{activeQ.data.display_name}</span> — wird alle
              7s neu geprüft.
            </div>
          )}
        </Card>
      )}

      {/* Punktestände */}
      {armed && (
        <Card className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="font-semibold">
              Wettberechtigte Accounts
              {balancesQ.data && (
                <span className="ml-2 text-sm font-normal text-zinc-400">
                  gesamt: {fmt(balancesQ.data.total_balance)} Punkte
                </span>
              )}
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={balancesQ.isFetching}
              onClick={() => balancesQ.refetch()}
            >
              {balancesQ.isFetching ? "Lädt…" : "Punktestände laden"}
            </Button>
          </div>
          {balancesQ.data ? (
            <div className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2 lg:grid-cols-3">
              {balancesQ.data.accounts.map((a) => (
                <div key={a.id} className="flex items-center justify-between gap-2">
                  <span>
                    {a.username}
                    {!a.logged_in && <span className="text-amber-400"> (kein Login)</span>}
                  </span>
                  <span className="tabular-nums text-zinc-300">
                    {a.balance != null ? fmt(a.balance) : (
                      <span className="text-zinc-600" title={a.error ?? undefined}>—</span>
                    )}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-sm text-zinc-500">
              {(activeQ.data?.accounts ?? []).map((a) => a.username).join(", ") ||
                "keine Accounts"}
            </div>
          )}
        </Card>
      )}

      {/* Laufende / letzte Runde */}
      {run && (
        <Card className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="font-semibold">
              {runRunning ? "🎲 Wett-Runde läuft" : "Letzte Wett-Runde"}
              <span className="ml-2 text-sm font-normal text-zinc-400">
                „{run.outcome_title}" · {run.event_title} · #{run.channel}
              </span>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-sm text-zinc-400">
                {okCount}/{run.results.length} gesetzt · {fmt(runTotal)} Punkte
              </span>
              {runRunning && (
                <Button variant="outline" size="sm" onClick={cancelRun}>
                  Abbrechen
                </Button>
              )}
            </div>
          </div>
          <div className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
            {run.results.map((r) => (
              <div key={r.account_id} className="flex items-center justify-between gap-2">
                <span>{r.username}</span>
                <span className="text-right">
                  {RUN_STATUS[r.status] ?? r.status}
                  {r.message && (
                    <span className="ml-1 text-xs text-zinc-500">{r.message}</span>
                  )}
                </span>
              </div>
            ))}
          </div>

          {tosBlocked.length > 0 && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
              <div className="font-medium text-amber-300">
                🔒 {tosBlocked.length} Account(s) müssen die Wett-AGB einmalig annehmen
              </div>
              <div className="mt-1 text-zinc-300">
                Twitch bietet dafür <b>keine API</b> — die Zustimmung geht nur einmal
                pro Account über die Website. Sobald wieder eine Wette offen ist:
                als betroffener Account auf twitch.tv einloggen, im Wett-Fenster
                irgendeinen Betrag setzen und das Häkchen „Predictions Terms" bestätigen.
                Danach wettet der Account dauerhaft automatisch mit.
              </div>
              <div className="mt-2 text-xs text-amber-200/80">
                Betroffen: {tosBlocked.map((r) => r.username).join(", ")}
              </div>
            </div>
          )}
        </Card>
      )}

      {/* Einstellungen */}
      {cfg && (
        <Card className="space-y-3">
          <div className="font-semibold">Einstellungen</div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field
              label="Ausgeschlossene Accounts"
              hint="kommasepariert — diese Accounts wetten NIE mit"
            >
              <Input
                value={cfg.exclude}
                onChange={(e) => setCfg({ ...cfg, exclude: e.target.value })}
                placeholder="patriqcs"
              />
            </Field>
            <Field label="Abstand min (s)" hint="min. Pause zwischen zwei Accounts">
              <Input
                type="number"
                value={String(cfg.spacing_min)}
                onChange={(e) => setCfg({ ...cfg, spacing_min: Number(e.target.value) })}
              />
            </Field>
            <Field label="Abstand max (s)" hint="max. Pause zwischen zwei Accounts">
              <Input
                type="number"
                value={String(cfg.spacing_max)}
                onChange={(e) => setCfg({ ...cfg, spacing_max: Number(e.target.value) })}
              />
            </Field>
          </div>
          <Button variant="outline" onClick={saveCfg}>
            Speichern
          </Button>
        </Card>
      )}

      {msg && (
        <div
          className="rounded-md border border-zinc-700 bg-zinc-900 p-3 text-sm"
          onClick={() => setMsg(null)}
        >
          {msg} <span className="text-zinc-500">(klicken zum Schließen)</span>
        </div>
      )}
    </div>
  );
}

function OutcomeCard({
  outcome,
  totalPool,
  selected,
  disabled,
  onClick,
}: {
  outcome: PredictionOutcome;
  totalPool: number;
  selected: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  const style = OUTCOME_STYLES[outcome.color] ?? OUTCOME_STYLES.BLUE;
  const share = totalPool > 0 ? (outcome.total_points / totalPool) * 100 : 0;
  const quote = outcome.total_points > 0 ? totalPool / outcome.total_points : null;
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`rounded-xl border p-4 text-left transition-colors disabled:opacity-60 ${
        selected ? style.selected : `${style.border} hover:bg-zinc-800/60`
      }`}
    >
      <div className={`font-semibold ${style.text}`}>
        {selected && "✔ "}
        {outcome.title}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-4 text-xs text-zinc-400">
        <span>{fmt(outcome.total_points)} Punkte ({share.toFixed(0)}%)</span>
        <span>{fmt(outcome.total_users)} Wetter</span>
        <span>Quote {quote != null ? `1:${quote.toFixed(2)}` : "—"}</span>
      </div>
    </button>
  );
}

function fmtSecs(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
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
