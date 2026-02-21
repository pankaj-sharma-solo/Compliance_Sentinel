"""
ADK bridge for Compliance Sentinel.

Uses google.adk.agents.LangGraphAgent — the official ADK bridge for LangGraph.
This gives you:
  - adk web       → Dev UI for testing the orchestrator conversationally
  - adk run       → CLI interaction
  - adk deploy    → deploy to Vertex AI Agent Engine

The LangGraph orchestrator (compliance_orchestrator) remains the source of truth.
ADK is the deployment/interaction surface.
"""
from google.adk.agents.langgraph_agent import LangGraphAgent
from sentinel.agents.compliance_orchestrator import compliance_orchestrator
from sentinel.checkpointer import get_checkpointer

# ADK requires a module-level variable named `root_agent`
# The Dev UI and CLI discover the agent via this convention
root_agent = LangGraphAgent(
    name="compliance_sentinel",
    description=(
        "AI-native compliance governance agent. "
        "Handles policy review, violation remediation, "
        "and natural language compliance queries with human-in-the-loop approval gates."
    ),
    graph=compliance_orchestrator.compile(
        checkpointer=get_checkpointer()
    ),
)
