"""FastAPI backend — REST endpoints + WebSocket streaming.

Supports two modes:
- Agent Mode: routes messages through the 4-agent LangGraph pipeline
- Chat Mode: direct Ollama conversation (pass-through)
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import ollama_client
from config import get_default_model

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

app = FastAPI(title="AutoDev Chat")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_graph_instance = None
_config_instance = None


def _get_pipeline():
    """Lazy-init the LangGraph pipeline so import errors don't block Chat Mode."""
    global _graph_instance, _config_instance
    if _graph_instance is not None:
        return _graph_instance, _config_instance

    from autodev.config import load_config
    from autodev.context_manager import ContextManager
    from autodev.graph import build_graph
    from autodev.llm_client import LLMClient
    from autodev.observability import ObservabilityLogger
    from autodev.sandbox import Sandbox, SandboxUnavailable

    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    config = load_config(str(config_path))
    _config_instance = config

    logger = ObservabilityLogger(config.observability.log_dir)
    llm = LLMClient(config, logger)
    context_mgr = ContextManager(config, llm)

    sandbox = None
    try:
        sandbox = Sandbox(config.sandbox)
    except SandboxUnavailable:
        pass

    _graph_instance = build_graph(config, llm, sandbox, context_mgr)
    return _graph_instance, config


@dataclass
class Conversation:
    id: str
    title: str = "New Chat"
    messages: list[dict] = field(default_factory=list)
    _pending_approval: asyncio.Future | None = field(default=None, repr=False)


conversations: dict[str, Conversation] = {}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (Path(__file__).parent / "static" / "index.html").read_text()


@app.get("/api/health")
async def health():
    ok = await ollama_client.check_health()
    pipeline_ok = False
    try:
        _get_pipeline()
        pipeline_ok = True
    except Exception:
        pass
    return {"ollama": ok, "pipeline": pipeline_ok}


@app.get("/api/models")
async def models():
    model_list = await ollama_client.list_models()
    return {"models": model_list, "default": get_default_model()}


@app.get("/api/conversations")
async def list_conversations():
    return [
        {"id": c.id, "title": c.title, "message_count": len(c.messages)}
        for c in conversations.values()
    ]


@app.post("/api/conversations")
async def create_conversation():
    cid = uuid.uuid4().hex[:12]
    conversations[cid] = Conversation(id=cid)
    return {"id": cid, "title": "New Chat"}


@app.get("/api/conversations/{cid}")
async def get_conversation(cid: str):
    conv = conversations.get(cid)
    if not conv:
        return {"error": "not found"}, 404
    return {"id": conv.id, "title": conv.title, "messages": conv.messages}


@app.delete("/api/conversations/{cid}")
async def delete_conversation(cid: str):
    conversations.pop(cid, None)
    return {"ok": True}


@app.post("/api/upload")
async def upload_files(files: list[UploadFile]):
    results = []
    allowed = {".py", ".js", ".ts", ".txt", ".md", ".yaml", ".yml", ".json", ".pdf", ".toml", ".cfg", ".sh"}
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in allowed:
            results.append({"name": f.filename, "error": f"Unsupported file type: {ext}"})
            continue
        content = await f.read()
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            text = content.decode("latin-1", errors="replace")
        results.append({"name": f.filename, "content": text})
    return {"files": results}


@app.get("/api/workspace/files")
async def list_workspace_files():
    """List files in the workspace directory."""
    try:
        import yaml
        config_path = Path(__file__).resolve().parent.parent / "config.yaml"
        raw = yaml.safe_load(config_path.read_text())
        ws_dir = raw.get("workspace_dir", "./workspace")
        workspace = (Path(__file__).resolve().parent.parent / ws_dir).resolve()
        if not workspace.is_dir():
            return {"files": []}
        files = []
        for p in sorted(workspace.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(workspace))
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    content = None
                files.append({"path": rel, "size": p.stat().st_size, "content": content})
        return {"files": files}
    except Exception as e:
        return {"files": [], "error": str(e)}


@app.websocket("/ws/chat/{cid}")
async def chat_ws(ws: WebSocket, cid: str):
    await ws.accept()
    print(f"[WS] WebSocket accepted for conversation {cid}")

    await ws.send_text(json.dumps({"type": "connected", "message": "Ready"}))

    if cid not in conversations:
        conversations[cid] = Conversation(id=cid)
    conv = conversations[cid]

    try:
        while True:
            raw = await ws.receive_text()
            print(f"[WS] Received message: {raw[:200]}")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print("[WS] Invalid JSON received")
                await ws.send_text(json.dumps({"type": "error", "content": "Invalid JSON message"}))
                continue

            msg_type = data.get("type", "message")

            if msg_type == "plan_decision":
                decision = data.get("approved", False)
                print(f"[WS] Plan decision: {'approved' if decision else 'rejected'}")
                conv.messages.append({
                    "role": "system",
                    "content": f"Plan {'approved' if decision else 'rejected'} by user",
                })
                if conv._pending_approval is not None and not conv._pending_approval.done():
                    conv._pending_approval.set_result(decision)
                continue

            mode = data.get("mode", "agent")
            model = data.get("model", get_default_model())
            user_content = data.get("content", "")
            file_context = data.get("files", [])
            print(f"[WS] Mode={mode}, Model={model}, Content={user_content[:100]}")

            parts = []
            for fc in file_context:
                parts.append(f"[File: {fc['name']}]\n```\n{fc['content']}\n```")
            parts.append(user_content)
            full_content = "\n\n".join(parts)

            user_msg = {"role": "user", "content": full_content}
            conv.messages.append(user_msg)

            if conv.title == "New Chat" and len(conv.messages) == 1:
                conv.title = user_content[:50].strip() or "New Chat"

            try:
                if mode == "agent":
                    print(f"[WS] Starting agent pipeline...")
                    await _run_agent_pipeline(ws, conv, user_content, model)
                    print(f"[WS] Agent pipeline finished")
                else:
                    print(f"[WS] Starting chat mode...")
                    await _run_chat_mode(ws, conv, model)
                    print(f"[WS] Chat mode finished")
            except Exception as exc:
                tb = traceback.format_exc()
                print(f"[WS] Unhandled error: {exc}\n{tb}")
                await ws.send_text(json.dumps({
                    "type": "error",
                    "content": f"Unhandled error: {type(exc).__name__}: {exc}\n\n```\n{tb}\n```",
                }))

    except WebSocketDisconnect:
        print(f"[WS] WebSocket disconnected for {cid}")
    except Exception as exc:
        print(f"[WS] Unexpected error in WS loop: {exc}\n{traceback.format_exc()}")


async def _run_chat_mode(ws: WebSocket, conv: Conversation, model: str):
    """Direct Ollama chat — same as original."""
    api_messages = [{"role": m["role"], "content": m["content"]} for m in conv.messages
                    if m["role"] in ("user", "assistant")]

    assistant_content = ""
    try:
        healthy = await ollama_client.check_health()
        if not healthy:
            await ws.send_text(json.dumps({
                "type": "error",
                "content": (
                    "Ollama is not running. Start it with:\n\n"
                    "```bash\nollama serve\n```\n\n"
                    "Then pull a model:\n\n"
                    f"```bash\nollama pull {model}\n```"
                ),
            }))
            conv.messages.pop()
            return

        await ws.send_text(json.dumps({"type": "start"}))

        async for token in ollama_client.stream_chat(model, api_messages):
            assistant_content += token
            await ws.send_text(json.dumps({"type": "token", "content": token}))

        await ws.send_text(json.dumps({"type": "done"}))
        conv.messages.append({"role": "assistant", "content": assistant_content})

    except Exception as exc:
        err_msg = f"Error: {type(exc).__name__}: {exc}"
        await ws.send_text(json.dumps({"type": "error", "content": err_msg}))
        if assistant_content:
            conv.messages.append({"role": "assistant", "content": assistant_content})
        else:
            conv.messages.pop()


async def _run_agent_pipeline(ws: WebSocket, conv: Conversation, request: str, model: str | None = None):
    """Run the full 4-agent LangGraph pipeline, streaming progress to the UI.

    Plan approval uses a background task to read WebSocket messages while
    the pipeline is blocked waiting for user input — this avoids the deadlock
    where the main read loop can't process plan_decision messages.
    """
    from langgraph.types import Command

    try:
        print("[PIPELINE] Initializing pipeline...")
        compiled, config = _get_pipeline()
        print("[PIPELINE] Pipeline initialized OK")
    except Exception as exc:
        print(f"[PIPELINE] Init failed: {exc}\n{traceback.format_exc()}")
        await ws.send_text(json.dumps({
            "type": "error",
            "content": f"Failed to initialize pipeline: {type(exc).__name__}: {exc}",
        }))
        return

    if model and model != config.models.default:
        config.models.default = model
        for agent in ("architect", "developer", "tester", "reviewer"):
            setattr(config.agent_models, agent, model)

    import yaml
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    raw = yaml.safe_load(config_path.read_text())
    ws_dir = raw.get("workspace_dir", "./workspace")
    workspace = (Path(__file__).resolve().parent.parent / ws_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    thread_id = str(uuid.uuid4())
    thread_config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "user_request": request,
        "workspace_path": str(workspace),
        "plan": None,
        "plan_approved": False,
        "code_bundle": None,
        "test_result": None,
        "review": None,
        "iteration": 0,
        "attempt_history": [],
        "error_hashes": [],
        "final_status": "",
        "stop_reason": "",
        "feedback": "",
    }

    await ws.send_text(json.dumps({"type": "pipeline_start", "model": config.models.default}))
    print(f"[PIPELINE] Sent pipeline_start, model={config.models.default}")

    try:
        current_input = initial_state

        while True:
            print(f"[PIPELINE] Streaming graph (input type: {type(current_input).__name__})...")
            events = await asyncio.to_thread(
                lambda ci=current_input: list(compiled.stream(ci, thread_config, stream_mode="updates"))
            )
            print(f"[PIPELINE] Got {len(events)} events")

            for event in events:
                for node_name, node_data in event.items():
                    if node_name == "__interrupt__":
                        continue
                    print(f"[PIPELINE] Phase: {node_name}")
                    await _send_phase_update(ws, node_name, node_data)

            snapshot = await asyncio.to_thread(compiled.get_state, thread_config)

            if not snapshot.next:
                await _send_final_result(ws, snapshot.values, workspace)
                conv.messages.append({
                    "role": "assistant",
                    "content": _build_summary_text(snapshot.values, workspace),
                })
                return

            if snapshot.next == ("approval_gate",):
                plan_data = None
                for task in snapshot.tasks:
                    for intr in getattr(task, "interrupts", []):
                        val = getattr(intr, "value", None)
                        if isinstance(val, dict) and val.get("type") == "plan_approval":
                            plan_data = val.get("plan")

                if plan_data:
                    await ws.send_text(json.dumps({
                        "type": "plan_approval",
                        "plan": plan_data,
                    }))

                    approved = await _wait_for_plan_decision(ws, conv)

                    resume_val = "yes" if approved else "no"
                    current_input = Command(resume=resume_val)
                    continue

            await _send_final_result(ws, snapshot.values, workspace)
            conv.messages.append({
                "role": "assistant",
                "content": _build_summary_text(snapshot.values, workspace),
            })
            return

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[PIPELINE] Error: {exc}\n{tb}")
        await ws.send_text(json.dumps({
            "type": "error",
            "content": f"Pipeline error: {type(exc).__name__}: {exc}\n\n```\n{tb}\n```",
        }))


async def _wait_for_plan_decision(ws: WebSocket, conv: Conversation) -> bool:
    """Read WebSocket messages until we get a plan_decision.

    This solves the deadlock: the main read loop (chat_ws) is blocked inside
    _run_agent_pipeline, so we read directly from the WebSocket here.
    """
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    conv._pending_approval = future

    async def read_until_decision():
        try:
            while not future.done():
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "plan_decision":
                    decision = data.get("approved", False)
                    conv.messages.append({
                        "role": "system",
                        "content": f"Plan {'approved' if decision else 'rejected'} by user",
                    })
                    if not future.done():
                        future.set_result(decision)
                    return
        except WebSocketDisconnect:
            if not future.done():
                future.set_result(False)
        except Exception:
            if not future.done():
                future.set_result(False)

    reader_task = asyncio.create_task(read_until_decision())

    try:
        approved = await asyncio.wait_for(future, timeout=600)
    except asyncio.TimeoutError:
        approved = False
        await ws.send_text(json.dumps({
            "type": "error",
            "content": "Plan approval timed out after 10 minutes. Rejecting plan.",
        }))
    finally:
        conv._pending_approval = None
        if not reader_task.done():
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass

    return approved


async def _send_phase_update(ws: WebSocket, node: str, data: dict):
    """Send a phase progress message to the UI."""
    phase_map = {
        "architect": {"icon": "\U0001f9e0", "label": "Architect", "color": "cyan"},
        "approval_gate": {"icon": "⏸️", "label": "Approval", "color": "yellow"},
        "developer": {"icon": "\U0001f4bb", "label": "Developer", "color": "green"},
        "tester": {"icon": "\U0001f9ea", "label": "Tester", "color": "magenta"},
        "reviewer": {"icon": "\U0001f50d", "label": "Reviewer", "color": "blue"},
        "prepare_retry": {"icon": "\U0001f504", "label": "Retry", "color": "orange"},
        "done": {"icon": "✅", "label": "Done", "color": "green"},
        "failed": {"icon": "❌", "label": "Failed", "color": "red"},
    }

    info = phase_map.get(node, {"icon": "•", "label": node, "color": "gray"})

    detail = {}
    if node == "architect":
        detail = {"plan": data.get("plan")}
    elif node == "developer":
        cb = data.get("code_bundle")
        if cb:
            detail = {"files": [f["path"] for f in cb.get("files", [])]}
    elif node == "tester":
        tr = data.get("test_result")
        if tr:
            detail = {
                "passed": tr.get("passed", False),
                "exit_code": tr.get("exit_code"),
                "stderr": (tr.get("stderr") or "")[:1000],
                "stdout": (tr.get("stdout") or "")[:500],
            }
    elif node == "reviewer":
        rv = data.get("review")
        if rv:
            detail = {
                "approved": rv.get("approved", False),
                "summary": rv.get("summary", ""),
                "comments": rv.get("comments", []),
            }
    elif node == "prepare_retry":
        detail = {"iteration": data.get("iteration", 0)}
    elif node == "failed":
        detail = {"reason": data.get("stop_reason", "")}

    await ws.send_text(json.dumps({
        "type": "phase",
        "node": node,
        "icon": info["icon"],
        "label": info["label"],
        "color": info["color"],
        "iteration": data.get("iteration", 0),
        "detail": detail,
    }))


async def _send_final_result(ws: WebSocket, state: dict, workspace: Path):
    """Send the final pipeline result to the UI."""
    status = state.get("final_status", "unknown")
    cb = state.get("code_bundle") or {}
    files_info = []
    for f in cb.get("files", []):
        fp = workspace / f["path"]
        content = None
        size = 0
        if fp.exists():
            size = fp.stat().st_size
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        files_info.append({"path": f["path"], "size": size, "content": content})

    await ws.send_text(json.dumps({
        "type": "pipeline_done",
        "status": status,
        "stop_reason": state.get("stop_reason", ""),
        "iterations": state.get("iteration", 0),
        "files": files_info,
    }))


def _build_summary_text(state: dict, workspace: Path) -> str:
    """Build a text summary for conversation history."""
    status = state.get("final_status", "unknown")
    cb = state.get("code_bundle") or {}
    files = cb.get("files", [])
    if status == "success":
        lines = ["Pipeline completed successfully!"]
        if files:
            lines.append(f"Created {len(files)} file(s):")
            for f in files:
                lines.append(f"  - {f['path']}")
        lines.append(f"Workspace: {workspace}")
        return "\n".join(lines)
    return f"Pipeline stopped: {state.get('stop_reason', 'unknown reason')}"
