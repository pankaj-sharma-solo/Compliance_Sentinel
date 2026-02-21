"""
Enforcement / scanning agent — LangGraph StateGraph.
Context-isolated per DB connection (Deep Agent pattern from reference codebase).
Nodes: filter_relevant_tables → run_enforcement_checks → persist_violations

Scan time is almost entirely programmatic — LLM is rare fallback only.
"""
from langgraph.graph import StateGraph, START, END
from sqlalchemy.orm import Session
from sentinel.states.state import ScanState
from sentinel.tools.enforcement_tools import (
    check_schema_map_match, evaluate_condition_chain
)
from sentinel.dao.rule_dao import get_active_rules
from sentinel.dao.vector_store import retrieve_relevant_rules
from sentinel.dao.violation_dao import persist_violation
from sentinel.config import settings
import logging

logger = logging.getLogger(__name__)


def node_filter_relevant_tables(state: ScanState) -> dict:
    """
    Pre-scan table filtering — matches schema_map against rule library via Qdrant.
    Only tables with compliance relevance get deep-scanned.
    Reduces O(n*m) to O(k*m) where k << n.
    """
    schema_map = state.get("schema_map", {})
    relevant_rules = []

    for table_name, columns in schema_map.items():
        # Build schema context string for semantic search
        col_summaries = ", ".join(
            f"{col}:{meta.get('compliance_category', 'Unknown')}"
            for col, meta in columns.items()
        )
        schema_context = f"Table: {table_name}. Columns: {col_summaries}"
        rules = retrieve_relevant_rules(schema_context, top_k=settings.max_relevant_rules_per_table)
        for r in rules:
            r["_matched_table"] = table_name
            relevant_rules.append(r)

    logger.info(
        "DB %s: %d relevant rule matches across %d tables",
        state["db_connection_id"], len(relevant_rules), len(schema_map)
    )
    return {"relevant_rules": relevant_rules}


def node_run_enforcement_checks(state: ScanState) -> dict:
    """
    Core enforcement loop — three-layer detection per (table, column, condition).
    80–90% of checks are pure SQL/metadata. LLM fallback is rare.
    """
    schema_map = state.get("schema_map", {})
    relevant_rules = state.get("relevant_rules", [])
    connection_string = state.get("connection_string", "")
    server_region = state.get("server_region", "")
    violations = []
    errors = list(state.get("errors", []))

    for rule_entry in relevant_rules:
        table = rule_entry.get("_matched_table")
        violation_conditions = rule_entry.get("violation_conditions", [])

        if not isinstance(violation_conditions, list):
            continue

        for condition in violation_conditions:
            condition["rule_id"] = rule_entry["rule_id"]
            # Layer 1: find matching columns via schema map
            match_result = check_schema_map_match.invoke({
                "schema_map": schema_map,
                "condition": condition,
            })
            for match in match_result.get("matches", []):
                col = match["column"]
                try:
                    evidence = evaluate_condition_chain(
                        connection_string=connection_string,
                        server_region=server_region,
                        schema_map=schema_map,
                        condition=condition,
                        table=match["table"],
                        column=col,
                    )
                    if evidence:
                        evidence["db_connection_id"] = state["db_connection_id"]
                        violations.append(evidence)
                        logger.info(
                            "Violation found: %s.%s → rule %s",
                            match["table"], col, rule_entry["rule_id"]
                        )
                except Exception as e:
                    errors.append(f"Enforcement check error {table}.{col}: {e}")

    return {"violations_found": violations, "errors": errors}


def node_persist_violations(state: ScanState, db: Session) -> dict:
    """Persist all detected violations to MySQL and append audit records."""
    persisted = []
    errors = list(state.get("errors", []))
    checkpoint_id = state.get("langgraph_checkpoint_id")

    for v_data in state.get("violations_found", []):
        try:
            v = persist_violation(db, v_data, checkpoint_id=checkpoint_id)
            persisted.append(v.id)
        except Exception as e:
            logger.error("Failed to persist violation: %s", e)
            errors.append(f"Violation persist failed: {e}")

    logger.info("Persisted %d violations for DB %s", len(persisted), state["db_connection_id"])
    return {"scan_results": persisted, "errors": errors}


def build_enforcement_graph(db: Session):
    """
    Build the enforcement StateGraph.
    Context isolated per invocation — matches the _create_task_tool pattern:
    state["messages"] = [task_description_only] before invoking.
    """
    graph = StateGraph(ScanState)

    graph.add_node("filter_relevant_tables", node_filter_relevant_tables)
    graph.add_node("run_enforcement_checks", node_run_enforcement_checks)
    graph.add_node("persist_violations", lambda state: node_persist_violations(state, db))

    graph.add_edge(START, "filter_relevant_tables")
    graph.add_edge("filter_relevant_tables", "run_enforcement_checks")
    graph.add_edge("run_enforcement_checks", "persist_violations")
    graph.add_edge("persist_violations", END)

    return graph.compile()
