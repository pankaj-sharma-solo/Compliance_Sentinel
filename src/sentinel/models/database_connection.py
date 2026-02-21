from datetime import datetime
from sqlalchemy import Column, String, Integer, Enum, DateTime, JSON, func, Text
from sentinel.database import Base
import enum


class ScanMode(str, enum.Enum):
    CDC = "CDC"
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


class DatabaseConnection(Base):
    __tablename__ = "database_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(256), nullable=False)               # friendly name
    connection_string_enc = Column(Text, nullable=False)     # encrypted at rest
    db_type = Column(String(64), nullable=False, default="mysql")
    server_region = Column(String(64), nullable=True)        # e.g. us-east-1
    scan_mode = Column(Enum(ScanMode), nullable=False, default=ScanMode.SCHEDULED)
    cron_expression = Column(String(128), nullable=True)     # e.g. "0 2 * * *"
    schema_map = Column(JSON, nullable=True)                 # {table: {col: category}}
    owner_user_id = Column(String(128), nullable=True)
    last_scanned_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
