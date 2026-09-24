"""Shared constants for Apple Mail MCP tools."""

# Newsletter detection patterns (sender-based)
NEWSLETTER_PLATFORM_PATTERNS = [
    "substack.com", "beehiiv.com", "mailchimp", "sendgrid",
    "convertkit", "buttondown", "ghost.io", "revue.co", "mailgun",
]

NEWSLETTER_KEYWORD_PATTERNS = [
    "newsletter", "digest", "weekly", "daily",
    "bulletin", "briefing", "news@", "updates@",
]

# Folders to skip during broad searches.
# Includes localized variants so non-English Mail.app accounts (Exchange,
# Outlook, Gmail in non-English locales) skip system folders correctly.
SKIP_FOLDERS = [
    # English / IMAP standards
    "Trash", "Junk", "Junk Email", "Deleted Items",
    "Sent", "Sent Items", "Sent Messages", "Drafts",
    "Spam", "Deleted Messages",
    # French (Exchange/Outlook + Gmail FR)
    "Corbeille", "Courrier indésirable", "Indésirables",
    "Éléments supprimés", "Éléments envoyés", "Messages envoyés",
    "Brouillons", "Boîte d'envoi",
    # German
    "Papierkorb", "Gesendet", "Entwürfe", "Werbung",
    # Spanish
    "Papelera", "Enviados", "Borradores", "Correo no deseado",
    # Swedish (Exchange/Outlook + Gmail SV)
    "Skickat", "Skickade meddelanden", "Papperskorgen", "Borttagna objekt",
    "Skräppost", "Utkast",
    # Gmail's English sent folder
    "Sent Mail",
]

# Names of an account's Sent mailbox, tried in this order. The first three
# are the ones the tools always tried; Gmail and localised accounts use the
# others ("[Gmail]/Skickat" is shown by Mail as a top-level "Skickat").
SENT_MAILBOX_NAMES = [
    "Sent Messages", "Sent", "Sent Items", "Sent Mail",
    "Skickat", "Skickade meddelanden",
    "Éléments envoyés", "Messages envoyés", "Gesendet", "Enviados",
]

# Names of Gmail's "All Mail", which holds every message that also appears in
# INBOX and other label mailboxes. A search over all mailboxes lists such a
# message once, under its label mailbox rather than here.
ALL_MAIL_NAMES = [
    "All Mail", "All e-post", "Alle Nachrichten", "Tous les messages", "Todos",
]

# Apple Mail flag colors -> AppleScript `flag index` values.
# Mail only scripts the seven indexed colors; custom flag names assigned in
# the UI are not accessible via AppleScript. An index of -1 means unflagged.
FLAG_COLORS = {
    "red": 0,
    "orange": 1,
    "yellow": 2,
    "green": 3,
    "blue": 4,
    "purple": 5,
    "gray": 6,
    "grey": 6,
}

# Reverse mapping: flag index -> canonical color name (the "grey" alias is
# accepted on input only; index 6 always reports as "gray").
FLAG_COLOR_NAMES = {
    index: name for name, index in FLAG_COLORS.items() if name != "grey"
}

# Thread subject prefixes to strip when matching threads
THREAD_PREFIXES = ["Re:", "Fwd:", "FW:", "RE:", "Fw:"]

# Human-friendly time range mappings (name -> days)
TIME_RANGES = {
    "today": 1,
    "yesterday": 2,
    "week": 7,
    "month": 30,
    "all": 0,
}
