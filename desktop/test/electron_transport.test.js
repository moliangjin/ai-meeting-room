const assert = require('node:assert/strict');
const test = require('node:test');
const {ElectronWebContentsTransport, ElectronTransportError} = require('../electron_transport');

class FakeBrainHost {
  constructor({authState = 'AUTHENTICATED', alive = true, responses = []} = {}) {
    this.authState = authState;
    this.alive = alive;
    this.responses = [...responses];
  }

  status() { return {authState: this.authState, webContentsAlive: this.alive, url: 'https://chatgpt.com/'}; }

  executeJavaScript(expression) {
    if (expression.includes('prepareDesktopRequest')) return Promise.resolve({ok: true});
    if (expression.includes('writeDesktopPrompt')) return Promise.resolve({ok: true, composerWrite: true});
    if (expression.includes('sendDesktopPrompt')) return Promise.resolve({ok: true, composerWrite: true, send: true});
    if (expression.includes('desktopResponseState')) return Promise.resolve(this.responses.shift() || {completed: false});
    return Promise.resolve(null);
  }
}

test('desktop transport requires authenticated brain', async () => {
  await assert.rejects(
    () => new ElectronWebContentsTransport(new FakeBrainHost({authState: 'AUTH_REQUIRED'})).connect(),
    error => error instanceof ElectronTransportError && error.code === 'AUTH_REQUIRED',
  );
});

test('desktop transport preserves loading and unknown health classifications', async () => {
  await assert.rejects(
    () => new ElectronWebContentsTransport(new FakeBrainHost({authState: 'LOADING'})).connect(),
    error => error instanceof ElectronTransportError && error.code === 'LOADING',
  );
  await assert.rejects(
    () => new ElectronWebContentsTransport(new FakeBrainHost({authState: 'DOM_UNKNOWN'})).connect(),
    error => error instanceof ElectronTransportError && error.code === 'DOM_UNKNOWN',
  );
});

test('desktop transport rejects a browser challenge without treating it as authenticated', async () => {
  await assert.rejects(
    () => new ElectronWebContentsTransport(new FakeBrainHost({authState: 'CHALLENGE_REQUIRED'})).connect(),
    error => error instanceof ElectronTransportError && error.code === 'CHALLENGE_REQUIRED',
  );
});

test('desktop transport requires live WebContents', async () => {
  await assert.rejects(
    () => new ElectronWebContentsTransport(new FakeBrainHost({alive: false})).connect(),
    error => error instanceof ElectronTransportError && error.code === 'WEB_CONTENTS_LOST',
  );
});

test('composer write failure fails closed', async () => {
  const host = new FakeBrainHost();
  host.executeJavaScript = expression => expression.includes('prepareDesktopRequest')
    ? Promise.resolve({ok: false, error: 'COMPOSER_WRITE_FAILED'})
    : Promise.resolve(null);
  const transport = new ElectronWebContentsTransport(host);
  await assert.rejects(
    () => transport.prepareRequest({brainRequestId: 'r-1', taskId: 'desktop-brain-poc', prompt: 'x'}),
    error => error.code === 'COMPOSER_WRITE_FAILED',
  );
});

test('send not confirmed fails closed', async () => {
  const host = new FakeBrainHost({responses: [{completed: false, sendConfirmed: false}]});
  const transport = new ElectronWebContentsTransport(host, {pollIntervalMs: 1});
  await transport.prepareRequest({brainRequestId: 'r-1', taskId: 'desktop-brain-poc', prompt: 'x'});
  await transport.sendPrompt('x');
  await assert.rejects(
    () => transport.waitForCompletion(0.002),
    error => error.code === 'RESPONSE_TIMEOUT',
  );
});

test('response completion and boundary are required', async () => {
  const host = new FakeBrainHost({responses: [{completed: true, responseBoundary: false, rawResponse: '{"x":1}'}]});
  const transport = new ElectronWebContentsTransport(host);
  await transport.prepareRequest({brainRequestId: 'r-1', taskId: 'desktop-brain-poc', prompt: 'x'});
  await transport.sendPrompt('x');
  await assert.rejects(
    () => transport.waitForCompletion(1),
    error => error.code === 'RESPONSE_BOUNDARY_FAILURE',
  );
});

test('successful transport returns only the completed latest response', async () => {
  const raw = '{"decision":"ACCEPT"}';
  const host = new FakeBrainHost({responses: [{completed: true, responseBoundary: true, sendConfirmed: true, rawResponse: raw}]});
  const transport = new ElectronWebContentsTransport(host);
  const request = {brainRequestId: 'r-1', taskId: 'desktop-brain-poc', prompt: 'x'};
  await transport.prepareRequest(request);
  const sent = await transport.sendPrompt('x');
  const state = await transport.waitForCompletion(1);
  assert.equal(sent.composerWrite, true);
  assert.equal(state.responseBoundary, true);
  assert.equal(transport.readResponse(), raw);
});
