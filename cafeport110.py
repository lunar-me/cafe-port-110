#!/usr/bin/env python3
"""Cafe Port 110: dummy POP3 and SMTP servers for testing mail clients.

Says yes to everything and keeps nothing: any user name and any password are
accepted, every message is read off the wire and thrown away. Run it when you
need a mail server that answers on ports 110 and 25 without a real mailbox.

See README.md for the full tour, or ``python cafeport110.py --help``.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import enum
import logging
import socket
import sys
import time
from functools import partial
from itertools import count
from typing import List, Optional

import trio

connection_ids = count()
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=110, help="Listen for POP3 on PORT")
    parser.add_argument(
        "--smtp-port", type=int, default=25, help="Listen for SMTP on PORT"
    )
    parser.add_argument(
        "--no-smtp", action="store_true", help="Don't run the SMTP server at all"
    )
    parser.add_argument("--verbose", action="store_true", help="Log debug messages")
    # argparse.BooleanOptionalAction only exists on Python 3.9+, so the pair of
    # flags is spelled out by hand to keep the documented 3.8 support working.
    keypress = parser.add_mutually_exclusive_group()
    keypress.add_argument(
        "--press-any-key-to-exit",
        dest="press_any_key_to_exit",
        action="store_true",
        help="Exit when a key is pressed (default: on if stdin is a terminal)",
    )
    keypress.add_argument(
        "--no-press-any-key-to-exit",
        dest="press_any_key_to_exit",
        action="store_false",
        help="Keep running until interrupted, even if stdin is a terminal",
    )
    parser.set_defaults(press_any_key_to_exit=None)
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(
        datefmt="%Y-%m-%d %H:%M:%S",
        format=f"%(asctime)s.%(msecs)03d %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stdout,
    )
    smtp_port = None if args.no_smtp else args.smtp_port
    logger.info("Listening for POP3 on port %s", args.port)
    if smtp_port is not None:
        logger.info("Listening for SMTP on port %s", smtp_port)
    logger.info(
        "Logging every POP3 and SMTP event; --verbose adds the raw bytes "
        "and full message bodies"
    )
    keypress = args.press_any_key_to_exit
    if keypress is None:
        keypress = sys.stdin is not None and sys.stdin.isatty()
    if keypress:
        logger.info("Press any key to exit")
    try:
        trio.run(partial(serve, args.port, smtp_port, wait_for_keypress=keypress))
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
        return
    logger.info("Shut down")


def split_lines(buffer: bytes):
    """Split *buffer* into complete CRLF-terminated lines.

    Returns the complete lines plus whatever trailing bytes were not yet
    terminated by a newline, so the caller can prepend them to the next chunk.
    """
    parts = buffer.split(b"\r\n")
    return parts[:-1], parts[-1]


def describe(data: bytes) -> str:
    """Render a protocol line for the screen: printable ASCII, no line ending.

    Non-printable bytes are escaped (``\\x0d``, ``\\x1b`` ...) so a client that
    tries to log-inject cannot scramble the operator's terminal.
    """
    text = data.decode("ascii", errors="replace").rstrip("\r\n")
    return "".join(char if 32 <= ord(char) < 127 else f"\\x{ord(char):02x}" for char in text)


def ok(msg: Optional[str] = None) -> bytes:
    if msg is None:
        return b"+OK\r\n"
    return b"+OK %b\r\n" % msg.encode("ascii", errors="replace")


def err(msg: Optional[str] = None) -> bytes:
    if msg is None:
        return b"-ERR\r\n"
    return b"-ERR %b\r\n" % msg.encode("ascii", errors="replace")


def reply(code: int, msg: Optional[str] = None) -> bytes:
    if msg is None:
        return b"%d\r\n" % code
    return b"%d %b\r\n" % (code, msg.encode("ascii", errors="replace"))


# POP3 multi-line reply meaning "there are no messages": an "+OK" status line
# with no extra text, followed by the empty listing terminator.
NO_MESSAGES = ok() + b".\r\n"

# SMTP AUTH (RFC 4954). The challenges the dummy server sends for LOGIN, as
# base64 of "Username:" / "Password:". Clients such as Python's smtplib and most
# mail clients expect exactly these.
AUTH_USERNAME_CHALLENGE = b"VXNlcm5hbWU6"
AUTH_PASSWORD_CHALLENGE = b"UGFzc3dvcmQ6"

# Codes for AUTH replies: 235 means "authentication successful", 334 carries a
# challenge, and 503 means "already authenticated" (also treated as success).
AUTH_OK = 235
AUTH_CHALLENGE = 334


def decode_base64(data: str) -> Optional[bytes]:
    """Decode a base64 AUTH argument, returning ``None`` if it is not valid."""
    try:
        return base64.b64decode(data.encode("ascii", errors="replace"), validate=True)
    except (binascii.Error, ValueError):
        return None


# How many lines of an SMTP message body are echoed to the screen. Mail bodies
# are usually the least interesting part of a session and can be huge, so the
# rest is only logged with --verbose.
MAX_LOGGED_BODY_LINES = 64

# The AUTH mechanisms this dummy server advertises and accepts. Any user name
# and any password are accepted; see SMTP.handle_auth.
EHLO_AUTH_MECHANISMS = "PLAIN LOGIN"

# Extensions advertised in the SMTP EHLO greeting. They only exist to make
# clients happy; this server implements none of them except AUTH, which accepts
# any credentials (see SMTP.handle_auth).
#
# IMPORTANT: keep this to a single extension, on the terminating line.
#
# Clients parse the EHLO reply line by line and *discard the first line* (they
# treat it as the greeting, not an extension), so AUTH has to appear on a later
# line. But each extra line also makes it more likely that a client's buffered
# reader hands back only the first line, in which case it takes that line as the
# whole reply and then consumes every later reply one command late -- e.g.
# smtplib.data() would see the answer to RCPT instead of "354". Two lines (the
# greeting, then this one) is the shape that reliably both advertises AUTH and
# keeps clients in sync.
EHLO_EXTENSIONS = [b"AUTH " + EHLO_AUTH_MECHANISMS.encode("ascii")]


class State(enum.Enum):
    authorization = enum.auto()
    transaction = enum.auto()
    update = enum.auto()
    done = enum.auto()


class Protocol:
    """Line-oriented server protocol: read commands, reply, log, never crash.

    Subclasses provide :meth:`banner` and :meth:`handle`, and set ``done`` to
    ``True`` when the connection should be closed. While ``data_mode`` is set
    (SMTP ``DATA``) incoming lines go to :meth:`handle_data` instead, and the
    lone ``.`` terminator is answered with a ``250`` by :meth:`run`.

    Every event is logged through :meth:`log`, tagged with the protocol name and
    the connection id, so POP3 and SMTP traffic sharing one screen stay
    readable. ``INFO`` (the default) shows the conversation line by line;
    ``DEBUG`` (``--verbose``) adds the exact bytes seen on the wire.
    """

    #: Protocol name used in log lines and state-change messages.
    name = "?"

    def __init__(self) -> None:
        self.id = next(connection_ids)
        self.peer: object = "?"
        self.tag = f"{self.name}[{self.id}]"
        self.done = False
        self.data_mode = False
        self.state: Optional[str] = None
        self.lines_in = 0
        self.lines_out = 0
        #: Set by a protocol that is mid-way through a multi-step exchange
        #: (SMTP AUTH). The next line the client sends is a raw argument for
        #: that exchange rather than a command, and goes to :meth:`handle_data`.
        self.expect_response = False

    def __str__(self) -> str:
        return self.tag

    def set_peer(self, stream: trio.SocketStream) -> None:
        """Remember who we are talking to, for the log lines."""
        try:
            host, port = stream.socket.getpeername()[:2]
        except OSError:  # pragma: no cover - already gone
            self.peer = "?"
        else:
            self.peer = f"{host}:{port}"
        self.tag = f"{self.name}[{self.id} {self.peer}]"

    def log(self, message: str, *args: object, level: int = logging.INFO) -> None:
        logger.log(level, "%s %s", self.tag, message % args if args else message)

    def show_reply(self, response: bytes) -> None:
        """Log one reply, keeping a multi-line reply's status line on one row."""
        lines = response.split(b"\r\n")
        if lines and lines[-1] == b"":  # trailing CRLF
            lines.pop()
        if not lines:
            self.log("-> empty reply")
            return
        suffix = f" ({len(lines)} lines)" if len(lines) > 1 else ""
        self.log("-> %s%s", describe(lines[0]), suffix)
        for line in lines[1:]:
            self.log("   %s", describe(line))

    def banner(self) -> bytes:
        raise NotImplementedError

    def handle(self, command: str, args: List[str]) -> bytes:
        raise NotImplementedError

    def handle_data(self, line: bytes) -> Optional[bytes]:
        """Handle one line of raw data; ``None`` means "say nothing"."""
        raise NotImplementedError

    def end_of_body(self) -> None:
        """Called when a data transfer ends (SMTP's lone ``.``), for logging."""

    def close(self) -> Optional[bytes]:
        """Reply (if any) to send when the peer hangs up mid-conversation."""
        return None

    async def run(self, stream: trio.SocketStream) -> None:
        self.set_peer(stream)
        banner = self.banner()
        self.lines_out += 1
        self.log("banner: sent")
        self.show_reply(banner)
        await stream.send_all(banner)
        pending = b""
        async for data in stream:
            self.log("raw %r", data, level=logging.DEBUG)
            pending += data
            # Process every complete non-data line before honouring a DATA
            # switch, so pipelined "DATA\r\n...body..." still works.
            while True:
                if self.data_mode:
                    if pending == b".":
                        # The "." terminator itself is only complete once the
                        # next chunk proves it is not actually ".foo".
                        break
                    index = pending.find(b"\r\n")
                    if index < 0:
                        break
                    line, pending = pending[:index], pending[index + 2 :]
                    self.lines_in += 1
                    if line == b".":
                        self.data_mode = False
                        self.end_of_body()
                        response: Optional[bytes] = reply(250, "OK: queued as CAFE")
                    else:
                        response = self.handle_data(line)
                elif self.expect_response:
                    # Mid AUTH: the client is sending the answer to our
                    # challenge, not a command.
                    index = pending.find(b"\r\n")
                    if index < 0:
                        break
                    line, pending = pending[:index], pending[index + 2 :]
                    self.lines_in += 1
                    self.log("<- %s (AUTH response)", describe(line))
                    response = self.handle_data(line)
                else:
                    index = pending.find(b"\r\n")
                    if index < 0:
                        break
                    line, pending = pending[:index], pending[index + 2 :]
                    self.lines_in += 1
                    self.log("<- %s", describe(line))
                    command, *args = (
                        line.decode("ascii", errors="replace").rstrip().split(" ")
                    )
                    response = self.handle(command.upper(), args)
                if response is not None:
                    self.lines_out += 1
                    self.show_reply(response)
                    await stream.send_all(response)
                if self.done:
                    return
        self.log("hang up: no QUIT received", level=logging.INFO)
        response = self.close()
        if response is not None:
            self.lines_out += 1
            self.show_reply(response)
            await stream.send_all(response)


class POP3(Protocol):
    name = "POP3"

    def __init__(self) -> None:
        super().__init__()
        self.state = State.authorization
        self.user: Optional[str] = None

    def banner(self) -> bytes:
        return ok(f"<{time.monotonic()}@{socket.gethostname()}>")

    def set_state(self, state: State) -> None:
        if state != self.state:
            self.log("state: %s -> %s", self.state.name, state.name)
            self.state = state

    def handle(self, command: str, args: List[str]) -> bytes:
        if self.state == State.authorization:
            if command == "USER":
                self.user = args[0] if args else ""
                self.log("user name given: %s", self.user or "(empty)")
                return ok("pretending your mailbox exists")
            if command == "PASS":
                self.log(
                    "password of %d characters accepted for user %s",
                    len(" ".join(args)),
                    self.user or "(none)",
                )
                self.set_state(State.transaction)
                return ok("how did you know")
            if command == "QUIT":
                self.set_state(State.done)
                self.done = True
                return ok("goodbye")
            if command == "APOP":
                self.log("APOP login accepted for %s", " ".join(args) or "(nothing)")
                self.set_state(State.transaction)
                return ok("pretending your mailbox exists")

        if self.state == State.transaction:
            if command == "STAT":
                return ok("0 0")
            if command == "LIST":
                return err("no such message") if args else NO_MESSAGES
            if command == "RETR":
                return err("no such message")
            if command == "DELE":
                return err("no such message")
            if command == "NOOP":
                return ok()
            if command == "RSET":
                return ok()
            if command == "QUIT":
                self.set_state(State.update)
                self.done = True
                self.set_state(State.done)
                return ok("goodbye")
            if command == "TOP":
                return err("no such message")
            if command == "UIDL":
                return err("no such message") if args else NO_MESSAGES

        return err("unrecognized command")


class SMTP(Protocol):
    """Dummy SMTP server: says "250 OK" to everything, sends and stores nothing."""

    name = "SMTP"

    def __init__(self) -> None:
        super().__init__()
        self.helo: Optional[str] = None
        self.sender: Optional[str] = None
        self.recipients: List[str] = []
        self.body_lines = 0
        self.body_bytes = 0
        self.authenticated = False
        self.auth_user: Optional[str] = None
        self.auth_mechanism: Optional[str] = None
        # Which AUTH stage the next client line belongs to: None means no AUTH
        # is in progress, "username"/"password"/"plain" mean we are awaiting
        # that piece of the exchange.
        self.auth_step: Optional[str] = None

    def banner(self) -> bytes:
        return reply(220, f"{socket.gethostname()} cafe-port-110 SMTP")

    def ehlo_reply(self) -> bytes:
        """The EHLO greeting followed by the advertised extension keywords.

        The greeting must be written with a "250-" continuation marker: clients
        stop reading at the first line that does not start with "250-", so if the
        greeting were a plain "250 " they would never see the AUTH keyword on the
        next line. Only the final line uses "250 ". Returned as one buffer so
        ``Protocol.run`` writes it in a single ``send_all``.
        """
        lines = [b"250-%b\r\n" % self.greeting()]
        for extension in EHLO_EXTENSIONS[:-1]:
            lines.append(b"250-%b\r\n" % extension)
        lines.append(b"250 " + EHLO_EXTENSIONS[-1] + b"\r\n")
        return b"".join(lines)

    @staticmethod
    def greeting() -> bytes:
        return f"{socket.gethostname()} greets you".encode("ascii", errors="replace")

    def auth_succeeded(self, user: str, mechanism: str) -> bytes:
        """Tell the client its credentials were accepted."""
        self.authenticated = True
        self.auth_user = user or None
        self.auth_mechanism = mechanism
        self.auth_step = None
        self.expect_response = False
        self.log(
            "AUTH %s succeeded: user %s accepted, password accepted "
            "(any password is accepted)",
            mechanism,
            user or "(none given)",
        )
        return reply(AUTH_OK, "2.7.0 Authentication successful")

    def auth_failed(self, why: str) -> bytes:
        self.auth_step = None
        self.expect_response = False
        self.log("AUTH rejected: %s", why)
        return reply(535, f"5.7.8 {why} (cafe-port-110 accepts anything though)")

    def handle_auth(self, args: List[str]) -> bytes:
        """Handle ``AUTH [mechanism [initial-response]]`` (RFC 4954)."""
        if not args:
            return reply(501, "5.5.4 Syntax: AUTH mechanism [initial-response]")
        mechanism = args[0].upper()
        initial = args[1] if len(args) > 1 else None

        if self.authenticated:
            # Already logged in: 503 is what clients treat as "fine, carry on".
            self.log("AUTH %s: already authenticated", mechanism)
            return reply(503, "5.5.1 Already authenticated")

        if mechanism == "PLAIN":
            self.auth_mechanism = mechanism
            if initial is not None:
                return self.finish_auth_plain(initial)
            # No initial response: ask for one with an empty challenge.
            self.auth_step = "plain"
            self.expect_response = True
            self.log("AUTH PLAIN: asking for credentials")
            return b"%d \r\n" % AUTH_CHALLENGE

        if mechanism == "LOGIN":
            self.auth_mechanism = mechanism
            if initial is not None:
                # smtplib sends LOGIN's first stage as an initial response.
                self.auth_step = "password"
                self.expect_response = True
                self.auth_user = self.decode_auth_text(initial)
                self.log("AUTH LOGIN: user name given as initial response")
                return reply(AUTH_CHALLENGE, AUTH_PASSWORD_CHALLENGE.decode())
            self.auth_step = "username"
            self.expect_response = True
            self.log("AUTH LOGIN: asking for user name")
            return reply(AUTH_CHALLENGE, AUTH_USERNAME_CHALLENGE.decode())

        self.log("AUTH %s: unsupported mechanism", mechanism)
        return reply(
            504,
            "5.5.4 Unrecognized authentication type "
            f"(cafe-port-110 supports {EHLO_AUTH_MECHANISMS})",
        )

    @staticmethod
    def decode_auth_text(encoded: str) -> str:
        """Decode one base64 AUTH argument to text, for the log only."""
        raw = decode_base64(encoded)
        if raw is None:
            return encoded
        return raw.decode("utf-8", errors="replace")

    def finish_auth_plain(self, encoded: str) -> bytes:
        """The PLAIN exchange is a single base64 blob: NUL user NUL password."""
        raw = decode_base64(encoded)
        if raw is None:
            return self.auth_failed("malformed base64 in AUTH PLAIN")
        parts = raw.split(b"\0")
        # "authzid\0authcid\0password" (authzid is usually empty).
        if len(parts) == 3:
            _, user, password = parts
        elif len(parts) == 2:  # some clients omit the authzid
            user, password = parts
        else:
            return self.auth_failed("malformed AUTH PLAIN payload")
        self.log("AUTH PLAIN: password of %d characters", len(password))
        return self.auth_succeeded(
            user.decode("utf-8", errors="replace"), "PLAIN"
        )

    def handle(self, command: str, args: List[str]) -> bytes:
        if command in {"EHLO", "HELO"}:
            self.helo = " ".join(args)
            self.sender = None
            self.recipients = []
            self.log("%s from %s", command, self.helo or "(no name given)")
            if command == "HELO":
                return reply(250, f"{socket.gethostname()} greets you")
            return self.ehlo_reply()
        if command == "AUTH":
            return self.handle_auth(args)
        if command == "MAIL":
            if not self.authenticated:
                self.log("MAIL without AUTH; accepting it anyway")
            self.sender = " ".join(args)
            self.log("sender accepted: %s", self.sender)
            return reply(250, "OK: sender accepted, and promptly forgotten")
        if command == "RCPT":
            self.recipients.append(" ".join(args))
            self.log(
                "recipient accepted: %s (%d so far)",
                " ".join(args),
                len(self.recipients),
            )
            return reply(250, "OK: recipient accepted, and promptly forgotten")
        if command == "DATA":
            self.data_mode = True
            self.body_lines = 0
            self.body_bytes = 0
            self.log(
                "start of message body (sender %s, recipients %s): discarding it all",
                self.sender,
                ", ".join(self.recipients) or "none",
            )
            return reply(354, "End data with <CRLF>.<CRLF>; nothing will happen")
        if command == "RSET":
            self.sender = None
            self.recipients = []
            return reply(250, "OK: state reset, as if anything had happened")
        if command == "NOOP":
            return reply(250, "OK: no operation performed, as requested")
        if command == "VRFY":
            return reply(250, "OK: sure, that address exists")
        if command == "EXPN":
            return reply(250, "OK: your list is as empty as our mailbox")
        if command == "HELP":
            return reply(250, "OK: just say EHLO and get on with it")
        if command == "QUIT":
            self.done = True
            return reply(221, "Bye: nothing was sent, nothing was stored")
        # Anything else: accept it with a shrug, so odd clients keep going.
        self.log("unknown command %r accepted and ignored", command)
        return reply(250, f"OK: {command} accepted, and ignored")

    def handle_data(self, line: bytes) -> Optional[bytes]:
        if self.expect_response:
            return self.handle_auth_response(line)
        # Swallow the message body; Protocol.run() answers the "." terminator.
        # Header lines are worth showing, bodies usually are not.
        self.body_lines += 1
        self.body_bytes += len(line) + 2
        if self.body_lines <= MAX_LOGGED_BODY_LINES:
            self.log("body + %s", describe(line) or "(blank line)")
        elif self.body_lines == MAX_LOGGED_BODY_LINES + 1:
            self.log(
                "body + ... (further body lines are only logged with --verbose)"
            )
        self.log("body + %r", line, level=logging.DEBUG)
        return None

    def handle_auth_response(self, line: bytes) -> bytes:
        """One step of an in-progress AUTH exchange."""
        encoded = line.decode("ascii", errors="replace").strip()
        if encoded == "*":
            # RFC 4954: a bare "*" aborts the exchange.
            return self.auth_failed("client cancelled the exchange")

        if self.auth_step == "plain":
            return self.finish_auth_plain(encoded)

        if self.auth_step == "username":
            self.auth_user = self.decode_auth_text(encoded)
            self.log("AUTH LOGIN: user name given: %s", self.auth_user)
            self.auth_step = "password"
            return reply(AUTH_CHALLENGE, AUTH_PASSWORD_CHALLENGE.decode())

        if self.auth_step == "password":
            password = self.decode_auth_text(encoded)
            self.log("AUTH LOGIN: password of %d characters", len(password))
            return self.auth_succeeded(self.auth_user or "", "LOGIN")

        return self.auth_failed("unexpected response")

    def close(self) -> Optional[bytes]:
        if self.data_mode:
            self.log("hang up during DATA; %d body lines discarded", self.body_lines)
            return None
        return reply(221, "Bye: client hung up, nothing was sent or stored")

    def end_of_body(self) -> None:
        self.log(
            "body finished: %d lines / %d bytes discarded, nothing was sent or stored",
            self.body_lines,
            self.body_bytes,
        )


def wait_for_keypress() -> None:
    """Block until a key is pressed, however stdin is wired up.

    On Windows a console keypress is read with :func:`msvcrt.getch`, while a
    pipe or redirected file feeds ``sys.stdin.buffer``: the *text* ``sys.stdin``
    wrapper swallows those bytes into its own (unused) buffer. ``msvcrt.kbhit``
    distinguishes the two cases without ever blocking on the console.
    """
    try:
        import msvcrt  # type: ignore[import-not-found]
    except ImportError:
        sys.stdin.read(1)
        return
    if msvcrt.kbhit():
        msvcrt.getch()
        return
    sys.stdin.buffer.read(1)


async def watch_stdin(shutdown: trio.Event) -> None:
    """Read a keypress in a worker thread, then trigger *shutdown*."""
    try:
        await trio.to_thread.run_sync(wait_for_keypress)
    except Exception:
        logger.warning("Keypress watcher failed; use Ctrl+C to exit", exc_info=True)
        return
    logger.info("Key pressed; shutting down")
    shutdown.set()


def make_protocol(port: int, smtp_port: Optional[int]) -> Protocol:
    """Pick the protocol matching the local port a connection arrived on."""
    if port == smtp_port:
        return SMTP()
    return POP3()


async def serve(
    port: int, smtp_port: Optional[int] = None, wait_for_keypress: bool = False
) -> None:
    listeners: List[trio.SocketListener] = []
    try:
        listeners += await trio.open_tcp_listeners(port)
        if smtp_port is not None:
            listeners += await trio.open_tcp_listeners(smtp_port)
    except OSError:
        for listener in listeners:
            await listener.aclose()
        raise

    async def service(stream: trio.SocketStream) -> None:
        protocol = make_protocol(stream.socket.getsockname()[1], smtp_port)
        protocol.set_peer(stream)
        started = time.monotonic()
        logger.info("%s connected", protocol.tag)
        try:
            await protocol.run(stream)
        except (trio.BrokenResourceError, trio.ClosedResourceError) as exc:
            # Normal-ish: the peer vanished without a clean shutdown, which
            # includes port probes that connect and immediately hang up.
            logger.info("%s connection ended abruptly: %s", protocol.tag, exc)
        except Exception:
            logger.warning("%s crashed", protocol.tag, exc_info=True)
        finally:
            logger.info(
                "%s disconnected after %.3fs (%d commands received, %d replies sent)",
                protocol.tag,
                time.monotonic() - started,
                protocol.lines_in,
                protocol.lines_out,
            )

    shutdown = trio.Event()
    async with trio.open_nursery() as nursery:
        if wait_for_keypress:
            nursery.start_soon(watch_stdin, shutdown)
        nursery.start_soon(trio.serve_listeners, service, listeners)
        await shutdown.wait()
        nursery.cancel_scope.cancel()


if __name__ == "__main__":
    main()
