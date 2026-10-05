# AI Engineering Manager

Phase 2 FastAPI service for a controlled multi-agent engineering workflow.

## Phase 2 Scope

The Manager Agent plans backend and frontend tasks and understands dependencies.
The Backend Agent is now a bounded local coding agent: it inspects `workspace/`,
derives acceptance criteria, passes current files and completed dependency
context to the LLM, validates and writes only changed files, runs declared
task-specific tests followed by tests owned by completed tasks for the same agent, and retries failed
implementations with up to three repair attempts after the initial generation.
The Frontend Agent follows the same controlled lifecycle for React/Vite JavaScript:
it writes only under `workspace/frontend/`, installs allowlisted npm dependencies,
runs fixed Vitest targets, and performs up to three evidence-driven repairs.

Not included: UI/UX, DevOps, mobile, QA agents,
email, deployment, production access, human approval, Git commits, or pushes.

## Architecture

```text
POST /projects
  -> Manager Agent: structured task plan
  -> Orchestrator: release only tasks whose dependencies completed
  -> Backend Agent: inspect -> criteria -> generate JSON -> validate -> write
  -> Dependency Manager: approve -> prepare venv -> install missing dependencies
  -> Test Runner: task tests -> completed-task regression tests
  -> Frontend Agent: compact plan -> generate/validate/write one file at a time -> dependencies -> Vitest
  -> Manager Agent: require task files, tests, dependencies, and regression proof
```
Frontend generation has two stages. A compact JSON plan lists source/test paths
and allowlisted dependencies without source contents. The agent then requests one
exact path and file body per LLM call, with bounded target/related context. Each
file is validated and written before moving to the next, so a provider failure
on a later file does not regenerate valid earlier files. Plan/file corrections
and provider fallback are separate from the three code-repair attempts, which
begin only after generated task tests run. HTTP 413 is classified as
`CONTEXT_TOO_LARGE` and the same oversized request is not retried.

`app/services/llm.py` owns the centralized router, Gemini, Groq, and optional
Ollama implementations, structured JSON parsing, a serial request gate, and
bounded HTTP retry behavior. Agents depend only on the common `LLMService`
interface and do not select providers.

The orchestrator tracks backend and frontend test ownership as task ID, test paths, and final
completion status. A task runs its own tests plus tests registered by previously
completed backend tasks; tests belonging to future, blocked, failed, or review
tasks are excluded.

`app/tools/workspace.py` confines file operations to `workspace/`, rejecting
absolute paths, traversal, and symlink escapes. `app/tools/test_runner.py` exposes
no arbitrary command input and executes only validated backend test paths:

```text
<approved-python> -m pytest <owned-backend-test-paths> -q
```

The test process runs with `workspace/` as its working directory, a minimal
environment that does not inherit API keys, disabled third-party pytest plugin
autoloading, and a timeout. The Backend Agent never receives a general-purpose
shell tool.

`app/tools/dependency_manager.py` reads the generated backend requirement
manifest, accepts only an explicit Python package allowlist, prepares
`workspace/backend/.venv`, and installs missing approved packages before pytest.
Initial generation must create implementation and task tests. Repair attempts
reuse those tests and make only meaningful implementation changes based on each
new pytest failure. Result `attempts` counts repairs only, from zero through three.

Backend execution uses explicit generation, format-validation,
implementation-validation, dependency-preparation, test-execution, code-repair,
and Manager-review stages. A semantically invalid initial generation receives one
same-provider generation correction before bounded escalation to the next configured
provider; no files are written and no code-repair attempt is consumed during this flow.

Backend generation prompts include the Pydantic-generated
`BackendImplementation` JSON schema. Invalid response shapes receive at most two
format-only correction calls before any files are written; these calls are
tracked separately and do not consume code-repair attempts.

Test failures are classified as code, dependency, environment, test, or unknown
failures. Dependency failures go through controlled compatibility resolution in
the generated virtualenv, using only validated allowlisted requirements, before
the identical owned test paths are rerun. Code repairs receive the exact traceback,
exception details, current task tests, traceback/import-related source contents,
prior strategies, and dependency evidence. Each repair must return a structured
failure category, root cause, strategy, declared files, and controlled patch.
Before/after failure signatures expose strategies that made no progress; valid
tests cannot be changed without an explicit inconsistency justification. Rejected
repair plans retain the original pytest traceback for the next attempt, and a repair
may patch a safe subset of its declared candidate files.

Dependency compatibility recovery avoids eager ecosystem-wide upgrades. Known
allowlisted compatibility profiles, including Passlib with Bcrypt, install controlled
constraints, verify them with `pip check`, persist exact versions, and normal dependency
preparation enforces those versions rather than checking package names alone.
Manager tasks expose normalized
`provides` and `requires` capabilities, and missing dependency edges are added
deterministically before cycle validation and execution.

## Completion Evidence

Backend completion uses explicit gates for implementation changes, task tests, completed-task regressions, dependency preparation, required acceptance criteria, and unresolved execution failures. Each acceptance criterion records applicability, satisfaction, and supporting evidence. Non-applicable criteria, such as FastAPI route registration for a pure ORM task, do not reject completion. Every rejection includes a machine-readable reason.

File-change accounting uses a dedicated content-hash baseline and is independent of the size-limited source context sent to the LLM.

## Project Structure

```text
app/
  main.py
  agents/
    manager.py
    backend.py
    frontend.py
  models/
    task.py
  orchestrator/
    workflow.py
  services/
    llm.py
  tools/
    workspace.py
    dependency_manager.py
    frontend_dependency_manager.py
    frontend_test_runner.py
    api_contract.py
    test_runner.py
tests/
  test_projects.py
  test_tools.py
workspace/
  .gitkeep
.env.example
requirements.txt
README.md
```

Generated projects are written below `workspace/backend/` and
`workspace/frontend/`. Frontend execution requires Node.js and npm; package
installation is restricted to the built-in allowlist and disables install scripts.

## Controlled File-Writing API

Set `N8N_TOOL_API_KEY` to a strong random secret in the root `.env`, then restart
the API. Send the same secret from n8n in the `X-Tool-API-Key` header. Missing or
incorrect keys receive HTTP 401; the key is never logged or returned. Swagger's
`Try it out` form exposes this header as `x-tool-api-key`.

`POST /tools/write-files` accepts only `agent: "backend"` and relative paths below
`workspace/backend/` (for example, `backend/app/main.py`). It checks all paths
before writing, rejects unsafe paths with structured evidence, and creates safe
parent directories as needed. It does not execute files, install packages, or
run tests. Keep this endpoint on a trusted network; the token grants write access
to generated backend files. The request and response schemas are available at
`/docs` and `/openapi.json`.

```bash
curl -X POST http://127.0.0.1:8000/tools/write-files \
  -H "X-Tool-API-Key: ${N8N_TOOL_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"TASK-002","agent":"backend","files":[{"path":"backend/app/main.py","content":"from fastapi import FastAPI\napp = FastAPI()\n"}]}'
```

`POST /tools/run-tests` uses the same `X-Tool-API-Key` header. It accepts only a
task ID and `agent: "backend"`; clients cannot select paths, interpreters, or
commands. The endpoint runs the complete `workspace/backend/tests` suite through
the controlled `BackendTestRunner`, using `workspace/backend/.venv/bin/python`
when present (otherwise the manager's Python). It does not install dependencies
or repair generated code. Pytest failures and timeouts return structured evidence.

```bash
curl -X POST http://127.0.0.1:8000/tools/run-tests \
  -H "X-Tool-API-Key: ${N8N_TOOL_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"TASK-002","agent":"backend"}'
```

## Run Locally

Python 3.11 or newer is required.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app
```

The generated workspace changes during an agent run. For development reloads,
watch only the manager application and exclude generated files:

```bash
uvicorn app.main:app --reload --reload-dir app --reload-exclude 'workspace/*'
```

The provider order is configurable. This example uses Gemini, local Ollama, then Groq. Configure
the router in `.env`:

```bash
LLM_PRIMARY_PROVIDER=gemini
LLM_SECONDARY_PROVIDER=ollama
LLM_TERTIARY_PROVIDER=groq
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.5-flash-lite

# Optional Groq configuration:
GROQ_API_KEY=your_groq_key_here
GROQ_FALLBACK_API_KEY=your_secondary_groq_key_here
GROQ_MODEL=openai/gpt-oss-120b

# Optional local fallback; no model is downloaded automatically:
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5-coder:3b
```

Cloud providers without keys are skipped. Ollama is enabled only when
`OLLAMA_MODEL` is set. If no provider is configured, the service retains the
deterministic local planning and coding fallback. No key is stored in source.

When both Groq keys are configured, the primary key uses the normal bounded
retry policy first. Groq switches once to `GROQ_FALLBACK_API_KEY` only after the
primary request fails; credentials are never included in logs.

HTTP 429, 500, 502, 503, and 504 responses plus connection and timeout failures
use bounded retries. `Retry-After` is honored when supplied; otherwise delays
are 1, 2, and 4 seconds. Exhaustion moves to the next configured provider.
Malformed JSON, validation failures, and HTTP 400 errors do not trigger router
fallback.

## API Example

```bash
curl -X POST http://127.0.0.1:8000/projects \
  -H "Content-Type: application/json" \
  -d '{"requirement":"Build a school management system with authentication and student dashboard"}'
```

Agent results include `files_created`, `files_modified`, task-test evidence,
selected completed-task regression paths and evidence, acceptance criteria, and
attempt count.
Dependent tasks stay `blocked` unless every dependency is `completed`.

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
```

The suite covers workspace path confinement, traversal and symlink escape
rejection, controlled file creation, the fixed test runner, dependency release
and blocking, controlled frontend coding, cycle detection, and retry limits.

## Remaining Limitations

- Backend generation is synchronous within the request lifecycle.
- Workspace contents persist between requests and are not isolated per project.
- Manager review is deterministic evidence checking, not a separate LLM review.
- Generated Python tests run as the local server user; OS-level process or
  network sandboxing is not included in Phase 2.
- Generated frontend tests run as the local server user; OS-level process or
  network sandboxing is not included.
