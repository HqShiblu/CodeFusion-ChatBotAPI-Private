"""Semantic cache lookup using pgvector cosine similarity."""

from __future__ import annotations

import logging

from django.conf import settings

from agent.models import ResearchSession

logger = logging.getLogger(__name__)


def find_cached_answer(repo_url: str, question_vector: list[float]):
    """Return the best prior `ResearchSession` for this repo above the
    similarity threshold, or `None`.

    Falls back gracefully when pgvector isn't available (e.g. SQLite tests):
    in that case there is no cache and we return None.
    """
    threshold = settings.SEMANTIC_CACHE_THRESHOLD
    try:
        from pgvector.django import CosineDistance
    except ImportError:
        logger.warning("pgvector not available; semantic cache disabled")
        return None

    try:
        qs = (
            ResearchSession.objects.alias(
                similarity=1 - CosineDistance("question_embedding", question_vector)
            )
            .filter(
                repository__url=repo_url,
                answer__isnull=False,
                similarity__gte=threshold,
            )
            .order_by("-started_at")
        )
        return qs.first()
    except Exception as exc:
        # In test environments using SQLite, pgvector operators raise.
        # That's fine — semantic cache simply doesn't apply there.
        logger.debug("Semantic cache lookup skipped: %s", exc)
        return None
