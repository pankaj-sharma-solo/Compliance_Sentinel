"""
Compliance Orchestrator — the Deep Agent supervisor.
This is the missing top layer that drives all 3 complex workflows.

Architecture:
  compliance_orchestrator (supervisor, Deep Agent)
       ↓ task(description, subagent_type)
  ┌────────────────────────────────────────┐
  │  ingestion-agent  │  enforcement-agent  │  remediation-agent  │
  └────────────────────────────────────────┘
       ↓ interrupt() at 3 HITL gates
  Human review → resume

Uses _create_task_tool from reference codebase — exact port.
"""
from datetime import datetime
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent

from sentinel.config import settings
from sentinel.tools.task_tools import _create_task_tool, SubAgentConfig
from sentinel.tools.todo_tools import write_todo, read_todo
from sentinel.tools.file_tools import ls, write_file, read_file
from sentinel.tools.think_tool import think_tool
from sentinel.tools.hitl_tools import (
    request_rule_commit_approval,
    request_remediation_approval,
    request_policy_gap_confirmation,
)
from sentinel.tools.extraction_tools import pass1_extract_candidates, pass2_extract_structured_spans
from sentinel.tools.decomposition_tool import decompose_rule_span
from sentinel.tools.enforcement_tools import (
    check_schema_map_match,
    run_sql_check,
    run_regex_check,
    check_metadata_condition,
    llm_fallback_classify,
)
from sentinel.dao.vector_store import semantic_search, retrieve_relevant_rules
from sentinel.orchestrator_prompt import (
    SUPERVISOR_PROMPT,
    INGESTION_SUBAGENT_PROMPT,
    ENFORCEMENT_SUBAGENT_PROMPT,
    REMEDIATION_SUBAGENT_PROMPT,
)
from sentinel.states.orchestrator_state import OrchestratorState

model = ChatOpenAI(
    model=settings.strong_model,
    temperature=0.0,
    api_key=settings.openai_api_key,
)

# ── Tool pools per subagent (selective assignment — reference pattern) ────────

ingestion_tool_names = [
    "pass1_extract_candidates",
    "pass2_extract_structured_spans",
    "decompose_rule_span",
    "think_tool",
    "write_file",
    "read_file",
]

enforcement_tool_names = [
    "check_schema_map_match",
    "run_sql_check",
    "run_regex_check",
    "check_metadata_condition",
    "llm_fallback_classify",
    "think_tool",
    "write_file",
    "read_file",
]

remediation_tool_names = [
    "check_schema_map_match",
    "run_sql_check",
    "think_tool",
    "write_file",
    "read_file",
]

# ── Subagent configs ──────────────────────────────────────────────────────────

ingestion_subagent: SubAgentConfig = {
    "name": "ingestion-agent",
    "description": "Ingest a compliance PDF, extract rule spans, decompose into violation conditions. Returns structured rule list.",
    "prompt": INGESTION_SUBAGENT_PROMPT,
    "tools": ingestion_tool_names,
}

enforcement_subagent: SubAgentConfig = {
    "name": "enforcement-agent",
    "description": "Run compliance scans on registered databases, retrieve violations, answer questions about compliance status.",
    "prompt": ENFORCEMENT_SUBAGENT_PROMPT,
    "tools": enforcement_tool_names,
}

remediation_subagent: SubAgentConfig = {
    "name": "remediation-agent",
    "description": "Generate and execute remediation plans for known violations. NEVER executes without human approval in state.",
    "prompt": REMEDIATION_SUBAGENT_PROMPT,
    "tools": remediation_tool_names,
}

# ── All tools available to subagents (shared pool) ───────────────────────────

all_subagent_tools = [
    pass1_extract_candidates,
    pass2_extract_structured_spans,
    decompose_rule_span,
    check_schema_map_match,
    run_sql_check,
    run_regex_check,
    check_metadata_condition,
    llm_fallback_classify,
    think_tool,
    write_file,
    read_file,
]

# ── Create the task delegation tool — _create_task_tool from reference ────────

task_tool = _create_task_tool(
    all_subagent_tools,
    [ingestion_subagent, enforcement_subagent, remediation_subagent],
    model,
    OrchestratorState,
)

# ── Supervisor tools (orchestrator-only — not passed to subagents) ────────────

supervisor_tools = [
    # Planning
    write_todo,
    read_todo,
    think_tool,
    # File system (context persistence across delegations)
    ls,
    write_file,
    read_file,
    # HITL gates
    request_rule_commit_approval,
    request_remediation_approval,
    request_policy_gap_confirmation,
    # Delegation
    task_tool,
]

# ── The orchestrator — create_agent mirrors reference orchestrator_agent.py ───

compliance_orchestrator = create_agent(
    model=model,
    tools=supervisor_tools,
    system_prompt=SUPERVISOR_PROMPT,
    state_schema=OrchestratorState,
)
