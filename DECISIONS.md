
# DECISIONS.md — Codebase Research Agent

## Architecture Overview

The agent is a Django REST Framework application backed by PostgreSQL. A single
`POST /api/sessions/` endpoint accepts a GitHub repository URL and a natural language
question, runs a structured pipeline before touching the codebase, and returns a precise
answer with references to specific files and line numbers. Every session — including
every tool call the agent makes — is persisted to the database so it can be retrieved
and built upon later.

The agent itself is implemented as a tool-calling loop: the LLM is given a set of
tools (GitHub exploration tools + database read/write tools) and iterates until it
has enough context to answer confidently, or until the hard loop cap is hit.

---

## Design Decisions

### 1. Remove the trailing slash from the repository URL before anything else

The very first operation on any incoming request is stripping the trailing slash from
`repository_url` and trimming whitespace. This happens before DB writes, before
embedding generation, before any API call.

**Why:** `https://github.com/owner/repo` and `https://github.com/owner/repo/` are the
same repository. If we don't normalize first, cache lookups miss, duplicate Repository
records get created, and GitHub API calls break. Normalizing at the entry point means
every downstream layer can trust the URL is clean.

---

### 2. Save the URL, question, and question embedding immediately after sanitization

As soon as the input is clean, we upsert a `Repository` record and create a
`ResearchSession` with `answer = NULL`. The question embedding is generated at this
point and stored on the session row.

**Why:** Writing the session early — before any expensive work — gives us a record of
every request even if the agent crashes halfway through. It also means the embedding
is generated exactly once and reused for both the cache lookup and the final storage,
avoiding a second round-trip to the embeddings API.

---

### 3. Search semantically for a prior answer before doing any work

Before calling the LLM or touching GitHub, we query the database for a past
`ResearchSession` on the same repository whose `question_embedding` is within cosine
distance ≥ 0.92 of the current question's embedding, and whose `answer` is not NULL.

**Why:** Embedding similarity catches rephrased questions that mean the same thing
("How does auth work?" vs "Explain the authentication flow") without requiring an exact
string match. This avoids redundant LLM calls and GitHub API usage for questions that
have already been answered, keeping costs and latency low.

---

### 4. If the LLM already knows the answer, return it without touching the codebase

After a cache miss, the agent asks the LLM whether it can answer the question
confidently from its own training data. Only if it explicitly signals high confidence
do we accept that answer and skip traversal entirely.

**Why:** Well-known, stable libraries (Django, FastAPI, React) are in the LLM's training
data. Asking the agent to re-read the FastAPI source to explain what a decorator is
wastes tokens and time. However, the bar for "confident" is deliberately high — the LLM
must assert certainty, not just attempt an answer — because hallucinated answers on
codebase questions are worse than no answer.

---

### 5. For summary or overview questions, fetch README and relevant meta-files first

If the question is about the project's purpose, setup, usage, or overall architecture,
the agent fetches `README.md`, the manifest file (`pyproject.toml`, `package.json`,
`Cargo.toml`, etc.), and `CONTRIBUTING.md` before considering a full traversal.
The relevant manifest is chosen based on detected repository type or framework.

**Why:** README files are written specifically to answer "what is this and how do I
use it" questions. Reading 3 lightweight files is far cheaper than running the full
traversal loop. If these files answer the question sufficiently, the agent stops there.

---

### 6. Only run the full traversal if none of the above resolved the question

The full tool-calling loop — fetching the directory tree, classifying the question,
reading files, searching code — is the most expensive path. It runs only when the
semantic cache missed, the LLM has no confident prior knowledge, and a README scan
was insufficient.

**Why:** This ordering (cache → LLM knowledge → README scan → full traversal) is a
cost ladder. Each step is progressively more expensive in tokens and API calls. The
vast majority of repeat questions and simple overview questions never reach step 6,
which keeps the system fast and cheap at scale.

---

### 7. Always fetch the full directory tree first before reading any files

The first tool call in any full traversal session is always `get_directory_tree()`,
which fetches the complete recursive file tree via the GitHub API in a single request.

**Why:** The tree is cheap (1 API call) and gives the agent a complete map of the
repository before it reads anything. Without it, the agent would have to blindly probe
paths or read files speculatively. With it, the agent can classify the question against
real paths, prioritize the most relevant files, and avoid reading irrelevant ones. This
single call pays for itself immediately by reducing subsequent file reads.

---

### 8. Save the final answer to the database for future semantic retrieval

After the agent produces its final answer, we update the `ResearchSession` record with
the answer text, source, token usage, and completion timestamp. This record is now
eligible to be returned as a cache hit for semantically similar future questions.

**Why:** The answer is the most valuable artifact of a session. Persisting it closes
the feedback loop: the system gets smarter with each unique question answered, without
re-running any work for questions it has seen before. The `answer = NULL` sentinel on
in-progress sessions means only completed, verified answers are ever served from cache.

---

### 9. Django + Django REST Framework + PostgreSQL

The application is built with Django and DRF for the API layer, with PostgreSQL as the
database. The `pgvector` extension adds native vector similarity search to PostgreSQL,
eliminating the need for a separate vector store.

**Why:** Django's ORM, migrations, and admin make the data layer fast to build and easy
to inspect. DRF adds clean serialization and view patterns. PostgreSQL with `pgvector`
means vector search lives in the same database as all other application data — one less
infrastructure dependency, consistent transactional guarantees, and no syncing between
stores.

---

### 10. The LLM must be OpenAI-compatible

All LLM calls use the OpenAI `chat/completions` API format with the `tools` parameter.
The base URL, model name, and API key are all configurable, so any OpenAI-compatible
provider (OpenAI, Azure OpenAI, Groq, local Ollama, etc.) works without code changes.

**Why:** The OpenAI API format is the de facto standard for tool-calling LLMs. Making
the provider swappable via config means the application is not locked to one vendor and
can be pointed at a local model for development or a different provider for cost reasons.

---

### 11. All credentials are read from the `.env` file

Database connection details (host, port, name, user, password), LLM endpoint, model
name, and API key are all read from environment variables via `.env`. They flow into
Django `settings.py` via `os.getenv()` and are never hardcoded anywhere in the codebase.
A `.env.example` with all keys but no real values is committed to the repository.

**Why:** Hardcoded credentials are a security risk and make the application impossible
to deploy in different environments without code changes. `.env` is the standard
twelve-factor approach: config lives in the environment, not in the code.

---

### 12. A single env variable caps the agent loop

`AGENT_MAX_LOOP` (read from `.env`, defaulting to `30`) is the only guardrail on the
agent loop. When the number of tool calls reaches this limit, the agent is forced to
produce a final answer immediately with whatever context it has accumulated.

**Why:** Without a hard cap, a confused or over-eager agent can call tools indefinitely,
burning tokens and time. One variable is simpler to reason about and tune than a matrix
of per-tool limits. The default of 30 is generous enough for complex questions on large
codebases while still being a meaningful ceiling. Operators can tighten or loosen it
per deployment via `.env` without touching code.

```python
# settings.py
AGENT_MAX_LOOP = int(os.getenv("AGENT_MAX_LOOP", 30))
```

---

### 13. The terminal prints every tool call as it happens

Every time the agent calls a tool, a line is printed to stdout before the result is
processed:

```
[Tool Call 3/30] search_code         |  tokens used: 1,842
[Tool Call 4/30] read_file           |  tokens used: 2,109
[Tool Call 5/30] save_finding        |  tokens used: 2,115
```

**Why:** The agent loop is opaque by default — without logging, there is no visibility
into what the agent is doing, why it is slow, or whether it is stuck. Printing each
tool call in real time makes the agent's reasoning transparent during development and
debugging, and makes runaway or looping behavior immediately obvious.

---

### 14. Token usage is printed per LLM call and totalled at the end

Each tool call line prints `tokens used: N` — the cumulative token count from
`response.usage.total_tokens` at that point in the session. After the loop exits, a
final summary line is printed:

```
Total tokens used: 6,066
```

This total is also saved to `ResearchSession.token_usage` in the database.

**Why:** Token usage is the primary cost driver for LLM applications. Without
per-call visibility, it is impossible to know which tool calls are expensive, whether
the agent is being efficient, or what a session actually cost. Printing it live lets
developers catch runaway token usage immediately rather than after the fact on an
invoice. Storing it in the DB enables cost analysis across sessions.

---

## Database Schema Rationale

Four models: `Repository`, `ResearchSession`, `Finding`, `ToolCallLog`.

`Repository` is separated from `ResearchSession` so that metadata about a repo
(last analyzed, name) is stored once and shared across all sessions for that repo,
rather than duplicated on every question row.

`ResearchSession` holds the question, its embedding, the final answer, and token
usage. The embedding lives here rather than on a separate table because it is
intrinsically tied to the question text of this session.

`Finding` is written by the agent mid-loop via the `save_finding` tool. These are
the agent's working notes — each one records what it concluded about a specific file
during this session. They also feed `get_previous_findings()` in future sessions,
letting the agent skip files it has already characterized.

`ToolCallLog` is written automatically by the tool dispatcher after every invocation.
It is not exposed to the LLM as a callable tool. It exists for auditability — every
step the agent took can be replayed and inspected.

**At scale:** The `question_embedding` vector index (`ivfflat`, cosine ops) will need
tuning as the table grows. The `Finding` and `ToolCallLog` tables will grow fast on
active systems and would benefit from partitioning by `session_id` or archiving old
sessions. Token usage in `JSONField` works fine now but would move to dedicated columns
for easier aggregation queries.

---

## What I Would Do Differently With More Time

- Add async task execution (Celery) so the API returns a session ID immediately and
  the agent runs in the background, with a polling or webhook endpoint for results.
- Add structured output parsing so file/line references in the final answer are
  extracted more reliably than regex on `[[path:line]]` markers.
- Rate-limit and cache GitHub API calls within a session to avoid redundant fetches
  when the agent calls `read_file` on the same path twice.
- Add a lightweight admin view to browse sessions, findings, and tool call logs
  without needing to query the database directly.

---

## Known Limitations

- The agent runs synchronously in the request/response cycle. Long sessions on large
  repositories will cause the HTTP request to time out.
- GitHub API rate limits (60 req/hr unauthenticated) will be hit quickly without a
  `GITHUB_TOKEN` set in `.env`.
- The semantic cache threshold (0.92 cosine similarity) is a fixed value. Questions
  that are semantically close but meaningfully different (asking about two different
  parts of the same system) could theoretically collide, though in practice 0.92 is
  tight enough to avoid this.
- Token usage tracking assumes a single LLM provider. If the embedding and completion
  endpoints are on different providers with different token counting, the total will
  reflect completion tokens only.