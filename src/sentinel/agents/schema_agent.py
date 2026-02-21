"""
Schema mapping agent — one-time run at DB registration.
Classifies every column in the registered DB into compliance categories.
LLM cost paid ONCE. Output stored in database_connections.schema_map.
"""
from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI
from sqlalchemy import text, create_engine
from sentinel.states.state import SchemaMappingState, SchemaColumnClassification, SchemaMap
from sentinel.config import settings
import logging
import json

logger = logging.getLogger(__name__)

_llm = ChatOpenAI(model=settings.strong_model, temperature=0.0, api_key=settings.openai_api_key)


def node_fetch_schema_info(state: SchemaMappingState) -> dict:
    """Query information_schema to get all table/column definitions for the target DB."""
    try:
        engine = create_engine(state["connection_string"], pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                ORDER BY TABLE_NAME, ORDINAL_POSITION
            """)).fetchall()
        schema_info = [dict(r._mapping) for r in rows]
        return {"raw_schema_info": schema_info}
    except Exception as e:
        logger.error("Schema fetch failed: %s", e)
        return {"errors": [f"Schema fetch failed: {e}"]}


def node_classify_columns(state: SchemaMappingState) -> dict:
    """
    LLM classifies each column into a compliance category.
    Batched by table to reduce LLM calls.
    """
    if not state.get("raw_schema_info"):
        return {"errors": ["No schema info to classify"]}

    # Group by table
    tables: dict[str, list] = {}
    for row in state["raw_schema_info"]:
        t = row["TABLE_NAME"]
        tables.setdefault(t, []).append(row)

    classifications = []
    for table_name, columns in tables.items():
        cols_desc = "\n".join(
            f"  - {c['COLUMN_NAME']} ({c['DATA_TYPE']}, {c.get('COLUMN_COMMENT', '')})"
            for c in columns
        )
        prompt = (
            f"Classify each column of table `{table_name}` into a compliance category.\n"
            f"Categories: PII_contact | PII_gov_id | Financial | Health | Geographic | Internal | None\n"
            f"Sensitivity: HIGH | MEDIUM | LOW | NONE\n\n"
            f"Columns:\n{cols_desc}\n\n"
            f"Return a JSON array: "
            f'[{{"column_name": "...", "compliance_category": "...", "sensitivity": "...", "reason": "..."}}]'
        )
        try:
            response = _llm.invoke(prompt)
            items = json.loads(response.content)
            for item in items:
                classifications.append(SchemaColumnClassification(
                    table_name=table_name,
                    column_name=item["column_name"],
                    data_type=next(
                        (c["DATA_TYPE"] for c in columns if c["COLUMN_NAME"] == item["column_name"]),
                        "unknown"
                    ),
                    compliance_category=item["compliance_category"],
                    sensitivity=item["sensitivity"],
                    reason=item.get("reason", ""),
                ))
        except Exception as e:
            logger.warning("Column classification failed for table %s: %s", table_name, e)

    schema_map = SchemaMap(
        db_connection_id=state["db_connection_id"],
        classifications=classifications,
    )
    return {"schema_map": schema_map}


def build_schema_agent(connection_string: str, db_connection_id: int):
    """Build and compile the schema mapping StateGraph."""
    graph = StateGraph(SchemaMappingState)
    graph.add_node("fetch_schema", node_fetch_schema_info)
    graph.add_node("classify_columns", node_classify_columns)
    graph.add_edge(START, "fetch_schema")
    graph.add_edge("fetch_schema", "classify_columns")
    graph.add_edge("classify_columns", END)
    return graph.compile()
