# AutoDev — Complete Project Report
### AI Software Company Runtime: Local Multi-Agent Development System
**Date:** June 21, 2026 | **Branch:** `claude/confident-lamport-f1hvnk` | **Total Commits:** 44

---

## 1. Project Overview

**AutoDev** is a fully local AI development team that takes a natural language request and produces tested, safe code through a 7-agent pipeline. The entire system runs locally using **Ollama** with configurable models (default: `qwen2.5-coder:7b`, 32K context), requiring no cloud APIs. Code execution is sandboxed in Docker containers with no network access.

### Core Philosophy
- **Zero hardcoded model names** — all models configured via `config.yaml`
- **Sandboxed execution** — Docker containers with `network_disabled=True`, memory limits, CPU limits
- **Self-healing** — deterministic code fixers run before and after LLM generation
- **Persistent memory** — ChromaDB-backed learning across sessions
- **Observable** — every LLM call logged with latency, tokens, and content

---

## 2. Architecture

### 2.1 Pipeline Flow (14 Graph Nodes)

```
START → Memory Recall → Product Manager → Architect → [Human Approval] →
Developer → Healer → Tester →
  ├─ [Tests Pass]  → Reviewer → Judge → [ACCEPT] → Memory Save → Done
  └─ [Tests Fail]  → Debugger → Reviewer → Judge →
                        ├─ [ACCEPT] → Memory Save → Done
                        ├─ [REJECT] → Prepare Retry → Developer/Tester (conditional)
                        └─ [ESCALATE/ROLLBACK] → Failed
```

### 2.2 Agent Roles (7 Agents)

| Agent | File | Lines | Role |
|-------|------|-------|------|
| **Product Manager** | `agents/product_manager.py` | 35 | Clarifies scope, milestones, success criteria |
| **Architect** | `agents/architect.py` | 57 | Creates implementation plan: files, tasks, acceptance criteria |
| **Developer** | `agents/developer.py` | 920 | Generates code with self-healing inner loop (up to 8 iterations) |
| **Tester** | `agents/tester.py` | 102 | Generates comprehensive test suites (pytest-based) |
| **Debugger** | `agents/debugger.py` | 52 | Deterministic traceback analysis (no LLM when tests fail) |
| **Reviewer** | `agents/reviewer.py` | 64 | Code review — deterministic reject when tests fail, LLM only when tests pass |
| **Judge** | `agents/judge.py` | 55 | Final decision: ACCEPT/REJECT/ESCALATE/ROLLBACK |

### 2.3 State Machine (`state.py` — 39 lines)

The `AutodevState` TypedDict carries all data between nodes:

```python
class AutodevState(TypedDict, total=False):
    user_request: str           # Original natural language request
    workspace_path: str         # Path to workspace directory
    product_spec: dict          # PM output: milestones, scope, criteria
    plan: dict                  # Architect output: files, tasks
    plan_approved: bool         # Human-in-the-loop gate
    code_bundle: dict           # Developer output: generated files
    test_result: dict           # Tester output: pass/fail, stdout/stderr
    debug_report: dict          # Debugger output: root cause, affected files
    review: dict                # Reviewer output: approved, comments
    judge_decision: dict        # Judge output: ACCEPT/REJECT/ESCALATE
    iteration: int              # Current retry iteration (max 15)
    attempt_history: list[dict] # All previous attempts (append-only)
    error_hashes: list[str]     # Normalized error hashes for no-progress detection
    retry_target: str           # "developer" or "tester" — conditional routing
    memory_context: str         # Retrieved lessons from ChromaDB
    healer_fixes: list[str]     # Fixes applied by code healer
    final_status: str           # "success" or "failed"
    stop_reason: str            # Why the pipeline stopped
```

---

## 3. File Inventory

### 3.1 Core Engine (33 files, 7,208 lines)

| File | Lines | Purpose |
|------|-------|---------|
| `graph.py` | 917 | LangGraph pipeline orchestration — all 14 nodes, routing, retry logic |
| `agents/developer.py` | 920 | Self-healing developer with 8-iteration inner loop |
| `code_healer.py` | 473 | Zero-LLM deterministic code healing (runs before sandbox) |
| `llm_client.py` | 352 | OpenAI-compatible client for Ollama with caching and structured output |
| `surgical_patcher.py` | 329 | Surgical code patching (locate → inspect → patch → verify) |
| `project_mapper.py` | 298 | Builds complete project map: classes, functions, imports |
| `main.py` | 292 | Rich CLI with phase indicators and styled output |
| `package_manager.py` | 291 | Autonomous dependency management |
| `diagnostics.py` | 272 | Traceback parsing, error classification, API mismatch detection |
| `error_graph.py` | 257 | Error occurrence tracking with escalation policy |
| `error_localizer.py` | 248 | Pinpoints exact error location in source files |
| `syntax_fixer.py` | 240 | Multi-pass syntax auto-repair (quotes, f-strings, brackets) |
| `import_fixer.py` | 240 | Auto-fixes missing and broken imports |
| `memory.py` | 450 | ChromaDB-backed persistent memory (lessons + solutions) |
| `deps.py` | 184 | Dependency validation against pre-installed sandbox packages |
| `verifier.py` | 165 | Verifies fixes actually resolve the original error |
| `sandbox.py` | 142 | Docker sandbox execution with security constraints |
| `context_manager.py` | 141 | Token-aware file selection and context building |
| `config.py` | 140 | Pydantic configuration loader from YAML |
| `patcher.py` | 109 | Line-level code patching utilities |
| `kwarg_fixer.py` | 105 | Keyword argument auto-fixer |
| `agents/tester.py` | 102 | Test suite generation with comprehensive categories |
| `git_manager.py` | 99 | Git commit/rollback/tagging |
| `schemas.py` | 78 | Pydantic output schemas for all agents |
| `agents/reviewer.py` | 64 | Code review with deterministic fast path |
| `observability.py` | 59 | Structured JSON logging |
| `agents/architect.py` | 57 | Implementation planning |
| `agents/judge.py` | 55 | Final accept/reject decision |
| `agents/debugger.py` | 52 | Deterministic error diagnosis |
| `state.py` | 38 | LangGraph state TypedDict |
| `agents/product_manager.py` | 35 | Scope and requirements |
| `__init__.py` | 3 | Package init |
| `agents/__init__.py` | 1 | Package init |

### 3.2 Web UI (3 files + 2 static, ~755 lines)

| File | Purpose |
|------|---------|
| `autodev-ui/main.py` | FastAPI + WebSocket server, dual mode (Chat + Agent) |
| `autodev-ui/config.py` | UI configuration bridge |
| `autodev-ui/ollama_client.py` | Direct Ollama API wrapper for chat mode |
| `autodev-ui/static/app.js` | Frontend: WebSocket, model dropdown, streaming |
| `autodev-ui/static/index.html` | UI layout |
| `autodev-ui/static/style.css` | Styling |

### 3.3 Prompts (7 files, ~506 lines)

Each agent has a dedicated Markdown prompt file loaded at runtime:
- `prompts/product_manager.md` — Scope, milestones, out-of-scope items
- `prompts/architect.md` — JSON plan with files_needed, tasks, acceptance_criteria
- `prompts/developer.md` — Code generation with string/import rules
- `prompts/tester.md` — 5 mandatory test categories, minimum counts
- `prompts/debugger.md` — Traceback analysis, culprit identification
- `prompts/reviewer.md` — 4 explicit rules (approve if tests pass, reject only for demonstrable bugs)
- `prompts/judge.md` — ACCEPT/REJECT/ESCALATE decision framework

### 3.4 Evaluation Suite (10 tasks)

| # | Task | Difficulty |
|---|------|------------|
| 01 | Hello World | Trivial |
| 02 | FizzBuzz | Easy |
| 03 | Reverse String | Easy |
| 04 | Prime Checker | Easy |
| 05 | Calculator | Medium |
| 06 | Word Frequency | Medium |
| 07 | Flatten List | Medium |
| 08 | Linked List | Medium-Hard |
| 09 | CSV Parser | Hard |
| 10 | Task Scheduler | Hard |

### 3.5 Configuration & Build

| File | Purpose |
|------|---------|
| `config.yaml` | Main configuration (all models, sandbox, loop, memory settings) |
| `pyproject.toml` | Package metadata and dependencies |
| `Makefile` | install, run, eval, update-model, logs targets |
| `scripts/update_model.sh` | Pull new Ollama model + update config.yaml |
| `docker/Dockerfile` | Pre-built sandbox image with common packages |

---

## 4. Subsystem Deep Dives

### 4.1 Self-Healing Developer (`agents/developer.py` — 920 lines)

The Developer is the most complex agent. It has an **inner self-healing loop** (up to 8 iterations) that catches and fixes its own errors before returning to the pipeline:

```
LLM Generate → Parse → Write Files → Self-Review → Validate → Auto-Fix → Loop
```

**Key capabilities:**
- **Scope guard**: Only touches files listed in `plan.files_needed` + `test_runner.py`; matches on both full path and basename
- **Subdirectory support**: Proactively creates `__init__.py` for package directories
- **Stale file cleanup**: Removes old `.py` files not in the current plan using `rglob`
- **Self-review** (`_self_review()`): Reads back all generated files, checks for syntax errors, applies `fix_syntax()` auto-repair
- **Validation** (`_validate()`): Detects missing typing imports (`List`, `Dict`, `Optional`), checks for syntax errors, verifies subdirectory packages exist
- **Auto-fix** (`_auto_fix()`): Calls `fix_syntax()`, `fix_imports()`, `fix_kwargs()`, `_fix_missing_typing_imports()`
- **Lesson extraction** (`_extract_lesson()`): Saves specific reminders (typing imports, triple quotes, local modules) to memory

### 4.2 Code Healer (`code_healer.py` — 473 lines)

A **zero-LLM deterministic fixer** that runs between Developer and Tester. It processes all files with multiple passes:

1. **Syntax fix** — bracket balancing, quote repair, f-string conflicts
2. **Import fix** — missing imports, circular import detection
3. **Keyword argument fix** — argument mismatch resolution
4. **Pydantic → dataclass conversion** — replaces Pydantic models with stdlib dataclasses (sandbox has no Pydantic)
5. **Test failure extraction** — parses pytest output for targeted healing

### 4.3 Deterministic Debugging Pipeline

After real-world testing revealed that sending errors to the LLM produced **hallucinated diagnoses** (contradictory root causes across iterations), the debugging pipeline was rewritten to be fully deterministic when tests fail:

**Debugger** (`agents/debugger.py` + `diagnostics.py`):
- Parses actual traceback with regex: extracts file, line, error type
- Uses `_file_owner()` to classify: is this a developer file or tester file?
- Falls back to last stderr line (never calls LLM when `diag.confident == False`)

**Reviewer** (`agents/reviewer.py`):
- Tests FAILED → immediate deterministic rejection with root cause from debug report
- Tests PASSED → calls LLM for nuanced code quality review

**Judge** (`agents/judge.py`):
- Tests FAILED → immediate deterministic REJECT
- Tests PASSED + Review APPROVED → ACCEPT (no LLM needed)
- Tests PASSED + Review has concerns → calls LLM for nuanced decision

**Net effect**: Zero LLM calls for Debugger + Reviewer + Judge when tests fail. This eliminated contradictory diagnoses.

### 4.4 Diagnostics Engine (`diagnostics.py` — 272 lines)

- `diagnose_traceback(stderr)` → `DiagResult(error_type, traceback_file, traceback_line, message, confident, culprit)`
- `check_api_mismatch(stderr, project_map)` → detects when code calls methods/args that don't exist in the project
- `_file_owner(filename)` → classifies file as "developer" or "tester" based on name patterns
- `_last_file_in_traceback(stderr)` → extracts the deepest file in the traceback chain

### 4.5 Surgical Patching System (4 files, ~1,040 lines)

When targeted fixes are needed (not full file rewrites):

1. **Error Localizer** (`error_localizer.py`, 248 lines) — Pinpoints exact error location in source
2. **Project Mapper** (`project_mapper.py`, 298 lines) — Builds complete AST map of project (classes, methods, functions, imports)
3. **Surgical Patcher** (`surgical_patcher.py`, 329 lines) — Applies minimal patches to fix specific errors
4. **Verifier** (`verifier.py`, 165 lines) — Confirms the fix resolved the original error without regressions

### 4.6 Syntax Fixer (`syntax_fixer.py` — 240 lines)

Multi-pass auto-repair for common LLM code generation errors:

| Fixer | What it fixes |
|-------|---------------|
| `_fix_fstring_quotes()` | State-machine parser detects f-string quote conflicts |
| `_fix_unterminated_strings_triple()` | Converts single-quoted multi-line strings to triple quotes |
| `_fix_unterminated_strings()` | Closes unclosed string literals |
| `_fix_unbalanced_brackets()` | Balances `()`, `[]`, `{}` |
| `_fix_bare_except()` | Converts `except:` to `except Exception:` |
| `_fix_missing_colons()` | Adds missing colons after `def`, `class`, `if`, `for`, `while` |

### 4.7 Memory System (`memory.py` — 450 lines)

ChromaDB-backed persistent cross-session learning:

- **Lesson Store**: Saves what went wrong and how it was fixed
  - Each lesson: `{error_pattern, solution, context, success_count, failure_count}`
  - Similarity search using `all-MiniLM-L6-v2` sentence embeddings
- **Solution Store**: Saves successful code solutions indexed by request
- **Memory Recall** (pipeline node): Retrieves top-K similar lessons before Developer starts
- **Memory Save** (pipeline node): Stores new lessons on successful completion
- Configurable: `top_k=3`, `min_similarity=0.7`

### 4.8 Error Graph (`error_graph.py` — 257 lines)

Tracks error recurrence across iterations with an escalation policy:

```
fix → change_approach → isolate → rollback → escalate
```

- Errors identified by normalized hash (line numbers and addresses stripped)
- Each recurrence increments the error's count
- After `escalate_after` occurrences (default: 5), the pipeline escalates
- Escalation changes the Judge's decision from REJECT to ESCALATE/ROLLBACK

### 4.9 Sandbox (`sandbox.py` — 142 lines)

Docker-based code execution with strict security:

```python
network_disabled = True
mem_limit = "512m"
cpu_limit = "1.0"
pids_limit = 50
timeout = 30s
image = "autodev-sandbox:latest"  # Pre-built with pytest, numpy, pandas, requests
```

- No pip install at runtime (container has no network)
- `PYTHONPATH=/app` for subdirectory package imports
- Workspace mounted as volume
- Automatic container cleanup on completion or timeout

### 4.10 Dependency Management (`deps.py` — 184 lines)

Validates imports against what's available in the sandbox:

- **stdlib detection**: Uses `sys.stdlib_module_names`
- **local module detection**: Scans workspace for `.py` files AND directories containing `.py` files
- **pre-installed packages**: `pytest`, `requests`, `numpy`, `pandas` + their submodules
- **module-to-package mapping**: `cv2→opencv-python`, `PIL→Pillow`, `sklearn→scikit-learn`, etc.
- `audit_dependencies()`: Classifies all workspace imports into builtin/preinstalled/local/missing

### 4.11 Context Manager (`context_manager.py` — 141 lines)

Token-aware file selection for LLM context:

- Indexes workspace files with size metadata
- Ranks files by relevance to current request
- Fills context window to ~70% capacity (reserves 30% for prompt + response)
- Token budget from `config.get_token_budget(model)` (default: 32,000)

### 4.12 LLM Client (`llm_client.py` — 352 lines)

OpenAI-compatible client for Ollama:

- Hash-based response caching: `SHA256(model + messages)` → cached response
- Structured output: Pydantic model validation with JSON extraction from LLM response
- Retry with exponential backoff on connection errors
- Token counting: `len(text) // 4` heuristic
- All calls logged via `ObservabilityLogger`

---

## 5. Retry & Routing Logic

### 5.1 Outer Loop (graph.py)

- **Max iterations**: 15 (configurable in `config.yaml`)
- **No-progress detection**: Requires **3 consecutive identical error hashes** before stopping
- **Error hash normalization**: Strips line numbers (`line \d+` → `line N`), memory addresses, timing values
- **Routing after retry**: Conditional edge sends to `tester` if error is in test file, or `developer` if error is in application code

### 5.2 Inner Loop (developer.py)

- **Max inner iterations**: 8
- Each iteration: generate → validate → auto-fix → re-validate
- Scope guard prevents touching files outside the plan
- Automatic `__init__.py` creation for subdirectory packages
- Missing typing imports detected and auto-fixed

### 5.3 Retry Target Classification

```python
# In prepare_retry():
if "test_" in traceback_file or traceback_file == "test_runner.py":
    retry_target = "tester"  # Route to tester, not developer
else:
    retry_target = "developer"

# In route_after_retry():
if state.get("retry_target") == "tester":
    return "tester"
return "developer"
```

---

## 6. Configuration System

### 6.1 Pydantic Config Models (`config.py` — 140 lines)

All settings are loaded from `config.yaml` via Pydantic models:

| Model | Key Settings |
|-------|-------------|
| `LLMConfig` | `base_url`, `api_key`, `timeout_seconds`, `cache_enabled` |
| `ModelsConfig` | `default` — the model name used by all agents |
| `AgentModelsConfig` | Per-agent model overrides (all default to `models.default`) |
| `ContextConfig` | `max_tokens_per_model`, `relevant_files_only`, `summarize_when_over_budget` |
| `SandboxConfig` | `backend`, `image`, `cpu_limit`, `memory_limit`, `timeout_seconds`, `network` |
| `LoopConfig` | `max_iterations=15`, `stop_if_no_progress=True`, `pass_full_attempt_history=True` |
| `GitConfig` | `auto_commit`, `rollback_on_repeated_error` |
| `ErrorGraphConfig` | `enabled`, `path`, `escalate_after=5` |
| `MemoryConfig` | `enabled`, `db_path`, `embedding_model`, `top_k=3`, `min_similarity=0.7` |

### 6.2 Model Swapping

To switch models, change ONE line in `config.yaml`:
```yaml
models:
  default: "llama3.1:8b"  # or any Ollama model
```

Per-agent overrides also supported:
```yaml
agent_models:
  developer: "qwen3-coder:latest"
  tester: "codellama:13b"
```

---

## 7. User Interfaces

### 7.1 Rich CLI (`main.py` — 292 lines)

- Phase indicators with icons: brain (Architect), laptop (Developer), test tube (Tester), magnifying glass (Reviewer)
- Plan display with approval prompt (human-in-the-loop)
- Iteration progress with attempt history
- Final result: success with file listing, or failure with diagnosis
- Color-coded status: green (success), red (failure), yellow (approval), cyan (architect)

### 7.2 Web UI (`autodev-ui/` — ~755 lines)

- **Dual mode**: Chat Mode (direct LLM conversation) + Agent Mode (full pipeline)
- **WebSocket streaming**: Real-time agent updates as pipeline runs
- **Model selector**: Dropdown populated from Ollama API
- **FastAPI backend**: Serves static files + WebSocket endpoint

---

## 8. Complete Commit History (44 commits)

| # | Hash | Description |
|---|------|-------------|
| 44 | `7fbf7b4` | Eliminate hallucinated debugging: deterministic Debugger/Reviewer/Judge when tests fail |
| 43 | `fd4d68f` | Fix 4 systemic pipeline issues: validation depth, error classification, auto-repair, subdirectory support |
| 42 | `3037c76` | Fix local package detection and unterminated string auto-repair |
| 41 | `935861a` | Fix test file handling: scope guard path matching, tester retry routing, dynamic test file detection |
| 40 | `be8efa5` | Fix 3 architectural issues: file scope leaking, reviewer/tester mismatch, shallow tests |
| 39 | `0bdc54b` | Fix LLM generating pydantic imports that fail in sandbox |
| 38 | `b300670` | Fix 'No models found' being sent as model name to Ollama |
| 37 | `9215545` | Fix empty model dropdown and 'invalid model name' error |
| 36 | `02e03bb` | Fix self-healing verification failures: f-string detection, healing loop duplicates, workspace paths |
| 35 | `6b4a28c` | Add self-healing developer with surgical locate→inspect→patch→verify loop |
| 34 | `6ad942f` | Add persistent memory system — ChromaDB-backed cross-session learning |
| 33 | `df37a39` | Add deterministic code healer — zero-LLM fixer runs before sandbox |
| 32 | `f97defd` | Add keyword argument auto-fixer and argument mismatch detection |
| 31 | `0703d16` | Fix retry loop failures: class-based state rules, error normalization, anti-pattern detection |
| 30 | `3012d8d` | Fix 3 bugs from deep audit: error graph reset, debugger affected_files, unused imports |
| 29 | `7ee17fb` | Evolve autodev into AI Software Company Runtime with 7-agent pipeline |
| 28 | `24c97ae` | Add self-healing system: diagnostics, structured outputs, pre-flight validation |
| 27 | `2a3e4be` | Add syntax fixer for LLM-generated code |
| 26 | `d6203ef` | Fix truncated LLM responses causing JSONParseError |
| 25 | `7ed7b53` | Add automatic import fixer for LLM-generated code |
| 24 | `762ddce` | Strengthen developer and tester prompts for robust code generation |
| 23 | `b08b342` | Fix retry routing and add priority sorting rule |
| 22 | `fe83fc6` | Fix circular retry loop: route test bugs to tester, not developer |
| 21 | `05279f9` | Fix sandbox trying to pip install stdlib modules like json |
| 20 | `0d5e78f` | Fix reviewer rejecting code for missing test coverage |
| 19 | `09a3bfd` | Fix JSONParseError: handle Python triple-quoted strings in LLM JSON output |
| 18 | `788c463` | Fix agent pipeline WebSocket: bulletproof sends with agent_update messages |
| 17 | `4945b66` | Fix regression: restore working Chat Mode, isolate Agent Mode |
| 16 | `ecf0769` | Fix double WebSocket bug causing Agent Mode to silently fail |
| 15 | `72b3ec1` | Fix WebSocket deadlock and race conditions in AutoDev Chat UI |
| 14 | `39f5cdb` | Set default model to qwen2.5-coder:7b |
| 13 | `7aa8bd1` | Allow switching models from UI dropdown in both Agent and Chat modes |
| 12 | `2bb07a2` | Integrate 4-agent LangGraph pipeline into AutoDev Chat UI |
| 11 | `aa395d1` | Add pre-built sandbox image, stop runtime pip install in no-network container |
| 10 | `79c732a` | Fix sandbox/deps bugs: skip local modules, set PYTHONPATH, update reviewer prompt |
| 09 | `9c5ba4c` | Harden JSON parsing in llm_client with retry mechanism |
| 08 | `2c7aa03` | Fix NameError: add missing Command import in main.py |
| 07 | `23e8de1` | Add AutoDev Chat — local web UI connecting to Ollama |
| 06 | `5b3c834` | Add Phase 5: eval suite, README, Makefile, and model update script |
| 05 | `2c6229a` | Add Phase 4: LangGraph orchestration and rich CLI |
| 04-01 | (earlier) | Phases 1-3: Foundation, Agents, Safety & Context |

---

## 9. Key Problems Discovered & Fixed

### 9.1 File Scope Leaking (Commit `be8efa5`)

**Problem**: Developer agent touched files unrelated to the current task, causing cascading failures.

**Fix**: Added scope guard that filters generated files to only those in `plan.files_needed` + `test_runner.py`. Added stale file cleanup to remove old files not in the plan.

### 9.2 Scope Guard Path Mismatch (Commit `935861a`)

**Problem**: Plan listed `autodev/error_localizer.py` but LLM generated `error_localizer.py`. Exact match failed, all files filtered out → 0-byte output.

**Fix**: Match on both full path AND basename (`Path(p).name`).

### 9.3 Test File Naming Mismatch (Commit `935861a`)

**Problem**: Sandbox hardcoded `python test_runner.py` but tester generated files like `test_complex_healing.py`.

**Fix**: Dynamic test file detection using `workspace.glob("test_*.py")`.

### 9.4 Unterminated String Literals (Commit `3037c76`)

**Problem**: LLM generated `f'def {name}(self):\n    pass\n'` with actual newlines inside single-quoted strings.

**Fix**: New `_fix_unterminated_strings_triple()` that converts to triple-quoted strings. Pre-flight validation now auto-applies syntax fix before failing.

### 9.5 Local Package Detection (Commit `3037c76`)

**Problem**: `deps.py` only checked `workspace.glob("*.py")`, missed subdirectory packages like `autodev/`.

**Fix**: `_local_module_names()` now also checks directories containing `.py` files. Proactive `__init__.py` creation in Developer and prepare_retry.

### 9.6 Missing Typing Imports (Commit `fd4d68f`)

**Problem**: LLM generated `-> List[str]` without `from typing import List`.

**Fix**: `_validate()` now AST-checks for typing names used but not imported. `_fix_missing_typing_imports()` auto-adds them.

### 9.7 Hallucinated Debugging — CRITICAL (Commit `7fbf7b4`)

**Problem**: When `diagnostics.diagnose_traceback()` returned `confident=False`, the system sent the error to LLM for diagnosis. The LLM hallucinated contradictory root causes across iterations:
- Iteration 1: "missing self parameter"
- Iteration 2: "method not implemented"
- Iteration 3: "wrong constructor signature"

All for the same unchanged file. The Reviewer and Judge contradicted each other (one said test bug, other said code bug).

**Fix**: Eliminated all LLM fallback in debugging pipeline:
- Debugger: uses last stderr line directly (never calls LLM)
- Reviewer: deterministic rejection when tests fail (no LLM call)
- Judge: deterministic REJECT when tests fail (no LLM call)
- LLM calls only happen when tests PASS and nuanced judgment is needed

### 9.8 Premature Stopping (Commit `7fbf7b4`)

**Problem**: Pipeline stopped after 5 iterations. User expected it to continue until clean code.

**Fix**: `max_iterations` 5→15, `MAX_INNER_ITERATIONS` 5→8, no-progress detection requires 3 consecutive identical error hashes (was 1).

### 9.9 Tester Retry Routing (Commit `935861a`)

**Problem**: When test file had SyntaxError, system retried Developer who can't fix Tester's file.

**Fix**: Added `retry_target` to state. Conditional edge routes to `tester` when error is in test file.

### 9.10 Pydantic in Sandbox (Commit `0bdc54b`)

**Problem**: LLM generated code using Pydantic, but sandbox image doesn't have Pydantic installed.

**Fix**: Code Healer auto-converts Pydantic models to stdlib `dataclasses`.

---

## 10. Evolution Timeline

### Phase 1 — Foundation
- Configuration system, LLM client, observability
- Pydantic models for type-safe settings
- OpenAI-compatible Ollama integration

### Phase 2 — Agents (4 agents)
- Architect → Developer → Tester → Reviewer
- Basic LangGraph pipeline with retry loop

### Phase 3 — Safety & Context
- Docker sandbox with security constraints
- Dependency validation
- Context-aware file selection

### Phase 4 — Orchestration
- LangGraph StateGraph compilation
- Rich CLI with phase indicators
- Human-in-the-loop approval gate

### Phase 5 — Evaluation & Packaging
- 10 eval tasks (trivial to hard)
- Makefile, README, model update script

### Phase 6 — Web UI
- FastAPI + WebSocket server
- Dual mode: Chat + Agent
- Model selector dropdown

### Phase 7 — Hardening
- JSON parse hardening with retry
- Sandbox pip install fix (skip stdlib, set PYTHONPATH)
- WebSocket deadlock fixes

### Phase 8 — AI Software Company (7 agents)
- Added Product Manager, Debugger, Judge
- Self-healing diagnostics
- Structured LLM outputs
- Syntax fixer

### Phase 9 — Self-Healing
- Deterministic code healer (zero LLM)
- Persistent memory (ChromaDB)
- Import fixer, kwarg fixer
- Self-healing developer inner loop
- Error graph with escalation

### Phase 10 — Systemic Fixes
- Scope guards, retry routing
- Deterministic debugging (no hallucination)
- Subdirectory package support
- Typing import auto-fix
- Max iterations 5→15

---

## 11. Dependencies

```
langgraph        — Pipeline orchestration
openai           — LLM client (Ollama-compatible)
pydantic         — Config and schema validation
pyyaml           — YAML config loading
rich             — CLI formatting
docker           — Sandbox execution
chromadb         — Persistent memory (vector search)
sentence-transformers — Embedding model for memory
fastapi          — Web UI backend
uvicorn          — ASGI server
websockets       — Real-time updates
```

---

## 12. Current System Ratings (User's Assessment)

| Agent | Rating | Notes |
|-------|--------|-------|
| Product Manager | 8/10 | Good scope definition |
| Architect | 8/10 | Solid plans |
| Developer | 5/10 | Code quality improving with self-healing |
| Tester | 2/10 | Still generates shallow tests |
| Debugger | 1→5/10 | Was hallucinating, now deterministic |
| Reviewer | 2→5/10 | Was contradicting, now deterministic |
| Judge | 3→5/10 | Was contradicting, now deterministic |

**Overall pipeline**: Moved from repeated failure loops to functional retry with targeted fixes. The deterministic debugging pipeline eliminated the worst failure mode (hallucinated contradictory diagnoses).

---

## 13. Known Remaining Challenges

1. **Tester quality**: Still generates shallow tests that don't cover edge cases well
2. **Developer code quality**: Inner loop catches many issues but LLM still generates problematic patterns
3. **Context window pressure**: 32K context fills up quickly with attempt history
4. **First-iteration success rate**: Low — system relies heavily on retry loop
5. **Complex multi-file projects**: Subdirectory support added but not battle-tested on large projects

---

## 14. How to Run

```bash
# Install
make install

# Configure Ollama model
ollama pull qwen2.5-coder:7b

# Build sandbox image
docker build -t autodev-sandbox:latest ./docker/

# Run CLI
make run
# or
python -m autodev.main "Create a REST API for a todo app"

# Run Web UI
cd autodev-ui && uvicorn main:app --port 8000

# Run evaluation
make eval
```

---

*Report generated from branch `claude/confident-lamport-f1hvnk` with 44 commits, 33 Python source files (7,208 lines), 7 prompt files, 10 eval tasks, and a web UI.*
