const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.join(__dirname, '..', '..');
const main = fs.readFileSync(path.join(root, 'desktop', 'main.js'), 'utf8');
const runtimeConfig = fs.readFileSync(path.join(root, 'desktop', 'runtime_config.js'), 'utf8');
const launcher = fs.readFileSync(path.join(root, '启动 AI Meeting Room.command'), 'utf8');
const packageJson = JSON.parse(fs.readFileSync(path.join(root, 'desktop', 'package.json'), 'utf8'));
const version = fs.readFileSync(path.join(root, 'ai_meeting_room', 'VERSION'), 'utf8').trim();

test('v1 desktop has process-level single-instance protection', () => {
  assert.match(main, /app\.requestSingleInstanceLock\(\)/);
  assert.match(main, /app\.on\('second-instance'/);
  assert.match(main, /productWindow\.focus\(\)/);
});

test('V1 boot does not automatically launch or probe the experimental GPT Web runtime', () => {
  const bootBody = main.match(/async function boot\(\) \{([\s\S]*?)\n\}/)?.[1] || '';
  assert.doesNotMatch(bootBody, /startFormalChromeBrain\s*\(/);
  assert.match(bootBody, /if\s*\(process\.env\.AIMR_ENABLE_ELECTRON_BRAIN_LEGACY === '1'\)\s*createBrainWindow\(\)/);
  const ui = fs.readFileSync(path.join(root, 'ai_meeting_room', 'product', 'server.py'), 'utf8');
  assert.match(ui, /desktopBrainStatusOptIn/);
  assert.match(ui, /if\(!desktopBrainStatusOptIn\)/);
});

test('desktop metadata follows the authoritative V1 version', () => {
  assert.equal(packageJson.version, version);
});

test('launcher writes bounded product logs outside the source tree', () => {
  assert.match(launcher, /AI_MEETING_ROOM_DATA_DIR/);
  assert.match(launcher, /LOG_DIR="\$PRODUCT_DATA_DIR\/logs"/);
  assert.match(launcher, /desktop-launch\.log/);
  assert.match(launcher, /tail -c/);
  assert.doesNotMatch(launcher, /LOG_DIR="\$PROJECT_DIR\/logs"/);
});

test('operational files do not persist raw Product Shell output or arbitrary brain trace values', () => {
  assert.match(main, /coreProcess\.stdout\.on\('data', \(\) => \{\}\)/);
  assert.match(main, /coreProcess\.stderr\.on\('data', \(\) => \{\}\)/);
  assert.match(main, /const allowed = new Set\(/);
  assert.match(main, /desktopLogPath = productStoragePaths\?\.database \? path\.join\(productStoragePaths\.logs, 'desktop\.log'\)/);
  assert.doesNotMatch(main, /appendProductLog\(/);
});

test('packaged Python runtime does not create bytecode caches inside the app bundle', () => {
  assert.match(main, /PYTHONDONTWRITEBYTECODE:\s*'1'/);
  assert.match(main, /createBackendEnvironment\(process\.env, productStoragePaths, launchConfiguration\)/);
  assert.match(runtimeConfig, /AI_MEETING_ROOM_DATA_DIR/);
  assert.match(runtimeConfig, /AI_MEETING_ROOM_DB:\s*productPaths\.database/);
  assert.ok(main.indexOf('resolveProductLaunchConfiguration({') < main.indexOf('configureUserDataPath(app, {dataRoot:'));
  assert.ok(main.indexOf('app.requestSingleInstanceLock()') < main.indexOf('configureUserDataPath(app, {dataRoot:'));
});

test('test_acceptance_manifest_and_paths_validate_before_lock_or_runtime_side_effects', () => {
  const manifest = main.indexOf('loadBundledAcceptanceConfiguration({');
  const resolve = main.indexOf('resolveProductLaunchConfiguration({');
  const configure = main.indexOf('configureUserDataPath(app, {dataRoot: productStoragePaths.root})');
  const lock = main.indexOf('app.requestSingleInstanceLock()');
  const log = main.indexOf("writeDesktopEvent('electron-startup'");
  const spawn = main.indexOf('coreProcess = spawn(python');
  assert.ok(manifest >= 0 && manifest < resolve && resolve < lock && lock < configure);
  assert.ok(lock < log && lock < spawn);
  assert.match(runtimeConfig, /SEALED_ACCEPTANCE_ROOT = '\/private\/tmp'/);
  assert.match(runtimeConfig, /config\.acceptanceMode !== true/);
  assert.match(runtimeConfig, /requireBuildMarker/);
  assert.match(runtimeConfig, /error\?\.code === 'ENOENT' && acceptanceBuild/);
  assert.match(runtimeConfig, /assertAcceptancePathsContained\(productPaths, fsModule\)/);
});

test('test_finder_launch_arguments_resolve_overrides_before_storage_and_backend_initialization', () => {
  const lock = main.indexOf('app.requestSingleInstanceLock()');
  const resolve = main.indexOf('resolveProductLaunchConfiguration({');
  const configure = main.indexOf('configureUserDataPath(app, {dataRoot:');
  const spawn = main.indexOf('coreProcess = spawn(python');
  assert.ok(resolve >= 0 && resolve < lock && lock < configure && configure < spawn);
  assert.match(main, /argv: process\.argv\.slice\(1\)/);
  assert.match(runtimeConfig, /--ai-meeting-room-data-root/);
  assert.match(runtimeConfig, /--ai-meeting-room-acceptance/);
  assert.match(runtimeConfig, /--ai-meeting-room-port/);
  assert.match(runtimeConfig, /--ai-meeting-room-cao-url/);
});

test('test_second_instance_lock_is_acquired_before_backend_or_data_initialization', () => {
  const lock = main.indexOf('app.requestSingleInstanceLock()');
  assert.ok(lock < main.indexOf('configureUserDataPath(app'));
  assert.ok(lock < main.indexOf('writeDesktopEvent(\'electron-startup\''));
  assert.ok(lock < main.indexOf('spawn(python'));
});

test('sealed acceptance Electron lock identity is unique before requesting the lock', () => {
  const acceptanceIdentity = main.indexOf('app.setName(`AI Meeting Room Acceptance ${path.basename(productStoragePaths.root)}`)');
  const lock = main.indexOf('app.requestSingleInstanceLock()');
  assert.ok(acceptanceIdentity >= 0 && acceptanceIdentity < lock);
  assert.match(main, /if \(startupConfigurationReady && launchConfiguration\?\.acceptanceMode\) \{\s+app\.setName\(/);
  assert.match(main, /the OS-facing\s+\/\/ product name and normal Release instance identity remain unchanged/);
});

test('test_losing_instance_quits_immediately_without_shared_profile_initialization', () => {
  const lock = main.indexOf('app.requestSingleInstanceLock()');
  const configure = main.indexOf('configureUserDataPath(app, {dataRoot: productStoragePaths.root})');
  const loser = main.match(/else if \(!singleInstanceLock\) \{([\s\S]*?)\n\}/)?.[1] || '';
  assert.ok(lock >= 0 && configure > lock);
  assert.match(loser, /app\.quit\(\)/);
  assert.doesNotMatch(loser, /app\.whenReady\(\)/);
  assert.doesNotMatch(loser, /writeDesktopEvent|spawn\(/);
});

test('test_second_instance_does_not_open_data_paths_or_spawn_backend', () => {
  const lock = main.indexOf('app.requestSingleInstanceLock()');
  const rootResolution = main.indexOf('resolveProductLaunchConfiguration({');
  const configure = main.indexOf('configureUserDataPath(app, {dataRoot: productStoragePaths.root})');
  const loserQuit = main.indexOf('else if (!singleInstanceLock)');
  assert.ok(rootResolution >= 0 && lock > rootResolution && configure > lock && loserQuit > lock);
  assert.match(main, /else if \(!singleInstanceLock\) \{\s+app\.quit\(\);\s+\}/);
  assert.ok(main.indexOf('loadBundledAcceptanceConfiguration') < main.indexOf('resolveProductLaunchConfiguration({'));
  assert.match(main, /if \(singleInstanceLock\) \{[\s\S]*?app\.whenReady\(\)\.then/);
  assert.match(main, /if \(!singleInstanceLock \|\| !productStoragePaths\) return;[\s\S]*writeDesktopEvent\('electron-shutdown'/);
});

test('test_renderer_acceptance_marker_is_opt_in_and_stays_under_runtime_root', () => {
  assert.match(runtimeConfig, /AI_MEETING_ROOM_ACCEPTANCE_MODE/);
  assert.match(main, /launchConfiguration\?\.acceptanceMode && productStoragePaths/);
  assert.match(main, /writeRendererAcceptanceMarker\(/);
  assert.match(main, /did-finish-load/);
  assert.match(main, /ready-to-show/);
  assert.match(main, /render-process-gone/);
  assert.match(runtimeConfig, /renderer-acceptance\.json/);
});

test('local V1 package builder uses the existing Electron runtime', () => {
  const builder = fs.readFileSync(path.join(root, 'scripts', 'build_v1_app.py'), 'utf8');
  assert.match(builder, /desktop.*node_modules.*electron.*dist.*Electron\.app/s);
  assert.match(builder, /AI Meeting Room\.app/);
  assert.match(builder, /AI Meeting Room V1\.app/);
  assert.match(builder, /refusing to overwrite existing package/);
  assert.match(builder, /--output/);
  assert.match(builder, /--acceptance-data-root/);
  assert.match(builder, /AIMRAcceptanceBuild/);
  assert.match(builder, /"acceptanceMode": True/);
  assert.match(builder, /aimr-acceptance-config\.json/);
  assert.match(main, /loadBundledAcceptanceConfiguration/);
  assert.match(main, /packagedAcceptanceConfig/);
  assert.ok(main.indexOf('loadBundledAcceptanceConfiguration') < main.indexOf('resolveProductLaunchConfiguration({'));
  assert.match(builder, /\.env/);
  assert.match(builder, /chrome-brain-profile/);
  assert.match(builder, /ai_meeting_room.*VERSION/s);
  assert.doesNotMatch(builder, /curl|npm install|brew install/);
});
