    // ==========================================================================
    // STATE MANAGEMENT & LOCAL STORAGE
    // ==========================================================================
    const state = {
      activeProfileId: "",
      currentChatProfileId: "",
      chatBusy: false,
      currentChatId: "",
      currentSessionId: "",
      lastUserMessage: "",
      pendingEdit: false,
      statusDefaultsApplied: false,
      maxChatFileUploadBytes: 128 * 1024 * 1024,
      maxCompletionFiles: 10,
      activeRequest: null,
      selectedFiles: [],
      sessions: [],
      apiKey: "",
      apiKeyRequired: false,
      apiKeySource: "store",
      playwrightAvailable: true,
      browserLoginBusy: false,
      lastStatus: null,
      profiles: [],
      maxProfiles: 0,
      concurrency: null
    };

    const STORAGE_KEY = "glm2api_local_sessions_v1";
    const API_KEY_SESSION_KEY = "glm2api_api_key_session_v1";
    const LOCAL_SESSION_MAX_TOTAL = 120;
    const LOCAL_SESSION_MAX_PER_PROFILE = 30;
    const LOCAL_SESSION_MAX_SCAN = 1000;
    const LOCAL_SESSION_MAX_MESSAGES = 40;
    const LOCAL_SESSION_TEXT_CHARS = 20000;
    const LOCAL_SESSION_MAX_ATTACHMENTS = 32;
    const LOCAL_SESSION_STORAGE_CHAR_BUDGET = 1500000;
    let localSessionStorageWarningShown = false;

    try {
      state.apiKey = sessionStorage.getItem(API_KEY_SESSION_KEY) || "";
    } catch (e) {
      state.apiKey = "";
    }

    function apiHeaders(extra = {}) {
      const headers = { ...extra };
      if (state.apiKey) headers["X-API-Key"] = state.apiKey;
      return headers;
    }

    const MANAGEMENT_FETCH_TIMEOUT_MS = 10000;
    const UPSTREAM_CONTROL_FETCH_TIMEOUT_MS = 15000;
    const FILE_UPLOAD_FETCH_TIMEOUT_MS = 10 * 60 * 1000;
    const HAR_UPLOAD_FETCH_TIMEOUT_MS = 10 * 60 * 1000;
    const BROWSER_LOGIN_FETCH_TIMEOUT_MS = 330 * 1000;

    async function fetchWithTimeout(resource, options = {}, timeoutMs = 10000) {
      const controller = new AbortController();
      const externalSignal = options.signal;
      let timedOut = false;
      const forwardAbort = () => controller.abort();
      if (externalSignal?.aborted) {
        forwardAbort();
      } else if (externalSignal) {
        externalSignal.addEventListener("abort", forwardAbort, { once: true });
      }
      const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, Math.max(1000, Number(timeoutMs) || MANAGEMENT_FETCH_TIMEOUT_MS));
      try {
        return await fetch(resource, { ...options, signal: controller.signal });
      } catch (error) {
        if (error?.name === "AbortError" && timedOut) throw new Error("本地服务响应超时");
        throw error;
      } finally {
        clearTimeout(timer);
        externalSignal?.removeEventListener?.("abort", forwardAbort);
      }
    }

    function profileHeaders(profileId = "", extra = {}) {
      const headers = apiHeaders(extra);
      if (profileId) headers["X-GLM2API-Profile-ID"] = profileId;
      return headers;
    }

    function normalizeLocalAttachment(value) {
      if (!value || typeof value !== "object") return null;
      const name = typeof value.name === "string" ? value.name.slice(0, 260) : "";
      if (!name) return null;
      const size = Number(value.size);
      const lastModified = Number(value.lastModified);
      return {
        name,
        size: Number.isFinite(size) ? Math.max(0, Math.floor(size)) : 0,
        type: typeof value.type === "string" ? value.type.slice(0, 160) : "",
        lastModified: Number.isFinite(lastModified) ? Math.max(0, Math.floor(lastModified)) : 0
      };
    }

    function normalizeLocalMessage(value, textChars = LOCAL_SESSION_TEXT_CHARS) {
      if (!value || typeof value !== "object") return null;
      const role = ["user", "assistant", "system"].includes(value.role) ? value.role : "assistant";
      const attachments = (Array.isArray(value.attachments) ? value.attachments : [])
        .slice(0, LOCAL_SESSION_MAX_ATTACHMENTS)
        .map(normalizeLocalAttachment)
        .filter(Boolean);
      return {
        role,
        content: (typeof value.content === "string" ? value.content : "").slice(0, textChars),
        thinking: (typeof value.thinking === "string" ? value.thinking : "").slice(0, textChars),
        time: typeof value.time === "string" ? value.time.slice(0, 24) : "",
        attachments,
        model: typeof value.model === "string" ? value.model.slice(0, 80) : "",
        incomplete: Boolean(value.incomplete)
      };
    }

    function normalizeLocalSession(value, options = {}) {
      if (!value || typeof value !== "object") return null;
      const id = typeof value.id === "string" ? value.id.slice(0, 120) : "";
      if (!id) return null;
      const maxMessages = Math.max(1, Number(options.maxMessages) || LOCAL_SESSION_MAX_MESSAGES);
      const textChars = Math.max(256, Number(options.textChars) || LOCAL_SESSION_TEXT_CHARS);
      const messages = (Array.isArray(value.messages) ? value.messages : [])
        .slice(-maxMessages)
        .map(message => normalizeLocalMessage(message, textChars))
        .filter(Boolean);
      const rawChatId = typeof value.upstreamChatId === "string" ? value.upstreamChatId : "";
      const upstreamChatId = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(rawChatId)
        ? rawChatId
        : "";
      return {
        id,
        title: typeof value.title === "string" ? value.title.slice(0, 80) : "新会话",
        time: typeof value.time === "string" ? value.time.slice(0, 40) : "",
        upstreamChatId,
        profileId: typeof value.profileId === "string" ? value.profileId.slice(0, 128) : "",
        messages
      };
    }

    function compactLocalSessions(values, options = {}) {
      const maxTotal = Math.max(1, Number(options.maxTotal) || LOCAL_SESSION_MAX_TOTAL);
      const maxPerProfile = Math.max(1, Number(options.maxPerProfile) || LOCAL_SESSION_MAX_PER_PROFILE);
      const budget = Math.max(4096, Number(options.budget) || LOCAL_SESSION_STORAGE_CHAR_BUDGET);
      const sessions = [];
      const seenIds = new Set();
      const perProfile = new Map();
      const source = (Array.isArray(values) ? values : []).slice(0, LOCAL_SESSION_MAX_SCAN);
      for (const value of source) {
        const session = normalizeLocalSession(value, options);
        if (!session || seenIds.has(session.id)) continue;
        const profileKey = session.profileId || "";
        const profileCount = perProfile.get(profileKey) || 0;
        if (profileCount >= maxPerProfile) continue;
        seenIds.add(session.id);
        perProfile.set(profileKey, profileCount + 1);
        sessions.push(session);
        if (sessions.length >= maxTotal) break;
      }

      // Input order is newest-first. Fill the JSON budget in that order, and
      // for each session retain the newest suffix of its chronological messages.
      const compacted = [];
      let usedChars = 2; // outer []
      for (const session of sessions) {
        const stored = { ...session, messages: [] };
        let sessionChars = JSON.stringify(stored).length;
        const outerCommaChars = compacted.length ? 1 : 0;
        if (usedChars + sessionChars + outerCommaChars > budget) continue;
        const newestFirst = [];
        for (let index = session.messages.length - 1; index >= 0; index -= 1) {
          const messageChars = JSON.stringify(session.messages[index]).length;
          const separatorChars = newestFirst.length ? 1 : 0;
          if (usedChars + sessionChars + messageChars + separatorChars + outerCommaChars > budget) break;
          newestFirst.push(session.messages[index]);
          sessionChars += messageChars + separatorChars;
        }
        if (session.messages.length && !newestFirst.length) continue;
        stored.messages = newestFirst.reverse();
        compacted.push(stored);
        usedChars += sessionChars + outerCommaChars;
      }
      return compacted;
    }

    function loadLocalSessions() {
      try {
        const raw = localStorage.getItem(STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        state.sessions = compactLocalSessions(parsed);
      } catch (e) {
        state.sessions = [];
      }
    }

    function saveLocalSessions() {
      const compacted = compactLocalSessions(state.sessions);
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(compacted));
        state.sessions = compacted;
        localSessionStorageWarningShown = false;
      } catch (e) {
        const emergency = compactLocalSessions(compacted.slice(0, 8), {
          maxTotal: 8,
          maxPerProfile: 8,
          maxMessages: 12,
          textChars: 4000,
          budget: 400000
        });
        try {
          localStorage.setItem(STORAGE_KEY, JSON.stringify(emergency));
          state.sessions = emergency;
          if (!localSessionStorageWarningShown) {
            window.showToast?.("本地存储空间不足，已保留最近会话的精简副本", "standby");
            localSessionStorageWarningShown = true;
          }
        } catch (e2) {
          if (!localSessionStorageWarningShown) {
            window.showToast?.("本地会话保存失败：存储空间不足或已被浏览器禁用", "error");
            localSessionStorageWarningShown = true;
          }
        }
      }
    }

    function newLocalSessionId() {
      const random = globalThis.crypto?.randomUUID?.().replace(/-/g, "")
        || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
      return `sess_${random.slice(0, 48)}`;
    }

    // Per-model effort memory: 每个模型记住自己上次的思考挡位，切换模型互不影响；
    // 未记忆时一律落到该模型的最高挡 max。
    const EFFORT_BY_MODEL_KEY = "glm2api_effort_by_model_v1";

    function loadEffortByModel() {
      try {
        return JSON.parse(localStorage.getItem(EFFORT_BY_MODEL_KEY) || "{}") || {};
      } catch (e) {
        return {};
      }
    }

    function saveEffortByModel() {
      try {
        localStorage.setItem(EFFORT_BY_MODEL_KEY, JSON.stringify(state.effortByModel || {}));
      } catch (e) { /* 存储不足时静默退化为会话内记忆 */ }
    }

    state.effortByModel = loadEffortByModel();

    function rememberCurrentEffort() {
      if (!reasoningEffortSelect.value) return;
      state.effortByModel[modelSelect.value] = reasoningEffortSelect.value;
      saveEffortByModel();
    }

    // DOM Elements Cache
    const $ = (id) => document.getElementById(id);
    const messageStream = $("message-stream");
    const composerForm = $("composer-form");
    const promptInput = $("prompt-input");
    const btnSubmit = $("btn-submit");
    const btnSubmitText = $("btn-submit-text");
    const chatFileInput = $("chat-file-input");
    const attachedFilesBar = $("attached-files-bar");
    const uploadHint = $("upload-hint");

    const statusPill = $("status-pill");
    const statusText = $("status-text");
    const modelPillText = $("model-pill-text");
    const currentChatIdBadge = $("current-chat-id");

    const modelSelect = $("model-select");
    const thinkingToggle = $("thinking-toggle");
    const reasoningEffortSelect = $("reasoning-effort-select");
    const reasoningEffortGroup = $("reasoning-effort-group");
    const webSearchToggle = $("web-search-toggle");
    const includeThinkingToggle = $("include-thinking-toggle");
    const autoDeleteToggle = $("auto-delete-toggle");
    const reuseChatToggle = $("reuse-chat-toggle");

    const btnNewSession = $("btn-new-session");
    const btnSidebarNew = $("btn-sidebar-new");
    const btnEditLast = $("btn-edit-last");
    const btnStopActive = $("btn-stop-active");
    const btnDeleteUpstream = $("btn-delete-upstream");
    const btnSaveSettings = $("btn-save-settings");
    const settingsStoreStatus = $("settings-store-status");
    const upstreamTimeoutInput = $("upstream-timeout-input");
    const upstreamRetryWaitInput = $("upstream-retry-wait-input");
    const upstreamRetryAttemptsInput = $("upstream-retry-attempts-input");
    const historyMaxRecordsInput = $("history-max-records-input");
    const apiKeyInput = $("api-key-input");
    const btnSetApiKey = $("btn-set-api-key");
    const btnClearApiKey = $("btn-clear-api-key");
    const apiKeyStatus = $("api-key-status");
    const apiKeyConfigInput = $("api-key-config-input");
    const apiKeyCurrentInput = $("api-key-current-input");
    const btnSaveApiKeyConfig = $("btn-save-api-key-config");
    const btnClearApiKeyConfig = $("btn-clear-api-key-config");
    const apiKeyConfigStatus = $("api-key-config-status");
    const btnClearChat = $("btn-clear-chat");
    const btnExportChat = $("btn-export-chat");
    const sessionListEl = $("session-list");

    const profileSelect = $("profile-select");
    const btnSwitchProfile = $("btn-switch-profile");
    const btnRemoveProfile = $("btn-remove-profile");
    const btnCompactProfiles = $("btn-compact-profiles");
    const btnRefreshProfiles = $("btn-refresh-profiles");
    const profileNameInput = $("profile-name-input");
    const btnBrowserLogin = $("btn-browser-login");
    const harFileInput = $("har-file-input");
    const btnUploadHar = $("btn-upload-har");
    const tokenInput = $("token-input");
    const btnTokenLogin = $("btn-token-login");
    const authStatusBanner = $("auth-status-banner");
    const profilePoolList = $("profile-pool-list");
    const accountRoutingPill = $("account-routing-pill");
    const accountRoutingStatus = $("account-routing-status");

    // ==========================================================================
    // TOAST NOTIFICATION SYSTEM
    // ==========================================================================
    window.showToast = function(text, type = "info") {
      const container = $("toast-container");
      const item = document.createElement("div");
      item.className = `toast-item ${type === "error" ? "toast-error" : (type === "success" ? "toast-success" : "")}`;
      const label = document.createElement("span");
      label.textContent = String(text ?? "");
      item.appendChild(label);
      container.appendChild(item);
      setTimeout(() => {
        item.style.opacity = "0";
        item.style.transform = "translateY(10px)";
        item.style.transition = "all 0.25s ease";
        setTimeout(() => item.remove(), 250);
      }, 3200);
    };

    // ==========================================================================
    // CODE HIGHLIGHTING & MARKDOWN RENDERER
    // ==========================================================================
    function escapeHtml(text) {
      return (text || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function highlightSimpleCode(code, lang = "") {
      let escaped = escapeHtml(code);
      // Comments
      escaped = escaped.replace(/(\/\/[^\n]*|#[^\n]*)/g, '<span class="code-token-cmt">$1</span>');
      // Strings
      escaped = escaped.replace(/(&quot;[\s\S]*?&quot;|&#039;[\s\S]*?&#039;|`[\s\S]*?`)/g, '<span class="code-token-str">$1</span>');
      // Keywords
      escaped = escaped.replace(/\b(const|let|var|function|return|if|else|for|while|import|export|from|class|async|await|try|catch|def|self|public|private|new|switch|case|break|struct|type|interface)\b/g, '<span class="code-token-kw">$1</span>');
      // Numbers
      escaped = escaped.replace(/\b(\d+)\b/g, '<span class="code-token-num">$1</span>');
      return escaped;
    }

    function renderMarkdown(text) {
      if (!text) return "";

      const codeBlocks = [];
      const codeBlockTokens = [];
      let codeBlockTokenIndex = 0;
      // 1. Extract and preserve fenced code blocks
      let processed = text.replace(/```([a-zA-Z0-9_\-#+]*)\n([\s\S]*?)```/g, (match, lang, code) => {
        const cleanLang = (lang || 'plaintext').toLowerCase();
        const highlighted = highlightSimpleCode(code, cleanLang);
        let placeholder = "";
        do {
          placeholder = `__GLM2API_INTERNAL_CODE_BLOCK_${codeBlockTokenIndex}__`;
          codeBlockTokenIndex += 1;
        } while (text.includes(placeholder));
        codeBlockTokens.push(placeholder);
        codeBlocks.push(`
          <div class="code-block-wrapper">
            <div class="code-block-header">
              <span class="code-block-lang">${cleanLang}</span>
              <button class="code-block-copy-btn" onclick="copyCodeFromBlock(this)">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                <span>复制代码</span>
              </button>
            </div>
            <pre><code class="language-${cleanLang}">${highlighted}</code></pre>
          </div>
        `);
        return placeholder;
      });

      // 2. Escape HTML for ordinary text
      processed = escapeHtml(processed);

      // 3. Headings #, ##, ###
      processed = processed.replace(/^### (.*$)/gim, '<h3>$1</h3>');
      processed = processed.replace(/^## (.*$)/gim, '<h2>$1</h2>');
      processed = processed.replace(/^# (.*$)/gim, '<h1>$1</h1>');

      // 4. Blockquotes
      processed = processed.replace(/^\> (.*$)/gim, '<blockquote>$1</blockquote>');

      // 5. Unordered List Items
      processed = processed.replace(/^\s*[\-\*]\s+(.*$)/gim, '<li>$1</li>');
      processed = processed.replace(/(<li>.*<\/li>)/gim, '<ul>$1</ul>');

      // 6. Inline Code `code`
      processed = processed.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');

      // 7. Bold & Italic
      processed = processed.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      processed = processed.replace(/\*([^*]+)\*/g, '<em>$1</em>');

      // 8. Markdown Links [text](url)
      processed = processed.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" style="color: var(--cyan-core); text-decoration: underline;">$1</a>');

      // 9. Paragraphs
      processed = processed.replace(/\n\n+/g, '</p><p>');
      processed = `<p>${processed}</p>`;
      processed = processed.replace(/<p><\/p>/g, '');

      // 10. Restore code blocks
      codeBlocks.forEach((blockHtml, i) => {
        processed = processed.replace(codeBlockTokens[i], blockHtml);
      });

      return processed;
    }

    window.copyCodeFromBlock = function(btn) {
      const codeEl = btn.closest(".code-block-wrapper").querySelector("pre code");
      if (!codeEl) return;
      navigator.clipboard.writeText(codeEl.innerText).then(() => {
        const span = btn.querySelector("span");
        const oldText = span.textContent;
        span.textContent = "已复制!";
        setTimeout(() => { span.textContent = oldText; }, 2000);
      });
    };

    // ==========================================================================
    // PAGE ROUTER（左侧导航 → 页面切换）
    // ==========================================================================
    const PAGE_META = {
      dashboard: { title: "概览", sub: "服务状态与快捷入口" },
      stats: { title: "统计", sub: "请求趋势、Token、模型与运行态" },
      chat: { title: "对话", sub: "与 GLM 模型聊天 · 会话按账号隔离" },
      history: { title: "历史", sub: "请求级镜像：思维链 / 流式输出 / 发给上游的提示词" },
      logs: { title: "日志", sub: "调用过程、重试与报错全链路" },
      accounts: { title: "账号", sub: "登录态管理与添加方式" },
      settings: { title: "设置", sub: "默认行为与上游参数" },
      api: { title: "接口", sub: "API Key、端点与调用示例" }
    };
    let currentPage = "dashboard";

    function navigateTo(pageId) {
      if (!PAGE_META[pageId]) return;
      currentPage = pageId;
      document.querySelectorAll(".nav-item").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.page === pageId);
      });
      document.querySelectorAll(".page").forEach(p => {
        p.classList.toggle("active", p.id === `page-${pageId}`);
      });
      $("topbar-title").textContent = PAGE_META[pageId].title;
      $("topbar-sub").textContent = PAGE_META[pageId].sub;

      if (pageId === "logs") {
        fetchLogs();
        setLogAuto(true);
      } else {
        setLogAuto(false);
      }
      if (pageId === "dashboard") {
        refreshDashboardLog();
        setDashLogAuto(true);
      } else {
        setDashLogAuto(false);
      }
      if (pageId === "dashboard" || pageId === "stats") {
        refreshMetrics();
        setMetricAuto(true);
      } else {
        setMetricAuto(false);
      }
      if (pageId === "history") {
        ensureHistoryLoaded();
        startRecordListAuto();
        scheduleRecordPoll();
      } else {
        stopRecordListAuto();
        stopRecordPoll();
      }
    }

    // ==========================================================================
