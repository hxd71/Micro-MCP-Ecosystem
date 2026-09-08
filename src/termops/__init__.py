"""Termops — Terminal Operations Agent.

A local, LLM-powered agent that captures terminal errors, analyzes them
with your chosen AI provider, and proposes approval-gated remediation
actions through a CLI-first interface.
"""

from .config import LLMProvider, Settings
from .engine import OpsEngine

__all__ = ["LLMProvider", "OpsEngine", "Settings"]
