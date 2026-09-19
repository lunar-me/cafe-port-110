"""End-to-end smoke test for the Cafe Port 110 dummy POP3 and SMTP servers.

Starts ``cafeport110.py`` as a real subprocess on an ephemeral port and speaks
POP3 and SMTP to it over a socket, exercising the same code path as
``uvx cafe-port-110``. There are also a handful of unit tests for the small
pure helpers (line splitting, reply formatting, protocol selection).

Run with ``python test_cafeport110.py`` or ``pytest test_cafeport110.py``.
"""
from __future__ import annotations

import base64
import re
import smtplib
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List

HOST = "127.0.0.1"
SCRIPT = Path(__file__).with_name("cafeport110.py")


def free_port() -> int:
    """Reserve an ephemeral port the OS is unlikely to hand out twice."""
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


class Server:
    """Runs ``cafeport110.py`` in a subprocess and waits until it accepts TCP."""

    def __enter__(self) -> "Server":
        self._popen_extra = getattr(self, "_extra_args", ())
        return self._start()

    def _start(self) -> "Server":
        self.port = free_port()
        self.smtp_port = free_port()
        self.log_path = Path(__file__).with_name(f"cafeport110-test-{self.port}.log")
        self._log = self.log_path.open("wb")
        # The keypress flags are mutually exclusive, so drop the default
        # "--no-press-any-key-to-exit" whenever a test supplies its own.
        keypress = [
            flag
            for flag in ("--press-any-key-to-exit", "--no-press-any-key-to-exit")
            if flag in self._popen_extra
        ] or ["--no-press-any-key-to-exit"]
        argv = [
            sys.executable,
            str(SCRIPT),
            "--port",
            str(self.port),
            "--smtp-port",
            str(self.smtp_port),
            *keypress,
            *self._popen_extra,
        ]
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                self._log.flush()
                raise AssertionError(
                    f"cafe-port-110 exited immediately with code {self._proc.returncode}:\n"
                    f"{self.log_path.read_text(errors='replace')}"
                )
            if self.listening(self.port) and (
                "--no-smtp" in self._popen_extra or self.listening(self.smtp_port)
            ):
                return self
            time.sleep(0.05)
        raise AssertionError("cafe-port-110 did not start listening within 10s")

    @staticmethod
    def listening(port: int) -> bool:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            return probe.connect_ex((HOST, port)) == 0

    def __exit__(self, *exc: object) -> None:
        self._proc.terminate()
        try:
            self._proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.communicate()
        self._log.flush()
        self._log.close()
        self.remove_log()

    def remove_log(self) -> None:
        """Delete the log file, tolerating Windows still holding the handle."""
        for attempt in range(5):
            try:
                self.log_path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))


class Client:
    """A POP3 client that reads server replies line by line."""

    def __init__(self, port: int) -> None:
        self._sock = socket.create_connection((HOST, port), timeout=5)
        self._file = self._sock.makefile("rb")
        self.banner = self.read()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self._file.close()
        self._sock.close()

    def read(self) -> bytes:
        return self._file.readline().rstrip(b"\r\n")

    def send(self, line: bytes) -> bytes:
        self._sock.sendall(line + b"\r\n")
        return self.read()

    def raw(self, data: bytes) -> None:
        self._sock.sendall(data)


class SMTPClient:
    """An SMTP client that reads single-line replies."""

    def __init__(self, port: int) -> None:
        self._sock = socket.create_connection((HOST, port), timeout=5)
        self._file = self._sock.makefile("rb")
        self.banner = self.read()

    def __enter__(self) -> "SMTPClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._file.close()
        self._sock.close()

    def read(self) -> bytes:
        return self._file.readline().rstrip(b"\r\n")

    def send(self, line: bytes) -> bytes:
        self._sock.sendall(line + b"\r\n")
        return self.read()

    def send_multi(self, line: bytes, lines: int) -> List[bytes]:
        """Send a command and read a multi-line reply of ``lines`` lines."""
        self._sock.sendall(line + b"\r\n")
        return [self.read() for _ in range(lines)]

    def ehlo(self, name: bytes = b"test") -> List[bytes]:
        """Send EHLO and consume the whole two-line reply (greeting + AUTH)."""
        return self.send_multi(b"EHLO " + name, 2)

    def raw(self, data: bytes) -> None:
        self._sock.sendall(data)


def test_full_session() -> None:
    with Server() as server, Client(server.port) as client:
        assert client.banner.startswith(b"+OK <"), client.banner
        assert client.send(b"USER bob") == b"+OK pretending your mailbox exists"
        assert client.send(b"PASS hunter2") == b"+OK how did you know"
        assert client.send(b"STAT") == b"+OK 0 0"
        # LIST and UIDL are multi-line replies terminated by a lone "."
        for listing in (b"LIST", b"UIDL"):
            assert client.send(listing) == b"+OK", listing
            assert client.read() == b".", listing
        # ...and -ERR for a specific message that does not exist.
        assert client.send(b"LIST 1") == b"-ERR no such message"
        assert client.send(b"UIDL 1") == b"-ERR no such message"
        assert client.send(b"RETR 1") == b"-ERR no such message"
        assert client.send(b"DELE 1") == b"-ERR no such message"
        assert client.send(b"TOP 1 0") == b"-ERR no such message"
        assert client.send(b"NOOP") == b"+OK"
        assert client.send(b"RSET") == b"+OK"
        assert client.send(b"QUIT") == b"+OK goodbye"
        # The server must close the connection once QUIT has been answered.
        assert client.read() == b"", "server did not close after QUIT"


def test_batched_commands() -> None:
    """A client may pipeline several commands into a single TCP segment."""
    with Server() as server, Client(server.port) as client:
        client.raw(b"USER bob\r\nPASS hunter2\r\nSTAT\r\nQUIT\r\n")
        assert [client.read() for _ in range(4)] == [
            b"+OK pretending your mailbox exists",
            b"+OK how did you know",
            b"+OK 0 0",
            b"+OK goodbye",
        ]


def test_split_commands() -> None:
    """A command trickled across two TCP segments must still be parsed once."""
    with Server() as server, Client(server.port) as client:
        # TCP_NODELAY stops the OS coalescing the fragments into one segment,
        # so the server really has to buffer the partial "USER bo" line.
        client._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.raw(b"USER bo")
        client.raw(b"b\r\n")
        assert client.read() == b"+OK pretending your mailbox exists"
        assert client.send(b"PASS hunter2") == b"+OK how did you know"
        assert client.send(b"STAT") == b"+OK 0 0"


def test_apop_login() -> None:
    with Server() as server, Client(server.port) as client:
        assert client.send(b"APOP bob abcdef") == b"+OK pretending your mailbox exists"
        assert client.send(b"STAT") == b"+OK 0 0"


def test_quit_during_authorization() -> None:
    with Server() as server, Client(server.port) as client:
        assert client.send(b"QUIT") == b"+OK goodbye"
        assert client.read() == b"", "server did not close after QUIT"


def test_rejects_garbage() -> None:
    with Server() as server, Client(server.port) as client:
        assert client.send(b"FROBNICATE") == b"-ERR unrecognized command"


def test_smtp_delivery_session() -> None:
    """A whole mail transaction is accepted, then quietly thrown away."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        assert client.banner.startswith(b"220 "), client.banner
        # EHLO answers with the greeting (as a "250-" continuation) and then the
        # AUTH keyword on the terminating line. The greeting must use "250-", or
        # a client would stop reading before it ever saw AUTH.
        ehlo = client.send_multi(b"EHLO test", 2)
        assert ehlo[0].startswith(b"250-"), ehlo
        assert ehlo[1] == b"250 AUTH PLAIN LOGIN", ehlo
        assert client.send(b"MAIL FROM:<bob@example.com>").startswith(b"250 OK")
        assert client.send(b"RCPT TO:<alice@example.com>").startswith(b"250 OK")
        assert client.send(b"RCPT TO:<carol@example.com>").startswith(b"250 OK")
        assert client.send(b"DATA").startswith(b"354 ")
        # Everything up to the lone "." is swallowed without any reply at all.
        client.raw(b"Subject: hi\r\n\r\nNothing to see here.\r\n")
        client.raw(b"..stuffed line stays stuffed\r\n")
        assert client.send(b".").startswith(b"250 OK")
        assert client.send(b"QUIT").startswith(b"221 ")
        assert client.read() == b"", "server did not close after QUIT"


def test_smtp_helo_and_odd_commands() -> None:
    """HELO works, and verbs the server does not care about are accepted."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        assert client.send(b"HELO test").startswith(b"250 ")
        assert client.send(b"NOOP").startswith(b"250 OK")
        assert client.send(b"VRFY bob@example.com").startswith(b"250 OK")
        assert client.send(b"RSET").startswith(b"250 OK")
        assert client.send(b"HELP").startswith(b"250 OK")
        assert client.send(b"FROBNICATE").startswith(b"250 OK")


def test_smtp_pipelined_commands() -> None:
    """Commands and the message body sent in one segment are all handled."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        client.raw(
            b"EHLO test\r\n"
            b"MAIL FROM:<bob@example.com>\r\n"
            b"RCPT TO:<alice@example.com>\r\n"
            b"DATA\r\n"
            b"Subject: hi\r\n"
            b"\r\n"
            b"body\r\n"
            b".\r\n"
            b"QUIT\r\n"
        )
        # EHLO is answered with two lines; the rest with one each, in order.
        lines = [client.read() for _ in range(7)]
        assert lines[0].startswith(b"250-") and b"greets you" in lines[0], lines
        assert lines[1] == b"250 AUTH PLAIN LOGIN", lines
        assert [line[:3] for line in lines[2:]] == [b"250", b"250", b"354", b"250", b"221"], lines
        assert b"sender accepted" in lines[2], lines
        assert b"recipient accepted" in lines[3], lines
        assert b"queued as CAFE" in lines[5], lines


def test_smtp_hanging_up() -> None:
    """Dropping the connection mid-session must not disturb the server."""
    with Server() as server:
        with SMTPClient(server.smtp_port) as client:
            client.raw(b"EHLO test\r\nMAIL FROM:<a@b>\r\nRCPT TO:<c@d>\r\nDATA\r\n")
            assert client.send(b".\r\nbody\r\n")
        # A second connection must still be served.
        with SMTPClient(server.smtp_port) as client:
            assert client.banner.startswith(b"220 ")


def test_pop3_and_smtp_share_one_process() -> None:
    """Both listeners answer at the same time, each on its own port."""
    with Server() as server:
        with Client(server.port) as pop3, SMTPClient(server.smtp_port) as smtp:
            assert pop3.banner.startswith(b"+OK <")
            assert smtp.banner.startswith(b"220 ")
            assert smtp.send(b"NOOP").startswith(b"250 OK")
            assert pop3.send(b"USER bob").startswith(b"+OK ")
            assert pop3.send(b"PASS hunter2").startswith(b"+OK ")
            assert pop3.send(b"STAT") == b"+OK 0 0"


def test_no_smtp_flag() -> None:
    """--no-smtp runs the POP3 server on its own."""
    server = Server()
    server._extra_args = ("--no-smtp",)
    with server:
        assert not Server.listening(server.smtp_port)
        with Client(server.port) as client:
            assert client.banner.startswith(b"+OK <")


def read_log(server: "Server") -> str:
    """Everything the server has printed to the screen so far."""
    server._log.flush()
    return server.log_path.read_text(errors="replace")


def test_events_are_logged() -> None:
    """Every POP3 and SMTP event is printed, tagged with protocol and id."""
    with Server() as server:
        with Client(server.port) as pop3:
            pop3.send(b"USER bob")
            pop3.send(b"PASS hunter2")
            pop3.send(b"STAT")
            pop3.send(b"QUIT")
        with SMTPClient(server.smtp_port) as smtp:
            smtp.ehlo()
            smtp.send(b"MAIL FROM:<bob@example.com>")
            smtp.send(b"RCPT TO:<alice@example.com>")
            smtp.send(b"DATA")
            smtp.raw(b"Subject: hi\r\n")
            smtp.raw(b"\r\n")
            smtp.raw(b"the void stares back\r\n")
            smtp.send(b".")
            smtp.send(b"QUIT")

        log = read_log(server)

    # Connection lifecycle, tagged with protocol, connection id and peer.
    assert re.search(r"POP3\[\d+ \S+:\d+\] connected", log), log
    assert re.search(r"POP3\[\d+ \S+:\d+\] disconnected", log), log
    assert re.search(r"SMTP\[\d+ \S+:\d+\] connected", log), log
    assert re.search(r"SMTP\[\d+ \S+:\d+\] disconnected", log), log

    # POP3: banner, commands, replies, login details and state changes.
    for expected in (
        "banner: sent",
        "-> +OK <",  # the banner itself
        "<- USER bob",
        "user name given: bob",
        "<- PASS hunter2",
        "state: authorization -> transaction",
        "<- STAT",
        "-> +OK 0 0",
        "state: transaction -> update",
        "-> +OK goodbye",
    ):
        assert expected in log, f"{expected!r} missing from:\n{log}"
    # The password must not be echoed to the screen verbatim.
    assert "password of 7 characters accepted for user bob" in log, log

    # SMTP: greeting, envelope, body and the "thrown away" summary. The log
    # quotes the arguments as the client sent them, so keep their casing.
    for expected in (
        "EHLO from test",
        "sender accepted: FROM:<bob@example.com>",
        "recipient accepted: TO:<alice@example.com> (1 so far)",
        "start of message body (sender FROM:<bob@example.com>",
        "body + Subject: hi",
        "body + (blank line)",
        "body + the void stares back",
        "body finished: 3 lines / 37 bytes discarded",
        "-> 221 Bye: nothing was sent, nothing was stored",
    ):
        assert expected in log, f"{expected!r} missing from:\n{log}"


def test_verbose_logs_raw_bytes() -> None:
    """--verbose adds the raw wire bytes on top of the readable events."""
    server = Server()
    server._extra_args = ("--verbose",)
    with server:
        with SMTPClient(server.smtp_port) as client:
            client.ehlo()
        log = read_log(server)

    assert "raw b'EHLO test\\r\\n'" in log, log
    # The greeting is built from the *server's* hostname, so match it loosely
    # rather than hard-coding the name of whichever machine runs the tests.
    assert re.search(r"-> 250-\S+ greets you \(2 lines\)", log), log
    assert "   250 AUTH PLAIN LOGIN" in log, log


def b64(text: str) -> bytes:
    """base64 of a UTF-8 string, as SMTP AUTH arguments are encoded."""
    return base64.b64encode(text.encode("utf-8"))


def test_smtp_advertises_auth() -> None:
    """EHLO advertises AUTH PLAIN LOGIN without losing the rest of the reply."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        greeting, auth_line = client.send_multi(b"EHLO test", 2)
        # The greeting uses "250-" so clients keep reading to the AUTH keyword,
        # and the whole reply is exactly two lines so clients stay in sync.
        assert greeting.startswith(b"250-"), greeting
        assert b"greets you" in greeting, greeting
        assert auth_line == b"250 AUTH PLAIN LOGIN", auth_line
        # ...and the next reply is not displaced.
        assert client.send(b"NOOP").startswith(b"250 OK")


def test_smtp_auth_plain() -> None:
    """AUTH PLAIN accepts any user/password, with or without initial response."""
    with Server() as server:
        with SMTPClient(server.smtp_port) as client:
            client.ehlo()
            assert client.send(b"NOOP").startswith(b"250 OK")
            blob = b64("\0bob@example.com\0hunter2")
            response = client.send(b"AUTH PLAIN " + blob)
            # 235 is the RFC 4954 "authentication successful" code, which is how
            # the client is told its password was accepted.
            assert response.startswith(b"235 "), response
            assert b"Authentication successful" in response, response
            assert client.send(b"QUIT").startswith(b"221 ")

        # Same again, but the client does not send the initial response.
        with SMTPClient(server.smtp_port) as client:
            client.ehlo()
            assert client.send(b"AUTH PLAIN") == b"334 "
            assert client.send(b64("\0carol\0anything")).startswith(b"235 ")


def test_smtp_auth_plain_without_authzid() -> None:
    """Some clients send only "user\\0password" for PLAIN."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        client.ehlo()
        assert client.send(b"AUTH PLAIN " + b64("dave\0pw")).startswith(b"235 ")


def test_smtp_auth_login() -> None:
    """AUTH LOGIN runs the username/password challenge exchange."""
    with Server() as server:
        with SMTPClient(server.smtp_port) as client:
            client.ehlo()
            # The classic base64 of "Username:" and "Password:".
            assert client.send(b"AUTH LOGIN") == b"334 VXNlcm5hbWU6"
            assert client.send(b64("erin")) == b"334 UGFzc3dvcmQ6"
            assert client.send(b64("s3cr3t")).startswith(b"235 ")

        # smtplib sends LOGIN's first stage as an initial response.
        with SMTPClient(server.smtp_port) as client:
            client.ehlo()
            assert client.send(b"AUTH LOGIN " + b64("frank")) == b"334 UGFzc3dvcmQ6"
            assert client.send(b64("pw")).startswith(b"235 ")


def test_smtp_auth_rejections() -> None:
    """Unknown mechanisms and malformed payloads get proper errors."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        client.ehlo()
        assert client.send(b"AUTH CRAM-MD5").startswith(b"504 ")
        assert client.send(b"AUTH PLAIN !!!not base64!!!").startswith(b"535 ")
        assert client.send(b"AUTH").startswith(b"501 ")
        # A failed exchange must not desynchronise the connection.
        assert client.send(b"NOOP").startswith(b"250 OK")


def test_smtp_auth_cancel() -> None:
    """RFC 4954 lets a client abort the exchange with a bare "*"."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        client.ehlo()
        assert client.send(b"AUTH LOGIN") == b"334 VXNlcm5hbWU6"
        assert client.send(b"*").startswith(b"535 ")
        assert client.send(b"NOOP").startswith(b"250 OK")


def test_smtplib_login_works() -> None:
    """Python's smtplib can log in and then deliver a message."""
    if smtplib is None:
        return
    with Server() as server:
        client = smtplib.SMTP(HOST, server.smtp_port, timeout=10)
        try:
            client.ehlo("test")
            assert client.has_extn("auth"), client.esmtp_features
            assert set(client.esmtp_features["auth"].split()) == {"PLAIN", "LOGIN"}
            # 235 (or 503 for an already-authenticated session) means success.
            assert client.login("bob@example.com", "hunter2")[0] in (235, 503)
            assert client.sendmail(
                "bob@example.com",
                ["alice@example.com"],
                "Subject: hi\r\n\r\nnothing to see here\r\n",
            ) == {}
        finally:
            client.quit()


def test_smtp_auth_is_logged() -> None:
    """The AUTH exchange is visible on screen, without leaking the password."""
    with Server() as server:
        with SMTPClient(server.smtp_port) as client:
            client.ehlo()
            client.send(b"NOOP")
            client.send(b"AUTH PLAIN " + b64("\0bob@example.com\0hunter2"))
        log = read_log(server)

    for expected in (
        "250 AUTH PLAIN LOGIN",
        "AUTH PLAIN AGJvYkBleGFtcGxlLmNvbQBodW50ZXIy",
        "AUTH PLAIN: password of 7 characters",
        "AUTH PLAIN succeeded: user bob@example.com accepted, "
        "password accepted (any password is accepted)",
        "-> 235 2.7.0 Authentication successful",
    ):
        assert expected in log, f"{expected!r} missing from:\n{log}"
    assert "hunter2" not in log, "the password was logged"


def test_keypress_exits() -> None:
    """With --press-any-key-to-exit, a byte on stdin shuts the server down."""
    server = Server()
    server._extra_args = ("--press-any-key-to-exit",)
    with server:
        assert server._proc.stdin is not None
        server._proc.stdin.write(b"x")
        server._proc.stdin.flush()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and server._proc.poll() is None:
            time.sleep(0.05)
        assert server._proc.poll() is not None, "keypress did not stop the server"
        assert not Server.listening(server.port), "listener was left open"


def test_smtp_banner_is_branded() -> None:
    """The SMTP greeting names the project, which helps when debugging ports."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        assert b"cafe-port-110 SMTP" in client.banner, client.banner


def test_smtp_unsupported_mechanism_names_the_project() -> None:
    """A 504 for an unknown AUTH type mentions the project and what it does."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        client.ehlo()
        response = client.send(b"AUTH CRAM-MD5")
        assert response.startswith(b"504 "), response
        assert b"cafe-port-110 supports PLAIN LOGIN" in response, response


def test_mail_without_auth_is_accepted() -> None:
    """smtplib.sendmail() without login() still gets its mail thrown away."""
    with Server() as server, SMTPClient(server.smtp_port) as client:
        assert client.send(b"HELO test").startswith(b"250 ")
        assert client.send(b"MAIL FROM:<bob@example.com>").startswith(b"250 OK")
        assert client.send(b"RCPT TO:<alice@example.com>").startswith(b"250 OK")
        assert client.send(b"DATA").startswith(b"354 ")
        client.raw(b"Subject: no auth needed\r\n\r\nbody\r\n")
        assert client.send(b".").startswith(b"250 OK")


# --------------------------------------------------------------------------
# Unit tests for the pure helpers, imported from the module under test rather
# than exercised over a socket. These need no server process at all.
# --------------------------------------------------------------------------

def unit_tests():
    """Return the unit tests as (name, callable) pairs, import kept lazy."""
    import cafeport110

    def test_split_lines_keeps_partial_line() -> None:
        complete, pending = cafeport110.split_lines(b"USER bob\r\nPASS x")
        assert complete == [b"USER bob"], complete
        assert pending == b"PASS x", pending

    def test_split_lines_handles_empty_buffer() -> None:
        assert cafeport110.split_lines(b"") == ([], b"")

    def test_describe_escapes_control_bytes() -> None:
        assert cafeport110.describe(b"USER bob\r\n") == "USER bob"
        # A client must not be able to inject control characters into the log.
        assert cafeport110.describe(b"a\x1bb") == "a\\x1bb"
        assert cafeport110.describe(b"tab\there") == "tab\\x09here"
        # Non-ASCII bytes are decoded with replacement, then escaped, so a
        # client can never smuggle raw bytes past the log's ASCII rendering.
        assert cafeport110.describe(b"caf\xc3\xa9") == "caf\\xfffd\\xfffd"

    def test_reply_formatting() -> None:
        assert cafeport110.ok() == b"+OK\r\n"
        assert cafeport110.ok("hi") == b"+OK hi\r\n"
        assert cafeport110.err() == b"-ERR\r\n"
        assert cafeport110.err("nope") == b"-ERR nope\r\n"
        assert cafeport110.reply(250) == b"250\r\n"
        assert cafeport110.reply(250, "OK") == b"250 OK\r\n"
        # Non-ASCII text must not raise; it is replaced, never encoded blindly.
        assert cafeport110.reply(250, "caf\u00e9") == b"250 caf?\r\n"
        assert cafeport110.ok("caf\u00e9") == b"+OK caf?\r\n"

    def test_no_messages_is_a_terminated_multiline_reply() -> None:
        assert cafeport110.NO_MESSAGES == b"+OK\r\n.\r\n"

    def test_decode_base64_accepts_and_rejects() -> None:
        assert cafeport110.decode_base64("Ym9i") == b"bob"
        assert cafeport110.decode_base64("!!!not base64!!!") is None

    def test_make_protocol_picks_by_listening_port() -> None:
        assert isinstance(cafeport110.make_protocol(110, 25), cafeport110.POP3)
        assert isinstance(cafeport110.make_protocol(25, 25), cafeport110.SMTP)
        # A second POP3 listener (smtp_port is None) stays POP3.
        assert isinstance(cafeport110.make_protocol(110, None), cafeport110.POP3)

    def test_parse_args_defaults() -> None:
        import sys

        argv = sys.argv
        try:
            sys.argv = ["cafeport110.py"]
            args = cafeport110.parse_args()
        finally:
            sys.argv = argv
        assert args.port == 110
        assert args.smtp_port == 25
        assert args.no_smtp is False
        assert args.verbose is False
        # None means "decide from whether stdin is a terminal".
        assert args.press_any_key_to_exit is None

    def test_parse_args_keypress_flags() -> None:
        """Both spellings of the keypress switch must work on Python 3.8.

        argparse.BooleanOptionalAction is 3.9+, so this is a regression guard for
        the hand-rolled mutually exclusive pair that replaced it.
        """
        import sys

        argv = sys.argv
        try:
            for flag, expected in (
                ("--press-any-key-to-exit", True),
                ("--no-press-any-key-to-exit", False),
            ):
                sys.argv = ["cafeport110.py", flag]
                assert cafeport110.parse_args().press_any_key_to_exit is expected
            sys.argv = ["cafeport110.py", "--port", "11110", "--no-smtp"]
            args = cafeport110.parse_args()
            assert args.port == 11110
            assert args.no_smtp is True
            # Passing both spellings at once must be refused, as argparse does
            # for a mutually exclusive group; argparse exits rather than raising.
            sys.argv = [
                "cafeport110.py",
                "--press-any-key-to-exit",
                "--no-press-any-key-to-exit",
            ]
            try:
                cafeport110.parse_args()
            except SystemExit:
                pass
            else:
                raise AssertionError("both keypress flags were accepted together")
        finally:
            sys.argv = argv

    def test_no_3_9_only_stdlib_features() -> None:
        """The module must import on Python 3.8, as pyproject.toml claims."""
        source = (Path(__file__).with_name("cafeport110.py")).read_text(
            encoding="utf-8"
        )
        code = "\n".join(
            line.split("#", 1)[0] for line in source.splitlines()
        )
        # BooleanOptionalAction (3.9+) and str.removeprefix/removesuffix (3.9+)
        # are the easy accidents; argparse would blow up at parse_args() time.
        assert "BooleanOptionalAction" not in code, (
            "argparse.BooleanOptionalAction is not available on Python 3.8"
        )
        for method in ("removeprefix(", "removesuffix("):
            assert method not in code, f"{method} is not available on Python 3.8"

    return [
        (name, test)
        for name, test in sorted(locals().items())
        if name.startswith("test_") and callable(test)
    ]


def remove_stale_logs() -> int:
    """Delete log files a killed test run may have left behind."""
    removed = 0
    for leftover in Path(__file__).parent.glob("cafeport110-test-*.log"):
        try:
            leftover.unlink()
            removed += 1
        except (PermissionError, FileNotFoundError):
            pass
    return removed


if __name__ == "__main__":
    remove_stale_logs()
    tests = [
        (name, test)
        for name, test in sorted(globals().items())
        if name.startswith("test_") and callable(test)
    ]
    tests += unit_tests()
    for name, test in tests:
        test()
        print(f"{name}: ok")
    print(f"all {len(tests)} Cafe Port 110 tests passed")
