// Typed REST client for the backend.

export interface Account {
  id: number;
  username: string;
  enabled: boolean;
  status: string;
  proxy_id: number | null;
  has_password: boolean;
  no_proxy: boolean;
  heist_opener: boolean;
  heist_joiner: boolean;
  chat_redeemer: boolean;
  web_redeemer: boolean;
  signup_email: string | null;
  created_at: string;
  last_login_at: string | null;
}

export interface ChatRedeemCommand {
  command: string;
  reward_id: string;
  reward_title?: string;
  cooldown?: number;
  enabled: boolean;
}

export interface ChatRedeemConfig {
  enabled: boolean;
  channel: string;
  announcer: string;
  commands: ChatRedeemCommand[];
  on_text: string;
  off_text: string;
}

export interface ChatRedeemStatus {
  runtime: {
    active: boolean;
    reason: string;
    observer_connected: boolean;
    announcer: string | null;
    channel: string | null;
    balances: Record<string, number>;
    last_triggers: { command: string; nick: string; ok: boolean; message: string; age: number }[];
  };
  config: ChatRedeemConfig;
  redeemers: { id: number; username: string; logged_in: boolean; balance: number | null }[];
}

export interface WebRedeemItem {
  reward_id: string;
  label?: string;
  reward_title?: string;
  description?: string;
  cooldown?: number;
  enabled: boolean;
}

export interface WebRedeemConfig {
  enabled: boolean;
  public: boolean;
  channel: string;
  items: WebRedeemItem[];
  title: string;
  tagline: string;
  offline_text: string;
  announce: boolean;
  announcer: string;
  announce_text: string;
}

export interface WebUserRow {
  id: number;
  username: string;
  must_change_password: boolean;
  approved: boolean;
  created_at: string;
  last_seen_at: string | null;
  generated_password?: string;
}

export interface WebRedeemStatus {
  runtime: {
    enabled: boolean;
    reason: string;
    channel: string | null;
    channel_display: string | null;
    catalog_loaded: boolean;
    announce: boolean;
    announcer: string | null;
    announcer_connected: boolean;
    balances: Record<string, number>;
    last_triggers: { label: string; visitor: string; ok: boolean; message: string; age: number }[];
  };
  config: WebRedeemConfig;
  redeemers: { id: number; username: string; logged_in: boolean; balance: number | null }[];
}

export interface HeistConfig {
  enabled: boolean;
  channel: string;
  bot: string;
  trigger_regex: string;
  end_regex: string;
  reject_regex: string;
  start_command: string;
  join_command: string;
  start_cooldown: number;
  spacing_min: number;
  spacing_max: number;
  join_delay_ms: number;
}

export interface HeistStatus {
  runtime: {
    online: boolean | null;
    observer_connected: boolean;
    observer_account_id: number | null;
    observer_username: string | null;
    heist_active: boolean;
    pending_open: {
      account_id: number;
      username: string;
      confirmed: boolean;
      age: number;
    } | null;
    next_open_in: number;
    cooldowns: { account_id: number; remaining: number }[];
  };
  config: HeistConfig;
  openers: { id: number; username: string; logged_in: boolean }[];
  joiners: { id: number; username: string; logged_in: boolean }[];
}

export interface PredictionOutcome {
  id: string;
  title: string;
  color: string; // "BLUE" | "PINK"
  total_points: number;
  total_users: number;
}

export interface PredictionEvent {
  id: string;
  title: string;
  status: string; // ACTIVE | LOCKED | ...
  created_at: string;
  window_seconds: number;
  locks_in: number | null; // Sekunden bis zur Sperre (Server-Snapshot)
  locks_at_epoch: number | null;
  outcomes: PredictionOutcome[];
}

export interface PredictionActive {
  channel: string;
  channel_id: string | null;
  display_name: string;
  event: PredictionEvent | null;
  accounts: { id: number; username: string; logged_in: boolean }[];
}

export interface PredictionBalances {
  channel: string;
  accounts: { id: number; username: string; logged_in: boolean; balance: number | null; error: string | null }[];
  total_balance: number;
}

export interface PredictionConfig {
  channel: string;
  exclude: string;
  spacing_min: number;
  spacing_max: number;
  bet_pct_min: number;
  bet_pct_max: number;
}

export interface PredictionRun {
  run_id: string;
  channel: string;
  event_id: string;
  event_title: string;
  outcome_id: string;
  outcome_title: string;
  locks_at_epoch: number | null;
  started_at: number;
  done: boolean;
  cancelled: boolean;
  results: {
    account_id: number;
    username: string;
    status: "waiting" | "betting" | "ok" | "skipped" | "failed" | "tos_blocked";
    balance: number | null;
    points: number | null;
    message: string;
    code: string | null;
  }[];
}

export interface Proxy {
  id: number;
  name: string;
  scheme: string;
  host: string;
  port: number;
  username: string | null;
  has_password: boolean;
  account_count: number;
  created_at: string;
}

export interface ProxyTestResult {
  ok: boolean;
  ip: string | null;
  latency_ms: number | null;
  error: string | null;
}

export interface ProxyImportResult {
  added: number;
  skipped_duplicate: number;
  skipped_offline: number;
  failed: number;
  errors: { line: number; value: string; error: string }[];
  proxies: Proxy[];
}

export interface Reward {
  id: string;
  title: string;
  cost: number;
  isEnabled: boolean;
  isPaused: boolean;
  isInStock: boolean;
  isUserInputRequired: boolean;
  prompt: string;
}

export interface EventRow {
  id: number;
  type: string;
  streamer: string | null;
  points: number | null;
  balance: number | null;
  reason: string | null;
  message: string | null;
  ts: string | null;
}

export interface LoginStart {
  status: string;
  user_code: string | null;
  verification_uri: string;
  expires_at: string | null;
}

export interface CoverConfig {
  enabled: boolean;
  pool: string[];
  raw: string;
  count: number;
  max_count: number;
  default_pool: string[];
  offline_presence: number;
  offline_hours: number;
  max_offline_presence: number;
  max_offline_hours: number;
  exclude: string;
}

async function req<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      const d = body?.detail ?? detail;
      // FastAPI validation (422) returns `detail` as an array of
      // {loc, msg, type} objects; join their messages instead of rendering
      // "[object Object]".
      if (Array.isArray(d)) {
        detail = d
          .map((e) => (typeof e === "string" ? e : e?.msg ?? JSON.stringify(e)))
          .join("; ");
      } else if (typeof d === "string") {
        detail = d;
      } else if (d != null) {
        detail = JSON.stringify(d);
      }
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  // accounts
  listAccounts: () => req<Account[]>("/api/accounts"),
  createAccount: (b: Partial<Account> & { username: string; password?: string; email?: string }) =>
    req<Account>("/api/accounts", { method: "POST", body: JSON.stringify(b) }),
  updateAccount: (id: number, b: Record<string, unknown>) =>
    req<Account>(`/api/accounts/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
  renameAccount: (id: number, username: string) =>
    req<Account>(`/api/accounts/${id}/rename`, {
      method: "POST",
      body: JSON.stringify({ username }),
    }),
  deleteAccount: (id: number) =>
    req<void>(`/api/accounts/${id}`, { method: "DELETE" }),
  startAccount: (id: number) =>
    req<{ started: boolean }>(`/api/accounts/${id}/start`, { method: "POST" }),
  stopAccount: (id: number) =>
    req<{ stopped: boolean }>(`/api/accounts/${id}/stop`, { method: "POST" }),
  restartAccount: (id: number) =>
    req<unknown>(`/api/accounts/${id}/restart`, { method: "POST" }),
  startLogin: (id: number) =>
    req<LoginStart>(`/api/accounts/${id}/login`, { method: "POST" }),
  loginStatus: (id: number) =>
    req<{ status: string; user_code: string | null; verification_uri: string; error: string | null }>(
      `/api/accounts/${id}/login/status`
    ),
  loginTest: (id: number) =>
    req<{ ok: boolean; error: string | null }>(`/api/accounts/${id}/login-test`, {
      method: "POST",
    }),
  authToken: (id: number) =>
    req<{ auth_token: string | null; error: string | null }>(
      `/api/accounts/${id}/auth-token`
    ),
  accountPoints: (id: number) =>
    req<{ ts: string; balance: number | null }[]>(`/api/accounts/${id}/points`),
  accountBalances: () =>
    req<{ account_id: number; username: string; balance: number | null }[]>(
      "/api/accounts/balances"
    ),
  accountEvents: (id: number) =>
    req<EventRow[]>(`/api/accounts/${id}/events`),

  // proxies
  listProxies: () => req<Proxy[]>("/api/proxies"),
  createProxy: (b: Record<string, unknown>) =>
    req<Proxy>("/api/proxies", { method: "POST", body: JSON.stringify(b) }),
  importProxies: (text: string, testBeforeAdd: boolean) =>
    req<ProxyImportResult>("/api/proxies/import", {
      method: "POST",
      body: JSON.stringify({ text, test_before_add: testBeforeAdd }),
    }),
  importMullvad: (body: { country_code?: string; limit: number; daita_only: boolean }) =>
    req<ProxyImportResult>("/api/proxies/mullvad-import", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  testAllProxies: () =>
    req<(ProxyTestResult & { id: number; name: string })[]>("/api/proxies/test-all", {
      method: "POST",
    }),
  bulkDeleteProxies: (ids: number[]) =>
    req<{ deleted: number; skipped_in_use: number }>("/api/proxies/bulk-delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  updateProxy: (id: number, b: Record<string, unknown>) =>
    req<Proxy>(`/api/proxies/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
  deleteProxy: (id: number) =>
    req<void>(`/api/proxies/${id}`, { method: "DELETE" }),
  testProxy: (id: number) =>
    req<ProxyTestResult>(`/api/proxies/${id}/test`, { method: "POST" }),

  // redeem (spend channel points on a custom reward, per account, via its proxy)
  channelPoints: (id: number, channel: string) =>
    req<{ channelId: string; displayName: string; balance: number; rewards: Reward[] }>(
      `/api/redeem/${id}/channel-points?channel=${encodeURIComponent(channel)}`
    ),
  redeem: (id: number, body: { channel: string; reward_id: string; count: number; prompt?: string }) =>
    req<{ reward: string; attempted: number; succeeded: number; results: { ok: boolean; message?: string }[] }>(
      `/api/redeem/${id}`,
      { method: "POST", body: JSON.stringify(body) }
    ),
  redeemAll: (body: { channel: string; reward_id: string; count?: number; global_delay?: number }) =>
    req<{ reward: string; accounts: number; scheduled: number; global_delay: number; run_id: string }>(
      "/api/redeem/all",
      { method: "POST", body: JSON.stringify(body) }
    ),
  redeemAllStatus: () =>
    req<{ run_id: string; reward_id: string; title: string; fired: number; count: number }[]>(
      "/api/redeem/all/status"
    ),
  cancelRedeemAll: (body: { reward_id?: string; run_id?: string }) =>
    req<{ cancelled: number }>("/api/redeem/all/cancel", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getCooldowns: () =>
    req<{ account_id: number; reward_id: string; remaining: number }[]>("/api/redeem/cooldowns"),
  getRedeemConfig: () =>
    req<{ channel: string; cooldowns: Record<string, number>; master_delays: Record<string, number>; counts: Record<string, number>; all_delay: number }>(
      "/api/redeem/config"
    ),
  putRedeemConfig: (body: { channel?: string; cooldowns?: Record<string, number>; master_delays?: Record<string, number>; counts?: Record<string, number>; all_delay?: number }) =>
    req<{ channel: string; cooldowns: Record<string, number>; master_delays: Record<string, number>; counts: Record<string, number>; all_delay: number }>(
      "/api/redeem/config",
      { method: "PUT", body: JSON.stringify(body) }
    ),

  // heist (open heists with opener accounts, join them with joiner accounts)
  getHeistConfig: () => req<HeistConfig>("/api/heist/config"),
  putHeistConfig: (body: Partial<HeistConfig>) =>
    req<HeistConfig>("/api/heist/config", { method: "PUT", body: JSON.stringify(body) }),
  getHeistStatus: () => req<HeistStatus>("/api/heist/status"),
  heistTest: (id: number, command?: string) =>
    req<{ ok: boolean; username: string; channel: string; command: string }>(
      `/api/heist/test/${id}`,
      { method: "POST", body: JSON.stringify({ command: command ?? null }) }
    ),
  setHeistCooldown: (id: number, seconds?: number) =>
    req<{ account_id: number; remaining: number }>(`/api/heist/cooldown/${id}`, {
      method: "POST",
      body: JSON.stringify({ seconds: seconds ?? null }),
    }),

  // predictions (all-in channel-point bets with all accounts except the exclude list)
  getPredictionConfig: () => req<PredictionConfig>("/api/predictions/config"),
  putPredictionConfig: (body: Partial<PredictionConfig>) =>
    req<PredictionConfig>("/api/predictions/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getActivePrediction: (channel: string) =>
    req<PredictionActive>(
      `/api/predictions/active?channel=${encodeURIComponent(channel)}`
    ),
  getPredictionBalances: (channel: string) =>
    req<PredictionBalances>(
      `/api/predictions/balances?channel=${encodeURIComponent(channel)}`
    ),
  startPredictionBet: (body: { channel: string; event_id: string; outcome_id: string }) =>
    req<{ run_id: string; accounts: number; outcome: string; event: string }>(
      "/api/predictions/bet",
      { method: "POST", body: JSON.stringify(body) }
    ),
  getPredictionRun: () => req<PredictionRun | null>("/api/predictions/run"),
  cancelPredictionRun: () =>
    req<{ cancelled: boolean }>("/api/predictions/cancel", { method: "POST" }),

  // chat-redeem (viewers trigger reward redemptions by typing chat commands)
  getChatRedeemConfig: () => req<ChatRedeemConfig>("/api/chat-redeem/config"),
  putChatRedeemConfig: (body: Partial<Pick<ChatRedeemConfig, "enabled" | "channel" | "announcer" | "commands" | "on_text" | "off_text">>) =>
    req<ChatRedeemConfig>("/api/chat-redeem/config", { method: "PUT", body: JSON.stringify(body) }),
  getChatRedeemStatus: () => req<ChatRedeemStatus>("/api/chat-redeem/status"),
  announceChatRedeem: () =>
    req<{ ok: boolean; text?: string }>("/api/chat-redeem/announce", { method: "POST" }),
  getChatRedeemRewards: (channel: string) =>
    req<{ channelId: string; displayName: string; balance: number; rewards: Reward[] }>(
      `/api/chat-redeem/rewards?channel=${encodeURIComponent(channel)}`
    ),
  testChatRedeem: (message?: string) =>
    req<{
      via_proxy: boolean; joined: boolean; sent: boolean;
      connect_error: string | null; notice_error: string | null;
      msg_error: string | null; send_error: string | null;
      channel: string; announcer: string;
    }>("/api/chat-redeem/test", { method: "POST", body: JSON.stringify({ message: message ?? null }) }),

  // web-redeem (visitors trigger reward redemptions from the public website)
  getWebRedeemConfig: () => req<WebRedeemConfig>("/api/web-redeem/config"),
  putWebRedeemConfig: (body: Partial<Pick<WebRedeemConfig, "enabled" | "public" | "channel" | "items" | "title" | "tagline" | "offline_text" | "announce" | "announcer" | "announce_text">>) =>
    req<WebRedeemConfig>("/api/web-redeem/config", { method: "PUT", body: JSON.stringify(body) }),
  getWebRedeemStatus: () => req<WebRedeemStatus>("/api/web-redeem/status"),
  getWebRedeemRewards: (channel: string) =>
    req<{ channelId: string; displayName: string; balance: number; rewards: Reward[] }>(
      `/api/web-redeem/rewards?channel=${encodeURIComponent(channel)}`
    ),
  getWebRedeemToken: () => req<{ token: string }>("/api/web-redeem/token"),
  listWebUsers: () => req<WebUserRow[]>("/api/web-redeem/users"),
  createWebUser: (body: { username: string; password?: string }) =>
    req<WebUserRow>("/api/web-redeem/users", { method: "POST", body: JSON.stringify(body) }),
  approveWebUser: (id: number) =>
    req<WebUserRow>(`/api/web-redeem/users/${id}/approve`, { method: "POST" }),
  resetWebUser: (id: number, password?: string) =>
    req<WebUserRow>(`/api/web-redeem/users/${id}/reset`, {
      method: "POST", body: JSON.stringify({ password: password ?? null }),
    }),
  deleteWebUser: (id: number) =>
    req<void>(`/api/web-redeem/users/${id}`, { method: "DELETE" }),

  // settings
  getStreamers: () => req<{ streamers: string[]; raw: string }>("/api/settings/streamers"),
  putStreamers: (value: string) =>
    req<{ ok: boolean }>("/api/settings/streamers", {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  getCover: () =>
    req<CoverConfig>("/api/settings/cover"),
  putCover: (body: { enabled?: boolean; raw?: string; count?: number; offline_presence?: number; offline_hours?: number; exclude?: string }) =>
    req<CoverConfig>("/api/settings/cover", { method: "PUT", body: JSON.stringify(body) }),
  getDiurnal: () =>
    req<{ enabled: boolean; sleep_hours: number; max_sleep_hours: number }>("/api/settings/diurnal"),
  putDiurnal: (body: { enabled?: boolean; sleep_hours?: number }) =>
    req<{ enabled: boolean; sleep_hours: number; max_sleep_hours: number }>(
      "/api/settings/diurnal",
      { method: "PUT", body: JSON.stringify(body) }
    ),

  // system
  startAll: () => req<{ started: string[] }>("/api/system/start-all", { method: "POST" }),
  stopAll: () => req<{ stopped: string[] }>("/api/system/stop-all", { method: "POST" }),
};
