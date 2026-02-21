from datetime import date, datetime
from sqlalchemy import (
    Column, String, Text, Integer, Enum, Date, DateTime,
    ForeignKey, JSON, func
)
from sentinel.database import Base
import enum


class ObligationType(str, enum.Enum):
    PROHIBITION = "PROHIBITION"
    REQUIREMENT = "REQUIREMENT"
    PERMISSION = "PERMISSION"


class RuleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    DRAFT = "DRAFT"


class Rule(Base):
    __tablename__ = "rules"

    rule_id = Column(String(64), primary_key=True)           # e.g. GDPR-Art44-001
    rule_text = Column(Text, nullable=False)                 # raw regulatory text
    source_doc = Column(String(256), nullable=False)         # e.g. GDPRv2.pdf
    article_ref = Column(String(128), nullable=True)         # e.g. Article 44
    version = Column(Integer, nullable=False, default=1)
    status = Column(Enum(RuleStatus), nullable=False, default=RuleStatus.ACTIVE)
    superseded_by = Column(String(64), ForeignKey("rules.rule_id"), nullable=True)
    effective_date = Column(Date, nullable=False, default=date.today)
    obligation_type = Column(Enum(ObligationType), nullable=False)
    data_subject_scope = Column(JSON, nullable=True)         # list of strings
    violation_conditions = Column(JSON, nullable=False)      # THE key field — pre-decomposed logic
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
