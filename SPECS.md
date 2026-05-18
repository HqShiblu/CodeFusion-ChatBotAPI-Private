# SPECS.md — Codebase Research Agent

## Project Overview

A Django REST Framework AI agent that accepts a GitHub repository URL and a natural
language question, intelligently traverses the codebase using the GitHub API via
**tool calling**, and returns a precise answer with **references to specific files,
functions, and line numbers**. Every research session is fully persisted to PostgreSQL
so it can be retrieved, reviewed, and built upon in future sessions.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend Framework | Django + Django REST Framework |
| Database | PostgreSQL with `pgvector` extension |
| Vector Search | pgvector (cosine similarity on question embeddings) |
| LLM | OpenAI-compatible API (tool/function calling required) |
| Embeddings | `sentence-transformers` — local, no API key needed (`all-MiniLM-L6-v2`) |
| GitHub Data | GitHub REST API (unauthenticated or token-based) |

---

## Environment Variables (`.env`)

All credentials must be read from `.env`. Never hardcode secrets. Ship a `.env.example`
with all keys present but no real values.

```env
# Database
DB_HOST=localhost
DB_PORT=5432
DB_NAME=github_analyzer
DB_USER=postgres
DB_PASSWORD=yourpassword

# LLM (OpenAI-compatible, must support tool/function calling)
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL_NAME=gpt-4o
LLM_API_KEY=sk-...

# Embeddings (local model via sentence-transformers)
EMBEDDING_MODEL_NAME=all-MiniLM-L6-v2

# GitHub (optional but strongly recommended to avoid rate limits)
GITHUB_TOKEN=ghp_...

# Agent loop — hard stop after this many tool calls
AGENT_MAX_LOOP=30

# App
DEBUG=True
SECRET_KEY=your-django-secret-key
```

---

## REST API Endpoints

### 1. `POST /api/sessions/`
Start a new research session.

**Request:**
```json
{
  "repository_url": "https://github.com/owner/repo",
  "question": "How does FastAPI handle dependency injection internally?"
}
```

**Response:**
```json
{
  "session_id": "uuid",
  "repository_url": "https://github.com/owner/repo",
  "question": "How does FastAPI handle dependency injection internally?",
  "answer": "FastAPI resolves dependencies via ...",
  "source": "cache | llm_knowledge | readme_scan | full_traversal",
  "references": [
    {
      "file_path": "fastapi/dependencies/utils.py",
      "line_start": 42,
      "line_end": 78,
      "note": "Core dependency resolver"
    }
  ],
  "token_usage": {
    "prompt_tokens": 3200,
    "completion_tokens": 410,
    "total_tokens": 3610
  },
  "created_at": "2024-01-01T12:00:00Z"
}
```

### 2. `GET /api/sessions/{session_id}/`
Retrieve a specific session with its full tool call log and findings.

**Response:** Full session object including all `ToolCallLog` entries and `Finding` records.

### 3. `GET /api/sessions/?repo={repository_url}`
List all past sessions for a given repository, ordered by most recent.

### 4. `GET /api/repos/`
List all repositories that have been researched, with last analyzed timestamp.

---

## Database Schema

Design using Django ORM and migrations. No raw SQL unless justified.

### Model: `Repository`
```
id               UUID, primary key
url              TextField, unique          # sanitized, no trailing slash
name             CharField                  # e.g. "tiangolo/fastapi"
last_analyzed_at DateTimeField, nullable
created_at       DateTimeField, auto
```

### Model: `ResearchSession`
```
id               UUID, primary key
repository       ForeignKey → Repository
question         TextField
question_embedding  VectorField(1536)       # pgvector, for semantic cache
answer           TextField, nullable        # NULL until agent completes
source           CharField                  # cache | llm_knowledge | readme_scan | full_traversal
token_usage      JSONField, nullable        # { prompt_tokens, completion_tokens, total_tokens }
started_at       DateTimeField, auto
completed_at     DateTimeField, nullable
```

### Model: `Finding`
Agent-written notes on what it discovered during a session.
The agent calls `save_finding(session_id, file_path, note, line_start, line_end)` as a tool.

```
id               UUID, primary key
session          ForeignKey → ResearchSession
file_path        TextField                  # e.g. "fastapi/dependencies/utils.py"
line_start       IntegerField, nullable
line_end         IntegerField, nullable
note             TextField                  # agent's conclusion about this file
created_at       DateTimeField, auto
```

### Model: `ToolCallLog`
Every tool invocation the agent makes, logged for auditability and replay.

```
id               UUID, primary key
session          ForeignKey → ResearchSession
tool_name        CharField                  # list_files | read_file | search_code | save_finding | ...
input_params     JSONField                  # exactly what the agent passed
output_summary   TextField                  # truncated/summarized result
called_at        DateTimeField, auto
```

### pgvector setup
```sql
-- Run once on the PostgreSQL instance before migrations:
CREATE EXTENSION IF NOT EXISTS vector;
-- Django migrations via django-pgvector will handle the rest.
```

---

## Processing Pipeline (Strict Order)

```
POST /api/sessions/
        │
        ▼
┌─────────────────────────────┐
│ STEP 1: Sanitize Input      │
│  - Strip trailing slash     │
│  - Lowercase & trim         │
│  - Parse owner/repo from URL│
└────────────┬────────────────┘
             │
             ▼
┌─────────────────────────────┐
│ STEP 2: Persist Request     │
│  - Upsert Repository record │
│  - Generate embedding of    │
│    the question             │
│  - Create ResearchSession   │
│    with answer = NULL       │
└────────────┬────────────────┘
             │
             ▼
┌─────────────────────────────┐
│ STEP 3: Semantic Cache Hit? │
│  - Cosine similarity search │
│    on question_embedding    │
│  - Threshold: >= 0.92       │
│  - Same repo URL required   │
│  - answer must NOT be NULL  │
│  - HIT → return answer,     │
│    source = "cache"         │
└────────────┬────────────────┘
             │ MISS
             ▼
┌─────────────────────────────┐
│ STEP 4: Check Prior Work    │
│  - Agent calls DB tool:     │
│    get_previous_findings()  │
│  - Reads past sessions and  │
│    findings for this repo   │
│  - Informs traversal plan   │
│    (skip already-read files)│
└────────────┬────────────────┘
             │
             ▼
┌─────────────────────────────┐
│ STEP 5: LLM Self-Assessment │
│  - Ask LLM: can it answer   │
│    confidently from its own │
│    training data alone?     │
│  - Only accept "yes" if     │
│    explicitly confident     │
│  - source: "llm_knowledge"  │
└────────────┬────────────────┘
             │ NOT CONFIDENT
             ▼
┌─────────────────────────────┐
│ STEP 6: README / Summary    │
│  - If question is about     │
│    purpose, setup, or usage:│
│    Fetch via GitHub API:    │
│    • README.md              │
│    • pyproject.toml /       │
│      package.json / etc.    │
│    • CONTRIBUTING.md        │
│  - If sufficient → answer,  │
│    source: "readme_scan"    │
└────────────┬────────────────┘
             │ NOT SUFFICIENT
             ▼
┌─────────────────────────────┐
│ STEP 7: Full Traversal      │
│  Tool-calling agent loop    │
│  (see section below)        │
│  source: "full_traversal"   │
└────────────┬────────────────┘
             │
             ▼
┌─────────────────────────────┐
│ STEP 8: Finalize & Persist  │
│  - Update ResearchSession:  │
│    answer, source,          │
│    token_usage,             │
│    completed_at             │
│  - Update Repository:       │
│    last_analyzed_at         │
│  - Answer now available for │
│    future semantic cache    │
└─────────────────────────────┘
```

---

## Step 7: Full Traversal — Tool-Calling Agent Loop

The agent is given a defined set of tools and runs a **multi-step reasoning loop**,
calling tools until it has sufficient context to produce a final answer with
file and line references.

### Agent Tools

#### Code Exploration Tools

```python
get_directory_tree() -> str
# Fetches the full recursive file tree of the repository.
# MUST be the first tool called in any traversal session.
# GitHub API: GET /repos/{owner}/{repo}/git/trees/HEAD?recursive=1

list_files(path: str) -> list[str]
# Lists files and directories at a given path in the repo.
# GitHub API: GET /repos/{owner}/{repo}/contents/{path}

read_file(path: str) -> str
# Reads file content decoded from base64, with line numbers prepended.
# GitHub API: GET /repos/{owner}/{repo}/contents/{path}

get_file_summary(path: str) -> str
# Returns the first 80 lines of a file.
# Use before read_file on any file larger than 50KB.

search_code(query: str) -> list[dict]
# Searches the codebase for a keyword or symbol.
# Returns: [{ file_path, line_number, snippet }]
# GitHub API: GET /search/code?q={query}+repo:{owner}/{repo}
```

#### Database Tools

```python
save_finding(session_id, file_path, note, line_start=None, line_end=None) -> dict
# Persists a Finding record for this session.
# The agent MUST call this whenever it learns something meaningful about a file.

get_previous_findings(repo_url: str) -> list[dict]
# Returns all Findings from prior sessions for this repository.
# Agent should call this early to avoid redundant exploration.

list_past_sessions(repo_url: str) -> list[dict]
# Returns summary of all past ResearchSessions for this repo.
```

> **Note:** `log_tool_call` is called automatically by the tool dispatcher after every
> tool invocation. It is never exposed to the LLM as a callable tool.

### Agent Loop Design

```
System prompt is set once per session (see LLM section below).

LOOP:
  while tool_calls_made < MAX_TOOL_CALLS:
      response = llm.chat(messages, tools=TOOL_DEFINITIONS)

      if response.finish_reason == "stop":
          break   # agent decided it has sufficient context

      for each tool_call in response.tool_calls:
          result = dispatch(tool_call)
          log_tool_call(...)          # always written to ToolCallLog
          messages.append(tool_result)

  final_answer = extract_text(response)
  references   = parse_file_citations(final_answer)   # [[path:line_start-line_end]]
```

### Loop Guardrail

A single variable controls the maximum number of tool calls the agent can make in one
session. When the limit is reached, the agent is forced to produce a final answer
immediately with whatever context it has collected so far.

Read it in `settings.py`:
```python
# settings.py
AGENT_MAX_LOOP = int(os.getenv("AGENT_MAX_LOOP", 30))
```

Consume in `agent_loop.py` via Django settings — never hardcode:
```python
from django.conf import settings
MAX_LOOP = settings.AGENT_MAX_LOOP
```

### Console Logging During the Loop

Every iteration of the loop must print to stdout:

```
[Tool Call 3/30] search_code         |  tokens used: 1,842
[Tool Call 4/30] read_file           |  tokens used: 2,109
[Tool Call 5/30] save_finding        |  tokens used: 2,115

Total tokens used: 6,066
```

Implementation in `agent_loop.py`:
```python
print(f"[Tool Call {tool_call_count}/{MAX_LOOP}] {tool_name:<20} |  tokens used: {total_tokens:,}")

# After the loop ends:
print(f"\nTotal tokens used: {cumulative_tokens:,}")
```

`total_tokens` is read from `response.usage.total_tokens` after each LLM call.
`cumulative_tokens` is the running sum across all LLM calls in the session.

---

## Efficient Codebase Traversal Strategy

### Phase 1 — Directory Tree First (Always)

```
GET https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1
```

Mandatory first step — cheap (1 API call), returns all paths, informs every
subsequent decision. Never skip.

### Phase 2 — Check Prior Work

Agent calls `get_previous_findings(repo_url)` and `list_past_sessions(repo_url)`.
Files already analyzed in prior sessions can be skipped unless the new question
requires re-reading them.

### Phase 3 — Classify the Question → Prioritize Files

| Question Theme | Target Paths/Patterns |
|---|---|
| Authentication / Auth | `auth/`, `login.*`, `middleware.*`, `jwt.*`, `session.*` |
| Database / Schema | `migrations/`, `models/`, `schema.*`, `db.*` |
| API / Endpoints | `routes/`, `controllers/`, `views/`, `api/`, `urls.*` |
| Configuration / Setup | `.env.example`, `config/`, `settings.*`, `docker-compose.*` |
| Testing | `tests/`, `__tests__/`, `*.test.*`, `*.spec.*` |
| Dependencies | `package.json`, `requirements.txt`, `Pipfile`, `Cargo.toml` |
| CI/CD | `.github/workflows/`, `Jenkinsfile`, `.circleci/` |
| General / Summary | `README.md`, entry points (`main.*`, `index.*`, `app.*`) |

### Phase 4 — Ranked Fetch Order

```
1. Entry points         (main.*, index.*, app.*, server.*)
2. Question-matched     (paths matching classified theme above)
3. Manifest files       (package.json, requirements.txt, etc.)
4. Config files         (.env.example, settings.*, config.*)
5. Code search results  (search_code for specific symbols if needed)
6. Other files          (only if still insufficient — up to the cap)
```

Stop as soon as `finish_reason == "stop"` or guardrail caps are hit.

---

## GitHub API Usage Guidelines

- Include `Authorization: Bearer {GITHUB_TOKEN}` if token is set in `.env`
- Set `Accept: application/vnd.github+json` on all requests
- Rate limits: unauthenticated = 60 req/hr, authenticated = 5000 req/hr
- File content in API responses is base64-encoded — always decode before use
- Parse `owner` and `repo` by splitting the sanitized URL on `/`

---

## LLM Interaction Guidelines

- Use OpenAI-compatible `chat/completions` with the `tools` parameter
- The model **must support tool/function calling**
- Read `LLM_BASE_URL`, `LLM_MODEL_NAME`, `LLM_API_KEY` from `.env` at startup
- Track and persist `usage.prompt_tokens`, `usage.completion_tokens` from every response

**System prompt template:**
```
You are a codebase research agent with tools to explore a GitHub repository.
Repository: {repo_url}
Question: {question}

Rules:
1. Always call get_directory_tree() first.
2. Always call get_previous_findings() before reading any files.
3. Call save_finding() whenever you learn something meaningful about a file.
4. Cite files in your final answer as [[path/to/file.py:line_start-line_end]].
5. Stop calling tools once you can answer confidently. Do not over-explore.
6. If you cannot determine the answer, say so clearly. Do not hallucinate.
7. Your final answer must include specific file paths, function names, and line numbers.
```

---

## Embedding & Semantic Cache Guidelines

Embeddings are generated **locally** using `sentence-transformers`. No external API
call or API key is required for embeddings.

- Library: `sentence-transformers`
- Default model: `all-MiniLM-L6-v2` (384-dimensional vectors)
- Model name is read from `.env` as `EMBEDDING_MODEL_NAME` — never hardcoded
- The model is loaded once at application startup and reused across requests

```python
# settings.py
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
```

```python
# services/embeddings.py
from sentence_transformers import SentenceTransformer
from django.conf import settings

_model = None

def get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(settings.EMBEDDING_MODEL_NAME)
    return _model

def embed(text: str) -> list[float]:
    return get_model().encode(text).tolist()
```

> **Note:** `all-MiniLM-L6-v2` produces 384-dimensional vectors. The `VectorField`
> on `ResearchSession` must be declared as `VectorField(384)`, not `VectorField(1536)`.

- Embed **question text only** (not the URL)
- Store as `VectorField(384)` via `django-pgvector`
- Cache hit requires: same `repository__url` + cosine similarity >= **0.92** + non-null answer
- Generate embedding once per request; reuse for both cache lookup and storage

**ORM query (django-pgvector):**
```python
from pgvector.django import CosineDistance

ResearchSession.objects.alias(
    similarity=1 - CosineDistance("question_embedding", question_vector)
).filter(
    repository__url=repo_url,
    answer__isnull=False,
    similarity__gte=0.92
).order_by("-similarity").first()
```

---

## Django Project Structure

```
project_root/
├── .env
├── .env.example                   # all keys, no real values — commit this
├── manage.py
├── requirements.txt
├── README.md                      # setup and run instructions
├── DECISIONS.md                   # required deliverable (500-800 words)
├── fixtures/
│   └── sample_sessions.json       # real agent run output for review
├── config/
│   ├── settings.py                # reads all credentials from .env
│   ├── urls.py
│   └── wsgi.py
└── agent/
    ├── models.py                  # Repository, ResearchSession, Finding, ToolCallLog
    ├── serializers.py             # DRF serializers for all models
    ├── views.py                   # POST /api/sessions/, GET session, list sessions, list repos
    ├── urls.py
    ├── migrations/                # committed Django migrations
    ├── tests/
    │   ├── test_pipeline.py       # key unit tests for pipeline steps
    │   └── test_tools.py          # tool dispatcher tests
    └── services/
        ├── sanitizer.py           # URL cleaning (trailing slash removal, etc.)
        ├── embeddings.py          # embedding generation + similarity search
        ├── cache.py               # semantic cache lookup and save logic
        ├── github.py              # GitHub API: tree, file fetch, code search
        ├── classifier.py          # question theme → prioritized file list
        ├── tools.py               # all agent tool implementations + dispatcher
        ├── agent_loop.py          # main LLM tool-calling loop with guardrails
        └── llm.py                 # OpenAI-compatible chat completion wrapper
```

---

## Required Deliverables Checklist

- [ ] Public GitHub repository, runnable from a clean clone
- [ ] `README.md` with setup steps and example `curl` commands
- [ ] `DECISIONS.md` (500–800 words) covering architecture, schema rationale,
      trade-offs, what you'd do differently, how AI tools were used, known limitations
- [ ] `.env.example` with all keys, no real values
- [ ] Django migrations committed
- [ ] `fixtures/sample_sessions.json` or a seed script with real agent output
- [ ] At least a few key tests in `agent/tests/`

---

## Key Implementation Notes

1. **Input sanitization is step 1** — strip trailing slash, whitespace, normalize URL.
2. **Embedding generated once per request** — reused for both cache lookup and DB storage.
3. **Agent tools write to DB mid-loop** — `save_finding` and auto-logged `ToolCallLog`
   happen during traversal, not as post-processing. Persistence is part of the workflow.
4. **Never clone the repo** — GitHub API only for all file access.
5. **Fetch lazily** — tree first, then targeted reads; hard cap at 15 files.
6. **`answer` is nullable on insert** — set NULL initially, updated on completion.
   Prevents duplicate work on concurrent identical requests.
7. **Token usage must be tracked and stored** — `prompt_tokens`, `completion_tokens`,
   `total_tokens` from every LLM response go into `ResearchSession.token_usage`.
8. **References must be structured** — parse `[[path:line_start-line_end]]` markers
   from the final answer into the `references` array in the API response.
9. **`log_tool_call` is automatic** — the tool dispatcher calls it after every
   invocation. Never expose it to the LLM as a callable tool.
10. **pgvector must be installed on PostgreSQL** before running migrations:
    `CREATE EXTENSION IF NOT EXISTS vector;`