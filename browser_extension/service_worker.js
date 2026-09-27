const BRIDGE = 'http://127.0.0.1:9890';
const keys = ['bridgeToken', 'meetingId', 'connectionId'];

async function state() { return await chrome.storage.local.get(keys); }
async function bridge(path, options = {}) {
  const s = await state();
  const headers = {'Content-Type': 'application/json'};
  if (s.bridgeToken) headers['X-AIMR-Bridge-Token'] = s.bridgeToken;
  const response = await fetch(`${BRIDGE}${path}`, {...options, headers: {...headers, ...(options.headers || {})}});
  const body = await response.json();
  if (!response.ok || body.error) throw new Error(body.error || `bridge HTTP ${response.status}`);
  return body;
}
function conversationId(url) {
  const match = /^https:\/\/(?:chatgpt\.com|chat\.openai\.com)\/c\/([A-Za-z0-9-]+)/.exec(url || '');
  return match ? match[1] : null;
}
async function boundTabMessage(type) {
  const status = await bridge('/bridge/status');
  if (status.tabId === null || status.tabId === undefined) throw new Error('EXTENSION_NOT_BOUND');
  await chrome.tabs.get(status.tabId);
  return await chrome.tabs.sendMessage(status.tabId, {type});
}
async function healthHeartbeat() {
  try {
    const s = await state();
    if (!s.bridgeToken) return;
    const status = await bridge('/bridge/status');
    if (!status.tabId) return;
    const tab = await chrome.tabs.get(status.tabId);
    const page = await chrome.tabs.sendMessage(status.tabId, {type: 'HEALTH_CHECK'});
    await bridge('/bridge/heartbeat', {method: 'POST', body: JSON.stringify({
      authState: page.authState, domRecognized: page.domRecognized, tabId: tab.id,
      conversationBindingId: conversationId(tab.url)
    })});
  } catch (_) { /* fail-closed state is reported by the next health check */ }
}
async function pollRequests() {
  try {
    const s = await state();
    if (!s.bridgeToken) return;
    const request = await bridge('/bridge/poll');
    if (request.type === 'BRAIN_REQUEST') {
      const tab = await chrome.tabs.get((await bridge('/bridge/status')).tabId);
      const response = await chrome.tabs.sendMessage(tab.id, {type: 'BRAIN_REQUEST', request});
      if (response.error) {
        await bridge('/bridge/error', {method: 'POST', body: JSON.stringify({brainRequestId: request.brainRequestId, reason: response.error})});
      } else {
        await bridge('/bridge/response', {method: 'POST', body: JSON.stringify({brainRequestId: request.brainRequestId, rawResponse: response.rawResponse || ''})});
      }
    }
  } catch (_) { /* no retry may submit a second BrainDecision */ }
  finally { setTimeout(pollRequests, 500); }
}
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message.type === 'PAIR') {
      const connectionId = crypto.randomUUID();
      const result = await bridge('/bridge/hello', {method: 'POST', body: JSON.stringify({pairingCode: message.pairingCode, connectionId})});
      await chrome.storage.local.set({bridgeToken: result.bridgeToken, meetingId: result.meetingId, connectionId});
      pollRequests();
      sendResponse({ok: true, meetingId: result.meetingId});
    } else if (message.type === 'BIND') {
      const s = await state();
      const id = conversationId(message.url);
      if (!s.bridgeToken || !id || !message.tabId) throw new Error('bind requires a ChatGPT conversation tab');
      const result = await bridge('/bridge/bind', {method: 'POST', body: JSON.stringify({meetingId: s.meetingId, tabId: message.tabId, conversationBindingId: id})});
      await chrome.tabs.sendMessage(message.tabId, {type: 'BIND_CONFIRMED', conversationBindingId: id});
      sendResponse({ok: true, binding: result});
    } else if (message.type === 'DOM_DIAGNOSTICS') {
      sendResponse({ok: true, diagnostics: await boundTabMessage('DOM_DIAGNOSTICS')});
    } else if (message.type === 'STATUS') {
      sendResponse({ok: true, status: await bridge('/bridge/status')});
    } else if (message.type === 'UNBIND') {
      await bridge('/bridge/unbind', {method: 'POST', body: '{}'});
      sendResponse({ok: true});
    } else sendResponse({ok: false, error: 'unknown extension command'});
  })().catch(error => sendResponse({ok: false, error: error.message}));
  return true;
});
setTimeout(pollRequests, 500);
setInterval(healthHeartbeat, 5000);
