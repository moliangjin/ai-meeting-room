"use strict";

// Official Chrome DevTools MCP client boundary. The official SDK owns the
// stdio child process and protocol handshake. The formal route uses an
// explicit loopback browserUrl so the user-selected dedicated Chrome profile
// is deterministic; no endpoint discovery or hand-written JSON-RPC exists here.
const MCP_PACKAGE = "chrome-devtools-mcp@latest";
const MCP_BROWSER_URL = "http://127.0.0.1:9222";
const MCP_STRUCTURED_CONTENT_FLAG = "--experimentalStructuredContent";
const MCP_ARGS = ["-y", MCP_PACKAGE, "--browserUrl", MCP_BROWSER_URL, MCP_STRUCTURED_CONTENT_FLAG];
const AUTOCONNECT_ARGS = ["-y", MCP_PACKAGE, "--autoConnect"];
const BROWSER_TOOL_NAMES = new Set(["list_pages", "select_page", "evaluate_script"]);
const CHATGPT_HOST = "chatgpt.com";

const MCP_STATES = Object.freeze({
  STOPPED: "STOPPED",
  STARTING_MCP: "STARTING_MCP",
  INITIALIZING_MCP: "INITIALIZING_MCP",
  DISCOVERING_TOOLS: "DISCOVERING_TOOLS",
  CONNECTING_BROWSER: "CONNECTING_BROWSER",
  LISTING_PAGES: "LISTING_PAGES",
  BINDING_CHATGPT: "BINDING_CHATGPT",
  CHECKING_COMPOSER: "CHECKING_COMPOSER",
  READY: "READY",
  ERROR: "ERROR",
  UNKNOWN: "UNKNOWN",
});

function isChatGPTUrl(rawUrl) {
  try {
    const parsed = new URL(String(rawUrl || ""));
    const path = parsed.pathname || "/";
    return parsed.protocol === "https:" && parsed.hostname === CHATGPT_HOST &&
      (path === "/" || path.startsWith("/c/") || path.startsWith("/g/"));
  } catch (_) {
    return false;
  }
}

function normalizeMcpPage(page) {
  if (!page || typeof page !== "object") return null;
  const rawId = page.pageId ?? page.id;
  const pageId = Number.isInteger(rawId) ? rawId :
    (typeof rawId === "string" && /^\d+$/.test(rawId) ? Number(rawId) : null);
  const url = typeof page.url === "string" ? page.url.split("?")[0].split("#")[0] : "";
  if (pageId === null || !url) return null;
  return {
    pageId,
    url,
    title: typeof page.title === "string" ? page.title : "",
    selected: page.selected === true,
  };
}

function normalizeStructuredPages(pages) {
  if (!Array.isArray(pages)) return null;
  const normalized = pages.map(normalizeMcpPage);
  return normalized.every(Boolean) ? normalized : null;
}

function safePageMetadata(value) {
  if (Array.isArray(value)) return normalizeStructuredPages(value) || [];
  if (!value || typeof value !== "object") return [];
  return normalizeStructuredPages(value.pages) || [];
}

function safeShapeKeys(value) {
  if (!value || typeof value !== "object") return [];
  return Object.keys(value)
    .filter(key => !/(cookie|token|secret|auth|authorization|credential|password|api.?key)/i.test(key))
    .slice(0, 20);
}

function parserDiagnostics(toolResult) {
  const structured = toolResult && typeof toolResult === "object" ? toolResult.structuredContent : undefined;
  const content = Array.isArray(toolResult?.content) ? toolResult.content : [];
  const structuredPages = structured && typeof structured === "object" ? structured.pages : undefined;
  const firstPage = Array.isArray(structuredPages) && structuredPages.length && structuredPages[0] && typeof structuredPages[0] === "object"
    ? structuredPages[0]
    : null;
  const contentTypes = content.map(item => String(item?.type || "unknown")).slice(0, 20);
  return {
    resultType: typeof toolResult,
    hasStructuredContent: Boolean(structured),
    structuredContentKeys: safeShapeKeys(structured),
    pagesIsArray: Array.isArray(structuredPages),
    pagesCount: Array.isArray(structuredPages) ? structuredPages.length : 0,
    firstPageKeys: safeShapeKeys(firstPage),
    contentTypes,
    // Backward-compatible aliases retained for existing Product Shell details.
    resultTypeof: typeof toolResult,
    structuredPagesIsArray: Array.isArray(structuredPages),
    structuredPagesCount: Array.isArray(structuredPages) ? structuredPages.length : 0,
    contentIsArray: Array.isArray(toolResult?.content),
    contentItemCount: content.length,
    contentItemTypes: contentTypes,
  };
}

function extractPageUrl(line) {
  const matches = [...String(line || "").matchAll(/(?:https?|chrome):\/\/[^\s)]+/gi)];
  if (!matches.length) return null;
  const match = matches[matches.length - 1];
  return {url: match[0].replace(/[.,;]+$/, ""), index: match.index};
}

function parseTextPages(text) {
  const pages = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    const idMatch = line.match(/^\s*(\d+)\s*:\s*/);
    const urlMatch = extractPageUrl(line);
    if (!idMatch || !urlMatch) continue;
    const beforeUrl = line.slice(idMatch[0].length, urlMatch.index).trim();
    const title = beforeUrl.replace(/\s*\($/, "").trim();
    pages.push({pageId: Number(idMatch[1]), url: urlMatch.url.split("?")[0].split("#")[0], title, selected: /\[selected\]/i.test(line)});
  }
  return pages.length ? pages : null;
}

function parserError(code, resultType, diagnostics, contentType, contentItemCount) {
  const error = new Error(code);
  error.code = code;
  error.resultType = resultType;
  error.diagnostics = diagnostics;
  error.contentType = contentType;
  error.contentItemCount = contentItemCount;
  return error;
}

function parseListPagesResult(toolResult) {
  const diagnostics = parserDiagnostics(toolResult);
  const structured = toolResult && typeof toolResult === "object" ? toolResult.structuredContent : undefined;
  const hasStructuredPages = Boolean(structured && typeof structured === "object" && "pages" in structured);
  if (hasStructuredPages) {
    if (!Array.isArray(structured.pages)) {
      throw parserError("MCP_STRUCTURED_PAGES_INVALID", "structuredContent.pages.invalid", diagnostics,
        diagnostics.contentTypes[0] || "none", diagnostics.contentItemCount);
    }
    const pages = normalizeStructuredPages(structured.pages);
    if (!pages) {
      throw parserError("MCP_STRUCTURED_PAGES_INVALID", "structuredContent.pages.invalid", diagnostics,
        diagnostics.contentTypes[0] || "none", diagnostics.contentItemCount);
    }
    return {
      pages,
      resultType: "structuredContent",
      contentType: diagnostics.contentItemTypes[0] || "none",
      contentItemCount: diagnostics.contentItemCount,
      diagnostics,
    };
  }

  // Parse only text payloads from MCP content blocks. Do not JSON.parse the
  // whole CallToolResult and do not retain page body or other fields.
  const content = Array.isArray(toolResult?.content) ? toolResult.content : [];
  for (const block of content.filter(item => item?.type === "text")) {
    if (typeof block?.text !== "string") continue;
    const pages = parseTextPages(block.text);
    if (pages) return {
      pages,
      resultType: "content.text",
      contentType: "text",
      contentItemCount: diagnostics.contentItemCount,
      diagnostics,
    };
  }
  if (content.some(item => item?.type === "text")) {
    throw parserError("MCP_TEXT_PAGES_PARSE_FAILED", "content.text.invalid", diagnostics,
      diagnostics.contentTypes[0] || "text", diagnostics.contentItemCount);
  }
  if (!structured || typeof structured !== "object" || !("pages" in structured)) {
    throw parserError("MCP_STRUCTURED_PAGES_MISSING", "structuredContent.pages.missing", diagnostics,
      diagnostics.contentTypes[0] || "none", diagnostics.contentItemCount);
  }
  throw parserError("MCP_LIST_PAGES_RESULT_PARSE_FAILED", "content.unrecognized", diagnostics,
    diagnostics.contentTypes[0] || "none", diagnostics.contentItemCount);
}

function readBooleanToolResult(value) {
  const directValues = [value?.structuredContent?.result, value?.result, value?.structuredContent];
  for (const candidate of directValues) {
    if (typeof candidate === "boolean") return candidate;
    if (candidate && typeof candidate === "object" && typeof candidate.result === "boolean") {
      return candidate.result;
    }
  }
  for (const block of Array.isArray(value?.content) ? value.content : []) {
    if (typeof block?.text !== "string") continue;
    try {
      const parsed = JSON.parse(block.text);
      if (typeof parsed === "boolean") return parsed;
      if (parsed && typeof parsed.result === "boolean") return parsed.result;
    } catch (_) {}
  }
  return false;
}

function sanitizeErrorMessage(error) {
  const message = String(error?.message || "").replace(/[\r\n\t]+/g, " ");
  return message
    .replace(/([?&](?:code|token|auth|authorization|cookie|key|secret|state)=)[^&\s]+/gi, "$1[REDACTED]")
    .replace(/\b(?:Bearer|Basic)\s+[^\s]+/gi, "[REDACTED]")
    .slice(0, 240) || "UNKNOWN";
}

function safeError(code, cause = null) {
  const error = new Error(code);
  error.code = code;
  if (cause) error.cause = cause;
  return error;
}

function classifyProcessError(error) {
  const message = String(error?.message || "");
  if (error?.code === "ENOENT" || message.includes("ENOENT") || message.includes("spawn npx")) {
    return "MCP_EXECUTABLE_NOT_FOUND";
  }
  return "MCP_PROCESS_START_FAILED";
}

function withTimeout(promise, timeoutMs, code) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(safeError(code)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

class ChromeDevToolsMcpClient {
  constructor({browserConnectionTimeoutMs = 60000, authorizationTimeoutMs = null, userApprovalTimeoutMs = null, onFailure = null} = {}) {
    this.browserConnectionTimeoutMs = browserConnectionTimeoutMs;
    // Compatibility aliases are accepted for callers from the experimental
    // autoConnect route, but do not alter the formal browserUrl command.
    if (authorizationTimeoutMs !== null) this.browserConnectionTimeoutMs = authorizationTimeoutMs;
    if (userApprovalTimeoutMs !== null) this.browserConnectionTimeoutMs = userApprovalTimeoutMs;
    this.onFailure = onFailure;
    this.client = null;
    this.transport = null;
    this.state = MCP_STATES.STOPPED;
    this.lastError = null;
    this.lastMcpError = null;
    this.lastMcpErrorStage = null;
    this.initializeState = "NOT_RUN";
    this.toolsListState = "NOT_RUN";
    this.listPagesState = "NOT_RUN";
    this.mcpProcessStarted = false;
    this.mcpProcessAlive = false;
    this.listPagesDispatched = false;
    this.listPagesCompleted = false;
    this.listPagesResultType = "NOT_RUN";
    this.listPagesDiagnostics = null;
    this.listPagesContentType = "NOT_RUN";
    this.listPagesContentItemCount = 0;
    this.listPagesPageCount = null;
    this.chatgptPageCount = 0;
    this.chatgptPageSelected = false;
    this.pages = [];
    this.target = null;
    this.composerDetected = false;
    this.composerCheckState = "NOT_RUN";
    this.firstBrowserToolCall = null;
    this.browserToolCallCount = 0;
    this._closed = false;
  }

  async _loadSdk() {
    try {
      const clientModule = await import("@modelcontextprotocol/sdk/client/index.js");
      const stdioModule = await import("@modelcontextprotocol/sdk/client/stdio.js");
      return {Client: clientModule.Client, StdioClientTransport: stdioModule.StdioClientTransport};
    } catch (error) {
      throw safeError("MCP_SDK_NOT_INSTALLED", error);
    }
  }

  _recordError(error, stage, fallbackCode = "UNKNOWN") {
    const code = String(error?.code || fallbackCode);
    this.lastError = code;
    this.lastMcpErrorStage = stage;
    this.lastMcpError = {
      name: String(error?.name || "Error"),
      code,
      message: sanitizeErrorMessage(error),
      stage,
    };
    return code;
  }

  _fail(code, state = MCP_STATES.ERROR, error = null, stage = null) {
    this._recordError(error || safeError(code), stage || this.state || "UNKNOWN", code);
    this.state = state;
    if (this.onFailure) this.onFailure(state, code);
  }

  async start() {
    if (this.client && !this._closed) return;
    this._closed = false;
    this.state = MCP_STATES.STARTING_MCP;
    let Client;
    let StdioClientTransport;
    try {
      ({Client, StdioClientTransport} = await this._loadSdk());
    } catch (error) {
      this._fail(error?.code || "MCP_SDK_NOT_INSTALLED", MCP_STATES.ERROR, error, "MCP_PROCESS_START");
      throw error;
    }
    try {
      this.client = new Client({name: "ai-meeting-room", version: "0.1.0"}, {capabilities: {}});
      this.transport = new StdioClientTransport({command: "npx", args: MCP_ARGS, stderr: "pipe"});
      this.mcpProcessStarted = true;
      this.mcpProcessAlive = true;
    } catch (error) {
      const code = classifyProcessError(error);
      this._fail(code, MCP_STATES.ERROR, error, "MCP_PROCESS_START");
      throw safeError(code, error);
    }
    this.transport.onclose = () => {
      this.mcpProcessAlive = false;
      if (!this._closed) this._fail("MCP_PROCESS_LOST", MCP_STATES.ERROR, null, "MCP_PROCESS");
    };
    this.transport.onerror = error => {
      this.mcpProcessAlive = false;
      if (!this._closed) this._fail("MCP_PROCESS_LOST", MCP_STATES.ERROR, error, "MCP_PROCESS");
    };
    this.state = MCP_STATES.INITIALIZING_MCP;
    try {
      await this.client.connect(this.transport);
      this.initializeState = "PASS";
    } catch (error) {
      this.initializeState = "FAILED";
      const code = error?.code === "ENOENT" ? "MCP_EXECUTABLE_NOT_FOUND" : "MCP_INITIALIZE_FAILED";
      this._fail(code, MCP_STATES.ERROR, error, "MCP_INITIALIZE");
      throw safeError(code, error);
    }
    this.state = MCP_STATES.DISCOVERING_TOOLS;
    try {
      const tools = await this.client.listTools();
      this.toolsListState = Array.isArray(tools?.tools) ? `PASS:${tools.tools.length}` : "PASS:0";
    } catch (error) {
      this.toolsListState = "FAILED";
      this._fail("MCP_TOOLS_LIST_FAILED", MCP_STATES.ERROR, error, "MCP_TOOLS_LIST");
      throw safeError("MCP_TOOLS_LIST_FAILED", error);
    }
  }

  async _callBrowserTool(name, args = {}, timeoutMs = this.browserConnectionTimeoutMs) {
    if (!this.client) throw safeError("MCP_PROCESS_NOT_STARTED");
    if (!BROWSER_TOOL_NAMES.has(name)) throw safeError("MCP_TOOL_NOT_ALLOWED");
    if (!this.firstBrowserToolCall) {
      if (name !== "list_pages") throw safeError("FIRST_BROWSER_TOOL_MUST_BE_LIST_PAGES");
      this.firstBrowserToolCall = name;
    }
    this.browserToolCallCount += 1;
    const request = this.client.callTool({name, arguments: args});
    if (name === "list_pages") this.listPagesDispatched = true;
    const timeoutCode = name === "list_pages" ? "MCP_LIST_PAGES_TIMEOUT" : "MCP_BROWSER_TOOL_TIMEOUT";
    return withTimeout(request, timeoutMs, timeoutCode);
  }

  async connectAndDiscover() {
    await this.start();
    this.state = MCP_STATES.CONNECTING_BROWSER;
    this.state = MCP_STATES.LISTING_PAGES;
    let result;
    try {
      result = await this._callBrowserTool("list_pages", {});
      this.listPagesCompleted = true;
      this.listPagesState = "PASS";
      const parsed = parseListPagesResult(result);
      this.listPagesResultType = parsed.resultType;
      this.listPagesContentType = parsed.contentType;
      this.listPagesContentItemCount = parsed.contentItemCount;
      this.listPagesDiagnostics = parsed.diagnostics;
      this.pages = parsed.pages;
      this.listPagesPageCount = this.pages.length;
    } catch (error) {
      this.listPagesState = "FAILED";
      this.listPagesResultType = String(error?.resultType || "ERROR");
      this.listPagesDiagnostics = error?.diagnostics || null;
      this.listPagesContentType = String(error?.contentType || "UNKNOWN");
      this.listPagesContentItemCount = Number(error?.contentItemCount || 0);
      let code = error?.code || "MCP_CONNECTION_REJECTED";
      if (String(error?.message || "").includes("9222") || String(error?.message || "").includes("ECONNREFUSED")) {
        code = "REMOTE_DEBUGGING_NOT_AVAILABLE";
      }
      this._fail(code, MCP_STATES.ERROR, error, "MCP_LIST_PAGES");
      throw safeError(code, error);
    }

    const candidates = this.pages.filter(page => isChatGPTUrl(page.url));
    this.chatgptPageCount = candidates.length;
    if (!candidates.length) {
      this._fail("CHATGPT_TARGET_NOT_FOUND", MCP_STATES.ERROR, null, "CHATGPT_TARGET");
      throw safeError("CHATGPT_TARGET_NOT_FOUND");
    }
    this.state = MCP_STATES.BINDING_CHATGPT;
    this.target = candidates[0];
    // Other page metadata is discarded without selection or content access.
    try {
      await this._callBrowserTool("select_page", {pageId: this.target.pageId});
      this.chatgptPageSelected = true;
    } catch (error) {
      this._fail("MCP_CONNECTION_REJECTED", MCP_STATES.ERROR, error, "CHATGPT_TARGET");
      throw safeError("MCP_CONNECTION_REJECTED", error);
    }

    this.state = MCP_STATES.CHECKING_COMPOSER;
    try {
      const composer = await this._callBrowserTool("evaluate_script", {
        function: "() => Boolean(document.querySelector('[data-testid=\\\"prompt-textarea\\\"], #prompt-textarea, textarea[placeholder], [contenteditable=\\\"true\\\"]'))",
      });
      this.composerDetected = readBooleanToolResult(composer);
      this.composerCheckState = this.composerDetected ? "PASS" : "FAIL";
    } catch (error) {
      this.composerCheckState = "FAILED";
      this._fail("COMPOSER_NOT_FOUND", MCP_STATES.ERROR, error, "COMPOSER_CHECK");
      throw safeError("COMPOSER_NOT_FOUND", error);
    }
    if (!this.composerDetected) {
      this._fail("COMPOSER_NOT_FOUND", MCP_STATES.ERROR, null, "COMPOSER_CHECK");
      throw safeError("COMPOSER_NOT_FOUND");
    }
    this.state = MCP_STATES.READY;
    return this.status();
  }

  async close() {
    this._closed = true;
    try { if (this.client) await this.client.close(); } catch (_) {}
    this.mcpProcessAlive = false;
    this.client = null;
    this.transport = null;
    this.state = MCP_STATES.STOPPED;
  }

  status() {
    return {
      state: this.state,
      browserRuntime: "USER_OWNED_REAL_CHROME_SESSION",
      mcpProcess: this.mcpProcessAlive,
      mcpProcessStarted: this.mcpProcessStarted,
      mcpCommand: `chrome-devtools-mcp@latest --browserUrl ${MCP_BROWSER_URL} ${MCP_STRUCTURED_CONTENT_FLAG}`,
      structuredContentFlag: MCP_STRUCTURED_CONTENT_FLAG,
      MCP_STRUCTURED_CONTENT_ENABLED: true,
      initialize: this.initializeState,
      toolsList: this.toolsListState,
      listPages: this.listPagesState,
      firstBrowserToolCall: this.firstBrowserToolCall,
      browserToolCallCount: this.browserToolCallCount,
      chatgptTarget: this.target ? {pageId: this.target.pageId, url: this.target.url} : null,
      composerDetected: this.composerDetected,
      mcpScopeCapability: "PROFILE_WIDE",
      applicationTabPolicy: "CHATGPT_ONLY",
      cookieReadByApp: "NONE",
      tokenReadByApp: "NONE",
      otherTabReadByApp: "NONE",
      MCP_PROCESS_STARTED: this.mcpProcessStarted,
      MCP_INITIALIZE: this.initializeState,
      MCP_TOOLS_LIST: this.toolsListState,
      MCP_LIST_PAGES_DISPATCHED: this.listPagesDispatched,
      MCP_LIST_PAGES_COMPLETED: this.listPagesCompleted,
      MCP_LIST_PAGES_RESULT_TYPE: this.listPagesResultType,
      MCP_LIST_PAGES_RESULT_DIAGNOSTICS: this.listPagesDiagnostics,
      MCP_LIST_PAGES_SHAPE_DIAGNOSTICS: this.listPagesDiagnostics,
      MCP_LIST_PAGES_PAGE_COUNT: this.listPagesPageCount,
      CHATGPT_PAGE_COUNT: this.chatgptPageCount,
      CHATGPT_PAGE_SELECTED: this.chatgptPageSelected,
      COMPOSER_CHECK: this.composerCheckState,
      LAST_MCP_ERROR_CODE: this.lastMcpError?.code || this.lastError,
      LAST_MCP_ERROR_STAGE: this.lastMcpErrorStage,
      lastMcpError: this.lastMcpError,
      mcpBrowserUrl: MCP_BROWSER_URL,
      lastError: this.lastError,
    };
  }
}

module.exports = {
  AUTOCONNECT_ARGS,
  MCP_PACKAGE,
  MCP_BROWSER_URL,
  MCP_STRUCTURED_CONTENT_FLAG,
  MCP_ARGS,
  MCP_STATES,
  ChatGPTDevToolsMcpClient: ChromeDevToolsMcpClient,
  ChromeDevToolsMcpClient,
  isChatGPTUrl,
  normalizeMcpPage,
  parseListPagesResult,
  readBooleanToolResult,
  safePageMetadata,
  sanitizeErrorMessage,
};
