"""Pytest configuration for the project.

Forces SQLite + disables pgvector-specific behavior so tests run without a
local PostgreSQL or downloaded sentence-transformers model.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("USE_SQLITE", "1")
os.environ.setdefault("EMBEDDING_DIMENSIONS", "384")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()


def pytest_configure(config):  # noqa: D401
    """Pytest hook — anything that must run after Django setup goes here."""
    sys.modules.setdefault("agent_tools_placeholder", type(sys)("agent_tools_placeholder"))
