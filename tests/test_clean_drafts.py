"""Drafts built as whole messages (draft_mime) and saved through Mail's own
compose window (draft_gui). No Mail.app is touched here."""

import contextlib
import hashlib
import os
import plistlib
import stat
from email import message_from_bytes, policy
from pathlib import Path
from unittest.mock import patch

import pytest
from apple_mail_mcp import draft_gui, draft_mime

# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------


def _build(**kwargs):
    args = dict(subject="Offert åäö", sender="Ann <ann@example.com>", to="bo@example.com",
                text_body="Hej Bo,\n\nhär kommer offerten.\n<b>inte html</b>")
    args.update(kwargs)
    return draft_mime.build_draft(**args)


def test_draft_is_an_unsent_message_with_text_and_html():
    msg = _build()
    assert msg["X-Unsent"] == "1"
    assert msg["From"] == "Ann <ann@example.com>"
    assert msg["To"] == "bo@example.com"
    parsed = message_from_bytes(bytes(msg), policy=policy.default)
    plain = parsed.get_body(("plain",)).get_content()
    html = parsed.get_body(("html",)).get_content()
    assert "här kommer offerten." in plain
    assert "<div>här kommer offerten.</div>" in html
    assert "&lt;b&gt;inte html&lt;/b&gt;" in html  # plain text is escaped, not rendered
    assert "blockquote" not in html and "URLShare" not in html


def test_html_body_is_used_as_given_inside_body():
    msg = _build(text_body=None, html_body="<html><body><h1>Rubrik</h1><p>Text</p></body></html>")
    parsed = message_from_bytes(bytes(msg), policy=policy.default)
    html = parsed.get_body(("html",)).get_content()
    assert "<h1>Rubrik</h1><p>Text</p>" in html
    assert html.count("<body") == 1
    assert "Rubrik" in parsed.get_body(("plain",)).get_content()


def test_attachments_are_attached_with_their_bytes():
    item = draft_mime.DraftAttachment(path="/x/offer.pdf", filename="offer.pdf",
                                      content_type="application/pdf", data=b"%PDF-1.4 x")
    msg = _build(attachments=[item])
    parts = list(message_from_bytes(bytes(msg), policy=policy.default).iter_attachments())
    assert [(p.get_filename(), p.get_content()) for p in parts] == [("offer.pdf", b"%PDF-1.4 x")]


def test_signature_is_embedded_once_with_its_pictures():
    sig = draft_mime.Signature(id="SIG-1", html='<div>Ann</div><img src="cid:logo1">',
                               related=[(b"\x89PNG..", "image", "png", "logo1")])
    msg = _build(signature=sig)
    raw = bytes(msg)
    parsed = message_from_bytes(raw, policy=policy.default)
    html = parsed.get_body(("html",)).get_content()
    assert html.count('id="AppleMailSignature"') == 1
    assert msg["X-Apple-Mail-Signature"] == "SIG-1"
    assert b"multipart/related" in raw
    assert any(p.get("Content-ID") == "<logo1>" for p in parsed.walk())
    assert parsed.get_body(("plain",)).get_content().rstrip().endswith("Ann")


@pytest.mark.parametrize("bad", [
    dict(subject=""),
    dict(subject="two\nlines"),
    dict(to=""),
    dict(to="not an address"),
    dict(to="a@example.com\r\nBcc: evil@example.com"),
    dict(cc="x@example.com\nBcc: evil@example.com"),
    dict(text_body=None, html_body=None),
])
def test_invalid_input_is_refused(bad):
    with pytest.raises(draft_mime.DraftError):
        _build(**bad)


def test_several_recipients_with_names():
    msg = _build(to='"Bo B" <bo@example.com>, cy@example.com', cc="dee@example.com")
    assert msg["To"] == "Bo B <bo@example.com>, cy@example.com"
    assert msg["Cc"] == "dee@example.com"


# ---------------------------------------------------------------------------
# Attachment paths
# ---------------------------------------------------------------------------


def test_attachment_from_temp_folder_is_read(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"abc")
    [item] = draft_mime.load_attachments(str(f))
    assert (item.filename, item.content_type, item.data) == ("a.pdf", "application/pdf", b"abc")
    assert item.sha256 == hashlib.sha256(b"abc").hexdigest()


def test_attachment_list_and_comma_string(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a")
    b.write_text("b")
    assert [i.filename for i in draft_mime.load_attachments([str(a), str(b)])] == ["a.txt", "b.txt"]
    assert [i.filename for i in draft_mime.load_attachments(f"{a}, {b}")] == ["a.txt", "b.txt"]


def test_existing_path_with_comma_is_not_split(tmp_path):
    f = tmp_path / "Offert, rev 2.pdf"
    f.write_bytes(b"x")
    assert [i.filename for i in draft_mime.load_attachments(str(f))] == ["Offert, rev 2.pdf"]


@pytest.mark.parametrize("path", [
    "/etc/hosts",
    "~/.ssh/id_ed25519",
    "~/Library/Mail/V10/MailData/Envelope Index",
    "~/.config/memant/secrets.env",
])
def test_protected_attachments_are_refused(path):
    with pytest.raises(draft_mime.DraftError):
        draft_mime.load_attachments(path)


@pytest.mark.parametrize("name", [".env", ".env.local", "server.pem", "id_rsa", "credentials.json", "vault.kdbx"])
def test_secret_looking_names_are_refused(tmp_path, name):
    f = tmp_path / name
    f.write_text("x")
    with pytest.raises(draft_mime.DraftError):
        draft_mime.load_attachments(str(f))


def test_symlink_out_of_allowed_folders_is_refused(tmp_path):
    link = tmp_path / "hosts.txt"
    link.symlink_to("/etc/hosts")
    with pytest.raises(draft_mime.DraftError):
        draft_mime.load_attachments(str(link))


def test_attachment_limits(tmp_path, monkeypatch):
    files = []
    for i in range(draft_mime.MAX_ATTACHMENTS + 1):
        f = tmp_path / f"f{i}.txt"
        f.write_text("x")
        files.append(str(f))
    with pytest.raises(draft_mime.DraftError, match="At most"):
        draft_mime.load_attachments(files)
    monkeypatch.setattr(draft_mime, "MAX_ATTACHMENT_TOTAL_BYTES", 3)
    big = tmp_path / "big.bin"
    big.write_bytes(b"1234")
    with pytest.raises(draft_mime.DraftError, match="add up"):
        draft_mime.load_attachments(str(big))


def test_missing_attachment_is_refused(tmp_path):
    with pytest.raises(draft_mime.DraftError, match="not a file"):
        draft_mime.load_attachments(str(tmp_path / "nope.pdf"))


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

SIGNATURE_FILE = (
    b"Content-Transfer-Encoding: quoted-printable\n"
    b"Content-Type: text/html;\n\tcharset=utf-8\n"
    b"Mime-Version: 1.0 (Mac OS X Mail)\n\n"
    b"<body style=3D\"x\"><div>Ann Example</div><div>Example AB</div></body>"
)


def _signature_dir(tmp_path, accounts):
    with open(tmp_path / "AccountsMap.plist", "wb") as handle:
        plistlib.dump({uuid: {"AccountURL": "x", "Signatures": ids} for uuid, ids in accounts.items()}, handle)
    for ids in accounts.values():
        for sid in ids:
            (tmp_path / f"{sid}.mailsignature").write_bytes(SIGNATURE_FILE)
    return tmp_path


def test_single_signature_is_used(tmp_path):
    folder = _signature_dir(tmp_path, {"ACC": ["S1"]})
    sig, note = draft_mime.resolve_signature("ACC", selected={}, directory=folder)
    assert sig is not None and sig.id == "S1"
    assert sig.html == "<div>Ann Example</div><div>Example AB</div>"
    assert "added" in note


def test_selected_signature_wins(tmp_path):
    folder = _signature_dir(tmp_path, {"ACC": ["S1", "S2"]})
    sig, _ = draft_mime.resolve_signature("ACC", selected={"ACC": "S2"}, directory=folder)
    assert sig.id == "S2"


def test_ambiguous_or_missing_signature_is_skipped(tmp_path):
    folder = _signature_dir(tmp_path, {"ACC": ["S1", "S2"], "NONE": []})
    assert draft_mime.resolve_signature("ACC", selected={}, directory=folder)[0] is None
    assert draft_mime.resolve_signature("NONE", selected={}, directory=folder)[0] is None
    assert draft_mime.resolve_signature("OTHER", selected={}, directory=folder)[0] is None


def test_unreadable_signature_folder_is_skipped(tmp_path):
    sig, note = draft_mime.resolve_signature("ACC", selected={}, directory=tmp_path / "missing")
    assert sig is None and "no signature" in note


# ---------------------------------------------------------------------------
# The GUI transaction script
# ---------------------------------------------------------------------------


def test_gui_script_checks_before_every_press():
    script = draft_gui.gui_script('Offert "A" åäö', Path("/tmp/a b.eml"))
    assert 'set subj to "Offert \\"A\\" åäö"' in script
    assert 'open -g -a Mail " & quoted form of "/tmp/a b.eml"' in script
    # Send Again is pressed only when it says so and our viewer has the focus.
    assert 'is not "Send Again" then error' in script
    assert draft_gui.SEND_AGAIN_ID in script and draft_gui.SAVE_ID in script
    press = script.index('click menu item "Send Again"')
    assert script.index('is not "Send Again" then error') < script.index("another window took the focus\"") < press
    assert script.index("before saving") < script.index("click saveItem")
    # Focus is handed back on every path.
    assert script.index("end try\n\n-- Always") < script.index("set frontmost of process prevApp")


def test_gui_script_never_types_or_scripts_content():
    script = draft_gui.gui_script("S", Path("/tmp/x.eml"))
    for forbidden in ("keystroke", "key code", "content:", "set content", "outgoing message",
                      "send newMessage", "make new attachment", "the clipboard"):
        assert forbidden not in script


def test_save_eml_is_private_and_never_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_gui, "DRAFT_DIR", tmp_path / "drafts")
    msg = _build()
    first, second = draft_gui.save_eml(msg), draft_gui.save_eml(msg)
    assert first != second
    assert stat.S_IMODE(os.stat(first).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "drafts").st_mode) == 0o700
    assert first.read_bytes() == bytes(msg)


# ---------------------------------------------------------------------------
# Verification of the stored draft
# ---------------------------------------------------------------------------


def _emlx(raw: bytes) -> bytes:
    return f"{len(raw)}\n".encode() + raw + b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>'


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """Write a stored draft (id 77) and point the verifier at it."""
    monkeypatch.setattr(draft_gui, "REID_SETTLE_S", 0)
    monkeypatch.setattr(draft_gui, "POLL_S", 0.01)
    path = tmp_path / "77.emlx"

    def write(raw: bytes, found=((77, "drafts"),)):
        path.write_bytes(_emlx(raw))
        monkeypatch.setattr(draft_gui, "_new_messages", lambda *a: list(found))
        monkeypatch.setattr("apple_mail_mcp.emlx.find_message_file",
                            lambda mid: path if int(mid) == 77 else None)
        monkeypatch.setattr("apple_mail_mcp.emlx._store_root", lambda: tmp_path)
        return path

    return write


def _verify(**kwargs):
    args = dict(account_uuid="ACC", subject="Offert åäö", after_rowid=10,
                plain_body="Hej Bo,\n\nhär kommer offerten.", attachments=(), sender="ann@example.com",
                wait_s=0.2)
    args.update(kwargs)
    return draft_gui.verify_draft(**args)


def test_clean_stored_draft_passes(stored):
    item = draft_mime.DraftAttachment(path="/x/o.pdf", filename="o.pdf",
                                      content_type="application/pdf", data=b"%PDF x")
    stored(bytes(_build(attachments=[item])))
    result = _verify(attachments=[("o.pdf", item.sha256)])
    assert result.ok, result.lines
    assert result.draft_id == 77


def test_wrapped_draft_fails(stored):
    raw = bytes(_build(text_body=None, html_body=(
        '<div class="Apple-Mail-URLShareUserContentTopClass"></div>'
        '<div class="Apple-Mail-URLShareWrapperClass"><blockquote type="cite">'
        "<div>Hej Bo,</div><div><br></div><div>här kommer offerten.</div></blockquote></div>")))
    stored(raw)
    result = _verify()
    assert not result.ok and result.draft_id == 77
    assert "quote wrapper" in result.lines[0]


def test_changed_attachment_fails(stored):
    item = draft_mime.DraftAttachment(path="/x/o.pdf", filename="o.pdf",
                                      content_type="application/pdf", data=b"%PDF x")
    stored(bytes(_build(attachments=[item])))
    result = _verify(attachments=[("o.pdf", hashlib.sha256(b"other").hexdigest())])
    assert not result.ok and "o.pdf" in result.lines[0]


def test_wrong_sender_and_body_fail(stored):
    stored(bytes(_build(sender="Eve <eve@example.com>", text_body="something else")))
    result = _verify()
    assert not result.ok
    assert "sender" in result.lines[0] and "body differs" in result.lines[0]


def test_anything_in_sent_is_an_alarm(stored):
    stored(bytes(_build()), found=((77, "drafts"), (78, "sent")))
    result = _verify()
    assert not result.ok and "Sent/Outbox" in result.lines[0]


def test_no_draft_in_time_fails(stored):
    stored(bytes(_build()), found=())
    result = _verify(wait_s=0.05)
    assert not result.ok and result.draft_id is None


@pytest.mark.parametrize("path,kind", [
    ("Drafts", "drafts"), ("[Gmail]/Drafts", "drafts"), ("Utkast", "drafts"),
    ("Sent Items", "sent"), ("[Gmail]/Sent Mail", "sent"), ("Skickat", "sent"), ("Outbox", "sent"),
    ("INBOX", None), ("Projects/Draft ideas", None),
])
def test_box_kind(path, kind):
    assert draft_gui._box_kind(path) == kind


# ---------------------------------------------------------------------------
# The whole transaction, with Mail replaced
# ---------------------------------------------------------------------------


@pytest.fixture
def no_mail(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_gui, "_wait_for_user_pause", lambda: None)
    monkeypatch.setattr(draft_gui, "DRAFT_DIR", tmp_path / "drafts")
    monkeypatch.setattr(draft_gui, "_snapshot_max_rowid", lambda: 10)
    monkeypatch.setattr(draft_gui, "app_lock", lambda *a, **k: contextlib.nullcontext())


def _create():
    return draft_gui.create_in_mail(_build(), account_uuid="ACC", subject="Offert åäö",
                                    plain_body="x", attachments=(), sender="ann@example.com")


def test_abort_keeps_the_eml_and_reports_why(no_mail, tmp_path):
    with patch.object(draft_gui, "_run_applescript_unlocked",
                      return_value="ABORT: another window took the focus") as run, \
            patch.object(draft_gui, "verify_draft") as verify:
        result = _create()
    assert not result.ok
    assert "another window took the focus" in result.lines[0]
    assert result.eml_path is not None and result.eml_path.exists()
    verify.assert_not_called()
    gui_runs = [c for c in run.call_args_list if "open -g -a Mail" in c.args[0]]
    assert len(gui_runs) == 1


def test_accessibility_refusal_explains_the_fix(no_mail):
    with patch.object(draft_gui, "_run_applescript_unlocked",
                      side_effect=Exception("osascript is not allowed assistive access. (-25211)")):
        result = _create()
    assert not result.ok and "Accessibility" in result.lines[-1]


def test_success_removes_the_eml(no_mail):
    ok = draft_gui.DraftResult(True, ["Checked."], draft_id=77)
    with patch.object(draft_gui, "_run_applescript_unlocked", return_value="OK saved and closed"), \
            patch.object(draft_gui, "verify_draft", return_value=ok) as verify:
        result = _create()
    assert result.ok and result.draft_id == 77
    assert verify.call_args.args[2] == 10  # rows newer than the snapshot only
    assert list((draft_gui.DRAFT_DIR).iterdir()) == []


def test_failed_check_keeps_the_eml(no_mail):
    bad = draft_gui.DraftResult(False, ["Draft saved but failed the check: x."], draft_id=77)
    with patch.object(draft_gui, "_run_applescript_unlocked", return_value="OK saved and closed"), \
            patch.object(draft_gui, "verify_draft", return_value=bad):
        result = _create()
    assert not result.ok and result.eml_path is not None and result.eml_path.exists()


def test_busy_mail_lock_is_reported(no_mail, monkeypatch):
    from apple_mail_mcp.app_lock import AppBusyError

    @contextlib.contextmanager
    def busy(*a, **k):
        raise AppBusyError("Mail is busy with another client")
        yield

    monkeypatch.setattr(draft_gui, "app_lock", busy)
    with patch.object(draft_gui, "_run_applescript_unlocked") as run:
        result = _create()
    assert not result.ok and "busy" in result.lines[0]
    run.assert_not_called()


# ---------------------------------------------------------------------------
# Review fixes (2026-11-14)
# ---------------------------------------------------------------------------


def test_send_again_is_pressed_by_title_only_with_no_compose_window_open():
    script = draft_gui.gui_script("S", Path("/tmp/x.eml"))
    press = script.index('click menu item "Send Again" of menu "Message" of menu bar 1')
    assert script.index("a compose window is open in Mail") < press
    assert script.index("another window took the focus\"") < press
    assert "click sendAgain" not in script  # never a stored reference whose title can change


def test_cleanup_never_touches_windows_from_before():
    script = draft_gui.gui_script("S", Path("/tmp/x.eml"))
    assert script.index("already open; close it") < script.index("set opened to true")
    assert script.index("set opened to true") < script.index("do shell script \"open -g -a Mail")
    assert script.index("if opened then") < script.rindex("AXCloseButton")


def _sequence(monkeypatch, *answers):
    calls = iter(answers)
    last = answers[-1]
    monkeypatch.setattr(draft_gui, "_new_messages", lambda *a: list(next(calls, last)))


def test_draft_that_moves_to_sent_while_settling_is_an_alarm(stored, monkeypatch):
    stored(bytes(_build()))
    _sequence(monkeypatch, [(77, "drafts")], [(77, "sent")])
    result = _verify()
    assert not result.ok and "Sent/Outbox" in result.lines[0]


def test_draft_that_vanishes_while_settling_fails(stored, monkeypatch):
    stored(bytes(_build()))
    _sequence(monkeypatch, [(77, "drafts")], [])
    result = _verify()
    assert not result.ok and "gone" in result.lines[0]


def test_reid_takes_the_new_draft(stored, monkeypatch, tmp_path):
    path = stored(bytes(_build()))
    monkeypatch.setattr("apple_mail_mcp.emlx.find_message_file",
                        lambda mid: path if int(mid) in (77, 80) else None)
    _sequence(monkeypatch, [(77, "drafts")], [(80, "drafts")])
    result = _verify()
    assert result.ok and result.draft_id == 80


def test_whole_body_and_exact_sender_are_compared(stored):
    long_body = "Första stycket. " * 20 + "Slutet som ändrades."
    stored(bytes(_build(text_body=long_body.replace("ändrades", "ÄNDRAT AV NÅGON"),
                        sender="Jo <joann@example.com>")))
    result = _verify(plain_body=long_body, sender="ann@example.com")
    assert not result.ok
    assert "body differs" in result.lines[0] and "sender" in result.lines[0]


def test_timeout_still_gives_the_front_back(no_mail):
    calls = []

    def fake_run(script, timeout):
        calls.append(script)
        if "open -g -a Mail" in script:  # the GUI transaction
            raise Exception("AppleScript execution timed out after 90s")
        if "return name of first application process" in script:
            return "Terminal"
        return ""

    with patch.object(draft_gui, "_run_applescript_unlocked", side_effect=fake_run):
        result = _create()
    assert not result.ok and "timed out" in result.lines[0]
    assert any('set frontmost of process "Terminal" to true' in c for c in calls)


def test_save_waits_for_mail_to_be_in_front_and_reports_an_open_compose_window():
    script = draft_gui.gui_script("S", Path("/tmp/x.eml"))
    wait = script.index("if frontmost and (enabled of saveItem)")
    assert script.index("set composerOpen to true") < wait < script.index("click saveItem")
    assert "compose window with the message is left open in Mail" in script


def test_waits_for_a_pause_in_typing(monkeypatch):
    idle = iter([0.1, 0.4, 2.0])
    monkeypatch.setattr(draft_gui, "_user_idle_seconds", lambda: next(idle))
    monkeypatch.setattr(draft_gui.time, "sleep", lambda s: None)
    draft_gui._wait_for_user_pause(quiet_s=1.5, max_wait_s=60)
    assert next(idle, "done") == "done"
