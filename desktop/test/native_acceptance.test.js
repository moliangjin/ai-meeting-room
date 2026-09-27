const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const test = require('node:test');
const {SIZES, assertSealedNativeAudit, readProductBundleMarker, hasSealedAcceptanceMarker} = require('../native_acceptance');

const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');

test('native audit is restricted to a packaged sealed acceptance manifest and runtime root', () => {
  const options = {sealedBundle: true, bundledAcceptanceConfiguration: {acceptanceMode: true},
    launchConfiguration: {acceptanceMode: true},
    paths: {root: '/private/tmp/aimr-native-audit-test', runtime: '/private/tmp/aimr-native-audit-test/runtime'}};
  assert.doesNotThrow(() => assertSealedNativeAudit(options));
  for (const change of [
    {sealedBundle: false},
    {bundledAcceptanceConfiguration: null},
    {launchConfiguration: {acceptanceMode: false}},
    {paths: {...options.paths, runtime: '/private/tmp/outside'}},
  ]) assert.throws(() => assertSealedNativeAudit({...options, ...change}), /NATIVE_ACCEPTANCE_/);
});

test('native packaged audit uses all three required BrowserWindow sizes', () => {
  assert.deepEqual(SIZES.map(({width, height}) => [width, height]),
    [[1440, 900], [1200, 800], [1024, 700]]);
});

test('normal packaged release cannot start internal native audit through environment flags alone', () => {
  assert.match(main, /!sealedNativeAcceptanceBundle \|\| !bundledAcceptanceConfiguration \|\|/);
  assert.match(main, /nativeAcceptanceAuditStarted = true/);
  assert.match(main, /maybeRunNativeAcceptanceAudit\(\);/);
  assert.equal(hasSealedAcceptanceMarker('/private/tmp/missing-resources'), false);
});

test('official app bundle marker distinguishes sealed and normal release without app.isPackaged', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-bundle-marker-'));
  const resources = path.join(root, 'Contents', 'Resources');
  fs.mkdirSync(resources, {recursive: true});
  const plist = path.join(root, 'Contents', 'Info.plist');
  try {
    const source = value => `<plist><dict><key>CFBundleIdentifier</key><string>local.ai-meeting-room.desktop</string><key>AIMRAcceptanceBuild</key><${value}/></dict></plist>`;
    fs.writeFileSync(plist, source('true'));
    assert.equal(readProductBundleMarker(resources), true);
    assert.equal(hasSealedAcceptanceMarker(resources), true);
    fs.writeFileSync(plist, source('false'));
    assert.equal(readProductBundleMarker(resources), false);
    assert.equal(hasSealedAcceptanceMarker(resources), false);
    fs.writeFileSync(plist, source('false').replace('local.ai-meeting-room.desktop', 'com.github.Electron'));
    assert.equal(readProductBundleMarker(resources), null);
  } finally {
    fs.rmSync(root, {recursive: true});
  }
});

test('official app bundles do not enable dev probes or auth diagnostics', () => {
  assert.match(main, /AI_MEETING_ROOM_ENABLE_DEV_PROBES: officialProductBundle \? '0' : '1'/);
  assert.match(main, /additionalArguments: officialProductBundle \? \[\] : \['--aimr-auth-diagnostics=1'\]/);
});
