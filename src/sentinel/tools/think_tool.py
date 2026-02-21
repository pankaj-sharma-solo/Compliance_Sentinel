from langchain_core.tools import tool


@tool
def think_tool(reflection: str) -> str:
    """
    Strategic reflection tool — ported from reference codebase.
    Use after every subagent delegation to:
    1. Analyse what the previous step returned
    2. Validate data integrity
    3. Decide the next sequential action OR identify what's blocked
    4. Determine if human review is needed before proceeding

    This creates a deliberate quality-control pause in the workflow.
    """
    return f"Reflection recorded: {reflection}"
