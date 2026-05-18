"""HTTP views for the Codebase Research Agent.

Endpoints:

    POST /api/sessions/                    -> start a new research session
    GET  /api/sessions/<uuid>/             -> retrieve full session detail
    GET  /api/sessions/?repo=<url>         -> list sessions for a repo
    GET  /api/repos/                       -> list all researched repositories
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from agent.models import Repository, ResearchSession
from agent.serializers import (
    CreateSessionRequestSerializer,
    RepositorySerializer,
    ResearchSessionDetailSerializer,
    ResearchSessionSummarySerializer,
)
from agent.services.pipeline import run_pipeline
from agent.services.sanitizer import InvalidRepositoryURL, sanitize_repo_url

logger = logging.getLogger(__name__)


@api_view(["GET", "POST"])
def sessions_list_create(request):
    if request.method == "POST":
        serializer = CreateSessionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = run_pipeline(
                repository_url=serializer.validated_data["repository_url"],
                question=serializer.validated_data["question"],
            )
        except InvalidRepositoryURL as exc:
            return Response(
                {"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as exc:
            logger.exception("Pipeline crashed")
            return Response(
                {"error": f"internal error: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "session_id": str(result.session.id),
                "repository_url": result.session.repository.url,
                "question": result.session.question,
                "answer": result.answer,
                "source": result.source,
                "references": result.references,
                "token_usage": result.token_usage,
                "created_at": result.session.started_at.isoformat(),
                "completed_at": (
                    result.session.completed_at.isoformat()
                    if result.session.completed_at
                    else None
                ),
            },
            status=status.HTTP_201_CREATED,
        )

    repo_url = request.query_params.get("repo")
    qs = ResearchSession.objects.select_related("repository")
    if repo_url:
        try:
            parsed = sanitize_repo_url(repo_url)
        except InvalidRepositoryURL as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(repository__url=parsed.url)
    qs = qs.order_by("-started_at")[:100]
    data = ResearchSessionSummarySerializer(qs, many=True).data
    return Response(data)


@api_view(["GET"])
def session_detail(request, session_id):
    try:
        session = (
            ResearchSession.objects.select_related("repository")
            .prefetch_related("findings", "tool_calls")
            .get(pk=session_id)
        )
    except ResearchSession.DoesNotExist:
        return Response({"error": "session not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(ResearchSessionDetailSerializer(session).data)


@api_view(["GET"])
def repos_list(_request):
    qs = Repository.objects.order_by("-last_analyzed_at", "-created_at")[:200]
    return Response(RepositorySerializer(qs, many=True).data)
