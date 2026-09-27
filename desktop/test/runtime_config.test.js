const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {
  GPT_BRAIN_PARTITION,
  APP_USER_DATA_DIR_NAME,
  resolveUserDataPath,
  resolveUserDataPaths,
  resolveProductStoragePaths,
  resolveProductLaunchConfiguration,
  loadBundledAcceptanceConfiguration,
  createBackendEnvironment,
  writeRendererAcceptanceMarker,
  ProductPathConfigurationError,
  configureUserDataPath,
  getPersistentBrainSession,
  getIsolatedTestPartition,
  resetRuntimeConfigForTests,
} = require('../runtime_config');

const desktopRoot = path.join(__dirname, '..');

test('test_desktop_launcher_uses_its_own_source_directory', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', '..', '启动 AI Meeting Room.command'), 'utf8');
  assert.match(source, /PROJECT_DIR="\$\{0:A:h\}"/);
  assert.doesNotMatch(source, /\/Users\/[^/]+\/Documents\/ChatGPT\/软件开发/);
  assert.doesNotMatch(source, /--user-data-dir|NODE_ENV=test|AIMR_DESKTOP_BRAIN_POC_ON_START=1/);
});

test('test_gpt_brain_partition_is_persistent', () => {
  assert.match(GPT_BRAIN_PARTITION, /^persist:/);
});

test('test_gpt_brain_partition_name_is_constant', () => {
  const main = fs.readFileSync(path.join(desktopRoot, 'main.js'), 'utf8');
  assert.match(main, /GPT_BRAIN_PARTITION/);
  assert.doesNotMatch(main, /fromPartition\(['"]persist:chatgpt-brain['"]\)/);
});

test('test_user_data_configured_before_session_creation', () => {
  resetRuntimeConfigForTests();
  const calls = [];
  const fakeApp = {
    getPath(name) { assert.equal(name, 'appData'); return '/Users/test/Library/Application Support'; },
    setPath(name, value) { calls.push([name, value]); },
  };
  const fakeSession = {fromPartition: partition => ({partition})};
  const configured = configureUserDataPath(fakeApp);
  assert.equal(configured, path.join('/Users/test/Library/Application Support', APP_USER_DATA_DIR_NAME));
  assert.deepEqual(calls, [['userData', configured], ['sessionData', configured]]);
  assert.deepEqual(getPersistentBrainSession(fakeSession), {partition: GPT_BRAIN_PARTITION});
  assert.throws(() => configureUserDataPath(fakeApp), /TOO_LATE/);
  resetRuntimeConfigForTests();
});

test('test_explicit_product_data_root_contains_electron_user_data_session_and_cache', () => {
  const paths = resolveUserDataPaths({dataRoot: '/private/tmp/aimr-isolated'});
  assert.deepEqual(paths, {
    userData: path.join('/private/tmp/aimr-isolated', 'electron', 'user-data'),
    sessionData: path.join('/private/tmp/aimr-isolated', 'electron', 'user-data'),
    cache: path.join('/private/tmp/aimr-isolated', 'electron', 'cache'),
  });
});

test('test_explicit_product_data_root_is_applied_to_all_electron_storage_paths', () => {
  resetRuntimeConfigForTests();
  const calls = [];
  const fakeApp = {
    getPath(name) { return name === 'appData' ? '/Users/test/Library/Application Support' : '/unused'; },
    setPath(name, value) { calls.push([name, value]); },
  };
  const expected = resolveUserDataPaths({dataRoot: '/private/tmp/aimr-isolated'});
  configureUserDataPath(fakeApp, {dataRoot: '/private/tmp/aimr-isolated'});
  assert.deepEqual(calls, [
    ['userData', expected.userData],
    ['sessionData', expected.sessionData],
    ['cache', expected.cache],
  ]);
  resetRuntimeConfigForTests();
});

test('test_explicit_root_resolves_all_product_storage_under_one_root', () => {
  const paths = resolveProductStoragePaths({dataRoot: '/private/tmp/aimr-root'});
  assert.equal(paths.database, path.join(paths.root, 'meeting.db'));
  for (const key of ['logs', 'backups', 'diagnostics', 'runtime', 'electron']) {
    assert.equal(path.dirname(paths[key]), paths.root);
  }
});

test('test_acceptance_mode_fails_closed_without_explicit_product_root', () => {
  assert.throws(
    () => resolveProductStoragePaths({home: '/private/tmp/fake-home', requireExplicitRoot: true}),
    error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_DATA_ROOT_OVERRIDE_REQUIRED',
  );
});

test('test_finder_launch_acceptance_args_preserve_root_port_and_cao_url_with_sanitized_environment', () => {
  const os = require('node:os');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-finder-acceptance-'));
  try {
    const launch = resolveProductLaunchConfiguration({
      argv: [
        `--ai-meeting-room-data-root=${root}`,
        '--ai-meeting-room-acceptance',
        '--ai-meeting-room-port=18765',
        '--ai-meeting-room-cao-url=http://127.0.0.1:19889',
      ],
      environment: {HOME: '/Users/test', PATH: '/usr/bin:/bin'},
    });
    assert.equal(launch.acceptanceMode, true);
    assert.equal(launch.productPort, 18765);
    assert.equal(launch.caoBaseUrl, 'http://127.0.0.1:19889');
    assert.equal(launch.productPaths.root, fs.realpathSync(root));
    const child = createBackendEnvironment(
      {HOME: '/Users/test', PATH: '/usr/bin:/bin'},
      launch.productPaths,
      launch,
    );
    assert.equal(child.AI_MEETING_ROOM_DATA_DIR, launch.productPaths.root);
    assert.equal(child.AI_MEETING_ROOM_DB, path.join(launch.productPaths.root, 'meeting.db'));
    assert.equal(child.AI_MEETING_ROOM_ACCEPTANCE_MODE, '1');
    assert.equal(child.AIMR_PRODUCT_PORT, '18765');
    assert.equal(child.CAO_BASE_URL, 'http://127.0.0.1:19889');
  } finally {
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('test_finder_acceptance_fails_closed_if_launch_services_drops_both_environment_and_root_argument', () => {
  assert.throws(
    () => resolveProductLaunchConfiguration({
      argv: ['--ai-meeting-room-acceptance'],
      environment: {HOME: '/Users/test', PATH: '/usr/bin:/bin'},
    }),
    error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_DATA_ROOT_OVERRIDE_REQUIRED',
  );
});

test('test_bundled_acceptance_manifest_preserves_clean_room_when_gui_drops_launch_arguments', () => {
  const os = require('node:os');
  const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-packaged-acceptance-'));
  const root = fs.mkdtempSync('/private/tmp/aimr-packaged-data-');
  try {
    const manifestPath = path.join(resourcesPath, 'aimr-acceptance-config.json');
    fs.writeFileSync(manifestPath, JSON.stringify({
      schemaVersion: 'ai-meeting-room.acceptance-config.v1',
      acceptanceMode: true,
      dataRoot: root,
      productPort: 18765,
      caoBaseUrl: 'http://127.0.0.1:19889',
    }));
    const packagedAcceptanceConfig = loadBundledAcceptanceConfiguration({resourcesPath});
    const launch = resolveProductLaunchConfiguration({
      argv: [],
      environment: {HOME: '/Users/test'},
      packagedAcceptanceConfig,
    });
    assert.equal(launch.acceptanceMode, true);
    assert.equal(launch.productPort, 18765);
    assert.equal(launch.caoBaseUrl, 'http://127.0.0.1:19889');
    assert.equal(launch.productPaths.root, fs.realpathSync(root));
    const child = createBackendEnvironment({}, launch.productPaths, launch);
    assert.equal(child.AI_MEETING_ROOM_ACCEPTANCE_MODE, '1');
    assert.equal(child.AI_MEETING_ROOM_DATA_DIR, fs.realpathSync(root));
    assert.equal(child.AIMR_PRODUCT_PORT, '18765');
  } finally {
    fs.rmSync(resourcesPath, {recursive: true, force: true});
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('test_r70_acceptance_session_namespace_flows_only_to_sealed_backend', () => {
  const root = fs.mkdtempSync('/private/tmp/aimr-test-session-root-');
  const sessionPrefix = 'aimr-v1-r70-a50cb28352-';
  try {
    const launch = resolveProductLaunchConfiguration({
      argv: [],
      environment: {HOME: '/Users/test'},
      packagedAcceptanceConfig: {
        acceptanceMode: true,
        dataRoot: root,
        productPort: 62116,
        caoBaseUrl: 'http://127.0.0.1:9889',
        agentSessionPrefix: sessionPrefix,
      },
    });
    assert.equal(launch.agentSessionPrefix, sessionPrefix);
    const child = createBackendEnvironment({HOME: '/Users/test'}, launch.productPaths, launch);
    assert.equal(child.AI_MEETING_ROOM_ACCEPTANCE_SESSION_PREFIX, sessionPrefix);
    assert.equal(child.AI_MEETING_ROOM_ACCEPTANCE_MODE, '1');
  } finally {
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('test_bundled_acceptance_manifest_requires_true_mode_and_private_tmp_containment', () => {
  const os = require('node:os');
  const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-r69-manifest-'));
  const root = fs.mkdtempSync('/private/tmp/aimr-test-data-');
  const manifestPath = path.join(resourcesPath, 'aimr-acceptance-config.json');
  const manifest = {
    schemaVersion: 'ai-meeting-room.acceptance-config.v1',
    acceptanceMode: true,
    dataRoot: root,
    productPort: 18765,
    caoBaseUrl: 'http://127.0.0.1:19889',
  };
  try {
    fs.writeFileSync(manifestPath, JSON.stringify({...manifest, acceptanceMode: false}));
    assert.throws(
      () => loadBundledAcceptanceConfiguration({resourcesPath}),
      error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_ACCEPTANCE_CONFIG_INVALID',
    );

    fs.writeFileSync(manifestPath, JSON.stringify({...manifest, acceptanceMode: undefined}));
    assert.throws(
      () => loadBundledAcceptanceConfiguration({resourcesPath}),
      error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_ACCEPTANCE_CONFIG_INVALID',
    );

    fs.writeFileSync(manifestPath, JSON.stringify({...manifest, dataRoot: '/Users/test/ai-meeting-room-data'}));
    assert.throws(
      () => loadBundledAcceptanceConfiguration({resourcesPath}),
      error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_ACCEPTANCE_CONFIG_INVALID',
    );

    const outsideRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-r69-outside-'));
    const symlink = path.join('/private/tmp', `aimr-r69-symlink-${process.pid}`);
    try {
      fs.symlinkSync(outsideRoot, symlink, 'dir');
      fs.writeFileSync(manifestPath, JSON.stringify({...manifest, dataRoot: symlink}));
      assert.throws(
        () => loadBundledAcceptanceConfiguration({resourcesPath}),
        error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_ACCEPTANCE_CONFIG_INVALID',
      );
    } finally {
      fs.rmSync(symlink, {force: true});
      fs.rmSync(outsideRoot, {recursive: true, force: true});
    }

    fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    assert.equal(loadBundledAcceptanceConfiguration({resourcesPath}).acceptanceMode, true);
  } finally {
    fs.rmSync(resourcesPath, {recursive: true, force: true});
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('test_packaged_acceptance_build_fails_closed_when_manifest_is_missing', () => {
  const os = require('node:os');
  const contentsPath = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-r69-sealed-marker-'));
  const resourcesPath = path.join(contentsPath, 'Resources');
  fs.mkdirSync(resourcesPath);
  try {
    fs.writeFileSync(path.join(contentsPath, 'Info.plist'), [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<plist version="1.0"><dict><key>AIMRAcceptanceBuild</key><true/></dict></plist>',
    ].join('\n'));
    assert.throws(
      () => loadBundledAcceptanceConfiguration({resourcesPath, requireBuildMarker: true}),
      error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_ACCEPTANCE_CONFIG_INVALID',
    );
  } finally {
    fs.rmSync(contentsPath, {recursive: true, force: true});
  }
});

test('test_packaged_release_marker_allows_release_without_acceptance_manifest', () => {
  const os = require('node:os');
  const contentsPath = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-r69-release-marker-'));
  const resourcesPath = path.join(contentsPath, 'Resources');
  fs.mkdirSync(resourcesPath);
  try {
    fs.writeFileSync(path.join(contentsPath, 'Info.plist'), [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<plist version="1.0"><dict><key>AIMRAcceptanceBuild</key><false/></dict></plist>',
    ].join('\n'));
    assert.equal(loadBundledAcceptanceConfiguration({resourcesPath, requireBuildMarker: true}), null);
  } finally {
    fs.rmSync(contentsPath, {recursive: true, force: true});
  }
});

test('test_bundled_acceptance_rejects_electron_cache_symlink_escape_before_configuration', () => {
  const os = require('node:os');
  const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-r69-cache-manifest-'));
  const root = fs.mkdtempSync('/private/tmp/aimr-test-cache-root-');
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-r69-cache-outside-'));
  try {
    fs.mkdirSync(path.join(root, 'electron'), {recursive: true});
    fs.symlinkSync(outside, path.join(root, 'electron', 'cache'), 'dir');
    fs.writeFileSync(path.join(resourcesPath, 'aimr-acceptance-config.json'), JSON.stringify({
      schemaVersion: 'ai-meeting-room.acceptance-config.v1',
      acceptanceMode: true,
      dataRoot: root,
      productPort: 18765,
      caoBaseUrl: 'http://127.0.0.1:19889',
    }));
    const packagedAcceptanceConfig = loadBundledAcceptanceConfiguration({resourcesPath});
    assert.throws(
      () => resolveProductLaunchConfiguration({
        argv: [],
        environment: {HOME: '/Users/test'},
        packagedAcceptanceConfig,
      }),
      error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT',
    );
  } finally {
    fs.rmSync(resourcesPath, {recursive: true, force: true});
    fs.rmSync(root, {recursive: true, force: true});
    fs.rmSync(outside, {recursive: true, force: true});
  }
});

test('test_bundled_acceptance_manifest_conflicts_fail_closed_before_paths_are_used', () => {
  const os = require('node:os');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-packaged-data-'));
  try {
    assert.throws(
      () => resolveProductLaunchConfiguration({
        argv: ['--ai-meeting-room-data-root=/private/tmp/conflicting-root'],
        environment: {HOME: '/Users/test'},
        packagedAcceptanceConfig: {
          dataRoot: root,
          productPort: 18765,
          caoBaseUrl: 'http://127.0.0.1:19889',
        },
      }),
      error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_LAUNCH_CONFIGURATION_CONFLICT',
    );
  } finally {
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('test_invalid_bundled_acceptance_manifest_fails_closed', () => {
  const os = require('node:os');
  const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-invalid-acceptance-'));
  try {
    fs.writeFileSync(path.join(resourcesPath, 'aimr-acceptance-config.json'), '{invalid json');
    assert.throws(
      () => loadBundledAcceptanceConfiguration({resourcesPath}),
      error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_ACCEPTANCE_CONFIG_INVALID',
    );
  } finally {
    fs.rmSync(resourcesPath, {recursive: true, force: true});
  }
});

test('test_conflicting_environment_and_finder_root_arguments_fail_closed', () => {
  const os = require('node:os');
  const first = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-root-first-'));
  const second = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-root-second-'));
  try {
    assert.throws(
      () => resolveProductLaunchConfiguration({
        argv: [`--ai-meeting-room-data-root=${second}`, '--ai-meeting-room-acceptance'],
        environment: {HOME: '/Users/test', AI_MEETING_ROOM_DATA_DIR: first},
      }),
      error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_LAUNCH_CONFIGURATION_CONFLICT',
    );
  } finally {
    fs.rmSync(first, {recursive: true, force: true});
    fs.rmSync(second, {recursive: true, force: true});
  }
});

test('test_explicit_root_rejects_existing_symlink_escape_for_every_product_path', () => {
  const os = require('node:os');
  const fs = require('node:fs');
  for (const key of ['database', 'logs', 'backups', 'diagnostics', 'runtime', 'electron']) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-path-containment-'));
    const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-path-outside-'));
    try {
      const name = key === 'database' ? 'meeting.db' : key;
      const target = path.join(root, name);
      if (key !== 'database') fs.mkdirSync(outside, {recursive: true});
      fs.symlinkSync(outside, target, key === 'database' ? 'file' : 'dir');
      assert.throws(
        () => resolveProductStoragePaths({dataRoot: root}),
        error => error instanceof ProductPathConfigurationError && error.code === 'PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT',
        `expected ${key} escape to be rejected`,
      );
    } finally {
      fs.rmSync(root, {recursive: true, force: true});
      fs.rmSync(outside, {recursive: true, force: true});
    }
  }
});

test('test_backend_child_receives_same_root_as_electron', () => {
  const paths = resolveProductStoragePaths({dataRoot: '/private/tmp/aimr-root'});
  const environment = createBackendEnvironment({PATH: '/usr/bin:/bin'}, paths);
  assert.equal(environment.AI_MEETING_ROOM_DATA_DIR, paths.root);
  assert.equal(environment.AI_MEETING_ROOM_DB, paths.database);
  assert.equal(environment.PATH, '/usr/bin:/bin');
});

test('test_renderer_acceptance_marker_is_opt_in_and_metadata_only', () => {
  const fs = require('node:fs');
  const os = require('node:os');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'aimr-renderer-marker-'));
  try {
    const paths = resolveProductStoragePaths({dataRoot: root});
    const state = {rendererCreated: true, didFinishLoad: true, readyToShow: true, processId: 1234};
    assert.equal(writeRendererAcceptanceMarker({enabled: false, paths, state}), false);
    assert.equal(fs.existsSync(path.join(paths.runtime, 'renderer-acceptance.json')), false);
    assert.equal(writeRendererAcceptanceMarker({enabled: true, paths, state}), true);
    const marker = JSON.parse(fs.readFileSync(path.join(paths.runtime, 'renderer-acceptance.json'), 'utf8'));
    assert.deepEqual(Object.keys(marker).sort(), ['didFinishLoad', 'processId', 'readyToShow', 'rendererCreated', 'timestamp']);
    assert.deepEqual({...marker, timestamp: undefined}, {...state, timestamp: undefined});
  } finally {
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('test_production_startup_does_not_clear_gpt_storage', () => {
  const main = fs.readFileSync(path.join(desktopRoot, 'main.js'), 'utf8');
  assert.doesNotMatch(main, /clearStorageData|clearCache|flushStorageData|removeSessionData/);
});

test('test_test_partition_cannot_touch_production_partition', () => {
  const isolated = getIsolatedTestPartition('auth-regression');
  assert.equal(isolated, 'persist:chatgpt-brain-test-auth-regression');
  assert.notEqual(isolated, GPT_BRAIN_PARTITION);
  assert.match(isolated, /^persist:chatgpt-brain-test-/);
});

test('test_v1_app_start_keeps_experimental_chrome_brain_opt_in', () => {
  const main = fs.readFileSync(path.join(desktopRoot, 'main.js'), 'utf8');
  assert.match(main, /startFormalChromeBrain/);
  assert.match(main, /api\/brain\/real-chrome\/open/);
  const boot = main.slice(main.indexOf('async function boot()'), main.indexOf('const singleInstanceLock'));
  assert.doesNotMatch(boot, /startFormalChromeBrain\s*\(/);
  assert.match(main, /ipcMain\.handle\('brain:open'/);
});

test('test_explicit_runtime_connect_still_reports_runtime_health', () => {
  const main = fs.readFileSync(path.join(desktopRoot, 'main.js'), 'utf8');
  assert.match(main, /startup state=/);
  const product = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  assert.match(product, /start_or_attach/);
  assert.match(product, /CHROME_PROCESS_LOST/);
});

test('test_boot_skips_experimental_brain_probe_by_default', () => {
  const main = fs.readFileSync(path.join(desktopRoot, 'main.js'), 'utf8');
  const boot = main.slice(main.indexOf('async function boot()'));
  assert.doesNotMatch(boot, /startFormalChromeBrain\s*\(/);
  assert.ok(boot.indexOf('Do not launch, attach, or probe') < boot.indexOf('createProductWindow()'));
});

test('test_chrome_start_failure_not_mapped_dom_unknown', () => {
  const brain = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  assert.match(brain, /CHROME_EXECUTABLE_NOT_FOUND/);
  assert.match(brain, /set_stage\(ChromeBrainStartupStage\.FAILED/);
});

test('test_dynamic_chrome_port_uses_devtools_active_port', () => {
  const brain = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  assert.match(brain, /--remote-debugging-port=0/);
  assert.match(brain, /DevToolsActivePort/);
  assert.match(brain, /_discover_cdp_endpoint/);
});

test('test_cdp_missing_not_mapped_dom_unknown', () => {
  const brain = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  assert.match(brain, /CDP_ENDPOINT_NOT_FOUND|DEDICATED_CHROME_NOT_STARTED|DEVTOOLS_START_TIMEOUT/);
  assert.match(brain, /CHROME_CDP_UNAVAILABLE|DEDICATED_CHROME_NOT_STARTED/);
});

test('test_cdp_connect_failure_not_mapped_dom_unknown', () => {
  const brain = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  assert.match(brain, /CONNECTING_CDP/);
  assert.match(brain, /cdp_connected = True/);
});

test('test_no_chatgpt_target_opens_chatgpt_page', () => {
  const brain = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  assert.match(brain, /CHATGPT_TARGET_NOT_FOUND/);
  assert.match(brain, /self\.page\.goto\(CHATGPT_URL/);
});

test('test_manual_reconnect_reuses_same_profile', () => {
  const product = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'product', 'server.py'), 'utf8');
  const brain = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  assert.match(product, /重新连接 GPT 主脑/);
  assert.match(product, /reconnectGptBrain/);
  assert.match(brain, /--user-data-dir=/);
});

test('test_manual_reconnect_does_not_spawn_duplicate_chrome', () => {
  const brain = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  assert.match(brain, /_owned_profile_process_exists/);
  assert.match(brain, /self\._adopted = True/);
});

test('test_raw_cdp_forensics_are_archived_outside_formal_connection_details', () => {
  const brain = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  const product = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'product', 'server.py'), 'utf8');
  assert.match(brain, /chromeStderrTail/);
  assert.match(brain, /chromeStartTimeline/);
  assert.match(brain, /chromeExitSignal/);
  assert.match(product, /用户已授权的 Google Chrome 会话/);
  assert.match(product, /playwright\/status/);
  assert.doesNotMatch(product, /Chrome退出代码|Chrome stderr尾部|启动时间线/);
});

test('test_chrome_forensics_keep_launch_args_secret_safe', () => {
  const brain = fs.readFileSync(path.join(desktopRoot, '..', 'ai_meeting_room', 'brain', 'chrome_brain.py'), 'utf8');
  assert.match(brain, /enable-logging=stderr/);
  assert.match(brain, /SafeChromeOutputTail/);
  assert.match(brain, /redacted/);
  assert.doesNotMatch(brain, /storage_state|document\.cookie|Authorization/);
});
