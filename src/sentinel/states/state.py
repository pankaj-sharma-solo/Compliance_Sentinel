from typing import TypedDict, Annotated, Optional, Any
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from enum import Enum


# ── Pydantic schemas for structured LLM outputs ──────────────────────────────

class CheckType(str, Enum):
    METADATA = "metadata"       # DB registration metadata (region, encryption flags)
    SQL = "sql"                 # information_schema query
    REGEX = "regex"             # pattern match on column definition / sample
    LLM_FALLBACK = "llm_fallback"


class ViolationCondition(BaseModel):
    """
    A single machine-checkable condition produced during rule decomposition.
    Stored as JSON inside rules.violation_conditions[].
    """
    condition_id: str = Field(..., description="Unique ID within this rule, e.g. 'vc-01'")
    data_category: str = Field(..., description="PII | Financial | Health | Geographic | etc.")
    trigger: str = Field(..., description="storage_outside_eu | no_encryption | retention_exceeded | etc.")
    check_type: CheckType
    requires_encryption: bool = False
    requires_adequacy_decision: bool = False
    sql_check_template: Optional[str] = Field(
        None,
        description="SQL using {table} {column} placeholders, queries information_schema"
    )
    regex_pattern: Optional[str] = Field(None, description="Regex to detect pattern in column value samples")
    severity: str = Field("MEDIUM", description="LOW | MEDIUM | HIGH | CRITICAL")
    remediation_template: str = Field(..., description="Short fix instruction, e.g. MASK_PII_col")


class DecomposedRule(BaseModel):
    """Full structured output from the decomposition LLM call."""
    rule_id: str
    rule_text: str
    source_doc: str
    article_ref: Optional[str]
    obligation_type: str = Field(..., description="PROHIBITION | REQUIREMENT | PERMISSION")
    data_subject_scope: list[str] = Field(default_factory=list)
    violation_conditions: list[ViolationCondition]


class TableRelevance(BaseModel):
    """Output from schema-to-rule semantic match — used to filter tables pre-scan."""
    table_name: str
    relevance_score: float = Field(..., ge=0.0, le=1.0)
    matched_rule_ids: list[str]
    reason: str


class SchemaColumnClassification(BaseModel):
    """Single column classification produced by the schema mapping agent."""
    table_name: str
    column_name: str
    data_type: str
    compliance_category: str = Field(
        ...,
        description="PII_contact | PII_gov_id | Financial | Health | Geographic | Internal | None"
    )
    sensitivity: str = Field(..., description="HIGH | MEDIUM | LOW | NONE")
    reason: str


class SchemaMap(BaseModel):
    """Full schema map for a registered DB — stored in database_connections.schema_map."""
    db_connection_id: int
    classifications: list[SchemaColumnClassification]


# ── LangGraph State definitions ───────────────────────────────────────────────

class IngestionState(TypedDict):
    """State for the PDF ingestion pipeline."""
    messages: Annotated[list, add_messages]
    pdf_path: str
    source_doc: str
    raw_chunks: list[dict]                      # {page, text, section_header}
    candidate_spans: list[dict]                 # Pass-1 output: {span_text, page_ref, section}
    decomposed_rules: list[DecomposedRule]
    persisted_rule_ids: list[str]
    errors: list[str]
    langgraph_checkpoint_id: Optional[str]


class ScanState(TypedDict):
    """State for the enforcement / scanning pipeline."""
    messages: Annotated[list, add_messages]
    db_connection_id: int
    connection_string: str                      # decrypted at runtime
    server_region: str
    schema_map: dict                            # {table: {col: {category, sensitivity}}}
    relevant_rules: list[dict]                  # top-k from Qdrant
    scan_results: list[dict]                    # raw per-table check outputs
    violations_found: list[dict]
    errors: list[str]
    langgraph_checkpoint_id: Optional[str]


class SchemaMappingState(TypedDict):
    """State for the one-time schema classification agent (run at DB registration)."""
    messages: Annotated[list, add_messages]
    db_connection_id: int
    connection_string: str
    raw_schema_info: list[dict]                 # column defs from information_schema
    schema_map: Optional[SchemaMap]
    errors: list[str]
