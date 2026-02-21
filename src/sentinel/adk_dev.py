"""
Launches the ADK Dev UI — the graphical test interface.
Access at http://localhost:8000/dev-ui after running `poetry run dev`
"""
import subprocess
import sys
from pathlib import Path

def launch():
    """Entry point for poetry run dev"""
    agent_dir = str(Path(__file__).parent)
    subprocess.run(
        [sys.executable, "-m", "adk", "web", agent_dir],
        check=True
    )
