"use strict";

const fs = require('node:fs');
const path = require('node:path');

// This is the only production ChatGPT session. Keep the value stable across
// development launches, the macOS command launcher, and a future packaged app.
const GPT_BRAIN_PARTITION = 'persist:chatgpt-brain';
const APP_USER_DATA_DIR_NAME = 'ai-meeting-room-desktop';
const PRODUCT_DATA_ROOT_ENV = 'AI_MEETING_ROOM_DATA_DIR';
const ACCEPTANCE_MODE_ENV = 'AI_MEETING_ROOM_ACCEPTANCE_MODE';
const PRODUCT_PORT_ENV = 'AIMR_PRODUCT_PORT';
const CAO_BASE_URL_ENV = 'CAO_BASE_URL';
const DEFAULT_PRODUCT_PORT = 8765;
const DEFAULT_CAO_BASE_URL = 'http://127.0.0.1:9889';
const ACCEPTANCE_CONFIG_FILENAME = 'aimr-acceptance-config.json';
const ACCEPTANCE_CONFIG_SCHEMA = 'ai-meeting-room.acceptance-config.v1';
const SEALED_ACCEPTANCE_ROOT = '/private/tmp';
const ACCEPTANCE_SESSION_PREFIX_PATTERN = /^aimr-v[0-9]+-r[0-9]+-[a-f0-9]{10}-$/;
const LAUNCH_ARGUMENTS = Object.freeze({
  dataRoot: '--ai-meeting-room-data-root',
  acceptanceMode: '--ai-meeting-room-acceptance',
  productPort: '--ai-meeting-room-port',
  caoBaseUrl: '--ai-meeting-room-cao-url',
});
const AUTH_RECOVERY_WAIT_MS = 10000;

let userDataConfigured = false;
let sessionCreationStarted = false;

class ProductPathConfigurationError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function resolveExistingPath(candidate, fsModule = fs) {
  let current = path.resolve(candidate);
  const suffix = [];
  while (true) {
    try {
      const realpath = fsModule.realpathSync.native
        ? fsModule.realpathSync.native(current)
        : fsModule.realpathSync(current);
      return path.resolve(realpath, ...suffix);
    } catch (error) {
      if (!['ENOENT', 'ENOTDIR'].includes(error?.code)) throw error;
      const parent = path.dirname(current);
      suffix.unshift(path.basename(current));
      if (parent === current) return path.resolve(current, ...suffix);
      current = parent;
    }
  }
}

function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

function argumentValue(argv, name) {
  const prefix = `${name}=`;
  const matches = argv.filter(argument => argument.startsWith(prefix));
  if (matches.length > 1 || matches.some(argument => argument.slice(prefix.length).trim() === '')) {
    throw new ProductPathConfigurationError('PRODUCT_LAUNCH_ARGUMENT_INVALID');
  }
  return matches.length ? matches[0].slice(prefix.length) : null;
}

function resolvePairedValue(environmentValue, argument, normalize = value => String(value)) {
  if (environmentValue != null && argument != null && normalize(environmentValue) !== normalize(argument)) {
    throw new ProductPathConfigurationError('PRODUCT_LAUNCH_CONFIGURATION_CONFLICT');
  }
  return environmentValue ?? argument;
}

function loadBundledAcceptanceConfiguration({resourcesPath = process.resourcesPath, requireBuildMarker = false, fsModule = fs} = {}) {
  if (!resourcesPath) {
    if (requireBuildMarker) throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
    return null;
  }
  let acceptanceBuild = false;
  const infoPlistPath = path.join(path.dirname(resourcesPath), 'Info.plist');
  try {
    const infoPlist = fsModule.readFileSync(infoPlistPath, 'utf8');
    const marker = infoPlist.match(/<key>AIMRAcceptanceBuild<\/key>\s*<(true|false)\s*\/>/);
    if (marker) acceptanceBuild = marker[1] === 'true';
    else if (requireBuildMarker) throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
  } catch (error) {
    if (error instanceof ProductPathConfigurationError) throw error;
    if (error?.code !== 'ENOENT' || requireBuildMarker) {
      throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
    }
  }
  const configPath = path.join(resourcesPath, ACCEPTANCE_CONFIG_FILENAME);
  let configText;
  try {
    const stat = fsModule.lstatSync(configPath);
    if (!stat.isFile() || stat.isSymbolicLink()) {
      throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
    }
    configText = fsModule.readFileSync(configPath, 'utf8');
  } catch (error) {
    if (error?.code === 'ENOENT' && !acceptanceBuild) return null;
    if (error?.code === 'ENOENT' && acceptanceBuild) {
      throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
    }
    if (error instanceof ProductPathConfigurationError) throw error;
    throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
  }
  let config;
  try {
    config = JSON.parse(configText);
  } catch (_) {
    throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
  }
  if (
    !config || typeof config !== 'object' || Array.isArray(config)
    || config.schemaVersion !== ACCEPTANCE_CONFIG_SCHEMA
    || config.acceptanceMode !== true
    || typeof config.dataRoot !== 'string' || !path.isAbsolute(config.dataRoot)
    || !Number.isInteger(config.productPort)
    || typeof config.caoBaseUrl !== 'string'
    || Object.keys(config).some(key => !['schemaVersion', 'acceptanceMode', 'dataRoot', 'productPort', 'caoBaseUrl', 'agentSessionPrefix'].includes(key))
    || (config.agentSessionPrefix != null && (typeof config.agentSessionPrefix !== 'string' || !ACCEPTANCE_SESSION_PREFIX_PATTERN.test(config.agentSessionPrefix)))
  ) {
    throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
  }
  let sealedRoot;
  let dataRoot;
  try {
    sealedRoot = resolveExistingPath(SEALED_ACCEPTANCE_ROOT, fsModule);
    dataRoot = resolveExistingPath(config.dataRoot, fsModule);
  } catch (_) {
    throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
  }
  if (dataRoot === sealedRoot || !isWithin(sealedRoot, dataRoot)) {
    throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
  }
  return Object.freeze({
    acceptanceMode: true,
    dataRoot,
    productPort: config.productPort,
    caoBaseUrl: config.caoBaseUrl,
    agentSessionPrefix: config.agentSessionPrefix || null,
  });
}

function assertAcceptancePathsContained(productPaths, fsModule = fs) {
  const electronPaths = resolveUserDataPaths({dataRoot: productPaths.root});
  const candidates = [
    ...Object.values(productPaths),
    ...Object.values(electronPaths),
  ];
  for (const candidate of candidates) {
    let actual;
    try {
      actual = resolveExistingPath(candidate, fsModule);
    } catch (_) {
      throw new ProductPathConfigurationError('PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT');
    }
    if (!isWithin(productPaths.root, actual)) {
      throw new ProductPathConfigurationError('PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT');
    }
  }
}

function resolveProductLaunchConfiguration({
  argv = process.argv.slice(1),
  environment = process.env,
  packagedAcceptanceConfig = null,
  home,
  fsModule = fs,
} = {}) {
  const argumentRoot = argumentValue(argv, LAUNCH_ARGUMENTS.dataRoot);
  const environmentRoot = environment[PRODUCT_DATA_ROOT_ENV];
  const configuredRootFromProcess = resolvePairedValue(
    environmentRoot,
    argumentRoot,
    value => resolveExistingPath(value, fsModule),
  );
  const configuredRoot = resolvePairedValue(
    configuredRootFromProcess,
    packagedAcceptanceConfig?.dataRoot,
    value => resolveExistingPath(value, fsModule),
  );
  const acceptanceMode = environment[ACCEPTANCE_MODE_ENV] === '1'
    || argv.includes(LAUNCH_ARGUMENTS.acceptanceMode)
    || Boolean(packagedAcceptanceConfig);
  const productPortFromProcess = resolvePairedValue(
    environment[PRODUCT_PORT_ENV],
    argumentValue(argv, LAUNCH_ARGUMENTS.productPort),
  );
  const productPortValue = resolvePairedValue(productPortFromProcess, packagedAcceptanceConfig?.productPort);
  const productPort = productPortValue == null ? DEFAULT_PRODUCT_PORT : Number(productPortValue);
  if (!Number.isInteger(productPort) || productPort < 1 || productPort > 65535) {
    throw new ProductPathConfigurationError('PRODUCT_PORT_INVALID');
  }
  const caoBaseUrlFromProcess = resolvePairedValue(
    environment[CAO_BASE_URL_ENV],
    argumentValue(argv, LAUNCH_ARGUMENTS.caoBaseUrl),
  );
  const caoBaseUrl = resolvePairedValue(caoBaseUrlFromProcess, packagedAcceptanceConfig?.caoBaseUrl)
    || DEFAULT_CAO_BASE_URL;
  const agentSessionPrefix = packagedAcceptanceConfig?.agentSessionPrefix || null;
  if (agentSessionPrefix != null && !ACCEPTANCE_SESSION_PREFIX_PATTERN.test(agentSessionPrefix)) {
    throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_CONFIG_INVALID');
  }
  let parsedCaoUrl;
  try {
    parsedCaoUrl = new URL(caoBaseUrl);
  } catch (_) {
    throw new ProductPathConfigurationError('CAO_URL_INVALID');
  }
  if (
    parsedCaoUrl.protocol !== 'http:'
    || !['127.0.0.1', 'localhost', '::1'].includes(parsedCaoUrl.hostname)
    || parsedCaoUrl.username || parsedCaoUrl.password
    || parsedCaoUrl.search || parsedCaoUrl.hash
    || !['', '/'].includes(parsedCaoUrl.pathname)
  ) {
    throw new ProductPathConfigurationError('CAO_LOCAL_ONLY_REQUIRED');
  }
  const productPaths = resolveProductStoragePaths({
    dataRoot: configuredRoot || undefined,
    databasePath: environment.AI_MEETING_ROOM_DB,
    home: home || environment.HOME || environment.USERPROFILE,
    requireExplicitRoot: acceptanceMode,
    fsModule,
  });
  if (packagedAcceptanceConfig) assertAcceptancePathsContained(productPaths, fsModule);
  return Object.freeze({productPaths, productPort, caoBaseUrl: parsedCaoUrl.origin, acceptanceMode, agentSessionPrefix});
}

function resolveProductStoragePaths({
  dataRoot,
  databasePath,
  home = process.env.HOME || process.env.USERPROFILE,
  requireExplicitRoot = false,
  fsModule = fs,
} = {}) {
  if (requireExplicitRoot && !dataRoot) {
    throw new ProductPathConfigurationError('PRODUCT_DATA_ROOT_OVERRIDE_REQUIRED');
  }
  if (!dataRoot && !databasePath && !home) throw new ProductPathConfigurationError('PRODUCT_HOME_UNAVAILABLE');
  const resolvedDatabaseOverride = databasePath ? resolveExistingPath(databasePath, fsModule) : null;
  const configuredRoot = dataRoot || (resolvedDatabaseOverride
    ? path.dirname(resolvedDatabaseOverride)
    : path.join(home, '.ai-meeting-room'));
  const root = resolveExistingPath(configuredRoot, fsModule);
  const resolved = {
    root,
    database: resolvedDatabaseOverride || path.join(root, 'meeting.db'),
    logs: path.join(root, 'logs'),
    backups: path.join(root, 'backups'),
    diagnostics: path.join(root, 'diagnostics'),
    runtime: path.join(root, 'runtime'),
    electron: path.join(root, 'electron'),
  };
  for (const key of ['database', 'logs', 'backups', 'diagnostics', 'runtime', 'electron']) {
    const actual = resolveExistingPath(resolved[key], fsModule);
    if (!isWithin(root, actual)) {
      throw new ProductPathConfigurationError('PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT');
    }
  }
  if (dataRoot && resolvedDatabaseOverride && resolvedDatabaseOverride !== path.join(root, 'meeting.db')) {
    throw new ProductPathConfigurationError('PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT');
  }
  return Object.freeze(resolved);
}

function createBackendEnvironment(baseEnvironment, productPaths, launchConfiguration = {}) {
  const environment = {
    ...baseEnvironment,
    [PRODUCT_DATA_ROOT_ENV]: productPaths.root,
    AI_MEETING_ROOM_DB: productPaths.database,
  };
  if (launchConfiguration.acceptanceMode) environment[ACCEPTANCE_MODE_ENV] = '1';
  if (launchConfiguration.acceptanceMode && launchConfiguration.agentSessionPrefix) {
    environment.AI_MEETING_ROOM_ACCEPTANCE_SESSION_PREFIX = launchConfiguration.agentSessionPrefix;
  }
  if (launchConfiguration.productPort != null) environment[PRODUCT_PORT_ENV] = String(launchConfiguration.productPort);
  if (launchConfiguration.caoBaseUrl) environment[CAO_BASE_URL_ENV] = launchConfiguration.caoBaseUrl;
  return environment;
}

function writeRendererAcceptanceMarker({enabled, paths, state, fsModule = fs}) {
  if (!enabled) return false;
  if (!paths || !state) throw new ProductPathConfigurationError('PRODUCT_ACCEPTANCE_PATHS_UNAVAILABLE');
  const runtime = path.join(paths.root, 'runtime');
  const marker = path.join(runtime, 'renderer-acceptance.json');
  if (!isWithin(paths.root, resolveExistingPath(runtime, fsModule)) || !isWithin(paths.root, resolveExistingPath(marker, fsModule))) {
    throw new ProductPathConfigurationError('PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT');
  }
  fsModule.mkdirSync(runtime, {recursive: true, mode: 0o700});
  const payload = {
    rendererCreated: Boolean(state.rendererCreated),
    didFinishLoad: Boolean(state.didFinishLoad),
    readyToShow: Boolean(state.readyToShow),
    timestamp: new Date().toISOString(),
    processId: Number.isInteger(state.processId) ? state.processId : 0,
  };
  const temporary = `${marker}.${process.pid}.tmp`;
  fsModule.writeFileSync(temporary, `${JSON.stringify(payload)}\n`, {encoding: 'utf8', mode: 0o600, flag: 'w'});
  fsModule.renameSync(temporary, marker);
  return true;
}

function resolveUserDataPath({appDataPath, platform = process.platform} = {}) {
  // Electron supplies the platform-specific Application Support root. Keeping
  // this helper platform-neutral also lets unit tests run off macOS without
  // ever creating a production session.
  void platform;
  if (!appDataPath) throw new Error('appDataPath is required');
  return path.join(appDataPath, APP_USER_DATA_DIR_NAME);
}

function resolveUserDataPaths({appDataPath, dataRoot, platform = process.platform} = {}) {
  if (dataRoot) {
    const electronRoot = path.join(path.resolve(dataRoot), 'electron');
    const userData = path.join(electronRoot, 'user-data');
    return {
      userData,
      sessionData: userData,
      cache: path.join(electronRoot, 'cache'),
    };
  }
  const userData = resolveUserDataPath({appDataPath, platform});
  return {userData, sessionData: userData, cache: null};
}

function configureUserDataPath(app, {appDataPath, dataRoot} = {}) {
  if (sessionCreationStarted) {
    throw new Error('USER_DATA_CONFIG_TOO_LATE_SESSION_ALREADY_CREATED');
  }
  if (userDataConfigured) return app.getPath('userData');
  const resolved = resolveUserDataPaths({
    appDataPath: appDataPath || app.getPath('appData'),
    dataRoot,
    platform: process.platform,
  });
  app.setPath('userData', resolved.userData);
  app.setPath('sessionData', resolved.sessionData);
  if (resolved.cache) app.setPath('cache', resolved.cache);
  userDataConfigured = true;
  return resolved.userData;
}

function getPersistentBrainSession(sessionModule) {
  if (!userDataConfigured) throw new Error('USER_DATA_NOT_CONFIGURED_BEFORE_SESSION');
  if (!GPT_BRAIN_PARTITION.startsWith('persist:')) throw new Error('BRAIN_PARTITION_NOT_PERSISTENT');
  sessionCreationStarted = true;
  return sessionModule.fromPartition(GPT_BRAIN_PARTITION);
}

function getIsolatedTestPartition(suffix = 'unit') {
  const safeSuffix = String(suffix).replace(/[^A-Za-z0-9_-]/g, '-');
  return `persist:chatgpt-brain-test-${safeSuffix}`;
}

function resetRuntimeConfigForTests() {
  userDataConfigured = false;
  sessionCreationStarted = false;
}

module.exports = {
  GPT_BRAIN_PARTITION,
  APP_USER_DATA_DIR_NAME,
  PRODUCT_DATA_ROOT_ENV,
  ACCEPTANCE_MODE_ENV,
  ACCEPTANCE_CONFIG_FILENAME,
  ACCEPTANCE_CONFIG_SCHEMA,
  SEALED_ACCEPTANCE_ROOT,
  AUTH_RECOVERY_WAIT_MS,
  ProductPathConfigurationError,
  LAUNCH_ARGUMENTS,
  loadBundledAcceptanceConfiguration,
  resolveProductStoragePaths,
  resolveProductLaunchConfiguration,
  createBackendEnvironment,
  writeRendererAcceptanceMarker,
  resolveUserDataPath,
  resolveUserDataPaths,
  configureUserDataPath,
  getPersistentBrainSession,
  getIsolatedTestPartition,
  resetRuntimeConfigForTests,
};
