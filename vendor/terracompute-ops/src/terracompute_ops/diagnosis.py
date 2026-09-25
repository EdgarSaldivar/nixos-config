"""What a diagnosis may say, and what it is allowed to ask for.

The investigator reads evidence and answers with a finding: what it thinks is wrong and,
optionally, one action from the catalogue below. Model text is data, never a command, so
this module parses that answer strictly and refuses anything it does not recognise.

There is no catalogue of actions any more. A finding names the COMMAND it wants run,
and :mod:`terracompute_ops.authorization` decides who may say yes to it -- nobody, this
service alone, or a person shown the exact command. The docstring there explains why an
enumeration was the wrong shape; the short version is that the read loop already lets
this model write arbitrary shell commands under a read-only mount, so listing five for
writes was a vocabulary rather than a boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .authorization import MAX_COMMAND_CHARS, SELF_SERVICE_SUMMARY, Risk, classify

MAX_FINDING_BYTES = 16 * 1024
MAX_TEXT_CHARS = 1200
MAX_LIST_ITEMS = 8
MAX_EVIDENCE_REFS = 16
# One round of the read loop may ask for this many commands; a genuinely broad question
# is several narrow reads, not one unbounded scan, and the round can always ask again.
MAX_READS_PER_ROUND = 8
MAX_READ_COMMAND_CHARS = 512
# One read may be a short script -- a loop over containers, a pipeline across lines --
# because that is how the agent writes them, and splitting a loop into lines breaks it.
MAX_READ_SCRIPT_CHARS = 8000
MAX_VERIFY_COMMANDS = 4
CONFIDENCE = ("low", "medium", "high")
# Anything but control characters, which are the part that can do harm: an escape
# sequence reaches a terminal, an em dash does not. Restricting this to ASCII threw
# away whole diagnoses over the punctuation a model naturally writes.
_TEXT = re.compile(r"^(?:[^\x00-\x1f\x7f-\x9f]|\n){1,%d}$" % MAX_TEXT_CHARS)
_BDF = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RENTAL = re.compile(r"^C\.[0-9]{1,20}$")
_EVIDENCE_REF = re.compile(r"^[A-Za-z0-9:._@-]{1,128}$")


def ours(value: object) -> bool:
    """A container we may act on: any well-formed name that is not a rental.

    This used to be a list of the seven monitoring containers on the machine. A list
    is wrong here for the same reason it is wrong in the proxy: it has to be written
    before we know what is on the machine. Its first version named two containers that
    did not exist -- `gddr6-exporter` for `vast-gddr6-metrics-exporter-1`, and
    `vast-node-exporter` for `node-exporter` -- so two thirds of the only action this
    service can take passed validation here and failed at execution with "No such
    object". And the day the monitoring stack is replaced, the list is stale again and
    the agent is locked out of the thing it was installed to look after.

    So the rule is the boundary itself, stated once: Vast names every rental
    ``C.<digits>`` and a tenant cannot choose that name, so everything else on this
    machine is ours. A name docker does not know fails at execution, which is where
    that belongs -- docker knows what exists and we do not.
    """
    if not isinstance(value, str):
        return False
    return bool(_CONTAINER.fullmatch(value)) and not _RENTAL.fullmatch(value)


class Tier(str, Enum):
    LOOK = "look"
    REPAIR = "repair"
    CHANGE = "change"


_OBSERVE_CONTRACT = (
    "If you need to look at the host before you can answer, do not guess. Reply "
    "instead with only:\n"
    '{"reads_requested": ["a read-only shell command, or a short multi-line script", ...], '
    '"note": "why"}\n'
    f"and you will be given each command's output to continue. Ask for up to "
    f"{MAX_READS_PER_ROUND} commands at a time; you can ask again after seeing the "
    "results, and a read that has not finished in a minute is cut off, so prefer "
    "specific reads over broad scans.\n\n"
    "Every filesystem is mounted read-only for these commands and other tenants' data "
    "is walled off, so nothing you run can alter what is on this machine or read "
    "another tenant's files: look freely at processes, /proc, /sys, devices, drivers, "
    "logs and our own containers. You are still root, and read-only mounts do not stop "
    "a command that signals a process, resets a device or reaches the network -- so do "
    "not run one. If what you need requires changing something, that is an action: say "
    "so in your finding and let a person decide.\n\n"
    "Docker is available to you for READING: inspect, logs, ps, stats, events, any "
    "field, on any container that is ours. Anything that would change a container -- "
    "restart, stop, kill, exec, rm -- is refused here exactly as a write to the "
    "filesystem is; ask for it in your finding instead. Requests naming a container Vast has "
    "rented out -- they are named C.<digits> -- are refused, because that is somebody "
    "else's machine. If the evidence genuinely points INTO a tenant's container and "
    "nothing outside it will settle the question, do not work around it and do not "
    "guess: say plainly in your finding that this is what you need and why, and let a "
    "person decide.\n\n"
    "When you have enough to conclude, answer with the finding object above instead.\n\n"
)


class FindingRejected(ValueError):
    """The answer did not meet the contract; nothing in it is used."""


@dataclass(frozen=True)
class ProposedAction:
    """One thing the investigator wants done, as the command that would do it.

    It used to be a name from a catalogue of five plus validated parameters. The
    catalogue could only say things somebody had already thought of, and only one of
    the five could actually be carried out; see :mod:`terracompute_ops.authorization`
    for why that shape had to go.

    The command is the thing itself, and it is also what a person is shown before they
    approve. ``docker restart dcgm-exporter`` is more reviewable than
    ``restart-monitoring-container(container=dcgm-exporter)``, not less -- it says
    exactly what will run, with nothing between the sentence and the machine.
    """

    command: str
    intent: str = ""
    # How to undo it, and read-only commands that show whether it worked. A plan with
    # neither is a plan a person has to take on trust; with them, the approver can see
    # the way back, and the service runs the checks afterwards and shows what they say.
    rollback: str = ""
    verify: tuple[str, ...] = ()

    @property
    def risk(self) -> Risk:
        return classify(self.command)[0]

    @property
    def why(self) -> str:
        """The reason for that risk, written to be read beside the command."""
        return classify(self.command)[1]

    @property
    def tier(self) -> Tier:
        """The old vocabulary, for the parts that still speak it."""
        return {
            Risk.SELF: Tier.REPAIR,
            Risk.APPROVAL: Tier.CHANGE,
            Risk.REFUSED: Tier.CHANGE,
        }[self.risk]

    def describe(self) -> str:
        return f"{self.command}  ({self.intent})" if self.intent else self.command

    def details(self) -> list[str]:
        """The way back and the checks, for the person deciding."""
        lines = []
        if self.rollback:
            lines.append(f"To undo: {self.rollback}")
        if self.verify:
            lines.append("I will check afterwards with:\n" + "\n".join(
                f"  {command}" for command in self.verify
            ))
        return lines


@dataclass(frozen=True)
class Recurrence:
    """Whether this comes back, by what mechanism, and what would end it."""

    expected: bool
    mechanism: str = ""
    ends_when: str = ""


@dataclass(frozen=True)
class Finding:
    summary: str
    mechanism: str
    evidence: tuple[str, ...]
    action: ProposedAction | None
    expected_effect: str
    alternatives: tuple[str, ...]
    prevention: str
    confidence: str
    unsupported_request: str | None = field(default=None)
    # The second horizon: the smallest change that removes the mechanism, rather than
    # the safest thing that restores service now. It may be a catalogued action, or
    # prose where nothing in the catalogue expresses it, or nothing at all. Without
    # somewhere to say this, a finding has to choose between stopping the bleeding and
    # naming the cure, and it will reasonably choose the former every time.
    durable_action: ProposedAction | None = field(default=None)
    durable_recommendation: str = field(default="")
    durable_unsupported: str | None = field(default=None)
    recurrence: Recurrence | None = field(default=None)

    @property
    def tier(self) -> Tier | None:
        return None if self.action is None else self.action.tier

    @property
    def palliative(self) -> bool:
        """True when this fixes today and says so about tomorrow."""
        return bool(self.recurrence and self.recurrence.expected)


def _text(document: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = document.get(key, "")
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str) or not _TEXT.fullmatch(value):
        raise FindingRejected(f"{key} must be printable text of at most {MAX_TEXT_CHARS} characters")
    return value


def _string_list(
    document: Mapping[str, Any],
    key: str,
    pattern: re.Pattern[str] | None,
    limit: int | None = None,
) -> tuple[str, ...]:
    values = document.get(key, [])
    if values is None:
        return ()
    if limit is None:
        limit = MAX_EVIDENCE_REFS if pattern is not None else MAX_LIST_ITEMS
    if not isinstance(values, list) or len(values) > limit:
        raise FindingRejected(f"{key} must be a list of at most {limit} entries")
    items = []
    for value in values:
        if pattern is not None:
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise FindingRejected(f"{key} entries must be evidence references")
        elif not isinstance(value, str) or not _TEXT.fullmatch(value):
            raise FindingRejected(f"{key} entries must be printable text")
        items.append(value)
    return tuple(items)


def _action(value: object) -> tuple[ProposedAction | None, str | None]:
    """Parse the proposed command, or report what was asked for and refused.

    Anything may be proposed. What varies is who decides it, which is
    :func:`~terracompute_ops.authorization.classify`'s job, not this one's. The only
    thing rejected here is a command nobody may run -- naming somebody else's rental --
    and that comes back as "it asked for this and was refused", so a person still reads
    it. A finding is never discarded over its action.
    """
    if value in (None, "", "none"):
        return None, None
    if not isinstance(value, dict):
        raise FindingRejected("action must be an object or null")
    command = value.get("command")
    if not isinstance(command, str) or not command.strip():
        raise FindingRejected("action.command must be a command to run")
    command = command.strip()
    intent = value.get("intent", "")
    if not isinstance(intent, str) or (intent and not _TEXT.fullmatch(intent)):
        raise FindingRejected("action.intent must be text")
    risk, reason = classify(command)
    if risk is Risk.REFUSED:
        # Not a rejection of the finding. The model may genuinely need something we
        # will not do, and a person should read that rather than have it discarded.
        return None, f"{command[:160]} ({reason})"
    # Loose on purpose, like the rest of the prose: a malformed way back or check is
    # dropped, and the action it came with still stands.
    rollback = value.get("rollback", "")
    if not isinstance(rollback, str) or not _TEXT.fullmatch(rollback or "x"):
        rollback = ""
    verify = value.get("verify", [])
    if isinstance(verify, str):
        verify = [verify]
    checks = tuple(
        entry.strip() for entry in (verify if isinstance(verify, list) else [])
        if isinstance(entry, str) and entry.strip()
        and len(entry) <= MAX_READ_COMMAND_CHARS and "\x00" not in entry
    )[:MAX_VERIFY_COMMANDS]
    return ProposedAction(command, intent[:MAX_TEXT_CHARS], rollback, checks), None


def _recurrence(value: object) -> Recurrence | None:
    if value in (None, "", "none"):
        return None
    if not isinstance(value, dict):
        raise FindingRejected("recurrence must be an object or null")
    expected = value.get("expected")
    if not isinstance(expected, bool):
        raise FindingRejected("recurrence.expected must be true or false")
    return Recurrence(
        expected=expected,
        mechanism=_text(value, "mechanism", required=False),
        ends_when=_text(value, "ends_when", required=False),
    )


@dataclass(frozen=True)
class ReadRequest:
    """The model wants to look before it concludes: read commands to run and re-ask with.

    This is not a finding and carries no action. Each command runs on the target under
    the read-only observe profile, so the model may ask for anything without being able
    to change the machine. `note` is the model's own reason, kept for the record.
    """

    commands: tuple[str, ...]
    note: str = ""


def _extract_document(text: str) -> dict[str, Any]:
    """The JSON object in a model answer, however it wrapped it. Shared by both paths."""
    if not isinstance(text, str) or not text.strip():
        raise FindingRejected("empty answer")
    if len(text.encode("utf-8")) > MAX_FINDING_BYTES:
        raise FindingRejected("answer is too large")
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise FindingRejected("answer is not a JSON object")
    try:
        document = json.loads(body[start : end + 1])
    except ValueError as error:
        raise FindingRejected("answer is not valid JSON") from error
    if not isinstance(document, dict):
        raise FindingRejected("answer is not a JSON object")
    return document


def _read_commands(value: object) -> tuple[str, ...]:
    """Validate the commands a read request asks for; nothing about them reaches a shell here."""
    if not isinstance(value, list) or not value or len(value) > MAX_READS_PER_ROUND:
        raise FindingRejected(f"reads_requested must be 1..{MAX_READS_PER_ROUND} commands")
    commands = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise FindingRejected("each requested read must be a non-empty command")
        if len(entry) > MAX_READ_SCRIPT_CHARS or "\x00" in entry:
            raise FindingRejected("a requested read is too long or not text")
        commands.append(entry)
    return tuple(commands)


def parse_response(text: str) -> "ReadRequest | Finding":
    """One model turn: either a request to look further, or a finding.

    A non-empty ``reads_requested`` makes it a request; the loop runs those reads and
    asks again. Anything else is parsed as a final finding, exactly as before.
    """
    document = _extract_document(text)
    requested = document.get("reads_requested")
    if requested not in (None, "", [], "none"):
        return ReadRequest(
            commands=_read_commands(requested),
            note=_text(document, "note", required=False),
        )
    return _finding_from_document(document)


def parse_finding(text: str) -> Finding:
    """Parse one model answer as a finding. Anything unexpected raises; nothing is guessed."""
    return _finding_from_document(_extract_document(text))


def _finding_from_document(document: dict[str, Any]) -> Finding:
    confidence = document.get("confidence")
    if confidence not in CONFIDENCE:
        raise FindingRejected(f"confidence must be one of {CONFIDENCE}")
    action, unsupported = _action(document.get("action"))
    durable = document.get("durable")
    if durable in (None, "", "none"):
        durable = {}
    if not isinstance(durable, dict):
        raise FindingRejected("durable must be an object or null")
    durable_action, durable_unsupported = _action(durable.get("action"))
    # `prevention` was the contract's older name for the same question, and a model
    # that answers it instead has still answered. Dropping it silently is how the one
    # sentence naming the real fix ended up in a field nothing displays.
    prevention = _text(document, "prevention", required=False)
    durable_recommendation = _text(durable, "recommendation", required=False) or prevention
    return Finding(
        summary=_text(document, "summary"),
        mechanism=_text(document, "mechanism"),
        # What the finding rests on, in the investigator's own words. This is for a
        # person to check, not for a machine to act on -- the action is what is bound
        # to the catalogue -- so a good diagnosis is never thrown away over the shape
        # or the number of its citations.
        evidence=_string_list(document, "evidence", None, MAX_EVIDENCE_REFS),
        action=action,
        expected_effect=_text(document, "expected_effect", required=action is not None),
        alternatives=_string_list(document, "alternatives", None),
        prevention=prevention,
        confidence=confidence,
        unsupported_request=unsupported,
        durable_action=durable_action,
        durable_recommendation=durable_recommendation,
        durable_unsupported=durable_unsupported,
        recurrence=_recurrence(document.get("recurrence")),
    )


@dataclass(frozen=True)
class ObserveRound:
    """One round of looking: what was asked for, and what the host said back.

    The commands are the model's own words from a previous turn; the outputs are the
    host's. Both are evidence in the next prompt and neither is an instruction.
    """

    note: str
    results: tuple[tuple[str, str], ...]  # (command, output)


def contract_text(can_observe: bool = True) -> str:
    """The answer contract and catalogue, as given to the model.

    ``can_observe`` is false where no read channel is wired: the contract then never
    offers one. Asking a model for something it cannot have produces an answer nobody
    can use, and a request this service would have to refuse.
    """
    unattended = "\n".join(f"  {shape}" for shape in SELF_SERVICE_SUMMARY)
    return (
        "Answer with one JSON object and nothing else:\n"
        '{"summary": "one line, what is wrong",\n'
        ' "mechanism": "how it fails, citing the evidence",\n'
        ' "evidence": ["what you rely on, naming a read such as'
        ' target-read@gpu-handles where you can", ...],\n'
        ' "action": {"command": "the shell command, or a plan, that does it",'
        ' "intent": "what it is for, in one line",'
        ' "rollback": "how to undo it",'
        ' "verify": ["read-only command that shows it worked", ...]} or null,\n'
        ' "durable": {"action": {same shape as action} or null,'
        ' "recommendation": "the smallest change that removes the mechanism, in your'
        ' own words, citing where you checked"} or null,\n'
        ' "recurrence": {"expected": true|false, "mechanism": "why it comes back",'
        ' "ends_when": "what would stop it"} or null,\n'
        ' "expected_effect": "what you expect to observe if the action works",\n'
        ' "alternatives": ["other explanation and the check that separates it", ...],\n'
        ' "confidence": "low" | "medium" | "high"}\n\n'
        + (_OBSERVE_CONTRACT if can_observe else "")
        + "Ask for at most one action, and write it as the command that would do it. "
        "There is no catalogue to choose from: propose whatever the evidence supports, "
        "including something nobody has done here before.\n\n"
        "What happens to it depends on what it does, and it is not up to you:\n"
        "- These few shapes I carry out myself, because they are reversible in seconds "
        "and nobody paying us feels them:\n"
        f"{unattended}\n"
        "- Everything else is shown to a person, exactly as you wrote it, and they "
        "decide. That is the normal case and not a failure -- a reboot, a driver "
        "rebind, a tool nobody has used here yet, all of it is proposable.\n"
        "- A command naming a customer's rental (C.<digits>) is refused outright, and "
        "what you asked for is shown to a person instead.\n\n"
        "A change that takes several steps is a plan: a POSIX sh script, one step per "
        f"line, at most {MAX_COMMAND_CHARS} characters. Start it with `set -eu` so it "
        "stops at the first step that fails, and back up any file before you change it. "
        "A person approves the whole plan with one tap -- do not split it across "
        "investigations. Give `rollback` and up to four read-only `verify` commands; I "
        "run the checks after it and show what they say. Say what you mean plainly in "
        "`intent` -- it is read beside the command by the person deciding. For a host reboot, use `shutdown -r +1 'terracompute: approved "
        "host reboot'`, not an immediate `systemctl reboot`: scheduling one minute "
        "ahead lets the audited management session report acceptance before the host "
        "disconnects.\n\n"
        "`action` answers one question and `durable` answers another. `action` is the "
        "safest thing that restores service now. `durable` is the smallest change, "
        "supported by the evidence, that stops this recurring -- which is often a "
        "different and larger thing. When you can write the durable fix as a plan, put "
        "it in `durable.action`: it is put to a person with an Approve button, exactly "
        "like `action`. When the durable fix makes a stopgap pointless, leave `action` "
        "null. Either may be null. If the immediate action only buys time, say so "
        "in `recurrence` rather than leaving it to be inferred: a fix that has to be "
        "repeated is more disruptive over its life than one change made once. Where the "
        "durable answer is to replace or remove a component, name it concretely and say "
        "what you relied on to identify the replacement.\n\n"
        "`durable.recommendation` is prose for a person to read. It authorises nothing, "
        "so the bar for writing it is that the evidence supports it -- not that this "
        "system could carry it out. Withholding the real fix because no single command "
        "expresses it leaves the operator with only the stopgap.\n\n"
        "A component being unmaintained, abandoned or superseded is a durable finding "
        "like any other. You have web search: check the upstream project yourself -- "
        "its repository, README, open issues, whether it is archived and what it points "
        "to instead -- and cite the URL you relied on. Never hand a person a lookup you "
        "could have done."
    )


# -- What a person may ask for, in their own words -----------------------------------
#
# An operator speaks to this service in plain language; nobody should have to remember a
# command to pause a machine. So the model reads what they said and names one thing to
# do from the list below, and this module parses that exactly as strictly as it parses a
# finding: the model's text is data, the catalogue is the boundary, and anything outside
# it is refused rather than guessed at.
#
# Nothing here can act on the machine. The worst a misread costs is a look nobody wanted
# or a pause you undo -- which is why acting still happens the way it always did, as a
# button bound to one proposal. "Look at it again" is safe to say in words because
# looking is read-only; what the looking proposes still comes back for a person to press.


@dataclass(frozen=True)
class SteeringEntry:
    name: str
    takes: str  # "" for nothing, "bdf" for one PCI address
    summary: str


STEERING: dict[str, SteeringEntry] = {
    entry.name: entry
    for entry in (
        SteeringEntry("pause", "", "Stop acting on anything. Keep watching and reporting."),
        SteeringEntry("resume", "", "Act again as usual."),
        SteeringEntry("hold", "bdf", "Leave this one GPU alone until released."),
        SteeringEntry("release", "bdf", "Stop holding this GPU."),
        SteeringEntry(
            "look-again", "bdf",
            "Investigate this again from the beginning, ignoring my own waiting "
            "periods. Name a GPU by its PCI address, or any other fault by the "
            "incident key I gave you. It proposes; it does not act.",
        ),
        SteeringEntry(
            "investigate", "",
            "Look the machine over from the beginning and say what you find, when they "
            "think something is wrong and I have not found it myself. It looks and "
            "reports; it changes nothing.",
        ),
        SteeringEntry(
            "ask-me", "bdf",
            "Put the request in front of me. Name this when they say to go ahead, to "
            "do it, or that they cannot find the request you mentioned. I will send "
            "the one that is waiting, or make one if there is none.",
        ),
        SteeringEntry(
            "withdraw", "bdf",
            "Say they may want the request waiting on this GPU taken back. I will ASK "
            "rather than do it, so name this only if they seem to want it dropped -- "
            "a question about whether the restart is needed is not that.",
        ),
    )
}
# The argument's character class admits the decoration a model reaches for -- brackets,
# backticks, quotes -- so that it is CAPTURED and then stripped, rather than failing the
# whole line. Stripping alone was not enough: the regex rejected the line first, so the
# steer was dropped and the operator was shown the raw `STEER:` text instead.
_STEER_LINE = re.compile(
    r"^STEER:\s*([a-z-]{1,24})(?:\s+([A-Za-z0-9._:\-\[\]`'\"<>]{1,72}))?\s*$"
)
# What a steer may name: a GPU by its PCI address, or a fault by its incident key.
# Only GPUs could be named before, so an incident with no GPU -- a BMC fault, a
# capacity fault, anything not on the PCI bus -- could be looked at exactly once per
# episode and never again, however plainly somebody asked.
_INCIDENT_KEY = re.compile(r"^[0-9a-f]{12,64}$")


def _SUBJECT(value: str) -> bool:
    return bool(_BDF.fullmatch(value) or _INCIDENT_KEY.fullmatch(value))


# Which steers make a waiting request mean something different, and so must take it
# back before it is answered.
#
# `pause` and `hold` say do not act, which a waiting button contradicts. `look-again`
# asks for a fresh opinion, and the request on the table is the old one.
#
# The rest do not: `resume` and `release` ENABLE acting, so cancelling the request that
# was waiting to be enabled is precisely backwards, and `investigate` is about the
# machine at large rather than this fault. Suspending on every steer meant that asking
# for another look destroyed the request you were looking at -- which is the churn it
# was supposed to prevent, wearing the other hat.
SUSPENDS_A_REQUEST = frozenset({"pause", "hold", "look-again"})


@dataclass(frozen=True)
class Steer:
    """One thing the operator asked for, named by the model and checked here."""

    name: str
    argument: str = ""

    @property
    def entry(self) -> SteeringEntry:
        return STEERING[self.name]

    def describe(self) -> str:
        return f"{self.name} {self.argument}".strip()


def parse_reply(text: str) -> tuple[str, Steer | None]:
    """An answer for the operator, and the one thing it asks this service to do.

    The steer is a single final line, so the prose above it is untouched and a reply
    that names nothing is simply a reply. An unparseable or unknown steer is dropped,
    never guessed at: the person still gets their answer, and nothing happens.
    """
    if not isinstance(text, str) or not text.strip():
        return "", None
    lines = text.strip().splitlines()
    last = lines[-1].strip() if lines else ""
    match = _STEER_LINE.fullmatch(last)
    if match is None:
        # A line that was TRYING to be a steer and failed is machinery, not an answer.
        # Leaving it in showed `STEER: look-again [0000:a1:00.0]` to the operator, who
        # then had a malformed instruction quoted at them and nothing done about it.
        if last.upper().startswith("STEER:"):
            return "\n".join(lines[:-1]).strip(), None
        return text.strip(), None
    prose = "\n".join(lines[:-1]).strip()
    entry = STEERING.get(match.group(1))
    argument = (match.group(2) or "").strip().strip("[]`'\"<>")
    # From here the line was a steer attempt, so it is dropped from the prose either
    # way: what a person reads is the answer, never the failed instruction.
    if entry is None:
        return prose, None
    if entry.takes == "bdf":
        if not argument or not _SUBJECT(argument):
            return prose, None
    elif argument:
        return prose, None
    return prose, Steer(entry.name, argument)


def steering_text() -> str:
    """The steering vocabulary, as given to the model."""
    lines = "\n".join(
        f"- {entry.name}"
        f"{' <pci-address-or-incident-key>' if entry.takes == 'bdf' else ''}: {entry.summary}"
        for entry in STEERING.values()
    )
    return (
        "If what they said asks you to change what I am doing, end your reply with one "
        "final line naming it. Two examples of the whole line, exactly as it should "
        "look -- no brackets, no backticks, no punctuation after it:\n"
        "STEER: pause\n"
        "STEER: look-again 0000:a1:00.0\n"
        f"{lines}\n"
        "Use it only when they are asking for that thing -- not when they are "
        "discussing it, asking what it would do, or telling you not to. If you are not "
        "sure they are asking, leave the line off and ask them. Say nothing about this "
        "format in the words above it; write to them as you would anyway, and I will "
        "tell them what I did."
    )


# -- What the operator's conversation may carry besides words ------------------------
#
# The chat used to be told it had no shell and could carry nothing out, so every answer
# ended "I need X" and waited for the operator to fetch X. It can now do the two things
# an agent does between sentences: look (reads, run under the read-only profile and
# handed back in the same thread) and propose (a plan, put to a person with a button).
# Both are fenced blocks so they can sit anywhere in a reply without being mistaken
# for prose, and both are parsed as strictly as a finding: the text is data.

_CHAT_BLOCK = re.compile(r"```(reads|read-script|plan)[ \t]*\r?\n(.*?)```", re.DOTALL)
# A script written for a person to run. On 2026-09-25 the agent put a complete, careful
# plan in a ```sh block: it was shown as text, no button appeared, and nothing could run.
# Reads that are certain to fail on the observe profile, and why.
_DOOMED_READ = re.compile(
    r"(?P<exec>\bdocker\s+(?:container\s+)?exec\b)"
    r"|(?P<tmp>\bmktemp\b|>{1,2}\s*/tmp\b|\btee\s+(?:-a\s+)?/tmp\b)"
    r"|(?P<errexit>(?m:^)\s*set\s+-[A-Za-z]*e)"
)
_DOOMED_WHY = {
    "exec": "`docker exec` into a container is refused; read from the host side",
    "tmp": "every filesystem is read-only, /tmp included; pipe or use a variable instead",
    "errexit": "`set -e` stops the whole read at the first refused command; use `set -u`",
}
_LOOKS_LIKE_A_SCRIPT = re.compile(
    r"(?m)^\s*set\s+-|\\\s*$|^\s*(?:do|done|then|fi|else|esac)\b|^[ \t]+\S"
)
_STRAY_SCRIPT = re.compile(r"```(?:sh|bash|shell|zsh)[ \t]*\r?\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class ChatReply:
    """One conversational answer, split into what is said and what is asked for."""

    text: str
    steer: Steer | None = None
    reads: tuple[str, ...] = ()
    plan: ProposedAction | None = None
    # Why a plan it wrote could not be taken, said to the operator rather than dropped.
    plan_problem: str = ""
    # Reads it asked for that could not be run, and why. Dropping them silently ended
    # a conversation on 2026-09-24: a 3,323-character script against a 2,000 limit
    # vanished, the reply read as finished, and nobody -- model or operator -- knew.
    read_problems: tuple[str, ...] = ()


def parse_chat(text: str) -> ChatReply:
    """Prose, the steer on its last line, and any reads or plan it asked for."""
    if not isinstance(text, str) or not text.strip():
        return ChatReply("")
    reads: list[str] = []
    plan: ProposedAction | None = None
    problem = ""
    refused: list[str] = []

    def take(command: str, limit: int) -> None:
        doomed = _DOOMED_READ.search(command)
        if doomed:
            # Checked here, not left to the prompt: it was told all three and did each
            # again on 2026-09-25, spending host rounds on reads that could only fail.
            refused.append(
                f"this read would fail before it told you anything ({_DOOMED_WHY[doomed.lastgroup]}), "
                f"so it was not run: {command[:80]!r}"
            )
        elif "\x00" in command:
            refused.append(f"a read contained a NUL byte: {command[:60]!r}")
        elif len(command) > limit:
            refused.append(
                f"a read was {len(command)} characters, over the {limit} limit "
                f"(it began {command[:60]!r}); split it"
            )
        elif len(reads) >= MAX_READS_PER_ROUND:
            refused.append(
                f"only {MAX_READS_PER_ROUND} reads run per round; this one was not run: "
                f"{command[:60]!r}"
            )
        else:
            reads.append(command)

    for kind, body in _CHAT_BLOCK.findall(text):
        if kind == "reads" and _LOOKS_LIKE_A_SCRIPT.search(body):
            # A script written in the one-command-per-line block. Split, it ran as
            # fragments on 2026-09-25 and a whole round came back meaningless.
            take(body.strip(), MAX_READ_SCRIPT_CHARS)
        elif kind == "reads":
            for line in body.splitlines():
                command = line.strip()
                if command and not command.startswith("#"):
                    take(command, MAX_READ_COMMAND_CHARS)
        elif kind == "read-script":
            script = body.strip()
            if script:
                take(script, MAX_READ_SCRIPT_CHARS)
        elif plan is None and not problem:
            body = body.strip()
            try:
                if body.startswith("{"):
                    document = json.loads(body)
                    if not isinstance(document, dict):
                        raise FindingRejected("it must be a JSON object")
                else:
                    # A bare script is a plan too: what matters is that a person sees
                    # exactly what will run, and a script is exactly that.
                    document = {"command": body, "intent": ""}
                plan, declined = _action(document)
            except (ValueError, FindingRejected) as error:
                plan, declined = None, f"the plan was not a valid object ({error})"
            if declined:
                problem = declined
    if plan is None and not problem and not reads and _STRAY_SCRIPT.search(text):
        refused.append(
            "you wrote a script in a ```sh block, which is only text: nobody can approve "
            "or run it. If it is the change you want, send it again in a ```plan block"
        )
    prose, steer = parse_reply(_CHAT_BLOCK.sub("", text))
    return ChatReply(prose, steer, tuple(reads), plan, problem, tuple(refused))


def chat_capabilities_text() -> str:
    """What the conversation may do, as given to the model."""
    return (
        "You can look at the machine yourself. To run read-only commands on the host, "
        "put them in a block like this, one command per line, at most "
        f"{MAX_READS_PER_ROUND}:\n"
        "```reads\n"
        "docker ps --format '{{.Names}} {{.Image}} {{.Label \"com.docker.compose.project.working_dir\"}}'\n"
        "journalctl -k -b --no-pager | tail -n 100\n"
        "```\n"
        "For a read that needs several lines -- a loop, a multi-line pipeline -- put it "
        "in its own block, which runs as one script:\n"
        "```read-script\n"
        "for c in dcgm-exporter node-exporter; do\n"
        "  docker inspect \"$c\" --format '{{.Name}} {{json .HostConfig.DeviceRequests}}'\n"
        "done\n"
        "```\n"
        "Every filesystem is read-only for reads, /tmp included: do not create files "
        "(no mktemp, no redirects to disk) -- pipe instead. `docker exec` into a "
        "container is refused; read from the host side. Do not use `set -e` in a read: "
        "one refused command would stop the rest; use `set -u`.\n\n"
        "Each output line longer than 300 characters is cut there by the host, and only "
        "the last 200 lines of a read are kept. So never print JSON on one line: use "
        "`--format` with one field per line, or pipe through `python3 -m json.tool`, "
        "and split a big survey into several reads.\n\n"
        "I run them under a profile that cannot write and give you the output in this "
        "same conversation, and you carry on from there -- as many rounds as you need, "
        "within reason. These blocks are your only way to see the host: you have no "
        "shell of your own, and anything you run any other way is not on this machine. "
        "Never hand the operator commands to run -- put them in a block and I will run "
        "them. A command printing a secret has the value replaced before you see it; do "
        "not go looking for credentials. Everything is read-only and tenant data is walled off, so look "
        "freely: our containers, compose files, units, logs, devices, /proc, /sys. When "
        "you need something you can read, read it -- never ask the operator to fetch it, "
        "and never tell them you are going to look without including the block. Send "
        "the operator a line saying what you are checking, if anything, and nothing "
        "else until you have the answer.\n\n"
        "You have web search. Use it to check upstream projects, issues and "
        "documentation, and cite what you read.\n\n"
        "To propose a change, put it in a block like this:\n"
        "```plan\n"
        '{"command": "one command, or a POSIX sh script starting with set -eu, as a JSON '
        f'string with \\n between lines (at most {MAX_COMMAND_CHARS} characters)", '
        '"intent": "what it does and why", "rollback": "how to undo it", '
        '"verify": ["read-only command that shows it worked"]}\n'
        "```\n"
        "A plan in any other block -- ```sh, ```bash -- is only text: nobody can approve "
        "it and it never runs. I put a ```plan block to the operator with an Approve button; one tap runs the whole plan in "
        "an audited management session, and I run the checks afterwards. Propose one "
        "plan per reply, and only once you have looked enough to stand behind it. A plan "
        "naming a customer's rental (C.<digits>) is refused."
    )
