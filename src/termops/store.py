from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from .models import (
    ALLOWED_TASK_TRANSITIONS,
    ActionProposal,
    ActionStatus,
    ActionStep,
    EnvSnapshot,
    Finding,
    MapePhase,
    Observation,
    RiskLevel,
    Severity,
    TargetRef,
    Task,
    TaskKind,
    TaskStatus,
    utc_now,
)
from .security import action_digest, canonical_json, redact, sha256_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'monitor',
    target_json TEXT NOT NULL, status TEXT NOT NULL,
    input_json TEXT NOT NULL, report_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
    status TEXT NOT NULL, summary TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, code TEXT NOT NULL, severity TEXT NOT NULL,
    confidence REAL NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
    remediation TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL, risk TEXT NOT NULL,
    payload_json TEXT NOT NULL, steps_json TEXT NOT NULL, preconditions_json TEXT NOT NULL,
    verification_json TEXT NOT NULL, rollback_json TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL,
    expires_at TEXT NOT NULL, result_json TEXT, decision_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, event_type TEXT NOT NULL, data_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL, event_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    content_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_codes (
    code_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL, used_at TEXT
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_observations_task ON observations(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_findings_task ON findings(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_actions_task ON actions(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_knowledge_task ON knowledge(task_id, created_at);
"""

# Applied only when the SQLite build supports FTS5.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(title, content, content='knowledge', content_rowid='rowid');
"""


def _escape_fts5(text: str) -> str:
    """Escape FTS5 special syntax and wrap terms for safe MATCH queries."""
    # Remove or replace FTS5 operators: * " ( ) AND OR NOT NEAR
    cleaned = text.translate(str.maketrans("", "", '*"()'))
    # Collapse whitespace and take first few meaningful tokens.
    tokens = cleaned.split()
    if not tokens:
        return '""'
    # Quote each token so FTS5 treats them as literal phrases.
    return " ".join(f'"{token}"' for token in tokens[:10])


def _escape_fts5_term(term: str) -> str:
    """Escape a single term for FTS5 MATCH."""
    return term.translate(str.maketrans("", "", '*"()'))


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._fts_enabled = False
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(SCHEMA)
            self._fts_enabled = self._probe_fts5()
            if self._fts_enabled:
                self._connection.executescript(FTS_SCHEMA)

    def _probe_fts5(self) -> bool:
        try:
            self._connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
            self._connection.execute("DROP TABLE IF EXISTS _fts_probe")
            return True
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def append_event(
        self, event_type: str, data: dict[str, Any], task_id: str | None = None
    ) -> dict[str, Any]:
        created_at = utc_now().isoformat()
        safe_data = redact(data)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            previous = str(row["event_hash"]) if row else "0" * 64
            event_hash = sha256_text(
                previous
                + canonical_json(
                    {
                        "task_id": task_id,
                        "event_type": event_type,
                        "data": safe_data,
                        "created_at": created_at,
                    }
                )
            )
            cursor = self._connection.execute(
                "INSERT INTO events(task_id,event_type,data_json,prev_hash,event_hash,created_at) VALUES(?,?,?,?,?,?)",
                (task_id, event_type, canonical_json(safe_data), previous, event_hash, created_at),
            )
            return {"seq": cursor.lastrowid, "event_hash": event_hash, "created_at": created_at}

    def add_knowledge(self, task_id: str, kind: str, title: str, content: dict[str, Any]) -> dict[str, Any]:
        entry_id = uuid.uuid4().hex
        created_at = utc_now().isoformat()
        rowid = 0
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO knowledge VALUES(?,?,?,?,?,?)",
                (entry_id, task_id, kind, title, canonical_json(redact(content)), created_at),
            )
            rowid = cursor.lastrowid or 0
        if self._fts_enabled and rowid:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT INTO knowledge_fts(rowid, title, content) VALUES(?,?,?)",
                    (rowid, title, canonical_json(redact(content))),
                )
        self.append_event("knowledge.recorded", {"id": entry_id, "kind": kind, "title": title}, task_id)
        return {"id": entry_id, "task_id": task_id, "kind": kind, "title": title, "created_at": created_at}

    def search_knowledge(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Full-text search over recorded knowledge entries."""
        if not self._fts_enabled or not query.strip():
            return []
        # Escape FTS5 special characters and wrap each term in double quotes.
        escaped = _escape_fts5(query.strip())
        with self._lock:
            rows = self._connection.execute(
                "SELECT k.id, k.task_id, k.kind, k.title, k.content_json, k.created_at "
                "FROM knowledge_fts fts JOIN knowledge k ON k.rowid = fts.rowid "
                "WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
                (escaped, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "title": row["title"],
                "content": json.loads(row["content_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_knowledge(self, task_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if task_id:
            rows = self._connection.execute(
                "SELECT * FROM knowledge WHERE task_id=? ORDER BY created_at LIMIT ?", (task_id, limit)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM knowledge ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "title": row["title"],
                "content": json.loads(row["content_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def knowledge_stats(self) -> dict[str, Any]:
        """Return summary statistics about the knowledge base."""
        with self._lock:
            total = self._connection.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge"
            ).fetchone()["cnt"]
            by_kind = self._connection.execute(
                "SELECT kind, COUNT(*) AS cnt FROM knowledge GROUP BY kind ORDER BY cnt DESC"
            ).fetchall()
            recent = self._connection.execute(
                "SELECT kind, title, created_at FROM knowledge ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
        return {
            "total": total,
            "by_kind": [{"kind": r["kind"], "count": r["cnt"]} for r in by_kind],
            "fts_enabled": self._fts_enabled,
            "recent": [{"kind": r["kind"], "title": r["title"], "created_at": r["created_at"]} for r in recent],
        }

    def related_knowledge(self, error_codes: list[str], limit: int = 3) -> list[dict[str, Any]]:
        """Find knowledge entries related to given error codes using FTS5."""
        if not self._fts_enabled or not error_codes:
            return []
        query = " OR ".join(f'"{_escape_fts5_term(c)}"' for c in error_codes if c)
        if not query:
            return []
        try:
            with self._lock:
                rows = self._connection.execute(
                    "SELECT k.id, k.task_id, k.kind, k.title, k.content_json, k.created_at "
                    "FROM knowledge_fts fts JOIN knowledge k ON k.rowid = fts.rowid "
                    "WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
        except Exception:
            return []
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "title": row["title"],
                "content": json.loads(row["content_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def verify_event_chain(self) -> bool:
        previous = "0" * 64
        rows = self._connection.execute("SELECT * FROM events ORDER BY seq").fetchall()
        for row in rows:
            expected = sha256_text(
                previous
                + canonical_json(
                    {
                        "task_id": row["task_id"],
                        "event_type": row["event_type"],
                        "data": json.loads(row["data_json"]),
                        "created_at": row["created_at"],
                    }
                )
            )
            if row["prev_hash"] != previous or row["event_hash"] != expected:
                return False
            previous = row["event_hash"]
        return True

    def create_task(self, kind: TaskKind, target: TargetRef, input_data: dict[str, Any]) -> Task:
        task_id = uuid.uuid4().hex
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    kind.value,
                    MapePhase.MONITOR.value,
                    canonical_json(target.model_dump(mode="json")),
                    TaskStatus.QUEUED.value,
                    canonical_json(redact(input_data)),
                    None,
                    None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        self.append_event("task.created", {"kind": kind.value, "target": target.model_dump()}, task_id)
        return self.get_task(task_id)

    def update_task_phase(self, task_id: str, phase: MapePhase) -> Task:
        with self._lock, self._connection:
            self._connection.execute("UPDATE tasks SET phase=? WHERE id=?", (phase.value, task_id))
        self.append_event("task.phase", {"phase": phase.value}, task_id)
        return self.get_task(task_id)

    def update_task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        phase: MapePhase | None = None,
        report: dict[str, Any] | None = None,
        error: str | None = None,
        force: bool = False,
    ) -> Task:
        current = self.get_task(task_id)
        if not force and status != current.status and status not in ALLOWED_TASK_TRANSITIONS[current.status]:
            raise ValueError(f"invalid task transition: {current.status.value} -> {status.value}")
        now = utc_now().isoformat()
        report_json = canonical_json(redact(report)) if report is not None else None
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE tasks SET status=?, phase=COALESCE(?,phase), report_json=COALESCE(?,report_json), error=?, updated_at=? WHERE id=?",
                (status.value, phase.value if phase else None, report_json, error, now, task_id),
            )
        self.append_event(
            "task.status", {"from": current.status.value, "to": status.value, "error": error}, task_id
        )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task:
        row = self._connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return Task(
            id=row["id"],
            kind=row["kind"],
            phase=row["phase"],
            target=json.loads(row["target_json"]),
            status=row["status"],
            input=json.loads(row["input_json"]),
            report=json.loads(row["report_json"]) if row["report_json"] else None,
            error=row["error"],
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
        )

    def list_tasks(self, limit: int = 100, statuses: set[TaskStatus] | None = None) -> list[Task]:
        params: list[Any] = []
        query = "SELECT id FROM tasks"
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(status.value for status in statuses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self.get_task(row["id"]) for row in self._connection.execute(query, params).fetchall()]

    def add_observation(
        self,
        task_id: str,
        kind: str,
        source: str,
        status: Literal["ok", "warning", "error", "unavailable"],
        summary: str,
        data: dict[str, Any],
    ) -> Observation:
        observation = Observation(
            id=uuid.uuid4().hex,
            task_id=task_id,
            kind=kind,
            source=source,
            status=status,
            summary=summary,
            data=redact(data),
            created_at=utc_now(),
        )
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)",
                (
                    observation.id,
                    task_id,
                    kind,
                    source,
                    status,
                    summary,
                    canonical_json(observation.data),
                    observation.created_at.isoformat(),
                ),
            )
        self.append_event(
            "observation.recorded", {"id": observation.id, "kind": kind, "status": status}, task_id
        )
        return observation

    def list_observations(self, task_id: str) -> list[Observation]:
        rows = self._connection.execute(
            "SELECT * FROM observations WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
        return [
            Observation(
                id=row["id"],
                task_id=row["task_id"],
                kind=row["kind"],
                source=row["source"],
                status=row["status"],
                summary=row["summary"],
                data=json.loads(row["data_json"]),
                created_at=parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def add_finding(
        self,
        task_id: str,
        code: str,
        severity: Severity,
        confidence: float,
        title: str,
        detail: str,
        evidence_ids: list[str],
        remediation: str | None = None,
    ) -> Finding:
        finding = Finding(
            id=uuid.uuid4().hex,
            task_id=task_id,
            code=code,
            severity=severity,
            confidence=confidence,
            title=title,
            detail=detail,
            evidence_ids=evidence_ids,
            remediation=remediation,
            created_at=utc_now(),
        )
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    finding.id,
                    task_id,
                    code,
                    severity.value,
                    confidence,
                    title,
                    detail,
                    canonical_json(evidence_ids),
                    remediation,
                    finding.created_at.isoformat(),
                ),
            )
        self.append_event(
            "finding.created", {"id": finding.id, "code": code, "severity": severity.value}, task_id
        )
        return finding

    def list_findings(self, task_id: str | None = None, limit: int = 100) -> list[Finding]:
        if task_id:
            rows = self._connection.execute(
                "SELECT * FROM findings WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM findings ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Finding(
                id=row["id"],
                task_id=row["task_id"],
                code=row["code"],
                severity=row["severity"],
                confidence=row["confidence"],
                title=row["title"],
                detail=row["detail"],
                evidence_ids=json.loads(row["evidence_ids_json"]),
                remediation=row["remediation"],
                created_at=parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def create_action(
        self,
        task_id: str,
        kind: str,
        target: str,
        risk: RiskLevel,
        payload: dict[str, Any],
        steps: list[ActionStep],
        preconditions: list[str],
        verification: list[str],
        rollback_plan: list[str],
        ttl_seconds: int,
    ) -> ActionProposal:
        action_id = uuid.uuid4().hex
        created_at = utc_now()
        expires_at = created_at + timedelta(seconds=ttl_seconds)
        safe_payload = redact(payload)
        digest_payload = {
            "id": action_id,
            "task_id": task_id,
            "kind": kind,
            "target": target,
            "risk": risk.value,
            "payload": safe_payload,
            "steps": [step.model_dump() for step in steps],
            "preconditions": preconditions,
            "verification": verification,
            "rollback_plan": rollback_plan,
            "expires_at": expires_at.isoformat(),
        }
        digest = action_digest(digest_payload)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    task_id,
                    kind,
                    target,
                    risk.value,
                    canonical_json(safe_payload),
                    canonical_json([step.model_dump() for step in steps]),
                    canonical_json(preconditions),
                    canonical_json(verification),
                    canonical_json(rollback_plan),
                    digest,
                    ActionStatus.PENDING.value,
                    expires_at.isoformat(),
                    None,
                    None,
                    created_at.isoformat(),
                ),
            )
        self.append_event(
            "action.proposed", {"id": action_id, "kind": kind, "risk": risk.value, "digest": digest}, task_id
        )
        return self.get_action(action_id)

    def get_action(self, action_id: str) -> ActionProposal:
        row = self._connection.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise KeyError(f"action not found: {action_id}")
        return ActionProposal(
            id=row["id"],
            task_id=row["task_id"],
            kind=row["kind"],
            target=row["target"],
            risk=row["risk"],
            payload=json.loads(row["payload_json"]),
            steps=[ActionStep(**item) for item in json.loads(row["steps_json"])],
            preconditions=json.loads(row["preconditions_json"]),
            verification=json.loads(row["verification_json"]),
            rollback_plan=json.loads(row["rollback_json"]),
            digest=row["digest"],
            status=row["status"],
            expires_at=parse_dt(row["expires_at"]),
            created_at=parse_dt(row["created_at"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
        )

    def list_actions(self, task_id: str) -> list[ActionProposal]:
        rows = self._connection.execute(
            "SELECT id FROM actions WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
        return [self.get_action(row["id"]) for row in rows]

    def decide_action(self, action_id: str, decision: str, supplied_digest: str) -> ActionProposal:
        action = self.get_action(action_id)
        if action.status != ActionStatus.PENDING:
            raise ValueError(f"action is not pending: {action.status.value}")
        if action.expires_at <= datetime.now(timezone.utc):
            with self._connection:
                self._connection.execute(
                    "UPDATE actions SET status=? WHERE id=?", (ActionStatus.EXPIRED.value, action_id)
                )
            raise ValueError("action approval has expired")
        if supplied_digest != action.digest:
            raise ValueError("action digest mismatch")
        status = ActionStatus.APPROVED if decision == "approve" else ActionStatus.REJECTED
        now = utc_now().isoformat()
        with self._lock, self._connection:
            updated = self._connection.execute(
                "UPDATE actions SET status=?, decision_at=? WHERE id=? AND status=?",
                (status.value, now, action_id, ActionStatus.PENDING.value),
            )
            if updated.rowcount != 1:
                raise ValueError("action decision was already consumed")
        self.append_event(
            "action.decided",
            {"id": action_id, "decision": decision, "digest": supplied_digest},
            action.task_id,
        )
        return self.get_action(action_id)

    def update_action(
        self, action_id: str, status: ActionStatus, result: dict[str, Any] | None = None
    ) -> ActionProposal:
        action = self.get_action(action_id)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE actions SET status=?, result_json=COALESCE(?,result_json) WHERE id=?",
                (status.value, canonical_json(redact(result)) if result is not None else None, action_id),
            )
        self.append_event(
            "action.status",
            {"id": action_id, "from": action.status.value, "to": status.value},
            action.task_id,
        )
        return self.get_action(action_id)

    def list_events(
        self, task_id: str | None = None, after_seq: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        if task_id:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                (task_id, after_seq, limit),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE seq>? ORDER BY seq DESC LIMIT ?", (after_seq, limit)
            ).fetchall()
        return [{**dict(row), "data": json.loads(row["data_json"])} for row in rows]

    def create_login_code(self, code: str, ttl_seconds: int = 120) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO login_codes(code_hash,expires_at,used_at) VALUES(?,?,NULL)",
                (sha256_text(code), (utc_now() + timedelta(seconds=ttl_seconds)).isoformat()),
            )

    def consume_login_code(self, code: str) -> bool:
        code_hash = sha256_text(code)
        row = self._connection.execute("SELECT * FROM login_codes WHERE code_hash=?", (code_hash,)).fetchone()
        if row is None or row["used_at"] or parse_dt(row["expires_at"]) <= datetime.now(timezone.utc):
            return False
        with self._connection:
            result = self._connection.execute(
                "UPDATE login_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL",
                (utc_now().isoformat(), code_hash),
            )
        return result.rowcount == 1

    def create_web_session(self, token: str, csrf_token: str, ttl_seconds: int) -> None:
        now = utc_now()
        with self._connection:
            self._connection.execute(
                "INSERT INTO web_sessions(token_hash,csrf_token,expires_at,created_at) VALUES(?,?,?,?)",
                (
                    sha256_text(token),
                    csrf_token,
                    (now + timedelta(seconds=ttl_seconds)).isoformat(),
                    now.isoformat(),
                ),
            )

    def get_web_session(self, token: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM web_sessions WHERE token_hash=?", (sha256_text(token),)
        ).fetchone()
        if row is None or parse_dt(row["expires_at"]) <= datetime.now(timezone.utc):
            return None
        return dict(row)