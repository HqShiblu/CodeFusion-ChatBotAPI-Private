"""End-to-end pipeline tests with mocked LLM, embeddings, and GitHub.

The pipeline orders matter: cache > llm_knowledge > readme_scan > full_traversal.
We verify each branch fires under the right preconditions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from django.test import TestCase

from agent.models import Finding, Repository, ResearchSession, ToolCallLog
from agent.services import pipeline


# A canonical 384-d zero vector for use everywhere the embedding is needed.
_VEC = [0.0] * 384


def _stub_embed(text: str) -> list[float]:
    return list(_VEC)


class _StubLLMResponse:
    def __init__(self, content: str, *, tool_calls=None, finish_reason="stop"):
        class _Msg:
            pass

        self.message = _Msg()
        self.message.role = "assistant"
        self.message.content = content
        self.message.tool_calls = tool_calls
        self.finish_reason = finish_reason
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.total_tokens = 15


class PipelineSelfAssessmentBranchTests(TestCase):
    @mock.patch("agent.services.pipeline.embeddings.embed", side_effect=_stub_embed)
    @mock.patch("agent.services.pipeline.find_cached_answer", return_value=None)
    @mock.patch("agent.services.pipeline.llm.chat")
    def test_llm_knowledge_branch(self, mock_chat, _mock_cache, _mock_embed):
        mock_chat.return_value = _StubLLMResponse(
            '{"confident": true, "answer": "Django is a Python web framework."}',
        )
        result = pipeline.run_pipeline(
            "https://github.com/django/django/",
            "What language is Django written in?",
        )
        self.assertEqual(result.source, ResearchSession.SOURCE_LLM_KNOWLEDGE)
        self.assertIn("Python", result.answer)
        self.assertEqual(result.session.source, ResearchSession.SOURCE_LLM_KNOWLEDGE)
        self.assertIsNotNone(result.session.completed_at)
        repo = Repository.objects.get(url="https://github.com/django/django")
        self.assertIsNotNone(repo.last_analyzed_at)

    @mock.patch("agent.services.pipeline.embeddings.embed", side_effect=_stub_embed)
    @mock.patch("agent.services.pipeline.find_cached_answer", return_value=None)
    @mock.patch("agent.services.pipeline.llm.chat")
    def test_low_confidence_falls_through_to_full_traversal(self, mock_chat, *_):
        # Self-assessment says "no" → README scan won't apply (not summary) → agent loop.
        chat_calls = []

        def fake_chat(messages, tools=None, tool_choice=None, temperature=0.0):
            chat_calls.append(tools)
            if not chat_calls or len(chat_calls) == 1:
                return _StubLLMResponse('{"confident": false, "answer": null}')
            # Inside the agent loop: produce a final answer immediately.
            return _StubLLMResponse(
                "The answer is in [[src/main.py:10-20]].",
                finish_reason="stop",
            )

        mock_chat.side_effect = fake_chat

        # Stub the GitHubClient constructor so it doesn't try to hit github.com
        with mock.patch("agent.services.pipeline.GitHubClient") as mock_gh:
            mock_gh.return_value = mock.MagicMock()
            result = pipeline.run_pipeline(
                "https://github.com/x/y",
                "How does the retry logic work inside this code?",
            )
        self.assertEqual(result.source, ResearchSession.SOURCE_FULL_TRAVERSAL)
        self.assertEqual(len(result.references), 1)
        self.assertEqual(result.references[0]["file_path"], "src/main.py")
        self.assertEqual(result.references[0]["line_start"], 10)


class PipelineCacheBranchTests(TestCase):
    @mock.patch("agent.services.pipeline.embeddings.embed", side_effect=_stub_embed)
    def test_cache_hit_short_circuits(self, _mock_embed):
        repo = Repository.objects.create(url="https://github.com/a/b", name="a/b")
        prior = ResearchSession.objects.create(
            repository=repo,
            question="How does X work?",
            answer="It works via Y, see [[src/x.py]].",
            source=ResearchSession.SOURCE_FULL_TRAVERSAL,
            completed_at=datetime.now(timezone.utc),
        )
        with mock.patch("agent.services.pipeline.find_cached_answer", return_value=prior):
            with mock.patch("agent.services.pipeline.llm.chat") as mock_chat:
                result = pipeline.run_pipeline(
                    "https://github.com/a/b/",
                    "How does X function in this repo?",
                )
        self.assertEqual(result.source, ResearchSession.SOURCE_CACHE)
        self.assertEqual(result.answer, prior.answer)
        # LLM must not have been called when we got a cache hit.
        mock_chat.assert_not_called()
