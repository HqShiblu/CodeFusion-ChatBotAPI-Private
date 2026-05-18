"""Tests for the agent tool dispatcher.

Exercises the dispatcher with a fake GitHub client so we can verify:
    - ToolCallLog rows are written automatically
    - save_finding persists Finding rows tied to the session
    - get_previous_findings pulls notes from *other* sessions on the same repo
    - read_file is gated by the file-read cap
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from django.test import TestCase

from agent.models import Finding, Repository, ResearchSession, ToolCallLog
from agent.services.tools import ToolContext, dispatch_tool_call


@dataclass
class _TreeEntry:
    path: str
    type: str = "blob"
    size: int | None = 100


@dataclass
class FakeGitHubClient:
    tree: list[_TreeEntry] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)

    def get_directory_tree(self):
        return list(self.tree)

    def list_contents(self, path: str):
        return [{"path": p.path, "type": p.type} for p in self.tree if p.path.startswith(path)]

    def read_file(self, path: str) -> str:
        return self.files.get(path, "")

    def get_file_summary(self, path: str, max_lines: int = 80) -> str:
        return "\n".join(self.read_file(path).splitlines()[:max_lines])

    def search_code(self, query: str, max_results: int = 10):
        return []


def _make_ctx(extra_files: dict | None = None) -> ToolContext:
    repo = Repository.objects.create(url="https://github.com/x/y", name="x/y")
    session = ResearchSession.objects.create(repository=repo, question="q")
    gh = FakeGitHubClient(
        tree=[_TreeEntry(path="src/main.py"), _TreeEntry(path="README.md")],
        files={"README.md": "hello\nworld", "src/main.py": "def f():\n    pass", **(extra_files or {})},
    )
    return ToolContext(
        session=session,
        repository=repo,
        github=gh,
        file_reads=set(),
        max_file_reads=2,
    )


class ToolDispatchTests(TestCase):
    def test_get_directory_tree_logs_tool_call(self):
        ctx = _make_ctx()
        out = dispatch_tool_call(ctx, "get_directory_tree", "{}")
        self.assertIn("src/main.py", out)
        self.assertIn("README.md", out)
        log = ToolCallLog.objects.get(session=ctx.session)
        self.assertEqual(log.tool_name, "get_directory_tree")

    def test_save_finding_creates_row(self):
        ctx = _make_ctx()
        out = dispatch_tool_call(
            ctx,
            "save_finding",
            json.dumps(
                {
                    "file_path": "src/main.py",
                    "note": "entry point",
                    "line_start": 1,
                    "line_end": 2,
                }
            ),
        )
        self.assertIn("saved finding", out)
        self.assertEqual(Finding.objects.filter(session=ctx.session).count(), 1)
        f = Finding.objects.first()
        self.assertEqual(f.file_path, "src/main.py")
        self.assertEqual(f.line_start, 1)

    def test_get_previous_findings_excludes_current_session(self):
        ctx = _make_ctx()
        prior_session = ResearchSession.objects.create(
            repository=ctx.repository, question="prior?"
        )
        Finding.objects.create(
            session=prior_session, file_path="src/main.py", note="prior insight"
        )
        Finding.objects.create(
            session=ctx.session, file_path="src/main.py", note="current insight"
        )

        out = dispatch_tool_call(ctx, "get_previous_findings", "{}")
        self.assertIn("prior insight", out)
        self.assertNotIn("current insight", out)

    def test_read_file_cap(self):
        ctx = _make_ctx(extra_files={"a.py": "1", "b.py": "2", "c.py": "3"})
        ctx.max_file_reads = 2

        dispatch_tool_call(ctx, "read_file", json.dumps({"path": "a.py"}))
        dispatch_tool_call(ctx, "read_file", json.dumps({"path": "b.py"}))
        out = dispatch_tool_call(ctx, "read_file", json.dumps({"path": "c.py"}))
        self.assertIn("File-read cap reached", out)

    def test_unknown_tool(self):
        ctx = _make_ctx()
        out = dispatch_tool_call(ctx, "nope", "{}")
        self.assertIn("Unknown tool", out)
