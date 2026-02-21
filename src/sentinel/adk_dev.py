import subprocess
import sys
from pathlib import Path


def launch():
    """Launch ADK web UI — uses console script entry point, not -m adk."""
    parent_dir = Path(__file__).parent.parent  # src/

    result = subprocess.run(
        ["adk", "web"],
        cwd=str(parent_dir),
        capture_output=False,
    )
    return result.returncode
