/* ── AutoDev Chat — Frontend ──────────────────────────────── */

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  currentConv: null,
  ws: null,
  streaming: false,
  attachedFiles: [],
  mode: "agent",
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

/* ── Mode Toggle ──────────────────────────────────────── */
function setMode(mode) {
  state.mode = mode;
  $("#mode-agent").classList.toggle("active", mode === "agent");
  $("#mode-chat").classList.toggle("active", mode === "chat");
  const input = $("#user-input");
  input.placeholder = mode === "agent"
    ? "Describe what you want to build..."
    : "Message AutoDev...";
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
  const ws = new WebSocket(`${proto}://${location.host}/ws/chat/${cid}`);
  state.ws = ws;
  ws.onclose = () => {
    if (state.ws === ws) state.ws = null;
  };
  ws.onerror = (e) => {
    console.error("WebSocket error:", e);
    if (state.ws === ws) {
      appendErrorMessage("WebSocket connection error. Is the server running?");
    }
  };
}

function disconnectWs() {
  if (state.ws) { state.ws.close(); state.ws = null; }
}

function waitForWsReady() {
  return new Promise((resolve, reject) => {
    const ws = state.ws;
    if (!ws) { reject(new Error("No WebSocket")); return; }
    if (ws.readyState === WebSocket.OPEN) { resolve(); return; }

    const onOpen = () => { cleanup(); resolve(); };
    const onError = (e) => { cleanup(); reject(e); };
    const onClose = () => { cleanup(); reject(new Error("WebSocket closed")); };
    const timer = setTimeout(() => { cleanup(); reject(new Error("WebSocket connect timeout")); }, 5000);

    function cleanup() {
      clearTimeout(timer);
      ws.removeEventListener("open", onOpen);
      ws.removeEventListener("error", onError);
      ws.removeEventListener("close", onClose);
    }

    ws.addEventListener("open", onOpen);
    ws.addEventListener("error", onError);
    ws.addEventListener("close", onClose);
  });
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
    try {
      await waitForWsReady();
    } catch (e) {
      appendErrorMessage(`Cannot connect to server: ${e.message}`);
      return;
    }
  }

  // Set up message handler BEFORE sending so no responses are missed
  if (state.mode === "agent") {
    setupAgentMessageHandler();
  } else {
    setupChatMessageHandler();
  }

  const files = [...state.attachedFiles];
  appendUserMessage(content, files);

  input.value = "";
  input.style.height = "auto";
  state.attachedFiles = [];
  renderFileTags();

  const payload = {
    content,
    model: $("#model-select").value,
    files,
    mode: state.mode,
  };

  state.streaming = true;
  $("#send-btn").classList.add("hidden");
  $("#stop-btn").classList.remove("hidden");

  if (state.mode === "agent") {
    appendProcessingIndicator();
  }

  state.ws.send(JSON.stringify(payload));
}

/* ── Chat Mode Message Handler ────────────────────────── */
function setupChatMessageHandler() {
  let aiDiv = null;
  let aiContent = "";

  state.ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === "connected") {
      return;
    } else if (msg.type === "start") {
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

/* ── Agent Mode Message Handler ───────────────────────── */
function setupAgentMessageHandler() {
  let pipelineDiv = null;

  state.ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      console.error("Failed to parse WS message:", event.data);
      return;
    }
    console.log("[WS-RECV]", msg.type, msg);

    if (msg.type === "connected") {
      return;

    } else if (msg.type === "pipeline_start") {
      pipelineDiv = appendPipelineStart(msg.model);

    } else if (msg.type === "agent_update") {
      if (!pipelineDiv) pipelineDiv = appendPipelineStart(null);
      appendAgentCard(pipelineDiv, msg.agent, msg.status, msg.content);

    } else if (msg.type === "plan_approval") {
      if (!pipelineDiv) pipelineDiv = appendPipelineStart(null);
      appendPlanApproval(pipelineDiv, msg.plan);

    } else if (msg.type === "done") {
      if (!pipelineDiv) pipelineDiv = appendPipelineStart(null);
      appendPipelineResult(pipelineDiv, msg);
      stopStreaming();
      loadConversations();

    } else if (msg.type === "phase") {
      if (!pipelineDiv) pipelineDiv = appendPipelineStart(null);
      appendPhaseCard(pipelineDiv, msg);

    } else if (msg.type === "pipeline_done") {
      if (!pipelineDiv) pipelineDiv = appendPipelineStart(null);
      appendPipelineResult(pipelineDiv, msg);
      stopStreaming();
      loadConversations();

    } else if (msg.type === "error") {
      appendErrorMessage(msg.content);
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
  }
  stopStreaming();
}

/* ── Pipeline Rendering ───────────────────────────────── */
function appendProcessingIndicator() {
  const welcome = $("#welcome");
  if (welcome) welcome.remove();

  const existing = $("#processing-indicator");
  if (existing) return;

  const container = $("#messages");
  const div = document.createElement("div");
  div.id = "processing-indicator";
  div.className = "msg assistant";
  div.innerHTML = `
    <div class="msg-avatar">A</div>
    <div class="msg-body">
      <div class="msg-content"><div class="typing-indicator"><span></span><span></span><span></span></div> Initializing agent pipeline...</div>
    </div>
  `;
  container.appendChild(div);
  scrollToBottom();
}

function appendPipelineStart(model) {
  const indicator = $("#processing-indicator");
  if (indicator) indicator.remove();

  const welcome = $("#welcome");
  if (welcome) welcome.remove();

  const container = $("#messages");
  const div = document.createElement("div");
  div.className = "msg assistant pipeline-msg";
  const modelTag = model ? ` <span class="pipeline-model">${escapeHtml(model)}</span>` : "";
  div.innerHTML = `
    <div class="msg-avatar">A</div>
    <div class="msg-body">
      <div class="pipeline-container">
        <div class="pipeline-header">Agent Pipeline${modelTag}</div>
        <div class="pipeline-phases"></div>
      </div>
    </div>
  `;
  container.appendChild(div);
  scrollToBottom();
  return div;
}

const AGENT_INFO = {
  product_manager: { icon: "\u{1F4CB}", label: "Product Mgr", color: "purple" },
  architect:     { icon: "\u{1F9E0}", label: "Architect",  color: "cyan" },
  approval_gate: { icon: "⻾️", label: "Approval",   color: "yellow" },
  developer:     { icon: "\u{1F4BB}", label: "Developer",  color: "green" },
  tester:        { icon: "\u{1F9EA}", label: "Tester",     color: "magenta" },
  debugger:      { icon: "\u{1F41B}", label: "Debugger",   color: "red" },
  reviewer:      { icon: "\u{1F50D}", label: "Reviewer",   color: "blue" },
  judge:         { icon: "⚖️", label: "Judge",      color: "gold" },
  prepare_retry: { icon: "\u{1F504}", label: "Retry",      color: "orange" },
  done:          { icon: "✅",    label: "Done",        color: "green" },
  failed:        { icon: "❌",    label: "Failed",      color: "red" },
};

function appendAgentCard(pipelineDiv, agent, status, content) {
  if (!pipelineDiv) return;
  const phases = pipelineDiv.querySelector(".pipeline-phases");
  if (!phases) return;

  const info = AGENT_INFO[agent] || { icon: "•", label: agent, color: "gray" };

  const card = document.createElement("div");
  card.className = `phase-card phase-${info.color}`;
  card.dataset.agent = agent;

  let statusText = status === "done" ? "completed" : status;
  let detailHtml = "";

  if (agent === "product_manager" && content) {
    statusText = "spec ready";
    let specHtml = "";
    if (content.scope) specHtml += `<div class="plan-field"><strong>Scope:</strong> ${escapeHtml(content.scope)}</div>`;
    if (content.milestones && content.milestones.length > 0) {
      specHtml += `<div class="plan-field"><strong>Milestones:</strong><ol class="plan-tasks">${content.milestones.map(m => `<li>${escapeHtml(m)}</li>`).join("")}</ol></div>`;
    }
    detailHtml = specHtml;
  } else if (agent === "architect" && content) {
    statusText = "plan ready";
    detailHtml = renderPlanSummary(content);
  } else if (agent === "developer" && content) {
    const fileList = Array.isArray(content) ? content : (content.files || []);
    const modified = content.modified || [];
    statusText = `wrote ${fileList.length} file(s)`;
    if (modified.length > 0 && modified.length < fileList.length) {
      statusText = `modified ${modified.length}/${fileList.length} file(s)`;
    }
    detailHtml = `<div class="phase-files">${fileList.map(f => `<span class="phase-file-tag">${escapeHtml(typeof f === 'string' ? f : f.path || '?')}</span>`).join("")}</div>`;
  } else if (agent === "tester" && content) {
    statusText = content.passed ? "PASSED" : "FAILED";
    if (content.stderr && !content.passed) {
      detailHtml = `<pre class="phase-stderr">${escapeHtml(String(content.stderr).substring(0, 500))}</pre>`;
    }
  } else if (agent === "debugger" && content) {
    statusText = content.error_category || "analyzed";
    if (content.root_cause) {
      detailHtml = `<div class="phase-summary"><strong>Root cause:</strong> ${escapeHtml(content.root_cause.substring(0, 300))}</div>`;
      if (content.affected_files && content.affected_files.length > 0) {
        detailHtml += `<div class="phase-files">${content.affected_files.map(f => `<span class="phase-file-tag">${escapeHtml(f)}</span>`).join("")}</div>`;
      }
    }
  } else if (agent === "reviewer" && content) {
    statusText = content.approved ? "APPROVED" : "CHANGES REQUESTED";
    if (content.summary) {
      detailHtml = `<div class="phase-summary">${escapeHtml(content.summary)}</div>`;
    }
    if (content.comments && content.comments.length > 0) {
      detailHtml += `<div class="phase-comments">${content.comments.map(c =>
        `<div class="phase-comment"><span class="comment-sev comment-sev-${c.severity || 'info'}">${c.severity || 'info'}</span> <strong>${escapeHtml(c.file_path || '')}</strong>: ${escapeHtml(c.message || '')}</div>`
      ).join("")}</div>`;
    }
  } else if (agent === "judge" && content) {
    const decisionIcons = { "ACCEPT": "✅", "REJECT": "🔄", "ROLLBACK": "⏪", "ESCALATE": "🚨" };
    const dec = (content.decision || "").toUpperCase();
    statusText = `${decisionIcons[dec] || ""} ${dec}`;
    if (content.reason) {
      detailHtml = `<div class="phase-summary">${escapeHtml(content.reason)}</div>`;
    }
    if (content.strategy) {
      detailHtml += `<div class="phase-summary"><strong>Strategy:</strong> ${escapeHtml(content.strategy)}</div>`;
    }
  } else if (agent === "prepare_retry") {
    statusText = `iteration ${content.iteration || "?"}`;
  } else if (agent === "done") {
    statusText = "complete";
  } else if (agent === "failed") {
    statusText = content.stop_reason || "stopped";
  }

  card.innerHTML = `
    <div class="phase-header">
      <span class="phase-icon">${info.icon}</span>
      <span class="phase-label">${escapeHtml(info.label)}</span>
      <span class="phase-status phase-status-${info.color}">${statusText}</span>
    </div>
    ${detailHtml ? `<div class="phase-detail">${detailHtml}</div>` : ""}
  `;

  phases.appendChild(card);
  scrollToBottom();
}

function appendPhaseCard(pipelineDiv, msg) {
  if (!pipelineDiv) return;
  const phases = pipelineDiv.querySelector(".pipeline-phases");
  if (!phases) return;

  const existing = phases.querySelector(`.phase-card[data-node="${msg.node}"]`);
  if (existing && msg.node !== "prepare_retry") {
    updatePhaseCard(existing, msg);
    return;
  }

  const card = document.createElement("div");
  card.className = `phase-card phase-${msg.color}`;
  card.dataset.node = msg.node;

  let statusText = "";
  let detailHtml = "";

  if (msg.node === "architect") {
    statusText = "planning...";
    if (msg.detail && msg.detail.plan) {
      statusText = "plan ready";
      detailHtml = renderPlanSummary(msg.detail.plan);
    }
  } else if (msg.node === "developer") {
    statusText = "writing code...";
    if (msg.detail && msg.detail.files) {
      statusText = `wrote ${msg.detail.files.length} file(s)`;
      detailHtml = `<div class="phase-files">${msg.detail.files.map(f => `<span class="phase-file-tag">${escapeHtml(f)}</span>`).join("")}</div>`;
    }
  } else if (msg.node === "tester") {
    statusText = "running tests...";
    if (msg.detail && msg.detail.passed !== undefined) {
      statusText = msg.detail.passed ? "PASSED" : "FAILED";
      if (msg.detail.stderr && !msg.detail.passed) {
        detailHtml = `<pre class="phase-stderr">${escapeHtml(msg.detail.stderr.substring(0, 500))}</pre>`;
      }
    }
  } else if (msg.node === "reviewer") {
    statusText = "reviewing...";
    if (msg.detail && msg.detail.approved !== undefined) {
      statusText = msg.detail.approved ? "APPROVED" : "CHANGES REQUESTED";
      if (msg.detail.summary) {
        detailHtml = `<div class="phase-summary">${escapeHtml(msg.detail.summary)}</div>`;
      }
      if (msg.detail.comments && msg.detail.comments.length > 0) {
        detailHtml += `<div class="phase-comments">${msg.detail.comments.map(c =>
          `<div class="phase-comment"><span class="comment-sev comment-sev-${c.severity || 'info'}">${c.severity || 'info'}</span> <strong>${escapeHtml(c.file_path || '')}</strong>: ${escapeHtml(c.message || '')}</div>`
        ).join("")}</div>`;
      }
    }
  } else if (msg.node === "prepare_retry") {
    const iter = msg.detail ? msg.detail.iteration : msg.iteration;
    statusText = `iteration ${iter}`;
  } else if (msg.node === "done") {
    statusText = "complete";
  } else if (msg.node === "failed") {
    statusText = msg.detail && msg.detail.reason ? msg.detail.reason : "stopped";
  }

  const iterTag = (msg.iteration > 0 && msg.node !== "prepare_retry") ? ` <span class="phase-iter">(iter ${msg.iteration})</span>` : "";

  card.innerHTML = `
    <div class="phase-header">
      <span class="phase-icon">${msg.icon}</span>
      <span class="phase-label">${escapeHtml(msg.label)}</span>${iterTag}
      <span class="phase-status phase-status-${msg.color}">${statusText}</span>
    </div>
    ${detailHtml ? `<div class="phase-detail">${detailHtml}</div>` : ""}
  `;

  phases.appendChild(card);
  scrollToBottom();
}

function updatePhaseCard(card, msg) {
  const statusEl = card.querySelector(".phase-status");
  if (!statusEl) return;

  if (msg.node === "tester" && msg.detail) {
    statusEl.textContent = msg.detail.passed ? "PASSED" : "FAILED";
    if (msg.detail.stderr && !msg.detail.passed) {
      let detailEl = card.querySelector(".phase-detail");
      if (!detailEl) {
        detailEl = document.createElement("div");
        detailEl.className = "phase-detail";
        card.appendChild(detailEl);
      }
      detailEl.innerHTML = `<pre class="phase-stderr">${escapeHtml(msg.detail.stderr.substring(0, 500))}</pre>`;
    }
  } else if (msg.node === "reviewer" && msg.detail) {
    statusEl.textContent = msg.detail.approved ? "APPROVED" : "CHANGES REQUESTED";
  }
  scrollToBottom();
}

function renderPlanSummary(plan) {
  if (!plan) return "";
  let html = "";
  if (plan.problem_description) {
    html += `<div class="plan-field"><strong>Problem:</strong> ${escapeHtml(plan.problem_description)}</div>`;
  }
  if (plan.files_needed && plan.files_needed.length > 0) {
    html += `<div class="plan-field"><strong>Files:</strong> ${plan.files_needed.map(f => escapeHtml(f)).join(", ")}</div>`;
  }
  if (plan.tasks && plan.tasks.length > 0) {
    html += `<div class="plan-field"><strong>Tasks:</strong><ol class="plan-tasks">`;
    for (const t of plan.tasks) {
      html += `<li>${escapeHtml(t.description || "")} <span class="plan-task-file">${escapeHtml(t.file_path || "")}</span></li>`;
    }
    html += `</ol></div>`;
  }
  if (plan.acceptance_criteria && plan.acceptance_criteria.length > 0) {
    html += `<div class="plan-field"><strong>Acceptance Criteria:</strong><ul>`;
    for (const c of plan.acceptance_criteria) {
      html += `<li>${escapeHtml(c)}</li>`;
    }
    html += `</ul></div>`;
  }
  return html;
}

function appendPlanApproval(pipelineDiv, plan) {
  if (!pipelineDiv) return;
  const phases = pipelineDiv.querySelector(".pipeline-phases");
  if (!phases) return;

  const card = document.createElement("div");
  card.className = "phase-card phase-yellow plan-approval-card";
  card.innerHTML = `
    <div class="phase-header">
      <span class="phase-icon">&#9208;&#65039;</span>
      <span class="phase-label">Plan Approval Required</span>
    </div>
    <div class="phase-detail">
      ${renderPlanSummary(plan)}
      <div class="approval-buttons">
        <button class="approve-btn" onclick="approvePlan(true, this)">&#10004; Approve</button>
        <button class="reject-btn" onclick="approvePlan(false, this)">&#10008; Reject</button>
      </div>
    </div>
  `;
  phases.appendChild(card);
  scrollToBottom();
}

window.approvePlan = function (approved, btnEl) {
  const card = btnEl.closest(".plan-approval-card");
  const buttons = card.querySelector(".approval-buttons");
  buttons.innerHTML = approved
    ? `<div class="approval-result approved">Plan Approved</div>`
    : `<div class="approval-result rejected">Plan Rejected</div>`;

  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "plan_decision", approved }));
  }
};

function appendPipelineResult(pipelineDiv, msg) {
  if (!pipelineDiv) return;
  const phases = pipelineDiv.querySelector(".pipeline-phases");
  if (!phases) return;

  const card = document.createElement("div");
  const isSuccess = msg.status === "success";
  card.className = `phase-card result-card ${isSuccess ? "result-success" : "result-failure"}`;

  let filesHtml = "";
  if (msg.files && msg.files.length > 0) {
    filesHtml = `<div class="result-files">
      <div class="result-files-header">Files created (${msg.files.length}):</div>
      ${msg.files.map((f, i) => `
        <div class="result-file">
          <div class="result-file-header" onclick="toggleFileContent(this)">
            <span class="result-file-name">${escapeHtml(f.path)}</span>
            <span class="result-file-size">${f.size} bytes</span>
            <span class="result-file-toggle">&#9660;</span>
          </div>
          ${f.content ? `<pre class="result-file-content hidden"><code class="language-python">${escapeHtml(f.content)}</code></pre>` : ""}
        </div>
      `).join("")}
    </div>`;
  }

  let summaryHtml = "";
  if (msg.content && typeof msg.content === "string") {
    summaryHtml = `<div class="result-summary">${escapeHtml(msg.content)}</div>`;
  }

  card.innerHTML = `
    <div class="phase-header">
      <span class="phase-icon">${isSuccess ? "&#10004;" : "&#10008;"}</span>
      <span class="phase-label">${isSuccess ? "Pipeline Complete" : "Pipeline Stopped"}</span>
      ${msg.iterations > 0 ? `<span class="phase-iter">(${msg.iterations} iteration(s))</span>` : ""}
    </div>
    <div class="phase-detail">
      ${summaryHtml}
      ${!isSuccess && msg.stop_reason ? `<div class="result-reason">${escapeHtml(msg.stop_reason)}</div>` : ""}
      ${filesHtml}
    </div>
  `;

  phases.appendChild(card);

  card.querySelectorAll("pre code").forEach((block) => {
    hljs.highlightElement(block);
  });

  scrollToBottom();
}

window.toggleFileContent = function (headerEl) {
  const pre = headerEl.nextElementSibling;
  if (!pre) return;
  pre.classList.toggle("hidden");
  const toggle = headerEl.querySelector(".result-file-toggle");
  if (toggle) {
    toggle.textContent = pre.classList.contains("hidden") ? "▼" : "▲";
  }
  scrollToBottom();
};

/* ── Render Messages ───────────────────────────────────── */
function renderMessages(messages) {
  const container = $("#messages");
  container.innerHTML = "";
  if (messages.length === 0) {
    container.innerHTML = `<div id="welcome" class="welcome">
      <h2>AutoDev Chat</h2>
      <p>Local AI dev team powered by Ollama. Describe what you want to build.</p>
      <div class="welcome-modes">
        <div class="welcome-mode"><strong>Agent Mode</strong> &mdash; 7-agent pipeline (PM &rarr; Architect &rarr; Developer &rarr; Tester &rarr; Debugger &rarr; Reviewer &rarr; Judge)</div>
        <div class="welcome-mode"><strong>Chat Mode</strong> &mdash; Direct conversation with Ollama</div>
      </div>
    </div>`;
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
  const indicator = $("#processing-indicator");
  if (indicator) indicator.remove();

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

  html = html.replace(/```(\w+)?\n([\s\S]*?)```/g, (_, lang, code) => {
    const language = lang || "plaintext";
    const escaped = escapeHtml(code.trimEnd());
    return `<pre><div class="code-header"><span>${language}</span><button class="copy-code-btn" onclick="copyText(\`${escaped.replace(/`/g, "\\`").replace(/\$/g, "\\$")}\`)">Copy</button></div><code class="language-${language}">${escaped}</code></pre>`;
  });

  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");
  html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  html = html.replace(/^[*-] (.+)$/gm, "<li>$1</li>");
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, "<ul>$1</ul>");
  html = html.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  html = html.replace(/\n\n+/g, "</p><p>");
  if (!html.startsWith("<")) html = "<p>" + html + "</p>";

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
  navigator.clipboard.writeText(text).then(() => {}).catch(() => {
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
  $("#new-chat-btn").onclick = newConversation;

  $("#sidebar-toggle").onclick = () => {
    $("#sidebar").classList.toggle("collapsed");
  };

  // Mode toggle
  $("#mode-agent").onclick = () => setMode("agent");
  $("#mode-chat").onclick = () => setMode("chat");

  // Send
  $("#send-btn").onclick = sendMessage;
  $("#stop-btn").onclick = stopGenerating;

  const input = $("#user-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  });

  $("#file-input").addEventListener("change", (e) => {
    handleFileUpload(e.target.files);
    e.target.value = "";
  });

  const inputBar = $(".input-row");
  inputBar.addEventListener("dragover", (e) => { e.preventDefault(); inputBar.style.borderColor = "var(--accent)"; });
  inputBar.addEventListener("dragleave", () => { inputBar.style.borderColor = ""; });
  inputBar.addEventListener("drop", (e) => {
    e.preventDefault();
    inputBar.style.borderColor = "";
    handleFileUpload(e.dataTransfer.files);
  });
}
