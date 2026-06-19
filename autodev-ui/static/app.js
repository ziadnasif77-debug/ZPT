/* ── AutoDev Chat — Frontend ──────────────────────────────── */

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  currentConv: null,
  ws: null,
  streaming: false,
  attachedFiles: [],
  abortController: null,
};

/* ── Init ──────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", async () => {
  checkOllamaHealth();
  loadModels();
  loadConversations();
  setupEventListeners();
  setInterval(checkOllamaHealth, 30000);
});

/* ── Health Check ──────────────────────────────────────── */
async function checkOllamaHealth() {
  const badge = $("#ollama-status");
  try {
    const r = await fetch("/api/health");
    const d = await r.json();
    if (d.ollama) {
      badge.textContent = "Ollama Connected";
      badge.className = "status-badge online";
    } else {
      badge.textContent = "Ollama Offline";
      badge.className = "status-badge offline";
    }
  } catch {
    badge.textContent = "Server Error";
    badge.className = "status-badge offline";
  }
}

/* ── Models ────────────────────────────────────────────── */
async function loadModels() {
  const sel = $("#model-select");
  try {
    const r = await fetch("/api/models");
    const d = await r.json();
    sel.innerHTML = "";
    if (d.models.length === 0) {
      sel.innerHTML = `<option>No models found</option>`;
      return;
    }
    for (const m of d.models) {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = m.name;
      if (m.name === d.default) opt.selected = true;
      sel.appendChild(opt);
    }
  } catch {
    sel.innerHTML = `<option>Error loading models</option>`;
  }
}

/* ── Conversations ─────────────────────────────────────── */
async function loadConversations() {
  const r = await fetch("/api/conversations");
  const convs = await r.json();
  renderConversationList(convs);
}

function renderConversationList(convs) {
  const list = $("#conversation-list");
  list.innerHTML = "";
  for (const c of convs) {
    const div = document.createElement("div");
    div.className = `conv-item${state.currentConv === c.id ? " active" : ""}`;
    div.innerHTML = `
      <span class="conv-title">${escapeHtml(c.title)}</span>
      <button class="conv-delete" title="Delete">&times;</button>
    `;
    div.querySelector(".conv-title").onclick = () => selectConversation(c.id);
    div.querySelector(".conv-delete").onclick = (e) => { e.stopPropagation(); deleteConversation(c.id); };
    list.appendChild(div);
  }
}

async function selectConversation(cid) {
  state.currentConv = cid;
  disconnectWs();
  connectWs(cid);
  const r = await fetch(`/api/conversations/${cid}`);
  const conv = await r.json();
  renderMessages(conv.messages || []);
  loadConversations();
}

async function newConversation() {
  const r = await fetch("/api/conversations", { method: "POST" });
  const d = await r.json();
  await selectConversation(d.id);
}

async function deleteConversation(cid) {
  await fetch(`/api/conversations/${cid}`, { method: "DELETE" });
  if (state.currentConv === cid) {
    state.currentConv = null;
    disconnectWs();
    renderMessages([]);
    $("#welcome").classList.remove("hidden");
  }
  loadConversations();
}

/* ── WebSocket ─────────────────────────────────────────── */
function connectWs(cid) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${proto}://${location.host}/ws/chat/${cid}`);
  state.ws.onclose = () => { state.ws = null; };
}

function disconnectWs() {
  if (state.ws) { state.ws.close(); state.ws = null; }
}

/* ── Send Message ──────────────────────────────────────── */
async function sendMessage() {
  const input = $("#user-input");
  const content = input.value.trim();
  if (!content && state.attachedFiles.length === 0) return;
  if (state.streaming) return;

  if (!state.currentConv) {
    await newConversation();
  }

  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    connectWs(state.currentConv);
    await new Promise((resolve) => {
      state.ws.onopen = resolve;
      setTimeout(resolve, 2000);
    });
  }

  const files = [...state.attachedFiles];
  appendUserMessage(content, files);

  input.value = "";
  input.style.height = "auto";
  state.attachedFiles = [];
  renderFileTags();

  const payload = { content, model: $("#model-select").value, files };
  state.ws.send(JSON.stringify(payload));

  state.streaming = true;
  $("#send-btn").classList.add("hidden");
  $("#stop-btn").classList.remove("hidden");

  let aiDiv = null;
  let aiContent = "";

  state.ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === "start") {
      aiDiv = appendAiMessageStart();
    } else if (msg.type === "token") {
      aiContent += msg.content;
      updateAiMessage(aiDiv, aiContent);
    } else if (msg.type === "done") {
      finalizeAiMessage(aiDiv, aiContent);
      stopStreaming();
      loadConversations();
    } else if (msg.type === "error") {
      if (aiDiv) {
        finalizeAiMessage(aiDiv, aiContent || msg.content);
      } else {
        appendErrorMessage(msg.content);
      }
      stopStreaming();
    }
  };
}

function stopStreaming() {
  state.streaming = false;
  $("#send-btn").classList.remove("hidden");
  $("#stop-btn").classList.add("hidden");
}

function stopGenerating() {
  if (state.ws) {
    state.ws.close();
    state.ws = null;
    connectWs(state.currentConv);
  }
  stopStreaming();
}

/* ── Render Messages ───────────────────────────────────── */
function renderMessages(messages) {
  const container = $("#messages");
  container.innerHTML = "";
  if (messages.length === 0) {
    container.innerHTML = `<div id="welcome" class="welcome"><h2>AutoDev Chat</h2><p>Local AI assistant powered by Ollama. Start typing below.</p></div>`;
    return;
  }
  for (const m of messages) {
    if (m.role === "user") {
      appendUserMessage(m.content, [], false);
    } else if (m.role === "assistant") {
      const div = appendAiMessageStart();
      finalizeAiMessage(div, m.content);
    }
  }
}

function appendUserMessage(content, files = [], scroll = true) {
  const welcome = $("#welcome");
  if (welcome) welcome.remove();

  const container = $("#messages");
  const div = document.createElement("div");
  div.className = "msg user";

  let fileTags = "";
  if (files.length > 0) {
    fileTags = `<div class="msg-files">${files.map(f => `<span class="msg-file-tag">${escapeHtml(f.name)}</span>`).join("")}</div>`;
  }

  const displayContent = extractUserDisplay(content);

  div.innerHTML = `
    <div class="msg-body">
      ${fileTags}
      <div class="msg-content">${escapeHtml(displayContent)}</div>
      <div class="msg-actions">
        <button class="msg-action-btn copy-btn" title="Copy">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
          Copy
        </button>
      </div>
    </div>
    <div class="msg-avatar">U</div>
  `;
  div.querySelector(".copy-btn").onclick = () => copyText(displayContent);
  container.appendChild(div);
  if (scroll) scrollToBottom();
}

function extractUserDisplay(content) {
  return content.replace(/\[File: [^\]]+\]\n```\n[\s\S]*?\n```\n*/g, "").trim();
}

function appendAiMessageStart() {
  const welcome = $("#welcome");
  if (welcome) welcome.remove();

  const container = $("#messages");
  const div = document.createElement("div");
  div.className = "msg assistant";
  div.innerHTML = `
    <div class="msg-avatar">A</div>
    <div class="msg-body">
      <div class="msg-content"><div class="typing-indicator"><span></span><span></span><span></span></div></div>
    </div>
  `;
  container.appendChild(div);
  scrollToBottom();
  return div;
}

function updateAiMessage(div, content) {
  if (!div) return;
  const el = div.querySelector(".msg-content");
  el.innerHTML = renderMarkdown(content);
  scrollToBottom();
}

function finalizeAiMessage(div, content) {
  if (!div) return;
  const el = div.querySelector(".msg-content");
  el.innerHTML = renderMarkdown(content);

  el.querySelectorAll("pre code").forEach((block) => {
    hljs.highlightElement(block);
  });

  const actionsDiv = document.createElement("div");
  actionsDiv.className = "msg-actions";
  actionsDiv.innerHTML = `
    <button class="msg-action-btn copy-btn" title="Copy">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
      Copy
    </button>
  `;
  actionsDiv.querySelector(".copy-btn").onclick = () => copyText(content);
  div.querySelector(".msg-body").appendChild(actionsDiv);
  scrollToBottom();
}

function appendErrorMessage(content) {
  const container = $("#messages");
  const div = document.createElement("div");
  div.className = "msg-error";
  div.innerHTML = renderMarkdown(content);
  container.appendChild(div);
  scrollToBottom();
}

/* ── Markdown Rendering ────────────────────────────────── */
function renderMarkdown(text) {
  if (!text) return "";
  let html = text;

  // Code blocks with language
  html = html.replace(/```(\w+)?\n([\s\S]*?)```/g, (_, lang, code) => {
    const language = lang || "plaintext";
    const escaped = escapeHtml(code.trimEnd());
    return `<pre><div class="code-header"><span>${language}</span><button class="copy-code-btn" onclick="copyText(\`${escaped.replace(/`/g, "\\`").replace(/\$/g, "\\$")}\`)">Copy</button></div><code class="language-${language}">${escaped}</code></pre>`;
  });

  // Inline code
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");

  // Bold
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");

  // Italic
  html = html.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");

  // Headers
  html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");

  // Unordered lists
  html = html.replace(/^[*-] (.+)$/gm, "<li>$1</li>");
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, "<ul>$1</ul>");

  // Ordered lists
  html = html.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");

  // Links
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

  // Paragraphs
  html = html.replace(/\n\n+/g, "</p><p>");
  if (!html.startsWith("<")) html = "<p>" + html + "</p>";

  // Fix nested tags
  html = html.replace(/<p>(<h[1-3]>)/g, "$1");
  html = html.replace(/(<\/h[1-3]>)<\/p>/g, "$1");
  html = html.replace(/<p>(<pre>)/g, "$1");
  html = html.replace(/(<\/pre>)<\/p>/g, "$1");
  html = html.replace(/<p>(<ul>)/g, "$1");
  html = html.replace(/(<\/ul>)<\/p>/g, "$1");
  html = html.replace(/<p><\/p>/g, "");

  return html;
}

/* ── File Upload ───────────────────────────────────────── */
async function handleFileUpload(fileList) {
  if (fileList.length === 0) return;
  const formData = new FormData();
  for (const f of fileList) formData.append("files", f);

  try {
    const r = await fetch("/api/upload", { method: "POST", body: formData });
    const d = await r.json();
    for (const f of d.files) {
      if (f.error) {
        appendErrorMessage(`File ${f.name}: ${f.error}`);
      } else {
        state.attachedFiles.push({ name: f.name, content: f.content });
      }
    }
    renderFileTags();
  } catch (e) {
    appendErrorMessage(`Upload failed: ${e.message}`);
  }
}

function renderFileTags() {
  const container = $("#file-tags");
  container.innerHTML = "";
  for (let i = 0; i < state.attachedFiles.length; i++) {
    const f = state.attachedFiles[i];
    const tag = document.createElement("div");
    tag.className = "file-tag";
    tag.innerHTML = `<span>${escapeHtml(f.name)}</span><button title="Remove">&times;</button>`;
    const idx = i;
    tag.querySelector("button").onclick = () => {
      state.attachedFiles.splice(idx, 1);
      renderFileTags();
    };
    container.appendChild(tag);
  }
}

/* ── Utilities ─────────────────────────────────────────── */
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => {
    // Brief visual feedback could go here
  }).catch(() => {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  });
}

function scrollToBottom() {
  const container = $("#messages");
  container.scrollTop = container.scrollHeight;
}

/* ── Event Listeners ───────────────────────────────────── */
function setupEventListeners() {
  // New chat
  $("#new-chat-btn").onclick = newConversation;

  // Sidebar toggle
  $("#sidebar-toggle").onclick = () => {
    $("#sidebar").classList.toggle("collapsed");
  };

  // Send
  $("#send-btn").onclick = sendMessage;
  $("#stop-btn").onclick = stopGenerating;

  // Enter to send, Shift+Enter for newline
  const input = $("#user-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Auto-resize textarea
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  });

  // File upload
  $("#file-input").addEventListener("change", (e) => {
    handleFileUpload(e.target.files);
    e.target.value = "";
  });

  // Drag and drop on input area
  const inputBar = $(".input-row");
  inputBar.addEventListener("dragover", (e) => { e.preventDefault(); inputBar.style.borderColor = "var(--accent)"; });
  inputBar.addEventListener("dragleave", () => { inputBar.style.borderColor = ""; });
  inputBar.addEventListener("drop", (e) => {
    e.preventDefault();
    inputBar.style.borderColor = "";
    handleFileUpload(e.dataTransfer.files);
  });
}
