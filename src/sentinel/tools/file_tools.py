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
    """
    List all files currently stored in the virtual filesystem (agent state).

    Use this tool to orient yourself before starting any multi-step task.
    Shows all file names and their sizes so you know what context already exists
    from previous steps or subagent delegations.

    Returns a list of file paths with character counts, or a message if empty.
    No parameters required — simply call ls() to see all available files.

    Example output:
        Files:
          - rules_retrieved.json (1423 chars)
          - scan_results.json (892 chars)
    """
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
    Create a new file or completely overwrite an existing file in the virtual filesystem.

    Use this tool to persist structured context — JSON rule lists, scan summaries,
    remediation plans — across subagent delegations so nothing is lost between steps.
    Always write intermediate results to a file before delegating to another subagent.

    Parameters:
    - file_path: Path/name for the file, e.g. 'rules_retrieved.json', 'scan_results.json'
    - content: The complete file content as a string. For JSON, serialise with json.dumps first.

    Important: This replaces the entire file content if the file already exists.
    Use read_file first if you need to append or merge content.

    Example usage:
        write_file("rules_retrieved.json", json.dumps(rules_list))
        write_file("scan_summary.txt", "Found 3 violations: ...")
    """
    return Command(
        update={
            "files": {file_path: content},
            "messages": [
                ToolMessage(
                    f"Written {len(content)} chars to {file_path}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


@tool
def read_file(
    file_path: str,
    state: Annotated[OrchestratorState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    offset: int = 0,
    limit: int = 2000,
) -> str:
    """
    Read content from a file in the virtual filesystem with optional pagination.

    Use this tool to retrieve previously saved context — scan results, rule lists,
    remediation plans — before passing them to subagents or making decisions.
    Always read a file before editing or overwriting it.

    Returns file content with line numbers (like cat -n) for easy reference.
    Supports reading large files in chunks using offset and limit to avoid
    flooding the context window.

    Parameters:
    - file_path: Exact path/name of the file to read, e.g. 'scan_results.json'
    - offset: Line number to start reading from (default 0 = beginning)
    - limit: Maximum number of lines to return (default 2000)

    Example usage:
        read_file("rules_retrieved.json")                    # read full file
        read_file("large_scan_results.json", offset=2000)   # read next chunk

    Returns an error message if the file does not exist — use ls() first to check.
    """
    files = state.get("files", {})
    if file_path not in files:
        return f"File not found: {file_path}. Use ls() to see available files."
    lines = files[file_path].split("\n")[offset: offset + limit]
    numbered = [f"{offset + i + 1:4d} | {line}" for i, line in enumerate(lines)]
    return "\n".join(numbered)
