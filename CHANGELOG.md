# Changelog

All notable changes to **mcp-apple-mail** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Message bodies are read from disk; the server never asks Mail for
  `content`.** `content of <message>` makes Mail convert HTML to text through
  legacy WebKit (NSHTMLReader) on its main thread. That cost 0.35-0.7 s per
  message, blocked every other client, and matches the stack of Mail's
  recurring ExcUserFault crash reports. The new `emlx.py` finds
  `~/Library/Mail/V*/…/<Box>.mbox/<uuid>/Data/<id digits>/Messages/<id>.emlx`
  (or `.partial.emlx`) and parses it with the stdlib email package, preferring
  text/plain and converting HTML itself. If the file is missing or
  unreadable, it falls back to `source of <message>` (raw MIME, no rendering).
  AppleScript now returns ids and metadata only. Body text search, content
  previews (`list_inbox_emails`, `search_emails`, `get_email_thread`, the
  dashboard), `get_needs_response`'s question check and `export_emails` use
  it. A test fails if any AppleScript in the package reads `content of`. On a
  2,335-message inbox, a full `body_text` search went from 138 s (stopped
  incomplete) to 11 s (complete).
  Details: only the live store (the newest `V<n>` with
  `MailData/Envelope Index`) is read, because an id in an older store is a
  different message; every inline text part is used in order, skipping
  attachments; an unknown charset decodes as UTF-8 or Latin-1 instead of
  failing; a file shorter than its declared length uses the source fallback;
  a missing id rescans the store at most once a minute; body tokens carry a
  per-call nonce, so a subject cannot pull in another message's body; and
  one deadline covers a whole `body_text` search, fallbacks and metadata
  fetch included. Structured script output in these paths separates fields
  and records with the ASCII unit and record separators instead of `|||` and
  line breaks.
- **`--read-only` is now a strict allowlist:** read mail, save attachments,
  export to local files, sync, and create or list drafts; nothing else.
  Previously it only removed the three send tools, so moving, flagging,
  marking, trashing (including `empty_trash`), creating mailboxes, and
  deleting or opening drafts all still worked. Tools not on
  `READ_ONLY_ALLOWED_TOOLS`, including any added later, are removed at
  startup. The mutating tools also refuse at call time with a
  "blocked by --read-only" error, and `manage_drafts` accepts only `create`
  and `list`. The README lists the exact set.

### Fixed
- **Timeouts on large mailboxes.** Mail answers one Apple Event per property
  read, so tools that walked a whole mailbox message by message ran into the
  120 s osascript kill on inboxes with thousands of messages. On a
  7,018-message inbox, `list_inbox_emails()` (default: all messages),
  `get_awaiting_reply` and `search_emails(body_text=...)` timed out, and
  `get_email_thread` and `get_statistics` took 70 to 90 s. These tools now
  pre-filter with `whose` clauses (subject, unread, date window) and fetch
  each property for a whole selection in one Apple Event:
  - `list_inbox_emails` defaults to the 20 newest messages per account
    (`max_emails=0` still returns everything). The text output now honours
    `account`, and `include_read=False` returns the N newest unread messages
    instead of the unread subset of the N newest.
  - `get_awaiting_reply` reads only inbox and sent mail inside `days_back`.
  - `get_needs_response`, `get_top_senders`, `get_email_thread`,
    `list_email_attachments`, `save_email_attachment`, `export_emails`
    (single email), `move_email` and `get_statistics` use filtered or bulk
    reads.
  - `search_emails(body_text=...)` applies every other filter in the `whose`
    clause first. It stops reading bodies after 120 s and marks the result as
    incomplete (`"incomplete": true` in JSON) instead of failing with a
    timeout.
  - The shared `lowercase` AppleScript handler no longer forks a shell per
    call (about 8 ms each, per message).
  - A timeout now returns an actionable error that states the limit and how
    to narrow the request.
- `--read-only` now also blocks `manage_drafts(action="send")`. The guard
  read a `READ_ONLY` value that was imported before `main()` set it, so it
  was always `False` and read-only mode still sent drafts.
- Removed the redundant `plugin/commands/email-management.md` slash command.
  It shadowed `plugin/skills/email-management/` under the same
  `apple-mail:email-management` listing key, so every session showed two
  near-duplicate entries with different descriptions (#82). Claude Code
  invokes skills directly as slash commands, so `/email-management` still
  works via the skill alone.
- `create_rich_email_draft`'s `save_as_draft` no longer claims or silently
  retries a save that can never succeed. Mail opens a `.eml` as a read-only
  message viewer, not a compose object, so the AppleScript `every outgoing
  message whose subject is ...` query can never match it — the old retry
  loop always returned `False` after ~5s of polling (#83). The tool now
  skips the futile retry and reports plainly that `save_as_draft` isn't
  supported for this workflow, pointing to `compose_email(mode="draft")` or
  `manage_drafts` for a real, sendable HTML draft.

## [3.2.0] - 2026-07-04

### Added
- **`get_email_source`** — new tool exposing an email's raw RFC 822 source
  (full headers + MIME body) for debugging threading, authentication results,
  and encoding issues (#66). Supports a `headers_only` mode and a configurable
  byte cap with an explicit truncation marker so huge messages can't flood the
  context. Uses Mail's `message id` property and resolves localized inbox
  names via `build_mailbox_ref`.
- **`list_inbox_emails` now surfaces `message_id`, `internet_message_id` and a
  `mail_link`** in both its JSON and text output, matching `search_emails`
  (#76) — inbox listings can be cited as Apple Mail deep links
  (`message://…`) and targeted by id without a second lookup.
  Backward-compatible with older 5-field records.

### Fixed
- **Stored XSS in the inbox dashboard** (#77). Attacker-controlled email
  subjects/senders containing `</script>` could break out of the dashboard's
  embedded JSON `<script>` block and inject arbitrary HTML. JSON injected into
  the template now escapes `<`, `>`, `&`, and U+2028/U+2029; remaining
  unescaped template interpolations in the account cards were also fixed.
- **Umlauts, ß, and emoji no longer garble when pasting HTML bodies into
  Mail** (#79). The Cocoa HTML→RTF importer auto-detected the charset and
  defaulted to Latin-1, double-decoding UTF-8 bytes into mojibake
  ("Grüße" → "GrÃ¼ÃŸe") on both the plain and HTML compose paths. The
  importer is now pinned to UTF-8 via `NSCharacterEncodingDocumentAttribute`.

## [3.1.8] - 2026-06-23

### Fixed
- **HTML replies/compositions no longer paste the body twice** (a rendered
  copy plus a second copy of the raw `<p>`/`<b>` source). `reply_to_email`,
  `compose_email`, and `forward_email` placed the raw HTML *source* on the
  clipboard under `NSPasteboardTypeHTML`; for any HTML that wasn't a complete
  document, Mail rendered it AND surfaced the literal markup, so the message
  appeared twice. The body is now converted to an `NSAttributedString` and
  written back as RTF (a single unambiguous rich-text flavor) plus a
  *rendered* plain-text fallback. `create_rich_email_draft` was unaffected
  (it already builds a proper multipart/alternative `.eml`).

### Added
- **Release workflow now builds and attaches the `.mcpb` bundle** to the GitHub
  Release automatically on each tag (verifies the bundle contains the
  `apple_mail_mcp` package and doesn't leak `venv/`/`__pycache__`).
- **`scripts/extract_changelog.py`** — pulls the current version's section from
  this file to use as the GitHub Release body.

## [3.1.7] - 2026-06-12

First 3.x release actually published to PyPI. Versions 3.0.0–3.1.6 were tagged
in git but never uploaded, so PyPI remained stuck on the broken 2.2.0 wheel and
`uvx mcp-apple-mail` kept failing with `ModuleNotFoundError: No module named
'apple_mail_mcp'`. This release ships the (already-correct) `plugin/` packaging
to PyPI and adds automation so a release can never again be built-but-not-shipped.

### Fixed
- **PyPI now serves a working wheel** that contains the `apple_mail_mcp/`
  package. Resolves the user-facing failure reported in #42 and #57.

### Added
- **Release automation via PyPI Trusted Publishing** (`.github/workflows/release.yml`).
  Pushing a `vX.Y.Z` tag builds, verifies, and publishes via OIDC — no API
  tokens stored. The build fails closed if the tag doesn't match the version or
  the wheel is missing the package.
- **CI packaging guard** (`.github/workflows/ci.yml`) — every push/PR builds the
  wheel and runs `verify_wheel.py`, plus the test suite on Python 3.10–3.13.
- **`scripts/verify_wheel.py`** — pre-publish artifact guard. Inspects a wheel
  for the `apple_mail_mcp/` package and payload size, then does a clean-venv
  install + import + entry-point check. Catches the 2.2.0-style "dist-info only"
  regression before it ships.
- **This `CHANGELOG.md`** (previously linked from `pyproject.toml` but missing)
  and **`RELEASING.md`** documenting the release flow and one-time PyPI setup.

### Notes
- The README's `uvx mcp-apple-mail` package name is correct — `mcp-apple-mail`
  is this project's PyPI distribution. (The similarly named `apple-mail-mcp` on
  PyPI is an unrelated package by a different author.) Issue #57's suggested
  rename would have pointed users at the wrong package, so it was not applied.

## [3.0.0] – [3.1.6] - 2026-03-27 → 2026-06-09 (git tags only, never on PyPI)

These versions were released as GitHub tags/MCPB bundles but were never uploaded
to PyPI. Highlights from this line, now reaching PyPI users via 3.1.7:

### Fixed
- Corrected `pyproject.toml` for the `plugin/` package layout so the wheel
  includes the `apple_mail_mcp/` package (the root fix for the 2.2.0 bug).
- Stop a Mail relaunch loop; prevent orphaned `osascript` from pinning Mail's
  main thread; poll for Mail focus before pasting to avoid silent empty sends.
- Attach files after the HTML paste so `Cmd+A` doesn't clobber them; scope
  `reply_to_email` attachment inserts to the reply message.
- Produce working Apple Mail deep links from search results; strip CDATA
  wrappers from body content; support localized inbox names (FR/DE/ES/IT/PT/NL/JA).
- Respect account default sender and allow `from_address` override.

### Added
- `synchronize_account` and `list_account_addresses` tools; `mail_link` in
  default search output.
- Version drift guard (`scripts/check_versions.py`) and `server.json` for MCP
  Registry validation.
- Plugin/marketplace distribution layout (`plugin/`), `/email-management` slash
  command.

### Changed
- Consolidated tools: 10 search tools → 2 (`search_emails` + `get_email_thread`);
  merged bulk operations into `manage.py`; trimmed verbose tool responses.

## [2.2.0] - 2026-03-27 (broken on PyPI — superseded by 3.1.7)

> **Do not use.** This wheel shipped only the dist-info and console-script entry
> point, not the `apple_mail_mcp/` package, so the server crashes on startup
> with `ModuleNotFoundError`. Fixed in 3.1.7. (Reported in #42, #57.)

[3.1.7]: https://github.com/patrickfreyer/apple-mail-mcp/releases/tag/v3.1.7
[2.2.0]: https://github.com/patrickfreyer/apple-mail-mcp/releases/tag/v2.2.0
