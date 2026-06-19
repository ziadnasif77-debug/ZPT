# AutoDev — Local AI Development Team

A fully local AI development team that takes natural language requests and produces tested, safe code. No paid APIs — runs entirely on your machine using Ollama.

```
User Request
     |
  Architect  -->  structured plan + acceptance criteria
     |
  Developer  -->  code implementation
     |
   Tester    -->  sandbox execution (Docker, isolated)
     |
  Reviewer   -->  approve or reject with feedback
     |
    Loop     -->  on failure: retry with full history
```

## Requirements

- Python 3.11+
- [Ollama](https://ollama.ai) installed and running
- Docker installed and running (for sandboxed code execution)

## Installation

```bash
# 1. Clone the repository
git clone <repo-url> && cd autodev

# 2. Install Ollama (if not already installed)
# See https://ollama.ai for your OS

# 3. Pull a coding model
ollama pull qwen3-coder:latest

# 4. Install autodev
make install
# or: pip install -e ".[dev]"

# 5. Verify Docker is running
docker info
```

## Usage

### Run AutoDev

```bash
make run
# or with a specific request:
python -m autodev.main "Write a Python function to sort a list using merge sort"
```

AutoDev will:
1. **Architect** analyzes your request and creates a plan
2. You **approve or reject** the plan (human-in-the-loop)
3. **Developer** writes the code
4. **Tester** runs it in a Docker sandbox (no network, limited CPU/RAM)
5. **Reviewer** checks code quality and test results
6. On failure: loops back to Developer with feedback (up to 5 retries)
7. Output is in `workspace/`

### View Logs

```bash
make logs
```

Every LLM call is logged as structured JSON in `logs/`. Each session creates two files:
- `{session_id}.jsonl` — summary (agent, model, tokens, latency)
- `{session_id}_detail.jsonl` — full prompts and responses

## Swapping the Model

The model name lives in **one place only**: `config.yaml` under `models.default`. No model names are hardcoded in source code.

### Option A: Automatic (recommended)

```bash
make update-model MODEL=qwen2.5-coder:14b
```

This pulls the model via Ollama, updates `config.yaml`, and verifies no hardcoded names leaked into `src/`.

### Option B: Manual

```bash
# 1. Pull the new model
ollama pull qwen2.5-coder:14b

# 2. Edit config.yaml — change this one line:
#    default: "qwen3-coder:latest"
#    to:
#    default: "qwen2.5-coder:14b"

# 3. Update token budget if the new model has a different context window:
#    max_tokens_per_model:
#      "qwen2.5-coder:14b": 32000

# 4. Restart autodev — no code changes needed
make run
```

### Per-agent Models (optional)

You can assign different models to different agents in `config.yaml`:

```yaml
agent_models:
  architect: "qwen3-coder:latest"    # planning needs reasoning
  developer: "qwen2.5-coder:14b"     # coding needs precision
  tester:    "default"                # uses models.default
  reviewer:  "default"               # uses models.default
```

## Benchmarking After a Model Swap

```bash
# Run the 10-task benchmark suite
make eval
```

This runs 10 tasks (trivial → hard) through the full pipeline and outputs a score:

```
Score: 7/10 tasks passed (70.0%)
Model: qwen2.5-coder:14b
  trivial: 1/1
  easy: 3/3
  medium: 2/3
  hard: 1/3
```

Results are saved to `eval/results/` with model name and timestamp for comparison across models.

## Configuration

All configuration is in `config.yaml`:

| Section | Key settings |
|---------|-------------|
| `llm` | `base_url` (default: Ollama), `timeout_seconds`, `cache_enabled` |
| `models` | `default` — the model used by all agents |
| `agent_models` | Per-agent model overrides (set to `"default"` to use `models.default`) |
| `context` | `max_tokens_per_model` — token budget per model, `summarize_when_over_budget` |
| `sandbox` | `image`, `cpu_limit`, `memory_limit`, `timeout_seconds`, `network` |
| `loop` | `max_iterations`, `stop_if_no_progress` |
| `human_in_the_loop` | `approve_plan` — pause for user approval after Architect |

### Using a Different LLM Backend

AutoDev uses the OpenAI-compatible API. Any backend that serves this API works:

```yaml
# LM Studio
llm:
  base_url: "http://localhost:1234/v1"

# vLLM
llm:
  base_url: "http://localhost:8000/v1"
```

## Security

- Generated code runs **only** inside Docker containers
- No network access in sandbox
- CPU and memory limits enforced (default: 1 CPU, 512MB)
- 30-second timeout kills runaway code
- If Docker is unavailable, AutoDev **refuses to run code** rather than falling back to unsafe execution

## Project Structure

```
├── config.yaml              # all settings (model names live here only)
├── pyproject.toml            # dependencies
├── Makefile                  # install / run / eval / logs / update-model
├── scripts/
│   ├── update_model.sh       # automated model swap
│   └── test_sandbox.py       # Docker sandbox security tests
├── prompts/                  # system prompts (editable)
│   ├── architect.md
│   ├── developer.md
│   ├── tester.md
│   └── reviewer.md
├── src/autodev/
│   ├── main.py               # CLI entry point
│   ├── config.py             # Pydantic settings
│   ├── llm_client.py         # LLM abstraction (caching + token budget)
│   ├── observability.py      # structured JSON logging
│   ├── context_manager.py    # relevant file selection + summarization
│   ├── sandbox.py            # Docker execution (security boundary)
│   ├── deps.py               # dependency detection
│   ├── state.py              # LangGraph state
│   ├── schemas.py            # Pydantic output schemas
│   ├── graph.py              # LangGraph orchestration
│   └── agents/               # agent implementations
├── eval/
│   ├── tasks/                # 10 benchmark tasks
│   └── run_eval.py           # evaluation runner
├── logs/                     # session logs (auto-created)
└── workspace/                # generated code output
```

## Makefile Commands

| Command | Description |
|---------|-------------|
| `make install` | Install autodev and dev dependencies |
| `make run` | Start the CLI |
| `make eval` | Run the 10-task benchmark suite |
| `make logs` | Show the latest session log |
| `make update-model MODEL=name` | Pull a new model and update config |
| `make clean` | Clear workspace, logs, and eval results |
