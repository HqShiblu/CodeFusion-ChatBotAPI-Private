
# DECISIONS.md — Codebase Research Agent

## Architecture Overview

The agent is a Django REST Framework application backed by PostgreSQL. A single
`POST /api/sessions/` endpoint accepts a GitHub repository URL and a natural language
question, runs a structured pipeline before touching the codebase and returns a precise
answer with references to specific files and line numbers. Every session — including
every tool call the agent makes — is persisted to the database so it can be retrieved
and built upon later.

The agent itself is implemented as a tool-calling loop: the LLM is given a set of
tools (GitHub exploration tools + database read/write tools) and iterates until it
has enough context to answer confidently, or until the hard loop cap is hit.

---

## Design Decisions

1. Remove trailing slash of repository.
2. Save the edited url, question and the embedding of the question.
3. Use local model for embedding. The model should be all-MiniLM-L6-v2 and the model name should be taken from .env file.
4. First, search with embedding if the question has already been asked and answered.
5. If the question is already known to the LLM then answer from it's own knowledge base.
6. If the user is asking for summary first search for readme.MD and relevant files that can be useful depending on the repository type or framework.
7. If none of the above is true then run the actual procedure.
8. First the ai agent should bring the directory tree with github api and then do the rest.
9. Instead of loading whole code, first send the method names to the llm to analyze based on the dependency. To detect the methods first get the regular expression for that specific programming language. Then analyze that specific method whichever the llm thinks might be useful.
10. After generating the answer the agent should save the answer so that it can be served later semantically.
11. The appliaction should use django, django REST framework and Postgresql database.
12. The LLM should be OpenAI compatible.
13. Database host, port, username, password, LLM url, LLM name, LLM api key i.e. all the credentials should be read from .env file.
14. Set a variable to run the maximum agent loop so that the agent doesn't run forever. The variable should be read from the .env file.
15. The cmd should show the tool it is calling and files that are being read.
16. Also print the number of tokens being used in each LLM call. At the end, print total number of tokens used for each api call.


## Database Schema Rationale

Four models: `Repository`, `ResearchSession`, `Finding`, `ToolCallLog`.

`Repository` keeps the tracks of the repository that are being searched.
It keeps single entry for each repository.

`ResearchSession` holds the question, its embedding, the final answer and token usage. Each time a question is asked it creates a new session and saves it with a reference of `Repository`.

`ToolCallLog` is written automatically by the tool dispatcher after every invocation.
It keeps track of tools that are being called. It has *session_id* as a foreign key and a `ResearchSession` may have many `ToolCallLog`.

`Finding` is written by the agent mid-loop via the `save_finding` tool. These are
the agent's working notes — each one records what it concluded about a specific file
during this session. They also feed `get_previous_findings()` in future sessions,
letting the agent skip files it has already characterized.
A `ResearchSession` can have only one `Finding`.

**At scale:** The `question_embedding` vector index (`ivfflat`, cosine ops) will need
tuning as the table grows. The `Finding` and `ToolCallLog` tables will grow fast on
active systems and would benefit from partitioning by `session_id` or archiving old
sessions.


## What I Would Do Differently With More Time

- Add async task execution (Celery) so the API returns a session ID immediately and
  the agent runs in the background, with a polling or webhook endpoint for results.
- Rate-limit and cache GitHub API calls within a session to avoid redundant fetches
  when the agent calls `read_file` on the same path twice.
- Add an admin dashboard to browse sessions, findings and tool call logs without needing to query the database directly.

---

## Known Limitations

- The agent runs synchronously in the request/response cycle. Long sessions on large
  repositories may cause the HTTP request to time out.
- The semantic cache threshold (0.92 cosine similarity) is a fixed value. Questions
  that are semantically close but meaningfully different (asking about two different
  parts of the same system) could theoretically collide, though in practice 0.92 is
  tight enough to avoid this.
