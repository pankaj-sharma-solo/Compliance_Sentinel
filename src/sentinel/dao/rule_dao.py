from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sentinel.models.rule import Rule, RuleStatus
from sentinel.dao.vector_store import (
    upsert_rule, find_nearest_rule, deprecate_rule_in_vector_store
)
from sentinel.config import settings
import logging

logger = logging.getLogger(__name__)


def get_rule_by_id(db: Session, rule_id: str) -> Rule | None:
    return db.query(Rule).filter_by(rule_id = rule_id).first()


def get_active_rules(db: Session) -> list[Rule]:
    return db.query(Rule).filter_by(status = RuleStatus.ACTIVE).all()


def insert_rule(db: Session, rule: Rule) -> Rule:
    db.add(rule)
    db.commit()
    db.refresh(rule)
    # Sync to Qdrant immediately — MySQL is canonical, Qdrant is index
    upsert_rule(
        rule_id=rule.rule_id,
        rule_text=rule.rule_text,
        metadata={
            "source_doc": rule.source_doc,
            "article_ref": rule.article_ref,
            "status": rule.status.value,
            "obligation_type": rule.obligation_type.value,
            "severity": _extract_max_severity(rule.violation_conditions),
        },
    )
    return rule


def _extract_max_severity(conditions: list) -> str:
    order = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    if not conditions:
        return "MEDIUM"
    return max(conditions, key=lambda c: order.get(c.get("severity", "MEDIUM"), 2)).get("severity", "MEDIUM")


def reconcile_version(db: Session, new_rule_text: str, new_rule_data: dict) -> dict:
    """
    Called during ingestion when a new PDF is uploaded.
    Returns {action: 'new' | 'supersede' | 'human_review', existing_rule_id?}
    """
    nearest = find_nearest_rule(new_rule_text)
    if nearest is None:
        return {"action": "new"}

    score = nearest["score"]
    existing_id = nearest["rule_id"]

    if score >= settings.similarity_high:
        return {"action": "human_review", "existing_rule_id": existing_id, "score": score}
    elif score >= settings.similarity_mid:
        return {"action": "supersede", "existing_rule_id": existing_id, "score": score}
    else:
        return {"action": "new"}


def supersede_rule(db: Session, old_rule_id: str, new_rule: Rule) -> Rule | None:
    """
    Insert new rule, mark old as DEPRECATED, set superseded_by FK, sync Qdrant.
    FK-safe order: new rule inserted + flushed FIRST, then old rule updated.
    """
    try:
        # ── Step 1: New rule in DB first ──────────────────────────────────────
        db.add(new_rule)
        db.flush()

        # ── Step 2: Now safe to point old rule at new rule_id ─────────────────
        old = get_rule_by_id(db, old_rule_id)
        if old:
            old.status = RuleStatus.DEPRECATED
            old.superseded_by = new_rule.rule_id
            db.flush()
            deprecate_rule_in_vector_store(old_rule_id)

        # ── Step 3: Commit both changes atomically ────────────────────────────
        db.commit()
        db.refresh(new_rule)

        # ── Step 4: Sync new rule to Qdrant ───────────────────────────────────
        upsert_rule(
            rule_id=new_rule.rule_id,
            rule_text=new_rule.rule_text,
            metadata={
                "source_doc": new_rule.source_doc,
                "article_ref": getattr(new_rule, "article_ref", ""),
                "status": new_rule.status.value,
                "obligation_type": new_rule.obligation_type.value,
                "severity": _extract_max_severity(new_rule.violation_conditions),
            },
        )
        logger.info("Superseded '%s' → '%s'", old_rule_id, new_rule.rule_id)
        return new_rule

    except IntegrityError as e:
        db.rollback()
        logger.error("supersede_rule failed for '%s': %s", new_rule.rule_id, e)
        return None

