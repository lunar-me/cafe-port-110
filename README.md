# Cafe Port 110

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/downloads/)

**A dummy POP3 and SMTP server in one file.** It accepts any user name and any
password, reads every message off the wire, and throws it all away. Nothing is
stored, nothing is relayed, nothing is sent.

Use it when you need something that *answers* on ports 110 and 25: pointing an
email client at a build for a smoke test, checking that a health probe treats
"server is up" as healthy, watching exactly which commands a library sends, or
filling a port that would otherwise be closed.

```
$ python cafeport110.py
2026-01-01 12:00:00.000 Listening for POP3 on port 110
2026-01-01 12:00:00.000 Listening for SMTP on port 25
2026-01-01 12:00:00.000 Logging every POP3 and SMTP event; --verbose adds the raw bytes and full message bodies
2026-01-01 12:00:00.000 Press any key to exit
2026-01-01 12:00:07.123 POP3[0 127.0.0.1:51422] connected
2026-01-01 12:00:07.124 POP3[0 127.0.0.1:51422] banner: sent
2026-01-01 12:00:07.124 POP3[0 127.0.0.1:51422] -> +OK <12345.678@myhost>
2026-01-01 12:00:07.125 POP3[0 127.0.0.1:51422] <- USER bob
2026-01-01 12:00:07.125 POP3[0 127.0.0.1:51422] user name given: bob
2026-01-01 12:00:07.125 POP3[0 127.0.0.1:51422] -> +OK pretending your mailbox exists
```

## Install and run

The whole server is one Python file that only needs
[Trio](https://trio.readthedocs.io/). Any of these work:

```bash
# 1. Run it straight out of a clone (no install at all)
python cafeport110.py

# 2. Install the console script
pip install .
cafe-port-110

# 3. Run it with uv, without installing
uvx --from git+https://github.com/lunar-me/cafe-port-110 cafe-port-110
```

On Windows you can also double-click `_start_me.bat`, which runs the server with
the interpreter from a local `.venv`.

Binding to ports 110 and 25 needs elevated privileges on most Linux and macOS
systems (below 1024). Either run as root, or pick high ports with `--port` and
`--smtp-port`.

## Command-line options

| Option | Default | Meaning |
| --- | --- | --- |
| `--port PORT` | `110` | Listen for POP3 on `PORT`. |
| `--smtp-port PORT` | `25` | Listen for SMTP on `PORT`. |
| `--no-smtp` | off | Don't run the SMTP server at all. |
| `--verbose` | off | Log debug messages, including the raw bytes on the wire and every body line. |
| `--press-any-key-to-exit` / `--no-press-any-key-to-exit` | on when stdin is a terminal | Exit when a key is pressed — no Enter needed. Handy on Windows, where Ctrl+C is easy to fumble. |

The protocol for an incoming connection is chosen by **which local port it
arrived on**, not by sniffing the first bytes. If you set both ports to the same
value, that port speaks SMTP.

## What the server answers

Everything is answered politely and forgotten immediately. Passwords are never
echoed at full length, and no message is ever written to disk.

### POP3 (RFC 1939)

| Command | Reply |
| --- | --- |
| (greeting) | `+OK <timestamp@hostname>` |
| `USER <name>` | `+OK pretending your mailbox exists` |
| `PASS <secret>` | `+OK how did you know` — any password is accepted |
| `APOP <name> <digest>` | `+OK pretending your mailbox exists` |
| `STAT` | `+OK 0 0` — the mailbox is always empty |
| `LIST`, `UIDL` | `+OK` then a lone `.` |
| `LIST n`, `UIDL n`, `RETR n`, `DELE n`, `TOP n m` | `-ERR no such message` |
| `NOOP`, `RSET` | `+OK` |
| `QUIT` | `+OK goodbye`, then the connection closes |
| anything else | `-ERR unrecognized command` |

### SMTP (RFC 5321, with AUTH from RFC 4954)

| Command | Reply |
| --- | --- |
| (greeting) | `220 <hostname> cafe-port-110 SMTP` |
| `HELO <name>` | `250 <hostname> greets you` |
| `EHLO <name>` | Two lines: the greeting with a `250-` continuation, then `250 AUTH PLAIN LOGIN` |
| `AUTH PLAIN`, `AUTH LOGIN` | Any credentials are accepted: `235 2.7.0 Authentication successful` |
| `AUTH <other>` | `504 5.5.4 Unrecognized authentication type` |
| `MAIL FROM:<...>` | `250 OK: sender accepted, and promptly forgotten` |
| `RCPT TO:<...>` | `250 OK: recipient accepted, and promptly forgotten` |
| `DATA` | `354 ...`; the body (dot-stuffing and all) is read and discarded, then `250 OK: queued as CAFE` |
| `RSET`, `NOOP`, `VRFY`, `EXPN`, `HELP` | `250 OK` with a quip |
| `QUIT` | `221 Bye: nothing was sent, nothing was stored` |
| anything else | `250 OK: <command> accepted, and ignored` |

Sending mail **without** authenticating is allowed too, so clients that skip
`AUTH` still work end to end.

## Try it by hand

```bash
# POP3: any credentials are fine
python -c "import poplib; p = poplib.POP3('127.0.0.1', 110); p.user('bob'); p.pass_('hunter2'); print(p.stat()); p.quit()"

# SMTP: authenticate and "deliver" a message that goes nowhere
python -c "import smtplib; s = smtplib.SMTP('127.0.0.1', 25); s.ehlo(); s.login('bob@example.com', 'hunter2'); s.sendmail('bob@example.com', ['alice@example.com'], 'Subject: hi\r\n\r\nnothing to see here\r\n'); s.quit()"
```

Point Thunderbird, Outlook or `mutt` at `127.0.0.1:110` / `127.0.0.1:25` and
watch the log fill up with exactly what the client is doing, which is the usual
reason to run this thing.



## Development

```bash
git clone https://github.com/lunar-me/cafe-port-110
cd cafe-port-110
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements-dev.txt
```

`requirements.txt` holds the runtime dependency (Trio, and nothing else);
`requirements-dev.txt` adds pytest and pulls the former in, so it is the one to
install while working on the project. Both are deliberately unpinned beyond a
lower bound, because `pyproject.toml` advertises Python 3.8 support and pinning
would quietly take that away.

The tests start a real subprocess on ephemeral ports and speak POP3 and SMTP to
it over a socket, so they cover the same path as a user running the program, plus
unit tests for the small pure helpers:

```bash
python test_cafeport110.py    # no pytest needed
pytest test_cafeport110.py    # or via pytest
```

There is no linting or build step; it is one module.

## Design notes

A few decisions in the code are load-bearing and easy to break by accident:

- **Only two lines in the EHLO reply.** Clients discard the first EHLO line as
  the greeting and read extensions from the rest, so `AUTH` has to be on the
  second line — but every extra line makes it likelier that a buffered reader
  returns only the first line, after which the client is permanently one reply
  behind (for example `smtplib.data()` would read the answer to `RCPT` instead
  of `354`). Two lines keeps both properties.
- **The greeting uses `250-`.** A plain `250 ` on the first line makes a client
  stop reading before it ever sees the `AUTH` keyword.
- **The AUTH `LOGIN` challenges are fixed.** `VXNlcm5hbWU6` and `UGFzc3dvcmQ6`
  are the base64 of `Username:` and `Password:`, which `smtplib` and most mail
  clients expect exactly.
- **Control bytes are escaped in the log.** `describe()` renders non-printable
  bytes as `\x0d`, `\x1b` and friends, so a client cannot log-inject escape
  sequences into the operator's terminal.
- **Bodies are summarised.** The first `MAX_LOGGED_BODY_LINES` body lines are
  shown; the rest appear only with `--verbose`, because message bodies are
  usually the least interesting part of a session and can be enormous.

## Limitations

- Nothing is stored: sent mail is discarded, and the POP3 mailbox is always
  empty, so `RETR` never returns a message.
- No TLS/STARTTLS, no SMTP relay, no delivery to real addresses.
- This is a test double, not a mail server. **Never point real mail at it** on a
  reachable interface: it accepts any credentials and has no access control.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 lunar-me.
