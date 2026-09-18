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
# Only monitoring we installed may be restarted or replaced; tenant containers never.
MONITORING_CONTAINERS = ("dcgm-exporter", "gddr6-exporter", "vast-node-exporter")


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
            "replace-monitoring-container", Tier.CHANGE,
            {"container": _one_of(MONITORING_CONTAINERS), "image": _matches(_IMAGE)},
            "Replace a monitoring container with a different pinned image.",
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

    @property
    def tier(self) -> Tier | None:
        return None if self.action is None else self.action.tier


def _text(document: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = document.get(key, "")
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str) or not _TEXT.fullmatch(value):
        raise FindingRejected(f"{key} must be printable text of at most {MAX_TEXT_CHARS} characters")
    return value


def _string_list(document: Mapping[str, Any], key: str, pattern: re.Pattern[str] | None) -> tuple[str, ...]:
    values = document.get(key, [])
    if values is None:
        return ()
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


def parse_finding(text: str) -> Finding:
    """Parse one model answer. Anything unexpected raises; nothing is guessed."""
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
    confidence = document.get("confidence")
    if confidence not in CONFIDENCE:
        raise FindingRejected(f"confidence must be one of {CONFIDENCE}")
    action, unsupported = _action(document.get("action"))
    return Finding(
        summary=_text(document, "summary"),
        mechanism=_text(document, "mechanism"),
        # What the finding rests on, in the investigator's own words. This is for a
        # person to check, not for a machine to act on -- the action is what is bound
        # to the catalogue -- so a good diagnosis is never thrown away over the shape
        # of its citations.
        evidence=_string_list(document, "evidence", None),
        action=action,
        expected_effect=_text(document, "expected_effect", required=action is not None),
        alternatives=_string_list(document, "alternatives", None),
        prevention=_text(document, "prevention", required=False),
        confidence=confidence,
        unsupported_request=unsupported,
    )


def contract_text() -> str:
    """The answer contract and catalogue, as given to the model."""
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
        ' "expected_effect": "what you expect to observe if the action works",\n'
        ' "alternatives": ["other explanation and the check that separates it", ...],\n'
        ' "prevention": "how to stop it recurring",\n'
        ' "confidence": "low" | "medium" | "high"}\n\n'
        "Ask for at most one action, and only from this catalogue:\n"
        f"{actions}\n\n"
        "Choose null when no catalogued action is right, and say in prevention or "
        "alternatives what you would want instead. Never invent an action name, a "
        "parameter, or a shell command: anything outside the catalogue is refused and a "
        "human reads your text instead."
    )
