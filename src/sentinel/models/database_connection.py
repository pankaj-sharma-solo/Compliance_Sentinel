from datetime import datetime
from sqlalchemy import Column, String, Integer, Enum, DateTime, JSON, func, Text, SmallInteger
from sentinel.database import Base
import enum


class ScanMode(str, enum.Enum):
    CDC       = "CDC"
    SCHEDULED = "SCHEDULED"
    MANUAL    = "MANUAL"


class DatabaseConnection(Base):
    __tablename__ = "database_connections"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    name                  = Column(String(256), nullable=False)
    connection_string_enc = Column(Text, nullable=False)
    db_type               = Column(String(64), nullable=False, default="mysql")
    server_region         = Column(String(64), nullable=True)
    scan_mode             = Column(Enum(ScanMode), nullable=False, default=ScanMode.SCHEDULED)
    cron_expression       = Column(String(128), nullable=True)
    schema_map            = Column(JSON, nullable=True)
    schema_mapped         = Column(SmallInteger, nullable=False, default=0)  # ← ADD THIS: 0=pending, 1=complete
    owner_user_id         = Column(String(128), nullable=True)
    last_scanned_at       = Column(DateTime, nullable=True)
    created_at            = Column(DateTime, server_default=func.now())
    updated_at            = Column(DateTime, server_default=func.now(), onupdate=datetime.utcnow)
