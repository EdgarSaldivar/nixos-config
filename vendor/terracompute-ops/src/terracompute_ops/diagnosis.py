"""What a diagnosis may say, and what it is allowed to ask for.

The investigator reads evidence and answers with a finding: what it thinks is wrong and,
optionally, one action from the catalogue below. Model text is data, never a command, so
this module parses that answer strictly and refuses anything it does not recognise.

The catalogue is the boundary. A finding can only name an action defined here, with
parameters this module validates, and each action's tier decides what happens next:

- ``Tier.LOOK``   read-only, runs freely.
- ``Tier.REPAIR`` reversible work on monitoring we installed; runs without approval
  under its own limits.
- ``Tier.CHANGE`` touches tenants, the machine or money; always needs a human approval.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping

MAX_FINDING_BYTES = 16 * 1024
MAX_TEXT_CHARS = 1200
MAX_LIST_ITEMS = 8
MAX_EVIDENCE_REFS = 16
# One round of the read loop may ask for this many commands; a genuinely broad question
# is several narrow reads, not one unbounded scan, and the round can always ask again.
MAX_READS_PER_ROUND = 8
MAX_READ_COMMAND_CHARS = 512
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
# Only monitoring we installed may be restarted or replaced; tenant containers never --
# a renter's container is named C.<id> by Vast and appears in no list here.
#
# Enumerated on machine 17049 on 2026-09-19, because two of the three names this held
# before did not exist on the machine: there is no `gddr6-exporter` (it is
# `vast-gddr6-metrics-exporter-1`) and no `vast-node-exporter` (it is `node-exporter`).
# A finding naming either passed validation here and then failed at execution with "No
# such object", so two thirds of the only action this service can take were unreachable.
# Re-check with `docker ps -a --format '{{.Names}}'` and keep tenants out of it.
MONITORING_CONTAINERS = (
    "dcgm-exporter",
    "node-exporter",
    "vast-gddr6-metrics-exporter-1",
    "vast-vastai-exporter-1",
    "vast-prometheus-1",
    "vast-grafana-1",
    "cadvisor",
)


class Tier(str, Enum):
    LOOK = "look"
    REPAIR = "repair"
    CHANGE = "change"


def _one_of(values: tuple[str, ...]) -> Callable[[object], bool]:
    return lambda value: isinstance(value, str) and value in values


def _matches(pattern: re.Pattern[str]) -> Callable[[object], bool]:
    return lambda value: isinstance(value, str) and bool(pattern.fullmatch(value))


@dataclass(frozen=True)
class CatalogueEntry:
    name: str
    tier: Tier
    parameters: Mapping[str, Callable[[object], bool]]
    summary: str
    # False until an adapter can carry it out; the finding is still allowed to ask.
    implemented: bool = False


CATALOGUE: dict[str, CatalogueEntry] = {
    entry.name: entry
    for entry in (
        CatalogueEntry(
            "restart-monitoring-container", Tier.REPAIR,
            {"container": _one_of(MONITORING_CONTAINERS)},
            "Restart one monitoring container we installed. Tenants are untouched.",
            implemented=True,
        ),
        CatalogueEntry(
            # CHANGE, though the charter files replacing an abandoned image under what
            # the agent may do on its own. The charter is reasoning about disruption and
            # is right about that: nobody paying us feels it, and it is reversible.
            # What it does not weigh is that the IMAGE is chosen by a model reading
            # tenant-written text, and this entry validates the image as any repo:tag.
            # Replacing an exporter means pulling and running unreviewed code beside the
            # tenants, with the nvidia runtime; "reversible" stops meaning anything once
            # the code has run. A person presses the button for that one.
            "replace-monitoring-container", Tier.CHANGE,
            {"container": _one_of(MONITORING_CONTAINERS), "image": _matches(_IMAGE)},
            "Replace a monitoring container with a different pinned image, keeping its "
            "configuration. Tenants are untouched.",
        ),
        CatalogueEntry(
            "rebind-gpu", Tier.CHANGE,
            {"bdf": _matches(_BDF), "driver": _one_of(("nvidia", "vfio-pci"))},
            "Move one GPU between the NVIDIA driver and vfio-pci. Kills any VM using it.",
        ),
        CatalogueEntry(
            "destroy-rental", Tier.CHANGE, {"rental": _matches(_RENTAL)},
            "Destroy one Vast rental through the API.",
        ),
        CatalogueEntry(
            "reboot-host", Tier.CHANGE, {},
            "Reboot the whole machine. Every tenant loses their work.",
        ),
    )
}


_OBSERVE_CONTRACT = (
    "If you need to look at the host before you can answer, do not guess. Reply "
    "instead with only:\n"
    '{"reads_requested": ["a read-only shell command", ...], "note": "why"}\n'
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
    "When you have enough to conclude, answer with the finding object above instead.\n\n"
)


class FindingRejected(ValueError):
    """The answer did not meet the contract; nothing in it is used."""


@dataclass(frozen=True)
class ProposedAction:
    name: str
    parameters: Mapping[str, str]

    @property
    def entry(self) -> CatalogueEntry:
        return CATALOGUE[self.name]

    @property
    def tier(self) -> Tier:
        return self.entry.tier

    def describe(self) -> str:
        arguments = ", ".join(f"{key}={self.parameters[key]}" for key in sorted(self.parameters))
        return f"{self.name}({arguments})" if arguments else self.name


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
    """Parse the requested action, or report what was asked for and refused."""
    if value in (None, "", "none"):
        return None, None
    if not isinstance(value, dict):
        raise FindingRejected("action must be an object or null")
    name = value.get("name")
    if not isinstance(name, str) or not _TEXT.fullmatch(name):
        raise FindingRejected("action name must be text")
    entry = CATALOGUE.get(name)
    if entry is None:
        # Not a refusal of the finding: the model may want something we cannot do yet,
        # and that is worth telling a human rather than discarding.
        return None, name[:128]
    parameters = value.get("parameters", {})
    if not isinstance(parameters, dict) or set(parameters) != set(entry.parameters):
        raise FindingRejected(f"{name} takes exactly {sorted(entry.parameters)}")
    for key, check in entry.parameters.items():
        if not check(parameters[key]):
            raise FindingRejected(f"{name}.{key} is not an accepted value")
    return ProposedAction(name, {key: str(parameters[key]) for key in entry.parameters}), None


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
        if len(entry) > MAX_READ_COMMAND_CHARS or "\x00" in entry:
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
    actions = "\n".join(
        f"- {entry.name} (tier {entry.tier.value}"
        f"{', needs human approval' if entry.tier is Tier.CHANGE else ', runs immediately'}"
        f"{'' if entry.implemented else ', NOT YET IMPLEMENTED'}): {entry.summary}"
        f" Parameters: {sorted(entry.parameters) or 'none'}."
        for entry in CATALOGUE.values()
    )
    return (
        "Answer with one JSON object and nothing else:\n"
        '{"summary": "one line, what is wrong",\n'
        ' "mechanism": "how it fails, citing the evidence",\n'
        ' "evidence": ["what you rely on, naming a read such as'
        ' target-read@gpu-handles where you can", ...],\n'
        ' "action": {"name": "<from the catalogue>", "parameters": {...}} or null,\n'
        ' "durable": {"action": {"name": "<from the catalogue>", "parameters": {...}}'
        ' or null, "recommendation": "the smallest change that removes the mechanism,'
        ' in your own words, if no catalogued action expresses it"} or null,\n'
        ' "recurrence": {"expected": true|false, "mechanism": "why it comes back",'
        ' "ends_when": "what would stop it"} or null,\n'
        ' "expected_effect": "what you expect to observe if the action works",\n'
        ' "alternatives": ["other explanation and the check that separates it", ...],\n'
        ' "confidence": "low" | "medium" | "high"}\n\n'
        + (_OBSERVE_CONTRACT if can_observe else "")
        + "Ask for at most one action, and only from this catalogue:\n"
        f"{actions}\n\n"
        "Choose null when no catalogued action is right, and say in durable or "
        "alternatives what you would want instead. Never invent an action name, a "
        "parameter, or a shell command: anything outside the catalogue is refused and a "
        "human reads your text instead.\n\n"
        "`action` answers one question and `durable` answers another. `action` is the "
        "safest thing that restores service now, and the least disruptive action that "
        "addresses the mechanism is the right one for it. `durable` is the smallest "
        "change, supported by the evidence, that stops this recurring -- which is often "
        "a different and larger thing, and is worth naming even when you would not do "
        "it today. Either may be null. If the immediate action only buys time, say so "
        "in `recurrence` rather than leaving it to be inferred: a fix that has to be "
        "repeated is more disruptive over its life than one change made once. Where the "
        "durable answer is to replace or remove a component, name it concretely and say "
        "what you relied on to identify the replacement.\n\n"
        "`durable.recommendation` is prose for a person to read and decide on. It is not "
        "bound to the catalogue, needs no parameters and authorises nothing, so the bar "
        "for writing it is that the evidence supports it -- not that this system could "
        "carry it out. Withholding the real fix because no catalogued action expresses "
        "it leaves the operator with only the stopgap.\n\n"
        "A component being unmaintained, abandoned or superseded is a durable finding "
        "like any other, and worth saying even though you cannot verify it here: this "
        "turn has no network. Name the image and where it lives so a person can check "
        "it, and label what you remember about a project as memory rather than as "
        "something you looked up. Do not spend effort trying to reach a registry or a "
        "repository -- you will not get there, and an answer that reads as though you "
        "did is worse than one that says plainly what it could not check."
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
            "Investigate this GPU again from the beginning, ignoring my own waiting "
            "periods. It proposes; it does not act.",
        ),
        SteeringEntry(
            "investigate", "",
            "Look the machine over from the beginning and say what you find, when they "
            "think something is wrong and I have not found it myself. It looks and "
            "reports; it changes nothing.",
        ),
        SteeringEntry(
            "withdraw", "bdf",
            "Take back the restart request waiting on this GPU, so nothing is waiting "
            "on a button that may no longer mean what it said.",
        ),
    )
}
_STEER_LINE = re.compile(r"^STEER:\s*([a-z-]{1,24})(?:\s+([A-Za-z0-9._:-]{1,64}))?\s*$")


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
    match = _STEER_LINE.fullmatch(lines[-1].strip()) if lines else None
    if match is None:
        return text.strip(), None
    prose = "\n".join(lines[:-1]).strip()
    entry = STEERING.get(match.group(1))
    argument = (match.group(2) or "").strip()
    if entry is None:
        return prose or text.strip(), None
    if entry.takes == "bdf":
        if not argument or not _BDF.fullmatch(argument):
            return prose or text.strip(), None
    elif argument:
        return prose or text.strip(), None
    return prose, Steer(entry.name, argument)


def steering_text() -> str:
    """The steering vocabulary, as given to the model."""
    lines = "\n".join(
        f"- {entry.name}"
        f"{' <pci-address>' if entry.takes == 'bdf' else ''}: {entry.summary}"
        for entry in STEERING.values()
    )
    return (
        "If what they said asks you to change what I am doing, end your reply with one "
        "final line naming it, exactly:\n"
        "STEER: <name> [<pci-address>]\n"
        f"{lines}\n"
        "Use it only when they are asking for that thing -- not when they are "
        "discussing it, asking what it would do, or telling you not to. If you are not "
        "sure they are asking, leave the line off and ask them. Say nothing about this "
        "format in the words above it; write to them as you would anyway, and I will "
        "tell them what I did."
    )
