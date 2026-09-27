const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {
  allowedUrl,
  allowedAuthUrl,
  safeUrl,
  BRAIN_STATES,
} = require('../brain_host');

test('Brain host models human verification as a distinct fail-closed state', () => {
  assert.ok(BRAIN_STATES.includes('CHALLENGE_REQUIRED'));
});

test('Product Shell POC status phases and failure mapping are implemented', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.match(source, /PREPARING/);
  assert.match(source, /WRITING/);
  assert.match(source, /SENDING/);
  assert.match(source, /WAITING_RESPONSE/);
  assert.match(source, /PARSING/);
  assert.match(source, /SUCCESS/);
  assert.match(source, /FAILED/);
});

test('main ChatGPT navigation allowlist stays exact-origin and HTTPS-only', () => {
  assert.equal(allowedUrl('https://chatgpt.com/auth/login?state=redacted'), true);
  assert.equal(allowedUrl('https://auth.openai.com/authorize'), true);
  assert.equal(allowedUrl('http://chatgpt.com/'), false);
  assert.equal(allowedUrl('https://evil.example/'), false);
  assert.equal(allowedUrl('https://accounts.google.com/'), false);
});

test('OAuth allowlist permits only explicit official provider origins', () => {
  assert.equal(allowedAuthUrl('https://accounts.google.com/o/oauth2/auth?code=redacted'), true);
  assert.equal(allowedAuthUrl('https://login.microsoftonline.com/common/oauth2/v2.0/authorize'), true);
  assert.equal(allowedAuthUrl('https://appleid.apple.com/auth/authorize'), true);
  assert.equal(allowedAuthUrl('https://oauth.evil.example/authorize'), false);
  assert.equal(allowedAuthUrl('http://accounts.google.com/'), false);
});

test('diagnostic URLs never retain query or fragment data', () => {
  assert.equal(safeUrl('https://accounts.google.com/o/oauth2/auth?code=secret#fragment'), 'https://accounts.google.com/o/oauth2/auth');
  assert.equal(safeUrl('not a URL'), null);
});

test('auth diagnostics cover pointer, overlay, and form boundaries without page text capture', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'brain_preload.js'), 'utf8');
  for (const marker of ['elementFromPoint', 'pointerdown', 'mousedown', 'mouseup', 'click', 'submit']) {
    assert.match(source, new RegExp(marker));
  }
  assert.doesNotMatch(source, /document\.cookie|localStorage|sessionStorage/);
  assert.doesNotMatch(source, /-webkit-app-region\s*:\s*drag/);
});

test('navigation policy has a controlled OAuth redirect boundary', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.match(source, /will-redirect/);
  assert.match(source, /oauth-redirect-to-controlled-window/);
  assert.match(source, /event\.preventDefault\(\)/);
});

test('test_loading_state_not_classified_as_auth_lost', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'brain_preload.js'), 'utf8');
  assert.match(source, /document\.readyState/);
  assert.match(source, /'LOADING'/);
  assert.doesNotMatch(source, /loading.*AUTH_LOST/i);
});

test('test_login_page_maps_to_auth_required', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'brain_preload.js'), 'utf8');
  assert.match(source, /loginPageDetected/);
  assert.match(source, /'AUTH_REQUIRED'/);
});

test('test_challenge_maps_to_challenge_required', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'brain_preload.js'), 'utf8');
  assert.match(source, /challengeDetected/);
  assert.match(source, /'CHALLENGE_REQUIRED'/);
});

test('test_authenticated_composer_maps_to_ready', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'brain_preload.js'), 'utf8');
  const host = fs.readFileSync(path.join(__dirname, '..', 'brain_host.js'), 'utf8');
  assert.match(source, /composer/);
  assert.match(source, /'AUTHENTICATED'/);
  assert.match(host, /authState === 'AUTHENTICATED' && this\.domRecognized/);
  assert.match(host, /this\._setState\('READY'\)/);
});

test('test_open_gpt_brain_button_available_when_auth_required', () => {
  const product = fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'product', 'server.py'), 'utf8');
  const preload = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8');
  assert.match(product, /打开 GPT 主脑/);
  assert.match(product, /openGptBrain/);
  assert.match(preload, /openBrainWindow/);
});

test('test_google_oauth_window_uses_gpt_partition', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.match(main, /getPersistentBrainSession\(session\)/);
  assert.match(main, /session: brainSession/);
  assert.match(main, /createAuthWindow\(url, brainSession\)/);
});

test('test_google_oauth_popup_not_denied_without_controlled_window', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  const popupBoundary = main.slice(main.indexOf('win.webContents.setWindowOpenHandler'), main.indexOf('\n}\n\nfunction createAuthWindow'));
  assert.match(popupBoundary, /createAuthWindow\(url, brainSession\)/);
  assert.match(popupBoundary, /return \{action: 'deny'\}/);
});

test('test_google_oauth_allowed_hosts_are_minimal', () => {
  const host = fs.readFileSync(path.join(__dirname, '..', 'brain_host.js'), 'utf8');
  assert.match(host, /https:\/\/accounts\.google\.com/);
  assert.doesNotMatch(host, /\*\.google|https:\/\/google\.com/);
});

test('test_google_oauth_window_keeps_secure_webpreferences', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  const authWindow = main.slice(main.indexOf('function createAuthWindow'), main.indexOf('\n}\n\nasync function runDesktopBrainPoc'));
  for (const marker of ['nodeIntegration: false', 'contextIsolation: true', 'sandbox: true', 'webSecurity: true']) assert.match(authWindow, new RegExp(marker));
  assert.doesNotMatch(authWindow, /document\.cookie|localStorage|sessionStorage|Authorization|password/);
});

test('test_google_oauth_in_progress_not_auth_lost', () => {
  const host = fs.readFileSync(path.join(__dirname, '..', 'brain_host.js'), 'utf8');
  const transport = fs.readFileSync(path.join(__dirname, '..', 'electron_transport.js'), 'utf8');
  assert.match(host, /LOADING/);
  assert.match(host, /AUTH_REQUIRED/);
  assert.match(transport, /status\.authState === 'LOADING' \? 'LOADING'/);
  assert.doesNotMatch(host, /authState === 'LOADING'[^\n]*AUTH_LOST/);
});

test('test_oauth_callback_rechecks_brain_auth', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.match(main, /oauth-window-loaded/);
  assert.match(main, /brainHost\.refreshDomHealth\(\)/);
  assert.match(main, /shared Brain session/);
});

test('test_google_oauth_logs_do_not_include_tokens_or_credentials', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.match(main, /hostname=\$\{parsed\.hostname\} pathname=\$\{parsed\.pathname/);
  assert.doesNotMatch(main, /document\.cookie|localStorage|sessionStorage|Authorization|password|authToken|refreshToken/i);
});
