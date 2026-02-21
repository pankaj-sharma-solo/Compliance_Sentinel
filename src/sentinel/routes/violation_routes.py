"""
Violations and audit log routes.
GET  /violations             — list violations (filterable)
GET  /violations/{id}        — violation detail
PATCH /violations/{id}/resolve  — resolve/dismiss a violation
GET  /audit-logs             — audit trail
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from sentinel.database import get_db
from sentinel.models.violation import Violation, ViolationStatus
from sentinel.dao.violation_dao import (
    get_violations_by_connection, get_open_violations, resolve_violation, get_audit_logs
)

router = APIRouter(tags=["Violations & Audit"])


class ResolveRequest(BaseModel):
    new_status: str  # REMEDIATED | ACCEPTED_RISK | FALSE_POSITIVE
    resolved_by: str


@router.get("/violations")
def list_violations(
    db_connection_id: int | None = Query(None),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    if db_connection_id:
        s = ViolationStatus(status) if status else None
        violations = get_violations_by_connection(db, db_connection_id, s)
    else:
        violations = get_open_violations(db)

    return [
        {
            "id": v.id,
            "db_connection_id": v.db_connection_id,
            "rule_id": v.rule_id,
            "table_name": v.table_name,
            "column_name": v.column_name,
            "condition_matched": v.condition_matched,
            "severity": v.severity.value,
            "status": v.status.value,
            "remediation_template": v.remediation_template,
            "detected_at": str(v.detected_at),
        }
        for v in violations
    ]


@router.get("/violations/{violation_id}")
def get_violation(violation_id: int, db: Session = Depends(get_db)):
    v = db.query(Violation).filter_by(id = violation_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Violation not found")
    return {
        "id": v.id,
        "db_connection_id": v.db_connection_id,
        "rule_id": v.rule_id,
        "table_name": v.table_name,
        "column_name": v.column_name,
        "condition_matched": v.condition_matched,
        "evidence_snapshot": v.evidence_snapshot,
        "severity": v.severity.value,
        "status": v.status.value,
        "remediation_template": v.remediation_template,
        "detected_at": str(v.detected_at),
        "resolved_at": str(v.resolved_at) if v.resolved_at else None,
        "resolved_by": v.resolved_by,
    }


@router.patch("/violations/{violation_id}/resolve")
def resolve_violation_endpoint(
    violation_id: int,
    req: ResolveRequest,
    db: Session = Depends(get_db),
):
    try:
        status = ViolationStatus(req.new_status)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {req.new_status}")

    v = resolve_violation(db, violation_id, status, req.resolved_by)
    if not v:
        raise HTTPException(status_code=404, detail="Violation not found")
    return {"id": v.id, "status": v.status.value, "resolved_by": v.resolved_by}


@router.get("/audit-logs")
def list_audit_logs(
    entity_id: str | None = Query(None),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
):
    logs = get_audit_logs(db, entity_id=entity_id, limit=limit)
    return [
        {
            "id": l.id,
            "event_type": l.event_type,
            "entity_type": l.entity_type,
            "entity_id": l.entity_id,
            "actor": l.actor,
            "detail": l.detail,
            "langgraph_checkpoint_id": l.langgraph_checkpoint_id,
            "created_at": str(l.created_at),
        }
        for l in logs
    ]
