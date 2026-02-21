"""
Core Deep Agent pattern — ported from reference task_tool.py.
_create_task_tool builds a delegation tool that:
  1. Routes to named subagents
  2. Isolates context (no parent history leaks)
  3. Returns result as ToolMessage via Command
"""
from typing import Annotated, NotRequired
from typing_extensions import TypedDict
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langchain.agents import create_agent
from langgraph.prebuilt import InjectedState
from langgraph.types import Command


class SubAgentConfig(TypedDict):
    name: str
    description: str
    prompt: str
    tools: NotRequired[list[str]]


TASK_DESCRIPTION_PREFIX = """Delegate a task to a specialized compliance sub-agent with isolated context.
The sub-agent receives ONLY your task description — no parent conversation history.
Available sub-agents:
{other_agents}
"""


def _create_task_tool(
    all_tools: list,
    subagents: list[SubAgentConfig],
    model: BaseChatModel,
    state_schema,
):
    """
    Factory — creates the task() delegation tool.
    Mirrors reference _create_task_tool exactly.
    """
    # Build tool registry by name
    tools_by_name: dict[str, BaseTool] = {}
    for t in all_tools:
        if not isinstance(t, BaseTool):
            t = tool(t)
        tools_by_name[t.name] = t

    # Build subagent registry
    agents = {}
    for cfg in subagents:
        agent_tools = (
            [tools_by_name[n] for n in cfg["tools"]]
            if "tools" in cfg
            else list(tools_by_name.values())
        )
        agents[cfg["name"]] = create_agent(
            model,
            system_prompt=cfg["prompt"],
            tools=agent_tools,
            state_schema=state_schema,
        )

    agent_list_str = "\n".join(f"  - {c['name']}: {c['description']}" for c in subagents)

    @tool(description=TASK_DESCRIPTION_PREFIX.format(other_agents=agent_list_str))
    def task(
        description: str,
        subagent_type: str,
        state: Annotated[state_schema, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ):
        """
        Delegate to a subagent with context isolation.
        The subagent only sees the task description — not the full orchestrator history.
        This prevents context pollution across workflow steps.
        """
        if subagent_type not in agents:
            valid = list(agents.keys())
            return f"Error: unknown subagent '{subagent_type}'. Valid types: {valid}"

        sub_agent = agents[subagent_type]

        # ── Context isolation (key pattern from reference) ──
        state["messages"] = [{"role": "user", "content": description}]

        result = sub_agent.invoke(state)

        return Command(
            update={
                "files": result.get("files", {}),
                "messages": [
                    ToolMessage(
                        result["messages"][-1].content,
                        tool_call_id=tool_call_id,
                    )
                ],
            }
        )

    return task
