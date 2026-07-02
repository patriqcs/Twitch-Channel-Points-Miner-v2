import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Gift, Loader2, Square, Users } from "lucide-react";
import { api, type Reward } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";

type Loaded =
  | { balance: number; displayName: string; rewards: Reward[] }
  | { error: string };

// fewest points first; disabled rewards go to the bottom
const byReward = (a: Reward, b: Reward) =>
  a.isEnabled === b.isEnabled ? a.cost - b.cost : a.isEnabled ? -1 : 1;

export default function Redeem() {
  const { data: accounts = [] } = useQuery({ queryKey: ["accounts"], queryFn: api.listAccounts });

  const [channel, setChannel] = useState("");
  const [cooldowns, setCooldowns] = useState<Record<string, number>>({});      // reward_id -> per-account cooldown (s)
  const [masterDelays, setMasterDelays] = useState<Record<string, number>>({}); // reward_id -> global spacing (s)
  const [allCount, setAllCount] = useState<Record<string, string>>({});        // reward_id -> how many to schedule
  const [rewards, setRewards] = useState<Reward[]>([]);                    // reward catalogue (from a scout account)
  const [data, setData] = useState<Record<number, Loaded>>({});           // per-account balance/rewards
  const [sel, setSel] = useState<Record<number, string>>({});
  const [count, setCount] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<Record<number, boolean>>({});
  const [allBusy, setAllBusy] = useState<string | null>(null);            // reward_id being redeemed for all
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [cdSet, setCdSet] = useState<Set<string>>(new Set());             // "accId:rewardId" on cooldown
  const [runs, setRuns] = useState<Record<string, { fired: number; count: number }>>({}); // reward_id -> live progress
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());   // reward_ids with a pending cancel

  const refreshCooldowns = () =>
    api.getCooldowns()
      .then((cds) => setCdSet(new Set(cds.map((c) => `${c.account_id}:${c.reward_id}`))))
      .catch(() => {});

  const refreshRuns = () =>
    api.redeemAllStatus()
      .then((rs) => {
        setRuns(Object.fromEntries(rs.map((r) => [r.reward_id, { fired: r.fired, count: r.count }])));
        // a reward that's no longer running has finished/aborted -> clear its pending-cancel flag
        const live = new Set(rs.map((r) => r.reward_id));
        setCancelling((c) => new Set([...c].filter((id) => live.has(id))));
      })
      .catch(() => {});

  // poll cooldowns + running master redeems while rewards are shown
  useEffect(() => {
    if (rewards.length === 0) return;
    refreshCooldowns();
    refreshRuns();
    const tCd = setInterval(refreshCooldowns, 5000);
    const tRun = setInterval(refreshRuns, 1500);
    return () => { clearInterval(tCd); clearInterval(tRun); };
  }, [rewards.length]);

  // accounts ready for a reward: enough points AND not on cooldown
  const readyCount = (r: Reward) =>
    accounts.filter((a) => {
      const d = data[a.id];
      return d && !("error" in d) && d.balance >= r.cost && !cdSet.has(`${a.id}:${r.id}`);
    }).length;

  // load persisted config once
  useEffect(() => {
    api.getRedeemConfig().then((c) => {
      setChannel(c.channel ?? "");
      setCooldowns(c.cooldowns ?? {});
      setMasterDelays(c.master_delays ?? {});
      setAllCount(Object.fromEntries(
        Object.entries(c.counts ?? {}).map(([k, v]) => [k, String(v)])
      ));
    }).catch(() => {});
  }, []);

  const saveConfig = (patch: { channel?: string; cooldowns?: Record<string, number>; master_delays?: Record<string, number>; counts?: Record<string, number> }) =>
    api.putRedeemConfig(patch).catch((e) => setToast((e as Error).message));

  // auto-load the saved channel's rewards once on open, so the persisted
  // cooldown/spacing/count values are visible immediately (no manual reload).
  const didAutoLoad = useRef(false);
  useEffect(() => {
    if (!didAutoLoad.current && channel.trim() && accounts.length > 0) {
      didAutoLoad.current = true;
      load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel, accounts.length]);

  const loadSeq = useRef(0);
  const load = async () => {
    const ch = channel.trim().toLowerCase();
    if (!ch) return;
    // Sequence guard: Enter can trigger load() without the button's loading
    // guard, so two loads for different channels can race. Only the most recent
    // load may apply its results; a slower earlier one is dropped, otherwise it
    // would show channel A's balances/rewards while the input says B (and a
    // redeem would then post A's reward_id against channel B).
    const seq = ++loadSeq.current;
    setLoading(true);
    setData({});
    saveConfig({ channel: ch });
    const entries = await Promise.all(
      accounts.map(async (a) => {
        try {
          const r = await api.channelPoints(a.id, ch);
          return [a.id, { balance: r.balance, displayName: r.displayName, rewards: r.rewards }] as const;
        } catch (e) {
          return [a.id, { error: (e as Error).message }] as const;
        }
      })
    );
    if (seq !== loadSeq.current) return; // a newer load started -> drop stale results
    const map = Object.fromEntries(entries);
    setData(map);
    // reward catalogue = rewards from the first account that loaded successfully
    const ok = entries.map(([, v]) => v).find((v): v is Extract<Loaded, { rewards: Reward[] }> => "rewards" in v);
    if (ok) setRewards(ok.rewards);
    setLoading(false);
  };

  const refreshOne = async (id: number) => {
    try {
      const cp = await api.channelPoints(id, channel.trim().toLowerCase());
      setData((d) => ({ ...d, [id]: { balance: cp.balance, displayName: cp.displayName, rewards: cp.rewards } }));
    } catch { /* ignore */ }
  };

  const doRedeem = async (accountId: number) => {
    const rewardId = sel[accountId];
    if (!rewardId) { setToast("Bitte erst eine Belohnung wählen."); return; }
    setBusy((b) => ({ ...b, [accountId]: true }));
    try {
      const r = await api.redeem(accountId, {
        channel: channel.trim().toLowerCase(), reward_id: rewardId, count: Number(count[accountId]) || 1,
      });
      const fail = r.results.find((x) => !x.ok);
      setToast(`${r.reward}: ${r.succeeded}/${r.attempted} eingelöst${fail ? ` — ${fail.message}` : ""}`);
      refreshOne(accountId);
    } catch (e) {
      setToast((e as Error).message);
    } finally {
      setBusy((b) => ({ ...b, [accountId]: false }));
    }
  };

  const doRedeemAll = async (rewardId: string) => {
    setAllBusy(rewardId);
    try {
      const cnt = Number(allCount[rewardId]) || undefined;
      const r = await api.redeemAll({ channel: channel.trim().toLowerCase(), reward_id: rewardId, count: cnt });
      setToast(`„${r.reward}": ${r.scheduled} Einlösungen über ${r.accounts} Accounts geplant (Spacing ${r.global_delay}s). Läuft im Hintergrund — siehe Logs.`);
      refreshRuns();
      setTimeout(() => { accounts.forEach((a) => refreshOne(a.id)); refreshCooldowns(); }, 1500);
    } catch (e) {
      setToast((e as Error).message);
    } finally {
      setAllBusy(null);
    }
  };

  const doCancelAll = async (rewardId: string) => {
    setCancelling((c) => new Set(c).add(rewardId));
    try {
      await api.cancelRedeemAll({ reward_id: rewardId });
      refreshRuns();
    } catch (e) {
      setToast((e as Error).message);
      setCancelling((c) => { const n = new Set(c); n.delete(rewardId); return n; });
    }
  };

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-bold">Einlösen</h1>
      <p className="text-sm text-zinc-400">
        Channel-Points pro Account einlösen — jeweils über Proxy + Login. „Alle Accounts" plant
        <b> Anzahl</b> Einlösungen und verteilt sie auf den jeweils zuerst freien Account, unter
        Beachtung von <b>Cooldown</b> (pro Account, z. B. 60s) und <b>Spacing</b> (globaler
        Mindestabstand zwischen zwei Einlösungen desselben Rewards). Läuft im Hintergrund.
      </p>

      <Card className="flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-[200px]">
          <label className="text-xs text-zinc-400">Channel (Twitch-Login)</label>
          <Input placeholder="z. B. j4nkttv" value={channel}
            onChange={(e) => setChannel(e.target.value)}
            onBlur={() => saveConfig({ channel: channel.trim().toLowerCase() })}
            onKeyDown={(e) => e.key === "Enter" && load()} />
        </div>
        <Button disabled={!channel.trim() || loading} onClick={load}>
          {loading ? <Loader2 className="animate-spin" size={15} /> : <Gift size={15} />} Belohnungen laden
        </Button>
      </Card>

      {/* Reward catalogue: per-reward "Alle Accounts (Spam)" with the 3 controls */}
      {rewards.length > 0 && (
        <Card className="space-y-3">
          <div className="text-sm font-semibold text-zinc-300">
            Alle Accounts (Spam) — Einstellungen gelten für alle Accounts
          </div>
          {[...rewards].sort(byReward).map((r) => {
            const ready = readyCount(r);
            return (
              <div key={r.id} className="flex flex-wrap items-end gap-3 border-t border-zinc-800 pt-3">
                <div className="min-w-[150px] flex-1">
                  <div className="font-medium">{r.title}</div>
                  <div className="text-xs text-zinc-500">
                    {r.cost.toLocaleString()} P
                    {!r.isEnabled ? " · aus" : r.isPaused ? " · pausiert" : ""} ·{" "}
                    <span className={ready ? "text-emerald-400" : "text-zinc-500"}>
                      {ready}/{accounts.length} bereit
                    </span>
                  </div>
                </div>
                <div>
                  <label className="text-[11px] text-zinc-400">Anzahl (gesamt)</label>
                  <Input className="w-20" type="number" min={1}
                    placeholder="1"
                    value={allCount[r.id] ?? ""}
                    onChange={(e) => setAllCount((c) => ({ ...c, [r.id]: e.target.value }))}
                    onBlur={() => {
                      const nums = Object.fromEntries(
                        Object.entries({ ...allCount, [r.id]: allCount[r.id] })
                          .filter(([, v]) => Number(v) >= 1)
                          .map(([k, v]) => [k, Number(v)])
                      );
                      saveConfig({ counts: nums });
                    }} />
                </div>
                <div>
                  <label className="text-[11px] text-zinc-400">Delay/Account (s)</label>
                  <Input className="w-24" type="number" min={0}
                    value={String(cooldowns[r.id] ?? 0)}
                    onChange={(e) => setCooldowns((c) => ({ ...c, [r.id]: Number(e.target.value) || 0 }))}
                    onBlur={() => saveConfig({ cooldowns: { ...cooldowns, [r.id]: cooldowns[r.id] ?? 0 } })} />
                </div>
                <div>
                  <label className="text-[11px] text-zinc-400">Globaler Delay (s)</label>
                  <Input className="w-24" type="number" min={0} step="0.5"
                    value={String(masterDelays[r.id] ?? 0)}
                    onChange={(e) => setMasterDelays((m) => ({ ...m, [r.id]: Number(e.target.value) || 0 }))}
                    onBlur={() => saveConfig({ master_delays: { ...masterDelays, [r.id]: masterDelays[r.id] ?? 0 } })} />
                </div>
                {runs[r.id] ? (
                  <Button size="sm" variant="danger"
                    disabled={cancelling.has(r.id)}
                    onClick={() => doCancelAll(r.id)}>
                    {cancelling.has(r.id)
                      ? <Loader2 className="animate-spin" size={14} />
                      : <Square size={14} />}
                    {runs[r.id].fired}/{runs[r.id].count} {cancelling.has(r.id) ? "Stoppt…" : "Stop"}
                  </Button>
                ) : (
                  <Button size="sm" disabled={allBusy === r.id || !r.isEnabled || r.isPaused}
                    onClick={() => doRedeemAll(r.id)}>
                    {allBusy === r.id ? <Loader2 className="animate-spin" size={14} /> : <Users size={14} />}
                    Einlösen
                  </Button>
                )}
              </div>
            );
          })}
        </Card>
      )}

      {/* Per-account: balance + single redeem */}
      <div className="space-y-3">
        {accounts.map((a) => {
          const d = data[a.id];
          return (
            <Card key={a.id} className="flex flex-wrap items-center gap-3">
              <div className="min-w-[140px] flex-1">
                <div className="font-semibold">{a.username}</div>
                <div className="text-xs text-zinc-500">
                  {d ? ("error" in d ? <span className="text-red-400">{d.error}</span>
                    : <>Guthaben: <b className="text-emerald-400">{d.balance.toLocaleString()}</b> P</>)
                    : "—"}
                </div>
              </div>
              {d && !("error" in d) && (
                <>
                  <select
                    className="h-9 min-w-[180px] flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
                    value={sel[a.id] ?? ""}
                    onChange={(e) => setSel((s) => ({ ...s, [a.id]: e.target.value }))}>
                    <option value="">— Belohnung wählen —</option>
                    {[...d.rewards].sort(byReward).map((r) => (
                      <option key={r.id} value={r.id} disabled={!r.isEnabled || r.isPaused}>
                        {r.title} — {r.cost.toLocaleString()} P
                      </option>
                    ))}
                  </select>
                  <Input className="w-20" type="number" min={1} value={count[a.id] ?? "1"}
                    onChange={(e) => setCount((c) => ({ ...c, [a.id]: e.target.value }))} />
                  <Button size="sm" disabled={busy[a.id]} onClick={() => doRedeem(a.id)}>
                    {busy[a.id] ? <Loader2 className="animate-spin" size={14} /> : <Gift size={14} />} Einlösen
                  </Button>
                </>
              )}
            </Card>
          );
        })}
        {accounts.length === 0 && <Card className="text-zinc-400">Noch keine Accounts.</Card>}
      </div>

      {toast && (
        <div className="rounded-md border border-zinc-700 bg-zinc-900 p-3 text-sm" onClick={() => setToast(null)}>
          {toast} <span className="text-zinc-500">(klicken zum Schließen)</span>
        </div>
      )}
    </div>
  );
}
