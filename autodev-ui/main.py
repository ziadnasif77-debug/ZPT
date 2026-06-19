"""FastAPI backend — REST endpoints + WebSocket streaming."""

from __future__ import annotations

import json
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


@dataclass
class Conversation:
    id: str
    title: str = "New Chat"
    messages: list[dict] = field(default_factory=list)


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


@app.websocket("/ws/chat/{cid}")
async def chat_ws(ws: WebSocket, cid: str):
    await ws.accept()
    if cid not in conversations:
        conversations[cid] = Conversation(id=cid)
    conv = conversations[cid]

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            model = data.get("model", get_default_model())
            user_content = data.get("content", "")
            file_context = data.get("files", [])

            parts = []
            for fc in file_context:
                parts.append(f"[File: {fc['name']}]\n```\n{fc['content']}\n```")
            parts.append(user_content)
            full_content = "\n\n".join(parts)

            user_msg = {"role": "user", "content": full_content}
            conv.messages.append(user_msg)

            if conv.title == "New Chat" and len(conv.messages) == 1:
                conv.title = user_content[:50].strip() or "New Chat"

            api_messages = [{"role": m["role"], "content": m["content"]} for m in conv.messages]

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
                    continue

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

    except WebSocketDisconnect:
        pass
