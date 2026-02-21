from datetime import datetime
from sqlalchemy import Column, String, Integer, Enum, DateTime, JSON, ForeignKey, Text, func
from sentinel.database import Base
import enum


class Severity(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ViolationStatus(str, enum.Enum):
    OPEN = "OPEN"
    REMEDIATED = "REMEDIATED"
    ACCEPTED_RISK = "ACCEPTED_RISK"
    FALSE_POSITIVE = "FALSE_POSITIVE"


class Violation(Base):
    __tablename__ = "violations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    db_connection_id = Column(Integer, ForeignKey("database_connections.id"), nullable=False)
    rule_id = Column(String(64), ForeignKey("rules.rule_id"), nullable=False)
    table_name = Column(String(256), nullable=False)
    column_name = Column(String(256), nullable=True)
    condition_matched = Column(String(512), nullable=False)  # which violation_condition triggered
    evidence_snapshot = Column(JSON, nullable=True)          # sample values (anonymised)
    severity = Column(Enum(Severity), nullable=False, default=Severity.MEDIUM)
    remediation_template = Column(Text, nullable=True)
    status = Column(Enum(ViolationStatus), nullable=False, default=ViolationStatus.OPEN)
    detected_at = Column(DateTime, server_default=func.now())
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(String(128), nullable=True)
