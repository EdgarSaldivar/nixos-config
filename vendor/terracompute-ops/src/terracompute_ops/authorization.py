"""What a proposed command costs if it is wrong, and who therefore decides it.

This replaces the action catalogue's enumeration. The catalogue answered three
questions at once -- what may be said, what parameters are valid, and who approves --
and the first of those was a list of five. The agent could name five things and
perform one; the other four failed at execution with "action class has no configured
fixed adapter". ``docs/AUTONOMOUS-OPERATION.md`` names the cost of that shape: the
agent's capability equals the number of executors somebody has written, so autonomy is
bounded by the author's throughput, which is the thing autonomy was supposed to
relieve.

The argument that settles it is one this system already accepted. The read loop lets
the model write arbitrary shell commands, because a kernel-enforced read-only profile
makes the blast radius nil. Model-composed text already reaches a root shell by
design. Enumerating five actions for writes, while reads are open, is not a boundary --
it is a vocabulary. Swap the protection rather than the freedom: read-only mount for
reads, a person reading the literal command for writes.

So expression is open. Anything can be proposed, and what varies is who decides:

- :attr:`Risk.REFUSED` -- nobody here. One rule, the same one the docker proxy
  enforces: it names somebody else's rental.
- :attr:`Risk.SELF` -- the agent may do it alone. Deliberately narrow and deliberately
  a positive list, because this is the only path with no human in it.
- :attr:`Risk.APPROVAL` -- everything else, which is the DEFAULT. A person is shown the
  exact command and decides.

The difference from the catalogue is the default. An unrecognised action used to be
impossible; now it is merely something a person is asked about. That is a deny posture
on autonomy, not on capability, and it is the distinction the catalogue lost.
"""

from __future__ import annotations

import re
from enum import Enum

MAX_COMMAND_CHARS = 512

# Vast names every rental this way, and a tenant cannot choose the name. Same rule and
# same reasoning as the docker proxy; stated here too because this is a different
# doorway to the same machine and a boundary worth having is worth stating twice.
TENANT = re.compile(r"(?:^|[^A-Za-z0-9._-])C\.[0-9]{1,20}(?:$|[^A-Za-z0-9._-])")

# Anything that lets one command become several. A self-service classification says
# "this exact thing is safe to do unattended", which cannot be true of a string that
# carries a second command after it.
COMPOUND = re.compile(r"[;&|`$(){}<>\n\r\\!*?\[\]]")

# What the agent may do without asking. A positive list on purpose: this is the only
# path with no person in it, so it should be short, boring, and reversible within
# seconds. Everything absent from it is not forbidden -- it is asked about.
#
# `docker restart <name>` and `docker start <name>` on a container of ours: the
# charter's line is that managing the monitoring we installed is the agent's own work,
# because it is reversible and nobody paying us feels it.
SELF_SERVICE = (
    re.compile(r"^docker (?:restart|start|stop) [A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"),
    re.compile(r"^systemctl (?:restart|start) [A-Za-z0-9][A-Za-z0-9_.@-]{0,63}(?:\.service)?$"),
)


# The same set in words, for the model's contract. A regex is a precise thing to
# enforce and a poor thing to read, and what goes in a prompt has to be read.
SELF_SERVICE_SUMMARY = (
    "docker restart|start|stop <container>, on one container of ours",
    "systemctl restart|start <unit>",
)


class Risk(str, Enum):
    REFUSED = "refused"
    APPROVAL = "approval"
    SELF = "self"


def classify(command: object) -> tuple[Risk, str]:
    """Who decides this command, and the reason to tell a person.

    The reason is written to be read, not parsed: it appears in the group chat beside
    the command itself, so somebody deciding at speed can see why they were asked.
    """
    if not isinstance(command, str):
        return Risk.REFUSED, "that is not a command"
    command = command.strip()
    if not command:
        return Risk.REFUSED, "that is an empty command"
    if len(command) > MAX_COMMAND_CHARS:
        return Risk.REFUSED, "that command is too long to review"
    if "\x00" in command or any(ord(c) < 0x20 for c in command):
        return Risk.REFUSED, "that command contains control characters"
    if TENANT.search(command):
        return Risk.REFUSED, (
            "that names a customer's rental. This agent does not touch tenant "
            "containers; if the evidence truly requires it, say so and ask a person."
        )
    if COMPOUND.search(command):
        # Not refused -- a person may well want a compound command run. It simply
        # cannot be one of the few shapes that go without asking, because what runs
        # after the separator is not what was matched.
        return Risk.APPROVAL, "it runs more than one thing"
    for shape in SELF_SERVICE:
        if shape.fullmatch(command):
            return Risk.SELF, "reversible work on what we installed"
    return Risk.APPROVAL, "it is not one of the few things I do unattended"


def needs_a_person(command: object) -> bool:
    """True when a human must see this exact command before it runs."""
    return classify(command)[0] is Risk.APPROVAL
