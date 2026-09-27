const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const {assertIpcSerializable, findNonSerializableField, serializeError} = require('../ipc_serialization');
const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
const product = fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'product', 'app.py'), 'utf8');
const renderer = fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'product', 'server.py'), 'utf8');
const attached = fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'brain', 'playwright_attached_brain.py'), 'utf8');
const locale = fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'locales', 'zh-CN.json'), 'utf8');

test('test_connect_result_contains_plain_data_only', () => {
  const result = {
    ok: true,
    data: {connection: {
      attemptId: 'attempt-1', brainConnectionId: 'chrome-1', connectionMode: 'PLAYWRIGHT_CONNECT_OVER_CDP_CHANNEL',
      channel: 'chrome', browserConnected: true, contextCount: 1, pageCount: 2, chatgptPageCount: 1,
      selectedPage: {hostname: 'chatgpt.com', pathname: '/'}, composerDetected: false, state: 'ERROR',
    }},
  };
  assert.equal(assertIpcSerializable(result), result);
});

test('test_playwright_page_never_crosses_ipc', () => {
  class Page {}
  const issue = findNonSerializableField({result: {boundPage: new Page()}});
  assert.deepEqual(issue, {path: 'result.result.boundPage', constructorName: 'Page'});
});

test('test_browser_context_never_crosses_ipc', () => {
  class BrowserContext {}
  assert.throws(() => assertIpcSerializable({connection: {context: new BrowserContext()}}), error => (
    error.code === 'IPC_RESULT_NOT_SERIALIZABLE' && error.fieldPath === 'result.connection.context'
  ));
});

test('test_playwright_browser_never_crosses_ipc', () => {
  class Browser {}
  assert.throws(() => assertIpcSerializable({host: {browser: new Browser()}}), error => (
    error.code === 'IPC_RESULT_NOT_SERIALIZABLE' && error.fieldPath === 'result.host.browser'
  ));
});

test('test_error_instance_serialized_before_ipc', () => {
  const error = Object.assign(new Error('hidden diagnostic'), {code: 'COMPOSER_NOT_FOUND', stage: 'T13_COMPOSER_CHECK'});
  const serialized = serializeError(error, 'attempt-2');
  assert.deepEqual(serialized, {
    name: 'Error', message: 'hidden diagnostic', code: 'COMPOSER_NOT_FOUND',
    stage: 'T13_COMPOSER_CHECK', attemptId: 'attempt-2',
  });
  assert.equal('stack' in serialized, false);
});

test('test_ipc_payload_passes_structured_clone', () => {
  assert.doesNotThrow(() => assertIpcSerializable({ok: false, error: {
    name: 'PlaywrightError', message: 'composer missing', code: 'COMPOSER_NOT_FOUND',
    stage: 'T13_COMPOSER_CHECK', attemptId: 'attempt-3',
  }}));
});

test('test_non_serializable_field_reports_path', () => {
  assert.throws(() => assertIpcSerializable({result: {page: new URL('https://chatgpt.com/')}}), error => (
    error.code === 'IPC_RESULT_NOT_SERIALIZABLE' && error.fieldPath === 'result.result.page' && error.constructorName === 'URL'
  ));
});

test('test_business_error_not_mapped_to_ipc_failure', () => {
  assert.match(main, /if \(result\?\.ok === false\)/);
  assert.match(main, /return safeIpcReturn\(result, attemptId\)/);
  assert.match(renderer, /let code=d\.code\|\|'INTERNAL_UNCLASSIFIED_EXCEPTION'/);
});

test('test_composer_not_found_survives_ipc_envelope', () => {
  assert.match(attached, /code="COMPOSER_NOT_FOUND"/);
  assert.match(main, /safeIpcReturn\(result, attemptId\)/);
  assert.match(locale, /COMPOSER_NOT_FOUND/);
});

test('test_success_result_survives_ipc_round_trip', () => {
  assert.match(main, /return safeIpcReturn\(\{ok: true, data:/);
  assert.match(main, /assertIpcSerializable/);
});

test('test_attempt_id_survives_ipc_round_trip', () => {
  assert.match(main, /connectAttemptId: attemptId/);
  assert.match(main, /serializeError\(error, attemptId\)/);
  assert.match(product, /_connect_result_dto\(status, attempt_id\)/);
});

test('test_last_success_stage_is_preserved', () => {
  assert.match(attached, /lastSuccessfulStage/);
  assert.match(attached, /T12_CHATGPT_PAGE_RESOLVE_RESULT/);
});

test('test_last_failure_stage_is_preserved', () => {
  assert.match(attached, /lastFailureStage/);
  assert.match(attached, /T13_COMPOSER_CHECK/);
});

test('test_renderer_does_not_show_native_modal_for_business_failure', () => {
  assert.match(renderer, /function showDesktopConnectionFailure/);
  assert.match(renderer, /isDesktopIpcInfrastructureFailure/);
  assert.match(renderer, /else showDesktopConnectionFailure/);
});

test('test_renderer_does_not_classify_10s_wait_as_ipc_failure', () => {
  assert.doesNotMatch(renderer, /Promise\.race\([\s\S]{0,300}10000/);
  assert.match(main, /FORMAL_BRAIN_OPEN_TIMEOUT_MS/);
  assert.match(main, /CONNECT_OPERATION_TIMEOUT/);
});

test('test_slow_success_after_10s_is_delivered', () => {
  assert.match(main, /FORMAL_BRAIN_OPEN_TIMEOUT_MS.*60000/);
  assert.match(main, /return safeIpcReturn\(\{ok: true, data:/);
});

test('test_slow_business_failure_after_10s_is_delivered', () => {
  assert.match(main, /if \(result\?\.ok === false\)/);
  assert.match(main, /return safeIpcReturn\(result, attemptId\)/);
});

test('test_true_ipc_rejection_maps_to_desktop_connect_ipc_failed', () => {
  assert.match(renderer, /code:'DESKTOP_CONNECT_IPC_FAILED'/);
  assert.match(main, /IPC_RESULT_NOT_SERIALIZABLE/);
});

test('test_single_connect_attempt_survives_long_wait', () => {
  assert.match(renderer, /if\(desktopConnectInFlight\)return/);
  assert.match(renderer, /desktopConnectInFlight=true/);
  assert.match(main, /connectAttemptId: attemptId/);
});

test('test_connect_button_disabled_while_attempt_running', () => {
  assert.match(renderer, /function setDesktopConnectUiState/);
  assert.match(renderer, /button\.disabled=inFlight/);
  assert.match(renderer, /desktopConnectButton/);
});

test('test_no_duplicate_connect_over_cdp_during_long_attempt', () => {
  assert.match(attached, /_connect_lock/);
  assert.match(attached, /_connect_in_progress/);
  assert.match(attached, /chromium\.connect_over_cdp/);
});

test('test_attempt_stage_remains_visible_during_wait', () => {
  assert.match(attached, /attemptStartedAt/);
  assert.match(attached, /stageStartedAt/);
  assert.match(attached, /stageFinishedAt/);
  assert.match(renderer, /最后成功阶段/);
});

test('test_composer_not_found_not_remapped_to_ipc_failure', () => {
  assert.match(attached, /code="COMPOSER_NOT_FOUND"/);
  assert.match(renderer, /showDesktopConnectionFailure\(code/);
  assert.match(main, /return safeIpcReturn\(result, attemptId\)/);
});

test('test_ready_after_long_connect_is_not_timed_out_by_renderer', () => {
  assert.match(renderer, /await window\.aimrDesktop\.openBrainWindow\(attemptId\)/);
  assert.match(renderer, /await refreshDesktopBrainStatus\(\)/);
  assert.match(renderer, /finally\{desktopConnectInFlight=false/);
});
