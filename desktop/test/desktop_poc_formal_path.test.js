const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.join(__dirname, '..', '..');
const main = fs.readFileSync(path.join(root, 'desktop', 'main.js'), 'utf8');
const app = fs.readFileSync(path.join(root, 'ai_meeting_room', 'product', 'app.py'), 'utf8');
const host = fs.readFileSync(path.join(root, 'ai_meeting_room', 'brain', 'playwright_attached_brain.py'), 'utf8');
const prompt = fs.readFileSync(path.join(root, 'ai_meeting_room', 'brain', 'desktop.py'), 'utf8');
const runState = fs.readFileSync(path.join(root, 'desktop', 'poc_run_state.js'), 'utf8');
const pocBody = main.slice(main.indexOf('async function runDesktopBrainPoc'), main.indexOf('\nfunction createProductWindow'));

test('test_desktop_poc_requires_ready_brain', () => {
  assert.match(app, /live_poc_health_check\(\)/);
  assert.match(app, /code="GPT_WEB_BRAIN_NOT_READY"/);
  assert.match(app, /stage="POC_PRECHECK"/);
});

test('test_desktop_poc_reuses_bound_chatgpt_page', () => {
  assert.match(app, /"reuseCurrentPage": True/);
  assert.match(host, /def prepare_request\(/);
});

test('test_poc_writes_exact_prompt', () => {
  assert.match(prompt, /desktop-brain-poc/);
  assert.match(prompt, /desktop brain poc/);
  assert.match(host, /resolve_composer\(page\)/);
  assert.match(host, /write_composer\(target, prompt\)/);
  assert.match(host, /COMPOSER_WRITE_VERIFY_FAILED/);
});

test('test_send_requires_confirmation', () => {
  assert.match(host, /chatgptSendConfirmed/);
  assert.match(host, /send_confirmed = bool/);
});

test('test_generation_completion_required', () => {
  assert.match(host, /responseCompletion/);
  assert.match(host, /stable_since/);
});

test('test_response_boundary_ignores_old_messages', () => {
  assert.match(host, /baselineAssistantCount/);
  assert.match(host, /assistant_count > self\._poc_baseline_assistant_count\(\)/);
});

test('test_only_new_assistant_turn_collected', () => {
  assert.match(host, /articles\.last/);
  assert.match(host, /_poc_baseline_assistant_count/);
});

test('test_markdown_wrapped_json_is_protocol_failure', () => {
  assert.match(fs.readFileSync(path.join(root, 'ai_meeting_room', 'brain', 'desktop.py'), 'utf8'), /parse_brain_decision/);
  assert.match(prompt, /no Markdown/);
});

test('test_invalid_json_is_parse_failure', () => {
  assert.match(app, /decision = bridge\.decide\(\)/);
  assert.match(app, /no validated decision/);
});

test('test_wrong_task_id_fails_poc', () => {
  assert.match(main, /result\.decision\.taskId !== 'desktop-brain-poc'/);
  assert.match(app, /task_id="desktop-brain-poc"/);
});

test('test_wrong_decision_fails_poc', () => {
  assert.match(main, /result\.decision\.decision !== 'ACCEPT'/);
  assert.match(app, /allowed_actions=\("ACCEPT",\)/);
});

test('test_poc_does_not_mutate_meeting_core', () => {
  assert.match(app, /"meetingCoreMutation": False/);
  assert.match(app, /_desktop_poc_active/);
});

test('test_poc_does_not_dispatch_codex', () => {
  assert.doesNotMatch(pocBody, /dispatch_task|Codex|MiniMax/);
  assert.match(app, /"codexDispatch": False/);
});

test('test_poc_single_flight', () => {
  assert.match(pocBody, /desktopPocRuns\.begin\(\)/);
  assert.match(runState, /POC_ALREADY_RUNNING/);
});

test('test_page_loss_aborts_poc', () => {
  assert.match(host, /_ensure_target\(\)/);
  assert.match(host, /CHATGPT_PAGE_NOT_FOUND/);
});

test('test_formal_poc_uses_playwright_product_route', () => {
  assert.doesNotMatch(pocBody, /ElectronWebContentsTransport/);
  assert.match(pocBody, /FORMAL_BRAIN_POC_PATH/);
  assert.match(app, /PlaywrightAttachedBrainHost/);
});
