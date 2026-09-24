"""Take secret values out of text, and leave everything else exactly as it was.

This one scrubber serves two doorways. Output from the host passes through it before
anything stores it, shows it to the model, or sends it to Telegram. The agent's replies
pass through it before they reach the group.

It redacts values, not lines and not paths. The redactor it replaces dropped any line
that mentioned a credential. It also dropped every absolute path outside /dev, /proc and
/sys, and every token of 32 characters or more. So the agent's warning that a key had
leaked was blanked, a plan editing `/home/vast/docker-compose.yml` would have reached
the approval button corrupted, and an incident key (64 hex characters) could not be
named in a steer. Paths and identifiers are the agent's working vocabulary. What must
never travel is the secret itself.

On 2026-09-24 a model-authored `ps ... args` read captured the Vast API key from the
exporter's command line. The observe profile walls off tenant data; it does not know
which of our own strings are secrets. This is that knowledge.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# The value of an assignment, a JSON field or an argument runs to the next space,
# quote or separator. Take all of it: taking a well-formed prefix left the rest behind.
_VALUE = r"""[^\s"'`,;}\]]+"""
# A name that says outright that its value is a secret. Assigned (`=`, `:`) or as a
# command-line flag's argument, its value is redacted whole, digits or not --
# `password=correcthorsebatterystaple` is still a password. Followed only by a space in
# prose, it takes a value only if that value has a digit in it, so "password
# authentication failed" survives while "password hunter2secret" does not.
_STRONG_NAME = (
    r"[A-Za-z0-9_]*?(?:api[-_]?key|apikey|access[-_]?key|secret[-_]?key|private[-_]?key|"
    r"auth[-_]?token|access[-_]?token|refresh[-_]?token|client[-_]?secret|password|passwd|"
    r"passphrase|credentials?|authorization)[A-Za-z0-9_]*"
)
# Not a value to redact: one already redacted, the scheme word before a token, or a
# variable reference -- `--api-key=$VAST_API_KEY` is how a plan SHOULD pass a secret,
# and redacting it would corrupt the plan while protecting nothing.
_NOT_DONE = r"(?!\[REDACTED|(?:Bearer|Basic|token)\s|\$)"
_STRONG = re.compile(
    r"(?i)((?:--?|\b)" + _STRONG_NAME + r"[\"']?\s*[:=]\s*[\"']?(?:Bearer\s+|Basic\s+|token\s+)?)"
    + _NOT_DONE + r"(" + _VALUE + r")"
)
_STRONG_FLAG = re.compile(
    r"(?i)((?:^|\s)--?" + _STRONG_NAME + r"\s+)" + _NOT_DONE + r"(?!-)(" + _VALUE + r")"
)
_STRONG_PROSE = re.compile(
    r"(?i)(\b" + _STRONG_NAME + r"\s+)" + _NOT_DONE
    + r"(?=[^\s\"'`,;}\]]*[0-9])(" + _VALUE + r")"
)
# A name that only might mean a secret: `key`, `token`, `secret`. Beside these the value
# must be assigned (`=` or `:`), at least 12 characters long and contain a digit.
# "incident key 40ed8f...", "the token budget" and "key /opt/releases/..." pass.
_WEAK = re.compile(
    r"(?i)(\b[A-Za-z0-9_]*?(?:secret|token|key)[A-Za-z0-9_]*[\"']?\s*[:=]\s*[\"']?)"
    r"(?!\[REDACTED|\$)(?=[^\s\"'`,;}\]]*[0-9])([^\s\"'`,;}\]/][^\s\"'`,;}\]]{11,})"
)
# The password in a URL: scheme://user:password@host.
_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s/@]+(@)")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}")
# Token formats that are secrets wherever they appear, named or not.
_SHAPED = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
    r"sk-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)


def scrub(text: str) -> str:
    """The same text with every recognisable secret value replaced."""
    if not text:
        return text
    text = _PRIVATE_KEY.sub(f"-----{REDACTED} PRIVATE KEY-----", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _USERINFO.sub(lambda m: m.group(1) + REDACTED + m.group(2), text)
    for pattern in (_STRONG, _STRONG_FLAG, _STRONG_PROSE):
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    text = _WEAK.sub(lambda m: m.group(1) + REDACTED, text)
    return _SHAPED.sub(REDACTED, text)
