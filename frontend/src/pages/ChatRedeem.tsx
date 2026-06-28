import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, MessageSquare, Play, Plus, Square, Trash2 } from "lucide-react";
import { api, type ChatRedeemCommand, type Reward } from "@/lib/api";
import { Button, Card, Input, Textarea } from "@/components/ui";

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <div className="text-xs text-zinc-400">{label}</div>
      {children}
      {hint && <div className="mt-0.5 text-[11px] text-zinc-500">{hint}</div>}
    </label>
  );
}

const byReward = (a: Reward, b: Reward) =>
  a.isEnabled === b.isEnabled ? a.cost - b.cost : a.isEnabled ? -1 : 1;

// local editing type: a stable _key for React list identity (stripped on save)
type EditCmd = ChatRedeemCommand & { _key: string };
const newKey = () =>
  (globalThis.crypto?.randomUUID?.() ?? `k${Date.now()}${Math.random()}`);
const withKey = (c: ChatRedeemCommand): EditCmd => ({ ...c, _key: newKey() });

const blankCmd = (): EditCmd => ({
  command: "!", reward_id: "", reward_title: "", cooldown: 30, enabled: true, _key: newKey(),
});

export default function ChatRedeem() {
  const qc = useQueryClient();
  const { data: accounts = [] } = useQuery({ queryKey: ["accounts"], queryFn: api.listAccounts });
  const { data: loaded } = useQuery({ queryKey: ["chat-redeem-config"], queryFn: api.getChatRedeemConfig });
  const { data: status } = useQuery({
    queryKey: ["chat-redeem-status"],
    queryFn: api.getChatRedeemStatus,
    refetchInterval: 4000,
  });

  const [channel, setChannel] = useState("");
  const [announcer, setAnnouncer] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [commands, setCommands] = useState<EditCmd[]>([]);
  const [onText, setOnText] = useState("");
  const [offText, setOffText] = useState("");
  const [rewards, setRewards] = useState<Reward[]>([]);
  const [loadingRewards, setLoadingRewards] = useState(false);
  const [savingCmds, setSavingCmds] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [initialized, setInitialized] = useState(false);

  // seed local state from the server config once
  useEffect(() => {
    if (loaded && !initialized) {
      setChannel(loaded.channel);
      setAnnouncer(loaded.announcer);
      setEnabled(loaded.enabled);
      setCommands(loaded.commands.length ? loaded.commands.map(withKey) : [blankCmd()]);
      setOnText(loaded.on_text);
      setOffText(loaded.off_text);
      setInitialized(true);
    }
  }, [loaded, initialized]);

  // keep the local enabled flag in sync with the server (e.g. after a toggle),
  // but not while a toggle is in flight (would briefly revert the optimistic UI)
  useEffect(() => {
    if (loaded && initialized && !toggling) setEnabled(loaded.enabled);
  }, [loaded, initialized, toggling]);

  const loadRewards = async () => {
    const ch = channel.trim().toLowerCase();
    if (!ch) return;
    setLoadingRewards(true);
    try {
      const r = await api.getChatRedeemRewards(ch);
      setRewards(r.rewards);
      setMsg(`✅ ${r.rewards.length} Belohnungen von ${r.displayName} geladen`);
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setLoadingRewards(false);
    }
  };

  // auto-load rewards once a channel is known
  useEffect(() => {
    if (initialized && channel.trim() && rewards.length === 0) loadRewards();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialized]);

  const saveField = async (patch: Parameters<typeof api.putChatRedeemConfig>[0]) => {
    try {
      await api.putChatRedeemConfig(patch);
      qc.invalidateQueries({ queryKey: ["chat-redeem-status"] });
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const toggleEnabled = async (next: boolean) => {
    setToggling(true);
    setEnabled(next); // optimistic
    try {
      // persist the whole current config alongside, so enabling immediately
      // announces with the right command list and edited message text
      await api.putChatRedeemConfig({
        enabled: next, channel: channel.trim().toLowerCase(),
        announcer: announcer.trim().toLowerCase(), commands: cleanCommands(),
        on_text: onText, off_text: offText,
      });
      await qc.invalidateQueries({ queryKey: ["chat-redeem-config"] });
      qc.invalidateQueries({ queryKey: ["chat-redeem-status"] });
      setMsg(next
        ? "✅ Gestartet – die An-Nachricht wird im Chat gepostet (siehe Status)."
        : "🛑 Gestoppt – die Aus-Nachricht wird im Chat gepostet.");
    } catch (e) {
      setEnabled(!next); // revert
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setToggling(false);
    }
  };

  // strip the local _key, trim, and drop incomplete/invalid rows (a command
  // must be a prefix sigil + a char, e.g. "!flash" or "?flash", and have a reward)
  const cleanCommands = (): ChatRedeemCommand[] =>
    commands
      .map(({ _key, ...c }) => ({ ...c, command: c.command.trim().toLowerCase() }))
      .filter((c) => c.command.length >= 2 && /^[^a-z0-9\s]/.test(c.command) && c.reward_id);

  const saveCommands = async () => {
    setSavingCmds(true);
    try {
      const saved = await api.putChatRedeemConfig({ commands: cleanCommands() });
      setCommands(saved.commands.length ? saved.commands.map(withKey) : [blankCmd()]);
      qc.invalidateQueries({ queryKey: ["chat-redeem-status"] });
      setMsg("✅ Commands gespeichert");
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setSavingCmds(false);
    }
  };

  const setCmd = (i: number, patch: Partial<ChatRedeemCommand>) =>
    setCommands((cs) => cs.map((c, idx) => (idx === i ? { ...c, ...patch } : c)));

  const [testing, setTesting] = useState(false);
  const testConnection = async () => {
    setTesting(true);
    setMsg("⏳ Teste Chat-Verbindung des Ansage-Accounts über seinen Proxy…");
    try {
      const r = await api.testChatRedeem();
      const via = r.via_proxy ? "über Proxy" : "direkt (ohne Proxy)";
      if (r.joined && r.msg_error) {
        setMsg(`⚠️ ${r.announcer} ist ${via} verbunden, aber Twitch hat die Nachricht ABGELEHNT: „${r.msg_error}". Dieser Account kann in #${r.channel} nicht schreiben (z. B. gebannt / Follower-/Handy-Pflicht) — nimm einen anderen Ansage-Account.`);
      } else if (r.joined && r.sent) {
        setMsg(`✅ ${r.announcer} ist ${via} verbunden UND hat in #${r.channel} geschrieben — funktioniert!`);
      } else if (r.joined) {
        setMsg(`⚠️ ${r.announcer} ist ${via} verbunden, aber Schreiben fehlgeschlagen${r.send_error ? `: ${r.send_error}` : ""}.`);
      } else if (r.notice_error) {
        setMsg(`❌ Twitch lehnt den Login ab: „${r.notice_error}". Token ungültig/abgelaufen, Account hier neu einloggen — oder die (Proxy-)IP ist bei Twitch-Chat gesperrt.`);
      } else if (r.connect_error) {
        setMsg(`❌ Verbindung ${via} fehlgeschlagen: ${r.connect_error}. Proxy erreichbar? IRC (Port 6667) erlaubt?`);
      } else {
        setMsg(`❌ Timeout — keine Verbindung ${via} zustande gekommen. Proxy blockt evtl. IRC, oder Twitch-Chat sperrt die IP. Teste denselben Account einmal ohne Proxy.`);
      }
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setTesting(false);
    }
  };

  const toggleRedeemer = async (id: number, next: boolean) => {
    try {
      await api.updateAccount(id, { chat_redeemer: next });
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["chat-redeem-status"] });
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const rt = status?.runtime;
  const balById = new Map<number, number | null>((status?.redeemers ?? []).map((r) => [r.id, r.balance]));
  const sortedRewards = [...rewards].sort(byReward);
  // mirror the backend's render_on_text: only enabled commands that have a
  // reward and a valid sigil prefix, lowercased (so the preview matches chat)
  const activeCmdList = commands
    .filter((c) => c.enabled && c.reward_id && /^[^a-z0-9\s]/.test(c.command.trim().toLowerCase()) && c.command.trim().length >= 2)
    .map((c) => c.command.trim().toLowerCase())
    .join(" ");

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-2xl font-bold">
          <MessageSquare size={22} /> Chat-Einlösen
        </h1>
        <div className="flex items-center gap-2">
          {enabled ? (
            <Button variant="danger" disabled={toggling} onClick={() => toggleEnabled(false)}>
              {toggling ? <Loader2 className="animate-spin" size={15} /> : <Square size={15} />} Stoppen
            </Button>
          ) : (
            <Button disabled={toggling} onClick={() => toggleEnabled(true)}>
              {toggling ? <Loader2 className="animate-spin" size={15} /> : <Play size={15} />} Starten
            </Button>
          )}
        </div>
      </div>

      <p className="text-sm text-zinc-400">
        Liest den Chat des Streamers. Schreibt ein Zuschauer einen Command (z. B. <b>!flash</b> oder
        <b> ?flash</b>), löst der zuerst freie <b>Chat-Einlöser</b>-Account <b>mit den meisten Punkten</b>
        die zugeordnete Belohnung ein. Der Command muss <b>exakt</b> so getippt werden, wie du ihn
        einträgst, und mit einem Zeichen wie <b>! ? #</b> beginnen (ein normales Wort wie „flash" würde
        sonst bei jeder Chat-Nachricht auslösen). Beim An- und Ausschalten postet der Ansage-Account
        eine Nachricht im Chat. Cooldown pro Command verhindert Spam.
      </p>

      {/* Live status */}
      <Card className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div><div className="text-zinc-500">Status</div><div>{rt?.active ? "🟢 aktiv" : "⚪ aus"}</div></div>
        <div><div className="text-zinc-500">Ansage-Account</div>
          <div>{rt?.observer_connected ? `🟢 ${rt.announcer}` : rt?.announcer ? `⏳ ${rt.announcer}` : "—"}</div></div>
        <div><div className="text-zinc-500">Channel</div><div>{rt?.channel ?? "—"}</div></div>
        <div><div className="text-zinc-500">Letzte Auslösungen</div><div>{rt?.last_triggers?.length ?? 0}</div></div>
        <div className="col-span-2 sm:col-span-4">
          <div className="text-zinc-500">Grund / Diagnose</div>
          <div className={rt?.active ? "text-emerald-400" : "text-amber-400"}>{rt?.reason ?? "—"}</div>
        </div>
        {rt?.last_triggers && rt.last_triggers.length > 0 && (
          <div className="col-span-2 space-y-0.5 sm:col-span-4">
            {rt.last_triggers.slice(0, 5).map((t, i) => (
              <div key={i} className="text-xs">
                <span className={t.ok ? "text-emerald-400" : "text-amber-400"}>{t.ok ? "✅" : "⚠️"}</span>{" "}
                <b>{t.command}</b> von {t.nick} – {t.message} <span className="text-zinc-600">({Math.round(t.age)}s)</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Channel + announcer */}
      <Card className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Streamer-Channel" hint="Chat, der gelesen wird, und Channel, in dem eingelöst wird">
          <Input value={channel} placeholder="z. B. j4nkttv"
            onChange={(e) => setChannel(e.target.value)}
            onBlur={() => { const ch = channel.trim().toLowerCase(); setChannel(ch); saveField({ channel: ch }); }} />
        </Field>
        <Field label="Ansage-Account" hint="postet die An/Aus-Nachricht und liest den Chat (muss eingeloggt sein)">
          <select
            className="h-9 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
            value={announcer}
            onChange={(e) => { setAnnouncer(e.target.value); saveField({ announcer: e.target.value }); }}>
            <option value="">— wählen —</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.username.toLowerCase()}>{a.username}</option>
            ))}
          </select>
        </Field>
        <div className="sm:col-span-2">
          <Button size="sm" variant="outline" disabled={testing || !announcer || !channel.trim()}
            onClick={testConnection}>
            {testing ? <Loader2 className="animate-spin" size={14} /> : null} Chat-Verbindung testen
          </Button>
          <span className="ml-2 text-[11px] text-zinc-500">
            meldet den Ansage-Account über seinen Proxy an und postet eine kurze Test-Zeile in #{channel.trim() || "channel"}
          </span>
        </div>
      </Card>

      {/* Editable announcement messages */}
      <Card className="space-y-3">
        <div className="text-sm font-semibold">Chat-Nachrichten</div>
        <Field label="An-Nachricht (beim Starten)"
          hint="Platzhalter {commands} wird durch die aktiven Commands ersetzt.">
          <Textarea rows={2} value={onText}
            onChange={(e) => setOnText(e.target.value)}
            onBlur={() => saveField({ on_text: onText })} />
        </Field>
        <div className="rounded-md border border-zinc-800 bg-zinc-950 p-2 text-xs text-zinc-300">
          <span className="text-zinc-500">Vorschau: </span>
          {onText.replace(/\{commands\}/g, activeCmdList || "(keine)")}
        </div>
        <Field label="Aus-Nachricht (beim Stoppen)">
          <Textarea rows={2} value={offText}
            onChange={(e) => setOffText(e.target.value)}
            onBlur={() => saveField({ off_text: offText })} />
        </Field>
      </Card>

      {/* Commands */}
      <Card className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">Commands → Belohnungen</div>
          <Button size="sm" variant="outline" disabled={loadingRewards || !channel.trim()} onClick={loadRewards}>
            {loadingRewards ? <Loader2 className="animate-spin" size={14} /> : null} Belohnungen laden
          </Button>
        </div>

        {commands.map((c, i) => (
          <div key={c._key} className="flex flex-wrap items-end gap-2 border-t border-zinc-800 pt-3">
            <div className="w-28">
              <label className="text-[11px] text-zinc-400">Command</label>
              <Input value={c.command} placeholder="!flash oder ?flash"
                onChange={(e) => setCmd(i, { command: e.target.value })} />
            </div>
            <div className="min-w-[180px] flex-1">
              <label className="text-[11px] text-zinc-400">Belohnung</label>
              <select
                className="h-9 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
                value={c.reward_id}
                onChange={(e) => {
                  const rw = sortedRewards.find((r) => r.id === e.target.value);
                  setCmd(i, { reward_id: e.target.value, reward_title: rw?.title ?? c.reward_title });
                }}>
                <option value="">— Belohnung wählen —</option>
                {/* keep the stored reward selectable even before the catalogue loads */}
                {c.reward_id && !sortedRewards.some((r) => r.id === c.reward_id) && (
                  <option value={c.reward_id}>{c.reward_title || c.reward_id}</option>
                )}
                {sortedRewards.map((r) => (
                  <option key={r.id} value={r.id} disabled={!r.isEnabled || r.isPaused}>
                    {r.title} — {r.cost.toLocaleString()} P
                  </option>
                ))}
              </select>
            </div>
            <div className="w-24">
              <label className="text-[11px] text-zinc-400">Cooldown (s)</label>
              <Input type="number" min={0} value={String(c.cooldown ?? 30)}
                onChange={(e) => setCmd(i, { cooldown: e.target.value === "" ? 30 : Math.max(0, Number(e.target.value) || 0) })} />
            </div>
            <label className="flex h-9 items-center gap-1.5 text-xs text-zinc-400">
              <input type="checkbox" checked={c.enabled}
                onChange={(e) => setCmd(i, { enabled: e.target.checked })} />
              an
            </label>
            <Button size="sm" variant="ghost"
              onClick={() => setCommands((cs) => cs.filter((_, idx) => idx !== i))}>
              <Trash2 size={14} />
            </Button>
          </div>
        ))}

        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => setCommands((cs) => [...cs, blankCmd()])}>
            <Plus size={14} /> Command
          </Button>
          <Button size="sm" disabled={savingCmds} onClick={saveCommands}>
            {savingCmds ? <Loader2 className="animate-spin" size={14} /> : null} Speichern
          </Button>
        </div>
      </Card>

      {/* Which accounts may spend points */}
      <Card className="space-y-2">
        <div className="text-sm font-semibold">Chat-Einlöser (welche Accounts Punkte ausgeben dürfen)</div>
        <div className="text-[11px] text-zinc-500">
          Aus diesen wählt das Modul pro Command den zuerst freien Account mit den meisten Punkten.
          Guthaben aktualisiert sich alle ~45 s, während das Modul aktiv ist.
        </div>
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
          {accounts.map((a) => {
            const bal = balById.get(a.id);
            return (
              <label key={a.id} className="flex items-center justify-between gap-2 rounded-md border border-zinc-800 px-3 py-1.5 text-sm">
                <span className="flex items-center gap-2">
                  <input type="checkbox" checked={a.chat_redeemer}
                    onChange={(e) => toggleRedeemer(a.id, e.target.checked)} />
                  {a.username}
                </span>
                {a.chat_redeemer && (
                  <span className="text-xs text-zinc-400">
                    {bal != null ? <>{bal.toLocaleString()} P</> : <span className="text-zinc-600">—</span>}
                  </span>
                )}
              </label>
            );
          })}
          {accounts.length === 0 && <div className="text-zinc-500">Noch keine Accounts.</div>}
        </div>
      </Card>

      {msg && (
        <div className="rounded-md border border-zinc-700 bg-zinc-900 p-3 text-sm" onClick={() => setMsg(null)}>
          {msg} <span className="text-zinc-500">(klicken zum Schließen)</span>
        </div>
      )}
    </div>
  );
}
