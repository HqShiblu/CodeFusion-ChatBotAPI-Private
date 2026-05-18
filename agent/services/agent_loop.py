"""Main tool-calling agent loop.

Runs only when the cache miss + LLM-knowledge + README-scan steps have all
failed. Implements the strict ordering described in SPECS.md:

    while tool_calls < MAX_LOOP:
        response = llm.chat(messages, tools=TOOL_DEFINITIONS)
        if finish_reason == "stop": break
        for tc in response.tool_calls:
            result = dispatch(tc)
            log_tool_call(...)
            messages.append(tool_result)
    final_answer = response.message.content
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings

from agent.services import llm
from agent.services.tools import TOOL_DEFINITIONS, ToolContext, dispatch_tool_call

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """You are a codebase research agent with tools to explore a GitHub repository.
Repository: {repo_url}
Question: {question}

Rules:
1. Always call get_directory_tree() first.
2. Always call get_previous_findings() before reading any files.
3. Call save_finding() whenever you learn something meaningful about a file. Include line_start/line_end whenever possible.
4. Cite files in your final answer as [[path/to/file.py:line_start-line_end]] (use just [[path/to/file.py]] when you don't have line numbers).
5. Stop calling tools once you can answer confidently. Do not over-explore.
6. If you cannot determine the answer, say so clearly. Do not hallucinate.
7. Your final answer must include specific file paths, function names, and (when possible) line numbers.

You have a hard limit of {max_loop} tool calls. Be efficient.
"""


@dataclass
class AgentResult:
    answer: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    tool_calls_made: int


def run_agent_loop(ctx: ToolContext, question: str) -> AgentResult:
    """Run the tool-calling loop and return the final answer + token totals."""
    max_loop = settings.AGENT_MAX_LOOP

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        repo_url=ctx.repository.url,
        question=question,
        max_loop=max_loop,
    )
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    tool_calls_made = 0
    cumulative_prompt = 0
    cumulative_completion = 0
    cumulative_total = 0
    last_message_content: str | None = None

    while True:
        # If we've hit the cap, force a final answer (no tools available).
        tools_for_round = TOOL_DEFINITIONS if tool_calls_made < max_loop else None
        if tools_for_round is None:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"Tool-call budget exhausted ({max_loop}/{max_loop}). "
                        "Produce your final answer now from the context you have, "
                        "with whatever file/line citations you can offer."
                    ),
                }
            )

        response = llm.chat(messages=messages, tools=tools_for_round)
        cumulative_prompt += response.prompt_tokens
        cumulative_completion += response.completion_tokens
        cumulative_total += response.total_tokens

        msg = response.message
        last_message_content = getattr(msg, "content", None) or last_message_content

        tool_calls = getattr(msg, "tool_calls", None) or []

        # No tool calls? Loop is done.
        if not tool_calls or tools_for_round is None:
            break

        # Append the assistant turn before tool results (required by the API).
        messages.append(llm.message_to_dict(msg))

        for tc in tool_calls:
            tool_calls_made += 1
            tool_name = tc.function.name
            print(
                f"[Tool Call {tool_calls_made}/{max_loop}] "
                f"{tool_name:<22} |  tokens used: {cumulative_total:,}",
                flush=True,
            )
            result = dispatch_tool_call(ctx, tool_name, tc.function.arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": result,
                }
            )

        if response.finish_reason == "stop" and not tool_calls:
            break

    print(f"\nTotal tokens used: {cumulative_total:,}\n", flush=True)

    return AgentResult(
        answer=(last_message_content or "").strip(),
        prompt_tokens=cumulative_prompt,
        completion_tokens=cumulative_completion,
        total_tokens=cumulative_total,
        tool_calls_made=tool_calls_made,
    )
