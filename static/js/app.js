const API = window.location.origin;
const POLL_INTERVAL = 2000;
const SESSION_ID_SHORT_LEN = 8;
const TERMINAL_STATUSES = ['cancelled', 'failed'];
const ACTIVE_STATUSES = ['pending', 'planning', 'developing', 'committing'];

let sessions = [];

function formatTime(isoStr) {
  if (!isoStr) return '-';
  const d = new Date(isoStr);
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const hour = String(d.getHours()).padStart(2, '0');
  const minute = String(d.getMinutes()).padStart(2, '0');
  return `${month}-${day} ${hour}:${minute}`;
}

function formatElapsed(s) {
  if (s == null) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s/60)}m${s%60}s`;
  return `${Math.floor(s/3600)}h${Math.floor((s%3600)/60)}m`;
}

// Local elapsed timers for active tasks
const activeTimers = {}; // session_id -> {serverStart, fetchedAt, base}
function getElapsed(s) {
  const active = ACTIVE_STATUSES.includes(s.status);
  if (!active) return s.elapsed_seconds;
  if (!s.started_at) return s.elapsed_seconds;
  const serverStart = new Date(s.started_at).getTime();
  const base = s.elapsed_seconds || 0;
  const existing = activeTimers[s.session_id];
  if (!existing || existing.serverStart !== serverStart) {
    activeTimers[s.session_id] = { serverStart, fetchedAt: Date.now(), base };
  }
  const t = activeTimers[s.session_id];
  return t.base + Math.floor((Date.now() - t.fetchedAt) / 1000);
}

let selectedId = null;
let activeFilter = null;
let streamSource = null;
let streamLogs = {};
let autoScroll = true;
let lastRenderedId = null;
let elapsedTimer = null;

const STATUS_LABEL = {
  pending: "Pending",
  planning: "Planning",
  developing: "Developing",
  committing: "Committing",
  review: "Review",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};


async function fetchSessions() {
  try {
    const [sessionsRes, statsRes] = await Promise.all([
      fetch(`${API}/sessions`),
      fetch(`${API}/stats`),
    ]);
    sessions = await sessionsRes.json();
    const stats = await statsRes.json();
    updateStats(stats);
    renderList();
    if (selectedId) renderDetail(selectedId);
  } catch (e) {
    document.getElementById("refresh-hint").textContent = "Connection lost";
  }
}

function updateStats(stats) {
  const s = stats.by_status || {};
  document.getElementById("cnt-planning").textContent = s.planning || 0;
  document.getElementById("cnt-developing").textContent = s.developing || 0;
  document.getElementById("cnt-committing").textContent = s.committing || 0;
  document.getElementById("cnt-pending").textContent = s.pending || 0;
  document.getElementById("cnt-review").textContent = s.review || 0;
  document.getElementById("cnt-completed").textContent = s.completed || 0;
  document.getElementById("cnt-failed").textContent = s.failed || 0;
  document.getElementById("cnt-cancelled").textContent = s.cancelled || 0;
}

function toggleFilter(status) {
  activeFilter = activeFilter === status ? null : status;
  document.querySelectorAll('.stat').forEach(el => el.classList.remove('active-filter'));
  if (activeFilter) {
    const el = document.getElementById(`filter-${activeFilter}`);
    if (el) el.classList.add('active-filter');
  }
  renderList();
}

// ---- Render list ----

function renderList() {
  const el = document.getElementById("sessions-list");
  const filtered = activeFilter
    ? sessions.filter(s => s.status === activeFilter)
    : sessions.filter(s => !TERMINAL_STATUSES.includes(s.status));
  if (!filtered.length) {
    el.innerHTML = `<div style="padding:32px;text-align:center;color:#475569;font-size:13px;">${activeFilter ? 'No tasks with this status' : 'No tasks yet'}</div>`;
    return;
  }

  const sorted = [...filtered].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

  el.innerHTML = sorted.map(s => `
    <div class="session-item ${s.session_id === selectedId ? 'active' : ''}" onclick="selectSession('${s.session_id}')">
      <div class="session-row1">
        <span class="session-id">${s.session_id.slice(0, SESSION_ID_SHORT_LEN)}</span>
        <span class="status-badge ${s.status}">${STATUS_LABEL[s.status] || s.status}</span>
        ${s.status === 'review' && s.mr_url ? `<a href="${s.mr_url}" target="_blank" onclick="event.stopPropagation()" class="mr-link"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>View MR${s.mr_number ? ' !' + s.mr_number : ''}</a>` : ''}
        ${s.status === 'review' && s.mr_url ? `<span class="approval-badge ${s.mr_approved ? 'approved' : 'pending'}">${s.mr_approved ? '✓' : '⏳'}</span>` : ''}
      </div>
      <div class="session-prompt">${escHtml(s.prompt)}</div>
      <div class="session-time">${formatTime(s.created_at)}${!ACTIVE_STATUSES.includes(s.status) && s.elapsed_seconds != null ? ` · ${formatElapsed(s.elapsed_seconds)}` : ''}${s.total_cost_usd > 0 ? ` · $${s.total_cost_usd.toFixed(4)}` : ''}</div>
    </div>
  `).join("");
}

// ---- Select session ----

function selectSession(id) {
  selectedId = id;
  renderList();
  renderDetail(id);
  const s = sessions.find(x => x.session_id === id);
  if (s && ["completed", "failed", "cancelled", "review"].includes(s.status)) {
    loadHistoryLogs(id);
  } else {
    startStream(id);
  }
  if (window.innerWidth <= 768) {
    document.querySelector('.detail-panel').classList.add('mobile-show');
  }
}

function closeMobileDetail() {
  document.querySelector('.detail-panel').classList.remove('mobile-show');
}

async function loadHistoryLogs(id) {
  if (streamLogs[id] && streamLogs[id].length > 0) return;
  try {
    const res = await fetch(`${API}/sessions/${id}/logs`);
    const logs = await res.json();
    streamLogs[id] = logs;
    const box = document.getElementById("logs-box");
    const countEl = document.getElementById("logs-count");
    if (box) {
      box.innerHTML = logs.length
        ? logs.map(l => {
            const formattedContent = formatLogContent(l.content);
            return `<div class="log-line"><div class="log-level ${l.level}">${l.level}</div><div class="log-content">${formattedContent}</div></div>`;
          }).join("")
        : `<span style="color:#475569">No logs</span>`;
      if (autoScroll) box.scrollTop = box.scrollHeight;
    }
    if (countEl) countEl.textContent = `Logs (${logs.length})`;
  } catch (e) {}
}

function renderDetail(id) {
  const s = sessions.find(x => x.session_id === id);
  if (!s) return;

  const isPlanning = s.status === "planning";
  const isDeveloping = ["developing", "committing"].includes(s.status);
  const isReview = s.status === "review";
  const isDone = TERMINAL_STATUSES.includes(s.status) || s.status === "completed";

  document.getElementById("btn-stop").disabled = !isDeveloping && s.status !== "pending" && !isReview && !isPlanning;
  document.getElementById("btn-complete").disabled = !isReview;
  document.getElementById("btn-delete").disabled = !isDone;

  if (isReview) {
    refreshHappyBtn(id);
  }

  // If already rendered this session, only update dynamic parts
  if (lastRenderedId === id) {
    if (isPlanning && document.getElementById("plan-messages")) {
      const statusEl = document.querySelector("#detail-content .status-badge");
      if (statusEl) {
        statusEl.className = `status-badge ${s.status}`;
        statusEl.textContent = STATUS_LABEL[s.status];
      }
      if (!planPollingInterval) {
        startPlanPolling(id);
      }
      return;
    }

    if (isPlanning && document.getElementById("logs-box")) {
      lastRenderedId = null;
    } else if (!isPlanning && document.getElementById("plan-messages")) {
      lastRenderedId = null;
    } else if (document.getElementById("logs-box")) {
      const statusEl = document.querySelector("#detail-content .status-badge");
      if (statusEl) {
        statusEl.className = `status-badge ${s.status}`;
        statusEl.textContent = STATUS_LABEL[s.status] || s.status;
      }
      const logs = streamLogs[id] || [];
      const countEl = document.getElementById("logs-count");
      if (countEl) countEl.textContent = `Logs (${logs.length})`;
      if (!elapsedTimer) {
        const elapsedEl = document.getElementById("elapsed-display");
        if (elapsedEl && s.elapsed_seconds != null) {
          elapsedEl.textContent = formatElapsed(s.elapsed_seconds);
        }
      }
      const mrLinkEl = document.getElementById("detail-mr-link");
      if (s.mr_url && !mrLinkEl) {
        const grid = document.querySelector("#detail-content .info-grid");
        if (grid) {
          const mrItem = document.createElement("div");
          mrItem.className = "info-item";
          mrItem.innerHTML = `<div class="info-label">MR</div><div class="info-value"><a id="detail-mr-link" href="${s.mr_url}" target="_blank" style="color:#c4b5fd;text-decoration:none;">🔗 View MR</a></div>`;
          grid.appendChild(mrItem);
        }
      }
      if (isReview && !document.getElementById("btn-happy")) {
        const grid = document.querySelector("#detail-content .info-grid");
        if (grid) {
          const happyItem = document.createElement("div");
          happyItem.className = "info-item";
          happyItem.id = "detail-links-item";
          happyItem.innerHTML = `<div class="info-label">Happy</div><div class="info-value" style="display:flex;align-items:center;gap:10px;"><button class="btn btn-happy" id="btn-happy" onclick="toggleHappy()">Start Happy</button><a id="detail-happy-link" href="" target="_blank" style="color:#4ade80;text-decoration:none;font-size:12px;display:none;">🟢 Open</a></div>`;
          grid.appendChild(happyItem);
          refreshHappyBtn(id);
        }
      }
      if (!ACTIVE_STATUSES.includes(s.status) && elapsedTimer) {
        clearInterval(elapsedTimer);
        elapsedTimer = null;
      }
      return;
    }
  }

  // Full render
  lastRenderedId = id;

  if (isPlanning) {
    if (planSessionStates.has(id)) {
      planSessionStates.delete(id);
    }

    document.getElementById("detail-content").innerHTML = `
      <div class="info-grid">
        <div class="info-item"><div class="info-label">Session ID</div><div class="info-value" style="font-family:monospace;">${s.session_id}</div></div>
        <div class="info-item"><div class="info-label">Status</div><div class="info-value"><span class="status-badge ${s.status}">${STATUS_LABEL[s.status]}</span></div></div>
      </div>
      <div class="prompt-box">${escHtml(s.prompt)}</div>
      <div style="margin-top: 16px; margin-bottom: 8px; font-size: 13px; color: #94a3b8; font-weight: 500;">Plan conversation</div>
      <div id="plan-messages" style="background: #0a0d14; border-radius: 8px; padding: 12px; max-height: 400px; overflow-y: auto; overflow-x: hidden; margin-bottom: 12px; word-wrap: break-word;"></div>
      <div class="plan-input-area" style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px;">
        <textarea id="plan-input" rows="2" placeholder="Message — Enter to send, Shift+Enter for new line..." autocomplete="off"></textarea>
        <div style="display: flex; flex-direction: column; gap: 8px;">
          <button class="btn-submit" onclick="sendPlanMessage()">Send</button>
          <button class="btn-submit" onclick="confirmPlan()" style="background: #059669;">Confirm plan & start developing</button>
        </div>
      </div>
    `;

    const planInput = document.getElementById('plan-input');
    if (planInput) {
      planInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          sendPlanMessage();
        }
      });
    }

    startPlanPolling(id);
    return;
  }

  const logs = streamLogs[id] || [];
  const logsHtml = logs.length
    ? logs.map(l => {
        const formattedContent = formatLogContent(l.content);
        return `<div class="log-line"><div class="log-level ${l.level}">${l.level}</div><div class="log-content">${formattedContent}</div></div>`;
      }).join("")
    : `<span style="color:#475569">No logs</span>`;

  document.getElementById("detail-content").innerHTML = `
    <div class="info-grid">
      <div class="info-item"><div class="info-label">Session ID</div><div class="info-value" style="font-family:monospace;cursor:pointer;user-select:all;" title="Click to copy" onclick="navigator.clipboard.writeText('${s.session_id}').then(()=>this.style.color='#4ade80').catch(()=>{})">${s.session_id}</div></div>
      <div class="info-item"><div class="info-label">Status</div><div class="info-value"><span class="status-badge ${s.status}">${STATUS_LABEL[s.status] || s.status}</span></div></div>
      <div class="info-item"><div class="info-label">Created</div><div class="info-value">${formatTime(s.created_at)}</div></div>
      <div class="info-item"><div class="info-label">Elapsed</div><div class="info-value" id="elapsed-display">${getElapsed(s) != null ? formatElapsed(getElapsed(s)) : '-'}</div></div>
      ${s.total_cost_usd > 0 ? `<div class="info-item"><div class="info-label">Cost</div><div class="info-value">$${s.total_cost_usd.toFixed(4)}</div></div>` : ''}
      <div class="info-item"><div class="info-label">Tokens</div><div class="info-value">${s.total_input_tokens > 0 || s.total_output_tokens > 0 ? `${s.total_input_tokens.toLocaleString()} in / ${s.total_output_tokens.toLocaleString()} out` : "-"}</div></div>
      ${s.branch_name ? `<div class="info-item"><div class="info-label">Branch</div><div class="info-value">${escHtml(s.branch_name)}</div></div>` : ''}
      ${s.mr_url ? `<div class="info-item"><div class="info-label">MR</div><div class="info-value"><a id="detail-mr-link" href="${s.mr_url}" target="_blank" style="color:#c4b5fd;text-decoration:none;">🔗 View MR</a></div></div>` : ''}
      ${s.status === 'review' ? `<div class="info-item" id="detail-links-item"><div class="info-label">Happy</div><div class="info-value" style="display:flex;align-items:center;gap:10px;"><button class="btn btn-happy" id="btn-happy" onclick="toggleHappy()">Start Happy</button>${s.happy_session_id ? `<a href="https://app.happy.engineering/session/${s.happy_session_id}" target="_blank" style="color:#4ade80;text-decoration:none;font-size:12px;">🔗 Web</a>` : ''}</div></div>` : ''}
    </div>
    ${s.worktree_path ? `<div class="info-item" style="margin-bottom:16px;"><div class="info-label">Worktree path</div><div class="info-value" style="word-break:break-all;font-size:12px;">${escHtml(s.worktree_path)}</div></div>` : ''}
    <div class="prompt-box">${escHtml(s.prompt)}</div>
    <div class="logs-title">
      <span id="logs-count">Logs (${logs.length})</span>
      <label style="margin-left: 20px; font-size: 13px; font-weight: normal; cursor: pointer;">
        <input type="checkbox" id="auto-scroll-checkbox" ${autoScroll ? 'checked' : ''}> Auto-scroll
      </label>
    </div>
    <div class="logs-box" id="logs-box">${logsHtml}</div>
  `;

  document.getElementById("auto-scroll-checkbox").onchange = (e) => {
    autoScroll = e.target.checked;
  };

  const box = document.getElementById("logs-box");
  if (box && autoScroll) box.scrollTop = box.scrollHeight;

  if (elapsedTimer) clearInterval(elapsedTimer);
  if (ACTIVE_STATUSES.includes(s.status)) {
    elapsedTimer = setInterval(() => {
      const el = document.getElementById("elapsed-display");
      if (!el) return;
      const latest = sessions.find(x => x.session_id === id);
      if (latest) el.textContent = formatElapsed(getElapsed(latest));
    }, 1000);
  } else {
    elapsedTimer = null;
  }
}

function appendLog(id, log) {
  const box = document.getElementById("logs-box");
  const countEl = document.getElementById("logs-count");
  if (!box) return;

  const div = document.createElement("div");
  div.className = "log-line";
  const formattedContent = formatLogContent(log.content);
  div.innerHTML = `<div class="log-level ${log.level}">${log.level}</div><div class="log-content">${formattedContent}</div>`;
  box.appendChild(div);

  if (countEl) countEl.textContent = `Logs (${streamLogs[id].length})`;
  if (autoScroll) box.scrollTop = box.scrollHeight;
}

// ---- SSE real-time logs ----

function startStream(id) {
  if (streamSource) streamSource.close();
  if (!streamLogs[id]) streamLogs[id] = [];

  const s = sessions.find(x => x.session_id === id);
  if (!s || ["completed", "failed", "cancelled", "review"].includes(s.status)) return;

  streamSource = new EventSource(`${API}/sessions/${id}/stream`);
  streamSource.onmessage = (e) => {
    if (e.data === "[DONE]") { streamSource.close(); return; }
    const idx = e.data.indexOf("|");
    const level = e.data.slice(0, idx);
    const content = e.data.slice(idx + 1);
    const log = { level, content };
    streamLogs[id].push(log);
    if (selectedId === id) appendLog(id, log);
  };
}

// ---- Session actions ----

async function submitTask() {
  const prompt = document.getElementById("prompt-input").value.trim();
  if (!prompt) return;

  const usePlanMode = document.getElementById("use-plan-mode").checked;
  const btn = document.getElementById("btn-submit");
  btn.disabled = true;
  btn.textContent = "Submitting...";

  try {
    const res = await fetch(`${API}/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, use_plan_mode: usePlanMode }),
    });
    const data = await res.json();
    document.getElementById("prompt-input").value = "";
    await fetchSessions();
    selectSession(data.session_id);
  } catch (e) {
    alert("Submit failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Submit";
  }
}

async function stopSession() {
  if (!selectedId) return;
  const s = sessions.find(x => x.session_id === selectedId);
  if (!s) return;

  const msg = s.status === 'review'
    ? 'Cancel this task? This will close the MR and clean up the worktree.'
    : 'Cancel this task?';

  if (!confirm(msg)) return;

  await fetch(`${API}/sessions/${selectedId}/stop`, { method: "POST" });
  await fetchSessions();
}

async function deleteSession() {
  if (!selectedId) return;
  if (!confirm('Delete this task? This cannot be undone.')) return;

  await fetch(`${API}/sessions/${selectedId}`, { method: "DELETE" });
  selectedId = null;
  lastRenderedId = null;
  document.getElementById("detail-content").innerHTML = `<div class="detail-empty">Select a task to view details</div>`;
  document.getElementById("btn-stop").disabled = true;
  document.getElementById("btn-complete").disabled = true;
  document.getElementById("btn-delete").disabled = true;
  await fetchSessions();
}

async function completeSession() {
  if (!selectedId) return;
  await fetch(`${API}/sessions/${selectedId}/complete`, { method: "POST" });
  await fetchSessions();
}

// ---- Happy ----

let happyRunning = false;

async function refreshHappyBtn(id) {
  try {
    const res = await fetch(`${API}/sessions/${id}/happy`);
    const data = await res.json();
    happyRunning = data.running;
    const btn = document.getElementById("btn-happy");
    const link = document.getElementById("detail-happy-link");
    if (!btn) return;
    if (happyRunning) {
      btn.textContent = "Stop Happy";
      btn.classList.add("running");
      if (link) link.style.display = "none";
    } else {
      btn.textContent = "Start Happy";
      btn.classList.remove("running");
      if (link) link.style.display = "none";
    }
  } catch (e) {}
}

async function toggleHappy() {
  if (!selectedId) return;
  const btn = document.getElementById("btn-happy");
  btn.disabled = true;
  try {
    if (happyRunning) {
      await fetch(`${API}/sessions/${selectedId}/happy`, { method: "DELETE" });
    } else {
      await fetch(`${API}/sessions/${selectedId}/happy`, { method: "POST" });
      for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 1000));
        await fetchSessions();
        const s = sessions.find(x => x.session_id === selectedId);
        if (s?.happy_session_id) {
          renderDetail(selectedId);
          break;
        }
      }
    }
    await refreshHappyBtn(selectedId);
  } catch (e) {
    alert("Action failed: " + e.message);
  } finally {
    btn.disabled = false;
  }
}

// ---- Plan conversation ----

let planPollingInterval = null;
const planSessionStates = new Map(); // sessionId -> { offset, renderedIds: Set }

function getPlanState(sessionId) {
  if (!planSessionStates.has(sessionId)) {
    planSessionStates.set(sessionId, { offset: 0, renderedIds: new Set() });
  }
  return planSessionStates.get(sessionId);
}

function startPlanPolling(id) {
  if (planPollingInterval) {
    clearInterval(planPollingInterval);
    planPollingInterval = null;
  }

  const state = getPlanState(id);

  async function pollMessages() {
    if (selectedId !== id) {
      clearInterval(planPollingInterval);
      planPollingInterval = null;
      return;
    }

    try {
      const res = await fetch(`${API}/sessions/${id}/plan/messages?offset=${state.offset}`);
      const data = await res.json();
      const messages = data.messages || [];

      if (messages.length > 0) {
        const container = document.getElementById("plan-messages");
        if (container) {
          const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 5;

          messages.forEach(msg => {
            if (!state.renderedIds.has(msg.id)) {
              state.renderedIds.add(msg.id);
              const div = document.createElement("div");
              div.style.marginBottom = "12px";
              div.innerHTML = `
                <div style="font-size: 11px; color: #64748b; margin-bottom: 4px;">${msg.role === 'user' ? 'You' : 'Claude'}</div>
                <div style="font-size: 13px; color: ${msg.role === 'user' ? '#e2e8f0' : '#cbd5e1'};">${formatPlanMessage(msg.content)}</div>
              `;
              container.appendChild(div);
            }
          });

          if (wasAtBottom) {
            container.scrollTop = container.scrollHeight;
          }

          state.offset += messages.length;
        }
      }
    } catch (e) {}
  }

  pollMessages();
  planPollingInterval = setInterval(pollMessages, POLL_INTERVAL);
}

function formatPlanMessage(content) {
  try {
    const json = JSON.parse(content);

    if (json.type === 'system' && json.subtype === 'init') {
      return `<div style="padding: 8px; background: #1a1f2e; border-radius: 6px; font-size: 12px; color: #64748b;">
        <div style="margin-bottom: 4px;">✓ Session initialized</div>
        <div style="font-family: monospace; color: #475569;">Model: ${escHtml(json.model || 'N/A')}</div>
        <div style="font-family: monospace; color: #475569;">Session ID: ${escHtml(json.session_id || 'N/A')}</div>
      </div>`;
    }

    if (json.type === 'user' && json.message) {
      const msg = json.message;
      let formatted = '';

      if (msg.content && Array.isArray(msg.content)) {
        for (const block of msg.content) {
          if (block.type === 'text') {
            formatted += `<div style="white-space: pre-wrap; line-height: 1.5;">${escHtml(block.text)}</div>`;
          } else if (block.type === 'tool_result') {
            let resultContent = '';
            if (typeof block.content === 'string') {
              resultContent = block.content;
            } else if (Array.isArray(block.content)) {
              resultContent = block.content.map(item => {
                if (typeof item === 'string') return item;
                if (item.type === 'text') return item.text || '';
                return JSON.stringify(item);
              }).join('\n');
            } else {
              resultContent = JSON.stringify(block.content);
            }

            const preview = resultContent.length > 500 ? resultContent.substring(0, 500) + '\n...' : resultContent;
            const isError = block.is_error || false;
            const borderColor = isError ? '#ef4444' : '#10b981';
            const iconColor = isError ? '#f87171' : '#34d399';
            const icon = isError ? '✗' : '✓';

            formatted += `<div style="margin: 8px 0; padding: 8px; background: #1e293b; border-left: 3px solid ${borderColor}; border-radius: 4px;">
              <div style="font-size: 11px; color: ${iconColor}; margin-bottom: 4px;">${icon} Tool Result</div>
              <div style="font-size: 12px; color: #94a3b8; font-family: monospace; white-space: pre-wrap;">${escHtml(preview)}</div>
            </div>`;
          }
        }
      }

      return formatted || escHtml(content);
    }

    if (json.type === 'assistant' && json.message) {
      const msg = json.message;
      let formatted = '';

      if (msg.content && Array.isArray(msg.content)) {
        for (const block of msg.content) {
          if (block.type === 'text') {
            formatted += `<div style="white-space: pre-wrap; line-height: 1.5;">${escHtml(block.text)}</div>`;
          } else if (block.type === 'tool_use') {
            if (block.name === 'AskUserQuestion') {
              const input = block.input || {};
              const questions = input.questions || [];

              formatted += `<div style="margin: 8px 0; padding: 12px; background: #1e293b; border-left: 3px solid #a855f7; border-radius: 4px;">
                <div style="font-size: 11px; color: #c4b5fd; margin-bottom: 8px;">❓ Claude's question</div>`;

              questions.forEach((q, qIndex) => {
                const questionId = `question-${block.id}-${qIndex}`;
                formatted += `<div style="margin-bottom: 12px;">
                  <div style="font-size: 13px; color: #e2e8f0; margin-bottom: 8px; font-weight: 500;">${escHtml(q.question)}</div>`;

                if (q.multiSelect) {
                  q.options.forEach((opt) => {
                    formatted += `<div style="margin-bottom: 4px;">
                      <label style="display: flex; align-items: start; gap: 8px; cursor: pointer; padding: 6px; border-radius: 4px; transition: background 0.15s;" onmouseover="this.style.background='#0f172a'" onmouseout="this.style.background='transparent'">
                        <input type="checkbox" name="${questionId}" value="${escHtml(opt.label)}" style="margin-top: 2px;">
                        <div>
                          <div style="font-size: 12px; color: #cbd5e1;">${escHtml(opt.label)}</div>
                          ${opt.description ? `<div style="font-size: 11px; color: #64748b; margin-top: 2px;">${escHtml(opt.description)}</div>` : ''}
                        </div>
                      </label>
                    </div>`;
                  });
                } else {
                  q.options.forEach((opt) => {
                    formatted += `<div style="margin-bottom: 4px;">
                      <label style="display: flex; align-items: start; gap: 8px; cursor: pointer; padding: 6px; border-radius: 4px; transition: background 0.15s;" onmouseover="this.style.background='#0f172a'" onmouseout="this.style.background='transparent'">
                        <input type="radio" name="${questionId}" value="${escHtml(opt.label)}" style="margin-top: 2px;">
                        <div>
                          <div style="font-size: 12px; color: #cbd5e1;">${escHtml(opt.label)}</div>
                          ${opt.description ? `<div style="font-size: 11px; color: #64748b; margin-top: 2px;">${escHtml(opt.description)}</div>` : ''}
                        </div>
                      </label>
                    </div>`;
                  });
                }

                formatted += `</div>`;
              });

              formatted += `<button onclick="submitQuestionAnswers('${block.id}')" style="background: #a855f7; color: #fff; font-size: 12px; padding: 6px 12px; border-radius: 6px; border: none; cursor: pointer; font-weight: 500; margin-top: 8px;">Submit answer</button>
              </div>`;
            } else {
              const inputStr = JSON.stringify(block.input || {}, null, 2);
              const preview = inputStr.length > 300 ? inputStr.substring(0, 300) + '\n...' : inputStr;
              formatted += `<div style="margin: 8px 0; padding: 8px; background: #1e293b; border-left: 3px solid #3b82f6; border-radius: 4px;">
                <div style="font-size: 11px; color: #60a5fa; margin-bottom: 4px;">🔧 ${escHtml(block.name || 'Tool')}</div>
                <div style="font-size: 12px; color: #94a3b8; font-family: monospace; white-space: pre-wrap;">${escHtml(preview)}</div>
              </div>`;
            }
          } else if (block.type === 'tool_result') {
            let resultContent = '';
            if (typeof block.content === 'string') {
              resultContent = block.content;
            } else if (Array.isArray(block.content)) {
              resultContent = block.content.map(item => {
                if (typeof item === 'string') return item;
                if (item.type === 'text') return item.text || '';
                return JSON.stringify(item);
              }).join('\n');
            } else {
              resultContent = JSON.stringify(block.content);
            }

            const preview = resultContent.length > 500 ? resultContent.substring(0, 500) + '\n...' : resultContent;
            const isError = block.is_error || false;
            const borderColor = isError ? '#ef4444' : '#10b981';
            const iconColor = isError ? '#f87171' : '#34d399';
            const icon = isError ? '✗' : '✓';

            formatted += `<div style="margin: 8px 0; padding: 8px; background: #1e293b; border-left: 3px solid ${borderColor}; border-radius: 4px;">
              <div style="font-size: 11px; color: ${iconColor}; margin-bottom: 4px;">${icon} Result</div>
              <div style="font-size: 12px; color: #94a3b8; font-family: monospace; white-space: pre-wrap;">${escHtml(preview)}</div>
            </div>`;
          }
        }
      }

      return formatted || escHtml(content);
    }

    if (json.type === 'result' && json.result) {
      return `<div style="white-space: pre-wrap; line-height: 1.5;">${escHtml(json.result)}</div>`;
    }

    return `<div style="white-space: pre-wrap;">${escHtml(content)}</div>`;
  } catch (e) {
    return `<div style="white-space: pre-wrap; line-height: 1.5;">${escHtml(content)}</div>`;
  }
}

function formatLogContent(content) {
  return formatPlanMessage(content);
}

async function sendPlanMessage() {
  if (!selectedId) return;
  const input = document.getElementById("plan-input");
  const message = input.value.trim();
  if (!message) return;

  input.value = "";
  input.disabled = true;
  try {
    await fetch(`${API}/sessions/${selectedId}/plan/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
  } catch (e) {
    alert("Send failed: " + e.message);
  } finally {
    input.disabled = false;
    input.focus();
  }
}

async function submitQuestionAnswers(toolUseId) {
  if (!selectedId) return;

  const answers = {};
  const questions = document.querySelectorAll(`input[name^="question-${toolUseId}-"]`);

  const questionGroups = {};
  questions.forEach(input => {
    const questionId = input.name;
    if (!questionGroups[questionId]) {
      questionGroups[questionId] = [];
    }

    if (input.type === 'radio' && input.checked) {
      questionGroups[questionId] = [input.value];
    } else if (input.type === 'checkbox' && input.checked) {
      questionGroups[questionId].push(input.value);
    }
  });

  Object.keys(questionGroups).forEach(questionId => {
    const match = questionId.match(/question-.*-(\d+)$/);
    if (match) {
      const qIndex = parseInt(match[1]);
      const values = questionGroups[questionId];
      if (values.length > 0) {
        answers[`question_${qIndex}`] = values.length === 1 ? values[0] : values;
      }
    }
  });

  const toolResultMessage = JSON.stringify({
    role: "user",
    content: [{
      type: "tool_result",
      tool_use_id: toolUseId,
      content: JSON.stringify(answers)
    }]
  });

  try {
    await fetch(`${API}/sessions/${selectedId}/plan/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: toolResultMessage }),
    });
  } catch (e) {
    alert("Submit failed: " + e.message);
  }
}

async function confirmPlan() {
  if (!selectedId) return;
  if (!confirm("Confirm plan and start developing?")) return;

  try {
    await fetch(`${API}/sessions/${selectedId}/plan/confirm`, { method: "POST" });
    await fetchSessions();
  } catch (e) {
    alert("Confirm failed: " + e.message);
  }
}

// ---- Keyboard shortcuts ----

document.getElementById("prompt-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    submitTask();
  }
});

// ---- Utility functions ----

function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---- Start polling ----

fetchSessions();
setInterval(fetchSessions, POLL_INTERVAL);
setInterval(() => {
  const hasActive = sessions.some(s => ACTIVE_STATUSES.includes(s.status));
  if (hasActive) renderList();
}, 1000);
