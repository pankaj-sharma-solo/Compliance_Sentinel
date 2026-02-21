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
    """
    Create or update the agent's TODO list for task planning and progress tracking.

    Use this tool at the START of any multi-step or complex task to break it into
    discrete steps. Call it again whenever a task status changes — mark tasks as
    in_progress when starting, completed immediately when done.

    CRITICAL: Always pass the FULL updated list on every call — not just the changed item.
    The entire list replaces the previous state.

    Parameters:
    - todos: Full list of todo items. Each item must have:
        - id: unique string identifier e.g. "t1", "t2"
        - content: clear, actionable description of the task
        - status: one of "pending" | "in_progress" | "completed" | "blocked"

    Example — creating initial plan:
        write_todo([
            {"id": "t1", "content": "Retrieve active GDPR rules from Qdrant", "status": "pending"},
            {"id": "t2", "content": "Run 3-layer scan on DB #1", "status": "pending"},
            {"id": "t3", "content": "Identify coverage gaps via think_tool", "status": "pending"},
            {"id": "t4", "content": "Confirm gaps with human via interrupt", "status": "pending"}
        ])

    Example — marking t1 complete, starting t2:
        write_todo([
            {"id": "t1", "content": "Retrieve active GDPR rules from Qdrant", "status": "completed"},
            {"id": "t2", "content": "Run 3-layer scan on DB #1", "status": "in_progress"},
            {"id": "t3", "content": "Identify coverage gaps via think_tool", "status": "pending"},
            {"id": "t4", "content": "Confirm gaps with human via interrupt", "status": "pending"}
        ])
    """
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
    """
    Read the current TODO list from orchestrator state to track progress.

    Use this tool after completing each task to remind yourself of the full plan
    and identify what to do next. Essential for maintaining focus across multi-step
    workflows involving subagent delegations and HITL interrupts.

    Call read_todo:
    - After every task completion to see remaining work
    - After resuming from a HITL interrupt to remember where you left off
    - Whenever unsure what the next step is

    Returns the full todo list with status indicators:
        ⏳ pending   — not started yet
        🔄 in_progress — currently being worked on
        ✅ completed — done
        🚫 blocked   — waiting on something, see blocker task

    No parameters required — reads directly from current agent state.

    Example output:
        Current TODOs:
        1. ✅ [t1] Retrieve active GDPR rules from Qdrant (completed)
        2. ✅ [t2] Run 3-layer scan on DB #1 (completed)
        3. 🔄 [t3] Identify coverage gaps via think_tool (in_progress)
        4. ⏳ [t4] Confirm gaps with human via interrupt (pending)
    """
    todos = state.get("todos", [])
    if not todos:
        return "No todos yet. Use write_todo to create a task plan."
    emoji = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "blocked": "🚫"}
    lines = [
        f"{i+1}. {emoji.get(t.get('status', 'pending'), '?')} "
        f"[{t.get('id', '?')}] {t['content']} ({t.get('status', 'pending')})"
        for i, t in enumerate(todos)
    ]
    return "Current TODOs:\n" + "\n".join(lines)
