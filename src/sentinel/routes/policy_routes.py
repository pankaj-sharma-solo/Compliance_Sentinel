"""
Policy / PDF ingestion routes.
POST /policies/upload        — trigger full ingestion pipeline
GET  /policies/rules         — list active rules
GET  /policies/rules/{id}    — rule detail
PATCH /policies/rules/{id}/approve — approve DRAFT rule (human review queue)
"""
import shutil
import os
import uuid
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sentinel.database import get_db
from sentinel.models.rule import Rule, RuleStatus
from sentinel.agents.ingestion_agent import build_ingestion_graph
from sentinel.models.audit_log import AuditLog

router = APIRouter(prefix="/policies", tags=["Policies"])

UPLOAD_DIR = "/tmp/compliance_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _run_ingestion(pdf_path: str, source_doc: str, db: Session):
    graph = build_ingestion_graph(db)
    result = graph.invoke({
        "messages": [],
        "pdf_path": pdf_path,
        "source_doc": source_doc,
        "raw_chunks": [],
        "candidate_spans": [],
        "decomposed_rules": [],
        "persisted_rule_ids": [],
        "errors": [],
    })
    return result


@router.post("/upload")
async def upload_policy_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a compliance PDF and trigger the ingestion pipeline."""
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files accepted")

    job_id = str(uuid.uuid4())
    pdf_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file.filename}")

    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f) # type: ignore[arg-type]

    background_tasks.add_task(_run_ingestion, pdf_path, file.filename, db)
    return {"job_id": job_id, "status": "ingestion_queued", "filename": file.filename}


@router.get("/rules")
def list_rules(status: str = "ACTIVE", db: Session = Depends(get_db)):
    rules = db.query(Rule).filter_by(status = status).all()
    return [
        {
            "rule_id": r.rule_id,
            "rule_text": r.rule_text[:200],
            "source_doc": r.source_doc,
            "article_ref": r.article_ref,
            "status": r.status.value,
            "obligation_type": r.obligation_type.value,
            "version": r.version,
        }
        for r in rules
    ]


@router.get("/rules/{rule_id}")
def get_rule(rule_id: str, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter_by(rule_id = rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {
        "rule_id": rule.rule_id,
        "rule_text": rule.rule_text,
        "source_doc": rule.source_doc,
        "article_ref": rule.article_ref,
        "status": rule.status.value,
        "violation_conditions": rule.violation_conditions,
        "obligation_type": rule.obligation_type.value,
        "version": rule.version,
        "superseded_by": rule.superseded_by,
        "effective_date": str(rule.effective_date),
    }


@router.patch("/rules/{rule_id}/approve")
def approve_draft_rule(rule_id: str, actor: str = "system", db: Session = Depends(get_db)):
    """Approve a DRAFT rule (from human review queue) → set to ACTIVE."""
    rule = db.query(Rule).filter_by(rule_id = rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.status != RuleStatus.DRAFT:
        raise HTTPException(status_code=400, detail=f"Rule status is {rule.status.value}, not DRAFT")
    rule.status = RuleStatus.ACTIVE
    audit = AuditLog(
        event_type="RULE_APPROVED",
        entity_type="rule",
        entity_id=rule_id,
        actor=actor,
        detail={"previous_status": "DRAFT", "new_status": "ACTIVE"},
    )
    db.add(audit)
    db.commit()
    return {"rule_id": rule_id, "status": "ACTIVE"}
