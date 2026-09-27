const {contextBridge} = require('electron');

let authDiagnosticsEnabled = process.argv.some(argument => (
  argument === '--aimr-auth-diagnostics' ||
  argument === 'aimr-auth-diagnostics' ||
  argument.startsWith('--aimr-auth-diagnostics=')
));
const nextButtonLabels = /^(next|下一步)$/i;
let authDiagnostics = {
  enabled: authDiagnosticsEnabled,
  targetFound: false,
  button: null,
  clickTarget: null,
  overlayBlocked: false,
  pointerdown: false,
  mousedown: false,
  mouseup: false,
  click: false,
  formSubmit: false,
};
let diagnosticsInstalled = false;
let desktopRequest = null;

function visibleLabel(element) {
  return String(element.getAttribute('aria-label') || element.textContent || '')
    .trim().replace(/\s+/g, ' ');
}

function findNextButton() {
  const candidates = document.querySelectorAll('button, [role="button"], input[type="submit"]');
  for (const candidate of candidates) {
    if (nextButtonLabels.test(visibleLabel(candidate))) return candidate;
  }
  return null;
}

function elementDescription(element) {
  if (!element) return null;
  const style = getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return {
    tag: element.tagName,
    type: element.getAttribute('type'),
    role: element.getAttribute('role'),
    disabled: Boolean(element.disabled),
    ariaDisabled: element.getAttribute('aria-disabled'),
    pointerEvents: style.pointerEvents,
    display: style.display,
    visibility: style.visibility,
    boundingClientRect: {
      x: Math.round(rect.x), y: Math.round(rect.y),
      width: Math.round(rect.width), height: Math.round(rect.height),
    },
  };
}

function refreshAuthDiagnostics() {
  if (!authDiagnosticsEnabled || !document.body) return;
  const button = findNextButton();
  authDiagnostics.targetFound = Boolean(button);
  authDiagnostics.button = elementDescription(button);
  authDiagnostics.clickTarget = null;
  authDiagnostics.overlayBlocked = false;
  if (!button) return;
  const rect = button.getBoundingClientRect();
  const x = Math.round(rect.left + (rect.width / 2));
  const y = Math.round(rect.top + (rect.height / 2));
  const topElement = document.elementFromPoint(x, y);
  authDiagnostics.overlayBlocked = Boolean(topElement && topElement !== button && !button.contains(topElement));
  authDiagnostics.clickTarget = elementDescription(topElement);
}

function eventIsInNextButtonArea(event) {
  const button = findNextButton();
  if (!button) return false;
  const rect = button.getBoundingClientRect();
  return button.contains(event.target) || (
    event.clientX >= rect.left && event.clientX <= rect.right &&
    event.clientY >= rect.top && event.clientY <= rect.bottom
  );
}

function installAuthDiagnostics() {
  if (!authDiagnosticsEnabled || diagnosticsInstalled) return;
  diagnosticsInstalled = true;
  for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
    document.addEventListener(eventName, event => {
      if (eventIsInNextButtonArea(event)) {
        authDiagnostics[eventName] = true;
        authDiagnostics.clickTarget = elementDescription(event.target);
      }
    }, true);
  }
  document.addEventListener('submit', event => {
    const button = findNextButton();
    if (button && (button.form === event.target || button.closest('form') === event.target)) {
      authDiagnostics.formSubmit = true;
    }
  }, true);
  const observer = new MutationObserver(() => refreshAuthDiagnostics());
  observer.observe(document.documentElement, {subtree: true, childList: true, attributes: true});
  refreshAuthDiagnostics();
}

function conversationId() {
  const match = /^https:\/\/(?:chatgpt\.com|chat\.openai\.com)\/c\/([A-Za-z0-9-]+)/.exec(location.href);
  return match ? match[1] : null;
}

function challengeDetected() {
  const urlSignal = /challenges\.cloudflare\.com|challenge-platform/i.test(location.hostname);
  const titleSignal = /cloudflare|verify you are human|checking your browser|human verification/i.test(document.title || '');
  const signalNodes = document.querySelectorAll('h1, h2, [role="heading"], [role="alert"], [aria-label*="human" i], [aria-label*="browser" i]');
  const visibleSignals = [...signalNodes].slice(0, 20).map(node => `${node.getAttribute('aria-label') || ''} ${node.textContent || ''}`.slice(0, 240)).join(' ');
  const bodySignal = /cloudflare|verify you are human|checking your browser|human verification|验证你是人类|正在检查浏览器/i.test(visibleSignals);
  return Boolean(urlSignal || titleSignal || bodySignal);
}

function loginPageDetected() {
  return /\/(?:auth\/)?login(?:\/|$)/i.test(location.pathname || '') ||
    /sign in|log in|登录|登入/i.test(document.title || '');
}

function health() {
  installAuthDiagnostics();
  const composer = document.querySelector('[role="textbox"][contenteditable="true"]') || document.querySelector('textarea[placeholder]');
  const challenge = challengeDetected();
  const loading = document.readyState !== 'complete';
  const login = loginPageDetected();
  const authState = challenge ? 'CHALLENGE_REQUIRED' :
    (loading ? 'LOADING' : (login ? 'AUTH_REQUIRED' : (composer ? 'AUTHENTICATED' : 'DOM_UNKNOWN')));
  return {
    authState,
    challengeDetected: challenge,
    domRecognized: Boolean(composer) && !challenge,
    conversationBindingId: conversationId(),
    pageReady: !loading,
  };
}

function composer() {
  return document.querySelector('[role="textbox"][contenteditable="true"]') || document.querySelector('textarea[placeholder]');
}

function composerText(node) {
  return String(node?.innerText || node?.value || '').trim();
}

function isDisabled(node) {
  return Boolean(node?.disabled || node?.getAttribute('aria-disabled') === 'true' || node?.getAttribute('data-disabled') === 'true');
}

function generating() {
  return [...document.querySelectorAll('button')].some(button => {
    const name = `${button.getAttribute('aria-label') || ''} ${button.getAttribute('title') || ''}`;
    return /stop generating|停止生成/i.test(name) && !isDisabled(button);
  });
}

function visibleModal() {
  return [...document.querySelectorAll('[role="dialog"]')].some(node => node.getClientRects?.().length);
}

function sendControl(node) {
  const form = node?.closest?.('form');
  const byTestId = [...document.querySelectorAll('[data-testid="send-button"], [data-testid="send-message-button"], [data-testid="composer-submit-button"]')];
  const byRole = [...document.querySelectorAll('button')].filter(button => {
    const name = `${button.getAttribute('aria-label') || ''} ${button.getAttribute('title') || ''}`;
    return /(?:^|\s)(send|发送|send prompt)(?:\s|$)/i.test(name.trim()) || /^(send|发送)/i.test(name.trim());
  });
  const inForm = form ? [...form.querySelectorAll('button[type="submit"], input[type="submit"]')] : [];
  const candidates = [...byTestId, ...byRole, ...inForm].filter((item, index, all) => all.indexOf(item) === index);
  const usable = candidates.find(button => !isDisabled(button));
  if (usable) return {kind: 'button', element: usable, strategy: usable.getAttribute('data-testid') ? 'data-testid' : 'accessible-role'};
  const disabled = candidates.find(button => isDisabled(button));
  if (disabled) return {kind: 'disabled', element: disabled, strategy: 'send-control-disabled'};
  if (form && typeof form.requestSubmit === 'function') return {kind: 'form', element: form, strategy: 'form-requestSubmit'};
  return null;
}

function inputEvent(type, data) {
  try { return new InputEvent(type, {bubbles: true, cancelable: type === 'beforeinput', inputType: 'insertText', data}); }
  catch (_) { return new Event(type, {bubbles: true, cancelable: type === 'beforeinput'}); }
}

function writePrompt(node, prompt) {
  node.focus();
  node.dispatchEvent(inputEvent('beforeinput', prompt));
  if ('value' in node) {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    if (!setter) throw new Error('COMPOSER_WRITE_FAILED');
    setter.call(node, prompt);
  } else {
    const changed = document.execCommand('insertText', false, prompt);
    if (!changed) throw new Error('COMPOSER_WRITE_FAILED');
  }
  node.dispatchEvent(inputEvent('input', prompt));
}

function prepareDesktopRequest(value) {
  const node = composer();
  if (!node) return {ok: false, error: 'COMPOSER_NOT_FOUND'};
  if (generating() || composerText(node)) return {ok: false, error: 'COMPOSER_BUSY'};
  desktopRequest = {
    brainRequestId: String(value?.brainRequestId || ''),
    taskId: String(value?.taskId || ''),
    baselineUserCount: document.querySelectorAll('[data-message-author-role="user"]').length,
    baselineAssistantCount: document.querySelectorAll('[data-message-author-role="assistant"]').length,
    sent: false,
    prompt: null,
    lastResponse: '',
    stableSince: 0,
  };
  if (!desktopRequest.brainRequestId || !desktopRequest.taskId) return {ok: false, error: 'REQUEST_INVALID'};
  return {ok: true, baselineUserCount: desktopRequest.baselineUserCount, baselineAssistantCount: desktopRequest.baselineAssistantCount};
}

function writeDesktopPrompt(value) {
  if (!desktopRequest) return {ok: false, error: 'REQUEST_NOT_PREPARED'};
  const node = composer();
  if (!node) return {ok: false, error: 'COMPOSER_NOT_FOUND'};
  if (generating() || composerText(node)) return {ok: false, error: 'COMPOSER_BUSY'};
  const prompt = String(value?.prompt || '');
  try { writePrompt(node, prompt); }
  catch (error) { return {ok: false, error: error.message || 'COMPOSER_WRITE_FAILED'}; }
  if (composerText(node) !== prompt) return {ok: false, error: 'COMPOSER_WRITE_FAILED'};
  desktopRequest.prompt = prompt;
  return {ok: true, composerWrite: true};
}

function sendDesktopPrompt() {
  if (!desktopRequest || desktopRequest.prompt === null) return {ok: false, error: 'PROMPT_NOT_WRITTEN'};
  const node = composer();
  if (!node) return {ok: false, error: 'COMPOSER_NOT_FOUND'};
  if (generating() || composerText(node) !== desktopRequest.prompt) return {ok: false, error: 'COMPOSER_BUSY'};
  const control = sendControl(node);
  if (control?.kind === 'disabled') return {ok: false, error: 'SEND_CONTROL_DISABLED'};
  try {
    if (control?.kind === 'button') control.element.click();
    else if (control?.kind === 'form') control.element.requestSubmit();
    else if (document.activeElement === node && !visibleModal() && !generating()) {
      node.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true}));
      node.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true}));
    } else return {ok: false, error: 'SEND_CONTROL_UNKNOWN'};
  } catch (_) { return {ok: false, error: 'SEND_FAILED'}; }
  desktopRequest.sent = true;
  return {ok: true, composerWrite: true, send: true, strategy: control?.strategy || 'enter-fallback'};
}

function desktopResponseState() {
  if (!desktopRequest || !desktopRequest.sent) return {completed: false, error: 'REQUEST_NOT_SENT'};
  const node = composer();
  const userCount = document.querySelectorAll('[data-message-author-role="user"]').length;
  const assistantNodes = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
  const responseNode = assistantNodes.length > desktopRequest.baselineAssistantCount ? assistantNodes.at(-1) : null;
  const rawResponse = responseNode?.innerText?.trim() || '';
  const isGenerating = generating();
  const sendConfirmed = Boolean(node && (!composerText(node) || userCount > desktopRequest.baselineUserCount || isGenerating || responseNode));
  if (!sendConfirmed) return {completed: false, sendConfirmed: false, generating: isGenerating, responseBoundary: false};
  if (rawResponse && rawResponse === desktopRequest.lastResponse && !isGenerating) {
    if (!desktopRequest.stableSince) desktopRequest.stableSince = Date.now();
  } else {
    desktopRequest.stableSince = 0;
    desktopRequest.lastResponse = rawResponse;
  }
  const completed = Boolean(rawResponse && !isGenerating && desktopRequest.stableSince && Date.now() - desktopRequest.stableSince >= 1000);
  return {
    completed,
    sendConfirmed,
    generating: isGenerating,
    responseBoundary: Boolean(responseNode),
    rawResponse: completed ? rawResponse : null,
  };
}

function diagnosticState() {
  authDiagnostics.enabled = authDiagnosticsEnabled;
  installAuthDiagnostics();
  refreshAuthDiagnostics();
  return {...authDiagnostics};
}

function enableAuthDiagnostics() {
  authDiagnosticsEnabled = true;
  installAuthDiagnostics();
  refreshAuthDiagnostics();
  return true;
}

// Deliberately expose no Node, filesystem, cookie, storage, or auth API to
// chatgpt.com. Prompt/response transport is a later, explicit POC boundary.
contextBridge.exposeInMainWorld('aimrBrainHost', Object.freeze({
  hostVersion: '0.1.0-poc',
  health,
  authDiagnostics: diagnosticState,
  enableAuthDiagnostics,
  prepareDesktopRequest,
  writeDesktopPrompt,
  sendDesktopPrompt,
  desktopResponseState,
}));
