"""Bounded command-line entry point; there is deliberately no remote command option."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .state import StateStore
from .supervisor import Supervisor
from .telegram import drain_outbox, read_credential

MAX_PROBE_BYTES = 1024 * 1024
SSH_TARGET = re.compile(r"^[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.:-]*$")


def load_probe(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_PROBE_BYTES:
        raise ValueError("probe exceeds the one MiB input limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("probe must be a JSON object")
    return value


def fixed_ssh_probe(
    ssh_binary: str,
    ssh_target: str,
    identity_file: Path,
    known_hosts_file: Path,
) -> dict[str, Any]:
    """Invoke only the account's server-side forced command; no command is supplied."""
    if not SSH_TARGET.fullmatch(ssh_target):
        raise ValueError("SSH target must be a plain user@host value")
    completed = subprocess.run(
        [
            ssh_binary,
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts_file}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"IdentityFile={identity_file}",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "RequestTTY=no",
            ssh_target,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=True,
    )
    return load_probe(completed.stdout)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="terracompute-ops")
    sub = result.add_subparsers(dest="action", required=True)
    observe = sub.add_parser("observe", help="process normalized JSON from stdin")
    observe.add_argument("--state-dir", required=True, type=Path)
    observe.add_argument("--machine-id", default="17049")

    run = sub.add_parser("run", help="perform one bounded observation and notify")
    run.add_argument("--state-dir", required=True, type=Path)
    run.add_argument("--machine-id", default="17049")
    run.add_argument("--ssh-target", required=True)
    run.add_argument("--ssh-binary", default="ssh")
    run.add_argument("--ssh-identity", required=True, type=Path)
    run.add_argument("--known-hosts", required=True, type=Path)
    run.add_argument("--telegram-token", required=True, type=Path)
    run.add_argument("--telegram-chat-id", required=True, type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = StateStore(args.state_dir)
    try:
        supervisor = Supervisor(store, expected_machine_id=args.machine_id)
        if args.action == "observe":
            result = supervisor.observe(load_probe(sys.stdin.buffer.read(MAX_PROBE_BYTES + 1)))
            print(
                json.dumps(
                    {
                        "healthy": result.healthy,
                        "created": len(result.created),
                        "duplicates": result.duplicates,
                    },
                    sort_keys=True,
                )
            )
            return 0

        # Read all mandatory credentials before contacting the target. Missing or
        # malformed inputs therefore fail the run closed.
        token = read_credential(args.telegram_token)
        chat_id = read_credential(args.telegram_chat_id)
        for required in (args.ssh_identity, args.known_hosts):
            if not required.is_file() or required.stat().st_size == 0:
                raise ValueError("required SSH credential file is absent or empty")
        # An unreachable target must not strand an older notification. Credential
        # validation still happens first, so delivery never weakens fail-closed
        # identity handling.
        drain_outbox(store, token, chat_id)
        probe = fixed_ssh_probe(
            args.ssh_binary, args.ssh_target, args.ssh_identity, args.known_hosts
        )
        supervisor.observe(probe)
        drain_outbox(store, token, chat_id)
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        # Never render exception text: URL-bearing transport errors can include a
        # Telegram bot token, and credential paths do not aid unattended recovery.
        print(f"terracompute-ops: observation failed ({type(error).__name__})", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
