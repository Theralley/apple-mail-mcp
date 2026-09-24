"""Tests for reading message bodies from Mail's on-disk .emlx store.

All fixtures are synthetic: a fake ~/Library/Mail/V10 tree is built in a
temporary directory, so these run in CI without Mail.app.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from apple_mail_mcp import emlx

PACKAGE = Path(emlx.__file__).resolve().parent
PLIST = b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict/></plist>\n'


def _emlx_bytes(raw: bytes) -> bytes:
    return f"{len(raw)}\n".encode() + raw + PLIST


def _raw(headers: str, body: bytes) -> bytes:
    return headers.replace("\n", "\r\n").encode() + b"\r\n" + body


PLAIN = _raw("Subject: Plain\nContent-Type: text/plain; charset=utf-8\n", "Hej, fungerar det?\nRad två".encode())
HTML_ONLY = _raw(
    "Subject: Html\nContent-Type: text/html; charset=utf-8\n",
    b"<html><head><style>p{color:red}</style><script>alert(1)</script></head>"
    b"<body><p>First&nbsp;line</p><div>Second<br>Third &amp; more</div></body></html>",
)
ALTERNATIVE = _raw(
    'Subject: Alt\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary="b1"\n',
    b"--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain version\r\n"
    b"--b1\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>html version</p>\r\n--b1--\r\n",
)
EMPTY_PLAIN_ALTERNATIVE = _raw(
    'Subject: Empty plain\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary="e1"\n',
    b"--e1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n \r\n"
    b"--e1\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>real body</p>\r\n--e1--\r\n",
)
QUOTED_PRINTABLE = _raw(
    "Subject: QP\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: quoted-printable\n",
    b"R=C3=A4kning bifogad, betala senast fredag. En l=C3=A5ng rad som fort=\r\ns=C3=A4tter",
)
LATIN1 = _raw(
    "Subject: Latin\nContent-Type: text/plain; charset=iso-8859-1\n",
    "Återbetalning för maj".encode("iso-8859-1"),
)
BASE64_HTML = _raw(
    "Subject: B64\nContent-Type: text/html; charset=utf-8\nContent-Transfer-Encoding: base64\n",
    b"PHA+SGVsbG8gPGI+d29ybGQ8L2I+PC9wPg==",  # <p>Hello <b>world</b></p>
)
PARTIAL_WITH_BODY = _raw(
    'Subject: Partial\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="m1"\n',
    b"--m1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nbody kept on disk\r\n"
    b"--m1\r\nContent-Type: application/pdf; name=a.pdf\r\nContent-Disposition: attachment; filename=a.pdf\r\n"
    b"X-Apple-Content-Length: 12345\r\n\r\n\r\n--m1--\r\n",
)
PARTIAL_WITHOUT_BODY = _raw(
    'Subject: Stub\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary="s1"\n',
    b"--s1\r\nContent-Type: text/plain; charset=utf-8\r\nX-Apple-Content-Length: 900\r\n\r\n\r\n--s1--\r\n",
)


@pytest.fixture
def mail_store(tmp_path, monkeypatch):
    """A fake Mail store with a nested, localised Gmail-style mailbox."""
    monkeypatch.setattr(emlx, "MAIL_DIR", tmp_path)
    monkeypatch.setattr(emlx, "_data_dirs", None)
    monkeypatch.setattr(emlx, "_path_cache", {})
    inbox = tmp_path / "V10" / "ACCT-1" / "INBOX.mbox" / "UUID-A" / "Data"
    gmail = tmp_path / "V10" / "ACCT-2" / "[Gmail].mbox" / "All e-post.mbox" / "UUID-B" / "Data"
    (tmp_path / "V10" / "MailData").mkdir(parents=True)

    def put(data_dir, message_id, raw, partial=False):
        folder = data_dir / emlx.message_subpath(message_id)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{message_id}.partial.emlx" if partial else f"{message_id}.emlx"
        (folder / name).write_bytes(_emlx_bytes(raw))

    put(inbox, 51, PLAIN)
    put(inbox, 999, HTML_ONLY)
    put(gmail, 1000, ALTERNATIVE)
    put(gmail, 52102, QUOTED_PRINTABLE)
    put(gmail, 52103, LATIN1)
    put(inbox, 7, BASE64_HTML)
    put(gmail, 52104, PARTIAL_WITH_BODY, partial=True)
    put(gmail, 52105, PARTIAL_WITHOUT_BODY, partial=True)
    put(gmail, 2001, EMPTY_PLAIN_ALTERNATIVE)
    return tmp_path


@pytest.mark.parametrize(
    "message_id, expected",
    [(51, "Messages"), (999, "Messages"), (1000, "1/Messages"), (52102, "2/5/Messages"), (123456, "3/2/1/Messages")],
)
def test_message_subpath(message_id, expected):
    assert emlx.message_subpath(message_id) == Path(expected)


def test_finds_files_in_nested_localised_mailboxes(mail_store):
    path = emlx.find_message_file(52102)
    assert path is not None
    assert path.relative_to(mail_store).parts[-6:] == ("UUID-B", "Data", "2", "5", "Messages", "52102.emlx")
    assert emlx.find_message_file(52104).name == "52104.partial.emlx"
    assert emlx.find_message_file(424242) is None


@pytest.mark.parametrize(
    "message_id, expected",
    [
        (51, "Hej, fungerar det?\nRad två"),
        (999, "First line\n\nSecond\nThird & more"),
        (1000, "plain version"),
        (52102, "Räkning bifogad, betala senast fredag. En lång rad som fortsätter"),
        (52103, "Återbetalning för maj"),
        (7, "Hello world"),
        (52104, "body kept on disk"),
        (2001, "real body"),
    ],
    ids=[
        "plain", "html-only", "alternative-prefers-plain", "quoted-printable",
        "iso-8859-1", "base64-html", "partial", "empty-plain-uses-html",
    ],
)
def test_body_from_disk_never_calls_mail(mail_store, message_id, expected):
    with patch.object(emlx, "_source_via_applescript") as fallback:
        assert emlx.get_message_body(message_id) == expected
    fallback.assert_not_called()


def test_html_drops_script_and_style(mail_store):
    body = emlx.get_message_body(999, allow_fallback=False)
    assert "alert" not in body and "color" not in body


def test_partial_without_body_falls_back_to_source(mail_store):
    with patch.object(emlx, "_source_via_applescript", return_value=PLAIN.decode()) as fallback:
        body = emlx.get_message_body(52105, "Work", "INBOX")
    fallback.assert_called_once_with(52105, "Work", "INBOX")
    assert body == "Hej, fungerar det?\nRad två"


def test_missing_file_falls_back_to_source_not_content(mail_store):
    scripts = []

    def fake_run(script, timeout=120):
        scripts.append(script)
        return PLAIN.decode()

    with patch("apple_mail_mcp.core.run_applescript", side_effect=fake_run):
        body = emlx.get_message_body(888888, "Work", "INBOX")

    assert body == "Hej, fungerar det?\nRad två"
    assert len(scripts) == 1
    assert "source of (first message of targetMailbox whose id is 888888)" in scripts[0]
    assert "content of" not in scripts[0]


def test_missing_everywhere_returns_none(mail_store):
    with patch("apple_mail_mcp.core.run_applescript", return_value=""):
        assert emlx.get_message_body(888888) is None


def test_fill_body_tokens(mail_store):
    text = "\n".join(
        [
            "✉ Subject",
            "   Content: ⟦body:51|Work|INBOX⟧",
            "   Content: ⟦body:888888|Work|INBOX⟧",
            "tail",
        ]
    )
    with patch.object(emlx, "_source_via_applescript", return_value=None):
        filled = emlx.fill_body_tokens(text, 10, missing="[Not available]")
        dropped = emlx.fill_body_tokens(text, 10, missing=None)
    assert filled.split("\n") == ["✉ Subject", "   Content: Hej, funge...", "   Content: [Not available]", "tail"]
    assert dropped.split("\n") == ["✉ Subject", "   Content: Hej, funge...", "tail"]


def test_preview_collapses_line_breaks_and_truncates():
    assert emlx.preview("a\nb\r\nc\td", 0) == "a b  c\td"
    assert emlx.preview("a\nb\r\nc\td", 0, strip_tabs=True) == "a b  c d"
    assert emlx.preview("abcdef", 3) == "abc..."
    assert emlx.preview(None, 3) is None


def test_package_never_asks_mail_for_content():
    """No AppleScript in the package may read `content of <message>`.

    Asking Mail for content makes it render HTML through WebKit on its main
    thread (the ExcUserFault_Mail crash frame). Bodies come from emlx.py.
    """
    pattern = re.compile(r"\bcontent\s+of\s+[\w{(]", re.IGNORECASE)
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(PACKAGE)}:{number}: {line.strip()}")
    assert offenders == []


def test_content_grep_would_catch_the_old_pattern():
    pattern = re.compile(r"\bcontent\s+of\s+[\w{(]", re.IGNORECASE)
    assert pattern.search("set msgContent to content of aMessage")
    assert pattern.search("set msgContent to content of {message_var}")
    assert not pattern.search("never requests ``content of <message>``")
