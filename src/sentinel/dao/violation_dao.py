from sqlalchemy.orm import Session
from sqlalchemy import desc
from sentinel.models.violation import Violation, ViolationStatus, Severity
from sentinel.models.audit_log import AuditLog
import logging

logger = logging.getLogger(__name__)


def persist_violation(db: Session, violation_data: dict, checkpoint_id: str | None = None) -> Violation:
    """Persist a detected violation and append an immutable audit record."""
    v = Violation(**{k: v for k, v in violation_data.items() if hasattr(Violation, k)})
    db.add(v)
    db.flush()  # get v.id before commit

    audit = AuditLog(
        event_type="VIOLATION_DETECTED",
        entity_type="violation",
        entity_id=str(v.id),
        actor="system",
        detail={
            "rule_id": v.rule_id,
            "table_name": v.table_name,
            "column_name": v.column_name,
            "condition_matched": v.condition_matched,
            "severity": v.severity
        },
        langgraph_checkpoint_id=checkpoint_id,
    )
    db.add(audit)
    db.commit()
    db.refresh(v)
    return v


def get_violations_by_connection(
    db: Session, db_connection_id: int, status: ViolationStatus | None = None
) -> list[Violation]:
    q = db.query(Violation).filter_by(db_connection_id = db_connection_id)
    if status:
        q = q.filter_by(status = status)
    return q.order_by(desc(Violation.detected_at)).all()


def get_open_violations(db: Session, limit: int = 100) -> list[Violation]:
    return (
        db.query(Violation)
        .filter_by(status = ViolationStatus.OPEN)
        .order_by(desc(Violation.detected_at))
        .limit(limit)
        .all()
    )


def resolve_violation(
    db: Session, violation_id: int, new_status: ViolationStatus, resolved_by: str
) -> Violation | None:
    from datetime import datetime
    v = db.query(Violation).filter_by(id = violation_id).first()
    if not v:
        return None
    v.status = new_status
    v.resolved_by = resolved_by
    v.resolved_at = datetime.utcnow()
    audit = AuditLog(
        event_type="VIOLATION_RESOLVED",
        entity_type="violation",
        entity_id=str(violation_id),
        actor=resolved_by,
        detail={"new_status": new_status.value},
    )
    db.add(audit)
    db.commit()
    db.refresh(v)
    return v


def get_audit_logs(db: Session, entity_id: str | None = None, limit: int = 200) -> list[AuditLog]:
    q = db.query(AuditLog)
    if entity_id:
        q = q.filter_by(entity_id = entity_id)
    return q.order_by(desc(AuditLog.created_at)).limit(limit).all()
