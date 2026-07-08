import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UserPlus } from "lucide-react";
import { api } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-sm text-zinc-300">{label}</span>
      {children}
      {hint && <span className="block text-xs text-zinc-500">{hint}</span>}
    </label>
  );
}

const ROLES = [
  ["heist_opener", "Heist-Opener"],
  ["heist_joiner", "Heist-Joiner"],
  ["chat_redeemer", "Chat-Einlösen"],
  ["web_redeemer", "Web-Einlösen"],
] as const;

type Roles = Record<(typeof ROLES)[number][0], boolean>;
const EMPTY_ROLES: Roles = {
  heist_opener: false,
  heist_joiner: false,
  chat_redeemer: false,
  web_redeemer: false,
};

export default function CreateAccount() {
  const qc = useQueryClient();
  const { data: proxies = [] } = useQuery({ queryKey: ["proxies"], queryFn: api.listProxies });

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [proxyId, setProxyId] = useState("");
  const [roles, setRoles] = useState<Roles>(EMPTY_ROLES);
  const [msg, setMsg] = useState<string | null>(null);
  const [created, setCreated] = useState<string[]>([]);

  const create = useMutation({
    mutationFn: () =>
      api.createAccount({
        username: username.trim(),
        password: password || undefined,
        email: email.trim() || undefined,
        proxy_id: proxyId ? Number(proxyId) : undefined,
        ...roles,
      }),
    onSuccess: (acc) => {
      setMsg(`✅ „${acc.username}" angelegt${proxyId ? " (Relay zugewiesen)" : ""}`);
      setCreated((c) => [acc.username, ...c].slice(0, 30));
      setUsername("");
      setEmail("");
      setPassword("");
      setProxyId("");
      setRoles(EMPTY_ROLES);
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["proxies"] });
    },
    onError: (e: unknown) => setMsg(`❌ ${(e as Error).message}`),
  });

  const full = proxies.filter((p) => p.account_count >= 5).length;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Account anlegen</h1>
      </div>

      <Card className="space-y-2 text-sm">
        <div className="font-semibold text-zinc-100">Vor dem Erstellen (gegen Verknüpfung)</div>
        <ul className="list-disc space-y-1 pl-5 text-zinc-400">
          <li>Eigenes <b>Anti-Detect-Profil</b> pro Account (eigener Fingerprint) — z.B. Dolphin Anty, Profil je Account recyceln.</li>
          <li><b>IPv6 aus</b> + WebRTC-Schutz an. Kontrolle: <code className="text-zinc-300">browserleaks.com/ip</code> zeigt nur deine IPv4.</li>
          <li>Eigene <b>E-Mail</b> je Account; bei Twitch <b>E-Mail-Verifizierung statt Telefon</b> wählen.</li>
          <li>Der zugewiesene <b>DE-Relay</b> wird die feste Mining-IP dieses Accounts (weniger „neuer Ort"-Prüfungen).</li>
        </ul>
      </Card>

      <Card className="space-y-3">
        <Field label="Twitch-Username">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="z.B. miner_xyz" />
        </Field>
        <Field label="E-Mail (optional, nur zur Übersicht)">
          <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="account@deine-domain.de" />
        </Field>
        <Field label="Passwort (optional, verschlüsselt gespeichert)">
          <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" />
        </Field>
        <Field label="DE-Relay (feste Mining-IP)" hint="Anzeige (belegt/5). Leer lassen = später zuweisen.">
          <select
            className="h-9 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
            value={proxyId}
            onChange={(e) => setProxyId(e.target.value)}
          >
            <option value="">— später zuweisen —</option>
            {proxies.map((p) => (
              <option key={p.id} value={p.id} disabled={p.account_count >= 5}>
                {p.name} ({p.account_count}/5){p.account_count >= 5 ? " — voll" : ""}
              </option>
            ))}
          </select>
        </Field>

        <div className="flex flex-wrap gap-4 pt-1">
          {ROLES.map(([k, l]) => (
            <label key={k} className="flex items-center gap-2 text-sm text-zinc-300">
              <input
                type="checkbox"
                checked={roles[k]}
                onChange={(e) => setRoles((r) => ({ ...r, [k]: e.target.checked }))}
              />
              {l}
            </label>
          ))}
        </div>

        <Button className="w-full" disabled={!username.trim() || create.isPending} onClick={() => create.mutate()}>
          <UserPlus size={16} /> {create.isPending ? "lege an…" : "Account anlegen"}
        </Button>
        {full > 0 && (
          <div className="text-xs text-amber-400">{full} Relay(s) voll (5/5) — für weitere Accounts neue DE-Relays importieren.</div>
        )}
      </Card>

      {created.length > 0 && (
        <Card className="space-y-2 text-sm">
          <div className="font-semibold text-zinc-100">In dieser Sitzung angelegt ({created.length})</div>
          <div className="flex flex-wrap gap-2">
            {created.map((u, i) => (
              <span key={i} className="rounded bg-zinc-800 px-2 py-0.5 text-zinc-300">{u}</span>
            ))}
          </div>
        </Card>
      )}

      {msg && (
        <div
          className="cursor-pointer rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200"
          onClick={() => setMsg(null)}
        >
          {msg}
        </div>
      )}
    </div>
  );
}
