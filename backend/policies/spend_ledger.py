"""Durable physical-send token reservations, above workflow send-count admission.

The original root/identity/pool scopes and lifecycle remain; pool identifies an
independent provider channel rather than an account-rotation pool. A missing
terminal usage frame moves the full reservation to unresolved spend.
"""

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from backend.platform.files import instance_lock

from .spend_policy import current_policy

KINDS = ("root", "identity", "pool")
MAX_SCOPES = 4096
MAX_SENDS = 16384
MAX_COUNT = (1 << 53) - 1


class SpendDenied(ValueError):
    status = 429

    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason, self.detail = reason, detail

    @property
    def code(self):
        return {
            "duplicate-send-id": "workflow_send_replayed",
            "reserve-not-durable": "workflow_spend_undurable",
            "journal-corrupt": "workflow_spend_undurable",
            "tracking-capacity-exhausted": "workflow_tracking_exhausted",
        }.get(self.reason, "workflow_spend_exhausted")


@dataclass(frozen=True)
class SpendReservation:
    send_id: str
    tokens: int
    durable: bool
    owner: str


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise ValueError("Spend token counts must be nonnegative safe integers.")
    return value


class SpendLedger:
    def __init__(self, directory: Path, config_loader, *, clock=time.time):
        self.path = directory / "spend-ledger.sqlite3"
        self.loader, self.clock = config_loader, clock
        self.lock = RLock()
        self.owner = secrets.token_hex(16)
        self._proofs = {}
        self.closed, self.durable, self.corrupt = False, True, False
        self.persist_failures = 0
        self.corrupt_records = 0
        self._lease = instance_lock(directory, "spend-ledger-owner.lock")
        self._lease.__enter__()
        self.db = None
        try:
            self._initialize()
        except BaseException:
            try:
                if self.db is not None:
                    self.db.close()
            finally:
                self.closed = True
                self._lease.__exit__(None, None, None)
            raise

    def _initialize(self):
        try:
            self.db = self._connect(self.path)
            self._schema(self.db)
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Ledger integrity check failed")
        except sqlite3.Error:
            self.persist_failures = min(1_000_000, self.persist_failures + 1)
            if self.db is not None:
                self.db.close()
            self.corrupt = self.path.exists() and self.path.stat().st_size > 0
            self.corrupt_records += int(self.corrupt)
            self.durable = False
            self.db = self._connect(":memory:")
            self._schema(self.db)
        salt = self.db.execute("SELECT value FROM metadata WHERE key='salt'").fetchone()
        try:
            self.salt = bytes.fromhex(salt[0]) if salt else secrets.token_bytes(32)
            if len(self.salt) != 32:
                raise ValueError()
        except ValueError, TypeError:
            self.corrupt = True
            self.corrupt_records += 1
            self.salt = secrets.token_bytes(32)
        bad_records = self.db.execute("""
            SELECT COUNT(*) FROM (SELECT 1 FROM scopes WHERE kind NOT IN ('root','identity','pool')
              OR typeof(settled)!='integer' OR typeof(reserved)!='integer'
              OR typeof(unresolved)!='integer' OR settled<0 OR reserved<0 OR unresolved<0
            UNION ALL SELECT 1 FROM sends WHERE typeof(tokens)!='integer' OR tokens<0
              OR status NOT IN ('open','dispatched','settled','lost','abandoned'))
        """).fetchone()[0]
        if bad_records:
            self.corrupt_records = min(1_000_000, self.corrupt_records + bad_records)
            self.corrupt = True
        if self.corrupt and self.durable:
            # Preserve unprovable history on disk; observe new traffic separately.
            # Every configured ceiling remains blocked until that history is repaired.
            self.db.close()
            self.db = self._connect(":memory:")
            self._schema(self.db)
            self.durable = False
            salt = None
            self.salt = secrets.token_bytes(32)
        if not salt:
            self._write(
                lambda db: db.execute("INSERT INTO metadata VALUES ('salt',?)", (self.salt.hex(),))
            )

        # Neither open nor dispatched has a live owner after restart. Both may have
        # reached upstream before the last disk write; never refund them.
        def recover(db):
            for (send,) in db.execute(
                "SELECT id FROM sends WHERE status IN ('open','dispatched')"
            ).fetchall():
                self._resolve(db, send, "lost", None)

        self._write(recover)

    @staticmethod
    def _connect(path):
        db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _schema(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scopes(kind TEXT NOT NULL,id TEXT NOT NULL,
                settled INTEGER NOT NULL DEFAULT 0,reserved INTEGER NOT NULL DEFAULT 0,
                unresolved INTEGER NOT NULL DEFAULT 0,seen REAL NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS sends(id TEXT PRIMARY KEY,tokens INTEGER NOT NULL,
                status TEXT NOT NULL,at REAL NOT NULL,resolved REAL);
            CREATE TABLE IF NOT EXISTS targets(send TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,
                PRIMARY KEY(send,kind,id),FOREIGN KEY(send) REFERENCES sends(id) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS target_scope ON targets(kind,id);
        """)

    def _alias(self, kind, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Spend identifiers must be nonempty strings.")
        return hmac.new(self.salt, f"{kind}\0{value}".encode(), hashlib.sha256).hexdigest()

    def _assert_open(self):
        if self.closed:
            raise RuntimeError("Spend ledger owner was released.")

    def _flush(self):
        if self.durable or self.corrupt:
            return self.durable
        try:
            disk = self._connect(self.path)
            try:
                self.db.backup(disk)
            finally:
                disk.close()
            memory = self.db
            self.db = self._connect(self.path)
            memory.close()
            self.durable = True
        except sqlite3.Error:
            self.persist_failures = min(1_000_000, self.persist_failures + 1)
            return False
        return True

    def _write(self, operation, *, enforced=False):
        self._assert_open()
        self._flush()
        if enforced and (self.corrupt or not self.durable):
            raise SpendDenied("journal-corrupt" if self.corrupt else "reserve-not-durable")
        try:
            with self.db:
                return operation(self.db)
        except sqlite3.Error as exc:
            self.persist_failures = min(1_000_000, self.persist_failures + 1)
            if enforced:
                raise SpendDenied("reserve-not-durable") from exc
            # Retain actual accounting after an I/O failure. The next operation
            # attempts to flush all debt before any enforced admission can pass.
            if self.durable:
                memory = self._connect(":memory:")
                self.db.backup(memory)
                self.db.close()
                self.db, self.durable = memory, False
            with self.db:
                return operation(self.db)

    def _cleanup(self, db, policy, *, force=False):
        cutoff = self.clock() - policy.retentionDays * 86400
        candidates = []
        for kind, identity, settled, reserved, unresolved, seen in db.execute(
            "SELECT * FROM scopes"
        ):
            limit = getattr(policy, kind).maxTokens
            live = db.execute(
                "SELECT 1 FROM targets t JOIN sends s ON s.id=t.send WHERE t.kind=? AND t.id=? AND s.status IN ('open','dispatched') LIMIT 1",
                (kind, identity),
            ).fetchone()
            if live or reserved or (limit is not None and settled + unresolved >= limit):
                continue
            if force or seen < cutoff:
                candidates.append((seen, kind, identity))
        for _, kind, identity in sorted(candidates)[: 1 if force else None]:
            db.execute("DELETE FROM scopes WHERE kind=? AND id=?", (kind, identity))
        # Forget only closed send IDs old enough to be safely fully forgotten;
        # scopes keep their settled/unresolved totals independently of send IDs.
        expired = db.execute(
            "SELECT id FROM sends WHERE status NOT IN ('open','dispatched') AND resolved < ?",
            (cutoff,),
        ).fetchall()
        db.execute(
            "DELETE FROM sends WHERE status NOT IN ('open','dispatched') AND resolved < ?",
            (cutoff,),
        )
        for (identity,) in expired:
            self._proofs.pop(identity, None)

    def reserve(self, send_id, scopes, input_tokens, output_ceiling, *, admission_policy=None):
        tokens = _count(input_tokens) + _count(output_ceiling)
        _count(tokens)
        with self.lock:
            policy = admission_policy or current_policy(self.loader)
            maintenance_policy = current_policy(self.loader)
            refs = [
                (kind, self._alias(kind, scopes[kind + "Id"]))
                for kind in KINDS
                if scopes.get(kind + "Id")
            ]
            send = self._alias("send", send_id)
            enforced = any(getattr(policy, kind).maxTokens is not None for kind, _ in refs)

            def book(db):
                if db.execute("SELECT 1 FROM sends WHERE id=?", (send,)).fetchone():
                    raise SpendDenied("duplicate-send-id")
                self._cleanup(db, maintenance_policy)
                missing = [
                    (k, i)
                    for k, i in refs
                    if not db.execute(
                        "SELECT 1 FROM scopes WHERE kind=? AND id=?", (k, i)
                    ).fetchone()
                ]
                while (
                    db.execute("SELECT count(*) FROM scopes").fetchone()[0] + len(missing)
                    > MAX_SCOPES
                ):
                    before = db.total_changes
                    self._cleanup(db, maintenance_policy, force=True)
                    if before == db.total_changes:
                        raise SpendDenied("tracking-capacity-exhausted")
                    missing = [
                        (k, i)
                        for k, i in refs
                        if not db.execute(
                            "SELECT 1 FROM scopes WHERE kind=? AND id=?", (k, i)
                        ).fetchone()
                    ]
                while db.execute("SELECT count(*) FROM sends").fetchone()[0] >= MAX_SENDS:
                    oldest = db.execute(
                        "SELECT id FROM sends WHERE status NOT IN ('open','dispatched') ORDER BY resolved,id LIMIT 1"
                    ).fetchone()
                    if not oldest:
                        raise SpendDenied("tracking-capacity-exhausted")
                    db.execute("DELETE FROM sends WHERE id=?", oldest)
                    self._proofs.pop(oldest[0], None)
                for kind, identity in refs:
                    row = db.execute(
                        "SELECT settled+reserved+unresolved FROM scopes WHERE kind=? AND id=?",
                        (kind, identity),
                    ).fetchone()
                    projected = (row[0] if row else 0) + tokens
                    limit = getattr(policy, kind).maxTokens
                    if limit is not None and projected > limit:
                        raise SpendDenied(
                            "spend-limit-exceeded", scope=kind, limit=limit, projected=projected
                        )
                now = self.clock()
                db.execute("INSERT INTO sends VALUES (?,?, 'open',?,NULL)", (send, tokens, now))
                for kind, identity in refs:
                    db.execute(
                        "INSERT INTO scopes(kind,id,seen) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                        (kind, identity, now),
                    )
                    db.execute(
                        "UPDATE scopes SET reserved=reserved+?,seen=? WHERE kind=? AND id=?",
                        (tokens, now, kind, identity),
                    )
                    db.execute("INSERT INTO targets VALUES(?,?,?)", (send, kind, identity))

            self._write(book, enforced=enforced)
            proof = SpendReservation(send_id, tokens, self.durable, self.owner)
            self._proofs[send] = proof
            return proof

    def _proof(self, proof):
        self._assert_open()
        if not isinstance(proof, SpendReservation) or proof.owner != self.owner:
            raise ValueError("Spend reservation belongs to a different ledger owner.")
        send = self._alias("send", proof.send_id)
        if self._proofs.get(send) is not proof:
            raise ValueError("Spend reservation was retired or replaced.")
        return send

    def forget_resolved(self, proof):
        """Release a completed request's send identity, never its booked scope totals."""
        with self.lock:
            send = self._proof(proof)

            def forget(db):
                return (
                    db.execute(
                        "DELETE FROM sends WHERE id=? AND status NOT IN ('open','dispatched')",
                        (send,),
                    ).rowcount
                    == 1
                )

            removed = self._write(forget)
            if removed:
                self._proofs.pop(send, None)
            return removed

    def mark_dispatched(self, proof):
        with self.lock:
            send = self._proof(proof)

            def dispatch(db):
                return (
                    db.execute(
                        "UPDATE sends SET status='dispatched' WHERE id=? AND status='open'", (send,)
                    ).rowcount
                    == 1
                )

            return self._write(dispatch)

    def _resolve(self, db, send, status, actual):
        row = db.execute("SELECT tokens,status FROM sends WHERE id=?", (send,)).fetchone()
        if not row or row[1] not in {"open", "dispatched"}:
            return False
        if status == "abandoned" and row[1] != "open":
            return False
        now = self.clock()
        for kind, identity in db.execute(
            "SELECT kind,id FROM targets WHERE send=?", (send,)
        ).fetchall():
            db.execute(
                "UPDATE scopes SET reserved=reserved-?, settled=settled+?, unresolved=unresolved+?,seen=? WHERE kind=? AND id=?",
                (
                    row[0],
                    actual if status == "settled" else 0,
                    row[0] if status == "lost" else 0,
                    now,
                    kind,
                    identity,
                ),
            )
        db.execute("UPDATE sends SET status=?,resolved=? WHERE id=?", (status, now, send))
        return True

    def settle(self, proof, usage):
        if not isinstance(usage, dict) or not {"inputTokens", "outputTokens"} & usage.keys():
            return self.mark_lost(proof)
        actual = _count(usage.get("inputTokens", 0)) + _count(usage.get("outputTokens", 0))
        return self._finish(proof, "settled", _count(actual))

    def mark_lost(self, proof):
        return self._finish(proof, "lost", None)

    def abandon(self, proof):
        return self._finish(proof, "abandoned", None)

    def _finish(self, proof, status, actual):
        with self.lock:
            send = self._proof(proof)
            return self._write(lambda db: self._resolve(db, send, status, actual))

    def diagnostics(self):
        policy = current_policy(self.loader)
        return {
            "ownership": "unheld" if self.closed else "held",
            "initialized": True,
            "configured": any(getattr(policy, kind).maxTokens is not None for kind in KINDS),
            "degraded": not self.durable or self.corrupt,
            "persistFailures": self.persist_failures,
            "corruptRecords": self.corrupt_records,
        }

    def snapshot(self):
        with self.lock:
            self._assert_open()
            return {
                "durable": self.durable,
                "corrupt": self.corrupt,
                "scopes": [
                    dict(
                        zip(
                            ("scope", "id", "settled", "reserved", "unresolved", "lastSeenAt"),
                            row,
                            strict=True,
                        )
                    )
                    for row in self.db.execute("SELECT * FROM scopes ORDER BY seen DESC")
                ],
                "sends": self.db.execute("SELECT count(*) FROM sends").fetchone()[0],
            }

    def close(self):
        with self.lock:
            if not self.closed:
                self._flush()
                self.db.close()
                self.closed = True
                self._lease.__exit__(None, None, None)
