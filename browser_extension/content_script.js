// ChatGPTDomAdapter: semantic/ARIA DOM access only; no auth storage access.
const ChatGPTDomAdapter = {
  conversationId() {
    const match = /^https:\/\/(?:chatgpt\.com|chat\.openai\.com)\/c\/([A-Za-z0-9-]+)/.exec(location.href);
    return match ? match[1] : null;
  },
  composer() {
    return document.querySelector('[role="textbox"][contenteditable="true"]') || document.querySelector('textarea[placeholder]');
  },
  composerText(node) { return (node?.innerText || node?.value || '').trim(); },
  isDisabled(node) {
    return Boolean(node?.disabled || node?.getAttribute('aria-disabled') === 'true' || node?.getAttribute('data-disabled') === 'true');
  },
  metadata(node) {
    if (!node) return null;
    return {
      tag: node.tagName?.toLowerCase() || '',
      role: node.getAttribute('role') || '',
      ariaLabel: node.getAttribute('aria-label') || '',
      title: node.getAttribute('title') || '',
      dataTestId: node.getAttribute('data-testid') || '',
      contenteditable: node.getAttribute('contenteditable') || '',
      disabled: this.isDisabled(node),
    };
  },
  form(node) { return node?.closest?.('form') || null; },
  nearbyButtons(node) {
    const roots = [this.form(node), node?.parentElement, node?.parentElement?.parentElement].filter(Boolean);
    const seen = new Set();
    const buttons = [];
    for (const root of roots) {
      for (const button of root.querySelectorAll('button, input[type="submit"]')) {
        if (!seen.has(button)) { seen.add(button); buttons.push(button); }
      }
    }
    return buttons;
  },
  sendControl(node) {
    const form = this.form(node);
    const byTestId = [...document.querySelectorAll('[data-testid="send-button"], [data-testid="send-message-button"], [data-testid="composer-submit-button"]')];
    const byRole = [...document.querySelectorAll('button')].filter(button => {
      const name = `${button.getAttribute('aria-label') || ''} ${button.getAttribute('title') || ''}`;
      return /(?:^|\s)(send|发送)(?:\s|$)/i.test(name.trim()) || /^(send|发送)/i.test(name.trim());
    });
    const inForm = form ? [...form.querySelectorAll('button[type="submit"], input[type="submit"]')] : [];
    const candidates = [...byTestId, ...byRole, ...inForm].filter((item, index, all) => all.indexOf(item) === index);
    const usable = candidates.find(button => !this.isDisabled(button));
    if (usable) return {kind: 'button', element: usable, strategy: usable.getAttribute('data-testid') ? 'data-testid' : 'accessible-role'};
    const disabled = candidates.find(button => this.isDisabled(button));
    if (disabled) return {kind: 'disabled', element: disabled, strategy: 'send-control-disabled'};
    if (form && typeof form.requestSubmit === 'function') return {kind: 'form', element: form, strategy: 'form-requestSubmit'};
    return null;
  },
  generating() {
    return [...document.querySelectorAll('button')].some(button => {
      const name = `${button.getAttribute('aria-label') || ''} ${button.getAttribute('title') || ''}`;
      return /stop generating|停止生成/i.test(name);
    });
  },
  visibleModal() {
    return [...document.querySelectorAll('[role="dialog"]')].some(node => node.getClientRects?.().length);
  },
  enterFallbackAllowed(node) {
    return Boolean(node && document.activeElement === node && !node.__aimrComposing && !this.visibleModal() && !this.generating());
  },
  health() {
    const composer = this.composer();
    return {authState: composer ? 'AUTHENTICATED' : 'UNKNOWN', domRecognized: Boolean(composer && this.conversationId()), conversationBindingId: this.conversationId()};
  },
  diagnostics() {
    const composer = this.composer();
    const form = this.form(composer);
    const control = composer ? this.sendControl(composer) : null;
    return {
      type: 'CHATGPT_DOM_DIAGNOSTICS',
      conversationBindingId: this.conversationId(),
      composer: this.metadata(composer),
      composerInForm: Boolean(form),
      nearbyButtons: this.nearbyButtons(composer).map(button => this.metadata(button)),
      sendControl: control ? {strategy: control.strategy, disabled: control.kind === 'disabled'} : null,
      activeElementIsComposer: Boolean(composer && document.activeElement === composer),
      generating: this.generating(),
      modalVisible: this.visibleModal(),
    };
  },
};

function inputEvent(type, data) {
  try { return new InputEvent(type, {bubbles: true, cancelable: type === 'beforeinput', inputType: 'insertText', data}); }
  catch (_) { return new Event(type, {bubbles: true, cancelable: type === 'beforeinput'}); }
}

function writePrompt(composer, prompt) {
  composer.focus();
  composer.dispatchEvent(inputEvent('beforeinput', prompt));
  if ('value' in composer) {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    if (!setter) throw new Error('COMPOSER_WRITE_FAILED');
    setter.call(composer, prompt);
  } else {
    const changed = document.execCommand('insertText', false, prompt);
    if (!changed) throw new Error('COMPOSER_WRITE_FAILED');
  }
  composer.dispatchEvent(inputEvent('input', prompt));
}

function installCompositionTracking(composer) {
  if (composer.__aimrTrackingInstalled) return;
  composer.__aimrTrackingInstalled = true;
  composer.__aimrComposing = false;
  composer.addEventListener('compositionstart', () => { composer.__aimrComposing = true; });
  composer.addEventListener('compositionend', () => { composer.__aimrComposing = false; });
}

async function waitForSendConfirmation(composer, beforeUserCount) {
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    const userCount = document.querySelectorAll('[data-message-author-role="user"]').length;
    if (!ChatGPTDomAdapter.composerText(composer) || userCount > beforeUserCount || ChatGPTDomAdapter.generating()) return true;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return false;
}

async function executeBrainRequest(request) {
  const composer = ChatGPTDomAdapter.composer();
  const currentConversation = ChatGPTDomAdapter.conversationId();
  if (!composer || !currentConversation) throw new Error('COMPOSER_NOT_FOUND');
  if (request.conversationBindingId && currentConversation !== request.conversationBindingId) throw new Error('CONVERSATION_BINDING_MISMATCH');
  installCompositionTracking(composer);
  if (ChatGPTDomAdapter.generating() || ChatGPTDomAdapter.composerText(composer)) throw new Error('COMPOSER_BUSY');
  const beforeUserCount = document.querySelectorAll('[data-message-author-role="user"]').length;
  const beforeAssistant = new Set([...document.querySelectorAll('[data-message-author-role="assistant"]')]);
  try { writePrompt(composer, request.prompt); } catch (error) { throw new Error(error.message || 'COMPOSER_WRITE_FAILED'); }
  if (ChatGPTDomAdapter.composerText(composer) !== request.prompt) throw new Error('COMPOSER_WRITE_FAILED');
  const control = ChatGPTDomAdapter.sendControl(composer);
  if (control?.kind === 'disabled') throw new Error('SEND_CONTROL_DISABLED');
  try {
    if (control?.kind === 'button') control.element.click();
    else if (control?.kind === 'form') control.element.requestSubmit();
    else if (ChatGPTDomAdapter.enterFallbackAllowed(composer)) {
      composer.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true, shiftKey: false, altKey: false, ctrlKey: false, metaKey: false}));
      composer.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true, shiftKey: false, altKey: false, ctrlKey: false, metaKey: false}));
    } else throw new Error('SEND_CONTROL_UNKNOWN');
  } catch (error) {
    if (error.message === 'SEND_CONTROL_UNKNOWN') throw error;
    throw new Error('SEND_ACTION_FAILED');
  }
  if (!(await waitForSendConfirmation(composer, beforeUserCount))) throw new Error('SEND_NOT_CONFIRMED');
  const deadline = Date.now() + 180000;
  let last = '';
  let stableSince = 0;
  while (Date.now() < deadline) {
    const candidates = [...document.querySelectorAll('[data-message-author-role="assistant"]')].filter(node => !beforeAssistant.has(node));
    const response = candidates.at(-1)?.innerText?.trim() || '';
    if (response && response === last && !ChatGPTDomAdapter.generating()) {
      if (!stableSince) stableSince = Date.now();
      if (Date.now() - stableSince >= 1000) return {rawResponse: response};
    } else { stableSince = 0; last = response; }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error('RESPONSE_TIMEOUT_OR_UNKNOWN');
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === 'HEALTH_CHECK') { sendResponse(ChatGPTDomAdapter.health()); return false; }
  if (message.type === 'DOM_DIAGNOSTICS') { sendResponse(ChatGPTDomAdapter.diagnostics()); return false; }
  if (message.type === 'BRAIN_REQUEST') {
    executeBrainRequest(message.request).then(sendResponse).catch(error => sendResponse({rawResponse: '', error: error.message}));
    return true;
  }
  return false;
});
