"""Settings loaded from the environment and .env."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None
    gemini_model: str
    mcp_command: list[str]
    max_iterations: int


def load_settings() -> Settings:
    load_dotenv()
    raw_cmd = os.getenv("DOCKAI_MCP_COMMAND", "").strip()
    # posix=False keeps Windows paths with backslashes intact.
    mcp_command = shlex.split(raw_cmd, posix=False) if raw_cmd else [sys.executable, "-m", "dockai.server"]
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        mcp_command=mcp_command,
        max_iterations=int(os.getenv("DOCKAI_MAX_ITERATIONS", "8")),
    )
