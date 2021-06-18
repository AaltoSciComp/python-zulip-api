import re
import textwrap
import irc.bot
import irc.strings
from irc.client import Event, ServerConnection, ip_numstr_to_quad
from irc.client_aio import AioReactor
import multiprocessing as mp
from typing import Any, Dict


class IRCBot(irc.bot.SingleServerIRCBot):
    reactor_class = AioReactor

    def __init__(self, zulip_client: Any, stream: str, topic: str, channel: irc.bot.Channel,
                 nickname: str, server: str, nickserv_password: str = '', port: int = 6667,
                 all_topics: bool = False) -> None:
        self.channel = channel  # type: irc.bot.Channel
        self.zulip_client = zulip_client
        self.stream = stream
        self.topic = topic
        self.IRC_DOMAIN = server
        self.nickserv_password = nickserv_password
        self.all_topics = all_topics
        # Make sure the bot is subscribed to the stream
        self.check_subscription_or_die()
        self._manager = mp.Manager()
        self.seen_topics = self._manager.dict()
        # Initialize IRC bot after proper connection to Zulip server has been confirmed.
        irc.bot.SingleServerIRCBot.__init__(self, [(server, port)], nickname, nickname)

    def zulip_sender(self, sender_string: str) -> str:
        nick = sender_string.split("!")[0]
        return nick + "@" + self.IRC_DOMAIN

    def connect(self, *args: Any, **kwargs: Any) -> None:
        # Taken from
        # https://github.com/jaraco/irc/blob/master/irc/client_aio.py,
        # in particular the method of AioSimpleIRCClient
        self.c = self.reactor.loop.run_until_complete(
            self.connection.connect(*args, **kwargs)
        )
        print("Listening now. Please send an IRC message to verify operation")

    def check_subscription_or_die(self) -> None:
        resp = self.zulip_client.list_subscriptions()
        if resp["result"] != "success":
            print("ERROR: %s" % (resp["msg"],))
            exit(1)
        subs = [s["name"] for s in resp["subscriptions"]]
        if self.stream not in subs:
            print("The bot is not yet subscribed to stream '%s'. Please subscribe the bot to the stream first." % (self.stream,))
            exit(1)

    def on_nicknameinuse(self, c: ServerConnection, e: Event) -> None:
        c.nick(c.get_nickname().replace("_zulip", "__zulip"))

    def on_welcome(self, c: ServerConnection, e: Event) -> None:
        if len(self.nickserv_password) > 0:
            msg = 'identify %s' % (self.nickserv_password,)
            c.privmsg('NickServ', msg)
        c.join(self.channel)

        def forward_to_irc(msg: Dict[str, Any], seen_topics=self.seen_topics) -> None:
            not_from_zulip_bot = msg["sender_email"] != self.zulip_client.email
            print(msg)
            print(self.all_topics)
            if not not_from_zulip_bot:
                # Do not forward echo
                return
            is_a_stream = msg["type"] == "stream"
            if is_a_stream:
                in_the_specified_stream = msg["display_recipient"] == self.stream
                at_the_specified_subject = msg["subject"].casefold() == self.topic.casefold()
                if in_the_specified_stream and at_the_specified_subject:
                    msg["content"] = ("<%s> " % msg["sender_full_name"]) + msg["content"]
                    seen_topics[self.topic] = self.topic
                    send = lambda x: self.c.privmsg(self.channel, x)
                elif in_the_specified_stream and msg["content"].startswith(self.channel):
                    topic = msg["subject"]
                    msg["content"] = msg["content"][len(self.channel):].lstrip(' :,')
                    msg["content"] = ("\x02%s\x02 <%s> " % (topic, msg["sender_full_name"])) + msg["content"]
                    seen_topics[topic] = topic
                    send = lambda x: self.c.privmsg(self.channel, x)
                elif in_the_specified_stream and self.all_topics:
                    topic = msg["subject"]
                    msg["content"] = ("\x02%s\x02 <%s> " % (topic, msg["sender_full_name"])) + msg["content"]
                    seen_topics[topic] = topic
                    send = lambda x: self.c.privmsg(self.channel, x)
                else:
                    return
            else:
                recipients = [u["short_name"] for u in msg["display_recipient"] if
                              u["email"] != msg["sender_email"]]
                if len(recipients) == 1:
                    send = lambda x: self.c.privmsg(recipients[0], x)
                else:
                    send = lambda x: self.c.privmsg_many(recipients, x)
            for line in msg["content"].split("\n"):
                # The raw message is of format "PRIVMSG #channel :text\r\n", the
                # RFC limits the max length for said message to 512 bytes.
                # IRCnet (or irc.cs.hut.fi) seems to truncate long messages to
                # 452 characters (excluding metadata)
                max_length = min(450, 500 - len(c.get_nickname()) - len(self.channel))
                parts = textwrap.wrap(line, max_length)
                for part in parts:
                    try:
                        send(part)
                    except:
                        import traceback
                        traceback.print_exc()
                        send("[error sending line]")

        z2i = mp.Process(target=self.zulip_client.call_on_each_message, args=(forward_to_irc,))
        z2i.start()

    def on_privmsg(self, c: ServerConnection, e: Event) -> None:
        content = e.arguments[0]
        sender = self.zulip_sender(e.source)
        if sender.endswith("_zulip@" + self.IRC_DOMAIN):
            return

        # Forward the PM to Zulip
        print(self.zulip_client.send_message({
            "sender": sender,
            "type": "private",
            "to": "username@example.com",
            "content": content,
        }))

    def on_pubmsg(self, c: ServerConnection, e: Event) -> None:
        content = e.arguments[0]
        sender = self.zulip_sender(e.source)
        if sender.endswith("_zulip@" + self.IRC_DOMAIN):
            return

        message_topic = content.split(':')[0]
        print(self.seen_topics)
        topic_match = re.match(r"^([^:]{,20})::\s+(.*)", content)
        #print(repr(content))
        #print(repr(self.seen_topics))
        if message_topic in self.seen_topics:
            topic = message_topic
            content = content.split(':', 1)[-1].lstrip()
        elif topic_match:
            topic = topic_match.group(1)
            content = topic_match.group(2)
        else:
            topic = self.topic

        # Forward the stream message to Zulip
        print(self.zulip_client.send_message({
            "type": "stream",
            "to": self.stream,
            "subject": topic,
            "content": "**<{}>** {}".format(sender.split('@')[0], content),
        }))

    def on_dccmsg(self, c: ServerConnection, e: Event) -> None:
        c.privmsg("You said: " + e.arguments[0])

    def on_dccchat(self, c: ServerConnection, e: Event) -> None:
        if len(e.arguments) != 2:
            return
        args = e.arguments[1].split()
        if len(args) == 4:
            try:
                address = ip_numstr_to_quad(args[2])
                port = int(args[3])
            except ValueError:
                return
            self.dcc_connect(address, port)
