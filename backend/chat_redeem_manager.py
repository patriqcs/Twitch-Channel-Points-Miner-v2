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
        self._balances: dict = {}          # account_id -> cached points balance
        self._reward_cache: dict = {}      # reward_id -> reward dict (from catalogue)
        self._channel_id: "str | None" = None
        self._last_balance_refresh = 0.0
        self._refreshing = False
        self._last_triggers: list = []     # recent fires (for the status UI)

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

        want = bool(cfg["enabled"] and cfg["channel"] and cfg["announcer"]
                    and cfg["commands"])
        if not want:
            if self._active or self._observer is not None:
                self._deactivate(announce=True)
            return

        # (re)connect the announcer if missing/dead or its identity changed
        self._ensure_observer(cfg)
        obs = self._observer
        connected = obs is not None and obs.joined.is_set()
        if connected and not self._active:
            self._announce(self._on_text(cfg))
            self._active = True
            self._last_balance_refresh = 0.0   # force an immediate first refresh
            logger.info("chat-redeem ON announced in #%s as %s",
                        cfg["channel"], cfg["announcer"])
        if self._active:
            self._maybe_refresh_balances(cfg)

    # ------------------------------------------------------------------ observer
    def _ensure_observer(self, cfg):
        alive = (self._observer_thread is not None
                 and self._observer_thread.is_alive())
        same = (self._observer_username == cfg["announcer"]
                and self._observer_channel == cfg["channel"])
        if alive and same:
            return
        # identity changed or connection down -> rebuild (no off-announce: this is
        # a reconnect, not a disable; ON re-announces once the new link joins)
        self._teardown_observer()
        with Session(engine) as session:
            rec = chat_redeem.announcer_creds(session, cfg["announcer"])
        if rec is None:
            logger.warning("chat-redeem: announcer %r not logged in; cannot connect",
                           cfg["announcer"])
            return
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
        if announce and self._active:
            obs = self._observer
            if obs is not None and obs.joined.is_set():
                self._announce(self._off_text())
                logger.info("chat-redeem OFF announced")
        self._active = False
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

    def _on_text(self, cfg) -> str:
        cmds = [c["command"] for c in cfg["commands"] if c["enabled"]]
        lst = " ".join(cmds) if cmds else "(keine)"
        return ("🎁 Chat-Redeems sind AN! Schreib einen dieser Commands, "
                f"um eine Belohnung auszulösen: {lst}")

    def _off_text(self) -> str:
        return "🛑 Chat-Redeems sind jetzt AUS."

    # ------------------------------------------------------------------ chat in
    def _on_chat_message(self, nick: str, msg: str):
        """Runs in the observer's IRC thread for every public chat message."""
        with self._lock:
            if not self._active:
                return
            token = chat_redeem.normalize_command(msg)
            entry = self._commands.get(token)
            if entry is None or not entry["enabled"]:
                return
            now = time.monotonic()
            if now < self._cmd_cd.get(token, 0.0):
                return  # command on cooldown -> ignore (anti-spam)
            # reserve the cooldown immediately so a burst of the same command in
            # the same instant only fires once
            self._cmd_cd[token] = now + entry["cooldown"]
        threading.Thread(target=self._do_redeem, args=(entry, nick), daemon=True,
                         name=f"chat-redeem-{entry['command']}").start()

    def _do_redeem(self, entry: dict, nick: str):
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

        with Session(engine) as session:
            redeemers = chat_redeem.load_redeemer_accounts(session)

        cost = reward.get("cost", 0)
        eligible = []
        for r in redeemers:
            if not r["logged_in"]:
                continue
            if redeem.cooldown_remaining(r["id"], reward_id) > 0:
                continue  # just used / server-cooled -> let another account go
            with self._lock:
                bal = self._balances.get(r["id"])
            if bal is None or bal < cost:
                continue
            eligible.append((bal, r))

        if not eligible:
            self._note_trigger(entry, nick, ok=False,
                               message="Kein freier Account mit genug Punkten")
            return

        # earliest-free with the MOST points: richest eligible account pays
        eligible.sort(key=lambda x: x[0], reverse=True)
        bal, acc = eligible[0]
        proxies = acc["proxy"].requests_proxies if acc["proxy"] else None
        res = redeem.redeem_reward(acc["token"], proxies, channel_id, reward)
        if res["ok"]:
            redeem.set_account_cooldown(acc["id"], reward_id, chat_redeem.ROTATE_COOLDOWN)
            with self._lock:
                self._balances[acc["id"]] = max(0, bal - cost)
            self._record_event(
                acc["id"],
                f'Chat „{entry["command"]}" von {nick} → '
                f'„{reward["title"]}" eingelöst ({acc["username"]})')
            self._note_trigger(entry, nick, ok=True, message=acc["username"])
        else:
            # server cooldown / transient -> back this account off briefly so the
            # next fire of this command rotates to someone else
            if res.get("reason") == "server_cooldown":
                redeem.set_account_cooldown(acc["id"], reward_id, 30.0)
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
        threading.Thread(target=self._refresh_balances, args=(cfg,), daemon=True,
                         name="chat-redeem-balances").start()

    def _refresh_balances(self, cfg):
        try:
            with Session(engine) as session:
                redeemers = chat_redeem.load_redeemer_accounts(session)
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
            with self._lock:
                self._balances = balances
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
                "observer_connected": obs is not None and obs.joined.is_set(),
                "announcer": self._observer_username or None,
                "channel": self._observer_channel or None,
                "balances": dict(self._balances),
                "last_triggers": triggers,
            }


# Module-level singleton (mirrors heist_manager) so routers and the app
# entrypoint share one coordinator instance.
chat_redeem_manager = ChatRedeemManager()
