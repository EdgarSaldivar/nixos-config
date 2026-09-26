"""Credential-free, identifier-only evidence tools and a bounded MCP server."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Mapping, Protocol

from .policy import validated_action_parameters


MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_MCP_FRAME_BYTES = 256 * 1024
MAX_EVIDENCE_SLICE_BYTES = 64 * 1024
MAX_BACKEND_ROWS = 100
TARGET_ID = "17049"

_IDENTIFIER = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}\Z")
_CREDENTIAL_NAMES = {
    ".env",
    "auth.json",
    "credentials",
    "credentials.json",
    "credential",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
}
_CREDENTIAL_WORDS = ("secret", "password", "private-key", "private_key", "token")


class EvidenceError(ValueError):
    """A safe validation or backend error suitable for model-visible output."""


class EvidenceBackend(Protocol):
    def query_asset_links(self, target_id: str, asset_id: str, limit: int) -> Any: ...

    def request_readonly_refresh(self, target_id: str, catalog_id: str) -> Any: ...

    def submit_proposal(self, target_id: str, proposal: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class EvidenceEntry:
    evidence_id: str
    target_id: str
    relative_path: str
    media_type: str = "application/json"


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise EvidenceError(f"invalid {name}")
    lowered = value.lower()
    if lowered in _CREDENTIAL_NAMES:
        raise EvidenceError(f"credential-like {name} rejected")
    return value


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise EvidenceError(f"invalid {name}")
    return value


def _json_size(value: Any, limit: int) -> None:
    try:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise EvidenceError("value is not bounded JSON") from error
    if len(encoded) > limit:
        raise EvidenceError("value exceeds size limit")


class EvidenceService:
    """Resolve a fixed sanitized manifest and dispatch narrow injected operations."""

    def __init__(
        self,
        root: Path,
        manifest: Mapping[str, EvidenceEntry | Mapping[str, Any]],
        backend: EvidenceBackend,
        *,
        target_id: str = TARGET_ID,
        allowed_refreshes: frozenset[str] = frozenset(),
    ):
        root = Path(root)
        try:
            if stat.S_ISLNK(root.lstat().st_mode):
                raise EvidenceError("evidence root symlink rejected")
            self.root = root.resolve(strict=True)
        except OSError as error:
            raise EvidenceError("evidence root unavailable") from error
        if not self.root.is_dir():
            raise EvidenceError("evidence root must be a directory")
        self.target_id = _identifier(target_id, "target id")
        if self.target_id != TARGET_ID:
            raise EvidenceError("cross-target service rejected")
        self.backend = backend
        self.allowed_refreshes = frozenset(_identifier(item, "refresh catalog id") for item in allowed_refreshes)
        self.manifest: dict[str, EvidenceEntry] = {}
        if len(manifest) > 10_000:
            raise EvidenceError("manifest exceeds entry limit")
        for key, raw in manifest.items():
            evidence_id = _identifier(key, "evidence id")
            if isinstance(raw, EvidenceEntry):
                entry = raw
            elif isinstance(raw, Mapping):
                try:
                    entry = EvidenceEntry(
                        evidence_id=evidence_id,
                        target_id=str(raw["target_id"]),
                        relative_path=str(raw["relative_path"]),
                        media_type=str(raw.get("media_type", "application/json")),
                    )
                except KeyError as error:
                    raise EvidenceError("invalid manifest entry") from error
            else:
                raise EvidenceError("invalid manifest entry")
            if entry.evidence_id != evidence_id or entry.target_id != self.target_id:
                raise EvidenceError("cross-target or mismatched manifest entry")
            if entry.media_type not in {"application/json", "text/plain", "text/csv"}:
                raise EvidenceError("unsupported evidence media type")
            self._validate_relative_path(entry.relative_path)
            self.manifest[evidence_id] = entry

    @staticmethod
    def _validate_relative_path(value: str) -> PurePosixPath:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise EvidenceError("invalid evidence path")
        path = PurePosixPath(value)
        if str(path) != value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise EvidenceError("evidence path traversal rejected")
        for part in path.parts:
            lowered = part.lower()
            if lowered in _CREDENTIAL_NAMES or any(word in lowered for word in _CREDENTIAL_WORDS):
                raise EvidenceError("credential path rejected")
        return path

    def _safe_file(self, entry: EvidenceEntry) -> Path:
        relative = self._validate_relative_path(entry.relative_path)
        current = self.root
        for part in relative.parts:
            candidate = current / part
            try:
                mode = candidate.lstat().st_mode
            except OSError as error:
                raise EvidenceError("evidence unavailable") from error
            if stat.S_ISLNK(mode):
                raise EvidenceError("evidence symlink rejected")
            current = candidate
        resolved = current.resolve(strict=True)
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise EvidenceError("evidence outside root rejected") from error
        if not resolved.is_file():
            raise EvidenceError("evidence is not a file")
        return resolved

    def _open_safe_file(self, entry: EvidenceEntry) -> tuple[int, int]:
        """Open through directory fds so a path swap cannot bypass symlink checks."""
        relative = self._validate_relative_path(entry.relative_path)
        self._safe_file(entry)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self.root, os.O_RDONLY | directory | nofollow)
        try:
            for part in relative.parts[:-1]:
                next_descriptor = os.open(
                    part, os.O_RDONLY | directory | nofollow, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = next_descriptor
            file_descriptor = os.open(
                relative.parts[-1], os.O_RDONLY | nofollow, dir_fd=descriptor
            )
            details = os.fstat(file_descriptor)
            if not stat.S_ISREG(details.st_mode):
                os.close(file_descriptor)
                raise EvidenceError("evidence is not a regular file")
            return file_descriptor, details.st_size
        except EvidenceError:
            raise
        except OSError as error:
            raise EvidenceError("evidence unavailable") from error
        finally:
            os.close(descriptor)

    def read_evidence_slice(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self._exact_keys(arguments, {"target_id", "evidence_id", "offset", "limit"})
        self._target(arguments.get("target_id"))
        evidence_id = _identifier(arguments.get("evidence_id"), "evidence id")
        offset = _bounded_int(arguments.get("offset"), "offset", 0, 16 * 1024 * 1024)
        limit = _bounded_int(arguments.get("limit"), "limit", 1, MAX_EVIDENCE_SLICE_BYTES)
        entry = self.manifest.get(evidence_id)
        if entry is None:
            raise EvidenceError("evidence id is not approved")
        descriptor, size = self._open_safe_file(entry)
        if size > 16 * 1024 * 1024:
            os.close(descriptor)
            raise EvidenceError("evidence file exceeds service bound")
        try:
            os.lseek(descriptor, offset, os.SEEK_SET)
            data = os.read(descriptor, limit + 1)
        finally:
            os.close(descriptor)
        truncated = len(data) > limit or offset + len(data) < size
        data = data[:limit]
        return {
            "evidence_id": evidence_id,
            "target_id": self.target_id,
            "offset": offset,
            "bytes": len(data),
            "truncated": truncated,
            "media_type": entry.media_type,
            "text": data.decode("utf-8", "replace"),
        }

    def query_asset_links(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self._exact_keys(arguments, {"target_id", "asset_id", "limit"})
        self._target(arguments.get("target_id"))
        asset_id = _identifier(arguments.get("asset_id"), "asset id")
        limit = _bounded_int(arguments.get("limit"), "limit", 1, MAX_BACKEND_ROWS)
        try:
            result = self.backend.query_asset_links(self.target_id, asset_id, limit)
        except Exception as error:
            raise EvidenceError("asset backend unavailable") from error
        if not isinstance(result, list) or len(result) > limit:
            raise EvidenceError("asset backend returned invalid rows")
        _json_size(result, MAX_MCP_FRAME_BYTES // 2)
        return {"target_id": self.target_id, "asset_id": asset_id, "links": result}

    def request_readonly_refresh(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self._exact_keys(arguments, {"target_id", "catalog_id"})
        self._target(arguments.get("target_id"))
        catalog_id = _identifier(arguments.get("catalog_id"), "catalog id")
        if catalog_id not in self.allowed_refreshes:
            raise EvidenceError("refresh is not allowlisted")
        try:
            result = self.backend.request_readonly_refresh(self.target_id, catalog_id)
        except Exception as error:
            raise EvidenceError("refresh backend unavailable") from error
        if not isinstance(result, Mapping):
            raise EvidenceError("refresh backend returned invalid result")
        _json_size(result, 16 * 1024)
        return {"target_id": self.target_id, "catalog_id": catalog_id, "request": result, "execution": "read-only-refresh-only"}

    def submit_proposal(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"target_id", "incident_id", "evidence_revision", "proposal_type", "summary", "action_class", "parameters"}
        self._exact_keys(arguments, allowed)
        self._target(arguments.get("target_id"))
        incident_id = _identifier(arguments.get("incident_id"), "incident id")
        evidence_revision = _identifier(arguments.get("evidence_revision"), "evidence revision")
        proposal_type = arguments.get("proposal_type")
        if proposal_type not in {"analysis", "action"}:
            raise EvidenceError("invalid proposal type")
        summary = arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip() or len(summary.encode("utf-8")) > 16 * 1024:
            raise EvidenceError("invalid proposal summary")
        action_class = arguments.get("action_class")
        if action_class is not None:
            action_class = _identifier(action_class, "action class")
        if proposal_type == "action" and action_class is None:
            raise EvidenceError("action proposal requires action class")
        parameters = arguments.get("parameters", {})
        try:
            safe_parameters = validated_action_parameters(parameters)
        except ValueError:
            # Do not reflect a rejected key or its value into model-visible errors.
            raise EvidenceError("proposal contains forbidden parameter fields") from None
        forbidden = {"approved", "approval", "execute", "execution", "command", "shell", "sql", "url", "path"}

        def has_forbidden_key(value: Any) -> bool:
            if isinstance(value, dict):
                return any(
                    str(key).lower() in forbidden or has_forbidden_key(item)
                    for key, item in value.items()
                )
            if isinstance(value, list):
                return any(has_forbidden_key(item) for item in value)
            return False

        _json_size(safe_parameters, 16 * 1024)
        if has_forbidden_key(safe_parameters):
            raise EvidenceError("proposal contains forbidden control fields")
        proposal = {
            "incident_id": incident_id,
            "evidence_revision": evidence_revision,
            "proposal_type": proposal_type,
            "summary": summary,
            "action_class": action_class,
            "parameters": safe_parameters,
            "execution": "not-authorized",
            "approval": "not-created",
        }
        try:
            result = self.backend.submit_proposal(self.target_id, proposal)
        except Exception as error:
            raise EvidenceError("proposal backend unavailable") from error
        if not isinstance(result, Mapping):
            raise EvidenceError("proposal backend returned invalid result")
        _json_size(result, 16 * 1024)
        return {"target_id": self.target_id, "proposal": result, "execution": "not-authorized", "approval": "not-created"}

    def call(self, name: str, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise EvidenceError("tool arguments must be an object")
        tools = {
            "read_evidence_slice": self.read_evidence_slice,
            "query_asset_links": self.query_asset_links,
            "request_readonly_refresh": self.request_readonly_refresh,
            "submit_proposal": self.submit_proposal,
        }
        handler = tools.get(name)
        if handler is None:
            raise EvidenceError("unknown tool")
        return handler(arguments)

    def _target(self, value: Any) -> None:
        if value != self.target_id:
            raise EvidenceError("cross-target request rejected")

    @staticmethod
    def _exact_keys(arguments: Mapping[str, Any], allowed: set[str]) -> None:
        extras = set(arguments) - allowed
        if extras:
            raise EvidenceError("unsupported arguments")


def tool_definitions() -> list[dict[str, Any]]:
    identifier = {"type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$"}
    target = {"type": "string", "const": TARGET_ID}
    closed = {"additionalProperties": False}
    return [
        {
            "name": "read_evidence_slice",
            "description": "Read a bounded slice of one approved sanitized evidence ID.",
            "inputSchema": {"type": "object", "properties": {"target_id": target, "evidence_id": identifier, "offset": {"type": "integer", "minimum": 0, "maximum": 16777216}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_EVIDENCE_SLICE_BYTES}}, "required": ["target_id", "evidence_id", "offset", "limit"], **closed},
        },
        {
            "name": "query_asset_links",
            "description": "Query bounded asset relationships through the controller backend.",
            "inputSchema": {"type": "object", "properties": {"target_id": target, "asset_id": identifier, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_BACKEND_ROWS}}, "required": ["target_id", "asset_id", "limit"], **closed},
        },
        {
            "name": "request_readonly_refresh",
            "description": "Request one catalogued allowlisted read-only evidence refresh.",
            "inputSchema": {"type": "object", "properties": {"target_id": target, "catalog_id": identifier}, "required": ["target_id", "catalog_id"], **closed},
        },
        {
            "name": "submit_proposal",
            "description": "Submit analysis or an action proposal; never execute or approve it.",
            "inputSchema": {"type": "object", "properties": {"target_id": target, "incident_id": identifier, "evidence_revision": identifier, "proposal_type": {"type": "string", "enum": ["analysis", "action"]}, "summary": {"type": "string", "minLength": 1, "maxLength": 16384}, "action_class": {"anyOf": [identifier, {"type": "null"}]}, "parameters": {"type": "object", "maxProperties": 64}}, "required": ["target_id", "incident_id", "evidence_revision", "proposal_type", "summary"], **closed},
        },
    ]


class McpServer:
    """Minimal MCP lifecycle and tools server for newline-delimited stdio."""

    def __init__(self, service: EvidenceService):
        self.service = service
        self.negotiated = False
        self.initialized = False

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            return self._error(request_id, -32600, "Invalid Request")
        if method == "initialize":
            if self.negotiated:
                return self._error(request_id, -32600, "Already initialized")
            if request_id is None:
                return None
            if params.get("protocolVersion") != MCP_PROTOCOL_VERSION:
                return self._error(request_id, -32602, "Unsupported protocol version")
            self.negotiated = True
            return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "terracompute-evidence", "title": "Terracompute Evidence", "version": "0.1.0"}}}
        if method == "notifications/initialized":
            if request_id is not None or not self.negotiated:
                return None if request_id is None else self._error(request_id, -32600, "Invalid initialization notification")
            self.initialized = True
            return None
        if method == "ping" and request_id is not None:
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if request_id is None:
            return None
        if not self.initialized:
            return self._error(request_id, -32002, "Server not initialized")
        if method == "tools/list":
            if params.get("cursor") not in (None, ""):
                return self._error(request_id, -32602, "Invalid cursor")
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_definitions()}}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return self._error(request_id, -32602, "Invalid tool name")
            try:
                result = self.service.call(name, params.get("arguments", {}))
                text = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
                response = {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}], "structuredContent": result, "isError": False}}
                _json_size(response, MAX_MCP_FRAME_BYTES)
                return response
            except EvidenceError as error:
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(error)}], "isError": True}}
            except Exception:
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": "tool unavailable"}], "isError": True}}
        return self._error(request_id, -32601, "Method not found")


def serve_stdio(
    service: EvidenceService,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
    *,
    max_frame_bytes: int = MAX_MCP_FRAME_BYTES,
) -> None:
    """Serve bounded newline-delimited MCP. Stdout contains protocol frames only."""
    if max_frame_bytes < 1024 or max_frame_bytes > MAX_MCP_FRAME_BYTES:
        raise ValueError("invalid MCP frame bound")
    incoming = input_stream if input_stream is not None else sys.stdin.buffer
    outgoing = output_stream if output_stream is not None else sys.stdout.buffer
    server = McpServer(service)
    while True:
        line = incoming.readline(max_frame_bytes + 1)
        if not line:
            return
        if len(line) > max_frame_bytes or not line.endswith(b"\n"):
            while line and not line.endswith(b"\n"):
                line = incoming.readline(max_frame_bytes + 1)
            reply = McpServer._error(None, -32700, "Parse error")
        else:
            try:
                decoded = line.decode("utf-8")
                if "\n" in decoded[:-1] or "\r" in decoded[:-1]:
                    raise ValueError
                message = json.loads(decoded)
                reply = server.handle(message)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                reply = McpServer._error(None, -32700, "Parse error")
        if reply is not None:
            encoded = json.dumps(reply, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
            if len(encoded) > max_frame_bytes:
                encoded = json.dumps(McpServer._error(reply.get("id"), -32603, "Response too large"), separators=(",", ":")).encode() + b"\n"
            outgoing.write(encoded)
            outgoing.flush()
