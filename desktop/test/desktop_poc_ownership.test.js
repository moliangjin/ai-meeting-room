const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.join(__dirname, '..', '..');
const main = fs.readFileSync(path.join(root, 'desktop', 'main.js'), 'utf8');
const preload = fs.readFileSync(path.join(root, 'desktop', 'preload.js'), 'utf8');
const app = fs.readFileSync(path.join(root, 'ai_meeting_room', 'product', 'app.py'), 'utf8');
const server = fs.readFileSync(path.join(root, 'ai_meeting_room', 'product', 'server.py'), 'utf8');
const registry = fs.readFileSync(path.join(root, 'ai_meeting_room', 'brain', 'runtime_registry.py'), 'utf8');
const host = fs.readFileSync(path.join(root, 'ai_meeting_room', 'brain', 'playwright_attached_brain.py'), 'utf8');

test('test_registry_singleton_is_process_local_not_global', () => {
  assert.match(registry, /registry_instance_id/);
  assert.match(registry, /os\.getpid\(\)/);
  assert.match(registry, /processLocal|process-local|process local/i);
});

test('test_formal_runtime_has_single_owner', () => {
  assert.match(registry, /runtime_owner/);
  assert.match(app, /FORMAL_RUNTIME_OWNER|PRODUCT_SHELL/);
  assert.doesNotMatch(main, /new\s+FormalBrainRuntimeRegistry|new\s+PlaywrightAttachedBrainHost/);
});

test('test_connect_routes_to_formal_owner', () => {
  const handler = main.slice(main.indexOf("ipcMain.handle('brain:open'"), main.indexOf("ipcMain.handle('brain:desktop-poc'"));
  assert.match(handler, /FORMAL_BRAIN_OPEN_PATH/);
  assert.match(handler, /postJson\(/);
  assert.doesNotMatch(handler, /if\s*\(brainWindow/);
});

test('test_desktop_poc_routes_to_formal_owner', () => {
  const handler = main.slice(main.indexOf('async function runDesktopBrainPoc'), main.indexOf('\nfunction createProductWindow'));
  assert.match(handler, /FORMAL_BRAIN_POC_PATH/);
  assert.match(handler, /postJson\(/);
  assert.doesNotMatch(handler, /new\s+ChatGPTDesktopBrainHost|brainHost/);
});

test('test_connect_and_poc_same_registry_instance', () => {
  assert.match(app, /self\.formal_brain_registry\.get\(\)/);
  assert.match(app, /registryInstanceId/);
  assert.match(app, /hostInstanceId/);
});

test('test_connect_and_poc_same_host_instance', () => {
  assert.match(host, /host_instance_id/);
  assert.match(app, /host = self\.formal_brain_registry\.get\(\)/);
  assert.match(app, /"hostInstanceId"/);
});

test('test_connect_and_poc_same_bound_page', () => {
  assert.match(host, /bound_page_id/);
  assert.match(host, /"boundPageId"/);
  assert.match(app, /"boundPageId": precheck/);
});

test('test_non_owner_cannot_create_formal_host', () => {
  assert.doesNotMatch(main, /FormalBrainRuntimeRegistry|PlaywrightAttachedBrainHost/);
  assert.match(app, /FormalBrainRuntimeRegistry\(/);
});

test('test_http_and_ipc_paths_do_not_create_duplicate_hosts', () => {
  assert.match(preload, /ipcRenderer\.invoke\('brain:open'/);
  assert.match(preload, /ipcRenderer\.invoke\('brain:desktop-poc'/);
  assert.match(main, /postJson\(`http:\/\/127\.0\.0\.1:\$\{PRODUCT_PORT\}\$\{FORMAL_BRAIN_OPEN_PATH\}/);
  assert.match(main, /postJson\(`http:\/\/127\.0\.0\.1:\$\{PRODUCT_PORT\}\$\{FORMAL_BRAIN_POC_PATH\}/);
  const formalHandlers = `${main.slice(main.indexOf("ipcMain.handle('brain:status'"), main.indexOf("ipcMain.handle('runtime:status'"))}`;
  assert.doesNotMatch(formalHandlers, /new\s+ChatGPTDesktopBrainHost/);
});

test('test_poc_uses_owner_live_health', () => {
  assert.match(app, /host\.live_poc_health_check\(\)/);
  assert.match(host, /def live_poc_health_check/);
  assert.match(host, /processPid|process_pid/);
});

test('test_cached_ui_ready_not_authoritative', () => {
  assert.match(app, /live_poc_health_check\(\)/);
  assert.match(main, /FORMAL_BRAIN_STATUS_PATH/);
  assert.doesNotMatch(main, /brainWindow[\s\S]{0,160}AUTHENTICATED/);
});

test('test_process_identity_is_safe_and_secret_free', () => {
  for (const source of [registry, host]) {
    assert.match(source, /process\.pid|os\.getpid/);
    assert.doesNotMatch(source, /document\.cookie|Authorization|refreshToken|accessToken/);
  }
  assert.match(app, /formalRuntimeType|registryInstanceId|hostInstanceId/);
  assert.doesNotMatch(app, /document\.cookie|Authorization|refreshToken|accessToken/);
  assert.match(server, /real_chrome_brain_status/);
});
