# Codebase Research Agent

A Django REST Framework AI agent that accepts a public GitHub repository URL and a
natural-language question, intelligently traverses the codebase using the GitHub
API via **tool calling**, and returns a precise answer with **references to
specific files, functions, and line numbers**. Every research session is
persisted to PostgreSQL so it can be retrieved, reviewed, and built upon later.

The pipeline cost-ladders questions through four progressively more expensive
sources before doing real work:

```
cache  →  llm_knowledge  →  readme_scan  →  full_traversal
```

See [`DECISIONS.md`](./DECISIONS.md) for the architectural rationale and
[`SPECS.md`](./SPECS.md) for the full specification.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django 5 + Django REST Framework |
| Database | PostgreSQL with the `pgvector` extension |
| Vector search | pgvector cosine similarity on question embeddings |
| LLM | Any OpenAI-compatible API that supports tool calling |
| Embeddings | `sentence-transformers` (`all-MiniLM-L6-v2`, 384-d, local) |
| GitHub data | GitHub REST API (token-authenticated) |

---

## Project Layout

```
.
├── manage.py
├── requirements.txt
├── .env.example
├── README.md
├── DECISIONS.md
├── SPECS.md
├── conftest.py
├── fixtures/
│   └── sample_sessions.json
├── config/
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
└── agent/
    ├── models.py
    ├── serializers.py
    ├── views.py
    ├── urls.py
    ├── migrations/
    ├── management/commands/
    │   └── seed_sample.py
    ├── tests/
    │   ├── test_sanitizer.py
    │   ├── test_classifier.py
    │   ├── test_references.py
    │   ├── test_tools.py
    │   └── test_pipeline.py
    └── services/
        ├── sanitizer.py        # URL normalization
        ├── embeddings.py       # local sentence-transformers
        ├── cache.py            # semantic cache via pgvector
        ├── github.py           # GitHub REST client
        ├── classifier.py       # question theme & path ranking
        ├── llm.py              # OpenAI-compatible chat wrapper
        ├── tools.py            # agent tools + auto ToolCallLog
        ├── agent_loop.py       # tool-calling loop + token logging
        └── pipeline.py         # top-level orchestrator
```

---

## Setup

### 1. Clone & create a virtual environment

```bash
git clone <this-repo-url>
cd "CodeFusion Chat API"
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Set up PostgreSQL with pgvector

```bash
# Once per Postgres instance:
psql -U postgres -c "CREATE DATABASE github_analyzer;"
psql -U postgres -d github_analyzer -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### 3. Configure `.env`

```bash
cp .env.example .env
# Edit .env: set LLM_API_KEY, GITHUB_TOKEN, DB_* values.
```

Key environment variables:

| Variable | Purpose |
|---|---|
| `LLM_BASE_URL` | OpenAI-compatible endpoint (`https://api.openai.com/v1`, Groq, local Ollama, …) |
| `LLM_MODEL_NAME` | Model that supports tool/function calling (e.g. `gpt-4o`) |
| `LLM_API_KEY` | API key for the LLM provider |
| `EMBEDDING_MODEL_NAME` | `all-MiniLM-L6-v2` (384-d) by default |
| `GITHUB_TOKEN` | Personal access token — required for >60 req/hr |
| `AGENT_MAX_LOOP` | Hard cap on agent tool calls per session (default 30) |
| `AGENT_MAX_FILE_READS` | Hard cap on file reads per session (default 15) |
| `DB_*` | PostgreSQL connection details |

### 4. Migrate & run

```bash
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Optional — seed a demo session so the GET endpoints return something even
before you run the agent:

```bash
python manage.py seed_sample
# or load the fixture (after running migrate):
python manage.py loaddata fixtures/sample_sessions.json
```

---

## API

### `POST /api/sessions/` — start a new research session

```bash
curl -X POST http://localhost:8000/api/sessions/ \
  -H "Content-Type: application/json" \
  -d '{
    "repository_url": "https://github.com/tiangolo/fastapi",
    "question": "How does FastAPI handle dependency injection internally?"
  }'
```

Response (truncated):

```json
{
  "session_id": "…",
  "repository_url": "https://github.com/tiangolo/fastapi",
  "question": "How does FastAPI handle dependency injection internally?",
  "answer": "FastAPI resolves dependencies via … [[fastapi/dependencies/utils.py:42-78]] …",
  "source": "full_traversal",
  "references": [
    {
      "file_path": "fastapi/dependencies/utils.py",
      "line_start": 42,
      "line_end": 78,
      "note": "Core dependency resolver entry point."
    }
  ],
  "token_usage": {"prompt_tokens": 3200, "completion_tokens": 410, "total_tokens": 3610},
  "created_at": "…",
  "completed_at": "…"
}
```

### `GET /api/sessions/<uuid>/` — full session detail

Includes every `Finding` recorded and every `ToolCallLog` row.

```bash
curl http://localhost:8000/api/sessions/<session-id>/
```

### `GET /api/sessions/?repo=<url>` — past sessions for a repo

```bash
curl "http://localhost:8000/api/sessions/?repo=https://github.com/tiangolo/fastapi"
```

### `GET /api/repos/` — all researched repos

```bash
curl http://localhost:8000/api/repos/
```

---

## What the agent prints

Every tool call is logged to stdout in real time. After the loop, the
cumulative token total is printed:

```
[Tool Call 1/30] get_directory_tree     |  tokens used: 412
[Tool Call 2/30] get_previous_findings  |  tokens used: 698
[Tool Call 3/30] read_file              |  tokens used: 1,842
[Tool Call 4/30] save_finding           |  tokens used: 2,109
[Tool Call 5/30] read_file              |  tokens used: 3,610

Total tokens used: 3,610
```

The same `total_tokens` is also persisted to `ResearchSession.token_usage`.

---

## Testing

Tests run on SQLite without any external services. Embeddings, the LLM, and
the GitHub API are mocked.

```bash
# pytest is convenient; pytest-django picks up conftest.py automatically.
pip install pytest pytest-django
pytest -q
```

Or via Django's test runner:

```bash
USE_SQLITE=1 python manage.py test agent
```

Key tests:
- `test_sanitizer.py` — URL normalization edge cases
- `test_classifier.py` — question theme detection + path ranking
- `test_references.py` — `[[path:line_start-line_end]]` parsing
- `test_tools.py` — dispatcher + automatic ToolCallLog writes
- `test_pipeline.py` — cache hit, llm_knowledge, and full_traversal branches

---

## Operational notes

- **Synchronous request/response.** Long-running sessions on huge repos will
  hit the HTTP timeout. Moving the agent loop to Celery would be the natural
  next step.
- **GitHub rate limits.** Unauthenticated callers get 60 req/hr; set
  `GITHUB_TOKEN` for 5000 req/hr.
- **Cost ceiling.** `AGENT_MAX_LOOP` is the only hard cap on tool calls. Tune
  it in `.env`.
- **No clones.** Files are only ever fetched via the GitHub REST API.

See [`DECISIONS.md`](./DECISIONS.md) for the trade-offs, known limitations,
and what we'd do differently with more time.
