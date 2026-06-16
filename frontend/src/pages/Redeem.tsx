import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Gift, Loader2 } from "lucide-react";
import { api, type Reward } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";

type Loaded = {
  balance: number;
  displayName: string;
  rewards: Reward[];
} | { error: string };

export default function Redeem() {
  const { data: accounts = [] } = useQuery({ queryKey: ["accounts"], queryFn: api.listAccounts });
  const [channel, setChannel] = useState("");
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<Record<number, Loaded>>({});
  const [sel, setSel] = useState<Record<number, string>>({});      // account -> reward_id
  const [count, setCount] = useState<Record<number, string>>({});  // account -> count
  const [busy, setBusy] = useState<Record<number, boolean>>({});
  const [toast, setToast] = useState<string | null>(null);

  const load = async () => {
    const ch = channel.trim().toLowerCase();
    if (!ch) return;
    setLoading(true);
    setData({});
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
    setData(Object.fromEntries(entries));
    setLoading(false);
  };

  const doRedeem = async (accountId: number) => {
    const ch = channel.trim().toLowerCase();
    const rewardId = sel[accountId];
    if (!rewardId) { setToast("Bitte erst eine Belohnung wählen."); return; }
    setBusy((b) => ({ ...b, [accountId]: true }));
    try {
      const r = await api.redeem(accountId, {
        channel: ch, reward_id: rewardId, count: Number(count[accountId]) || 1,
      });
      const fail = r.results.find((x) => !x.ok);
      setToast(`${r.reward}: ${r.succeeded}/${r.attempted} eingelöst${fail ? ` — ${fail.message}` : ""}`);
      // refresh this account's balance
      try {
        const cp = await api.channelPoints(accountId, ch);
        setData((d) => ({ ...d, [accountId]: { balance: cp.balance, displayName: cp.displayName, rewards: cp.rewards } }));
      } catch { /* ignore */ }
    } catch (e) {
      setToast((e as Error).message);
    } finally {
      setBusy((b) => ({ ...b, [accountId]: false }));
    }
  };

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-bold">Einlösen</h1>
      <p className="text-sm text-zinc-400">
        Channel-Points eines Senders pro Account einlösen — jeweils über den Proxy
        und Login des Accounts. Punkte werden <b>ausgegeben</b> (nicht gesammelt).
      </p>

      <Card className="flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-[200px]">
          <label className="text-xs text-zinc-400">Channel (Twitch-Login)</label>
          <Input placeholder="z. B. j4nkttv" value={channel}
            onChange={(e) => setChannel(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && load()} />
        </div>
        <Button disabled={!channel.trim() || loading} onClick={load}>
          {loading ? <Loader2 className="animate-spin" size={15} /> : <Gift size={15} />} Belohnungen laden
        </Button>
      </Card>

      <div className="space-y-3">
        {accounts.map((a) => {
          const d = data[a.id];
          return (
            <Card key={a.id} className="flex flex-wrap items-center gap-3">
              <div className="min-w-[140px] flex-1">
                <div className="font-semibold">{a.username}</div>
                <div className="text-xs text-zinc-500">
                  {d ? ("error" in d ? <span className="text-red-400">{d.error}</span>
                    : <>Guthaben: <b className="text-emerald-400">{d.balance.toLocaleString()}</b> Punkte</>)
                    : "—"}
                </div>
              </div>

              {d && !("error" in d) && (
                <>
                  <select
                    className="h-9 min-w-[200px] flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
                    value={sel[a.id] ?? ""}
                    onChange={(e) => setSel((s) => ({ ...s, [a.id]: e.target.value }))}>
                    <option value="">— Belohnung wählen —</option>
                    {d.rewards.map((r) => (
                      <option key={r.id} value={r.id} disabled={!r.isEnabled || r.isPaused}>
                        {r.title} — {r.cost.toLocaleString()} P
                        {!r.isEnabled ? " (aus)" : r.isPaused ? " (pausiert)" : ""}
                      </option>
                    ))}
                  </select>
                  <Input className="w-20" type="number" min={1} placeholder="Anzahl"
                    value={count[a.id] ?? "1"}
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
        <div className="rounded-md border border-zinc-700 bg-zinc-900 p-3 text-sm"
          onClick={() => setToast(null)}>
          {toast} <span className="text-zinc-500">(klicken zum Schließen)</span>
        </div>
      )}
    </div>
  );
}
