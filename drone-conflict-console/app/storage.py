"""SQLite persistence for imported scenarios and saved plans.

A *scenario* keeps the exact imported JSON (input snapshot) together with the
safety parameters used for the most recent solve, so saved plans can always be
reopened and compared against their original input.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Serializes writes inside one process; cross-process arbitration is handled
# by SQLite itself (WAL + a ``BEGIN IMMEDIATE`` write transaction).
_LOCK = threading.Lock()

#: How long SQLite waits on another process' write lock before raising
#: "database is locked"; the submit path additionally retries as a safety net.
_BUSY_TIMEOUT_MS = 5000
_BUSY_ATTEMPTS = 60
_BUSY_WAIT_SECONDS = 0.05

#: Two stored-plan threshold confirmations within this distance count as equal.
_THRESHOLD_TOL = 1e-9


class BatchRejected(Exception):
    """A batch-creation request refused against the AUTHORITATIVE scenario row.

    Raised inside the same ``BEGIN IMMEDIATE`` transaction that would insert
    the batch, so the refusal is decided on the committed state visible at
    write time (never on a snapshot taken before the transaction).  ``status_code``
    is the HTTP status the route must return and ``fields`` are merged into
    the response ``detail`` object; raising rolls the transaction back, so a
    refusal never leaves a batch behind.
    """

    def __init__(self, status_code: int, reason: str, fields: dict | None = None):
        self.status_code = status_code
        self.reason = reason
        self.fields = fields or {}
        super().__init__(f"batch creation rejected: {reason}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT '',
    input_json      TEXT NOT NULL,
    safety_radius   REAL NOT NULL,
    safety_height   REAL NOT NULL,
    created_at      REAL NOT NULL,
    solved_at       REAL,
    result_json     TEXT,
    -- State of the most recent /solve run, persisted SEPARATELY from the
    -- retained (old) result row:
    --   * NULL                     -> the stored result (if any) is the
    --                                product of the latest verified solve
    --                                (feasible/infeasible); normal creation;
    --   * 'unknown'                -> the latest solve proved NOTHING (timed
    --                                out).  The stored feasible table (kept
    --                                in result_json) is an unverified OLD
    --                                table; batches may freeze it only via an
    --                                explicit, threshold-checked selection.
    -- ``unverified_solve_json`` remembers that unproven run's attempted
    -- thresholds/locks so the marker survives restarts and page reloads and
    -- can be shown alongside the retained old table.  A new verified solve
    -- (feasible or proven infeasible) clears both columns.
    latest_solve_status     TEXT,
    unverified_solve_json   TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id     INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    name            TEXT NOT NULL DEFAULT '',
    input_json      TEXT NOT NULL,
    safety_radius   REAL NOT NULL,
    safety_height   REAL NOT NULL,
    plan_json       TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_reports (
    batch_id        INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    sortie_id       TEXT NOT NULL,
    actual_offset   INTEGER NOT NULL CHECK (actual_offset >= 0),
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    reported_at     REAL NOT NULL,
    PRIMARY KEY (batch_id, sortie_id)
);

-- One row per *accepted* report write ("node").  Each node stores the FULL
-- report state immediately before and immediately after its write, so a
-- warning-change review is a pure replay of two committed snapshots.  The
-- node row is inserted in the same BEGIN IMMEDIATE transaction as the report
-- write it describes; therefore, even with several processes sharing the
-- file, report writes and review nodes correspond atomically and the per-batch
-- seq order is exactly the database commit order.  A NULL trigger marks the
-- baseline node (an empty state for new batches, or the latest state visible
-- at upgrade time for batches created before this feature existed).
CREATE TABLE IF NOT EXISTS batch_report_nodes (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id                INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    seq                     INTEGER NOT NULL,
    trigger_sortie_id       TEXT,
    trigger_actual_offset   INTEGER,
    trigger_revision        INTEGER,
    before_json             TEXT NOT NULL,
    after_json              TEXT NOT NULL,
    created_at              REAL NOT NULL,
    UNIQUE (batch_id, seq)
);
"""


class Store:
    def __init__(self, path: str | Path = "data/console.db"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self):
        """Enable WAL and create the schema, tolerating a peer that is doing
        the same initialization concurrently.

        Switching journal mode (and, on a fresh file, creating the tables)
        needs an exclusive lock, so a second process booting at the same time
        can briefly hit ``database is locked``; retry until it succeeds or
        confirms the schema already exists.
        """
        for attempt in range(_BUSY_ATTEMPTS):
            with self._conn() as c:
                try:
                    # WAL lets multiple processes read while one writes and
                    # keeps short write transactions from colliding.
                    c.execute("PRAGMA journal_mode=WAL")
                    c.execute("PRAGMA synchronous=NORMAL")
                    c.executescript(_SCHEMA)
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == _BUSY_ATTEMPTS - 1:
                        raise
            time.sleep(_BUSY_WAIT_SECONDS)
        self._migrate_scenarios()
        # Backfill review baselines for batches that existed before the node
        # table.  Existing batches only get a single baseline node describing
        # the newest state knowable at upgrade time - earlier history is never
        # fabricated.  Idempotent and serialized via the write transaction.
        self._backfill_baselines()

    @staticmethod
    def _has_column(c, table: str, column: str) -> bool:
        return any(r["name"] == column
                   for r in c.execute(f"PRAGMA table_info({table})").fetchall())

    def _migrate_scenarios(self) -> None:
        """Add the solve-state columns to databases created before them."""
        with self._write_tx() as c:
            if not self._has_column(c, "scenarios", "latest_solve_status"):
                c.execute("ALTER TABLE scenarios ADD COLUMN latest_solve_status TEXT")
            if not self._has_column(c, "scenarios", "unverified_solve_json"):
                c.execute("ALTER TABLE scenarios ADD COLUMN unverified_solve_json TEXT")

    @staticmethod
    def _snapshot(c, batch_id: int) -> str:
        """Full per-batch report state as deterministic JSON (sortie id sorted)."""
        rows = c.execute(
            "SELECT sortie_id,actual_offset,revision FROM batch_reports "
            "WHERE batch_id=? ORDER BY sortie_id",
            (batch_id,)).fetchall()
        return json.dumps([dict(r) for r in rows], ensure_ascii=False,
                          sort_keys=True)

    def _backfill_baselines(self) -> None:
        with self._write_tx() as c:
            rows = c.execute(
                "SELECT b.id FROM batches b WHERE NOT EXISTS ("
                "SELECT 1 FROM batch_report_nodes n WHERE n.batch_id = b.id)"
            ).fetchall()
            now = time.time()
            for r in rows:
                bid = r["id"]
                snap = self._snapshot(c, bid)
                c.execute(
                    "INSERT INTO batch_report_nodes"
                    "(batch_id,seq,trigger_sortie_id,trigger_actual_offset,"
                    "trigger_revision,before_json,after_json,created_at) "
                    "VALUES (?,1,NULL,NULL,NULL,?,?,?)",
                    (bid, snap, snap, now))

    def _conn(self):
        conn = sqlite3.connect(
            self.path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def _write_tx(self):
        """A single ``BEGIN IMMEDIATE`` write transaction.

        This acquires SQLite's reserved lock up front, blocks on a writer that
        already holds it via ``busy_timeout``, and groups the conditional
        write + authoritative re-read in one commit.  It does *not* by itself
        make a read-then-write sequence safe across processes (WAL snapshots
        differ), so callers must make each write conditional in SQL.
        """
        with _LOCK, self._conn() as c:
            for attempt in range(_BUSY_ATTEMPTS):
                try:
                    c.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == _BUSY_ATTEMPTS - 1:
                        raise
                    time.sleep(_BUSY_WAIT_SECONDS)
            try:
                yield c
            except Exception:
                c.rollback()
                raise
            else:
                c.commit()

    def create_scenario(self, input_payload: dict, safety_radius: float,
                        safety_height: float, name: str = "") -> int:
        with self._write_tx() as c:
            cur = c.execute(
                "INSERT INTO scenarios(name,input_json,safety_radius,safety_height,created_at)"
                " VALUES (?,?,?,?,?)",
                (name, json.dumps(input_payload, ensure_ascii=False),
                 float(safety_radius), float(safety_height), time.time()),
            )
            return int(cur.lastrowid)

    def save_result(self, scenario_id: int, result: dict, safety_radius: float,
                    safety_height: float, name: str | None = None) -> None:
        """Persist a VERIFIED solve (feasible table or a proven infeasibility).

        The unverified/unknown marker belongs to a previous run and is cleared
        here, so the freshly proven result restores unmarked batch creation.
        """
        with self._write_tx() as c:
            if name is not None:
                c.execute("UPDATE scenarios SET name=? WHERE id=?", (name, scenario_id))
            c.execute(
                "UPDATE scenarios SET result_json=?, solved_at=?, safety_radius=?, "
                "safety_height=?, latest_solve_status=NULL, unverified_solve_json=NULL"
                " WHERE id=?",
                (json.dumps(result, ensure_ascii=False), time.time(),
                 float(safety_radius), float(safety_height), scenario_id),
            )

    def rename_scenario(self, scenario_id: int, name: str) -> None:
        """Rename a saved plan without touching solve state.

        Renaming the retained old table must not lift the unverified marker.
        """
        with self._write_tx() as c:
            c.execute("UPDATE scenarios SET name=? WHERE id=?", (name, scenario_id))

    def mark_solve_unknown(self, scenario_id: int, attempted: dict) -> None:
        """Record that the most recent solve came back ``unknown``.

        The previously stored (old) table is never overwritten; only the
        marker and the unproven run's attempted thresholds/locks are saved, so
        the distinction survives restarts, reloads and renames.  A later
        verified solve clears the marker via :meth:`save_result`.
        """
        with self._write_tx() as c:
            c.execute(
                "UPDATE scenarios SET latest_solve_status='unknown', "
                "unverified_solve_json=? WHERE id=?",
                (json.dumps(attempted, ensure_ascii=False), scenario_id),
            )

    def list_scenarios(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,name,safety_radius,safety_height,created_at,solved_at,"
                "CASE WHEN result_json IS NOT NULL THEN 1 ELSE 0 END AS has_result "
                "FROM scenarios ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_scenario(self, scenario_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["input"] = json.loads(d.pop("input_json"))
        d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
        d["unverified_solve"] = (
            json.loads(d.pop("unverified_solve_json"))
            if d.get("unverified_solve_json") else None)
        return d

    def delete_scenario(self, scenario_id: int) -> bool:
        with self._write_tx() as c:
            # Remove dependent batches (and their reports/nodes) explicitly so
            # the cascade works even on databases created before the FK cascade.
            c.execute("DELETE FROM batch_report_nodes WHERE batch_id IN "
                      "(SELECT id FROM batches WHERE scenario_id=?)",
                      (scenario_id,))
            c.execute("DELETE FROM batch_reports WHERE batch_id IN "
                      "(SELECT id FROM batches WHERE scenario_id=?)",
                      (scenario_id,))
            c.execute("DELETE FROM batches WHERE scenario_id=?", (scenario_id,))
            cur = c.execute("DELETE FROM scenarios WHERE id=?", (scenario_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Execution batches (frozen plan) and actual-take-off reports
    # ------------------------------------------------------------------

    def create_batch(self, scenario_id: int, name: str,
                     use_stored_plan: bool = False,
                     confirmed_radius: float | None = None,
                     confirmed_height: float | None = None) -> int:
        """Create a batch after arbitrating against the in-transaction row.

        The whole decision - read the authoritative scenario state, judge it,
        freeze its snapshot/thresholds/plan and insert the batch - happens in
        ONE ``BEGIN IMMEDIATE`` transaction.  Reading the feasible table, the
        ``unknown`` marker and the stored thresholds only after acquiring the
        write lock is what makes batch creation correct when another *process*
        sharing this SQLite file commits a solve in the gap a request would
        otherwise have used to pre-read that state:

        * if the latest solve is ``unknown`` (persisted marker) while an OLD
          feasible table is retained, the batch is created ONLY with an
          explicit ``use_stored_plan`` selection AND a confirmation matching
          the old table's ORIGINAL thresholds - a marker committed first can
          therefore never be passed on a stale pre-transaction read;
        * if a new VERIFIED solve (feasible, or a proven infeasibility)
          commits first, its own snapshot/thresholds/plan are what gets read
          and frozen, so a stale old table can never be frozen on a
          pre-transaction read.

        Every refusal raises :class:`BatchRejected`, which rolls the
        transaction back, so no batch is left behind.
        """
        with self._write_tx() as c:
            row = c.execute(
                "SELECT input_json,safety_radius,safety_height,result_json,"
                "latest_solve_status FROM scenarios WHERE id=?",
                (scenario_id,)).fetchone()
            if row is None:
                raise BatchRejected(404, "scenario_not_found")
            result = json.loads(row["result_json"]) if row["result_json"] else None
            if result is None or not result.get("feasible"):
                # No feasible table at the authoritative moment - neither a
                # plain missing result nor a freshly committed proven
                # infeasibility may create a batch.
                raise BatchRejected(
                    400, "no_feasible_plan",
                    {"message": "a feasible plan must be solved before creating "
                                "an execution batch"})
            old_R, old_H = row["safety_radius"], row["safety_height"]
            if row["latest_solve_status"] == "unknown":
                # The retained feasible table is an UNVERIFIED old table: the
                # new locks/thresholds of the latest run proved nothing.  Only
                # an explicit, threshold-checked selection may freeze it.
                if not use_stored_plan:
                    raise BatchRejected(409, "latest_solve_unverified", {
                        "scenario_id": scenario_id,
                        "latest_solve_status": "unknown",
                        "stored_plan_original_safety_radius": old_R,
                        "stored_plan_original_safety_height": old_H,
                        "message": "the most recent solve returned unknown; the "
                                   "stored feasible table is an unverified OLD "
                                   "table. Re-solve successfully first, or "
                                   "explicitly select it with use_stored_plan="
                                   "true and confirm its original thresholds",
                    })
                if confirmed_radius is None or confirmed_height is None:
                    raise BatchRejected(
                        409, "stored_plan_threshold_confirmation_required", {
                            "scenario_id": scenario_id,
                            "stored_plan_original_safety_radius": old_R,
                            "stored_plan_original_safety_height": old_H,
                        })
                if (abs(float(confirmed_radius) - float(old_R)) > _THRESHOLD_TOL
                        or abs(float(confirmed_height) - float(old_H))
                        > _THRESHOLD_TOL):
                    raise BatchRejected(409, "stored_plan_threshold_mismatch", {
                        "scenario_id": scenario_id,
                        "stored_plan_original_safety_radius": old_R,
                        "stored_plan_original_safety_height": old_H,
                        "confirmed_safety_radius": confirmed_radius,
                        "confirmed_safety_height": confirmed_height,
                    })
            # The frozen values come from the SAME authoritative row the
            # decision was made on; a solve committed after this point blocks
            # on our write lock and a later request arbitrates against it
            # afresh.
            input_payload = json.loads(row["input_json"])
            plan = {"offsets": dict(result["offsets"])}
            return self._insert_batch_locked(
                c, scenario_id, name, input_payload, old_R, old_H, plan)

    @staticmethod
    def _insert_batch_locked(c, scenario_id: int, name: str,
                             input_payload: dict, safety_radius: float,
                             safety_height: float, plan: dict,
                             now: float | None = None) -> int:
        """Insert a batch row + baseline node on an open write transaction."""
        if now is None:
            now = time.time()
        cur = c.execute(
            "INSERT INTO batches(scenario_id,name,input_json,safety_radius,"
            "safety_height,plan_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (scenario_id, name, json.dumps(input_payload, ensure_ascii=False),
             float(safety_radius), float(safety_height),
             json.dumps(plan, ensure_ascii=False), now),
        )
        bid = int(cur.lastrowid)
        # Baseline node (seq 1): the empty report state before any accepted
        # write. Created in the same transaction as the batch, so every batch
        # always has a replay chain starting at seq 1.
        c.execute(
            "INSERT INTO batch_report_nodes"
            "(batch_id,seq,trigger_sortie_id,trigger_actual_offset,"
            "trigger_revision,before_json,after_json,created_at) "
            "VALUES (?,1,NULL,NULL,NULL,'[]','[]',?)",
            (bid, now))
        return bid

    def insert_batch(self, scenario_id: int, name: str, input_payload: dict,
                     safety_radius: float, safety_height: float,
                     plan: dict) -> int:
        """Freeze an EXPLICIT snapshot/thresholds/plan into a new batch.

        Low-level entry point bypassing the feasible-plan/unknown-marker
        adjudication (which the HTTP route performs atomically in
        :meth:`create_batch`); used when the caller already owns the exact
        values to freeze (and by the storage-level report tests).
        """
        with self._write_tx() as c:
            return self._insert_batch_locked(
                c, scenario_id, name, input_payload,
                safety_radius, safety_height, plan)

    def list_batches(self, scenario_id: int | None = None) -> list[dict]:
        sql = ("SELECT b.id,b.scenario_id,b.name,b.safety_radius,b.safety_height,"
               "b.created_at, COUNT(r.sortie_id) AS reported_count "
               "FROM batches b LEFT JOIN batch_reports r "
               "ON r.batch_id = b.id ")
        args: tuple = ()
        if scenario_id is not None:
            sql += "WHERE b.scenario_id=? "
            args = (scenario_id,)
        sql += "GROUP BY b.id ORDER BY b.id DESC"
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def get_batch(self, batch_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["input"] = json.loads(d.pop("input_json"))
            d["plan"] = json.loads(d.pop("plan_json"))
            reports = c.execute(
                "SELECT sortie_id,actual_offset,revision,reported_at "
                "FROM batch_reports WHERE batch_id=? ORDER BY sortie_id",
                (batch_id,),
            ).fetchall()
        d["reports"] = [dict(r) for r in reports]
        return d

    def delete_batch(self, batch_id: int) -> bool:
        with self._write_tx() as c:
            cur = c.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            return cur.rowcount > 0

    def submit_report(self, batch_id: int, sortie_id: str,
                      actual_offset: int, revision: int) -> tuple[str, dict | None]:
        """Atomically apply a revision-controlled report.

        The decision is enforced by the database itself rather than by a
        read-then-write sequence, so it stays correct when several *processes*
        share the SQLite file.  Inside one ``BEGIN IMMEDIATE`` transaction two
        guarded statements are issued:

        1. an ``INSERT ... SELECT ... WHERE NOT EXISTS(...) AND revision = 1``
           that creates the row only for a first report at revision 1;
        2. an ``UPDATE ... WHERE incoming_revision > stored_revision`` that
           replaces the row only for a strictly higher revision.

        Because every write is conditional on the row committed at write
        time, a lower revision can never overwrite a higher one and a
        same-revision write can never change an existing value, no matter how
        overlapping transactions commit.  The row is then re-read inside the
        same transaction to classify the outcome against the authoritative
        state (so a loser reports the winner's ``current_revision``):

        * no row + revision 1          -> ``inserted``
        * no row + revision != 1       -> ``invalid_first_revision``
        * revision == stored revision  -> same value ``idempotent`` /
          different value ``revision_value_conflict``
        * revision  < stored revision  -> ``stale_revision``
        * revision  > stored revision  -> ``inserted``

        Returns ``(outcome, info)``.
        """
        with self._write_tx() as c:
            now = time.time()
            # The review node must describe the exact state transition
            # performed by THIS committed transaction, so the "before"
            # snapshot is taken inside it (after BEGIN IMMEDIATE, all prior
            # writers have committed) and the node is written in the same
            # commit as the report write.
            before_snapshot = self._snapshot(c, batch_id)
            # 1) First report only: create the row when none exists AND the
            #    incoming revision is 1.
            cur = c.execute(
                "INSERT INTO batch_reports(batch_id,sortie_id,actual_offset,"
                "revision,reported_at) "
                "SELECT ?,?,?,?,? WHERE ? = 1 AND NOT EXISTS ("
                "SELECT 1 FROM batch_reports WHERE batch_id=? AND sortie_id=?)",
                (batch_id, sortie_id, int(actual_offset), int(revision), now,
                 int(revision), batch_id, sortie_id),
            )
            wrote = cur.rowcount > 0
            # 2) Higher revision only: replace an existing strictly-lower row.
            if not wrote:
                cur = c.execute(
                    "UPDATE batch_reports SET actual_offset=?,revision=?,"
                    "reported_at=? WHERE batch_id=? AND sortie_id=? AND revision < ?",
                    (int(actual_offset), int(revision), now,
                     batch_id, sortie_id, int(revision)),
                )
                wrote = cur.rowcount > 0
            if wrote:
                after_snapshot = self._snapshot(c, batch_id)
                # seq follows the per-batch node count; committed
                # transactions serialise on the write lock, so this order is
                # exactly the actual submission (commit) order, even across
                # processes sharing the file.
                seq = int(c.execute(
                    "SELECT COUNT(*) AS n FROM batch_report_nodes WHERE batch_id=?",
                    (batch_id,)).fetchone()["n"]) + 1
                c.execute(
                    "INSERT INTO batch_report_nodes"
                    "(batch_id,seq,trigger_sortie_id,trigger_actual_offset,"
                    "trigger_revision,before_json,after_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (batch_id, seq, sortie_id, int(actual_offset),
                     int(revision), before_snapshot, after_snapshot, now))
                return "inserted", {"sortie_id": sortie_id,
                                    "actual_offset": int(actual_offset),
                                    "revision": int(revision)}
            row = c.execute(
                "SELECT actual_offset,revision FROM batch_reports "
                "WHERE batch_id=? AND sortie_id=?",
                (batch_id, sortie_id),
            ).fetchone()
            if row is None:
                # No row exists, so the only legal write is a first report at
                # revision 1; anything else is rejected unchanged.
                return "invalid_first_revision", None
            cur_off, cur_rev = row["actual_offset"], row["revision"]
            if revision == cur_rev:
                if actual_offset != cur_off:
                    return "revision_value_conflict", {
                        "stored_actual_offset": cur_off}
                return "idempotent", {"sortie_id": sortie_id,
                                      "actual_offset": cur_off,
                                      "revision": cur_rev}
            return "stale_revision", {"current_revision": cur_rev}

    # ------------------------------------------------------------------
    # Report nodes (accepted-write events) for warning-change reviews
    # ------------------------------------------------------------------

    @staticmethod
    def _node_row(r) -> dict:
        return {
            "id": r["id"],
            "batch_id": r["batch_id"],
            "seq": r["seq"],
            "created_at": r["created_at"],
            "trigger_sortie_id": r["trigger_sortie_id"],
            "trigger_actual_offset": r["trigger_actual_offset"],
            "trigger_revision": r["trigger_revision"],
            "is_baseline": r["trigger_sortie_id"] is None,
            "before": json.loads(r["before_json"]),
            "after": json.loads(r["after_json"]),
        }

    def list_report_nodes(self, batch_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM batch_report_nodes WHERE batch_id=? ORDER BY seq",
                (batch_id,)).fetchall()
        return [self._node_row(r) for r in rows]

    def get_report_node(self, batch_id: int, node_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM batch_report_nodes WHERE batch_id=? AND id=?",
                (batch_id, node_id)).fetchone()
        return self._node_row(row) if row is not None else None
