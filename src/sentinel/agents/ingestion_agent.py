"""
Policy ingestion pipeline — LangGraph StateGraph.
Nodes: extract_candidates → extract_spans → decompose_rules → persist_rules
Cost boundary: all LLM calls happen here, NEVER at scan time.
"""
from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI
from sqlalchemy.orm import Session
from sentinel.states.state import IngestionState, DecomposedRule
from sentinel.tools.extraction_tools import pass1_extract_candidates, pass2_extract_structured_spans
from sentinel.tools.decomposition_tool import decompose_rule_span
from sentinel.models.rule import Rule, RuleStatus, ObligationType
from sentinel.dao.rule_dao import insert_rule, reconcile_version, supersede_rule
from sentinel.models.audit_log import AuditLog
from sentinel.config import settings
from datetime import date
import logging

logger = logging.getLogger(__name__)


def node_extract_candidates(state: IngestionState) -> dict:
    """Pass-1: cheap model chunks PDF and identifies rule candidate sections."""
    try:
        candidates = pass1_extract_candidates.invoke({"pdf_path": state["pdf_path"]})
        return {"raw_chunks": candidates, "candidate_spans": []}
    except Exception as e:
        logger.error("Pass-1 extraction failed: %s", e)
        return {"errors": state.get("errors", []) + [f"Pass-1 failed: {e}"]}


def node_extract_spans(state: IngestionState) -> dict:
    """Pass-2: strong model extracts structured rule spans from candidate chunks only."""
    if not state.get("raw_chunks"):
        return {"errors": state.get("errors", []) + ["No candidate chunks from Pass-1"]}
    try:
        spans = pass2_extract_structured_spans.invoke({
            "candidates": state["raw_chunks"],
            "source_doc": state["source_doc"],
        })
        return {"candidate_spans": spans}
    except Exception as e:
        logger.error("Pass-2 extraction failed: %s", e)
        return {"errors": state.get("errors", []) + [f"Pass-2 failed: {e}"]}


def node_decompose_rules(state: IngestionState) -> dict:
    """Decompose each rule span into machine-checkable ViolationConditions."""
    if not state.get("candidate_spans"):
        return {"errors": state.get("errors", []) + ["No spans to decompose"]}

    decomposed = []
    errors = list(state.get("errors", []))

    for span in state["candidate_spans"]:
        result = decompose_rule_span.invoke({"span": span})
        if result:
            try:
                decomposed.append(DecomposedRule(**result))
            except Exception as e:
                errors.append(f"DecomposedRule validation failed for {span.get('article_ref')}: {e}")
        else:
            errors.append(f"Decomposition returned None for: {span.get('article_ref', 'unknown')}")

    logger.info("Decomposed %d/%d spans successfully", len(decomposed), len(state["candidate_spans"]))
    return {"decomposed_rules": decomposed, "errors": errors}


def node_persist_rules(state: IngestionState, db: Session) -> dict:
    """
    Persist decomposed rules to MySQL + sync Qdrant.
    Handles version reconciliation: new | supersede | human_review.
    """
    persisted_ids = []
    errors = list(state.get("errors", []))

    for rule_data in state.get("decomposed_rules", []):
        try:
            reconcile = reconcile_version(db, rule_data.rule_text, rule_data.model_dump())
            action = reconcile["action"]

            new_rule = Rule(
                rule_id=rule_data.rule_id,
                rule_text=rule_data.rule_text,
                source_doc=rule_data.source_doc,
                article_ref=rule_data.article_ref,
                version=1,
                status=RuleStatus.DRAFT if action == "human_review" else RuleStatus.ACTIVE,
                effective_date=date.today(),
                obligation_type=ObligationType[rule_data.obligation_type],
                data_subject_scope=rule_data.data_subject_scope,
                violation_conditions=[vc.model_dump() for vc in rule_data.violation_conditions],
            )

            if action == "supersede":
                supersede_rule(db, reconcile["existing_rule_id"], new_rule)
                logger.info("Superseded rule %s with %s", reconcile["existing_rule_id"], rule_data.rule_id)
            elif action == "human_review":
                insert_rule(db, new_rule)  # status=DRAFT, awaits human confirmation
                logger.info("Rule %s queued for human review (similarity %.3f)", rule_data.rule_id, reconcile["score"])
            else:
                insert_rule(db, new_rule)
                logger.info("Inserted new rule %s", rule_data.rule_id)

            persisted_ids.append(rule_data.rule_id)

        except Exception as e:
            logger.error("Failed to persist rule %s: %s", rule_data.rule_id, e)
            errors.append(f"Persist failed for {rule_data.rule_id}: {e}")

    return {"persisted_rule_ids": persisted_ids, "errors": errors}


def build_ingestion_graph(db: Session):
    """Build and compile the ingestion StateGraph with DB session injected."""
    graph = StateGraph(IngestionState)

    graph.add_node("extract_candidates", node_extract_candidates)
    graph.add_node("extract_spans", node_extract_spans)
    graph.add_node("decompose_rules", node_decompose_rules)
    graph.add_node("persist_rules", lambda state: node_persist_rules(state, db))

    graph.add_edge(START, "extract_candidates")
    graph.add_edge("extract_candidates", "extract_spans")
    graph.add_edge("extract_spans", "decompose_rules")
    graph.add_edge("decompose_rules", "persist_rules")
    graph.add_edge("persist_rules", END)

    return graph.compile()
