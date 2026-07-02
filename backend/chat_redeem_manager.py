# -*- coding: utf-8 -*-
"""Long-lived coordinator that drives the chat-command redeemer.

One background thread (started in the FastAPI lifespan, like the heist/proxy/
watch monitors) that, while the module is enabled:

  * keeps a persistent chat connection open as the *announcer* account (reads
    every public chat message in the configured channel);
  * when a viewer types a configured command (e.g. ``!flash``), redeems the
    mapped reward using the earliest-free ``chat_redeemer`` account that has the
    MOST points (respecting a per-command cooldown so one command can't fire on
    every chat line);
  * posts a chat message FROM the announcer when the module is switched ON
    (listing the active commands) and again when it's switched OFF.

Account balances + the channel's reward catalogue are cached and refreshed
periodically so a command can be served without a per-message GraphQL round trip.
State is in-memory and resets on restart (the on/off announce re-fires on a fresh
enable). The redemption itself reuses ``backend/redeem.py``; the IRC plumbing
reuses ``heist.HeistIRC``.
"""
import logging
import threading
import time

from sqlmodel import Session

from backend import chat_redeem, heist, redeem
from backend.db import engine
from backend.models import Event

logger = logging.getLogger("backend.chat_redeem_manager")


class ChatRedeemManager(threading.Thread):
    # On a command hit we reserve only this short window up front (so a burst of
    # the same command in the same instant spawns one worker); the FULL per-
    # command cooldown is applied only after an actual redemption.
    BURST_DEDUPE = 2.0
    # When a command can't fire yet (global spacing or all accounts on cooldown)
    # it is QUEUED and fired as soon as it's free — but never waits longer than
    # this (a runaway-cooldown backstop).
    MAX_QUEUE_WAIT = 300.0

    def __init__(self, poll_interval: float = 3.0, balance_refresh: float = 45.0):
        super().__init__(name="chat-redeem-manager", daemon=True)
        self.poll_interval = poll_interval
        self.balance_refresh = balance_refresh
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # config snapshot (refreshed each tick)
        self._cfg: dict = {"enabled": False, "channel": "", "announcer": "",
                           "commands": []}
        self._commands: dict = {}          # command token -> mapping entry

        # observer (announcer) connection
        self._observer: "heist.HeistIRC | None" = None
        self._observer_thread: "threading.Thread | None" = None
        self._observer_username: str = ""
        self._observer_channel: str = ""
        self._active = False               # ON has been announced for this session

        # runtime caches
        self._cmd_cd: dict = {}            # command token -> monotonic time free again
        self._queued: set = set()          # command tokens currently queued (waiting)
        self._inflight: set = set()        # command tokens with a live worker (fire or wait)
        self._balances: dict = {}          # account_id -> cached points balance
        self._spent_at: dict = {}          # account_id -> monotonic of last local spend
        self._reward_cache: dict = {}      # reward_id -> reward dict (from catalogue)
        self._channel_id: "str | None" = None
        self._last_balance_refresh = 0.0
        self._refreshing = False
        self._last_triggers: list = []     # recent fires (for the status UI)
        self._reason = "aus"               # human-readable current state (for the UI)
        self._connect_error: "str | None" = None  # last IRC connect failure (diag)

    # ------------------------------------------------------------------ lifecycle
    def stop(self):
        self._stop.set()
        self._deactivate(announce=False)

    def run(self):
        logger.info("Chat-redeem manager started.")
        while not self._stop.wait(self.poll_interval):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("chat-redeem manager tick failed")
        logger.info("Chat-redeem manager stopped.")

    # ------------------------------------------------------------------ main tick
    def _tick(self):
        with Session(engine) as session:
            cfg = chat_redeem.get_config(session)
        with self._lock:
            self._cfg = cfg
            self._commands = {c["command"]: c for c in cfg["commands"]}

        if not cfg["enabled"]:
            self._set_reason("aus")
            if self._active or self._observer is not None:
                self._deactivate(announce=True)
            return

        # enabled but not fully configured -> report exactly what's missing
        missing = []
        if not cfg["channel"]:
            missing.append("Channel")
        if not cfg["announcer"]:
            missing.append("Ansage-Account")
        if not cfg["commands"]:
            missing.append("mind. 1 Command mit Belohnung")
        if missing:
            self._set_reason("Es fehlt: " + ", ".join(missing))
            if self._active or self._observer is not None:
                self._deactivate(announce=True)
            return

        # (re)connect the announcer if missing/dead or its identity changed
        err = self._ensure_observer(cfg)
        if err:
            self._set_reason(err)
            return
        obs = self._observer
        # Twitch rejected the login (bad/expired oauth) -> actionable hint
        if obs is not None and obs.notice_error:
            self._set_reason(f"Chat lehnt den Login von „{cfg['announcer']}\" ab: "
                             f"{obs.notice_error} — Token ungültig/abgelaufen, "
                             "Account hier neu einloggen")
            return
        if obs is None or not obs.joined.is_set():
            if self._connect_error:
                self._set_reason(f"Chat-Verbindung fehlgeschlagen ({self._connect_error}) "
                                 "— Proxy des Ansage-Accounts erlaubt evtl. kein IRC "
                                 "(Port 6667); anderen Proxy/Account ohne Proxy nutzen")
            else:
                self._set_reason("verbinde mit dem Chat…")
            return
        self._connect_error = None  # joined successfully -> clear stale failure
        if not self._active:
            self._announce(self._on_text(cfg))
            self._active = True
            self._last_balance_refresh = 0.0   # force an immediate first refresh
            logger.info("chat-redeem ON announced in #%s as %s",
                        cfg["channel"], cfg["announcer"])
        if obs.msg_error:
            # connected & active, but Twitch is dropping our posts (announcer
            # banned / followers-only / verified-phone required …)
            self._set_reason(f"aktiv, aber Twitch lehnt Chat-Nachrichten ab: "
                             f"{obs.msg_error} — anderen Ansage-Account nutzen")
        else:
            self._set_reason("aktiv")
        self._maybe_refresh_balances(cfg)

    def _set_reason(self, reason: str):
        with self._lock:
            self._reason = reason

    # ------------------------------------------------------------------ observer
    def _ensure_observer(self, cfg) -> "str | None":
        """Connect/keep the announcer link. Returns an error reason, or None."""
        alive = (self._observer_thread is not None
                 and self._observer_thread.is_alive())
        same = (self._observer_username == cfg["announcer"]
                and self._observer_channel == cfg["channel"])
        if alive and same:
            return None
        if not same:
            # announcer/channel changed -> treat as a fresh session so the NEW
            # announcer re-announces ON once it joins (a pure reconnect keeps
            # _active, so a flaky link doesn't spam ON/OFF on every reconnect)
            self._active = False
        # identity changed or connection down -> rebuild (no off-announce here:
        # ON re-announces once the new link joins). Capture why a dead link
        # failed so the UI can show it (kept until a later attempt actually joins).
        if self._observer is not None:
            self._connect_error = (self._observer.connect_error
                                   or self._observer.notice_error
                                   or self._connect_error)
        self._teardown_observer()
        with Session(engine) as session:
            rec = chat_redeem.announcer_creds(session, cfg["announcer"])
        if rec is None:
            return f"Ansage-Account „{cfg['announcer']}\" nicht gefunden"
        if not rec["logged_in"]:
            return (f"Ansage-Account „{rec['username']}\" hat in dieser App keinen "
                    "Login-Cookie – bitte hier neu einloggen")
        observer = heist.HeistIRC(
            rec["username"], rec["token"], cfg["channel"], rec["proxy"],
            on_message=self._on_chat_message,
        )
        t = threading.Thread(target=observer.start,
                             name=f"chat-redeem-obs-{rec['username']}", daemon=True)
        t.start()
        self._observer = observer
        self._observer_thread = t
        self._observer_username = cfg["announcer"]
        self._observer_channel = cfg["channel"]
        logger.info("chat-redeem observer connecting as %s in #%s",
                    rec["username"], cfg["channel"])

    def _teardown_observer(self):
        # swap the pointers out atomically so a concurrent caller (stop() vs the
        # tick thread) can't double-die/double-join the same observer
        with self._lock:
            obs, t = self._observer, self._observer_thread
            self._observer = None
            self._observer_thread = None
            self._observer_username = ""
            self._observer_channel = ""
        if obs is not None:
            try:
                obs.die()
            except Exception:  # noqa: BLE001
                pass
        if t is not None:
            t.join(timeout=5)

    def _deactivate(self, announce: bool):
        """Announce OFF (if we were active + still connected) and drop the link."""
        was_active = self._active
        # stop accepting new redeems immediately, BEFORE the flush wait, so chat
        # messages arriving mid-shutdown don't spawn redemptions
        self._active = False
        if announce and was_active:
            obs = self._observer
            if obs is not None and obs.joined.is_set():
                self._announce(self._off_text())
                # let the observer's IRC reactor flush the privmsg before we
                # disconnect — otherwise the OFF message is dropped unsent
                time.sleep(2.0)
                logger.info("chat-redeem OFF announced")
        self._teardown_observer()

    # ------------------------------------------------------------------ announce
    def _announce(self, text: str):
        obs = self._observer
        if obs is None:
            return
        try:
            obs.send(text)
        except Exception:  # noqa: BLE001
            logger.exception("chat-redeem announce failed")

    def announce_now(self) -> dict:
        """Re-send the ON announcement immediately with the CURRENT config.

        Reads the freshly-saved config so any just-changed commands/text are
        included. Requires the announcer to be connected (module running).
        """
        obs = self._observer
        if obs is None or not obs.joined.is_set():
            return {"ok": False, "reason": self._reason}
        with Session(engine) as session:
            cfg = chat_redeem.get_config(session)
        text = self._on_text(cfg)
        self._announce(text)
        logger.info("chat-redeem ON re-announced on demand")
        return {"ok": True, "text": text}

    def _on_text(self, cfg) -> str:
        return chat_redeem.render_on_text(cfg.get("on_text", ""), cfg.get("commands", []))

    def _off_text(self) -> str:
        with self._lock:
            cfg = self._cfg or {}
        return cfg.get("off_text") or chat_redeem.DEFAULT_OFF_TEXT

    # ------------------------------------------------------------------ chat in
    def _on_chat_message(self, nick: str, msg: str):
        """Runs in the observer's IRC thread for every public chat message."""
        parts = (msg or "").strip().split()
        if not parts:
            return
        # Exact first-token match against the configured commands. Each stored
        # command carries its own prefix sigil (e.g. "!flash" or "?flash"), so a
        # bare word in normal chat never equals a command and never fires.
        token = chat_redeem.normalize_command(parts[0])
        if not token:
            return
        with self._lock:
            if not self._active:
                return
            entry = self._commands.get(token)
            if entry is None or not entry["enabled"]:
                return
            # Coalesce while a worker for this command is already live. This must
            # cover the ENTIRE worker lifetime (including the initial fire, whose
            # proxied redeem can take several seconds), not just the short burst
            # window: otherwise a second command arriving after BURST_DEDUPE but
            # before the first redeem completes spawns a second worker and
            # double-spends.
            if token in self._inflight or token in self._queued:
                return
            now = time.monotonic()
            if now < self._cmd_cd.get(token, 0.0):
                return  # command on cooldown -> ignore (anti-spam)
            self._cmd_cd[token] = now + (min(entry["cooldown"], self.BURST_DEDUPE) or 1.0)
            self._inflight.add(token)
        threading.Thread(target=self._do_redeem, args=(entry, nick, token), daemon=True,
                         name=f"chat-redeem-{entry['command']}").start()

    def _do_redeem(self, entry: dict, nick: str, token: str):
        # never let a worker thread die silently (a DB/transient error would
        # otherwise burn the command's reservation with no diagnostic)
        try:
            self._do_redeem_inner(entry, nick)
        except Exception:  # noqa: BLE001
            logger.exception("chat-redeem worker failed for %s", entry.get("command"))
            self._note_trigger(entry, nick, ok=False, message="interner Fehler")
        finally:
            with self._lock:
                self._inflight.discard(token)

    def _do_redeem_inner(self, entry: dict, nick: str):
        reward_id = entry["reward_id"]
        with self._lock:
            reward = self._reward_cache.get(reward_id)
            channel_id = self._channel_id
        if reward is None or channel_id is None:
            # catalogue not loaded yet (or reward vanished) -> can't redeem now
            self._note_trigger(entry, nick, ok=False,
                               message="Reward-Katalog noch nicht geladen")
            with self._lock:
                self._last_balance_refresh = 0.0   # pull a fresh catalogue soon
            return

        # input-required rewards need real viewer text we don't capture -> skip
        if reward.get("isUserInputRequired"):
            self._note_trigger(entry, nick, ok=False,
                               message="Reward erfordert Eingabe – nicht unterstützt")
            return

        token = entry["command"]
        cost = reward.get("cost", 0)
        deadline = time.monotonic() + self.MAX_QUEUE_WAIT
        queued_here = False
        announced_queue = False
        try:
            while True:
                # the module may have been stopped while we were queued
                if not self._active or self._stop.is_set():
                    return

                # Honor the cooldowns configured on the "Einlösen" page: a per-
                # account cooldown (used account drops out) AND a global spacing.
                with Session(engine) as session:
                    redeemers = chat_redeem.load_redeemer_accounts(session)
                    per_account_cd = redeem.cooldown_seconds(session, reward_id)
                    global_delay = redeem.master_delay(session, reward_id)

                # accounts with enough points (regardless of cooldown) + their wait
                funded = []
                for r in redeemers:
                    if not r["logged_in"]:
                        continue
                    with self._lock:
                        bal = self._balances.get(r["id"])
                    if bal is None or bal < cost:
                        continue
                    funded.append((bal, r, redeem.cooldown_remaining(r["id"], reward_id)))
                if not funded:
                    # waiting can't conjure points -> drop now
                    self._note_trigger(entry, nick, ok=False,
                                       message="Kein Account mit genug Punkten")
                    return

                grem = redeem.global_cooldown_remaining(reward_id)
                free = [(bal, r) for bal, r, rem in funded if rem <= 0]
                if grem <= 0 and free:
                    # FIRE: richest free account pays
                    free.sort(key=lambda x: x[0], reverse=True)
                    bal, acc = free[0]
                    self._fire(entry, nick, reward, channel_id, acc, bal, cost,
                               per_account_cd, global_delay)
                    return

                # can't fire yet -> wait until the global slot AND an account free
                acct_wait = 0.0 if free else min(rem for _, _, rem in funded)
                wait = max(grem, acct_wait) or 0.5
                if time.monotonic() + wait > deadline:
                    self._note_trigger(entry, nick, ok=False,
                                       message=f"Cooldown zu lang (> {int(self.MAX_QUEUE_WAIT)}s) – verworfen")
                    return
                # coalesce: only one queued worker per command
                if not queued_here:
                    with self._lock:
                        if token in self._queued:
                            return
                        self._queued.add(token)
                        queued_here = True
                if not announced_queue:
                    announced_queue = True
                    self._note_trigger(entry, nick, ok=False,
                                       message=f"eingereiht – feuert nach Cooldown (~{wait:.0f}s)")
                if self._stop.wait(min(wait, 3.0)):
                    return  # interruptible wait; re-check on the next loop
        finally:
            if queued_here:
                with self._lock:
                    self._queued.discard(token)

    def _fire(self, entry, nick, reward, channel_id, acc, bal, cost,
              per_account_cd, global_delay):
        """Redeem `reward` with `acc` and apply cooldowns/balance bookkeeping."""
        reward_id = reward["id"]
        proxies = acc["proxy"].requests_proxies if acc["proxy"] else None
        res = redeem.redeem_reward(acc["token"], proxies, channel_id, reward)
        if res["ok"]:
            # configured per-account cooldown (min the small rotate fallback so a
            # burst never re-picks the same account at once) + global spacing
            redeem.set_account_cooldown(acc["id"], reward_id,
                                        max(per_account_cd, chat_redeem.ROTATE_COOLDOWN))
            redeem.set_global_cooldown(reward_id, global_delay)
            now = time.monotonic()
            with self._lock:
                cur = self._balances.get(acc["id"], bal)
                self._balances[acc["id"]] = max(0, cur - cost)
                self._spent_at[acc["id"]] = now
                # apply the FULL per-command cooldown only now (a real spend)
                self._cmd_cd[entry["command"]] = now + entry["cooldown"]
            self._record_event(
                acc["id"],
                f'Chat „{entry["command"]}" von {nick} → '
                f'„{reward["title"]}" eingelöst ({acc["username"]})')
            self._note_trigger(entry, nick, ok=True, message=acc["username"])
            return

        # ---- failure: keep the rotation honest so we don't fail-loop on one acc
        reason = res.get("reason")
        if reason == "insufficient_points":
            with self._lock:
                self._balances[acc["id"]] = 0  # stale-high -> rotate to next-richest
        elif reason == "server_cooldown":
            redeem.set_account_cooldown(acc["id"], reward_id, max(per_account_cd, 30.0))
        elif reason in redeem.PERMANENT_REASONS:
            redeem.set_global_cooldown(reward_id, max(global_delay, 60.0))
        self._record_event(
            acc["id"],
            f'Chat „{entry["command"]}" von {nick} → '
            f'„{reward["title"]}" fehlgeschlagen: {res.get("message")}')
        self._note_trigger(entry, nick, ok=False,
                           message=f'{acc["username"]}: {res.get("message")}')

    # ------------------------------------------------------------------ balances
    def _maybe_refresh_balances(self, cfg):
        now = time.monotonic()
        with self._lock:
            if self._refreshing:
                return
            if self._reward_cache and (now - self._last_balance_refresh) < self.balance_refresh:
                return
            self._refreshing = True
            self._last_balance_refresh = now
        try:
            threading.Thread(target=self._refresh_balances, args=(cfg,), daemon=True,
                             name="chat-redeem-balances").start()
        except Exception:  # noqa: BLE001 — never leave _refreshing stuck True
            with self._lock:
                self._refreshing = False
            logger.exception("chat-redeem: could not start balance refresh")

    def _refresh_balances(self, cfg):
        try:
            with Session(engine) as session:
                redeemers = chat_redeem.load_redeemer_accounts(session)
            redeemer_ids = {r["id"] for r in redeemers if r["logged_in"]}
            balances, catalogue, channel_id = {}, None, None
            for r in redeemers:
                if not r["logged_in"]:
                    continue
                proxies = r["proxy"].requests_proxies if r["proxy"] else None
                try:
                    state = redeem.fetch_channel_points(r["token"], proxies, cfg["channel"])
                except redeem.RedeemError as e:
                    logger.debug("chat-redeem balance fetch failed for %s: %s",
                                 r["username"], e)
                    continue
                balances[r["id"]] = state["balance"]
                if catalogue is None:
                    channel_id = state["channelId"]
                    catalogue = {rw["id"]: rw for rw in state["rewards"]}
            now = time.monotonic()
            with self._lock:
                # Keep the previously cached balance for a current redeemer whose
                # fetch FAILED this cycle, instead of dropping it from the rotation
                # (a single transient proxy blip would otherwise evict a funded
                # account and reject every command until a later refresh). Scope to
                # the current redeemer set so removed accounts still fall out.
                merged = {aid: bal for aid, bal in self._balances.items()
                          if aid in redeemer_ids}
                for aid, fetched in balances.items():
                    cached = self._balances.get(aid)
                    spent = self._spent_at.get(aid, 0.0)
                    # Don't let a stale server balance UNDO a very recent local
                    # spend (Twitch's balance often hasn't reflected it yet).
                    if cached is not None and (now - spent) < 90 and fetched > cached:
                        merged[aid] = cached
                    else:
                        merged[aid] = fetched
                self._balances = merged
                if catalogue is not None:
                    self._reward_cache = catalogue
                    self._channel_id = channel_id
        finally:
            with self._lock:
                self._refreshing = False

    # ------------------------------------------------------------------ events / status
    def _note_trigger(self, entry, nick, ok, message):
        with self._lock:
            self._last_triggers.insert(0, {
                "command": entry["command"], "nick": nick, "ok": ok,
                "message": message, "age": 0.0, "_at": time.monotonic(),
            })
            del self._last_triggers[20:]

    def _record_event(self, account_id, message: str):
        if account_id is None:
            return  # Event.account_id is NOT NULL
        try:
            with Session(engine) as s:
                s.add(Event(account_id=account_id, type="redeem", message=message))
                s.commit()
        except Exception:  # noqa: BLE001
            logger.exception("could not record chat-redeem event")

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            obs = self._observer
            triggers = [
                {"command": t["command"], "nick": t["nick"], "ok": t["ok"],
                 "message": t["message"], "age": round(now - t["_at"], 1)}
                for t in self._last_triggers
            ]
            return {
                "active": self._active,
                "reason": self._reason,
                "observer_connected": obs is not None and obs.joined.is_set(),
                "announcer": self._observer_username or None,
                "channel": self._observer_channel or None,
                "balances": dict(self._balances),
                "last_triggers": triggers,
            }


# Module-level singleton (mirrors heist_manager) so routers and the app
# entrypoint share one coordinator instance.
chat_redeem_manager = ChatRedeemManager()
