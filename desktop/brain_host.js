const {URL} = require('node:url');
const {AUTH_RECOVERY_WAIT_MS} = require('./runtime_config');

const BRAIN_STATES = Object.freeze([
  'DISCONNECTED', 'STARTING', 'LOADING', 'AUTH_REQUIRED', 'AUTH_LOST',
  'CHALLENGE_REQUIRED', 'READY', 'THINKING', 'WAITING_RESPONSE', 'PARSING',
  'ERROR', 'DOM_UNKNOWN', 'UNKNOWN',
]);
const BRAIN_STATE_LABELS = Object.freeze({
  DISCONNECTED: '未连接', STARTING: '正在启动', LOADING: '正在恢复 GPT 主脑……',
  AUTH_REQUIRED: '需要登录', AUTH_LOST: '登录状态已失效',
  CHALLENGE_REQUIRED: '需要人工验证',
  READY: '已就绪', THINKING: '思考中', WAITING_RESPONSE: '等待回复',
  PARSING: '解析中', ERROR: '错误', DOM_UNKNOWN: '页面结构未知', UNKNOWN: '未知',
});

const ALLOWED_ORIGINS = new Set([
  'https://chatgpt.com',
  'https://chat.openai.com',
  'https://auth.openai.com',
  'https://accounts.openai.com',
]);

// These are exact, official identity-provider origins used by the supported
// login buttons. They are only usable in a separately controlled auth window.
const OAUTH_ORIGINS = new Set([
  'https://accounts.google.com',
  'https://login.live.com',
  'https://login.microsoftonline.com',
  'https://appleid.apple.com',
]);

function safeUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    return `${url.origin}${url.pathname}`;
  } catch (_) {
    return null;
  }
}

function allowedOriginUrl(rawUrl, origins) {
  try {
    const url = new URL(rawUrl);
    return url.protocol === 'https:' && origins.has(url.origin);
  } catch (_) {
    return false;
  }
}

function allowedUrl(rawUrl) {
  return allowedOriginUrl(rawUrl, ALLOWED_ORIGINS);
}

function allowedAuthUrl(rawUrl) {
  return allowedUrl(rawUrl) || allowedOriginUrl(rawUrl, OAUTH_ORIGINS);
}

function diagnosticCategory(message) {
  const value = String(message || '').toLowerCase();
  if (/csp|content-security-policy/.test(value)) return 'csp';
  if (/cookie|storage|same.?site/.test(value)) return 'cookie';
  if (/oauth|authorize|authorization|login|sso/.test(value)) return 'oauth';
  if (/navigation|redirect|blocked|frame/.test(value)) return 'navigation';
  if (/network|fetch|failed to load|net::/.test(value)) return 'network';
  if (/javascript|exception|undefined|typeerror|syntaxerror/.test(value)) return 'javascript';
  return 'unknown';
}

class ChatGPTDesktopBrainHost {
  constructor(win, {logger = console.log, onFailure = null} = {}) {
    this.win = win;
    this.logger = logger;
    this.state = 'STARTING';
    this.authState = 'LOADING';
    this.domRecognized = false;
    this.conversationBindingId = null;
    this.lastError = null;
    this.onFailure = onFailure;
    this._lastFailureSignature = null;
    this._lastAuthDiagnosticSignature = null;
    this._lastAuthHealthSignature = null;
    this._loadingUntil = Date.now() + AUTH_RECOVERY_WAIT_MS;
    this._wasAuthenticated = false;
    this._healthTimer = setInterval(() => this.refreshDomHealth(), 2000);
    this._wire();
  }

  _wire() {
    const contents = this.win.webContents;
    contents.on('did-start-loading', () => {
      this.logger('[brain-diagnostic] did-start-loading');
      this.authState = 'LOADING';
      this._loadingUntil = Date.now() + AUTH_RECOVERY_WAIT_MS;
      this._setState('STARTING');
    });
    contents.on('did-start-navigation', (_event, url, _isInPlace, _isMainFrame) => {
      this.logger(`[brain-diagnostic] did-start-navigation url=${safeUrl(url) || 'INVALID'}`);
    });
    contents.on('will-redirect', (_event, url) => {
      this.logger(`[brain-diagnostic] will-redirect url=${safeUrl(url) || 'INVALID'}`);
    });
    contents.on('did-finish-load', () => {
      this.logger(`[brain-diagnostic] did-finish-load url=${safeUrl(contents.getURL()) || 'INVALID'}`);
      this.authState = 'LOADING';
      this._loadingUntil = Date.now() + AUTH_RECOVERY_WAIT_MS;
      this.refreshDomHealth();
    });
    contents.on('dom-ready', () => {
      this.logger(`[brain-diagnostic] dom-ready url=${safeUrl(contents.getURL()) || 'INVALID'}`);
    });
    contents.on('console-message', (_event, level, message, line, sourceId) => {
      if (level < 2) return;
      this.logger(`[brain-diagnostic] console level=${level} category=${diagnosticCategory(message)} line=${line} source=${safeUrl(sourceId) || 'NON_URL'}`);
    });
    contents.on('did-fail-load', (_event, errorCode, _errorDescription, validatedURL) => {
      if (errorCode === -3) return;
      this.lastError = `navigation:${errorCode}`;
      this.logger(`[brain-diagnostic] did-fail-load code=${errorCode} url=${safeUrl(validatedURL) || 'INVALID'}`);
      this._setState('ERROR');
      if (validatedURL && !allowedUrl(validatedURL)) this._stopNavigation();
    });
    contents.on('render-process-gone', (_event, details) => {
      this.lastError = `render-process-gone:${details.reason}`;
      this.logger(`[brain-diagnostic] render-process-gone reason=${details.reason}`);
      this._setState('UNKNOWN');
    });
    contents.on('destroyed', () => {
      clearInterval(this._healthTimer);
      this._setState('DISCONNECTED');
    });
  }

  _stopNavigation() {
    if (!this.win.isDestroyed() && !this.win.webContents.isDestroyed()) this.win.webContents.stop();
  }

  setOperationalState(state) {
    this._setState(state);
  }

  executeJavaScript(expression) {
    if (this.win.isDestroyed() || this.win.webContents.isDestroyed()) {
      return Promise.reject(new Error('WEB_CONTENTS_LOST'));
    }
    return this.win.webContents.executeJavaScript(expression, true);
  }

  _setState(state) {
    if (!BRAIN_STATES.includes(state)) state = 'UNKNOWN';
    this.state = state;
    if (this.win && !this.win.isDestroyed()) this.win.setTitle(`ChatGPT 主脑 — ${BRAIN_STATE_LABELS[state] || '未知'}`);
    if (['AUTH_REQUIRED', 'AUTH_LOST', 'CHALLENGE_REQUIRED', 'ERROR', 'DOM_UNKNOWN', 'UNKNOWN'].includes(state)) this._notifyFailure(state);
  }

  refreshDomHealth() {
    if (this.win.isDestroyed() || this.win.webContents.isDestroyed()) return;
    this.win.webContents.executeJavaScript(
      '(() => { const h = window.aimrBrainHost; return h && h.health ? {health: h.health(), diagnostics: h.authDiagnostics ? h.authDiagnostics() : null} : null; })()',
      true,
    ).then(result => {
      if (!result) {
        if (Date.now() < this._loadingUntil) this._setState('LOADING');
        else this.markAuthState('DOM_UNKNOWN', false, null);
        return;
      }
      const health = result.health || result;
      this.markAuthState(health.authState, health.domRecognized, health.conversationBindingId, health.challengeDetected);
      this._logAuthDiagnostics(result.diagnostics);
    }).catch(() => {
      this.lastError = 'dom-health-check-failed';
      this._setState('UNKNOWN');
    });
  }

  _logAuthDiagnostics(diagnostics) {
    if (!diagnostics) {
      if (this._lastAuthDiagnosticSignature === 'UNAVAILABLE') return;
      this._lastAuthDiagnosticSignature = 'UNAVAILABLE';
      this.logger('[brain-diagnostic] next-button diagnostics=UNAVAILABLE');
      return;
    }
    if (!diagnostics.enabled) {
      if (this._lastAuthDiagnosticSignature === 'DISABLED') return;
      this._lastAuthDiagnosticSignature = 'DISABLED';
      this.logger('[brain-diagnostic] next-button diagnostics=DISABLED');
      return;
    }
    const summary = {
      targetFound: Boolean(diagnostics.targetFound),
      buttonTag: diagnostics.button && diagnostics.button.tag,
      buttonType: diagnostics.button && diagnostics.button.type,
      buttonRole: diagnostics.button && diagnostics.button.role,
      buttonDisabled: diagnostics.button && diagnostics.button.disabled,
      ariaDisabled: diagnostics.button && diagnostics.button.ariaDisabled,
      pointerEvents: diagnostics.button && diagnostics.button.pointerEvents,
      clickTargetTag: diagnostics.clickTarget && diagnostics.clickTarget.tag,
      clickTargetRole: diagnostics.clickTarget && diagnostics.clickTarget.role,
      overlayBlocked: Boolean(diagnostics.overlayBlocked),
      pointerdown: Boolean(diagnostics.pointerdown),
      mousedown: Boolean(diagnostics.mousedown),
      mouseup: Boolean(diagnostics.mouseup),
      click: Boolean(diagnostics.click),
      formSubmit: Boolean(diagnostics.formSubmit),
    };
    const signature = JSON.stringify(summary);
    if (signature === this._lastAuthDiagnosticSignature) return;
    this._lastAuthDiagnosticSignature = signature;
    this.logger(`[brain-diagnostic] next-button ${signature}`);
  }

  navigationAllowed(url) { return allowedUrl(url); }

  markAuthState(authState, domRecognized = false, conversationBindingId = null) {
    const allowedStates = ['AUTHENTICATED', 'AUTH_REQUIRED', 'AUTH_LOST', 'CHALLENGE_REQUIRED', 'LOADING', 'DOM_UNKNOWN', 'UNKNOWN'];
    this.authState = allowedStates.includes(authState) ? authState : 'DOM_UNKNOWN';
    this.domRecognized = Boolean(domRecognized);
    this.conversationBindingId = conversationBindingId || null;
    const signature = `${this.authState}:${this.domRecognized}`;
    if (signature !== this._lastAuthHealthSignature) {
      this._lastAuthHealthSignature = signature;
      this.logger(`[brain-diagnostic] auth-health authState=${this.authState} domRecognized=${this.domRecognized}`);
    }
    if (this.authState === 'LOADING') this._setState('LOADING');
    else if (this.authState === 'CHALLENGE_REQUIRED') this._setState('CHALLENGE_REQUIRED');
    else if (this.authState === 'AUTH_REQUIRED') this._setState('AUTH_REQUIRED');
    else if (this.authState === 'AUTH_LOST') this._setState('AUTH_LOST');
    else if (this.authState === 'AUTHENTICATED' && this.domRecognized && !['THINKING', 'WAITING_RESPONSE', 'PARSING'].includes(this.state)) this._setState('READY');
    else if (this.authState === 'DOM_UNKNOWN' || this.authState === 'UNKNOWN') {
      if (Date.now() < this._loadingUntil) this._setState('LOADING');
      else if (!['THINKING', 'WAITING_RESPONSE', 'PARSING'].includes(this.state)) this._setState('DOM_UNKNOWN');
    }
    if (this.authState === 'AUTHENTICATED' && this.domRecognized) {
      this._wasAuthenticated = true;
      this._lastFailureSignature = null;
    } else if (this.authState !== 'LOADING') this._notifyFailure(this.authState);
  }

  _notifyFailure(reason) {
    const signature = `${this.state}:${reason}`;
    if (signature === this._lastFailureSignature || typeof this.onFailure !== 'function') return;
    this._lastFailureSignature = signature;
    try { this.onFailure({state: this.state, reason}); } catch (_) { /* safety notification is best-effort */ }
  }

  status() {
    return {
      state: this.state,
      authState: this.authState,
      domRecognized: this.domRecognized,
      conversationBindingId: this.conversationBindingId,
      webContentsAlive: !this.win.isDestroyed() && !this.win.webContents.isDestroyed(),
      url: this.win.isDestroyed() ? null : safeUrl(this.win.webContents.getURL()),
      lastError: this.lastError,
    };
  }
}

module.exports = {
  ChatGPTDesktopBrainHost,
  allowedUrl,
  allowedAuthUrl,
  safeUrl,
  ALLOWED_ORIGINS,
  OAUTH_ORIGINS,
  BRAIN_STATES,
  BRAIN_STATE_LABELS,
};
