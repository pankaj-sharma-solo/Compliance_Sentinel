"""
Ported directly from reference codebase todo_tool.py.
Adapted for OrchestratorState.
"""
from typing import List, Annotated
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from langgraph.prebuilt import InjectedState
from sentinel.states.orchestrator_state import OrchestratorState, Todo

WRITE_TODOS_DESCRIPTION = """Create and manage structured task lists for tracking progress.

Status values: pending | in_progress | completed | blocked
Rules:
- Only one in_progress task at a time
- Mark completed immediately when done
- Always send the FULL updated list — not just changed items
- If blocked, keep in_progress and add a new task describing the blocker

Format:
{"content": "...", "status": "pending", "id": "t1"}
"""


@tool(description=WRITE_TODOS_DESCRIPTION)
def write_todo(
    todos: List[dict],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Write the full todo list to orchestrator state."""
    return Command(
        update={
            "todos": todos,
            "messages": [ToolMessage(f"Todo list updated: {len(todos)} tasks", tool_call_id=tool_call_id)],
        }
    )


@tool
def read_todo(
    state: Annotated[OrchestratorState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Read the current todo list from orchestrator state."""
    todos = state.get("todos", [])
    if not todos:
        return "No todos yet."
    emoji = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "blocked": "🚫"}
    lines = [
        f"{i+1}. {emoji.get(t.get('status','pending'), '?')} [{t.get('id','?')}] {t['content']} ({t.get('status','pending')})"
        for i, t in enumerate(todos)
    ]
    return "Current TODOs:\n" + "\n".join(lines)
