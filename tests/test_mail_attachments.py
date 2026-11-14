"""Attachments and inline pictures read from Mail's store on disk.

A downloaded message is often a ``.partial.emlx``: the attachment parts are
left empty in the file and Mail keeps their bytes in
``<mailbox>/Data/Attachments/<id>/<part>/<name>``. No Mail.app is touched here.
"""

import base64

import pytest
from apple_mail_mcp import mail_attachments as ma

PDF = b"%PDF-1.4\n% test pdf bytes\n"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

PARTIAL = (
    b"From: Bo <bo@example.com>\n"
    b"To: ann@example.com\n"
    b"Subject: Offert\n"
    b"MIME-Version: 1.0\n"
    b'Content-Type: multipart/mixed; boundary="OUTER"\n'
    b"\n"
    b"--OUTER\n"
    b'Content-Type: multipart/related; boundary="REL"\n'
    b"\n"
    b"--REL\n"
    b"Content-Type: text/html; charset=utf-8\n"
    b"\n"
    b'<p>Hej</p><img src="cid:logo@x">\n'
    b"--REL\n"
    b"Content-Type: image/png\n"
    b"Content-Disposition: inline\n"
    b"Content-ID: <logo@x>\n"
    b"Content-Transfer-Encoding: base64\n"
    b"\n" + base64.encodebytes(PNG) + b"--REL--\n"
    b"--OUTER\n"
    b'Content-Type: application/pdf; name="Offert 12.pdf"\n'
    b'Content-Disposition: attachment; filename="Offert 12.pdf"\n'
    b"Content-Transfer-Encoding: base64\n"
    b"X-Apple-Content-Length: 40\n"
    b"\n"
    b"--OUTER--\n"
)


def _emlx(raw: bytes) -> bytes:
    return f"{len(raw)}\n".encode() + raw + b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>'


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "V10"
    data = root / "ACC" / "INBOX.mbox" / "UUID" / "Data"
    messages = data / "Messages"
    messages.mkdir(parents=True)
    message = messages / "321.partial.emlx"
    message.write_bytes(_emlx(PARTIAL))
    stored_pdf = data / "Attachments" / "321" / "2" / "Offert 12.pdf"
    stored_pdf.parent.mkdir(parents=True)
    stored_pdf.write_bytes(PDF)
    monkeypatch.setattr("apple_mail_mcp.emlx._store_root", lambda: root)
    monkeypatch.setattr("apple_mail_mcp.emlx.find_message_file",
                        lambda mid: message if int(mid) == 321 else None)
    monkeypatch.setattr(ma, "_fetch_via_mail", lambda *a: pytest.fail("must not ask Mail"))
    return data


def test_parts_are_listed_with_imap_numbers(store):
    parts = {p.part: p for p in ma.list_parts(321)}
    assert set(parts) == {"1.2", "2"}  # 1.1 is the HTML body
    logo, pdf = parts["1.2"], parts["2"]
    assert logo.is_inline_picture and logo.source == "file" and logo.size == len(PNG)
    assert logo.content_id == "logo@x"
    assert (pdf.filename, pdf.source, pdf.size) == ("Offert 12.pdf", "disk", len(PDF))
    assert not pdf.is_inline_picture


def test_bytes_come_from_the_file_or_the_attachments_folder(store):
    pdf = ma.find_part(321, name="offert")
    assert ma.part_bytes(321, pdf, allow_mail=False) == PDF
    logo = ma.find_part(321, part="1.2")
    assert ma.part_bytes(321, logo, allow_mail=False) == PNG


def test_missing_bytes_are_reported_not_invented(store):
    (store / "Attachments" / "321" / "2" / "Offert 12.pdf").unlink()
    pdf = ma.find_part(321, part="2")
    assert pdf.source == "missing" and pdf.size == 30  # 40 base64 bytes
    with pytest.raises(ma.AttachmentError, match="not downloaded"):
        ma.part_bytes(321, pdf, allow_mail=False)


def test_a_link_out_of_the_store_is_not_followed(store, tmp_path):
    stored = store / "Attachments" / "321" / "2" / "Offert 12.pdf"
    stored.unlink()
    secret = tmp_path / "outside.txt"
    secret.write_text("not mail")
    stored.symlink_to(secret)
    assert ma.find_part(321, part="2").source == "missing"


def test_part_lookup_errors(store):
    with pytest.raises(ma.AttachmentError, match="no attachment part"):
        ma.find_part(321, part="9")
    with pytest.raises(ma.AttachmentError, match="Pass part"):
        ma.find_part(321)
    with pytest.raises(ma.AttachmentError, match="No attachment named"):
        ma.find_part(321, name="faktura")
    with pytest.raises(ma.AttachmentError, match="no file"):
        ma.list_parts(999)
    with pytest.raises(ma.AttachmentError, match="must be a number"):
        ma.list_parts("abc")


def test_iter_leaves_numbers_like_imap():
    from email import message_from_bytes, policy

    msg = message_from_bytes(PARTIAL, policy=policy.default)
    assert [n for n, _ in ma.iter_leaves(msg)] == ["1.1", "1.2", "2"]
    single = message_from_bytes(b"Content-Type: text/plain\n\nhi\n", policy=policy.default)
    assert [n for n, _ in ma.iter_leaves(single)] == ["1"]


def test_text_helpers():
    assert ma.is_text_type("text/csv") and ma.is_text_type("application/json")
    assert not ma.is_text_type("application/pdf")
    assert ma.decode_text("åäö".encode("latin-1"), "text/plain; charset=iso-8859-1") == "åäö"
    assert [ma.format_size(n) for n in (None, 580, 1536, 3 * 1024 * 1024)] == [
        "size unknown", "580 B", "2 KB", "3.0 MB"]


def test_read_tool_returns_the_picture_and_pdf_text(store, monkeypatch):
    from apple_mail_mcp.tools import attachments
    from fastmcp.utilities.types import Image

    out = attachments.read_email_attachment(321, part="1.2")
    assert isinstance(out, list) and isinstance(out[1], Image)
    assert "image/png" in out[0]

    monkeypatch.setattr(ma, "pdf_text", lambda data, max_pages: (1, "--- page 1 ---\nOffert rad ett, summa 12 000 kr"))
    text = attachments.read_email_attachment(321, attachment_name="Offert 12.pdf")
    assert "summa 12 000 kr" in text and "1 page(s)" in text


def test_read_tool_reports_errors_as_text(store):
    from apple_mail_mcp.tools import attachments

    assert attachments.read_email_attachment(321, part="9").startswith("Error:")


# ---------------------------------------------------------------------------
# Review fixes (2026-11-14): saving by id, protected folders, same names
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_save_by_id_writes_the_file(store, home):
    from apple_mail_mcp.tools import manage

    out = manage.save_email_attachment(message_id=321, part="2", save_path=str(home / "Downloads"))
    assert "saved successfully" in out
    assert (home / "Downloads" / "Offert 12.pdf").read_bytes() == PDF
    again = manage.save_email_attachment(message_id=321, part="2", save_path=str(home / "Downloads"))
    assert "already exists" in again


def test_save_never_writes_through_a_link(store, home, tmp_path):
    from apple_mail_mcp.tools import manage

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"keep me")
    (home / "Downloads" / "Offert 12.pdf").symlink_to(outside)
    out = manage.save_email_attachment(message_id=321, part="2", save_path=str(home / "Downloads"), overwrite=True)
    assert out.startswith("Error") and outside.read_bytes() == b"keep me"
    (home / "Downloads" / "Offert 12.pdf").unlink()
    (home / "Downloads" / "Offert 12.pdf").symlink_to(tmp_path / "dangling.pdf")
    out = manage.save_email_attachment(message_id=321, part="2", save_path=str(home / "Downloads"))
    assert out.startswith("Error") and not (tmp_path / "dangling.pdf").exists()


def test_protected_folders_ignore_case(store, home):
    from apple_mail_mcp import draft_mime
    from apple_mail_mcp.tools import manage

    (home / ".config").mkdir()
    secret = home / ".config" / "token.json"
    secret.write_text("x")
    with pytest.raises(draft_mime.DraftError, match="protected"):
        draft_mime.load_attachments(str(home / ".CoNfIg" / "token.json"))
    out = manage.save_email_attachment(message_id=321, part="2", save_path=str(home / ".SSH" / "x.pdf"))
    # Refused by the shared path guard (upstream wording) or by this tool's own check.
    assert out.startswith("Error") and ("protected location" in out or "sensitive directory" in out)


def test_same_named_parts_are_never_fetched_by_name(tmp_path, monkeypatch):
    raw = (b"Content-Type: multipart/mixed; boundary=B\n\n--B\nContent-Type: text/plain\n\nhi\n"
           b"--B\nContent-Type: application/pdf\nContent-Disposition: attachment; filename=invoice.pdf\n"
           b"Content-Transfer-Encoding: base64\nX-Apple-Content-Length: 40\n\n"
           b"--B\nContent-Type: application/pdf\nContent-Disposition: attachment; filename=invoice.pdf\n"
           b"Content-Transfer-Encoding: base64\nX-Apple-Content-Length: 40\n\n--B--\n")
    message = tmp_path / "Messages" / "5.partial.emlx"
    message.parent.mkdir()
    message.write_bytes(_emlx(raw))
    monkeypatch.setattr("apple_mail_mcp.emlx._store_root", lambda: tmp_path)
    monkeypatch.setattr("apple_mail_mcp.emlx.find_message_file", lambda mid: message)
    monkeypatch.setattr(ma, "_fetch_via_mail", lambda *a: pytest.fail("must not fetch by an ambiguous name"))
    item = ma.find_part(5, part="3")
    with pytest.raises(ma.AttachmentError, match="share that name"):
        ma.part_bytes(5, item)
