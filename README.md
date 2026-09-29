# AI Engineering Manager

Phase 2 FastAPI service for a controlled multi-agent engineering workflow.

## Phase 2 Scope

The Manager Agent plans backend and frontend tasks and understands dependencies.
The Backend Agent is now a bounded local coding agent: it inspects `workspace/`,
derives acceptance criteria, passes current files and completed dependency
context to the LLM, validates and writes only changed files, runs declared
task-specific tests followed by the complete backend suite, and retries failed
implementations with up to three repair attempts after the initial generation.
The Frontend Agent remains proposal-only.

Not included: autonomous frontend coding, UI/UX, DevOps, mobile, QA agents,
email, deployment, production access, human approval, Git commits, or pushes.

## Architecture

```text
POST /projects
  -> Manager Agent: structured task plan
  -> Orchestrator: release only tasks whose dependencies completed
  -> Backend Agent: inspect -> criteria -> generate JSON -> validate -> write
  -> Dependency Manager: approve -> prepare venv -> install missing dependencies
  -> Test Runner: task tests -> complete backend regression suite
  -> Manager Agent: require task files, tests, dependencies, and regression proof
```

`app/services/llm.py` owns the provider abstraction, Gemini and Groq
implementations, provider selection, structured JSON parsing, and bounded HTTP
retry behavior. Agents depend only on the common `LLMService` interface.
`app/tools/workspace.py` confines file operations to `workspace/`, rejecting
absolute paths, traversal, and symlink escapes. `app/tools/test_runner.py` exposes
no command input and executes only:

```text
<current-python> -m pytest backend/tests -q
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

Generated backend projects are written below `workspace/backend/`.

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

Gemini is the default provider. Configure either provider in `.env`:

```bash
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.5-flash-lite

# Optional Groq configuration:
GROQ_API_KEY=your_groq_key_here
GROQ_FALLBACK_API_KEY=your_secondary_groq_key_here
GROQ_MODEL=openai/gpt-oss-120b
```

Set `LLM_PROVIDER=groq` to switch back to Groq. Model settings are optional,
and no key is stored in source. Without the selected provider's key, the service
uses deterministic local planning and coding fallbacks.

When both Groq keys are configured, the primary key uses the normal bounded
retry policy first. Groq switches once to `GROQ_FALLBACK_API_KEY` only after the
primary request fails; credentials are never included in logs.

HTTP 429 and 503 responses honor `Retry-After` when supplied. Otherwise,
retries use bounded exponential delays of 1, 2, and 4 seconds. After three
retries the service raises a clear rate-limit or service-unavailable error.

## API Example

```bash
curl -X POST http://127.0.0.1:8000/projects \
  -H "Content-Type: application/json" \
  -d '{"requirement":"Build a school management system with authentication and student dashboard"}'
```

Backend results include `files_created`, `files_modified`, task-test evidence,
complete regression evidence, acceptance criteria, and attempt count.
Dependent tasks stay `blocked` unless every dependency is `completed`.

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
```

The suite covers workspace path confinement, traversal and symlink escape
rejection, controlled file creation, the fixed test runner, dependency release
and blocking, frontend proposal-only behavior, cycle detection, and retry limits.

## Remaining Limitations

- Backend generation is synchronous within the request lifecycle.
- Workspace contents persist between requests and are not isolated per project.
- Manager review is deterministic evidence checking, not a separate LLM review.
- Generated Python tests run as the local server user; OS-level process or
  network sandboxing is not included in Phase 2.
- Frontend tasks stop at `review` and do not write files.
