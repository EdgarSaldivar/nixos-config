"""Durable batch/epoch and recovery event interface (schema v4).

Consumers use StateStore.current_epoch(target), latest_accepted_evidence(target,
source), accepted_condition(incident_key), recovery_events(after_id, limit).
SQL equivalents: host_epochs (MAX(epoch) per target), observation_batches
(ordering='current', descending id), incident_conditions joined to batches, and
recovery_events (ascending id). Event IDs and payloads are immutable. A consumer
owns its cursor/ack; reading never consumes an event. Evidence is addressed by
batch ID + JSON pointer, or observation ID. Times are measurement times; receipt
is never substituted for a dependency measurement. All lifecycle writes take
one BEGIN IMMEDIATE lock, including epoch retirement and recovery settlement.

Only the authenticated SSH/target adapter may set boot_verified and measured_at
(the controller's collection time). The stdin ingest path strips those fields.
An external source may attach to an existing epoch but cannot create one.

Compatibility: incident keys/signatures and ObservationWriteResult tuple
unpacking are unchanged. ObservationResult adds batch_id/current. A missing
coverage row is unknown even when healthy/complete is true. SSH recovery also
requires a verified epoch; independent external checks may explicitly retain
unknown host-boot uncertainty. The former separate
settle_absent_incidents API now raises ValueError. ``historical`` incident status
and non-current batch ordering must be excluded from action/investigation work.
Derived batches require actual adopted dependency IDs, all refreshed since the
previous accepted derived batch; duplicate dependency sets are history. Their
330-second maximum sample gap accommodates the five-minute host collector.
accepted_condition returns stored evidence_freshness plus freshness/age_seconds
computed at read time; current also checks epoch membership. SQL readers must
perform the same age/epoch checks. Inventory.capture_probe accepts an optional
accepted_batch_id and uses its verified collection time while retaining target
source_observed_at. Bare legacy calls retain timestamp ordering.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .incidents import bounded_evidence, canonical_json, evidence_digest
from .recovery_coverage import condition, evidence_pointer, validate_coverage


# Two five-minute SSH cycles, bounded collection, and scheduler slack.
DERIVED_INPUT_MAX_AGE_SECONDS = 2 * 300 + 45 + 60


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def dependency_fresh(row: Any, at: datetime) -> bool:
    """A derived check cannot extend an input's own measurement lifetime."""
    if row is None or row["freshness"] != "fresh":
        return False
    document = json.loads(row["evidence_json"])
    limit = (DERIVED_INPUT_MAX_AGE_SECONDS if row["source"] in {"ssh", "target-probe"}
             else document.get("_max_source_age", 180))
    age = (at - instant(row["measured_utc"])).total_seconds()
    return -30 <= age <= limit


class Lifecycle:
    def _migrate_3_to_4(self) -> None:
        statements = [
            """CREATE TABLE host_epochs (
                target TEXT NOT NULL, epoch INTEGER NOT NULL, boot_id TEXT NOT NULL,
                verified_utc TEXT NOT NULL, retired_utc TEXT,
                PRIMARY KEY(target,epoch), UNIQUE(target,boot_id))""",
            """CREATE TABLE observation_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL,
                source TEXT NOT NULL, delivery_key TEXT UNIQUE, epoch INTEGER,
                boot_id TEXT NOT NULL, source_utc TEXT NOT NULL, measured_utc TEXT NOT NULL,
                receipt_utc TEXT NOT NULL, ordering TEXT NOT NULL, freshness TEXT NOT NULL,
                complete INTEGER NOT NULL, coverage_json TEXT NOT NULL,
                dependencies_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
                evidence_json BLOB NOT NULL)""",
            "CREATE INDEX batches_source ON observation_batches(target,source,id)",
            """CREATE TABLE incident_conditions (
                incident_key TEXT PRIMARY KEY REFERENCES incidents(dedup_key),
                batch_id INTEGER NOT NULL REFERENCES observation_batches(id),
                observation_id INTEGER NOT NULL REFERENCES observations(id),
                source TEXT NOT NULL, event_code TEXT NOT NULL, check_name TEXT NOT NULL,
                resource TEXT NOT NULL, condition TEXT NOT NULL,
                freshness TEXT NOT NULL)""",
            """CREATE TABLE recovery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE, incident_key TEXT NOT NULL REFERENCES incidents(dedup_key),
                episode INTEGER NOT NULL, transition_id INTEGER NOT NULL UNIQUE REFERENCES transitions(id),
                epoch INTEGER, boot_id TEXT, started_utc TEXT NOT NULL, ended_utc TEXT NOT NULL,
                payload_json TEXT NOT NULL)""",
            """CREATE TABLE recovery_evidence (
                event_id INTEGER NOT NULL REFERENCES recovery_events(id),
                batch_id INTEGER REFERENCES observation_batches(id),
                observation_id INTEGER REFERENCES observations(id))""",
            """CREATE TRIGGER recovery_events_immutable_update BEFORE UPDATE ON recovery_events
                BEGIN SELECT RAISE(ABORT, 'immutable recovery event'); END""",
            """CREATE TRIGGER recovery_events_immutable_delete BEFORE DELETE ON recovery_events
                BEGIN SELECT RAISE(ABORT, 'immutable recovery event'); END""",
        ]
        for statement in statements:
            self.db.execute(statement)
        self._add_column("observations", "batch_id INTEGER REFERENCES observation_batches(id)")
        self._add_column("incidents", "recovery_epoch INTEGER")
        self._add_column("incidents", "recovery_batch_id INTEGER")
        # Legacy healthy windows cannot prove coverage or boot provenance. Keep
        # every incident, episode, ack, bundle, attempt and transition intact.
        self.db.execute("""UPDATE incidents SET status='open', recovery_started_utc=NULL,
            recovery_last_healthy_utc=NULL WHERE status='recovery_pending'""")
        # v3 stored the fault event on each observation, but had no coverage
        # rows. Retain a provenance-only batch for a mapped event; it can never
        # itself verify recovery or establish an epoch.
        for incident in self.db.execute("""SELECT dedup_key,target,source,fault_family FROM incidents
                WHERE status='open'""").fetchall():
            observations = self.db.execute("""SELECT * FROM observations WHERE incident_key=?
                AND source=? AND target=? AND status='unhealthy' AND ordering='current' ORDER BY id DESC""",
                (incident["dedup_key"], incident["source"], incident["target"])).fetchall()
            mapped = None
            for obs in observations:
                try:
                    event = json.loads(obs["evidence_json"])
                except (ValueError, TypeError):
                    continue
                if (not isinstance(event, dict) or isinstance(event.get("code"), bool)
                        or not isinstance(event.get("code"), (str, int))
                        or event.get("fault_family") != incident["fault_family"]):
                    continue
                check, resource = condition(event)
                if (len(check) > 512 or len(resource) > 512 or check.endswith(":unknown")
                        or resource == "host" and event.get("fault_family") in {"xid", "aer"}):
                    continue
                mapped = obs, event, check, resource
                break
            if mapped is None:
                if not observations:
                    continue
                # A retained observation still gets an explicit unknown row.
                # This sentinel cannot be passed by a producer adapter.
                obs = observations[0]
                try:
                    event = json.loads(obs["evidence_json"])
                except (ValueError, TypeError):
                    event = {"legacy_observation_id": obs["id"]}
                check, resource = "unknown:legacy_unmapped", "unknown"
                event_code = "legacy_unmapped"
            else:
                obs, event, check, resource = mapped
                event_code = str(event["code"])
            evidence = canonical_json(event)
            batch_id = self.db.execute("""INSERT INTO observation_batches(
                target,source,delivery_key,epoch,boot_id,source_utc,measured_utc,receipt_utc,
                ordering,freshness,complete,coverage_json,dependencies_json,evidence_sha256,evidence_json)
                VALUES(?,?,NULL,NULL,?,?,?,?,'legacy','unknown',0,'[]','[]',?,?)""", (
                incident["target"], incident["source"], obs["boot_id"], obs["source_utc"],
                obs["source_utc"], obs["receipt_utc"], obs["evidence_sha256"], evidence)).lastrowid
            self.db.execute("""INSERT INTO incident_conditions VALUES(?,?,?,?,?,?,?,?,?)""", (
                incident["dedup_key"], batch_id, obs["id"], incident["source"], event_code,
                check, resource, "unknown", "unknown"))

    def current_epoch(self, target: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM host_epochs WHERE target=? ORDER BY epoch DESC LIMIT 1", (target,)).fetchone()
        return dict(row) if row else None

    def latest_accepted_evidence(self, target: str, source: str) -> dict[str, Any] | None:
        row = self.db.execute("""SELECT * FROM observation_batches WHERE target=? AND source=?
            AND ordering='current' ORDER BY id DESC LIMIT 1""", (target, source)).fetchone()
        return dict(row) if row else None

    def accepted_condition(self, incident_key: str) -> dict[str, Any] | None:
        row = self.db.execute("""SELECT c.*, b.epoch,b.boot_id,b.measured_utc,b.receipt_utc,
            b.ordering, i.status,i.notification_episode FROM incident_conditions c
            JOIN observation_batches b ON b.id=c.batch_id JOIN incidents i ON i.dedup_key=c.incident_key
            WHERE c.incident_key=?""", (incident_key,)).fetchone()
        if not row:
            return None
        value = dict(row)
        epoch = self.current_epoch(self.db.execute("SELECT target FROM incidents WHERE dedup_key=?", (incident_key,)).fetchone()[0])
        value["current_epoch"] = epoch["epoch"] if epoch else None
        value["age_seconds"] = (self.clock() - instant(value["measured_utc"])).total_seconds()
        value["current"] = value["ordering"] == "current" and value["epoch"] == value["current_epoch"]
        value["evidence_freshness"] = value["freshness"]
        if not value["current"]:
            value["freshness"] = "unknown"
        elif value["age_seconds"] > (DERIVED_INPUT_MAX_AGE_SECONDS if self.db.execute(
                "SELECT source FROM observation_batches WHERE id=?", (value["batch_id"],)
                ).fetchone()[0] in {"ssh", "target-probe", "capacity-reconciliation", "market-reconciliation"} else json.loads(self.db.execute(
                "SELECT evidence_json FROM observation_batches WHERE id=?", (value["batch_id"],)
                ).fetchone()[0]).get("_max_source_age", 180)):
            value["freshness"] = "stale"
        elif value["age_seconds"] < -30:
            value["freshness"] = "unknown"
        if value["source"] in {"capacity-reconciliation", "market-reconciliation"}:
            dependencies = json.loads(self.db.execute(
                "SELECT dependencies_json FROM observation_batches WHERE id=?", (value["batch_id"],)
            ).fetchone()[0])
            if not dependencies or any(not dependency_fresh(self.db.execute(
                    "SELECT * FROM observation_batches WHERE id=?", (dependency,)
                    ).fetchone(), self.clock()) for dependency in dependencies):
                value["freshness"] = "unknown"
        return value

    def recovery_events(self, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM recovery_events WHERE id>? ORDER BY id LIMIT ?",
            (max(0, int(after_id)), max(1, min(int(limit), 1000))))]

    def _batch_provenance(self, probe: dict[str, Any], observation: dict[str, Any], dependencies: list[int]) -> dict[str, Any]:
        target, source = observation["target"], observation["source"]
        boot, receipt = observation["boot_id"], observation["receipt_utc"]
        measured = probe.get("measured_at", observation["source_utc"])
        epoch = self.current_epoch(target)
        verified = (source in {"ssh", "target-probe"} and probe.get("boot_verified") is True
                    and boot not in {"unknown", "external", "fixture"}
                    and observation["freshness"] == "fresh")
        ordering = "current"
        changed = False
        retired = self.db.execute("SELECT epoch FROM host_epochs WHERE target=? AND boot_id=? AND retired_utc IS NOT NULL", (target, boot)).fetchone()
        previous = self.latest_accepted_evidence(target, source)
        if retired:
            ordering = "old_epoch"
        elif verified and (epoch is None or epoch["boot_id"] != boot):
            # Collection clock, not target wall clock, orders verified responses.
            latest_host = self.db.execute("""SELECT measured_utc FROM observation_batches
                WHERE target=? AND epoch=? AND source IN ('ssh','target-probe') AND ordering='current'
                ORDER BY id DESC LIMIT 1""", (target, epoch["epoch"] if epoch else None)).fetchone()
            frontier = max((instant(epoch["verified_utc"]), instant(latest_host[0]) if latest_host else instant(epoch["verified_utc"]))) if epoch else None
            if frontier and instant(measured) <= frontier:
                ordering = "out_of_order"
            else:
                number = 1 if epoch is None else epoch["epoch"] + 1
                self.db.execute("UPDATE host_epochs SET retired_utc=? WHERE target=? AND retired_utc IS NULL", (receipt, target))
                self.db.execute("INSERT INTO host_epochs VALUES(?,?,?,?,NULL)", (target, number, boot, measured))
                epoch = self.current_epoch(target)
                changed = True
        elif source in {"ssh", "target-probe"} and epoch and boot not in {epoch["boot_id"], "unknown"}:
            ordering = "unverified_boot"
        # Epoch changes legitimately reset target wall-clock ordering.
        if (ordering == "current" and previous and not changed
                and instant(measured) < instant(previous["measured_utc"])):
            ordering = "out_of_order"
        if (ordering == "current" and previous and not changed and previous["boot_id"] == boot
                and instant(observation["source_utc"]) < instant(previous["source_utc"])):
            ordering = "out_of_order"
        epoch_id = retired["epoch"] if retired else epoch["epoch"] if epoch else None
        if ordering == "unverified_boot":
            epoch_id = None
        if ordering == "current" and epoch and not verified and instant(measured) < instant(epoch["verified_utc"]):
            ordering = "old_epoch"
        fresh = observation["freshness"]
        if source in {"ssh", "target-probe"} and boot == "unknown":
            fresh = "unknown"
        if source in {"capacity-reconciliation", "market-reconciliation"}:
            required = {"ssh", "prometheus"} if source == "capacity-reconciliation" else {"vast", "ssh"}
            rows = [self.db.execute("SELECT * FROM observation_batches WHERE id=?", (item,)).fetchone() for item in dependencies]
            adopted_sources = ["ssh" if row and row["source"] == "target-probe" else row["source"]
                               for row in rows if row]
            if (epoch is None or any(row is None for row in rows)
                    or not required.issubset(adopted_sources)
                    or len(set(adopted_sources)) != len(adopted_sources)):
                fresh = "unknown"
            else:
                measured = min((row["measured_utc"] for row in rows), key=instant)
                previous_deps = json.loads(previous["dependencies_json"]) if previous else []
                old = {row["source"]: row["id"] for item in previous_deps
                       if (row := self.db.execute("SELECT id,source FROM observation_batches WHERE id=?", (item,)).fetchone())}
                if any(row["epoch"] != epoch_id or (epoch and instant(row["measured_utc"]) < instant(epoch["verified_utc"])) for row in rows):
                    ordering = "old_epoch"
                if any(row["id"] <= old.get(row["source"], 0) for row in rows):
                    ordering = "replayed_dependencies"
                if any(row["target"] != target or row["ordering"] != "current"
                       or row["freshness"] != "fresh" or row["epoch"] != epoch_id
                       or (epoch and instant(row["measured_utc"]) < instant(epoch["verified_utc"]))
                       or not dependency_fresh(row, instant(receipt))
                       or row["id"] <= old.get(row["source"], 0)
                       for row in rows):
                    fresh = "unknown"
                if previous and instant(measured) <= instant(previous["measured_utc"]):
                    fresh = "unknown"
            if fresh != "fresh" and ordering == "current":
                ordering = "unknown_dependencies"
        max_age = (DERIVED_INPUT_MAX_AGE_SECONDS if source in {"capacity-reconciliation", "market-reconciliation"}
                   else probe.get("_max_source_age", 180))
        if (instant(receipt) - instant(measured)).total_seconds() > max_age:
            fresh = "stale"
        return dict(epoch=epoch_id, ordering=ordering, measured=measured, freshness=fresh, changed=changed)

    def record_batch(self, probe: dict[str, Any], records: list[dict[str, Any]]) -> tuple[list[Any], int, bool]:
        """Internal Supervisor commit boundary. Inputs must be fully validated first."""
        coverage = validate_coverage(probe.get("coverage", []))
        document = bounded_evidence(probe)
        for row in coverage:
            if not evidence_pointer(document, row["evidence_ref"]):
                raise ValueError("coverage must reference retained measurement evidence")
        dependencies = probe.get("dependencies", [])
        if (not isinstance(dependencies, list) or len(dependencies) > 8
                or any(type(item) is not int or item < 1 for item in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            raise ValueError("dependencies must be up to eight distinct accepted batch IDs")
        # Validate every serialized event before acquiring the write lock or
        # publishing any immutable bundle.
        keys = set()
        for record in records:
            canonical_json(bounded_evidence(record.get("evidence")))
            self._notification_silent(record.get("severity", "warning"), record.get("silent"))
            incident = record.get("incident")
            if incident:
                if incident["dedup_key"] in keys:
                    raise ValueError("duplicate incident within a batch")
                keys.add(incident["dedup_key"])
                check, resource = condition(record["evidence"])
                if len(check) > 512 or len(resource) > 512:
                    raise ValueError("incident condition exceeds coverage bounds")
        # Fault observations take precedence over an erroneous passing coverage
        # claim for that same check/resource in the source envelope.
        by_check = {(row["check"], row["resource"]): row for row in coverage}
        for index, record in enumerate(records):
            if record.get("incident"):
                check, resource = condition(record["evidence"])
                pointer = f"/events/{index}" if index < len(probe.get("events", [])) else "/"
                by_check[(check, resource)] = dict(check=check, resource=resource, result="fail", evidence_ref=pointer)
        coverage = list(by_check.values())
        probe = dict(probe, coverage=coverage)
        observation = records[0]["observation"]
        # Batch identity is the source delivery, never an individual event suffix.
        from .incidents import delivery_identity
        event_id = probe.get("source_event_id", probe.get("delivery_id"))
        delivery = delivery_identity(observation["target"], observation["source"], str(event_id)) if event_id else None
        self.db.execute("BEGIN IMMEDIATE")
        self._in_batch = True
        try:
            duplicate = self.db.execute("SELECT id FROM observation_batches WHERE delivery_key=?", (delivery,)).fetchone() if delivery else None
            if duplicate:
                self.db.rollback()
                return [], int(duplicate[0]), False
            meta = self._batch_provenance(probe, observation, dependencies)
            digest, _ = evidence_digest(document)
            batch_id = self.db.execute("""INSERT INTO observation_batches(
                target,source,delivery_key,epoch,boot_id,source_utc,measured_utc,receipt_utc,
                ordering,freshness,complete,coverage_json,dependencies_json,evidence_sha256,evidence_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                observation["target"], observation["source"], delivery, meta["epoch"], observation["boot_id"],
                observation["source_utc"], meta["measured"], observation["receipt_utc"], meta["ordering"],
                meta["freshness"], int(probe.get("complete") is True), canonical_json(coverage).decode(),
                canonical_json(dependencies).decode(), digest, canonical_json(document))).lastrowid
            results = []
            present = set()
            for record in records:
                obs = record["observation"]
                obs.update(batch_id=batch_id, ordering=meta["ordering"], freshness=meta["freshness"])
                if (observation["source"] in {"ssh", "target-probe"}
                        and observation["boot_id"] == "unknown" and meta["freshness"] == "unknown"
                        and record["evidence"].get("fault_family") in {"gpu", "xid", "aer", "capacity"}):
                    obs["ordering"] = "unverified_boot"
                record.update(interrupt_other_recoveries=False, apply_healthy_recovery=False)
                result = self.record_observation(**record)
                results.append(result)
                incident = record.get("incident")
                if incident:
                    key = incident["dedup_key"]
                    if result.current:
                        present.add(key)
                    if meta["ordering"] == "current" and result.current:
                        check, resource = condition(record["evidence"])
                        self.db.execute("""INSERT INTO incident_conditions VALUES(?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(incident_key) DO UPDATE SET batch_id=excluded.batch_id,
                            observation_id=excluded.observation_id,event_code=excluded.event_code,
                            check_name=excluded.check_name,resource=excluded.resource,
                            condition=excluded.condition,freshness=excluded.freshness""", (
                            key, batch_id, result.observation_id, obs["source"], str(record["evidence"].get("code", "unknown")),
                            check, resource, "fail" if meta["freshness"] == "fresh" else "unknown", meta["freshness"]))
            if meta["ordering"] == "current":
                if meta["changed"]:
                    self._retire_verification(observation["target"], results[-1].observation_id, observation)
                if meta["freshness"] != "fresh" or observation["status"] in {"unknown", "stale"}:
                    self._interrupt_dependency_verification(observation, results[-1].observation_id)
                self._settle_coverage(probe, batch_id, results[-1].observation_id, meta, present, observation)
            self.db.commit()
            return results, int(batch_id), meta["changed"] and meta["epoch"] > 1
        except Exception:
            self.db.rollback()
            raise
        finally:
            self._in_batch = False
            self._active_recovery_batch = None

    def _retire_verification(self, target: str, observation_id: int, observation: dict[str, Any]) -> None:
        for row in self.db.execute("SELECT dedup_key FROM incidents WHERE target=? AND status='recovery_pending'", (target,)).fetchall():
            self._cancel_verification(row[0], observation_id, observation["receipt_utc"], observation["boot_id"])

    def _cancel_verification(self, key: str, observation_id: int, at: str, boot: str,
                             transition: str = "recovery_interrupted") -> None:
        self.db.execute("""UPDATE incidents SET status='open',recovery_started_utc=NULL,
            recovery_last_healthy_utc=NULL,recovery_batch_id=NULL,recovery_epoch=NULL WHERE dedup_key=?""", (key,))
        self.db.execute("UPDATE incident_conditions SET condition='unknown',freshness='unknown' WHERE incident_key=?", (key,))
        self._insert_transition(key, observation_id, transition, at, boot)

    def _interrupt_dependency_verification(self, observation: dict[str, Any], observation_id: int) -> None:
        """A current input outage interrupts the checks that depend on it."""
        rows = self.db.execute("""SELECT i.dedup_key,i.last_boot_id,b.dependencies_json
            FROM incidents i JOIN observation_batches b ON b.id=i.recovery_batch_id
            WHERE i.target=? AND i.status='recovery_pending'
            AND i.source IN ('capacity-reconciliation','market-reconciliation') LIMIT 1000""",
            (observation["target"],)).fetchall()
        for row in rows:
            dependencies = json.loads(row["dependencies_json"])
            if any((dependency := self.db.execute(
                    "SELECT source FROM observation_batches WHERE id=?", (item,)
                    ).fetchone()) and dependency[0] == observation["source"] for item in dependencies):
                self._cancel_verification(row["dedup_key"], observation_id, observation["receipt_utc"], row["last_boot_id"])

    def _settle_coverage(self, probe: dict[str, Any], batch_id: int, observation_id: int,
                         meta: dict[str, Any], present: set[str], observation: dict[str, Any]) -> None:
        rows = self.db.execute("""SELECT i.*,c.check_name,c.resource FROM incidents i
            LEFT JOIN incident_conditions c ON c.incident_key=i.dedup_key
            WHERE i.target=? AND i.source=? AND i.status IN ('open','recovery_pending')""",
            (observation["target"], observation["source"])).fetchall()
        coverage = {(row["check"], row["resource"]): row for row in probe.get("coverage", [])}
        passed = set()
        for row in rows:
            key = row["dedup_key"]
            if key in present:
                continue
            evidence = coverage.get((row["check_name"], row["resource"]))
            eligible = (evidence and evidence["result"] == "pass" and meta["freshness"] == "fresh"
                        and probe.get("recovery_eligible") is not False
                        and (observation["source"] not in {"ssh", "target-probe"}
                             or (meta["epoch"] is not None and probe.get("boot_verified") is True
                                 and observation["boot_id"] == self.current_epoch(observation["target"])["boot_id"])))
            if eligible:
                passed.add(key)
                if row["status"] == "recovery_pending" and row["recovery_epoch"] != meta["epoch"]:
                    self._cancel_verification(key, observation_id, meta["measured"], observation["boot_id"])
                self.db.execute("""UPDATE incident_conditions SET condition='pass',batch_id=?,
                    observation_id=?,freshness=? WHERE incident_key=?""", (batch_id, observation_id, meta["freshness"], key))
            else:
                if row["status"] == "recovery_pending":
                    self._cancel_verification(key, observation_id, meta["measured"], observation["boot_id"])
                self.db.execute("""UPDATE incident_conditions SET condition='unknown',batch_id=?,
                    observation_id=?,freshness=? WHERE incident_key=?""", (batch_id, observation_id, meta["freshness"], key))
        self._active_recovery_batch = batch_id
        self._apply_healthy_observation(observation["target"], observation["source"], meta["measured"],
                                        observation["boot_id"], observation_id,
                                        exclude=frozenset(row["dedup_key"] for row in rows if row["dedup_key"] not in passed))
        for key in passed:
            self.db.execute("""UPDATE incidents SET recovery_epoch=?,recovery_batch_id=?
                WHERE dedup_key=? AND status='recovery_pending'""", (meta["epoch"], batch_id, key))

    def _recovery_event(self, key: str, observation_id: int, started: str, ended: str, episode: int) -> None:
        batch_id = getattr(self, "_active_recovery_batch", None)
        if batch_id is None:
            return
        batch = self.db.execute("SELECT * FROM observation_batches WHERE id=?", (batch_id,)).fetchone()
        transition = self.db.execute("SELECT id FROM transitions WHERE incident_key=? AND observation_id=? AND transition='recovered'", (key, observation_id)).fetchone()[0]
        event_id = f"recovery:{key}:{transition}"
        pre = [row[0] for row in self.db.execute("SELECT id FROM observations WHERE incident_key=? AND ordering='current' ORDER BY id DESC LIMIT 4", (key,))]
        post = [row[0] for row in self.db.execute("""SELECT id FROM observation_batches WHERE target=? AND source=?
            AND ordering='current' AND julianday(measured_utc)>=julianday(?) AND id<=? ORDER BY id DESC LIMIT 8""", (batch["target"], batch["source"], started, batch_id))]
        first = self.db.execute("""SELECT id FROM observation_batches WHERE target=? AND source=?
            AND ordering='current' AND measured_utc=? ORDER BY id LIMIT 1""", (batch["target"], batch["source"], started)).fetchone()
        if first and first[0] not in post:
            post = post[:7] + [first[0]]
        check = self.db.execute("SELECT check_name,resource FROM incident_conditions WHERE incident_key=?", (key,)).fetchone()
        coverage = [row for row in json.loads(batch["coverage_json"]) if (row["check"], row["resource"]) == tuple(check)]
        verified = self.db.execute("SELECT boot_id FROM host_epochs WHERE target=? AND epoch=?", (batch["target"], batch["epoch"])).fetchone()
        payload = dict(event_id=event_id, transition_id=transition, incident_key=key, notification_episode=episode,
                       epoch=batch["epoch"], boot_id=verified[0] if verified else None,
                       verification_interval=[started, ended], pre_observations=pre, post_batches=post,
                       coverage=coverage, remaining_uncertainty=["sampled verification; no claim between samples"])
        if batch["epoch"] is None:
            payload["remaining_uncertainty"].append("host boot not verified")
        event = self.db.execute("""INSERT INTO recovery_events(event_id,incident_key,episode,transition_id,
            epoch,boot_id,started_utc,ended_utc,payload_json) VALUES(?,?,?,?,?,?,?,?,?)""", (
            event_id, key, episode, transition, batch["epoch"], payload["boot_id"], started, ended, canonical_json(payload).decode())).lastrowid
        # Preserve dependency closure as well as bounded presentation references.
        retained = set(post)
        for item in post:
            retained.update(json.loads(self.db.execute("SELECT dependencies_json FROM observation_batches WHERE id=?", (item,)).fetchone()[0]))
        for item in retained:
            self.db.execute("INSERT INTO recovery_evidence VALUES(?,?,NULL)", (event, item))
        for item in pre:
            self.db.execute("INSERT INTO recovery_evidence VALUES(?,NULL,?)", (event, item))

    def expire_verifications(self) -> int:
        """Bound silence by the configured source gap; call every daemon tick/read."""
        from .state import utc_text
        now = self.clock()
        count = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute("SELECT * FROM incidents WHERE status='recovery_pending' ORDER BY recovery_last_healthy_utc LIMIT 1000").fetchall()
            for row in rows:
                last = row["recovery_last_healthy_utc"]
                if last and (now - instant(last)).total_seconds() > self._healthy_gap_for(row["source"]):
                    evidence = self.db.execute("SELECT id FROM observations WHERE batch_id=? ORDER BY id DESC LIMIT 1", (row["recovery_batch_id"],)).fetchone()
                    if evidence:
                        self._cancel_verification(row["dedup_key"], evidence[0], utc_text(now), row["last_boot_id"], "recovery_expired")
                        count += 1
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return count
