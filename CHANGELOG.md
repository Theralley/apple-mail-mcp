# Changelog

All notable changes to **mcp-apple-mail** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Local search index of all mail: `index_mail` and `search_mail_index`.**
  Searching through Mail with AppleScript took 4-15 s per mailbox and up to
  100 s (incomplete) across accounts. The index is built from the Envelope
  Index and the `.emlx` files only (no AppleScript for bodies), in SQLite
  FTS5 (`unicode61`, diacritics kept so å/ä/ö stay letters of their own) at
  `~/Library/Caches/apple-mail-mcp/` (0700/0600; `APPLE_MAIL_MCP_INDEX_PATH`).
  `index_mail` covers every mailbox by default (`scope="all"`: inbox, sent
  and every other folder, Gmail labels respected); `scope="inbox,sent"` or a
  single folder narrows it, and later searches keep that scope. A message
  that moves between mailboxes is updated in place. Newest first in
  committed batches within a time budget, so a first run resumes; one writer
  at a time (flock), readers never wait (WAL). Bodies not downloaded yet are
  counted and retried. Searches top the index up first and skip that in
  ~10 ms when Mail has no new message. Measured on one Mac: 53,214 messages
  (all mailboxes) in 173 s, 294 MB; searches 2-40 ms. Queries support
  prefixes, phrases and `subject:`/`from:`/`to:`/`attachment:`/`body:`;
  every term is quoted, so FTS5 syntax typed by a caller is never executed.
  `folder` filters to `inbox`, `sent`, `both` or `other`.
- **Read and unread state in the index, and `set_email_read_status`.**
  `search_mail_index(read_status="unread")` returns only unread messages
  (and `"read"` only read ones), with the live state read from Mail's
  database at search time, so marking a message in Mail shows up at once;
  hits show `· unread`, and `mailbox=` narrows to one mailbox. `status`
  reports the unread count in the inbox. `read_email` states whether a
  message is read and does not change it. The new
  `set_email_read_status(message_ids, read=True)` marks up to 50 messages
  read or unread by message ID (one exact `id` lookup per message, no
  `whose` scan), skips messages already in that state, and confirms each
  change in Mail's database before reporting it. This makes a task like
  "go through the unread Railway mails, fix each one and mark it read when
  done" possible. It is the only state change allowed under `--read-only`;
  flags, moves and deletes stay blocked.
- **`read_email(message_id)`**: one message by id from Mail's files (headers,
  body text, attachment parts, read state), in any mailbox, without asking
  Mail. Takes upstream's `max_chars`, `include_headers` and
  `output_format="json"`; `account` and `mailbox` are accepted and ignored.
- **`read_email_attachment`**: an attachment or inline picture as something a
  client can use. Pictures come back as images (HEIC/TIFF/oversized ones are
  converted to JPEG with `sips`), PDFs as text (PDFKit), text-like files as
  text. Allowed under `--read-only`.
- **Attachments by message id.** `list_email_attachments(message_id=...)` and
  `save_email_attachment(message_id=..., part=...)` work for any message in
  any mailbox, and include inline pictures. Mail keeps most downloaded mail
  as `.partial.emlx` with empty attachment parts; the bytes are read from
  `Attachments/<id>/<part>/` beside it (never following a link out of Mail's
  store), and Mail is asked to save the file only when it is not on disk.
  Saving refuses dotfile folders and existing files unless `overwrite=True`.
- **`manage_drafts` `create` takes `html_body`, `attachments` and
  `include_signature`** (default on: the account's own Mail signature, as
  Mail embeds it; skipped when ambiguous).

- **Skill `apple-mail-mcp`**: which tool does what, how to check that the
  server starts, reconnect it in a running session, reinstall it from a
  checkout, and upgrade past the `mcp<2` pin.

### Changed
- **`manage_drafts` `delete` is allowed under `--read-only`, and never
  guesses.** Replacing a draft with a corrected copy left two drafts with
  the same subject and no way to remove the old one. Delete used to take the
  first draft whose subject contained the keyword; now the match is made in
  Python, and when more than one draft matches nothing is deleted and the
  matches are listed with their IDs. `list` shows each draft's `ID`, and the
  new `draft_id` parameter deletes exactly that draft (`whose id is N`, the
  value must be a number). Only the Drafts mailbox is touched; deleting
  received mail, `send` and `open` stay blocked under `--read-only`.
  Measured: Mail autosaves a newly created draft once, about 20 seconds
  after `create`, as a new message; a draft deleted before that comes back
  as the autosaved copy (README).
- **Listings, counts and searches read Mail's Envelope Index instead of
  asking Mail for messages.** `every message of <mailbox>`, `count of
  messages`, bulk property reads and whose-queries make Mail run a full
  database query on its main thread (`-[MFMailbox(ScriptingSupport)
  messages]` in a spindump of a hang); `list_inbox_emails` with its defaults
  hit the 120 s timeout. The new `envelope_index.py` opens
  `~/Library/Mail/V<n>/MailData/Envelope Index` read-only (SQLite
  `mode=ro`, `query_only`, retrying while Mail holds a lock) and answers
  `list_inbox_emails`, `list_mailboxes` counts, `get_inbox_overview`,
  `get_statistics`, `get_awaiting_reply`, `get_needs_response`,
  `get_top_senders`, `search_emails` (filters, and the candidate ids of a
  `body_text` search), `get_email_thread` and the dashboard's recent emails.
  Gmail messages kept in "All Mail" count in INBOX and other mailboxes
  through their labels; deleted messages are left out. Output is unchanged:
  account names and mailbox order still come from Mail (names only, never
  messages) and dates are formatted by osascript as before. If the database
  is missing, unreadable (no Full Disk Access) or has an unexpected schema,
  or an account or mailbox cannot be resolved, the tool runs its AppleScript
  as before; the reason is logged to stderr once. `APPLE_MAIL_MCP_NO_INDEX=1`
  forces AppleScript. `scripts/parity_check.py` compares both paths live.
- **Gmail and Swedish accounts.** `search_emails(mailbox="All")` lists a
  message once, under its label mailbox (INBOX, ...) rather than again under
  All Mail. `get_statistics` counts each message once instead of once per
  mailbox it is in. Swedish special folders (Skickat, Skickade meddelanden,
  Papperskorgen, Borttagna objekt, Skräppost, Utkast) are skipped like their
  English names, and `get_awaiting_reply` / `get_needs_response` find a
  Sent mailbox under any name in `SENT_MAILBOX_NAMES` (e.g. Gmail's
  "Skickat"). Both the AppleScript and the Envelope Index paths do this; the
  index recognises All Mail by its role (`mailboxes.source`), names are the
  fallback.
- **`search_emails(mailbox="All")` skipped no folder.** Its script compared
  each mailbox name with a `repeat with f in list` item reference, which is
  never equal in AppleScript, so Trash, Junk, Sent and Drafts were always
  searched. It now uses `is in`.
- **Nested mailboxes listed by Mail at the top** (Exchange accounts list
  every mailbox under its own name, e.g. "Inbox/Notes" as "Notes") are
  matched to the right mailbox in the Envelope Index, each once.
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
  export to local files, sync, create, list and delete drafts, and mark mail
  read or unread by message ID; nothing else.
  Previously it only removed the three send tools, so moving, flagging,
  marking, trashing (including `empty_trash`), creating mailboxes, and
  deleting or opening drafts all still worked. Tools not on
  `READ_ONLY_ALLOWED_TOOLS`, including any added later, are removed at
  startup. The mutating tools also refuse at call time with a
  "blocked by --read-only" error, and `manage_drafts` accepts only `create`
  and `list`. The README lists the exact set.

### Fixed
- **Drafts were sent as a quote ("> " on every line).** On macOS 27 Mail
  stores any body a script puts into an outgoing message (`content:`, even
  empty, `set content`, `make new paragraph`, scripted attachments) as
  shared content: an `Apple-Mail-URLShareWrapperClass` blockquote in the
  HTML, and quoted plain text on send. `manage_drafts` `create`,
  `compose_email` `mode="draft"` and `create_rich_email_draft`
  `save_as_draft=True` now build the whole message (text, HTML, signature,
  attachments) as an `.eml`, open it in Mail, press Message › Send Again and
  File › Save through Accessibility (each press only after checking the item
  and the focused window; no keystrokes), close both windows, restore the
  previous front app, and verify the stored draft (no wrapper, body, sender,
  attachment SHA-256, nothing new in Sent/Outbox) before reporting success.
  Mail comes to the front for a few seconds. The old `visible:false` path is
  gone: those compose windows could never be closed by script and re-saved
  deleted drafts until Mail quit (the "autosaved copy" that brought back a
  draft deleted within 20 seconds, noted under Changed, came from them).
- **`create_rich_email_draft` wrote anywhere `output_path` pointed, and
  reused one file per subject.** `output_path` must now be an `.eml` under the
  home or a temp folder, outside protected folders; the default is a new
  private file each time. `save_as_draft=True` now makes a real draft
  instead of reporting that it could not.
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

### Added (upstream)
- `reply_to_email` takes an optional `mailbox` (default `"INBOX"`), so you can reply to a message that a filter delivered to, or that was moved into, another folder. Nested paths use `/`, e.g. `"ASF/Projects/Magpie/Private"`. If nothing matches, the error names the mailbox that was searched (#89, reported by @rbowen).

### Changed (upstream)
- Docs: the email-management skill and README now say `create_rich_email_draft` exports an unsent `.eml` preview that Mail opens in a read-only viewer, and point to `compose_email(body_html=..., mode="draft")` for a sendable HTML draft. The draft workflow reads the saved draft back before sending, sends only when the user authorizes it, and treats a leftover draft as no proof of a failed send (#144 by @YassineElbouchaibi). In this fork `create_rich_email_draft(save_as_draft=True)` also makes a real, sendable draft (see Fixed above).

### Fixed (upstream)
- `search_emails(mailbox="All")` now skips the system folders (Trash, Junk, Sent, Drafts, ...) correctly: the exclusion list compared a repeat reference instead of its value, so nothing was ever skipped (#141 by @YassineElbouchaibi). This fork had the same fix (`is in`, under Changed above).
- `search_emails` and `get_email_thread` resolve a specific mailbox through the shared helper, so localized inbox names (e.g. "Boîte de réception", "Posteingang") and nested paths like `Archives/2026` work (#140 by @YassineElbouchaibi).


### Merged from upstream (patrickfreyer/apple-mail-mcp, 3.2.1 → 3.3.0 and Unreleased)
- Starts on `mcp` 2.x: `from fastmcp import FastMCP` (#97). The
  `--with 'mcp<2'` workaround is no longer needed.
- Path guard (`paths.py`, `--write-dir`, `--attach-dir`) now also covers this
  fork's attachment saving, rich-draft output and `.eml` attachments.
  Upstream's looser attachment rule (any readable file except named secrets)
  is not taken: `compose_email` and `reply_to_email` keep this fork's rule
  (home or temp folders, no `~/.config` and other protected folders), so
  credential files such as `~/.config/rclone/rclone.conf` stay unattachable.
- `reply_to_email(mailbox=...)`, the clipboard restore fix, the reply
  verification, `move_email`/`manage_trash` argument fixes and the localized
  and nested mailbox lookup (#140) are taken as they are.
- `get_email_thread` links messages by Message-ID / In-Reply-To /
  References (upstream's thread split), reading the headers from Mail's
  files; same-subject messages from another conversation are listed apart.
- Not taken: upstream's `read_email(account, message_id, mailbox)` (it asks
  Mail for `content of`, which this fork never does; this fork's
  `read_email` takes the same arguments), the AppleScript by-ID attachment
  lookups (this fork reads them from disk in any mailbox), and the bounded
  `whose` scan for very large mailboxes (this fork reads Mail's database
  instead). `--read-only` stays this fork's strict allowlist.

## [3.3.0] - 2026-10-28

### Added
- `list_email_attachments` and `save_email_attachment` accept `message_id` (the id `search_emails` returns) and `mailbox`, looking the message up directly instead of by subject, so recurring subjects can't pick the wrong month (#128 by @proscar87).
- `read_email(account, message_id, mailbox)` returns one message as headers, plain-text body and attachment list, looked up directly by the id `search_emails` returns, with the body capped at `max_chars` and the cut stated (#128 by @proscar87).

### Security
- The write-path denylist now also checks the path as requested, not only its
  symlink target. A symlink inside a protected folder (e.g.
  `/opt/homebrew/bin/python3`) resolves outside it, so writing through it was
  allowed (#125 by @proscar87, follow-up to #103).
- Hardened the file-path guard for file-writing tools (#103, follow-up to #19).
  The old denylist missed shell startup files, so a prompt-injected
  `save_email_attachment(save_path="~/.zshrc")` could plant an attacker-supplied
  attachment that ran on the next shell. `save_email_attachment`,
  `export_emails` and `create_rich_email_draft`'s `output_path` (previously
  unchecked) now refuse every dotfile and dot-folder directly in `$HOME`
  (`~/.zshrc`, `~/.ssh`, `~/.gitconfig`, `~/.config`, `~/.netrc`, ...),
  `.git/hooks`, `.git/config` and `.envrc` anywhere, autostart and persistence
  locations (`~/Library/LaunchAgents`, `/Library/LaunchDaemons`,
  `~/Library/Application Support`, `~/Library/Preferences`, ...) and `PATH`
  folders (`~/bin`, `/usr/local/bin`, `/opt/homebrew/bin`). Everything else
  stays writable, including paths outside `$HOME` such as the temp dir.
- Attachment paths for `compose_email` and `reply_to_email` may now be any
  readable file (previously `$HOME` only) except secrets: `~/.ssh`, `~/.gnupg`,
  `~/.aws`, `~/.netrc`, git/cloud credentials, `.env` files,
  `~/Library/Keychains` and browser profiles and cookie stores.
- New opt-in restriction: `APPLE_MAIL_MCP_WRITE_DIRS` and
  `APPLE_MAIL_MCP_ATTACH_DIRS` (colon-separated), or the repeatable
  `--write-dir` / `--attach-dir` flags, confine writes or attachments to the
  listed folders; the denylist still applies inside them.
- All checks live in one shared module (`apple_mail_mcp/paths.py`) instead of
  three copies, resolve `..` and symlinks before checking, and compare
  case-insensitively to match APFS (`~/.ZSHRC` is `~/.zshrc`).

### Fixed
- `list_inbox_emails` in text output and `get_mailbox_unread_counts(summary_only=True)`
  ignored `account` and returned every account; they now filter by it
  (#135 by @proscar87).
- `search_emails` on a mailbox over the whose-clause limit now stops its
  bounded index scan at the first message outside a `date_from` window
  (newest-first) or `date_to` window (oldest-first), instead of reading the
  full 4000-message cap. A one-week search of a 6,000-message mailbox no
  longer holds the automation lock for minutes (#133 by
  @joel-alturaeducation, related to #87 and #132).
- The Claude Code plugin manifests (`plugin/.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`) were stuck at 3.1.2, so `claude plugin
  update` reported installs as current. They are now at 3.2.1 and
  `scripts/check_versions.py` and `RELEASING.md` cover them (#127 by
  @proscar87).
- `move_email` no longer treats a negative `older_than_days` as a filter (it
  added no date condition, so the call moved everything), and `manage_trash`
  rejects an unrecognised `action` instead of falling through to
  `move_to_trash` (#124 by @proscar87).
- `reply_to_email` now restores the user's clipboard. The restore step sat
  after the script's `return`, so it never ran and every reply (send, draft
  or open) left the reply body on the clipboard. The error path now restores
  it too, as does `forward_email`'s error path when a `message` was pasted
  (#106).

## [3.2.1] - 2026-10-15

### Fixed
- The server failed to start on any fresh install since the `mcp` SDK 2.0
  release (`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`, #97).
  `server.py` imported `FastMCP` from the SDK's bundled v1 module instead of
  from the declared `fastmcp` dependency, so once `fastmcp` 4.x pulled in
  `mcp` 2.x the import path vanished. Now imports `from fastmcp import
  FastMCP`. `--read-only` also called `FastMCP.remove_tool()`, which is
  deprecated in fastmcp 3.x and removed in 4.x; it now goes through
  `local_provider.remove_tool()`, which both support.
- `search_emails` no longer hangs Mail or returns nothing on very large
  mailboxes such as a Gmail INBOX (#41, #87). Above 5,000 messages the full
  `whose` scan is skipped in favour of a bounded index scan (newest first for
  `date_desc`), capped at 4,000 messages per call; when the cap is reached
  the response says so (`scan_limited` in JSON, a NOTE line in text) instead
  of silently returning less. Body-text search on large mailboxes is bounded
  too.
- Reply and forward drafts render in the intended sans-serif font instead of
  Mail's small serif default (#100, #101). The reply/forward HTML now carries
  an explicit font style rather than `font: inherit`.
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
