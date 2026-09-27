const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
const preload = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8');
const product = fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'product', 'server.py'), 'utf8');
const attached = fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'brain', 'playwright_attached_brain.py'), 'utf8');

test('test_ipc_result_envelope_preserves_error_code', () => {
  assert.match(main, /ok: false/);
  assert.match(main, /error\.code/);
  assert.match(main, /attemptId/);
});

test('test_renderer_does_not_replace_known_error_with_unknown', () => {
  assert.match(product, /r\?\.ok===false/);
  assert.match(product, /d\.code/);
  assert.match(product, /INTERNAL_UNCLASSIFIED_EXCEPTION/);
  assert.match(product, /连接尝试ID/);
});

test('test_formal_runtime_never_starts_mcp', () => {
  assert.doesNotMatch(main, /ChromeDevToolsMcpClient|chromeMcpClient/);
  assert.match(main, /PLAYWRIGHT_ATTACH_EXISTING_REAL_CHROME/);
});

test('test_connect_trace_contains_required_stages', () => {
  for (const stage of ['T0_UI_CLICK', 'T1_IPC_REQUEST_SENT', 'T2_IPC_HANDLER_ENTER', 'T3_BRAIN_CONNECT_START', 'T4_PLAYWRIGHT_MODULE_READY', 'T5_CONNECT_OVER_CDP_START', 'T6_CONNECT_OVER_CDP_RESULT', 'T8_CONTEXTS_RESULT', 'T11_CHATGPT_RESOLVER_START', 'T12_CHATGPT_RESOLVER_RESULT', 'T13_COMPOSER_CHECK_START', 'T15_SUCCESS']) {
    assert.match(main + product + attached, new RegExp(stage));
  }
});

test('test_preload_passes_connect_attempt_id', () => {
  assert.match(preload, /openBrainWindow: connectAttemptId/);
  assert.match(preload, /brain:open/);
});

test('test_required_explicit_error_codes_are_present', () => {
  for (const code of ['DESKTOP_CONNECT_IPC_FAILED', 'CDP_ENDPOINT_UNREACHABLE', 'CDP_VERSION_ENDPOINT_INVALID', 'PLAYWRIGHT_LOAD_FAILED', 'PLAYWRIGHT_PACKAGE_VERSION_MISMATCH', 'PLAYWRIGHT_CONNECT_OVER_CDP_FAILED', 'PLAYWRIGHT_BROWSER_DISCONNECTED', 'PLAYWRIGHT_CONTEXT_NOT_FOUND', 'CHATGPT_PAGE_NOT_FOUND', 'CHATGPT_PAGE_URL_INVALID', 'COMPOSER_NOT_FOUND', 'CONNECT_RESULT_SERIALIZATION_FAILED', 'RENDERER_RESULT_HANDLING_FAILED', 'INTERNAL_UNCLASSIFIED_EXCEPTION']) {
    assert.match(product + main + attached, new RegExp(code));
  }
});
