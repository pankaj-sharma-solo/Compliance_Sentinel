# sentinel/models/ingestion_job.py
import enum
from datetime import datetime
from sqlalchemy import Column, String, Integer, Text, DateTime, Enum
from sentinel.database import Base

class IngestionJobStatus(str, enum.Enum):
    QUEUED          = "QUEUED"
    EXTRACTING      = "EXTRACTING"
    DECOMPOSING     = "DECOMPOSING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    COMPLETED       = "COMPLETED"
    FAILED          = "FAILED"

class IngestionJob(Base):
    __tablename__ = "pdf_ingestion_jobs"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    job_id           = Column(String(128), unique=True, nullable=False)
    filename         = Column(String(256), nullable=False)
    source_doc       = Column(String(256), nullable=False)
    status           = Column(Enum(IngestionJobStatus), default=IngestionJobStatus.QUEUED)
    candidate_spans  = Column(Integer, nullable=True)
    rules_decomposed = Column(Integer, nullable=True)
    rules_approved   = Column(Integer, default=0)
    error_detail     = Column(Text, nullable=True)
    started_at       = Column(DateTime, default=datetime.utcnow)
    completed_at     = Column(DateTime, nullable=True)
