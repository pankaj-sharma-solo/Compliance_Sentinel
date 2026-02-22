"""
Enforcement / scanning agent — LangGraph StateGraph.
"""
from langgraph.graph import StateGraph, START, END
from sqlalchemy.orm import Session
from sentinel.states.state import ScanState
from sentinel.tools.enforcement_tools import check_schema_map_match, evaluate_condition_chain
from sentinel.dao.rule_dao import get_rule_by_id
from sentinel.dao.vector_store import retrieve_relevant_rules
from sentinel.dao.violation_dao import persist_violation
from sentinel.config import settings
import logging

logger = logging.getLogger(__name__)


def node_filter_relevant_tables(state: ScanState) -> dict:
    """
    Pre-scan table filtering — matches schema_map against rule library via Qdrant.
    Returns: relevant_rules = list of {rule_id, score, _matched_table}
    NOTE: Qdrant payload intentionally has NO violation_conditions.
          Only rule_id + metadata is stored. Full rule is fetched from MySQL in next node.
    """
    schema_map    = state.get("schema_map", {})
    relevant_rules = []

    for table_name, columns in schema_map.items():
        col_summaries  = ", ".join(
            f"{col}:{meta.get('compliance_category', 'Unknown')}"
            for col, meta in columns.items()
        )
        schema_context = f"Table: {table_name}. Columns: {col_summaries}"

        hits = retrieve_relevant_rules(schema_context, top_k=settings.max_relevant_rules_per_table)
        for hit in hits:
            relevant_rules.append({
                "rule_id"       : hit["rule_id"],
                "score"         : hit["score"],
                "_matched_table": table_name,
            })

    logger.info(
        "DB %s: %d relevant rule matches across %d tables",
        state["db_connection_id"], len(relevant_rules), len(schema_map)
    )
    return {"relevant_rules": relevant_rules}


def node_run_enforcement_checks(state: ScanState, db: Session) -> dict:
    """
    Core enforcement loop.
    Qdrant gives us rule_id + matched_table.
    MySQL gives us the full Rule object including violation_conditions.
    """
    schema_map        = state.get("schema_map", {})
    relevant_rules    = state.get("relevant_rules", [])
    connection_string = state.get("connection_string", "")
    server_region     = state.get("server_region", "")
    violations        = []
    errors            = list(state.get("errors", []))

    # ── Bulk-fetch full Rule objects from MySQL ───────────────────────────────
    # Deduplicate rule_ids first — same rule may match multiple tables
    rule_ids_seen: set[str] = set()
    rule_cache   : dict[str, object] = {}

    for entry in relevant_rules:
        rid = entry["rule_id"]
        if rid not in rule_ids_seen:
            rule_ids_seen.add(rid)
            rule_obj = get_rule_by_id(db, rid)
            if rule_obj:
                rule_cache[rid] = rule_obj
            else:
                logger.warning("Rule %s found in Qdrant but not in MySQL — skipping", rid)

    logger.info("Fetched %d/%d rules from MySQL", len(rule_cache), len(rule_ids_seen))

    # ── Enforcement loop ──────────────────────────────────────────────────────
    for entry in relevant_rules:
        rule_id = entry["rule_id"]
        table   = entry["_matched_table"]

        rule_obj = rule_cache.get(rule_id)
        if not rule_obj:
            continue

        violation_conditions = rule_obj.violation_conditions  # JSON column → list[dict]
        if not isinstance(violation_conditions, list) or not violation_conditions:
            logger.debug("Rule %s has no violation_conditions — skipping", rule_id)
            continue

        for condition in violation_conditions:
            # Stamp rule_id onto condition so downstream tools know the source
            condition = {**condition, "rule_id": rule_id}

            # Layer 1 — schema map match: find which columns in `table` apply
            match_result = check_schema_map_match.invoke({
                "schema_map": schema_map,
                "condition" : condition,
            })

            logger.debug("Rule %s | table %s | matches: %s", rule_id, table, match_result)

            for match in match_result.get("matches", []):
                col = match["column"]
                try:
                    evidence = evaluate_condition_chain(
                        connection_string = connection_string,
                        server_region     = server_region,
                        schema_map        = schema_map,
                        condition         = condition,
                        table             = match["table"],
                        column            = col,
                    )
                    if evidence:
                        evidence["db_connection_id"] = state["db_connection_id"]
                        violations.append(evidence)
                        logger.info(
                            "Violation found: %s.%s → rule %s (score %.2f)",
                            match["table"], col, rule_id, entry["score"]
                        )
                except Exception as e:
                    errors.append(f"Enforcement check error {table}.{col} rule {rule_id}: {e}")
                    logger.error("Enforcement error %s.%s rule %s: %s", table, col, rule_id, e)

    return {"violations_found": violations, "errors": errors}


def node_persist_violations(state: ScanState, db: Session) -> dict:
    """Persist all detected violations to MySQL and append audit records."""
    persisted  = []
    errors     = list(state.get("errors", []))
    checkpoint = state.get("langgraph_checkpoint_id")

    for v_data in state.get("violations_found", []):
        try:
            v = persist_violation(db, v_data, checkpoint_id=checkpoint)
            persisted.append(v.id)
        except Exception as e:
            logger.error("Failed to persist violation: %s", e)
            errors.append(f"Violation persist failed: {e}")

    logger.info("Persisted %d violations for DB %s", len(persisted), state["db_connection_id"])
    return {"scan_results": persisted, "errors": errors}


def build_enforcement_graph(db: Session):
    graph = StateGraph(ScanState)

    graph.add_node("filter_relevant_tables", node_filter_relevant_tables)
    graph.add_node("run_enforcement_checks",  lambda state: node_run_enforcement_checks(state, db))
    graph.add_node("persist_violations",      lambda state: node_persist_violations(state, db))

    graph.add_edge(START, "filter_relevant_tables")
    graph.add_edge("filter_relevant_tables", "run_enforcement_checks")
    graph.add_edge("run_enforcement_checks",  "persist_violations")
    graph.add_edge("persist_violations",      END)

    return graph.compile()
