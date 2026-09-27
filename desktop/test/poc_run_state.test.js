const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {DesktopPocRunManager, PocRunStateError} = require('../poc_run_state');

function manager() {
  let tick = 0;
  return new DesktopPocRunManager({
    now: () => `2026-09-14T00:00:0${tick++}Z`,
    idFactory: () => `run-${tick}`,
  });
}

test('only an in-progress POC is single-flight blocked', () => {
  const runs = manager();
  const run = runs.begin();
  assert.throws(() => runs.begin(), error => error instanceof PocRunStateError && error.code === 'POC_ALREADY_RUNNING');
  runs.finish(run.runId, 'FAILED', {failureClass: 'AUTH_REQUIRED'});
  assert.doesNotThrow(() => runs.begin());
});

test('failed, auth, challenge, and timeout runs release the lock for retry', () => {
  for (const failureClass of ['AUTH_REQUIRED', 'CHALLENGE_REQUIRED', 'RESPONSE_TIMEOUT']) {
    const runs = manager();
    const run = runs.begin();
    runs.finish(run.runId, 'FAILED', {failureClass});
    assert.doesNotThrow(() => runs.begin(), failureClass);
  }
});

test('a passed run is retained as history and a new run gets a new id', () => {
  const runs = manager();
  const first = runs.begin();
  const passed = runs.finish(first.runId, 'PASSED', {result: {decision: 'ACCEPT'}});
  const second = runs.begin();
  assert.equal(passed.stage, 'PASSED');
  assert.notEqual(first.runId, second.runId);
});

test('stale in-progress state becomes interrupted after restart restore', () => {
  const runs = manager();
  const stale = {runId: 'stale', startedAt: 'old', stage: 'WAITING_RESPONSE', finishedAt: null};
  const restored = runs.restore({current: stale, last: null});
  assert.equal(restored.current, null);
  assert.equal(restored.last.stage, 'INTERRUPTED');
  assert.equal(restored.last.failureClass, 'INTERRUPTED');
  assert.doesNotThrow(() => runs.begin());
});

test('the UI guards one click and polling does not call the POC', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'product', 'server.py'), 'utf8');
  assert.match(source, /if\(desktopBrainPocInFlight\)return/);
  assert.match(source, /desktopBrainPocButton/);
  assert.match(source, /setInterval\(\(\)=>\{if\(selected\)refresh\(\)\},2500\)/);
  assert.doesNotMatch(source, /setInterval\(\(\)=>\{if\(selected\)runDesktopBrainPoc/);
});

test('POC already running has a Chinese user-facing message', () => {
  const locale = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'locales', 'zh-CN.json'), 'utf8'));
  assert.match(locale.errors.POC_ALREADY_RUNNING, /正在进行/);
});
