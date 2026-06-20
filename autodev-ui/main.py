"""FastAPI backend — REST endpoints + WebSocket streaming.

Supports two modes:
- Agent Mode: routes messages through the 4-agent LangGraph pipeline
- Chat Mode: direct Ollama conversation (pass-through)
"""

from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import ollama_client
from config import get_default_model

app = FastAPI(title="AutoDev Chat")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_graph_instance = None
_config_instance = None
_sys_path_added = False


def _get_pipeline():
    """Lazy-init the LangGraph pipeline so import errors don't block Chat Mode."""
    global _graph_instance, _config_instance, _sys_path_added
    if _graph_instance is not None:
        return _graph_instance, _config_instance

    import sys
    src_path = str(Path(__file__).resolve().parent.parent / "src")
    if not _sys_path_added:
        sys.path.insert(0, src_path)
        _sys_path_added = True

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
    return {"ollama": ok}


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
    print(f"[WS] WebSocket accepted for conversation {cid}", flush=True)

    await ws.send_text(json.dumps({"type": "connected", "message": "Ready"}))

    if cid not in conversations:
        conversations[cid] = Conversation(id=cid)
    conv = conversations[cid]

    try:
        while True:
            raw = await ws.receive_text()
            print(f"[WS] Received message: {raw[:200]}", flush=True)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print("[WS] Invalid JSON received", flush=True)
                await ws.send_text(json.dumps({"type": "error", "content": "Invalid JSON message"}))
                continue

            msg_type = data.get("type", "message")

            if msg_type == "plan_decision":
                decision = data.get("approved", False)
                print(f"[WS] Plan decision: {'approved' if decision else 'rejected'}", flush=True)
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
            print(f"[WS] Mode={mode}, Model={model}, Content={user_content[:100]}", flush=True)

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
                    print("[WS] Starting agent pipeline...", flush=True)
                    await _run_agent_pipeline(ws, conv, user_content, model)
                    print("[WS] Agent pipeline finished", flush=True)
                else:
                    print("[WS] Starting chat mode...", flush=True)
                    await _run_chat_mode(ws, conv, model)
                    print("[WS] Chat mode finished", flush=True)
            except Exception as exc:
                tb = traceback.format_exc()
                print(f"[WS] Unhandled error: {exc}\n{tb}", flush=True)
                await ws.send_text(json.dumps({
                    "type": "error",
                    "content": f"Unhandled error: {type(exc).__name__}: {exc}\n\n```\n{tb}\n```",
                }))

    except WebSocketDisconnect:
        print(f"[WS] WebSocket disconnected for {cid}", flush=True)
    except Exception as exc:
        print(f"[WS] Unexpected error in WS loop: {exc}\n{traceback.format_exc()}", flush=True)


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
    """Run the 4-agent LangGraph pipeline, sending agent_update after each node."""
    try:
        from langgraph.types import Command
        print("[PIPELINE] Initializing pipeline...", flush=True)
        compiled, config = _get_pipeline()
        print("[PIPELINE] Pipeline initialized OK", flush=True)
    except Exception as exc:
        print(f"[PIPELINE] Init failed: {exc}\n{traceback.format_exc()}", flush=True)
        await _ws_send(ws, {"type": "error", "content": f"Failed to initialize pipeline: {exc}"})
        return

    if model and model != config.models.default:
        config.models.default = model
        for agent in ("product_manager", "architect", "developer", "tester",
                       "debugger", "reviewer", "judge"):
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
        "product_spec": None,
        "plan": None,
        "plan_approved": False,
        "code_bundle": None,
        "test_result": None,
        "debug_report": None,
        "review": None,
        "judge_decision": None,
        "iteration": 0,
        "attempt_history": [],
        "error_hashes": [],
        "error_graph_context": "",
        "final_status": "",
        "stop_reason": "",
        "feedback": "",
        "modified_files": [],
    }

    await _ws_send(ws, {"type": "pipeline_start", "model": config.models.default})
    print(f"[PIPELINE] Sent pipeline_start, model={config.models.default}", flush=True)

    try:
        current_input = initial_state

        while True:
            print(f"[PIPELINE] Streaming graph (input type: {type(current_input).__name__})...", flush=True)
            events = await asyncio.to_thread(
                lambda ci=current_input: list(compiled.stream(ci, thread_config, stream_mode="updates"))
            )
            print(f"[PIPELINE] Got {len(events)} events", flush=True)

            for event in events:
                for node_name, node_data in event.items():
                    if node_name == "__interrupt__":
                        continue
                    print(f"[PIPELINE] Node finished: {node_name}", flush=True)
                    await _send_agent_update(ws, node_name, node_data)

            snapshot = await asyncio.to_thread(compiled.get_state, thread_config)

            if not snapshot.next:
                final = snapshot.values
                status = final.get("final_status", "unknown")
                summary = _build_summary_text(final, workspace)
                files_info = _collect_files(final, workspace)
                await _ws_send(ws, {
                    "type": "done",
                    "content": summary,
                    "status": status,
                    "stop_reason": final.get("stop_reason", ""),
                    "iterations": final.get("iteration", 0),
                    "files": files_info,
                })
                conv.messages.append({"role": "assistant", "content": summary})
                return

            if snapshot.next == ("approval_gate",):
                plan_data = None
                for task in snapshot.tasks:
                    for intr in getattr(task, "interrupts", []):
                        val = getattr(intr, "value", None)
                        if isinstance(val, dict) and val.get("type") == "plan_approval":
                            plan_data = val.get("plan")

                if plan_data:
                    await _ws_send(ws, {"type": "plan_approval", "plan": _to_serializable(plan_data)})
                    print("[PIPELINE] Sent plan_approval, waiting for user...", flush=True)

                    approved = await _wait_for_plan_decision(ws, conv)
                    print(f"[PIPELINE] Plan decision: {'approved' if approved else 'rejected'}", flush=True)

                    current_input = Command(resume="yes" if approved else "no")
                    continue

            final = snapshot.values
            summary = _build_summary_text(final, workspace)
            files_info = _collect_files(final, workspace)
            await _ws_send(ws, {
                "type": "done",
                "content": summary,
                "status": final.get("final_status", "unknown"),
                "stop_reason": final.get("stop_reason", ""),
                "iterations": final.get("iteration", 0),
                "files": files_info,
            })
            conv.messages.append({"role": "assistant", "content": summary})
            return

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[PIPELINE] Error: {exc}\n{tb}", flush=True)
        await _ws_send(ws, {
            "type": "error",
            "content": f"Pipeline error: {type(exc).__name__}: {exc}\n\n```\n{tb}\n```",
        })


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
        await _ws_send(ws, {
            "type": "error",
            "content": "Plan approval timed out after 10 minutes. Rejecting plan.",
        })
    finally:
        conv._pending_approval = None
        if not reader_task.done():
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass

    return approved


def _to_serializable(obj: object) -> object:
    """Recursively convert any object to JSON-safe primitives."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return _to_serializable(obj.model_dump())
    if hasattr(obj, "__dict__"):
        return _to_serializable(vars(obj))
    return str(obj)


async def _ws_send(ws: WebSocket, msg: dict) -> bool:
    """Send a JSON message over WebSocket with error handling."""
    try:
        payload = json.dumps(_to_serializable(msg))
        await ws.send_text(payload)
        print(f"[WS-SEND] type={msg.get('type')} len={len(payload)}", flush=True)
        return True
    except Exception as exc:
        print(f"[WS-SEND] FAILED: {exc}", flush=True)
        return False


async def _send_agent_update(ws: WebSocket, node: str, data: dict):
    """Send an agent_update message after a graph node completes."""
    content = {}
    if node == "product_manager":
        spec = data.get("product_spec") or {}
        content = {
            "scope": spec.get("scope", ""),
            "milestones": spec.get("milestones", []),
            "success_criteria": spec.get("success_criteria", []),
        }
    elif node == "architect":
        content = data.get("plan") or {}
    elif node == "developer":
        cb = data.get("code_bundle") or {}
        modified = data.get("modified_files", [])
        file_list = [f.get("path", "?") for f in cb.get("files", [])] if cb.get("files") else []
        content = file_list
        if modified:
            content = {"files": file_list, "modified": modified}
    elif node == "tester":
        tr = data.get("test_result") or {}
        content = {
            "passed": tr.get("passed", False),
            "exit_code": tr.get("exit_code"),
            "stderr": (tr.get("stderr") or "")[:1000],
            "stdout": (tr.get("stdout") or "")[:500],
        }
    elif node == "debugger":
        dr = data.get("debug_report") or {}
        content = {
            "root_cause": dr.get("root_cause", ""),
            "affected_files": dr.get("affected_files", []),
            "error_category": dr.get("error_category", ""),
        }
    elif node == "reviewer":
        rv = data.get("review") or {}
        content = {
            "approved": rv.get("approved", False),
            "summary": rv.get("summary", ""),
            "comments": rv.get("comments", []),
        }
    elif node == "judge":
        jd = data.get("judge_decision") or {}
        content = {
            "decision": jd.get("decision", ""),
            "reason": jd.get("reason", ""),
            "strategy": jd.get("strategy", ""),
        }
    elif node == "prepare_retry":
        content = {"iteration": data.get("iteration", 0)}
    elif node == "done":
        content = {"final_status": "success"}
    elif node == "failed":
        content = {"stop_reason": data.get("stop_reason", "")}
    else:
        content = {}

    await _ws_send(ws, {
        "type": "agent_update",
        "agent": node,
        "status": "done",
        "content": content,
    })


def _collect_files(state: dict, workspace: Path) -> list[dict]:
    """Collect file info from pipeline state for the final result."""
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
    return files_info


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
