"""Agent tools: definitions, dispatcher, and automatic ToolCallLog persistence.

Every public tool here is exposed to the LLM via the JSON schemas in
`TOOL_DEFINITIONS`. The dispatcher is responsible for two things:

1. Calling the right Python function with the parsed arguments.
2. Writing a `ToolCallLog` row after every invocation — this is *not* a tool
   the LLM can call; it's implicit on every dispatch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from agent.models import Finding, Repository, ResearchSession, ToolCallLog
from agent.services.github import GitHubAPIError, GitHubClient

logger = logging.getLogger(__name__)


# --- Tool schemas exposed to the LLM ----------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_directory_tree",
            "description": (
                "Fetch the full recursive file tree of the repository. "
                "MUST be the first tool called in any traversal session."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories at a given path in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path inside the repo. Use empty string for repo root."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file's full content (with line numbers prepended). "
                "Use get_file_summary first if the file is large."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path of the file inside the repo."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_summary",
            "description": "Return the first 80 lines of a file. Use before read_file on large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search the repository for a keyword or symbol. Returns a list of matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or symbol to search for."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_finding",
            "description": (
                "Persist a Finding for the current session. Call this whenever you "
                "learn something meaningful about a file. Always set line_start/line_end "
                "when you have them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "note": {"type": "string", "description": "Concise statement of what this file does relative to the question."},
                    "line_start": {"type": "integer"},
                    "line_end": {"type": "integer"},
                },
                "required": ["file_path", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_previous_findings",
            "description": (
                "Return all Findings recorded in prior sessions for this repository. "
                "Call this early to avoid re-reading files that have already been characterized."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_past_sessions",
            "description": "Return summaries of all past ResearchSessions for this repository.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


_OUTPUT_TRUNCATE = 2000


@dataclass
class ToolContext:
    """Container for everything the tool functions need access to.

    A fresh ToolContext is built once per session and passed into the
    dispatcher. The dispatcher does not look at Django settings or globals.
    """

    session: ResearchSession
    repository: Repository
    github: GitHubClient
    file_reads: set[str]                    # files already read this session
    max_file_reads: int

    # Cached state across calls
    _tree_cache: list[str] | None = None


# --- Tool implementations ---------------------------------------------------


def _tool_get_directory_tree(ctx: ToolContext, _args: dict) -> str:
    if ctx._tree_cache is not None:
        paths = ctx._tree_cache
    else:
        entries = ctx.github.get_directory_tree()
        paths = [e.path for e in entries if e.type == "blob"]
        ctx._tree_cache = paths
    listing = "\n".join(paths[:1000])
    extra = "" if len(paths) <= 1000 else f"\n... [{len(paths) - 1000} more paths truncated]"
    return f"Repository tree ({len(paths)} files):\n{listing}{extra}"


def _tool_list_files(ctx: ToolContext, args: dict) -> str:
    path = args.get("path", "") or ""
    items = ctx.github.list_contents(path)
    lines = [f"{i.get('type', '?'):<5} {i.get('path', '')}" for i in items]
    return "\n".join(lines) if lines else "(empty)"


def _tool_read_file(ctx: ToolContext, args: dict) -> str:
    path = args["path"]
    if path in ctx.file_reads:
        # Allow re-reads but warn the LLM so it doesn't waste budget.
        return ctx.github.read_file(path) + "\n\n[note] this file was already read earlier in the session."
    if len(ctx.file_reads) >= ctx.max_file_reads:
        return (
            f"[blocked] File-read cap reached ({ctx.max_file_reads}). "
            "Produce your final answer from the files already collected."
        )
    content = ctx.github.read_file(path)
    ctx.file_reads.add(path)
    return content


def _tool_get_file_summary(ctx: ToolContext, args: dict) -> str:
    return ctx.github.get_file_summary(args["path"])


def _tool_search_code(ctx: ToolContext, args: dict) -> str:
    results = ctx.github.search_code(args["query"])
    if not results:
        return "(no results)"
    return json.dumps(results, indent=2)


def _tool_save_finding(ctx: ToolContext, args: dict) -> str:
    finding = Finding.objects.create(
        session=ctx.session,
        file_path=args["file_path"],
        note=args["note"],
        line_start=args.get("line_start"),
        line_end=args.get("line_end"),
    )
    return f"saved finding {finding.id} for {finding.file_path}"


def _tool_get_previous_findings(ctx: ToolContext, _args: dict) -> str:
    qs = (
        Finding.objects.filter(session__repository=ctx.repository)
        .exclude(session=ctx.session)
        .order_by("-created_at")[:50]
    )
    out = [
        {
            "file_path": f.file_path,
            "line_start": f.line_start,
            "line_end": f.line_end,
            "note": f.note,
        }
        for f in qs
    ]
    if not out:
        return "(no prior findings for this repository)"
    return json.dumps(out, indent=2)


def _tool_list_past_sessions(ctx: ToolContext, _args: dict) -> str:
    qs = (
        ResearchSession.objects.filter(repository=ctx.repository, answer__isnull=False)
        .exclude(id=ctx.session.id)
        .order_by("-started_at")[:25]
    )
    out = [
        {
            "id": str(s.id),
            "question": s.question,
            "source": s.source,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
        }
        for s in qs
    ]
    if not out:
        return "(no past completed sessions for this repository)"
    return json.dumps(out, indent=2)


_DISPATCH = {
    "get_directory_tree": _tool_get_directory_tree,
    "list_files": _tool_list_files,
    "read_file": _tool_read_file,
    "get_file_summary": _tool_get_file_summary,
    "search_code": _tool_search_code,
    "save_finding": _tool_save_finding,
    "get_previous_findings": _tool_get_previous_findings,
    "list_past_sessions": _tool_list_past_sessions,
}


def dispatch_tool_call(ctx: ToolContext, tool_name: str, raw_arguments: str) -> str:
    """Run the tool, log it, and return its (possibly truncated) output."""
    try:
        args = json.loads(raw_arguments) if raw_arguments else {}
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        args = {}

    fn = _DISPATCH.get(tool_name)
    if fn is None:
        output = f"[error] Unknown tool: {tool_name}"
    else:
        try:
            output = fn(ctx, args)
        except GitHubAPIError as exc:
            output = f"[github error] {exc}"
        except Exception as exc:
            logger.exception("Tool %s raised", tool_name)
            output = f"[error] {exc}"

    summary = output if len(output) <= _OUTPUT_TRUNCATE else output[:_OUTPUT_TRUNCATE] + "\n... [truncated]"

    # ToolCallLog is automatic — never exposed to the LLM.
    try:
        ToolCallLog.objects.create(
            session=ctx.session,
            tool_name=tool_name,
            input_params=args,
            output_summary=summary,
        )
    except Exception:
        logger.exception("Failed to persist ToolCallLog for %s", tool_name)

    return summary
