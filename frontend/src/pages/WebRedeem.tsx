import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, ExternalLink, Globe, KeyRound, Loader2, Play, Plus, Square, Trash2, UserPlus } from "lucide-react";
import { api, type Reward, type WebRedeemItem } from "@/lib/api";
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
type EditItem = WebRedeemItem & { _key: string };
const newKey = () =>
  (globalThis.crypto?.randomUUID?.() ?? `k${Date.now()}${Math.random()}`);
const withKey = (i: WebRedeemItem): EditItem => ({ ...i, _key: newKey() });

const blankItem = (): EditItem => ({
  reward_id: "", label: "", reward_title: "", description: "", cooldown: 60,
  enabled: true, _key: newKey(),
});

export default function WebRedeem() {
  const qc = useQueryClient();
  const { data: accounts = [] } = useQuery({ queryKey: ["accounts"], queryFn: api.listAccounts });
  const { data: loaded } = useQuery({ queryKey: ["web-redeem-config"], queryFn: api.getWebRedeemConfig });
  const { data: status } = useQuery({
    queryKey: ["web-redeem-status"],
    queryFn: api.getWebRedeemStatus,
    refetchInterval: 4000,
  });

  const { data: webUsers = [] } = useQuery({
    queryKey: ["web-users"],
    queryFn: api.listWebUsers,
    refetchInterval: 10000,   // neue Konto-Anfragen automatisch anzeigen
  });

  const [channel, setChannel] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [items, setItems] = useState<EditItem[]>([]);
  const [title, setTitle] = useState("");
  const [tagline, setTagline] = useState("");
  const [offlineText, setOfflineText] = useState("");
  const [announce, setAnnounce] = useState(false);
  const [announcer, setAnnouncer] = useState("");
  const [announceText, setAnnounceText] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [creatingUser, setCreatingUser] = useState(false);
  const [rewards, setRewards] = useState<Reward[]>([]);
  const [loadingRewards, setLoadingRewards] = useState(false);
  const [savingItems, setSavingItems] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [initialized, setInitialized] = useState(false);
  const [token, setToken] = useState<string | null>(null);

  // seed local state from the server config once
  useEffect(() => {
    if (loaded && !initialized) {
      setChannel(loaded.channel);
      setEnabled(loaded.enabled);
      setItems(loaded.items.length ? loaded.items.map(withKey) : [blankItem()]);
      setTitle(loaded.title);
      setTagline(loaded.tagline);
      setOfflineText(loaded.offline_text);
      setAnnounce(loaded.announce);
      setAnnouncer(loaded.announcer);
      setAnnounceText(loaded.announce_text);
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
      const r = await api.getWebRedeemRewards(ch);
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

  const saveField = async (patch: Parameters<typeof api.putWebRedeemConfig>[0]) => {
    try {
      await api.putWebRedeemConfig(patch);
      qc.invalidateQueries({ queryKey: ["web-redeem-status"] });
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const toggleEnabled = async (next: boolean) => {
    setToggling(true);
    setEnabled(next); // optimistic
    try {
      // persist the whole current config alongside, so enabling immediately
      // serves the website with the just-edited items and texts
      await api.putWebRedeemConfig({
        enabled: next, channel: channel.trim().toLowerCase(),
        items: cleanItems(), title, tagline, offline_text: offlineText,
        announce, announcer, announce_text: announceText,
      });
      await qc.invalidateQueries({ queryKey: ["web-redeem-config"] });
      qc.invalidateQueries({ queryKey: ["web-redeem-status"] });
      setMsg(next
        ? "✅ Gestartet – die Webseite zeigt die Belohnungen jetzt an."
        : "🛑 Gestoppt – die Webseite zeigt jetzt den Offline-Text.");
    } catch (e) {
      setEnabled(!next); // revert
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setToggling(false);
    }
  };

  // strip the local _key and drop rows without a reward
  const cleanItems = (): WebRedeemItem[] =>
    items
      .map(({ _key, ...i }) => ({ ...i, label: (i.label ?? "").trim() }))
      .filter((i) => i.reward_id);

  const saveItems = async () => {
    setSavingItems(true);
    try {
      const saved = await api.putWebRedeemConfig({ items: cleanItems() });
      setItems(saved.items.length ? saved.items.map(withKey) : [blankItem()]);
      qc.invalidateQueries({ queryKey: ["web-redeem-status"] });
      setMsg("✅ Belohnungen gespeichert");
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setSavingItems(false);
    }
  };

  const setItem = (i: number, patch: Partial<WebRedeemItem>) =>
    setItems((is) => is.map((it, idx) => (idx === i ? { ...it, ...patch } : it)));

  const toggleRedeemer = async (id: number, next: boolean) => {
    try {
      await api.updateAccount(id, { web_redeemer: next });
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["web-redeem-status"] });
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const createUser = async () => {
    const username = newUsername.trim();
    if (!username) return;
    setCreatingUser(true);
    try {
      const u = await api.createWebUser({
        username, password: newPassword.trim() || undefined,
      });
      qc.invalidateQueries({ queryKey: ["web-users"] });
      setNewUsername("");
      setNewPassword("");
      setMsg(u.generated_password
        ? `✅ Benutzer „${u.username}" angelegt – generiertes Passwort: ${u.generated_password} (jetzt notieren, wird nicht wieder angezeigt; muss beim ersten Login geändert werden)`
        : `✅ Benutzer „${u.username}" angelegt`);
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    } finally {
      setCreatingUser(false);
    }
  };

  const approveUser = async (id: number, username: string) => {
    try {
      await api.approveWebUser(id);
      qc.invalidateQueries({ queryKey: ["web-users"] });
      setMsg(`✅ „${username}" freigeschaltet – kann sich jetzt einloggen`);
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const resetUser = async (id: number, username: string) => {
    try {
      const u = await api.resetWebUser(id);
      qc.invalidateQueries({ queryKey: ["web-users"] });
      setMsg(`🔑 Passwort von „${username}" zurückgesetzt – neues Passwort: ${u.generated_password} (jetzt notieren; muss beim nächsten Login geändert werden)`);
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const deleteUser = async (id: number, username: string) => {
    if (!confirm(`Benutzer „${username}" wirklich löschen?`)) return;
    try {
      await api.deleteWebUser(id);
      qc.invalidateQueries({ queryKey: ["web-users"] });
      setMsg(`🗑️ Benutzer „${username}" gelöscht`);
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const showToken = async () => {
    try {
      const r = await api.getWebRedeemToken();
      setToken(r.token);
    } catch (e) {
      setMsg(`❌ ${(e as Error).message}`);
    }
  };

  const copyToken = async () => {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(token);
      setMsg("✅ Token kopiert");
    } catch {
      setMsg("❌ Kopieren fehlgeschlagen – Token manuell markieren");
    }
  };

  const rt = status?.runtime;
  const balById = new Map<number, number | null>((status?.redeemers ?? []).map((r) => [r.id, r.balance]));
  const sortedRewards = [...rewards].sort(byReward);
  const totalPoints = (status?.redeemers ?? [])
    .reduce((sum, r) => sum + (r.balance ?? 0), 0);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-2xl font-bold">
          <Globe size={22} /> Webseite-Einlösen
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
        Besucher der öffentlichen Redeem-Webseite (eigener Docker-Container, siehe{" "}
        <b>WEBREDEEM.md</b>) melden sich mit einem <b>Webseiten-Benutzer</b> (unten anlegbar)
        an und lösen freigegebene Belohnungen per Klick aus — wie Chat-Einlösen, nur ohne
        Chat. Es zahlt der zuerst freie <b>Web-Einlöser</b>-Account <b>mit den meisten
        Punkten</b>. Optional sagt ein Account im Twitch-Chat an, wer was eingelöst hat.
        Cooldowns pro Belohnung verhindern Spam; die Cooldowns der Seite „Einlösen"
        (pro Account / global) gelten zusätzlich.
      </p>

      {/* Live status */}
      <Card className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div><div className="text-zinc-500">Status</div><div>{rt?.enabled ? "🟢 an" : "⚪ aus"}</div></div>
        <div><div className="text-zinc-500">Channel</div>
          <div>{rt?.channel_display ?? rt?.channel ?? "—"}</div></div>
        <div><div className="text-zinc-500">Punkte gesamt</div>
          <div>{totalPoints.toLocaleString()} P</div></div>
        <div><div className="text-zinc-500">Chat-Ansage</div>
          <div>{!rt?.announce ? "⚪ aus" : rt?.announcer_connected ? `🟢 ${rt.announcer}` : rt?.announcer ? `⏳ ${rt.announcer}` : "⚠️ kein Account"}</div></div>
        <div><div className="text-zinc-500">Letzte Auslösungen</div><div>{rt?.last_triggers?.length ?? 0}</div></div>
        <div className="col-span-2 sm:col-span-4">
          <div className="text-zinc-500">Grund / Diagnose</div>
          <div className={rt?.reason === "aktiv" ? "text-emerald-400" : "text-amber-400"}>{rt?.reason ?? "—"}</div>
        </div>
        {rt?.last_triggers && rt.last_triggers.length > 0 && (
          <div className="col-span-2 space-y-0.5 sm:col-span-4">
            {rt.last_triggers.slice(0, 5).map((t, i) => (
              <div key={i} className="text-xs">
                <span className={t.ok ? "text-emerald-400" : "text-amber-400"}>{t.ok ? "✅" : "⚠️"}</span>{" "}
                <b>{t.label}</b> von {t.visitor} – {t.message} <span className="text-zinc-600">({Math.round(t.age)}s)</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Channel + page texts */}
      <Card className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Streamer-Channel" hint="Channel, in dem die Punkte eingelöst werden">
          <Input value={channel} placeholder="z. B. j4nkttv"
            onChange={(e) => setChannel(e.target.value)}
            onBlur={() => { const ch = channel.trim().toLowerCase(); setChannel(ch); saveField({ channel: ch }); }} />
        </Field>
        <Field label="Seitentitel" hint="Überschrift auf der öffentlichen Seite">
          <Input value={title}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={() => saveField({ title })} />
        </Field>
        <div className="sm:col-span-2">
          <Field label="Untertitel" hint="kurzer Text unter der Überschrift">
            <Textarea rows={2} value={tagline}
              onChange={(e) => setTagline(e.target.value)}
              onBlur={() => saveField({ tagline })} />
          </Field>
        </div>
        <div className="sm:col-span-2">
          <Field label="Offline-Text" hint="wird angezeigt, solange das Modul aus ist">
            <Textarea rows={2} value={offlineText}
              onChange={(e) => setOfflineText(e.target.value)}
              onBlur={() => saveField({ offline_text: offlineText })} />
          </Field>
        </div>
        {channel.trim() && (
          <a className="flex items-center gap-1 text-xs text-brand hover:underline"
            href={`https://www.twitch.tv/${channel.trim().toLowerCase()}`} target="_blank" rel="noreferrer">
            <ExternalLink size={13} /> twitch.tv/{channel.trim().toLowerCase()}
          </a>
        )}
      </Card>

      {/* Chat announcement */}
      <Card className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">Chat-Ansage („wer hat was eingelöst")</div>
          <label className="flex items-center gap-1.5 text-xs text-zinc-400">
            <input type="checkbox" checked={announce}
              onChange={(e) => { setAnnounce(e.target.checked); saveField({ announce: e.target.checked }); }} />
            an
          </label>
        </div>
        <div className="text-[11px] text-zinc-500">
          Wenn an: Nach jeder erfolgreichen Web-Einlösung postet der Ansage-Account im Chat
          von #{channel.trim() || "channel"}, welcher Webseiten-Benutzer was eingelöst hat.
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Ansage-Account" hint="postet die Nachricht (muss eingeloggt sein)">
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
          <Field label="Ansage-Text"
            hint="Platzhalter: {user} = Webseiten-Benutzer, {reward} = Belohnung, {cost} = Kosten">
            <Textarea rows={2} value={announceText}
              onChange={(e) => setAnnounceText(e.target.value)}
              onBlur={() => saveField({ announce_text: announceText })} />
          </Field>
        </div>
      </Card>

      {/* Items */}
      <Card className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">Belohnungen auf der Webseite</div>
          <Button size="sm" variant="outline" disabled={loadingRewards || !channel.trim()} onClick={loadRewards}>
            {loadingRewards ? <Loader2 className="animate-spin" size={14} /> : null} Belohnungen laden
          </Button>
        </div>

        {items.map((it, i) => (
          <div key={it._key} className="flex flex-wrap items-end gap-2 border-t border-zinc-800 pt-3">
            <div className="min-w-[180px] flex-1">
              <label className="text-[11px] text-zinc-400">Belohnung</label>
              <select
                className="h-9 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
                value={it.reward_id}
                onChange={(e) => {
                  const rw = sortedRewards.find((r) => r.id === e.target.value);
                  setItem(i, { reward_id: e.target.value, reward_title: rw?.title ?? it.reward_title });
                }}>
                <option value="">— Belohnung wählen —</option>
                {/* keep the stored reward selectable even before the catalogue loads */}
                {it.reward_id && !sortedRewards.some((r) => r.id === it.reward_id) && (
                  <option value={it.reward_id}>{it.reward_title || it.reward_id}</option>
                )}
                {sortedRewards.map((r) => (
                  <option key={r.id} value={r.id} disabled={!r.isEnabled || r.isPaused || r.isUserInputRequired}>
                    {r.title} — {r.cost.toLocaleString()} P{r.isUserInputRequired ? " (Eingabe nötig)" : ""}
                  </option>
                ))}
              </select>
            </div>
            <div className="w-40">
              <label className="text-[11px] text-zinc-400">Anzeigename (optional)</label>
              <Input value={it.label ?? ""} placeholder="wie die Belohnung"
                onChange={(e) => setItem(i, { label: e.target.value })} />
            </div>
            <div className="min-w-[160px] flex-1">
              <label className="text-[11px] text-zinc-400">Beschreibung (optional)</label>
              <Input value={it.description ?? ""} placeholder="kurzer Text auf der Karte"
                onChange={(e) => setItem(i, { description: e.target.value })} />
            </div>
            <div className="w-24">
              <label className="text-[11px] text-zinc-400">Cooldown (s)</label>
              <Input type="number" min={0} value={String(it.cooldown ?? 60)}
                onChange={(e) => setItem(i, { cooldown: e.target.value === "" ? 60 : Math.max(0, Number(e.target.value) || 0) })} />
            </div>
            <label className="flex h-9 items-center gap-1.5 text-xs text-zinc-400">
              <input type="checkbox" checked={it.enabled}
                onChange={(e) => setItem(i, { enabled: e.target.checked })} />
              an
            </label>
            <Button size="sm" variant="ghost"
              onClick={() => setItems((is) => is.filter((_, idx) => idx !== i))}>
              <Trash2 size={14} />
            </Button>
          </div>
        ))}

        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => setItems((is) => [...is, blankItem()])}>
            <Plus size={14} /> Belohnung
          </Button>
          <Button size="sm" disabled={savingItems} onClick={saveItems}>
            {savingItems ? <Loader2 className="animate-spin" size={14} /> : null} Speichern
          </Button>
        </div>
      </Card>

      {/* Which accounts may spend points */}
      <Card className="space-y-2">
        <div className="text-sm font-semibold">Web-Einlöser (welche Accounts Punkte ausgeben dürfen)</div>
        <div className="text-[11px] text-zinc-500">
          Aus diesen wählt das Modul pro Klick den zuerst freien Account mit den meisten Punkten.
          Unabhängig vom Chat-Einlöser-Haken; Guthaben aktualisiert sich alle ~45 s, während das
          Modul an ist. Die Summe wird Besuchern als „verfügbare Punkte" angezeigt.
        </div>
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
          {accounts.map((a) => {
            const bal = balById.get(a.id);
            return (
              <label key={a.id} className="flex items-center justify-between gap-2 rounded-md border border-zinc-800 px-3 py-1.5 text-sm">
                <span className="flex items-center gap-2">
                  <input type="checkbox" checked={a.web_redeemer}
                    onChange={(e) => toggleRedeemer(a.id, e.target.checked)} />
                  {a.username}
                </span>
                {a.web_redeemer && (
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

      {/* Website login users */}
      <Card className="space-y-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          Webseiten-Benutzer (Login für die öffentliche Seite)
          {webUsers.some((u) => !u.approved) && (
            <span className="rounded-full border border-brand/40 bg-brand/15 px-2 py-0.5 text-[10px] font-normal text-brand">
              {webUsers.filter((u) => !u.approved).length} Anfrage(n) offen
            </span>
          )}
        </div>
        <div className="text-[11px] text-zinc-500">
          Nur mit so einem Benutzer kann man sich auf der Webseite einloggen und einlösen.
          Besucher können über „Konto erstellen" eine Anfrage stellen — sie erscheint hier
          und muss mit ✓ freigeschaltet werden. Ohne Passwort-Eingabe wird beim Anlegen eins
          generiert und einmalig angezeigt; der Benutzer muss es beim ersten Login ändern.
          „Reset" erzeugt ein neues Einmal-Passwort und meldet den Benutzer überall ab.
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-40">
            <label className="text-[11px] text-zinc-400">Benutzername</label>
            <Input value={newUsername} placeholder="z. B. max"
              onChange={(e) => setNewUsername(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") createUser(); }} />
          </div>
          <div className="w-48">
            <label className="text-[11px] text-zinc-400">Passwort (leer = generieren)</label>
            <Input type="text" value={newPassword} placeholder="min. 8 Zeichen"
              onChange={(e) => setNewPassword(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") createUser(); }} />
          </div>
          <Button size="sm" disabled={creatingUser || !newUsername.trim()} onClick={createUser}>
            {creatingUser ? <Loader2 className="animate-spin" size={14} /> : <UserPlus size={14} />} Anlegen
          </Button>
        </div>
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
          {[...webUsers].sort((a, b) => Number(a.approved) - Number(b.approved)).map((u) => (
            <div key={u.id}
              className={`flex items-center justify-between gap-2 rounded-md border px-3 py-1.5 text-sm ${u.approved ? "border-zinc-800" : "border-brand/40 bg-brand/5"}`}>
              <span className="flex items-center gap-2">
                {u.username}
                {!u.approved && (
                  <span className="rounded-full border border-brand/40 bg-brand/15 px-2 py-0.5 text-[10px] text-brand">
                    wartet auf Freischaltung
                  </span>
                )}
                {u.approved && u.must_change_password && (
                  <span className="rounded-full border border-amber-500/30 bg-amber-500/15 px-2 py-0.5 text-[10px] text-amber-400">
                    muss Passwort ändern
                  </span>
                )}
              </span>
              <span className="flex items-center gap-1">
                <span className="mr-1 text-[10px] text-zinc-600">
                  {u.last_seen_at ? `zuletzt ${new Date(u.last_seen_at).toLocaleString()}` : "noch nie eingeloggt"}
                </span>
                {!u.approved && (
                  <Button size="sm" variant="success" title="Anfrage freischalten"
                    onClick={() => approveUser(u.id, u.username)}>
                    <Check size={14} /> Freischalten
                  </Button>
                )}
                <Button size="sm" variant="ghost" title="Passwort zurücksetzen"
                  onClick={() => resetUser(u.id, u.username)}>
                  <KeyRound size={14} />
                </Button>
                <Button size="sm" variant="ghost" title={u.approved ? "Benutzer löschen" : "Anfrage ablehnen"}
                  onClick={() => deleteUser(u.id, u.username)}>
                  <Trash2 size={14} />
                </Button>
              </span>
            </div>
          ))}
          {webUsers.length === 0 && <div className="text-sm text-zinc-500">Noch keine Benutzer.</div>}
        </div>
      </Card>

      {/* Website container setup */}
      <Card className="space-y-2">
        <div className="text-sm font-semibold">Verbindung zum Webseiten-Container</div>
        <div className="text-[11px] text-zinc-500">
          Der öffentliche Container (Image <b>…-webredeem</b>) braucht die URL dieses Managers
          (<b>MANAGER_URL</b>) und das Zugriffs-Token (<b>REDEEM_TOKEN</b>). Setup: siehe WEBREDEEM.md.
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={showToken}>Token anzeigen</Button>
          {token && (
            <>
              <code className="rounded bg-zinc-950 px-2 py-1 text-xs">{token}</code>
              <Button size="sm" variant="ghost" onClick={copyToken}><Copy size={14} /></Button>
            </>
          )}
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
