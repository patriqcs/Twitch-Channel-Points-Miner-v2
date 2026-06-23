import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, Square, RotateCw, Trash2, LogIn, Plus, KeyRound, Cookie, Copy } from "lucide-react";
import { api, type Account, type LoginStart } from "@/lib/api";
import { Button, Card, Input, Modal, StatusBadge } from "@/components/ui";
import { fmtTime } from "@/lib/utils";

export default function Accounts() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["accounts"] });

  const { data: accounts = [] } = useQuery({ queryKey: ["accounts"], queryFn: api.listAccounts });
  const { data: proxies = [] } = useQuery({ queryKey: ["proxies"], queryFn: api.listProxies });

  const [showAdd, setShowAdd] = useState(false);
  const [newUser, setNewUser] = useState("");
  const [newProxy, setNewProxy] = useState<string>("");
  const [login, setLogin] = useState<{ account: Account; data: LoginStart } | null>(null);
  const [token, setToken] = useState<{ username: string; value: string | null; error: string | null } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      api.createAccount({
        username: newUser.trim(),
        proxy_id: newProxy ? Number(newProxy) : null,
      }),
    onSuccess: () => {
      setShowAdd(false);
      setNewUser("");
      setNewProxy("");
      invalidate();
    },
    onError: (e: Error) => setErr(e.message),
  });

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Accounts</h1>
        <Button onClick={() => setShowAdd(true)}>
          <Plus size={15} /> Account
        </Button>
      </div>

      <div className="space-y-3">
        {accounts.map((a) => (
          <Card key={a.id} className="flex flex-wrap items-center gap-3">
            <div className="min-w-[140px] flex-1">
              <div className="font-semibold">{a.username}</div>
              <div className="text-xs text-zinc-500">
                Letzter Login: {fmtTime(a.last_login_at)}
              </div>
            </div>

            <StatusBadge status={a.status} />

            <select
              className="h-9 rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
              value={a.proxy_id ?? ""}
              onChange={async (e) => {
                try {
                  await api.updateAccount(a.id, {
                    proxy_id: e.target.value ? Number(e.target.value) : null,
                  });
                  qc.invalidateQueries({ queryKey: ["accounts"] });
                  qc.invalidateQueries({ queryKey: ["proxies"] });
                } catch (er) {
                  setErr((er as Error).message);
                }
              }}
            >
              <option value="">— kein Proxy —</option>
              {proxies.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.account_count}/5)
                </option>
              ))}
            </select>

            <div className="flex gap-3 text-xs text-zinc-400">
              <label className="flex items-center gap-1.5" title="Öffnet Heists mit !heist">
                <input
                  type="checkbox"
                  checked={a.heist_opener}
                  onChange={async (e) => {
                    try {
                      await api.updateAccount(a.id, { heist_opener: e.target.checked });
                      invalidate();
                    } catch (er) { setErr((er as Error).message); }
                  }}
                />
                Opener
              </label>
              <label className="flex items-center gap-1.5" title="Tritt Heists mit !join bei">
                <input
                  type="checkbox"
                  checked={a.heist_joiner}
                  onChange={async (e) => {
                    try {
                      await api.updateAccount(a.id, { heist_joiner: e.target.checked });
                      invalidate();
                    } catch (er) { setErr((er as Error).message); }
                  }}
                />
                Joiner
              </label>
            </div>

            <div className="flex gap-1.5">
              <Button size="sm" variant="success" title="Start"
                onClick={async () => { await api.startAccount(a.id); invalidate(); }}>
                <Play size={14} />
              </Button>
              <Button size="sm" variant="outline" title="Stop"
                onClick={async () => { await api.stopAccount(a.id); invalidate(); }}>
                <Square size={14} />
              </Button>
              <Button size="sm" variant="ghost" title="Neustart"
                onClick={async () => { await api.restartAccount(a.id); invalidate(); }}>
                <RotateCw size={14} />
              </Button>
              <Button size="sm" variant="outline" title="Login (Device-Code)"
                onClick={async () => {
                  try {
                    const data = await api.startLogin(a.id);
                    setLogin({ account: a, data });
                  } catch (er) { setErr((er as Error).message); }
                }}>
                <LogIn size={14} />
              </Button>
              <Button size="sm" variant="ghost" title="Login testen"
                onClick={async () => {
                  const r = await api.loginTest(a.id);
                  setErr(r.ok ? "✅ Login gültig" : `❌ ${r.error}`);
                }}>
                <KeyRound size={14} />
              </Button>
              <Button size="sm" variant="ghost" title="auth-token anzeigen"
                onClick={async () => {
                  try {
                    const r = await api.authToken(a.id);
                    setToken({ username: a.username, value: r.auth_token, error: r.error });
                  } catch (er) { setErr((er as Error).message); }
                }}>
                <Cookie size={14} />
              </Button>
              <Button size="sm" variant="danger" title="Löschen"
                onClick={async () => {
                  if (confirm(`Account ${a.username} löschen?`)) {
                    await api.deleteAccount(a.id); invalidate();
                  }
                }}>
                <Trash2 size={14} />
              </Button>
            </div>
          </Card>
        ))}
        {accounts.length === 0 && <Card className="text-zinc-400">Noch keine Accounts.</Card>}
      </div>

      {err && (
        <div className="rounded-md border border-zinc-700 bg-zinc-900 p-3 text-sm" onClick={() => setErr(null)}>
          {err} <span className="text-zinc-500">(klicken zum Schließen)</span>
        </div>
      )}

      <Modal open={showAdd} onClose={() => setShowAdd(false)} title="Account hinzufügen">
        <div className="space-y-3">
          <Input placeholder="Twitch-Username" value={newUser} onChange={(e) => setNewUser(e.target.value)} />
          <select
            className="h-9 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2 text-sm"
            value={newProxy}
            onChange={(e) => setNewProxy(e.target.value)}
          >
            <option value="">— kein Proxy —</option>
            {proxies.map((p) => (
              <option key={p.id} value={p.id}>{p.name} ({p.account_count}/5)</option>
            ))}
          </select>
          <Button className="w-full" disabled={!newUser.trim() || create.isPending}
            onClick={() => create.mutate()}>
            Anlegen
          </Button>
        </div>
      </Modal>

      {login && <LoginModal info={login} onClose={() => { setLogin(null); invalidate(); }} />}

      {token && (
        <Modal open onClose={() => setToken(null)} title={`auth-token: ${token.username}`}>
          <div className="space-y-3">
            {token.value ? (
              <>
                <p className="text-xs text-amber-400">
                  ⚠️ Sensibel — das ist ein vollwertiger Account-Zugang. Nicht öffentlich teilen.
                </p>
                <textarea
                  readOnly
                  className="h-24 w-full break-all rounded-md border border-zinc-700 bg-zinc-950 p-2 font-mono text-xs"
                  value={token.value}
                  onFocus={(e) => e.currentTarget.select()}
                />
                <Button className="w-full" variant="outline"
                  onClick={() => { navigator.clipboard?.writeText(token.value!); setErr("auth-token kopiert"); }}>
                  <Copy size={14} /> Kopieren
                </Button>
              </>
            ) : (
              <p className="text-red-400">❌ {token.error ?? "kein auth-token gefunden"}</p>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}

function LoginModal({
  info,
  onClose,
}: {
  info: { account: Account; data: LoginStart };
  onClose: () => void;
}) {
  const [status, setStatus] = useState(info.data.status);

  useEffect(() => {
    const t = setInterval(async () => {
      const r = await api.loginStatus(info.account.id);
      setStatus(r.status);
      if (["authorized", "expired", "error"].includes(r.status)) clearInterval(t);
    }, 3000);
    return () => clearInterval(t);
  }, [info.account.id]);

  return (
    <Modal open onClose={onClose} title={`Login: ${info.account.username}`}>
      {status === "authorized" ? (
        <div className="text-emerald-400">✅ Erfolgreich eingeloggt! Cookie gespeichert.</div>
      ) : status === "expired" ? (
        <div className="text-red-400">⏰ Code abgelaufen. Bitte erneut versuchen.</div>
      ) : status === "error" ? (
        <div className="text-red-400">❌ Fehler beim Login.</div>
      ) : (
        <div className="space-y-3 text-sm">
          <p>
            1. Öffne{" "}
            <a className="text-brand underline" href={info.data.verification_uri} target="_blank">
              {info.data.verification_uri}
            </a>{" "}
            (eingeloggt als <b>{info.account.username}</b>).
          </p>
          <p>2. Gib diesen Code ein:</p>
          <div className="rounded-md bg-zinc-950 p-3 text-center text-2xl font-bold tracking-widest">
            {info.data.user_code}
          </div>
          <p className="text-zinc-400">Warte auf Bestätigung… (Status: {status})</p>
        </div>
      )}
      <Button className="mt-4 w-full" variant="outline" onClick={onClose}>
        Schließen
      </Button>
    </Modal>
  );
}
