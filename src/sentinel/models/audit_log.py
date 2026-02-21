from sqlalchemy import Column, Integer, String, DateTime, JSON, Text, func
from sentinel.database import Base


class AuditLog(Base):
    """
    INSERT-only table. DB-level trigger installed in database.py
    prevents any UPDATE or DELETE at the engine level.
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(128), nullable=False)         # VIOLATION_DETECTED, RULE_VERSION_UPDATED, etc.
    entity_type = Column(String(64), nullable=True)          # rule | violation | connection
    entity_id = Column(String(128), nullable=True)
    actor = Column(String(128), nullable=True)               # user_id or "system"
    detail = Column(JSON, nullable=True)                     # arbitrary structured payload
    langgraph_checkpoint_id = Column(String(256), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
