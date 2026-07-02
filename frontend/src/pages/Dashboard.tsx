import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { Play, Square } from "lucide-react";
import { api, type Account } from "@/lib/api";
import { useJsonWs } from "@/lib/ws";
import { Button, Card, StatusBadge } from "@/components/ui";
import { fmtNumber, parseTs } from "@/lib/utils";

interface StatusMsg {
  type: "status";
  accounts: { id: number; username: string; status: string }[];
}

interface EventMsg {
  type: "event";
  account_id: number;
  event_type: string;
  balance: number | null;
  ts: string | null;
}

type Pt = { ts: string; balance: number };

export default function Dashboard() {
  const qc = useQueryClient();
  const { data: accounts = [] } = useQuery({
    queryKey: ["accounts"],
    queryFn: api.listAccounts,
  });
  const [live, setLive] = useState<Record<string, string>>({});
  // live points appended from the events stream (per account id) — no polling
  const [livePoints, setLivePoints] = useState<Record<number, Pt[]>>({});
  const [sortBy, setSortBy] = useState<"points_desc" | "points_asc" | "name" | "status">(
    "points_desc"
  );

  // initial balances (latest snapshot per account); live updates override below
  const { data: balances = [] } = useQuery({
    queryKey: ["balances"],
    queryFn: api.accountBalances,
  });

  useJsonWs<StatusMsg>("/ws/status", (msg) => {
    if (msg.type === "status") {
      const map: Record<string, string> = {};
      msg.accounts.forEach((a) => (map[a.username] = a.status));
      setLive((prev) => {
        // The backend pushes every 2s regardless of change. Bail out (return the
        // same object) when nothing actually changed, so we don't re-render every
        // AccountCard and rebuild every chart twice a second for no reason.
        const keys = Object.keys(map);
        if (
          keys.length === Object.keys(prev).length &&
          keys.every((k) => prev[k] === map[k])
        ) {
          return prev;
        }
        return map;
      });
    }
  });

  useJsonWs<EventMsg>("/ws/events", (msg) => {
    if (msg.type === "event" && msg.event_type === "points_snapshot" && msg.balance != null && msg.ts) {
      setLivePoints((prev) => {
        const arr = [...(prev[msg.account_id] ?? []), { ts: msg.ts!, balance: msg.balance! }];
        return { ...prev, [msg.account_id]: arr.slice(-500) };
      });
    }
  });

  const statusOf = (a: Account) => live[a.username] ?? a.status;
  const running = accounts.filter((a) => statusOf(a) === "running").length;

  // current balance per account: newest live point if present, else REST snapshot
  const balanceOf = (a: Account): number | null => {
    const arr = livePoints[a.id];
    if (arr && arr.length) return arr[arr.length - 1].balance;
    const b = balances.find((x) => x.account_id === a.id);
    return b ? b.balance : null;
  };
  const totalPoints = accounts.reduce((sum, a) => sum + (balanceOf(a) ?? 0), 0);

  const sortedAccounts = [...accounts].sort((a, b) => {
    switch (sortBy) {
      case "points_desc":
        return (balanceOf(b) ?? 0) - (balanceOf(a) ?? 0);
      case "points_asc":
        return (balanceOf(a) ?? 0) - (balanceOf(b) ?? 0);
      case "status":
        return statusOf(a).localeCompare(statusOf(b)) ||
          a.username.localeCompare(b.username);
      default:
        return a.username.localeCompare(b.username);
    }
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <div className="flex gap-2">
          <Button
            variant="success"
            onClick={async () => {
              await api.startAll();
              qc.invalidateQueries({ queryKey: ["accounts"] });
            }}
          >
            <Play size={15} /> Alle starten
          </Button>
          <Button
            variant="outline"
            onClick={async () => {
              await api.stopAll();
              qc.invalidateQueries({ queryKey: ["accounts"] });
            }}
          >
            <Square size={15} /> Alle stoppen
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
        <Stat label="Gesamt-Punkte" value={fmtNumber(totalPoints)} accent="text-brand" />
        <Stat label="Accounts" value={accounts.length} />
        <Stat label="Laufend" value={running} accent="text-emerald-400" />
        <Stat label="Gestoppt" value={accounts.length - running} />
        <Stat
          label="Login nötig"
          value={accounts.filter((a) => statusOf(a) === "needs_login").length}
          accent="text-amber-400"
        />
      </div>

      <div className="flex items-center justify-end gap-2 text-sm">
        <span className="text-zinc-400">Sortieren:</span>
        <select
          className="h-9 rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as typeof sortBy)}
        >
          <option value="points_desc">Punkte ↓</option>
          <option value="points_asc">Punkte ↑</option>
          <option value="name">Name (A–Z)</option>
          <option value="status">Status</option>
        </select>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {sortedAccounts.map((a) => (
          <AccountCard key={a.id} account={a} status={statusOf(a)} live={livePoints[a.id] ?? []} />
        ))}
        {accounts.length === 0 && (
          <Card className="text-zinc-400">
            Noch keine Accounts. Lege welche unter „Accounts" an.
          </Card>
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  accent,
}: {
  label: string;
  value: number | string;
  accent?: string;
}) {
  return (
    <Card>
      <div className="text-sm text-zinc-400">{label}</div>
      <div className={`mt-1 text-3xl font-bold ${accent ?? ""}`}>{value}</div>
    </Card>
  );
}

function AccountCard({ account, status, live }: { account: Account; status: string; live: Pt[] }) {
  // history loaded once; live updates arrive via the events WS (no polling)
  const { data: history = [] } = useQuery({
    queryKey: ["points", account.id],
    queryFn: () => api.accountPoints(account.id),
  });
  const seen = new Set(history.map((p) => p.ts));
  const merged = [...history, ...live.filter((p) => !seen.has(p.ts))];
  const chartData = merged.map((p) => ({
    t: p.ts ? parseTs(p.ts).toLocaleTimeString("de-DE") : "",
    balance: p.balance ?? 0,
  }));
  const latest = merged.length ? merged[merged.length - 1].balance : null;

  return (
    <Card>
      <div className="mb-2 flex items-center justify-between">
        <div className="font-semibold">{account.username}</div>
        <StatusBadge status={status} />
      </div>
      <div className="mb-2 text-sm text-zinc-400">
        Punkte aktuell:{" "}
        <span className="font-semibold text-zinc-100">{fmtNumber(latest)}</span>
      </div>
      <div className="h-28">
        {chartData.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
              <XAxis dataKey="t" hide />
              <YAxis hide domain={["auto", "auto"]} />
              <Tooltip
                contentStyle={{ background: "#18181b", border: "1px solid #3f3f46" }}
              />
              <Line
                type="monotone"
                dataKey="balance"
                stroke="#9147ff"
                strokeWidth={2}
                dot={false}
              />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-zinc-500">
            Noch keine Punkte-Daten
          </div>
        )}
      </div>
    </Card>
  );
}
