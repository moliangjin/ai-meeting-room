const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.join(__dirname, '..', '..');
const main = fs.readFileSync(path.join(root, 'desktop', 'main.js'), 'utf8');
const app = fs.readFileSync(path.join(root, 'ai_meeting_room', 'product', 'app.py'), 'utf8');
const runtimeServer = fs.readFileSync(path.join(root, 'ai_meeting_room', 'product', 'server.py'), 'utf8');
const serve = fs.readFileSync(path.join(root, 'ai_meeting_room', 'product', 'serve.py'), 'utf8');
const server = fs.readFileSync(path.join(root, 'ai_meeting_room', 'product', 'server.py'), 'utf8');
const host = fs.readFileSync(path.join(root, 'ai_meeting_room', 'brain', 'playwright_attached_brain.py'), 'utf8');
const identity = fs.readFileSync(path.join(root, 'ai_meeting_room', 'runtime', 'identity.py'), 'utf8');

test('test_desktop_and_backend_share_launch_session', () => {
  assert.match(main, /const launchSessionId = randomUUID\(\)/);
  assert.match(main, /AI_MEETING_ROOM_LAUNCH_SESSION_ID: launchSessionId/);
  assert.match(identity, /AI_MEETING_ROOM_LAUNCH_SESSION_ID/);
});

test('test_old_backend_is_not_silently_reused', () => {
  assert.match(main, /readProductRuntimeIdentity\(\)/);
  assert.match(main, /identity\.launchSessionId !== launchSessionId/);
  assert.match(main, /stopOwnedStaleProductShell\(identity\)/);
  assert.match(main, /BACKEND_LAUNCH_SESSION_MISMATCH/);
});

test('test_unknown_port_owner_is_never_killed', () => {
  const shutdown = main.slice(main.indexOf('async function stopOwnedStaleProductShell'), main.indexOf('\nasync function ensureOwnedProductShell'));
  assert.match(shutdown, /PRODUCT_SHELL_PORT_OCCUPIED_BY_UNKNOWN_PROCESS/);
  assert.match(shutdown, /RUNTIME_SHUTDOWN_PATH/);
  assert.doesNotMatch(shutdown, /process\.kill/);
});

test('test_backend_child_terminates_with_electron', () => {
  assert.match(main, /function stopCoreProcessGracefully/);
  assert.match(main, /child\.kill\('SIGINT'\)/);
  assert.match(main, /child\.kill\('SIGTERM'\)/);
  assert.match(main, /app\.on\('before-quit'/);
});

test('test_formal_playwright_runtime_uses_one_request_thread', () => {
  assert.match(runtimeServer, /from http\.server import BaseHTTPRequestHandler, HTTPServer/);
  assert.match(runtimeServer, /return HTTPServer\(\(host, port\), handler\)/);
  assert.doesNotMatch(runtimeServer, /ThreadingHTTPServer/);
});

test('test_all_sync_playwright_calls_run_on_owner_thread', () => {
  assert.match(host, /def _record_owner_thread\(self, operation_name/);
  assert.match(host, /actual_thread_id != self\.owner_thread_id/);
  assert.match(host, /PLAYWRIGHT_THREAD_AFFINITY_VIOLATION/);
});

test('test_http_thread_never_touches_playwright_page', () => {
  assert.match(runtimeServer, /return HTTPServer\(\(host, port\), handler\)/);
  assert.match(host, /def _record_owner_thread/);
});

test('test_health_check_runs_on_owner_thread', () => {
  assert.match(host, /def checkHealth\(self\)[\s\S]*?_record_owner_thread\("health_check"\)/);
});

test('test_page_resolver_runs_on_owner_thread', () => {
  assert.match(host, /def _find_chatgpt_page[\s\S]*?_record_owner_thread\("page_resolver"\)/);
});

test('test_composer_check_runs_on_owner_thread', () => {
  assert.match(host, /def prepare_request[\s\S]*?_record_owner_thread\("composer_check"\)/);
});

test('test_poc_precheck_runs_on_owner_thread', () => {
  assert.match(host, /def live_poc_health_check[\s\S]*?_record_owner_thread\("poc_precheck"\)/);
});

test('test_connect_and_poc_same_owner_thread', () => {
  assert.match(host, /"ownerThreadId": self\.owner_thread_id/);
  assert.match(app, /poc_route_consistency/);
  assert.match(runtimeServer, /api\/runtime\/poc-route/);
});

test('test_owner_thread_is_persistent', () => {
  assert.match(host, /self\.owner_thread_id = threading\.get_ident\(\)/);
  assert.match(host, /self\._thread_operation_matrix/);
});

test('test_wrong_thread_has_explicit_affinity_error', () => {
  assert.match(host, /expectedThreadId/);
  assert.match(host, /actualThreadId/);
  assert.match(host, /operationName/);
});

test('test_poc_precheck_passes_for_live_ready_state_model', () => {
  assert.match(app, /"precheckResult": state\.get\("precheckResult"\)/);
  assert.match(app, /"composerReady": bool\(state\.get\("composerReady"\)\)/);
});

test('test_runtime_identity_reports_real_source_root', () => {
  assert.match(identity, /project_root/);
  assert.match(identity, /source_root/);
  assert.match(app, /RuntimeIdentity\.create\(Path\(__file__\)\.resolve\(\)\.parents\[2\]\)/);
  assert.match(server, /api\/runtime\/identity/);
});

test('test_runtime_identity_reports_module_paths', () => {
  assert.match(app, /formalRegistry/);
  assert.match(app, /playwrightHost/);
  assert.match(app, /desktopPocHandler/);
  assert.match(app, /connectHandler/);
  assert.match(identity, /module_metadata/);
  assert.match(identity, /sha256/);
});

test('test_connect_and_poc_same_launch_session', () => {
  assert.match(main, /launchSessionId: data\.launchSessionId/);
  assert.match(main, /launchSessionId: result\.launchSessionId/);
  assert.match(app, /"launchSessionId": self\._runtime_identity\.launch_session_id/);
});

test('test_connect_and_poc_same_process', () => {
  assert.match(app, /"processPid": self\._runtime_identity\.process_pid/);
  assert.match(identity, /process_pid=os\.getpid\(\)/);
  assert.match(main, /AI_MEETING_ROOM_ELECTRON_PID/);
});

test('test_connect_and_poc_same_registry', () => {
  assert.match(app, /self\.formal_brain_registry\.get\(\)/);
  assert.match(app, /"registryInstanceId": identity\.get/);
  assert.match(identity, /registryInstanceId/);
});

test('test_connect_and_poc_same_host', () => {
  assert.match(app, /host = self\.formal_brain_registry\.get\(\)/);
  assert.match(app, /hostInstanceId/);
  assert.match(identity, /host_instance_id/);
});

test('test_connect_and_poc_same_bound_page', () => {
  assert.match(app, /"boundPageId": live\.get/);
  assert.match(identity, /"boundPageId": state\.get/);
  assert.match(fs.readFileSync(path.join(root, 'ai_meeting_room', 'brain', 'playwright_attached_brain.py'), 'utf8'), /bound_page_id/);
});

test('test_ui_state_and_poc_state_share_authoritative_source', () => {
  assert.match(app, /def get_formal_brain_runtime_state/);
  assert.match(app, /status = self\.get_formal_brain_runtime_state\(\)/);
  assert.match(app, /precheck = self\.get_formal_brain_runtime_state\(\)/);
});

test('test_ready_not_ready_contradiction_raises_invariant_violation', () => {
  assert.match(app, /FORMAL_BRAIN_STATE_INVARIANT_VIOLATION/);
  assert.match(app, /invariantViolation/);
  assert.match(app, /cached_ready/);
  assert.match(app, /live_ready/);
});

test('test_stale_worktree_backend_detected', () => {
  assert.match(main, /sameProjectRoot\(identity, projectRoot\)/);
  assert.match(main, /LIVE_SOURCE_TREE_MISMATCH/);
  assert.match(identity, /projectRoot/);
});

test('test_wrong_source_tree_detected', () => {
  assert.match(main, /Product Shell source tree does not belong to this Electron/);
  assert.match(main, /sourceRoot/);
  assert.match(main, /PRODUCT_SHELL_PORT_OCCUPIED_BY_UNKNOWN_PROCESS/);
});
