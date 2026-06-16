import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { Play, Square } from "lucide-react";
import { api, type Account } from "@/lib/api";
import { useJsonWs } from "@/lib/ws";
import { Button, Card, StatusBadge } from "@/components/ui";
import { fmtNumber } from "@/lib/utils";

interface StatusMsg {
  type: "status";
  accounts: { id: number; username: string; status: string }[];
}

export default function Dashboard() {
  const qc = useQueryClient();
  const { data: accounts = [] } = useQuery({
    queryKey: ["accounts"],
    queryFn: api.listAccounts,
  });
  const [live, setLive] = useState<Record<string, string>>({});

  useJsonWs<StatusMsg>("/ws/status", (msg) => {
    if (msg.type === "status") {
      const map: Record<string, string> = {};
      msg.accounts.forEach((a) => (map[a.username] = a.status));
      setLive(map);
    }
  });

  const statusOf = (a: Account) => live[a.username] ?? a.status;
  const running = accounts.filter((a) => statusOf(a) === "running").length;

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

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Accounts" value={accounts.length} />
        <Stat label="Laufend" value={running} accent="text-emerald-400" />
        <Stat label="Gestoppt" value={accounts.length - running} />
        <Stat
          label="Login nötig"
          value={accounts.filter((a) => statusOf(a) === "needs_login").length}
          accent="text-amber-400"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {accounts.map((a) => (
          <AccountCard key={a.id} account={a} status={statusOf(a)} />
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
  value: number;
  accent?: string;
}) {
  return (
    <Card>
      <div className="text-sm text-zinc-400">{label}</div>
      <div className={`mt-1 text-3xl font-bold ${accent ?? ""}`}>{value}</div>
    </Card>
  );
}

function AccountCard({ account, status }: { account: Account; status: string }) {
  const { data: points = [] } = useQuery({
    queryKey: ["points", account.id],
    queryFn: () => api.accountPoints(account.id),
    refetchInterval: 30000,
  });
  const chartData = points.map((p) => ({
    t: p.ts ? new Date(p.ts).toLocaleTimeString("de-DE") : "",
    balance: p.balance ?? 0,
  }));
  const latest = points.length ? points[points.length - 1].balance : null;

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
