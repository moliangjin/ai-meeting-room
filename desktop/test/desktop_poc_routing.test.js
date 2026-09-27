const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.join(__dirname, '..', '..');
const app = fs.readFileSync(path.join(root, 'ai_meeting_room', 'product', 'app.py'), 'utf8');
const host = fs.readFileSync(path.join(root, 'ai_meeting_room', 'brain', 'playwright_attached_brain.py'), 'utf8');
const selection = fs.readFileSync(path.join(root, 'ai_meeting_room', 'brain', 'chatgpt_page_selection.py'), 'utf8');
const poc = app.slice(app.indexOf('    def run_real_chrome_brain_poc'), app.indexOf('\n    # Historical method names'));

test('test_desktop_poc_uses_same_host_as_connect_flow', () => {
  assert.match(app, /self\.formal_brain_registry\.get\(\)/);
  assert.match(poc, /host = self\.formal_brain_registry\.get\(\)/);
});

test('test_desktop_poc_uses_formal_playwright_attached_runtime', () => {
  assert.match(app, /FormalBrainRuntimeRegistry/);
  assert.match(app, /PlaywrightAttachedBrainHost/);
  assert.match(poc, /PlaywrightAttachedBrainHost|formal_brain_registry/);
});

test('test_desktop_poc_does_not_use_legacy_chrome_web_host', () => {
  assert.doesNotMatch(poc, /ChromeWebBrainHost|Chrome Web Brain is not ready/);
});

test('test_desktop_poc_does_not_use_manual_brain_state', () => {
  assert.doesNotMatch(poc, /ManualBrainBridge|_brains|snapshot\(/);
});

test('test_historical_auth_pause_does_not_override_current_ready_brain', () => {
  assert.match(poc, /get_formal_brain_runtime_state\(\)/);
  assert.doesNotMatch(poc, /historicalPauseReason|pauseReason|MeetingStatus/);
});

test('test_poc_precheck_uses_current_runtime_health', () => {
  assert.match(poc, /precheck = self\.get_formal_brain_runtime_state\(\)/);
  assert.match(poc, /precheck\.get\("precheckResult"\) != "PASS"/);
});

test('test_authenticated_ready_brain_passes_poc_precheck', () => {
  assert.match(host, /scored\["ready"\]/);
  assert.match(host, /result\["precheckResult"\] = "PASS"/);
});

test('test_auth_required_only_when_live_auth_check_detects_login_ui', () => {
  assert.match(selection, /LOGIN_UI/);
  assert.match(selection, /login_ui_detected/);
  assert.match(selection, /AUTH_REQUIRED/);
});

test('test_poc_reuses_bound_page', () => {
  assert.match(poc, /"reuseCurrentPage": True/);
  assert.match(host, /self\.page is None/);
  assert.match(host, /self\.page not in list\(self\.context\.pages\)/);
});

test('test_poc_does_not_reconnect_browser_when_bound_page_alive', () => {
  assert.match(host, /def live_poc_health_check/);
  assert.doesNotMatch(host.slice(host.indexOf('    def live_poc_health_check'), host.indexOf('\n    def recover')), /connect\(/);
});

test('test_business_not_ready_error_not_internal_unclassified', () => {
  assert.match(app, /code="GPT_WEB_BRAIN_NOT_READY"/);
  assert.match(app, /stage="POC_PRECHECK"/);
  assert.doesNotMatch(poc.slice(0, poc.indexOf('bridge =')), /INTERNAL_UNCLASSIFIED_EXCEPTION/);
});

test('test_poc_diagnostics_include_runtime_and_instance_id', () => {
  for (const field of ['formalRuntimeType', 'hostInstanceId', 'boundPageId', 'browserConnected', 'pageAlive', 'authState', 'composerReady', 'precheckResult', 'failureCode']) {
    assert.match(host, new RegExp(`"${field}"`));
  }
  assert.match(app, /"pocPrecheck": precheck/);
});
