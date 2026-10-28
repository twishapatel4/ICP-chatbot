"""Vercel entry point: exposes the FastAPI app from src/."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from icp_chatbot.main import app  # noqa: E402,F401
