"""
Virtual filesystem tools — ported from reference codebase file_tools.py.
Stores structured context (scan results, rule lists, plans) in agent state
across subagent delegations. Prevents context loss between steps.
"""
from typing import Annotated
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from langgraph.prebuilt import InjectedState
from sentinel.states.orchestrator_state import OrchestratorState


@tool
def ls(
    state: Annotated[OrchestratorState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """List all files in the virtual filesystem stored in agent state."""
    files = state.get("files", {})
    if not files:
        return "No files in virtual filesystem."
    return "Files:\n" + "\n".join(f"  - {k} ({len(v)} chars)" for k, v in files.items())


@tool
def write_file(
    file_path: str,
    content: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Write content to a file in the virtual filesystem.
    Use this to persist structured context (JSON rule lists, scan summaries)
    across subagent delegations so nothing is lost between steps.
    """
    def _update(state: OrchestratorState):
        files = dict(state.get("files", {}))
        files[file_path] = content
        return Command(
            update={
                "files": files,
                "messages": [ToolMessage(f"Written {len(content)} chars to {file_path}", tool_call_id=tool_call_id)],
            }
        )
    return _update


@tool
def read_file(
    file_path: str,
    state: Annotated[OrchestratorState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    offset: int = 0,
    limit: int = 2000,
) -> str:
    """Read a file from the virtual filesystem with optional pagination."""
    files = state.get("files", {})
    if file_path not in files:
        return f"File not found: {file_path}"
    lines = files[file_path].split("\n")[offset: offset + limit]
    numbered = [f"{offset + i + 1:4d} | {line}" for i, line in enumerate(lines)]
    return "\n".join(numbered)
