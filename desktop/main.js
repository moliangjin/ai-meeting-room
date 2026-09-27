const {app, BrowserWindow, ipcMain, session, dialog} = require('electron');
const {spawn, spawnSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const {randomUUID} = require('node:crypto');
const {readProductBundleMarker, hasSealedAcceptanceMarker} = require('./native_acceptance');
const {
  ChatGPTDesktopBrainHost,
  allowedUrl,
  allowedAuthUrl,
  safeUrl,
  OAUTH_ORIGINS,
} = require('./brain_host');
const {DesktopPocRunManager} = require('./poc_run_state');
const {assertIpcSerializable, serializeError} = require('./ipc_serialization');
const {
  GPT_BRAIN_PARTITION,
  AUTH_RECOVERY_WAIT_MS,
  createBackendEnvironment,
  configureUserDataPath,
  getPersistentBrainSession,
  loadBundledAcceptanceConfiguration,
  ProductPathConfigurationError,
  resolveProductLaunchConfiguration,
  writeRendererAcceptanceMarker,
} = require('./runtime_config');

let productPathConfigurationError = null;
let launchConfiguration = null;
let productStoragePaths = null;
let bundledAcceptanceConfiguration = null;
const officialBundleMarker = readProductBundleMarker(process.resourcesPath);
const officialProductBundle = officialBundleMarker !== null;
try {
  bundledAcceptanceConfiguration = loadBundledAcceptanceConfiguration({
    resourcesPath: process.resourcesPath,
    requireBuildMarker: app.isPackaged || officialProductBundle,
  });
  launchConfiguration = resolveProductLaunchConfiguration({
    argv: process.argv.slice(1),
    environment: process.env,
    packagedAcceptanceConfig: bundledAcceptanceConfiguration,
    home: os.homedir(),
  });
  productStoragePaths = launchConfiguration.productPaths;
} catch (error) {
  productPathConfigurationError = error;
}
const startupConfigurationReady = !productPathConfigurationError && Boolean(productStoragePaths);
const sealedNativeAcceptanceBundle = Boolean(bundledAcceptanceConfiguration &&
  hasSealedAcceptanceMarker(process.resourcesPath));
// Resolve and validate bootstrap paths without opening product state. A losing
// process must acquire no shared Electron profile before it exits.
// A sealed acceptance build may coexist with the installed desktop app. Scope
// Electron's pre-userData single-instance identity to its validated private
// acceptance root. This changes Electron's internal name only; the OS-facing
// product name and normal Release instance identity remain unchanged.
if (startupConfigurationReady && launchConfiguration?.acceptanceMode) {
  app.setName(`AI Meeting Room Acceptance ${path.basename(productStoragePaths.root)}`);
}
const singleInstanceLock = startupConfigurationReady && app.requestSingleInstanceLock();
if (startupConfigurationReady && launchConfiguration?.acceptanceMode) {
  console.log(`[desktop] single-instance-lock=${singleInstanceLock ? 'acquired' : 'denied'}`);
}
// A losing process must quit before Electron initializes Chromium against the
// primary process's user-data directory.
if (!startupConfigurationReady) {
  app.whenReady().then(() => {
    const code = String(productPathConfigurationError?.code || 'PRODUCT_DATA_PATHS_UNAVAILABLE');
    const message = code === 'PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT'
      ? '测试/指定数据目录已启用，但某个产品数据路径解析到了该目录之外，已阻止启动以保护现有数据。'
      : '产品数据目录无法安全解析，已阻止启动以保护现有数据。';
    dialog.showErrorBox('AI Meeting Room 启动失败', message);
    app.quit();
  });
} else if (!singleInstanceLock) {
  app.quit();
} else {
  try {
    configureUserDataPath(app, {dataRoot: productStoragePaths.root});
  } catch (error) {
    productPathConfigurationError = error;
    app.whenReady().then(() => {
      const code = String(productPathConfigurationError?.code || 'PRODUCT_DATA_PATHS_UNAVAILABLE');
      const message = code === 'PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT'
        ? '测试/指定数据目录已启用，但某个产品数据路径解析到了该目录之外，已阻止启动以保护现有数据。'
        : '产品数据目录无法安全解析，已阻止启动以保护现有数据。';
      dialog.showErrorBox('AI Meeting Room 启动失败', message);
      app.quit();
    });
  }
}
const PRODUCT_PORT = launchConfiguration?.productPort || 8765;
const PRODUCT_URL = `http://127.0.0.1:${PRODUCT_PORT}/?desktop=1`;
const CHATGPT_URL = 'https://chatgpt.com/';
const FORMAL_BRAIN_STATUS_PATH = '/api/brain/real-chrome/status';
const FORMAL_BRAIN_OPEN_PATH = '/api/brain/real-chrome/open';
const FORMAL_BRAIN_POC_PATH = '/api/brain/real-chrome-poc';
const FORMAL_RUNTIME_OWNER = 'PRODUCT_SHELL';
// A real attach may spend time in a read-only DOM readiness/composer check.
// Keep this transport timeout configurable and longer than the historical
// 10-second value so a business result can cross the boundary intact.
const FORMAL_BRAIN_OPEN_TIMEOUT_MS = Number(process.env.AIMR_BRAIN_OPEN_TIMEOUT_MS || 60000);
const FORMAL_BRAIN_POC_TIMEOUT_MS = Number(process.env.AIMR_BRAIN_POC_TIMEOUT_MS || 190000);
const RUNTIME_IDENTITY_PATH = '/api/runtime/identity';
const RUNTIME_SHUTDOWN_PATH = '/api/runtime/shutdown';
const launchSessionId = randomUUID();
let productWindow = null;
let brainWindow = null;
let brainHost = null;
let coreProcess = null;
let coreShutdownTimer = null;
const authWindows = new Set();
const desktopPocRuns = new DesktopPocRunManager();
const productDataRoot = productStoragePaths?.root || null;
const productLogPath = productStoragePaths?.database ? path.join(productStoragePaths.logs, 'product.log') : null;
const desktopLogPath = productStoragePaths?.database ? path.join(productStoragePaths.logs, 'desktop.log') : null;
const rendererAcceptanceState = {
  rendererCreated: false,
  didFinishLoad: false,
  readyToShow: false,
  processId: 0,
};
let productRendererAlive = false;
let nativeAcceptanceAuditStarted = false;

function maybeRunNativeAcceptanceAudit() {
  if (launchConfiguration?.acceptanceMode && productStoragePaths?.runtime) {
    fs.writeFileSync(path.join(productStoragePaths.runtime, 'native-layout-hook.json'),
      JSON.stringify({packaged: app.isPackaged,
        bundledConfigurationPresent: Boolean(bundledAcceptanceConfiguration),
        sealedBundleMarker: sealedNativeAcceptanceBundle,
        acceptanceMode: launchConfiguration.acceptanceMode,
        readyToShow: rendererAcceptanceState.readyToShow,
        didFinishLoad: rendererAcceptanceState.didFinishLoad,
        started: nativeAcceptanceAuditStarted}) + '\n', {mode: 0o600});
  }
  if (nativeAcceptanceAuditStarted || !sealedNativeAcceptanceBundle || !bundledAcceptanceConfiguration ||
      !launchConfiguration?.acceptanceMode || !rendererAcceptanceState.readyToShow ||
      !rendererAcceptanceState.didFinishLoad || !productWindow || productWindow.isDestroyed()) return;
  nativeAcceptanceAuditStarted = true;
  const writeStatus = (stage, error = null) => {
    const code = error ? String(error?.code || error?.message || 'UNKNOWN').replace(/[^A-Za-z0-9_]/g, '').slice(0, 100) : null;
    fs.writeFileSync(path.join(productStoragePaths.runtime, 'native-layout-status.json'),
      JSON.stringify({stage, code}) + '\n', {mode: 0o600});
  };
  try {
    writeStatus('STARTED');
    const {runNativeAcceptanceAudit} = require('./native_acceptance');
    runNativeAcceptanceAudit({window: productWindow, sealedBundle: sealedNativeAcceptanceBundle,
      bundledAcceptanceConfiguration, launchConfiguration, paths: productStoragePaths})
      .then(result => { writeStatus('COMPLETED'); console.log(`[desktop][native-acceptance] completed sizes=${result.sizes.length}`); })
      .catch(error => { writeStatus('FAILED', error); console.log('[desktop][native-acceptance] failed'); });
  } catch (error) {
    writeStatus('FAILED', error);
  }
}

function updateRendererAcceptanceState(changes = {}) {
  Object.assign(rendererAcceptanceState, changes);
  if (productWindow && !productWindow.isDestroyed()) {
    rendererAcceptanceState.processId = Number(productWindow.webContents.getOSProcessId()) || 0;
  }
  if (launchConfiguration?.acceptanceMode && productStoragePaths) {
    try {
      writeRendererAcceptanceMarker({enabled: true, paths: productStoragePaths, state: rendererAcceptanceState});
    } catch (_) {
      productRendererAlive = false;
      app.quit();
    }
  }
}

let quitting = false;

function resolvePythonExecutable() {
  const candidates = [
    process.env.AI_MEETING_ROOM_PYTHON,
    process.env.PYTHON,
    path.join(__dirname, '..', '.venv', 'bin', 'python3'),
    path.join(os.homedir(), '.local', 'bin', 'python3'),
    '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3',
    '/usr/bin/python3',
  ].filter(Boolean);
  for (const candidate of [...new Set(candidates)]) {
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      const probe = spawnSync(candidate, ['-c', 'import playwright'], {encoding: 'utf8', timeout: 5000});
      if (probe.status === 0) return candidate;
    } catch (_) {}
  }
  const failure = new Error('未找到已安装 Playwright 的 Python 3。请安装 Python 3 与项目依赖后重试。');
  failure.code = 'PYTHON_PLAYWRIGHT_UNAVAILABLE';
  throw failure;
}

function writeDesktopEvent(event, fields = {}) {
  if (!desktopLogPath) return;
  try {
    const logRoot = path.dirname(desktopLogPath);
    fs.mkdirSync(logRoot, {recursive: true});
    if (fs.existsSync(desktopLogPath) && fs.statSync(desktopLogPath).size > 2 * 1024 * 1024) {
      fs.renameSync(desktopLogPath, `${desktopLogPath}.1`);
    }
    const safeFields = {};
    for (const key of ['code', 'pid', 'exitCode', 'signal', 'launchSessionId', 'runtimeOwner']) {
      const value = fields[key];
      if (typeof value === 'number' && Number.isFinite(value)) safeFields[key] = value;
      else if (typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,120}$/.test(value)) safeFields[key] = value;
    }
    fs.appendFileSync(desktopLogPath, `${JSON.stringify({timestamp: new Date().toISOString(), event, ...safeFields})}\n`, 'utf8');
  } catch (_) {}
}

function emitDesktopBrainPocStatus(phase, detail = null, run = null) {
  if (!productWindow || productWindow.isDestroyed()) return;
  productWindow.webContents.send('brain:desktop-poc-status', {
    phase, detail, runId: run?.runId || null, startedAt: run?.startedAt || null,
  });
}

function healthCheck(url) {
  return new Promise(resolve => {
    const request = http.get(url, response => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on('error', () => resolve(false));
    request.setTimeout(800, () => { request.destroy(); resolve(false); });
  });
}

function tcpPortInUse(port) {
  return new Promise(resolve => {
    const socket = net.createConnection({host: '127.0.0.1', port});
    const finish = value => { socket.destroy(); resolve(value); };
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
    socket.setTimeout(500, () => finish(false));
  });
}

function postJson(url, payload, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const body = Buffer.from(JSON.stringify(payload), 'utf8');
    const request = http.request(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Content-Length': body.length},
      timeout: timeoutMs,
    }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => {
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
          if (response.statusCode < 200 || response.statusCode >= 300) {
            const error = value.error || {};
            const failure = new Error(error.message || value.error || 'Product Shell parser failed');
            failure.code = error.code || value.code || 'PRODUCT_SHELL_RESPONSE_ERROR';
            failure.stage = error.stage || 'T1_IPC_REQUEST_SENT';
            failure.attemptId = error.attemptId || payload?.connectAttemptId;
            failure.stackId = error.stackId;
            for (const field of ['formalRuntimeType', 'launchSessionId', 'registryInstanceId', 'hostInstanceId', 'processPid', 'parentPid', 'runtimeOwner', 'boundPageId', 'browserConnected', 'pageAlive', 'hostname', 'authState', 'composerReady', 'precheckResult', 'failureCode', 'invariantViolation', 'invariantDetails', 'pocState', 'pocFailure', 'pocFailureCode', 'pocFailureStage', 'pocFailureName', 'pocFailureMessage', 'pocFailureStackId']) {
              if (Object.prototype.hasOwnProperty.call(error, field)) failure[field] = error[field];
            }
            failure.diagnostics = Object.fromEntries(['formalRuntimeType', 'launchSessionId', 'registryInstanceId', 'hostInstanceId', 'processPid', 'parentPid', 'runtimeOwner', 'boundPageId', 'browserConnected', 'pageAlive', 'hostname', 'authState', 'composerReady', 'precheckResult', 'failureCode', 'invariantViolation', 'invariantDetails', 'pocState', 'pocFailure', 'pocFailureCode', 'pocFailureStage', 'pocFailureName', 'pocFailureMessage', 'pocFailureStackId']
              .filter(field => Object.prototype.hasOwnProperty.call(failure, field)).map(field => [field, failure[field]]));
            reject(failure);
          }
          else resolve(value);
        } catch (_) {
          const failure = new Error('Product Shell returned invalid JSON');
          failure.code = 'CONNECT_RESULT_SERIALIZATION_FAILED';
          failure.stage = 'T2_IPC_HANDLER_ENTER';
          failure.attemptId = payload?.connectAttemptId;
          reject(failure);
        }
      });
    });
    request.on('timeout', () => {
      const failure = new Error('Product Shell response timeout');
      failure.code = 'CONNECT_OPERATION_TIMEOUT';
      failure.stage = 'T3_BRAIN_CONNECT_START';
      failure.attemptId = payload?.connectAttemptId;
      request.destroy(failure);
    });
    request.on('error', reject);
    request.end(body);
  });
}

function writeBrainConnectTrace(item) {
  if (!productStoragePaths) return;
  try {
    const logRoot = path.join(productDataRoot, 'logs');
    const logPath = path.join(logRoot, 'brain-connect-debug.log');
    fs.mkdirSync(logRoot, {recursive: true});
    if (fs.existsSync(logPath) && fs.statSync(logPath).size > 2 * 1024 * 1024) {
      fs.renameSync(logPath, `${logPath}.1`);
    }
    const allowed = new Set([
      'attemptId', 'timestamp', 'stage', 'result', 'transport', 'runtimeOwner',
      'launchSessionId', 'processPid', 'parentPid', 'registryInstanceId',
      'hostInstanceId', 'boundPageId', 'MCP_PROCESS_STARTED', 'errorCode',
      'fieldPath', 'constructorName',
    ]);
    const safe = {};
    for (const [key, value] of Object.entries(item || {})) {
      if (!allowed.has(key) || value === null) continue;
      if (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) safe[key] = value;
      else if (typeof value === 'string' && /^[A-Za-z0-9_.:/-]{1,200}$/.test(value)) safe[key] = value;
    }
    const encoded = JSON.stringify(safe);
    fs.appendFileSync(logPath, `${encoded}\n`, 'utf8');
  } catch (_) {}
}

function ipcBrainError(error, attemptId) {
  return {
    ok: false,
    error: serializeError(error, attemptId),
  };
}

function safeIpcReturn(value, attemptId) {
  try {
    return assertIpcSerializable(value);
  } catch (error) {
    writeBrainConnectTrace({
      attemptId,
      timestamp: new Date().toISOString(),
      stage: 'T15_IPC_RESULT_SERIALIZATION',
      result: 'FAIL',
      errorCode: 'IPC_RESULT_NOT_SERIALIZABLE',
      fieldPath: String(error.fieldPath || 'result'),
      constructorName: String(error.constructorName || 'UNKNOWN'),
      MCP_PROCESS_STARTED: false,
    });
    return ipcBrainError(error, attemptId);
  }
}

function getJson(url, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, {timeout: timeoutMs}, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => {
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
          if (response.statusCode < 200 || response.statusCode >= 300) reject(new Error(value.error || 'Product Shell status failed'));
          else resolve(value);
        } catch (_) { reject(new Error('Product Shell status returned invalid JSON')); }
      });
    });
    request.on('timeout', () => request.destroy(new Error('Product Shell status timeout')));
    request.on('error', reject);
  });
}

function notifyBrainFailure(failure) {
  const state = String(failure?.state || 'UNKNOWN');
  const reason = String(failure?.reason || 'UNKNOWN');
  postJson(`http://127.0.0.1:${PRODUCT_PORT}/api/brain/failure`, {state, reason}, 3000)
    .then(result => console.log(`[desktop][safety] brain-failure state=${state} meetings=${result.observedMeetings?.length || 0}`))
    .catch(() => console.log(`[desktop][safety] brain-failure-notified=false state=${state}`));
}

function startCore() {
  if (!productStoragePaths) {
    const failure = productPathConfigurationError || new ProductPathConfigurationError('PRODUCT_DATA_PATHS_UNAVAILABLE');
    throw failure;
  }
  const projectRoot = path.resolve(__dirname, '..');
  writeDesktopEvent('product-shell-start-requested', {pid: process.pid, launchSessionId, runtimeOwner: FORMAL_RUNTIME_OWNER});
  const python = resolvePythonExecutable();
  coreProcess = spawn(python, [
    '-m', 'ai_meeting_room.product.serve',
    '--host', '127.0.0.1', '--port', String(PRODUCT_PORT), '--no-legacy-extension-bridge',
  ], {
    cwd: projectRoot,
    env: {
      ...createBackendEnvironment(process.env, productStoragePaths, launchConfiguration),
      PYTHONPATH: projectRoot,
      PYTHONDONTWRITEBYTECODE: '1',
      AI_MEETING_ROOM_LAUNCH_SESSION_ID: launchSessionId,
      AI_MEETING_ROOM_PROJECT_ROOT: projectRoot,
      AI_MEETING_ROOM_ELECTRON_PID: String(process.pid),
      APP_BUILD_ID: process.env.APP_BUILD_ID || 'UNTRACKED_WORKTREE',
      GIT_COMMIT: process.env.GIT_COMMIT || 'UNKNOWN',
      SOURCE_TIMESTAMP: process.env.SOURCE_TIMESTAMP || new Date().toISOString(),
      NODE_VERSION: process.version,
      ELECTRON_VERSION: process.versions.electron || 'UNKNOWN',
      // Safe diagnostics are available only from an unpackaged development
      // shell; packaged users never receive the dev-only probe route.
      AI_MEETING_ROOM_ENABLE_DEV_PROBES: officialProductBundle ? '0' : '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  // Backend output can contain exception/request material. The Product Shell
  // writes its own bounded metadata-only operational log; never persist raw
  // stdout/stderr from the child process.
  coreProcess.stdout.on('data', () => {});
  coreProcess.stderr.on('data', () => {});
  coreProcess.once('error', error => {
    writeDesktopEvent('product-shell-spawn-failed', {code: String(error?.code || 'SPAWN_FAILED')});
    if (!quitting) {
      dialog.showErrorBox('AI Meeting Room 启动失败', '本地服务无法启动，请查看产品诊断信息。');
      app.quit();
    }
  });
  coreProcess.once('exit', (exitCode, signal) => {
    writeDesktopEvent('product-shell-exit', {exitCode: Number.isInteger(exitCode) ? exitCode : -1, signal: signal || 'none'});
    coreProcess = null;
    if (!quitting) app.quit();
  });
}

async function waitForPortRelease(port, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await tcpPortInUse(port))) return true;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return !(await tcpPortInUse(port));
}

async function readProductRuntimeIdentity() {
  try {
    const identity = await getJson(`http://127.0.0.1:${PRODUCT_PORT}${RUNTIME_IDENTITY_PATH}`);
    if (!identity || typeof identity !== 'object' || !identity.launchSessionId) {
      throw new Error('Product Shell runtime identity is incomplete');
    }
    return identity;
  } catch (error) {
    const failure = new Error('Product Shell port owner could not be identified');
    failure.code = 'PRODUCT_SHELL_PORT_OCCUPIED_BY_UNKNOWN_PROCESS';
    failure.cause = error;
    throw failure;
  }
}

function sameProjectRoot(identity, projectRoot) {
  try {
    return path.resolve(String(identity.projectRoot || '')) === path.resolve(projectRoot)
      && path.resolve(String(identity.sourceRoot || identity.projectRoot || '')) === path.resolve(projectRoot);
  } catch (_) {
    return false;
  }
}

async function stopOwnedStaleProductShell(identity) {
  if (identity.runtimeOwner !== FORMAL_RUNTIME_OWNER || !identity.processPid) {
    const failure = new Error('Product Shell owner is not verifiable');
    failure.code = 'PRODUCT_SHELL_PORT_OCCUPIED_BY_UNKNOWN_PROCESS';
    throw failure;
  }
  await postJson(`http://127.0.0.1:${PRODUCT_PORT}${RUNTIME_SHUTDOWN_PATH}`, {}, 3000);
  if (!(await waitForPortRelease(PRODUCT_PORT))) {
    const failure = new Error('Previous Product Shell did not shut down');
    failure.code = 'STALE_PRODUCT_SHELL_SHUTDOWN_FAILED';
    throw failure;
  }
}

async function ensureOwnedProductShell() {
  const projectRoot = path.resolve(__dirname, '..');
  const running = await healthCheck(`http://127.0.0.1:${PRODUCT_PORT}/`);
  const occupied = running || await tcpPortInUse(PRODUCT_PORT);
  if (occupied) {
    const identity = await readProductRuntimeIdentity();
    if (identity.launchSessionId !== launchSessionId) {
      if (!sameProjectRoot(identity, projectRoot)) {
        const failure = new Error('Product Shell port belongs to an unknown project');
        failure.code = 'PRODUCT_SHELL_PORT_OCCUPIED_BY_UNKNOWN_PROCESS';
        throw failure;
      }
      await stopOwnedStaleProductShell(identity);
    }
  }
  if (!occupied || (await tcpPortInUse(PRODUCT_PORT)) === false) startCore();
  if (!(await waitForProductShell())) throw new Error('Product Shell did not become healthy');
  const identity = await readProductRuntimeIdentity();
  if (identity.launchSessionId !== launchSessionId) {
    const failure = new Error('Product Shell launch session does not belong to this Electron');
    failure.code = 'BACKEND_LAUNCH_SESSION_MISMATCH';
    throw failure;
  }
  if (!sameProjectRoot(identity, projectRoot)) {
    const failure = new Error('Product Shell source tree does not belong to this Electron');
    failure.code = 'LIVE_SOURCE_TREE_MISMATCH';
    throw failure;
  }
  return identity;
}

function stopCoreProcessGracefully() {
  const child = coreProcess;
  if (!child || child.killed) return;
  if (coreShutdownTimer) clearTimeout(coreShutdownTimer);
  const onExit = () => {
    if (coreShutdownTimer) clearTimeout(coreShutdownTimer);
    coreShutdownTimer = null;
  };
  child.once('exit', onExit);
  child.kill('SIGINT');
  coreShutdownTimer = setTimeout(() => {
    if (coreProcess === child && !child.killed) child.kill('SIGTERM');
  }, 2000);
}

async function waitForProductShell() {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    if (await healthCheck(`http://127.0.0.1:${PRODUCT_PORT}/`)) return true;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  return false;
}

async function startFormalChromeBrain() {
  // Historical function name retained for compatibility. Boot is passive;
  // the formal backend attaches to existing Chrome only after Connect.
  try {
    const status = await getJson(`http://127.0.0.1:${PRODUCT_PORT}${FORMAL_BRAIN_STATUS_PATH}`);
    console.log(`[desktop][attached-brain] passive startup state=${status.state || 'UNKNOWN'} auth=${status.authState || 'UNKNOWN'}`);
    return status;
  } catch (error) {
    console.error(`[desktop][attached-brain] passive status failed code=${error.code || error.message || 'UNKNOWN'}`);
    return {state: 'ERROR', authState: 'UNKNOWN', browserRuntime: 'PLAYWRIGHT_ATTACH_EXISTING_REAL_CHROME', browserAlive: false};
  }
}

function isOAuthOrigin(rawUrl) {
  try { return OAUTH_ORIGINS.has(new URL(rawUrl).origin); } catch (_) { return false; }
}

function logNavigationDecision(kind, url, allowed) {
  try {
    const parsed = new URL(url);
    console.log(`[desktop][auth-diagnostic] navigation-type=${kind} allowed=${allowed} hostname=${parsed.hostname} pathname=${parsed.pathname || '/'}`);
  } catch (_) {
    console.log(`[desktop][auth-diagnostic] navigation-type=${kind} allowed=${allowed} hostname=INVALID pathname=INVALID`);
  }
}

function applyNavigationPolicy(win, {authWindow = false, brainSession = null} = {}) {
  win.webContents.on('will-navigate', (event, url) => {
    const allowed = authWindow ? allowedAuthUrl(url) : allowedUrl(url);
    logNavigationDecision('will-navigate', url, allowed);
    if (!allowed) event.preventDefault();
  });
  win.webContents.on('will-redirect', (event, url) => {
    const allowed = authWindow ? allowedAuthUrl(url) : allowedUrl(url);
    if (allowed) {
      logNavigationDecision('will-redirect', url, true);
      return;
    }
    if (!authWindow && isOAuthOrigin(url)) {
      event.preventDefault();
      logNavigationDecision('oauth-redirect-to-controlled-window', url, true);
      createAuthWindow(url, brainSession);
      return;
    }
    logNavigationDecision('will-redirect', url, false);
    event.preventDefault();
  });
  win.webContents.setWindowOpenHandler(({url}) => {
    if (allowedAuthUrl(url) && (isOAuthOrigin(url) || authWindow)) {
      logNavigationDecision('oauth-popup', url, true);
      createAuthWindow(url, brainSession);
      return {action: 'deny'};
    }
    if (allowedUrl(url)) {
      logNavigationDecision('same-trust-popup', url, true);
      return {action: 'allow'};
    }
    logNavigationDecision('popup', url, false);
    return {action: 'deny'};
  });
}

function createAuthWindow(url, brainSession) {
  if (!brainSession) {
    logNavigationDecision('oauth-popup', url, false);
    return;
  }
  const authWindow = new BrowserWindow({
    width: 520, height: 760, minWidth: 420, minHeight: 520,
    title: 'ChatGPT 登录',
    show: true,
    backgroundColor: '#ffffff',
    webPreferences: {
      session: brainSession,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      javascript: true,
      webSecurity: true,
      additionalArguments: officialProductBundle ? [] : ['--aimr-auth-diagnostics=1'],
    },
  });
  authWindows.add(authWindow);
  applyNavigationPolicy(authWindow, {authWindow: true, brainSession});
  authWindow.webContents.on('did-finish-load', () => {
    console.log(`[desktop][auth-diagnostic] oauth-window-loaded url=${safeUrl(authWindow.webContents.getURL()) || 'INVALID'}`);
    if (brainHost) brainHost.refreshDomHealth();
  });
  authWindow.webContents.on('did-fail-load', (_event, errorCode, _description, validatedURL) => {
    if (errorCode !== -3) {
      console.log(`[desktop][auth-diagnostic] oauth-window-failed code=${errorCode} url=${safeUrl(validatedURL) || 'INVALID'}`);
    }
  });
  authWindow.on('closed', () => {
    authWindows.delete(authWindow);
    // Closing a Google window is not itself an auth failure. Re-check the
    // shared Brain session after the callback window disappears.
    if (brainHost) setTimeout(() => brainHost && brainHost.refreshDomHealth(), 0);
  });
  authWindow.loadURL(url);
  console.log(`[desktop][auth-diagnostic] oauth-window-created url=${safeUrl(url) || 'INVALID'}`);
  return authWindow;
}

async function runDesktopBrainPoc() {
  const run = desktopPocRuns.begin();
  console.log(`[desktop-poc] POC_RUN_CREATED runId=${run.runId}`);
  emitDesktopBrainPocStatus('PREPARING', null, run);
  try {
    desktopPocRuns.transition(run.runId, 'WRITING');
    emitDesktopBrainPocStatus('WRITING', null, run);
    desktopPocRuns.transition(run.runId, 'SENDING');
    emitDesktopBrainPocStatus('SENDING', null, run);
    desktopPocRuns.transition(run.runId, 'WAITING_RESPONSE');
    emitDesktopBrainPocStatus('WAITING_RESPONSE', null, run);
    // Formal POC route: Product Shell -> Python Playwright Channel attach.
    // Electron WebContents is not a production brain transport.
    const result = await postJson(`http://127.0.0.1:${PRODUCT_PORT}${FORMAL_BRAIN_POC_PATH}`, {}, FORMAL_BRAIN_POC_TIMEOUT_MS);
    if (!result || !result.decisionParsed || !result.decision ||
        result.decision.decision !== 'ACCEPT' || result.decision.taskId !== 'desktop-brain-poc' ||
        result.decision.reason !== 'desktop brain poc' || result.decision.instruction !== '' ||
        result.decision.confidence !== 1) {
      const error = new Error('RESPONSE_PARSE_FAILURE');
      error.code = 'RESPONSE_PARSE_FAILURE';
      throw error;
    }
    desktopPocRuns.transition(run.runId, 'PARSING');
    emitDesktopBrainPocStatus('PARSING', null, run);
    const completedRun = desktopPocRuns.finish(run.runId, 'PASSED', {result: result.decision});
    writeBrainConnectTrace({
      timestamp: new Date().toISOString(), stage: 'POC_OWNER_RESULT', result: 'PASS',
      transport: 'ELECTRON_IPC_TO_PRODUCT_SHELL_HTTP', runtimeOwner: FORMAL_RUNTIME_OWNER,
      launchSessionId: result.launchSessionId,
      processPid: result.processPid, parentPid: result.parentPid,
      registryInstanceId: result.registryInstanceId, hostInstanceId: result.hostInstanceId,
      boundPageId: result.boundPageId, MCP_PROCESS_STARTED: false,
    });
    emitDesktopBrainPocStatus('SUCCESS', null, completedRun);
    return {...result, phase: 'PHASE3_DESKTOP_BRAIN_POC'};
  } catch (error) {
    const failedRun = desktopPocRuns.finish(run.runId, 'FAILED', {failureClass: error.code || 'UNKNOWN'});
    emitDesktopBrainPocStatus('FAILED', error.code || 'UNKNOWN', failedRun);
    const safeError = new Error(error.message || error.code || 'UNKNOWN');
    safeError.code = error.code || 'UNKNOWN';
    safeError.stage = error.stage || 'POC_RUN';
    safeError.diagnostics = error.diagnostics || null;
    throw safeError;
  }
}

function createProductWindow() {
  productWindow = new BrowserWindow({
    width: 1280, height: 860, minWidth: 900, minHeight: 650,
    title: 'AI Meeting Room',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false, contextIsolation: true, sandbox: true,
      javascript: true, webSecurity: true,
      additionalArguments: officialProductBundle ? [] : ['--aimr-auth-diagnostics=1'],
    },
  });
  productRendererAlive = true;
  updateRendererAcceptanceState({rendererCreated: true});
  productWindow.once('ready-to-show', () => {
    rendererAcceptanceState.readyToShow = true;
    updateRendererAcceptanceState({readyToShow: true});
    productWindow.show();
    maybeRunNativeAcceptanceAudit();
    console.log('[desktop] Product Shell window ready');
  });
  productWindow.webContents.once('did-finish-load', () => {
    rendererAcceptanceState.didFinishLoad = true;
    updateRendererAcceptanceState({didFinishLoad: true});
    maybeRunNativeAcceptanceAudit();
  });
  productWindow.webContents.on('render-process-gone', () => {
    productRendererAlive = false;
    updateRendererAcceptanceState({rendererCreated: false, processId: 0});
  });
  productWindow.loadURL(PRODUCT_URL);
  console.log('[desktop] Product Shell window created');
  productWindow.on('closed', () => {
    productRendererAlive = false;
    updateRendererAcceptanceState({rendererCreated: false, processId: 0});
    productWindow = null;
  });
}

function createBrainWindow() {
  const brainSession = getPersistentBrainSession(session);
  console.log(`[desktop][auth-diagnostic] packaged=${officialProductBundle} dev-diagnostics=${!officialProductBundle}`);
  brainWindow = new BrowserWindow({
    width: 620, height: 860, minWidth: 420, minHeight: 520,
    title: 'ChatGPT 主脑',
    show: true,
    backgroundColor: '#ffffff',
    webPreferences: {
      session: brainSession,
      preload: path.join(__dirname, 'brain_preload.js'),
      nodeIntegration: false, contextIsolation: true, sandbox: true,
      javascript: true, webSecurity: true,
    },
  });
  brainSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    console.log(`[desktop][auth-diagnostic] permission-denied type=${permission}`);
    callback(false);
  });
  applyNavigationPolicy(brainWindow, {brainSession});
  brainHost = new ChatGPTDesktopBrainHost(brainWindow, {onFailure: notifyBrainFailure});
  console.log(`[desktop][auth-diagnostic] partition=${GPT_BRAIN_PARTITION} persistent=true cookies-api=${Boolean(brainSession.cookies)} javascript=true webSecurity=true`);
  console.log(`[desktop][auth-diagnostic] user-agent=${brainWindow.webContents.getUserAgent()}`);
  brainWindow.once('ready-to-show', () => { brainWindow.show(); console.log('[desktop] ChatGPT Brain window ready'); });
  brainWindow.webContents.on('did-finish-load', () => {
  console.log('[desktop] ChatGPT Brain page loaded');
    if (!officialProductBundle) {
      brainWindow.webContents.executeJavaScript(
        'window.aimrBrainHost && window.aimrBrainHost.enableAuthDiagnostics ? window.aimrBrainHost.enableAuthDiagnostics() : false',
        true,
      ).then(enabled => console.log(`[desktop][auth-diagnostic] runtime-enable=${Boolean(enabled)}`))
        .catch(() => console.log('[desktop][auth-diagnostic] runtime-enable=false'));
    }
  });
  brainWindow.loadURL(CHATGPT_URL);
  console.log('[desktop] ChatGPT Brain window created');
  brainWindow.on('closed', () => { brainWindow = null; brainHost = null; });
}

function registerIpc() {
  ipcMain.handle('brain:status', async () => {
    try {
      const status = await getJson(`http://127.0.0.1:${PRODUCT_PORT}${FORMAL_BRAIN_STATUS_PATH}`);
      if (status.authState === 'AUTHENTICATED') app.focus({steal: true});
      return status;
    } catch (_) {
      return {state: 'DISCONNECTED', authState: 'UNKNOWN', browserRuntime: 'PLAYWRIGHT_ATTACH_EXISTING_REAL_CHROME', browserAlive: false};
    }
  });
  ipcMain.handle('brain:open', async (_event, request = {}) => {
    const attemptId = String(request?.connectAttemptId || randomUUID());
    writeBrainConnectTrace({attemptId, timestamp: new Date().toISOString(), stage: 'T0_UI_CLICK', result: 'RECEIVED', MCP_PROCESS_STARTED: false});
    writeBrainConnectTrace({attemptId, timestamp: new Date().toISOString(), stage: 'T1_IPC_REQUEST_SENT', result: 'RECEIVED', MCP_PROCESS_STARTED: false});
    try {
      writeBrainConnectTrace({attemptId, timestamp: new Date().toISOString(), stage: 'T2_IPC_HANDLER_ENTER', result: 'RECEIVED', MCP_PROCESS_STARTED: false});
      const result = await postJson(`http://127.0.0.1:${PRODUCT_PORT}${FORMAL_BRAIN_OPEN_PATH}`, {connectAttemptId: attemptId}, FORMAL_BRAIN_OPEN_TIMEOUT_MS);
      app.focus({steal: true});
      if (result?.ok === false) {
        writeBrainConnectTrace({attemptId, timestamp: new Date().toISOString(), stage: result.error?.stage || 'T15_FAIL', result: 'FAIL', errorCode: result.error?.code, MCP_PROCESS_STARTED: false});
        return safeIpcReturn(result, attemptId);
      }
      const data = result?.data || result;
      const connection = result?.connection || data?.connection || null;
      writeBrainConnectTrace({
        attemptId, timestamp: new Date().toISOString(), stage: 'FORMAL_OWNER_RESULT', result: 'PASS',
        transport: 'ELECTRON_IPC_TO_PRODUCT_SHELL_HTTP', runtimeOwner: FORMAL_RUNTIME_OWNER,
        launchSessionId: data.launchSessionId,
        processPid: data.processPid, parentPid: data.parentPid,
        registryInstanceId: data.registryInstanceId, hostInstanceId: data.hostInstanceId,
        boundPageId: data.boundPageId, MCP_PROCESS_STARTED: false,
      });
      writeBrainConnectTrace({attemptId, timestamp: new Date().toISOString(), stage: 'T15_SUCCESS', result: 'PASS', MCP_PROCESS_STARTED: false});
      return safeIpcReturn({ok: true, data: {...data, ...(connection ? {connection} : {}), opened: true, browserRuntime: 'PLAYWRIGHT_ATTACH_EXISTING_REAL_CHROME', connectAttemptId: attemptId}}, attemptId);
    } catch (error) {
      const result = ipcBrainError(error, attemptId);
      writeBrainConnectTrace({attemptId, timestamp: new Date().toISOString(), stage: result.error.stage, result: 'FAIL', errorCode: result.error.code, safeMessage: result.error.message, MCP_PROCESS_STARTED: false});
      return safeIpcReturn(result, attemptId);
    }
  });
  ipcMain.handle('brain:desktop-poc', async () => {
    console.log('[desktop-poc] POC_IPC_REQUEST');
    try {
      return await runDesktopBrainPoc();
    } catch (error) {
      const safeCode = error?.code || 'UNKNOWN';
      if (['GPT_WEB_BRAIN_NOT_READY', 'AUTH_REQUIRED', 'AUTH_LOST', 'COMPOSER_NOT_FOUND', 'COMPOSER_BUSY', 'COMPOSER_NOT_VISIBLE', 'COMPOSER_NOT_EDITABLE', 'COMPOSER_AMBIGUOUS', 'COMPOSER_WRITE_FAILED', 'COMPOSER_WRITE_VERIFY_FAILED', 'POC_BOUND_PAGE_CHANGED', 'SEND_FAILED', 'RESPONSE_TIMEOUT', 'RESPONSE_NOT_AVAILABLE', 'RESPONSE_BOUNDARY_FAILURE', 'RESPONSE_PARSE_FAILURE', 'BRAIN_DECISION_INVALID', 'POC_FAILED'].includes(safeCode)) {
        return safeIpcReturn({ok: false, error: {
          code: safeCode,
          stage: error.stage || 'POC_PRECHECK',
          // Preserve the first Product Shell failure; the renderer maps the
          // stable code to the user-facing Chinese message.
          message: error.message || safeCode,
          ...(error.diagnostics || {}),
        }}, 'desktop-poc');
      }
      const safeError = new Error(safeCode);
      safeError.code = safeCode;
      throw safeError;
    }
  });
  ipcMain.handle('runtime:status', () => ({
    productShell: Boolean(productWindow && !productWindow.isDestroyed()),
    coreProcess: Boolean(coreProcess),
    rendererAlive: productRendererAlive && Boolean(productWindow && !productWindow.webContents.isCrashed()),
    rendererCreated: rendererAcceptanceState.rendererCreated,
    rendererDidFinishLoad: rendererAcceptanceState.didFinishLoad,
    rendererReadyToShow: rendererAcceptanceState.readyToShow,
    brainWindow: Boolean(brainWindow && !brainWindow.isDestroyed()),
    formalTransport: 'ELECTRON_IPC_TO_PRODUCT_SHELL_HTTP',
    formalRuntimeOwner: FORMAL_RUNTIME_OWNER,
    launchSessionId,
    processPid: process.pid,
    parentPid: process.ppid,
  }));
}

async function boot() {
  const identity = await ensureOwnedProductShell();
  writeDesktopEvent('product-shell-ready', {launchSessionId: identity.launchSessionId, pid: identity.pid, runtimeOwner: identity.runtimeOwner});
  // V1 defaults to MANUAL_GPT_HANDOFF. Do not launch, attach, or probe the
  // experimental GPT Web runtime during startup.
  createProductWindow();
  // Electron WebContents remains available only as an explicit legacy test
  // path. The formal GPT Web Brain runtime is the dedicated Chrome process.
  if (process.env.AIMR_ENABLE_ELECTRON_BRAIN_LEGACY === '1') createBrainWindow();
}

if (singleInstanceLock) {
  app.on('second-instance', () => {
    if (!productWindow || productWindow.isDestroyed()) return;
    if (productWindow.isMinimized()) productWindow.restore();
    productWindow.show();
    productWindow.focus();
  });
  app.whenReady().then(async () => {
    try {
      writeDesktopEvent('electron-startup', {pid: process.pid, launchSessionId, runtimeOwner: FORMAL_RUNTIME_OWNER});
      registerIpc();
      await boot();
    } catch (error) {
      const code = String(error?.code || 'STARTUP_FAILED');
      const safeMessage = code === 'PYTHON_PLAYWRIGHT_UNAVAILABLE'
        ? '未检测到可用的 Python 3 与 Playwright 依赖，请安装后重试。'
        : 'AI Meeting Room 启动失败，请查看应用数据目录中的 product.log。';
      writeDesktopEvent('startup-failed', {code});
      dialog.showErrorBox('AI Meeting Room 启动失败', safeMessage);
      app.quit();
    }
  });
}

app.on('certificate-error', (_event, _webContents, url, _error, callback) => {
  console.log(`[desktop][auth-diagnostic] certificate-error url=${safeUrl(url) || 'INVALID'}`);
  callback(false);
});

app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', () => {
  if (!singleInstanceLock || !productStoragePaths) return;
  quitting = true;
  writeDesktopEvent('electron-shutdown', {pid: process.pid, launchSessionId, runtimeOwner: FORMAL_RUNTIME_OWNER});
  stopCoreProcessGracefully();
});
