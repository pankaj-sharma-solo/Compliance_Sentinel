"""
LangGraph checkpointer — enables graph suspension and resumption for HITL.
Uses SQLite for dev (zero infra). Swap to AsyncPostgresSaver for production.
"""
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.memory import MemorySaver
import os

_checkpointer = None

def get_checkpointer():
    """
    Returns a persistent checkpointer.
    SqliteSaver stores graph state so interrupt() can survive
    across HTTP requests — the graph resumes exactly where it paused.
    """
    global _checkpointer
    if _checkpointer is None:
        db_path = os.getenv("CHECKPOINT_DB_PATH", "./compliance_checkpoints.db")
        _checkpointer = SqliteSaver.from_conn_string(db_path)
    return _checkpointer
