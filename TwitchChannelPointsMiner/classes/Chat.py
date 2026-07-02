import logging
import socket
import time
from enum import Enum, auto
from threading import Thread

from irc.bot import SingleServerIRCBot

from TwitchChannelPointsMiner.constants import IRC, IRC_PORT
from TwitchChannelPointsMiner.classes.Settings import Events, Settings

logger = logging.getLogger(__name__)


def _proxy_connect_factory(proxy):
    """irc connect_factory that routes the chat TCP socket through the account
    proxy, so chat presence does not leak the real host IP while every other
    request goes through the proxy. Falls back to a direct (but timeout-bounded)
    connection when no proxy is configured.
    """
    if proxy is None:
        def direct_factory(server_address):
            host, port = server_address
            sock = socket.create_connection((host, int(port)), timeout=20)
            sock.settimeout(None)
            return sock

        return direct_factory

    import socks  # PySocks, pulled in via requests[socks]

    proxy_types = {
        "http": socks.HTTP, "https": socks.HTTP,
        "socks4": socks.SOCKS4, "socks4a": socks.SOCKS4,
        "socks5": socks.SOCKS5, "socks5h": socks.SOCKS5,
    }
    ptype = proxy_types[proxy.scheme]
    rdns = proxy.scheme in ("socks4a", "socks5h")

    def factory(server_address):
        host, port = server_address
        sock = socks.socksocket()
        sock.set_proxy(
            ptype, proxy.host, int(proxy.port), rdns=rdns,
            username=proxy.username or None,
            password=proxy.password or None,
        )
        sock.settimeout(20)
        sock.connect((host, int(port)))
        sock.settimeout(None)
        return sock

    return factory


class ChatPresence(Enum):
    ALWAYS = auto()
    NEVER = auto()
    ONLINE = auto()
    OFFLINE = auto()

    def __str__(self):
        return self.name


class ClientIRC(SingleServerIRCBot):
    def __init__(self, username, token, channel):
        self.token = token
        self.channel = "#" + channel
        self.__active = False

        # Route the chat connection through the same per-account proxy as the
        # rest of the traffic; otherwise all accounts' authenticated chat logins
        # egress from the real host IP and link the accounts together.
        super(ClientIRC, self).__init__(
            [(IRC, IRC_PORT, f"oauth:{token}")], username, username,
            connect_factory=_proxy_connect_factory(Settings.proxy),
        )

    def on_welcome(self, client, event):
        client.join(self.channel)

    def start(self):
        self.__active = True
        self._connect()
        while self.__active:
            try:
                self.reactor.process_once(timeout=0.2)
                time.sleep(0.01)
            except Exception as e:
                logger.error(
                    f"Exception raised: {e}. Thread is active: {self.__active}"
                )

    def die(self, msg="Bye, cruel world!"):
        self.connection.disconnect(msg)
        self.__active = False

    """
    def on_join(self, connection, event):
        logger.info(f"Event: {event}", extra={"emoji": ":speech_balloon:"})
    """

    # """
    def on_pubmsg(self, connection, event):
        msg = event.arguments[0]
        mention = None

        if Settings.disable_at_in_nickname is True:
            mention = f"{self._nickname.lower()}"
        else:
            mention = f"@{self._nickname.lower()}"

        # also self._realname
        # if msg.startswith(f"@{self._nickname}"):
        if mention != None and mention in msg.lower():
            # nickname!username@nickname.tmi.twitch.tv
            nick = event.source.split("!", 1)[0]
            # chan = event.target

            logger.info(f"{nick} at {self.channel} wrote: {msg}", extra={
                        "emoji": ":speech_balloon:", "event": Events.CHAT_MENTION})
    # """


class ThreadChat(Thread):
    def __deepcopy__(self, memo):
        return None

    def __init__(self, username, token, channel):
        super(ThreadChat, self).__init__()

        self.username = username
        self.token = token
        self.channel = channel

        self.chat_irc = None

    def run(self):
        self.chat_irc = ClientIRC(self.username, self.token, self.channel)
        logger.info(
            f"Join IRC Chat: {self.channel}", extra={"emoji": ":speech_balloon:"}
        )
        self.chat_irc.start()

    def stop(self):
        if self.chat_irc is not None:
            logger.info(
                f"Leave IRC Chat: {self.channel}", extra={"emoji": ":speech_balloon:"}
            )
            self.chat_irc.die()
