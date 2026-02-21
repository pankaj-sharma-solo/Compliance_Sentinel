"""
LangGraph checkpointer — enables graph suspension and resumption for HITL.
Uses SQLite for dev (zero infra). Swap to AsyncPostgresSaver for production.
"""
import sqlite3
import os
from langgraph.checkpoint.sqlite import SqliteSaver

_checkpointer = None

def get_checkpointer() -> SqliteSaver:
    """
    Returns a persistent SqliteSaver checkpointer.
    Uses a raw sqlite3 connection (not from_conn_string) so it can be
    held open for the lifetime of the app — required for HITL interrupt/resume
    across multiple HTTP requests.
    """
    global _checkpointer
    if _checkpointer is None:
        db_path = os.getenv("CHECKPOINT_DB_PATH", "./compliance_checkpoints.db")
        conn = sqlite3.connect(db_path, check_same_thread=False)
        _checkpointer = SqliteSaver(conn)
    return _checkpointer
