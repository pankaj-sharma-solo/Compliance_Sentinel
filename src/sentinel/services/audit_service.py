import logging
from sqlalchemy.orm import Session
from sentinel.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


def log_event(
        db: Session,
        event_type: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        actor: str = "system",
        detail: dict | None = None,
        checkpoint_id: str | None = None,
) -> AuditLog:
    """
    Central audit logging utility.
    Call this everywhere instead of inline AuditLog() inserts.

    Usage:
        log_event(db, "RULE_UPDATED", "rule", rule_id, actor="admin",
                  detail={"field": "rule_text"})
    """
    entry = AuditLog(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        detail=detail,
        langgraph_checkpoint_id=checkpoint_id,
    )
    db.add(entry)
    db.commit()
    logger.info("AUDIT [%s] entity=%s/%s actor=%s", event_type, entity_type, entity_id, actor)
    return entry
