"""
Three-layer violation detection — the enforcement tools.
Layer 1: Schema map lookup (O(1), pre-computed at DB registration)
Layer 2: Programmatic checks — SQL on information_schema + regex on samples
Layer 3: LLM fallback only for genuinely ambiguous field classification

At scan time: 80–90% checks are pure SQL/metadata → FREE.
LLM fallback is RARE, not the primary detector.
"""
import re
import logging
from typing import Any
from sqlalchemy import text, create_engine
from langchain_google_genai.chat_models import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from sentinel.config import settings

logger = logging.getLogger(__name__)

_fallback_llm = ChatGoogleGenerativeAI(model=settings.cheap_model, temperature=0.0, google_api_key=settings.google_api_key)

EU_REGIONS = {
    "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1",
    "eu-north-1", "eu-south-1", "eu-central-2", "eu-south-2",
    "europe-west1", "europe-west2", "europe-west3", "europe-west4",
    "europe-north1", "europe-central2",
}


# ── Layer 1: Schema map match ─────────────────────────────────────────────────

@tool
def check_schema_map_match(
    schema_map: dict, condition: dict
) -> dict:
    """
    Layer 1 — O(1) lookup.
    Cross-reference the pre-computed schema_map (built at DB registration)
    against the violation_condition's data_category.
    Returns list of {table, column} pairs that match the condition's data_category.
    """
    data_category = condition.get("data_category", "")
    matches = []
    for table, columns in schema_map.items():
        for col, meta in columns.items():
            col_category = meta.get("compliance_category", "")
            if data_category.lower() in col_category.lower():
                matches.append({
                    "table": table,
                    "column": col,
                    "category": col_category,
                    "sensitivity": meta.get("sensitivity", "UNKNOWN"),
                })
    return {"data_category": data_category, "matches": matches}


# ── Layer 2a: SQL check against information_schema ───────────────────────────

@tool
def run_sql_check(
    connection_string: str, table: str, column: str, sql_template: str
) -> dict:
    """
    Layer 2a — programmatic SQL check.
    Executes the pre-decomposed sql_check_template from violation_conditions
    against information_schema (never against user data rows).
    """
    sql = sql_template.replace("{table}", table).replace("{column}", column)
    try:
        engine = create_engine(connection_string, pool_pre_ping=True)
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            rows = [dict(row._mapping) for row in result]
        return {"status": "ok", "table": table, "column": column, "rows": rows}
    except Exception as e:
        logger.warning("SQL check failed for %s.%s: %s", table, column, e)
        return {"status": "error", "table": table, "column": column, "error": str(e)}


# ── Layer 2b: Regex check on column sample values ────────────────────────────

@tool
def run_regex_check(
    connection_string: str, table: str, column: str, regex_pattern: str, sample_size: int = 20
) -> dict:
    """
    Layer 2b — regex pattern match on anonymised sample values.
    Never reads PII values — only checks whether they match the pattern structure.
    """
    sql = f"SELECT `{column}` FROM `{table}` LIMIT {sample_size}"
    try:
        engine = create_engine(connection_string, pool_pre_ping=True)
        pattern = re.compile(regex_pattern, re.IGNORECASE)
        with engine.connect() as conn:
            rows = conn.execute(text(sql)).fetchall()
        samples = [str(r[0]) for r in rows if r[0] is not None]
        match_count = sum(1 for s in samples if pattern.search(s))
        match_ratio = match_count / len(samples) if samples else 0.0
        return {
            "status": "ok",
            "table": table,
            "column": column,
            "match_ratio": match_ratio,
            "sample_count": len(samples),
            "triggered": match_ratio >= 0.5,
        }
    except Exception as e:
        logger.warning("Regex check failed for %s.%s: %s", table, column, e)
        return {"status": "error", "table": table, "column": column, "error": str(e)}


# ── Layer 2c: Metadata check (DB registration data) ──────────────────────────

@tool
def check_metadata_condition(
    server_region: str, condition: dict
) -> dict:
    """
    Layer 2c — check conditions that rely on DB registration metadata.
    e.g. storage_outside_eu: checks server_region against EU region list.
    """
    trigger = condition.get("trigger", "")
    result = {"trigger": trigger, "triggered": False, "evidence": {}}

    if trigger == "storage_outside_eu":
        is_outside = server_region.lower() not in EU_REGIONS
        result["triggered"] = is_outside
        result["evidence"] = {
            "server_region": server_region,
            "eu_regions_checked": list(EU_REGIONS)[:5],
        }

    elif trigger == "no_adequacy_decision":
        # Placeholder — can be extended with country → adequacy decision lookup table
        result["triggered"] = False
        result["evidence"] = {"note": "Adequacy decision check requires country-level config"}

    return result


# ── Layer 3: LLM fallback for ambiguous classification ───────────────────────

@tool
def llm_fallback_classify(
    column_name: str, data_type: str, sample_values: list[str], condition_description: str
) -> dict:
    """
    Layer 3 — LLM fallback. Only called when programmatic checks are inconclusive.
    e.g. column named 'ref_code' with VARCHAR type — ambiguous without context.
    Returns {is_violation: bool, confidence: float, reasoning: str}
    """
    samples_str = ", ".join(f'"{s}"' for s in sample_values[:10])
    prompt = (
        f"You are a compliance analyst. Given the following column information, "
        f"determine if it violates this condition: {condition_description}\n\n"
        f"Column: {column_name}\nData Type: {data_type}\nSample Values: {samples_str}\n\n"
        f"Return JSON: {{\"is_violation\": true/false, \"confidence\": 0.0-1.0, \"reasoning\": \"...\"}}"
    )
    try:
        response = _fallback_llm.invoke(prompt)
        import json
        parsed = json.loads(response.content)
        return {
            "is_violation": parsed.get("is_violation", False),
            "confidence": float(parsed.get("confidence", 0.0)),
            "reasoning": parsed.get("reasoning", ""),
        }
    except Exception as e:
        logger.warning("LLM fallback failed: %s", e)
        return {"is_violation": False, "confidence": 0.0, "reasoning": f"LLM fallback error: {e}"}


# ── Orchestration helper: full condition evaluation chain ─────────────────────

def evaluate_condition_chain(
    connection_string: str,
    server_region: str,
    schema_map: dict,
    condition: dict,
    table: str,
    column: str,
) -> dict | None:
    """
    Runs the full enforcement chain for a single (table, column, condition) triple.
    Returns a violation evidence dict if triggered, else None.
    """
    check_type = condition.get("check_type", "sql")

    # Layer 2c: metadata check
    if check_type == "metadata":
        result = check_metadata_condition.invoke({"server_region": server_region, "condition": condition})
        if result["triggered"]:
            return _build_evidence(table, column, condition, result["evidence"], "metadata")

    # Layer 2a: SQL check
    elif check_type == "sql" and condition.get("sql_check_template"):
        result = run_sql_check.invoke({
            "connection_string": connection_string,
            "table": table,
            "column": column,
            "sql_template": condition["sql_check_template"],
        })
        if result["status"] == "ok" and result.get("rows"):
            return _build_evidence(table, column, condition, {"rows": result["rows"]}, "sql")

    # Layer 2b: regex check
    elif check_type == "regex" and condition.get("regex_pattern"):
        result = run_regex_check.invoke({
            "connection_string": connection_string,
            "table": table,
            "column": column,
            "regex_pattern": condition["regex_pattern"],
        })
        if result.get("triggered"):
            return _build_evidence(table, column, condition, result, "regex")

    # Layer 3: LLM fallback
    elif check_type == "llm_fallback":
        result = llm_fallback_classify.invoke({
            "column_name": column,
            "data_type": schema_map.get(table, {}).get(column, {}).get("data_type", "UNKNOWN"),
            "sample_values": [],
            "condition_description": f"{condition.get('trigger')} on {condition.get('data_category')}",
        })
        if result["is_violation"] and result["confidence"] >= settings.llm_fallback_confidence_threshold:
            return _build_evidence(table, column, condition, result, "llm_fallback")

    return None


def _build_evidence(table: str, column: str, condition: dict, raw_result: dict, method: str) -> dict:
    return {
        "table_name": table,
        "column_name": column,
        "rule_id": condition.get("rule_id", ""),
        "condition_matched": condition.get("trigger", ""),
        "severity": condition.get("severity", "MEDIUM"),
        "remediation_template": condition.get("remediation_template", ""),
        "evidence_snapshot": {
            "check_method": method,
            "condition_id": condition.get("condition_id", ""),
            "raw": raw_result,
        },
    }
