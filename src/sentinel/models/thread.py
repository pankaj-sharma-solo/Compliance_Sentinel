# sentinel/models/thread.py
import enum
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, Enum, Integer, JSON, ForeignKey  # ← add ForeignKey
from sqlalchemy.orm import relationship
from sentinel.database import Base


class ThreadStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OrchestratorThread(Base):
    __tablename__ = "orchestrator_threads"

    thread_id = Column(String(128), primary_key=True)
    workflow_type = Column(String(64), nullable=False)
    status = Column(Enum(ThreadStatus), default=ThreadStatus.RUNNING, nullable=False)

    db_connection_id = Column(
        Integer,
        ForeignKey("database_connections.id", ondelete="SET NULL"),
        nullable=True,
    )

    user_message = Column(Text, nullable=True)
    final_response = Column(Text, nullable=True)
    todos = Column(JSON, nullable=True)
    pending_review = Column(JSON, nullable=True)
    human_decision = Column(String(64), nullable=True)
    human_feedback = Column(Text, nullable=True)
    actor = Column(String(128), nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    interrupted_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_detail = Column(Text, nullable=True)

    db_connection = relationship(
        "DatabaseConnection",
        foreign_keys=[db_connection_id],
        lazy="joined",
    )
