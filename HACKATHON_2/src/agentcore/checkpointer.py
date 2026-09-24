"""Persistent SQLite Checkpointer for LangGraph state retention.

Enables full state persistence across turns, sessions, and UI page refreshes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)


class SqliteCheckpointer(BaseCheckpointSaver):
    """Thread-safe SQLite checkpointer for LangGraph graph execution."""

    def __init__(self, db_path: str = ".cache/checkpoints.sqlite"):
        super().__init__()
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    thread_id TEXT,
                    checkpoint_ns TEXT,
                    checkpoint_id TEXT,
                    parent_checkpoint_id TEXT,
                    checkpoint_type TEXT,
                    checkpoint_blob BLOB,
                    metadata_type TEXT,
                    metadata_blob BLOB,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS writes (
                    thread_id TEXT,
                    checkpoint_ns TEXT,
                    checkpoint_id TEXT,
                    task_id TEXT,
                    idx INTEGER,
                    channel TEXT,
                    val_type TEXT,
                    val_blob BLOB,
                    task_path TEXT,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blobs (
                    thread_id TEXT,
                    checkpoint_ns TEXT,
                    channel TEXT,
                    version TEXT,
                    val_type TEXT,
                    val_blob BLOB,
                    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
                )
            """)
            conn.commit()

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id")

        with sqlite3.connect(self.db_path) as conn:
            if checkpoint_id:
                row = conn.execute(
                    "SELECT checkpoint_id, parent_checkpoint_id, checkpoint_type, checkpoint_blob, metadata_type, metadata_blob "
                    "FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                    (thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT checkpoint_id, parent_checkpoint_id, checkpoint_type, checkpoint_blob, metadata_type, metadata_blob "
                    "FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id, checkpoint_ns),
                ).fetchone()

            if not row:
                return None

            cid, parent_cid, c_type, c_blob, m_type, m_blob = row
            checkpoint = self.serde.loads_typed((c_type, c_blob))
            metadata = self.serde.loads_typed((m_type, m_blob))

            values: dict[str, Any] = {}
            for channel, version in checkpoint.get("channel_versions", {}).items():
                brow = conn.execute(
                    "SELECT val_type, val_blob FROM blobs WHERE thread_id=? AND checkpoint_ns=? AND channel=? AND version=?",
                    (thread_id, checkpoint_ns, channel, version),
                ).fetchone()
                if brow and brow[0] != "empty":
                    values[channel] = self.serde.loads_typed((brow[0], brow[1]))

            checkpoint["channel_values"] = values

            wrows = conn.execute(
                "SELECT task_id, channel, val_type, val_blob FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                (thread_id, checkpoint_ns, cid),
            ).fetchall()
            pending_writes = [(tid, ch, self.serde.loads_typed((vt, vb))) for tid, ch, vt, vb in wrows]

            return CheckpointTuple(
                config={"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": cid}},
                checkpoint=checkpoint,
                metadata=metadata,
                parent_config={"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": parent_cid}} if parent_cid else None,
                pending_writes=pending_writes,
            )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if not config:
            return
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        with sqlite3.connect(self.db_path) as conn:
            cids = [
                r[0]
                for r in conn.execute(
                    "SELECT checkpoint_id FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? ORDER BY checkpoint_id DESC",
                    (thread_id, checkpoint_ns),
                ).fetchall()
            ]
        count = 0
        for cid in cids:
            if limit and count >= limit:
                break
            tup = self.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": cid}})
            if tup:
                yield tup
                count += 1

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        c = checkpoint.copy()
        values = c.pop("channel_values", {})

        c_type, c_blob = self.serde.dumps_typed(c)
        meta = get_checkpoint_metadata(config, metadata)
        m_type, m_blob = self.serde.dumps_typed(meta)
        parent_id = config["configurable"].get("checkpoint_id")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (thread_id, checkpoint_ns, checkpoint["id"], parent_id, c_type, c_blob, m_type, m_blob),
            )
            for k, v in new_versions.items():
                if k in values:
                    vt, vb = self.serde.dumps_typed(values[k])
                else:
                    vt, vb = ("empty", b"")
                conn.execute(
                    "INSERT OR REPLACE INTO blobs VALUES (?, ?, ?, ?, ?, ?)",
                    (thread_id, checkpoint_ns, k, v, vt, vb),
                )
            conn.commit()

        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": checkpoint["id"]}}

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        with sqlite3.connect(self.db_path) as conn:
            for idx, (channel, val) in enumerate(writes):
                vt, vb = self.serde.dumps_typed(val)
                conn.execute(
                    "INSERT OR REPLACE INTO writes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, vt, vb, task_path),
                )
            conn.commit()

    def delete_thread(self, thread_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))
            conn.execute("DELETE FROM writes WHERE thread_id=?", (thread_id,))
            conn.execute("DELETE FROM blobs WHERE thread_id=?", (thread_id,))
            conn.commit()
