const STORAGE_KEY = "baxter.ic.workspace.threads.v1";

const DEFAULT_WORKSPACES = [
  {
    workspace_id: "prismax",
    label: "PrisMax",
    model: "PrisMax",
    description: "PrisMax Baxter source scope",
  },
  {
    workspace_id: "prismaflex",
    label: "Prismaflex",
    model: "Prismaflex",
    description: "Prismaflex Baxter source scope",
  },
];

const state = {
  workspaces: DEFAULT_WORKSPACES,
  store: null,
  busy: false,
  settings: null,
  settingsTab: "llm",
  settingsTest: null,
};

const qs = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${text}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function powerShellString(value) {
  return String(value ?? "").replaceAll("`", "``").replaceAll('"', '`"');
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "readonly");
  area.style.position = "fixed";
  area.style.left = "-9999px";
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  area.remove();
}

function newId(prefix) {
  const raw = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `${prefix}_${String(raw).replaceAll("-", "").slice(0, 16)}`;
}

function loadStore() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (parsed?.workspaces) return parsed;
  } catch (_error) {
    // Fall through to a fresh store.
  }
  return {
    activeWorkspaceId: "prismax",
    activeThreadId: null,
    workspaces: {},
  };
}

function saveStore() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.store));
}

function workspaceById(workspaceId) {
  return state.workspaces.find((item) => item.workspace_id === workspaceId) || state.workspaces[0];
}

function workspaceState(workspaceId) {
  if (!state.store.workspaces[workspaceId]) {
    state.store.workspaces[workspaceId] = { threads: [] };
  }
  return state.store.workspaces[workspaceId];
}

function createThread(workspaceId, title = "New Thread") {
  const thread = {
    id: newId("thread"),
    title,
    sessionId: null,
    messages: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  workspaceState(workspaceId).threads.unshift(thread);
  state.store.activeWorkspaceId = workspaceId;
  state.store.activeThreadId = thread.id;
  saveStore();
  return thread;
}

function activeWorkspace() {
  return workspaceById(state.store.activeWorkspaceId || "prismax");
}

function activeThread() {
  const workspaceId = activeWorkspace().workspace_id;
  const threads = workspaceState(workspaceId).threads;
  let thread = threads.find((item) => item.id === state.store.activeThreadId);
  if (!thread) thread = threads[0] || createThread(workspaceId);
  state.store.activeThreadId = thread.id;
  return thread;
}

function setActiveWorkspace(workspaceId) {
  state.store.activeWorkspaceId = workspaceId;
  const threads = workspaceState(workspaceId).threads;
  state.store.activeThreadId = threads[0]?.id || createThread(workspaceId).id;
  saveStore();
  render();
}

function setActiveThread(threadId) {
  state.store.activeThreadId = threadId;
  saveStore();
  render();
}

function deleteThread(threadId) {
  const workspaceId = activeWorkspace().workspace_id;
  const workspaceData = workspaceState(workspaceId);
  const index = workspaceData.threads.findIndex((thread) => thread.id === threadId);
  if (index < 0) return;

  workspaceData.threads.splice(index, 1);
  if (state.store.activeThreadId === threadId) {
    const nextThread = workspaceData.threads[index] || workspaceData.threads[index - 1] || createThread(workspaceId);
    state.store.activeThreadId = nextThread.id;
  }
  saveStore();
  render();
}

function addMessage(message) {
  const thread = activeThread();
  thread.messages.push({
    id: newId("msg"),
    createdAt: new Date().toISOString(),
    ...message,
  });
  thread.updatedAt = new Date().toISOString();
  if (message.role === "user" && (!thread.title || thread.title === "New Thread")) {
    thread.title = message.text.slice(0, 48) || "New Thread";
  }
  saveStore();
  renderThread();
  renderSidebar();
}

function applyWorkspaceTheme(workspaceId) {
  const nextTheme = `theme-${String(workspaceId || "prismax").toLowerCase()}`;
  for (const className of Array.from(document.body.classList)) {
    if (className.startsWith("theme-")) {
      document.body.classList.remove(className);
    }
  }
  document.body.classList.add(nextTheme);
  document.body.dataset.workspaceTheme = nextTheme;
}

function render() {
  const workspaceId = state.store.activeWorkspaceId || "prismax";
  
  // Manage theme classes without overwriting other classes (like sidebar-collapsed)
  document.body.classList.remove("theme-prismax", "theme-prismaflex");
  document.body.classList.add(`theme-${workspaceId}`);
  
  // Re-apply sidebar collapsed class based on store state
  if (state.store.sidebarCollapsed) {
    document.body.classList.add("sidebar-collapsed");
  } else {
    document.body.classList.remove("sidebar-collapsed");
  }

  renderSidebar();
  renderThread();
  renderScope();
  renderIcons();
}

function renderSidebar() {
  const root = qs("#workspaceList");
  const filter = qs("#searchInput").value.trim().toLowerCase();
  root.innerHTML = "";
  for (const workspace of state.workspaces) {
    const workspaceData = workspaceState(workspace.workspace_id);
    const visibleThreads = workspaceData.threads.filter((thread) =>
      `${workspace.label} ${thread.title}`.toLowerCase().includes(filter),
    );
    if (filter && !workspace.label.toLowerCase().includes(filter) && visibleThreads.length === 0) {
      continue;
    }

    const group = document.createElement("div");
    group.className = "workspace-group";

    const workspaceButton = document.createElement("button");
    workspaceButton.type = "button";
    workspaceButton.className = `workspace-item ${workspace.workspace_id === state.store.activeWorkspaceId ? "active" : ""}`;
    workspaceButton.addEventListener("click", () => setActiveWorkspace(workspace.workspace_id));

    const grip = document.createElement("i");
    grip.setAttribute("data-lucide", "grip-vertical");
    workspaceButton.appendChild(grip);

    const label = document.createElement("span");
    label.textContent = workspace.label;
    workspaceButton.appendChild(label);

    const count = document.createElement("small");
    count.textContent = String(workspaceData.threads.length || 0);
    workspaceButton.appendChild(count);
    group.appendChild(workspaceButton);

    if (workspace.workspace_id === state.store.activeWorkspaceId) {
      const threadList = document.createElement("div");
      threadList.className = "thread-list";
      for (const thread of visibleThreads.length ? visibleThreads : workspaceData.threads) {
        const row = document.createElement("div");
        row.className = `thread-row ${thread.id === state.store.activeThreadId ? "active" : ""}`;

        const threadButton = document.createElement("button");
        threadButton.type = "button";
        threadButton.className = "thread-item";
        threadButton.addEventListener("click", () => setActiveThread(thread.id));
        threadButton.textContent = thread.title || "New Thread";
        row.appendChild(threadButton);

        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = "thread-delete-button";
        deleteButton.title = "Delete thread";
        deleteButton.setAttribute("aria-label", `Delete ${thread.title || "thread"}`);
        deleteButton.addEventListener("click", (event) => {
          event.stopPropagation();
          deleteThread(thread.id);
        });
        const trash = document.createElement("i");
        trash.setAttribute("data-lucide", "trash-2");
        deleteButton.appendChild(trash);
        row.appendChild(deleteButton);

        threadList.appendChild(row);
      }
      group.appendChild(threadList);
    }

    root.appendChild(group);
  }
  renderIcons();
}

function renderScope() {
  const workspace = activeWorkspace();
  const thread = activeThread();
  qs("#activeWorkspaceLabel").textContent = workspace.label;
  qs("#activeThreadLabel").textContent = thread.title || "New Thread";
  qs("#selectedScopeChip").textContent = workspace.label;
}

let lastActiveThreadId = null;

function renderThread() {
  const root = qs("#thread");
  const thread = activeThread();
  root.innerHTML = "";
  qs("#emptyState").hidden = thread.messages.length > 0;
  for (const message of thread.messages) {
    root.appendChild(renderMessage(message));
  }
  root.scrollTop = root.scrollHeight;
  renderScope();
  renderIcons();

  if (thread.id !== lastActiveThreadId) {
    lastActiveThreadId = thread.id;
    const messages = root.querySelectorAll(".message");
    if (messages.length > 0 && window.anime) {
      window.anime({
        targets: messages,
        translateY: [24, 0],
        opacity: [0, 1],
        delay: window.anime.stagger(50),
        duration: 800,
        easing: "easeOutElastic(1, 0.8)",
      });
  }
}
}

function renderMessage(message) {
  const node = document.createElement("article");
  node.className = `message ${message.role}`;

  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = message.text || "";
  node.appendChild(body);

  if (message.meta || message.citations?.length) {
    const meta = document.createElement("div");
    meta.className = "message-meta";
    if (message.meta?.answerSource) {
      const source = document.createElement("span");
      source.className = "meta-pill";
      source.textContent = message.meta.answerSource;
      meta.appendChild(source);
    }
    if (message.meta?.hitCount !== undefined) {
      const hits = document.createElement("span");
      hits.className = "meta-pill";
      hits.textContent = `${message.meta.hitCount} hits`;
      meta.appendChild(hits);
    }
    node.appendChild(meta);
  }

  if (message.citations?.length) {
    const citations = document.createElement("div");
    citations.className = "citation-row";
    message.citations.forEach((citation, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "citation-chip";
      button.textContent = `[${index + 1}] ${citation.display || citation.doc_id || "Source"}`;
      button.addEventListener("click", () => previewSource(citation.citation_id));
      citations.appendChild(button);
    });
    node.appendChild(citations);
  }

  return node;
}

async function previewSource(citationId) {
  if (!citationId) return;
  const panel = qs("#sourcePanel");
  const preview = qs("#sourcePreview");
  panel.hidden = false;
  preview.textContent = "Loading source...";
  try {
    const payload = await api(`/api/sources/${encodeURIComponent(citationId)}`);
    preview.textContent = payload.preview || payload.error || "No preview";
  } catch (error) {
    preview.textContent = error.message;
  }
}

async function ensureBackendSession() {
  const workspace = activeWorkspace();
  const thread = activeThread();
  if (thread.sessionId) return thread.sessionId;
  const payload = await api("/api/chat/sessions", {
    method: "POST",
    body: JSON.stringify({
      source_workspace_id: workspace.workspace_id,
      model: workspace.model,
    }),
  });
  thread.sessionId = payload.session_id;
  saveStore();
  return thread.sessionId;
}

async function submitQuestion(event) {
  event.preventDefault();
  if (state.busy) return;
  const input = qs("#questionInput");
  const question = input.value.trim();
  if (!question) return;
  const workspace = activeWorkspace();

  state.busy = true;
  input.value = "";
  input.disabled = true;
  const sendBtn = qs(".send-button");
  if (sendBtn) sendBtn.disabled = true;

  addMessage({ role: "user", text: question });
  addMessage({ role: "system", text: "Searching selected Baxter source workspace..." });

  try {
    const sessionId = await ensureBackendSession();
    const payload = await api("/api/chat/messages", {
      method: "POST",
      body: JSON.stringify({
        question,
        session_id: sessionId,
        source_workspace_id: workspace.workspace_id,
        model: workspace.model,
        use_llm: true,
      }),
    });
    removeLastSystemMessage();
    activeThread().sessionId = payload.session_id;
    addMessage({
      role: "assistant",
      text: payload.answer || "",
      citations: payload.citations || [],
      meta: {
        answerSource: payload.answer_source,
        hitCount: payload.hits?.length || 0,
      },
    });
  } catch (error) {
    removeLastSystemMessage();
    addMessage({ role: "system", text: `Error: ${error.message}` });
  } finally {
    state.busy = false;
    input.disabled = false;
    if (sendBtn) sendBtn.disabled = false;
    input.focus();
  }
}

function removeLastSystemMessage() {
  const thread = activeThread();
  const last = thread.messages[thread.messages.length - 1];
  if (last?.role === "system") {
    thread.messages.pop();
    saveStore();
    renderThread();
  }
}

async function refreshStatus() {
  try {
    const payload = await api("/api/status");
    const inventory = payload.inventory?.summary || {};
    const index = payload.index || {};
    const documents = inventory.document_count || 0;
    const chunks = index.chunk_count || 0;
    qs("#runtimeSummary").textContent = `${documents} docs / ${chunks} chunks`;
    if (Array.isArray(payload.workspaces) && payload.workspaces.length) {
      state.workspaces = payload.workspaces;
      render();
    }
  } catch (error) {
    qs("#runtimeSummary").textContent = `Status error`;
  }
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  return state.settings;
}

async function openSettings() {
  qs("#settingsDialog").hidden = false;
  document.body.classList.add("settings-open");
  state.settingsTest = null;
  renderSettings();
  try {
    await loadSettings();
  } catch (error) {
    state.settings = { error: error.message };
  }
  renderSettings();
}

function closeSettings() {
  qs("#settingsDialog").hidden = true;
  document.body.classList.remove("settings-open");
  const apiKey = qs("#llmApiKey");
  if (apiKey) apiKey.value = "";
}

function renderSettings() {
  const panel = qs("#settingsPanel");
  if (!panel) return;
  document.querySelectorAll(".settings-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === state.settingsTab);
  });
  const title = qs("#settingsPanelTitle");
  const subtitle = qs("#settingsPanelSubtitle");
  const meta = {
    llm: ["AI Provider", "OpenAI-compatible endpoint for grounded Baxter answer stages."],
    retrieval: ["Retrieval", "Workspace filters, local hybrid search, and model profile."],
    data: ["Data / Index", "Corpus readiness, index status, and maintenance actions."],
  }[state.settingsTab];
  title.textContent = meta[0];
  subtitle.textContent = meta[1];

  if (!state.settings) {
    panel.innerHTML = `<div class="settings-loading">Loading settings...</div>`;
    renderIcons();
    return;
  }
  if (state.settings.error) {
    panel.innerHTML = `<div class="settings-callout danger">${escapeHtml(state.settings.error)}</div>`;
    return;
  }
  if (state.settingsTab === "retrieval") renderRetrievalSettings();
  else if (state.settingsTab === "data") renderDataSettings();
  else renderLLMSettings();
  renderIcons();
}

function renderLLMSettings() {
  const panel = qs("#settingsPanel");
  const settings = state.settings || {};
  const llm = settings.llm || {};
  const presets = settings.provider_presets || [];
  const configured = Boolean(llm.configured);
  const providerButtons = presets
    .map(
      (preset) => `
        <button class="provider-option ${preset.id === llm.provider_preset ? "active" : ""}" data-provider="${escapeHtml(preset.id)}" type="button">
          <span>${escapeHtml(preset.label)}${preset.recommended ? " / recommended" : ""}</span>
          <small>${escapeHtml(preset.note || "")}</small>
        </button>
      `,
    )
    .join("");
  panel.innerHTML = `
    <div class="settings-grid">
      <section class="settings-section provider-list">
        <h3>Provider</h3>
        <div class="provider-options">${providerButtons}</div>
      </section>
      <section class="settings-section settings-form">
        <div class="settings-callout ${configured ? "ok" : "warn"}">
          ${configured ? "LLM provider is configured from environment." : "LLM provider is not configured yet. Chat will stay on visible extractive fallback."}
        </div>
        <label class="field">
          <span>Base URL</span>
          <input id="llmBaseUrl" value="${escapeHtml(llm.base_url || "https://api.deepseek.com")}" spellcheck="false" />
        </label>
        <label class="field">
          <span>Model</span>
          <input id="llmModel" value="${escapeHtml(llm.model || "deepseek-v4-flash")}" spellcheck="false" />
        </label>
        <label class="field">
          <span>API key</span>
          <input id="llmApiKey" type="password" placeholder="${llm.api_key_set ? "Using key from environment" : "Paste key for test only"}" autocomplete="off" />
        </label>
        <label class="field compact-field">
          <span>Timeout seconds</span>
          <input id="llmTimeout" type="number" min="5" max="300" value="${escapeHtml(llm.timeout_seconds || 60)}" />
        </label>
        <div class="toggle-grid">
          <label class="toggle-line">
            <input id="externalUploadToggle" type="checkbox" ${llm.external_upload_allowed ? "checked" : ""} />
            <span>Allow external snippet upload</span>
          </label>
          <label class="toggle-line">
            <input id="answerQaToggle" type="checkbox" ${llm.answer_qa_enabled ? "checked" : ""} />
            <span>Answer QA</span>
          </label>
          <label class="toggle-line">
            <input id="semanticReviewToggle" type="checkbox" ${llm.semantic_review_enabled ? "checked" : ""} />
            <span>Semantic review</span>
          </label>
        </div>
        <div class="button-row">
          <button class="primary-action" id="testProviderBtn" type="button">
            <i data-lucide="plug-zap"></i>
            <span>Test Provider</span>
          </button>
          <button class="secondary-action" id="copyEnvBtn" type="button">
            <i data-lucide="copy"></i>
            <span>Copy Env</span>
          </button>
        </div>
        <div id="providerTestResult"></div>
      </section>
    </div>
  `;

  document.querySelectorAll(".provider-option").forEach((button) => {
    button.addEventListener("click", () => applyProviderPreset(button.dataset.provider));
  });
  qs("#testProviderBtn").addEventListener("click", testProvider);
  qs("#copyEnvBtn").addEventListener("click", copyLLMEnv);
  renderLLMTestResult();
}

function applyProviderPreset(providerId) {
  const preset = (state.settings?.provider_presets || []).find((item) => item.id === providerId);
  if (!preset) return;
  if (preset.base_url) qs("#llmBaseUrl").value = preset.base_url;
  if (preset.recommended_model) qs("#llmModel").value = preset.recommended_model;
  document.querySelectorAll(".provider-option").forEach((button) => {
    button.classList.toggle("active", button.dataset.provider === providerId);
  });
}

function llmDraft() {
  return {
    base_url: qs("#llmBaseUrl")?.value.trim() || "",
    model: qs("#llmModel")?.value.trim() || "",
    api_key: qs("#llmApiKey")?.value || "",
    timeout_seconds: Number(qs("#llmTimeout")?.value || 60),
  };
}

async function testProvider() {
  const btn = qs("#testProviderBtn");
  if (btn) btn.disabled = true;
  state.settingsTest = { status: "running", message: "Testing provider..." };
  renderLLMTestResult();
  try {
    const payload = await api("/api/settings/llm/test", {
      method: "POST",
      body: JSON.stringify(llmDraft()),
    });
    state.settingsTest = payload;
  } catch (error) {
    state.settingsTest = { ok: false, status: "failed", error: error.message };
  } finally {
    if (btn) btn.disabled = false;
  }
  renderLLMTestResult();
}

function renderLLMTestResult() {
  const target = qs("#providerTestResult");
  if (!target) return;
  const result = state.settingsTest;
  if (!result) {
    target.innerHTML = "";
    return;
  }
  const ok = result.ok === true || result.status === "copied";
  const detail = result.reason || result.error || result.response_preview || result.message || result.status;
  target.innerHTML = `<div class="settings-callout ${ok ? "ok" : result.status === "running" ? "warn" : "danger"}">${escapeHtml(detail)}</div>`;
}

async function copyLLMEnv() {
  const draft = llmDraft();
  const lines = [
    `$env:LLM_BASE_URL="${powerShellString(draft.base_url)}"`,
    `$env:LLM_MODEL="${powerShellString(draft.model)}"`,
    `$env:LLM_API_KEY="paste-your-key-here"`,
    `$env:LLM_TIMEOUT_SECONDS="${powerShellString(String(draft.timeout_seconds || 60))}"`,
    `$env:LLM_EXTERNAL_UPLOAD_ALLOWED="${qs("#externalUploadToggle")?.checked ? "true" : "false"}"`,
    `$env:LLM_ANSWER_QA_ENABLED="${qs("#answerQaToggle")?.checked ? "true" : "false"}"`,
    `$env:LLM_SEMANTIC_REVIEW_ENABLED="${qs("#semanticReviewToggle")?.checked ? "true" : "false"}"`,
  ];
  await copyText(lines.join("\n"));
  state.settingsTest = { ok: true, status: "copied", response_preview: "Environment snippet copied." };
  renderLLMTestResult();
}

function renderRetrievalSettings() {
  const panel = qs("#settingsPanel");
  const settings = state.settings || {};
  const retrieval = settings.retrieval || {};
  const index = settings.index || {};
  const profile = settings.model_profile || {};
  const current = profile.current || {};
  const recommended = profile.recommended || {};
  panel.innerHTML = `
    <div class="settings-section settings-form wide">
      <div class="settings-kpis">
        <div><span>Backend</span><strong>${escapeHtml(retrieval.backend || "local_hybrid")}</strong></div>
        <div><span>Collection</span><strong>${escapeHtml(retrieval.collection || "ic_baxter")}</strong></div>
        <div><span>Chunks</span><strong>${escapeHtml(index.chunk_count || 0)}</strong></div>
      </div>
      <div class="status-table">
        <div><span>Embedding</span><strong>${escapeHtml(current.embedding_model || "local_hash_bm25")}</strong></div>
        <div><span>Embedding backend</span><strong>${escapeHtml(current.embedding_backend || "hash_dense_plus_bm25")}</strong></div>
        <div><span>Rerank</span><strong>${escapeHtml(current.rerank_enabled ? current.rerank_model : "disabled")}</strong></div>
        <div><span>Recommended embedding</span><strong>${escapeHtml(recommended.embedding_model || "BAAI/bge-m3")}</strong></div>
        <div><span>Recommended rerank</span><strong>${escapeHtml(recommended.rerank_model || "BAAI/bge-reranker-v2-m3")}</strong></div>
        <div><span>Recommended LLM</span><strong>${escapeHtml(recommended.llm_model || "deepseek-v4-flash")}</strong></div>
      </div>
      <label class="field">
        <span>Top K</span>
        <input id="retrievalTopK" type="number" min="1" max="40" value="${escapeHtml(retrieval.top_k || 8)}" />
      </label>
      <label class="field">
        <span>Max context chars</span>
        <input id="retrievalMaxContext" type="number" min="1000" max="50000" step="500" value="${escapeHtml(retrieval.max_context_chars || 9000)}" />
      </label>
      <div class="workspace-chip-row">
        ${(settings.workspaces || [])
          .map((workspace) => `<span class="workspace-chip">${escapeHtml(workspace.label)} -> ${escapeHtml(workspace.model)}</span>`)
          .join("")}
      </div>
      <div class="button-row">
        <button class="secondary-action" id="copyRetrievalEnvBtn" type="button">
          <i data-lucide="copy"></i>
          <span>Copy Env</span>
        </button>
      </div>
      <div id="retrievalCopyResult"></div>
    </div>
  `;
  qs("#copyRetrievalEnvBtn").addEventListener("click", copyRetrievalEnv);
}

async function copyRetrievalEnv() {
  const lines = [
    `$env:BAXTER_RAG_TOP_K="${powerShellString(qs("#retrievalTopK")?.value || "8")}"`,
    `$env:BAXTER_RAG_MAX_CONTEXT_CHARS="${powerShellString(qs("#retrievalMaxContext")?.value || "9000")}"`,
    `$env:BAXTER_RAG_EMBEDDING_MODEL="local_hash_bm25"`,
    `$env:BAXTER_RAG_RECOMMENDED_EMBEDDING_MODEL="BAAI/bge-m3"`,
    `$env:BAXTER_RAG_RERANK_ENABLED="false"`,
    `$env:BAXTER_RAG_RERANK_MODEL="BAAI/bge-reranker-v2-m3"`,
  ];
  await copyText(lines.join("\n"));
  qs("#retrievalCopyResult").innerHTML = `<div class="settings-callout ok">Environment snippet copied.</div>`;
}

function renderDataSettings() {
  const panel = qs("#settingsPanel");
  const settings = state.settings || {};
  const inventory = settings.inventory?.summary || {};
  const index = settings.index || {};
  const evaluation = settings.evaluation || {};
  const seed = evaluation.seed || {};
  const audit = evaluation.audit || {};
  const auditRootCause = evaluation.root_cause?.audit?.counts || {};
  const auditRootCauseText = Object.keys(auditRootCause).length ? JSON.stringify(auditRootCause) : "none";
  panel.innerHTML = `
    <div class="settings-section settings-form wide">
      <div class="settings-kpis">
        <div><span>Documents</span><strong>${escapeHtml(inventory.document_count || 0)}</strong></div>
        <div><span>Chunks</span><strong>${escapeHtml(index.chunk_count || 0)}</strong></div>
        <div><span>Workspaces</span><strong>${escapeHtml((settings.workspaces || []).length)}</strong></div>
        <div><span>Seed eval</span><strong>${escapeHtml(seed.passed || 0)}/${escapeHtml(seed.case_count || 0)}</strong></div>
        <div><span>Audit eval</span><strong>${escapeHtml(audit.passed || 0)}/${escapeHtml(audit.case_count || 0)}</strong></div>
        <div><span>Audit root cause</span><strong>${escapeHtml(auditRootCauseText)}</strong></div>
      </div>
      <div class="status-table">
        <div><span>Inventory report</span><strong>${escapeHtml(Boolean(settings.inventory && Object.keys(settings.inventory).length))}</strong></div>
        <div><span>Index generated</span><strong>${escapeHtml(index.generated_at || "not available")}</strong></div>
        <div><span>Qdrant</span><strong>${escapeHtml(index.qdrant?.enabled ? "enabled" : "disabled")}</strong></div>
        <div><span>Seed eval generated</span><strong>${escapeHtml(seed.generated_at || "not available")}</strong></div>
        <div><span>Audit eval generated</span><strong>${escapeHtml(audit.generated_at || "not available")}</strong></div>
        <div><span>Audit pass rate</span><strong>${escapeHtml(audit.pass_rate ?? "not available")}</strong></div>
      </div>
      <div class="settings-kpis">
        <label class="field">
          <span>Audit count</span>
          <input id="auditEvalCount" type="number" min="12" max="300" value="${escapeHtml(audit.case_count || 144)}" />
        </label>
        <label class="field">
          <span>Audit seed</span>
          <input id="auditEvalSeed" type="number" min="1" value="20260621" />
        </label>
        <div><span>Audit suite</span><strong>index random</strong></div>
      </div>
      <div class="button-row">
        <button class="primary-action" id="settingsInventoryBtn" type="button">
          <i data-lucide="folder-search"></i>
          <span>Inventory</span>
        </button>
        <button class="secondary-action" id="settingsRebuildBtn" type="button">
          <i data-lucide="database-zap"></i>
          <span>Rebuild Index</span>
        </button>
        <button class="secondary-action" id="settingsRefreshBtn" type="button">
          <i data-lucide="refresh-cw"></i>
          <span>Refresh</span>
        </button>
        <button class="secondary-action" id="settingsEvalRunBtn" type="button">
          <i data-lucide="list-checks"></i>
          <span>Seed Eval</span>
        </button>
        <button class="secondary-action" id="settingsEvalAuditBtn" type="button">
          <i data-lucide="shield-check"></i>
          <span>Audit Eval</span>
        </button>
        <button class="secondary-action" id="settingsPipelineBtn" type="button">
          <i data-lucide="workflow"></i>
          <span>Pipeline</span>
        </button>
      </div>
    </div>
  `;
  qs("#settingsInventoryBtn").addEventListener("click", async () => {
    await runPipelineStep("Inventory", "/api/documents/inventory", {});
    await loadSettings();
    renderSettings();
  });
  qs("#settingsRebuildBtn").addEventListener("click", async () => {
    await runPipelineStep("Rebuild index", "/api/index/rebuild", {});
    await loadSettings();
    renderSettings();
  });
  qs("#settingsRefreshBtn").addEventListener("click", async () => {
    await refreshStatus();
    await loadSettings();
    renderSettings();
  });
  qs("#settingsEvalRunBtn").addEventListener("click", async () => {
    await runPipelineStep("Seed eval", "/api/eval/run", {});
    await loadSettings();
    renderSettings();
  });
  qs("#settingsEvalAuditBtn").addEventListener("click", async () => {
    await runPipelineStep("Audit eval", "/api/eval/audit", auditEvalBody());
    await loadSettings();
    renderSettings();
  });
  qs("#settingsPipelineBtn").addEventListener("click", async () => {
    await runPipelineStep("Pipeline", "/api/pipeline/run", {
      eval_audit_count: Number(qs("#auditEvalCount")?.value || 144),
      enable_eval_audit: true,
    });
    await loadSettings();
    renderSettings();
  });
}

async function runPipelineStep(label, path, body = {}) {
  addMessage({ role: "system", text: `${label} started...` });
  try {
    const payload = await api(path, { method: "POST", body: JSON.stringify(body) });
    addMessage({ role: "system", text: `${label} finished.\n${JSON.stringify(compactStepPayload(payload), null, 2)}` });
    await refreshStatus();
  } catch (error) {
    addMessage({ role: "system", text: `${label} error: ${error.message}` });
  }
}

function auditEvalBody() {
  return {
    count: Number(qs("#auditEvalCount")?.value || 144),
    seed: Number(qs("#auditEvalSeed")?.value || 20260621),
  };
}

function compactStepPayload(payload) {
  if (payload.summary) return payload.summary;
  if (payload.case_count !== undefined || payload.pass_rate !== undefined) {
    return {
      suite: payload.suite,
      case_count: payload.case_count,
      passed: payload.passed,
      failed: payload.failed,
      pass_rate: payload.pass_rate,
      root_cause_counts: payload.root_cause_counts,
      path: payload.path,
      root_cause_path: payload.root_cause_path,
    };
  }
  if (payload.status && Array.isArray(payload.stages)) {
    return {
      status: payload.status,
      run_id: payload.run_id,
      pipeline_quality_score: payload.final_snapshot?.pipeline_quality_score,
      eval_pass_rate: payload.final_snapshot?.eval_pass_rate,
      eval_audit_pass_rate: payload.final_snapshot?.eval_audit_pass_rate,
      stage_counts: payload.stage_counts,
      report_markdown: payload.report_markdown,
    };
  }
  return payload;
}

function clearThread() {
  const thread = activeThread();
  thread.messages = [];
  thread.sessionId = null;
  thread.title = "New Thread";
  saveStore();
  render();
}

function renderIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function toggleSidebar(collapsed) {
  state.store.sidebarCollapsed = collapsed;
  saveStore();
  if (collapsed) {
    document.body.classList.add("sidebar-collapsed");
  } else {
    document.body.classList.remove("sidebar-collapsed");
  }
}

function bindEvents() {
  qs("#chatForm").addEventListener("submit", submitQuestion);
  qs("#newThreadBtn").addEventListener("click", () => {
    createThread(activeWorkspace().workspace_id);
    render();
  });
  qs("#newThreadTopBtn").addEventListener("click", () => {
    createThread(activeWorkspace().workspace_id);
    render();
  });
  qs("#searchInput").addEventListener("input", renderSidebar);
  qs("#clearThreadBtn").addEventListener("click", clearThread);
  qs("#inventoryBtn").addEventListener("click", () => runPipelineStep("Inventory", "/api/documents/inventory", {}));
  qs("#indexBtn").addEventListener("click", () => runPipelineStep("Rebuild index", "/api/index/rebuild", {}));
  qs("#settingsBtn").addEventListener("click", openSettings);
  qs("#closeSettingsBtn").addEventListener("click", closeSettings);
  qs("#settingsDialog").addEventListener("click", (event) => {
    if (event.target === qs("#settingsDialog")) closeSettings();
  });
  document.querySelectorAll(".settings-tab").forEach((button) => {
    button.addEventListener("click", () => {
      state.settingsTab = button.dataset.tab || "llm";
      state.settingsTest = null;
      renderSettings();
    });
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !qs("#settingsDialog").hidden) closeSettings();
  });
  qs("#collapseBtn").addEventListener("click", () => {
    toggleSidebar(true);
  });
  qs("#showSidebarBtn").addEventListener("click", () => {
    toggleSidebar(false);
  });
  qs("#sourcePanelBtn").addEventListener("click", () => {
    qs("#sourcePanel").hidden = !qs("#sourcePanel").hidden;
  });
  qs("#closeSourceBtn").addEventListener("click", () => {
    qs("#sourcePanel").hidden = true;
  });
  qs("#sourcesHintBtn").addEventListener("click", () => {
    qs("#sourcePanel").hidden = false;
    qs("#sourcePreview").textContent = `Workspace: ${activeWorkspace().label}\nModel filter: ${activeWorkspace().model}`;
  });
  qs("#quickActionAgent").addEventListener("click", () => {
    state.settingsTab = "llm";
    openSettings();
  });
  qs("#quickActionWorkspace").addEventListener("click", () => {
    state.settingsTab = "retrieval";
    openSettings();
  });
  qs("#quickActionUpload").addEventListener("click", () => {
    state.settingsTab = "data";
    openSettings();
  });
}

function boot() {
  state.store = loadStore();
  
  // Apply initial sidebar collapsed state without triggering transitions
  document.body.classList.add("no-transition");
  if (state.store.sidebarCollapsed) {
    document.body.classList.add("sidebar-collapsed");
  } else {
    document.body.classList.remove("sidebar-collapsed");
  }
  document.body.offsetHeight; // Force reflow
  document.body.classList.remove("no-transition");

  for (const workspace of DEFAULT_WORKSPACES) {
    workspaceState(workspace.workspace_id);
  }
  if (!state.store.activeWorkspaceId) state.store.activeWorkspaceId = "prismax";
  if (!workspaceState(state.store.activeWorkspaceId).threads.length) {
    createThread(state.store.activeWorkspaceId);
  }
  bindEvents();
  render();
  refreshStatus();
}

window.addEventListener("DOMContentLoaded", boot);
