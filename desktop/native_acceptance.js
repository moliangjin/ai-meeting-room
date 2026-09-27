// Sealed-package-only native renderer audit. This module never runs in a
// normal release and never reads Chromium storage, credentials or chat pages.
const fs = require('node:fs');
const path = require('node:path');

const SIZES = Object.freeze([
  Object.freeze({width: 1440, height: 900}),
  Object.freeze({width: 1200, height: 800}),
  Object.freeze({width: 1024, height: 700}),
]);

function readProductBundleMarker(resourcesPath) {
  try {
    const infoPlist = fs.readFileSync(path.join(path.dirname(resourcesPath), 'Info.plist'), 'utf8');
    if (!/<key>CFBundleIdentifier<\/key>\s*<string>local\.ai-meeting-room\.desktop<\/string>/.test(infoPlist)) return null;
    const marker = infoPlist.match(/<key>AIMRAcceptanceBuild<\/key>\s*<(true|false)\s*\/>/);
    return marker ? marker[1] === 'true' : null;
  } catch (_) {
    return null;
  }
}

function hasSealedAcceptanceMarker(resourcesPath) {
  return readProductBundleMarker(resourcesPath) === true;
}

function rendererAudit() {
    const activeModal = document.querySelector('dialog[open]');
    const visible = node => {
      if (!(node instanceof Element)) return false;
      if (node.closest('[hidden], [aria-hidden="true"], details:not([open]) > :not(summary)')) return false;
      if (activeModal && node !== activeModal && !activeModal.contains(node)) return false;
      const style = getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const controls = [...document.querySelectorAll('button, input, textarea, select, [role="button"]')]
      .filter(visible).map(node => {
        const rect = node.getBoundingClientRect();
        return {tag: node.tagName, id: node.id || null, disabled: Boolean(node.disabled),
          x: rect.x, y: rect.y, width: rect.width, height: rect.height,
          reachable: rect.width > 0 && rect.height > 0 && rect.left >= -2 && rect.right <= innerWidth + 2};
      });
    const overlaps = [];
    for (let i = 0; i < controls.length; i++) for (let j = i + 1; j < controls.length; j++) {
      const a = controls[i], b = controls[j];
      const width = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
      const height = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
      if (width > 8 && height > 8) overlaps.push([a.id || `${a.tag}:${i}`, b.id || `${b.tag}:${j}`]);
    }
    const clipped = [...document.querySelectorAll('h1, h2, h3, button, summary, label, .status, .provider, .card')]
      .filter(visible).filter(node => {
        const style = getComputedStyle(node);
        return (style.overflowX === 'hidden' || style.overflowX === 'clip') && node.scrollWidth > node.clientWidth + 2;
      }).map(node => node.id || node.tagName).slice(0, 30);
    const residualLabels = new Set();
    const forbidden = /\b(?:New meeting|Create meeting|Global Pause|Generate Pairing Code|Submit decision|No tasks|No events|No meetings|Members|Legacy|Experimental|instruction|triggeredBy|Dispatch disabled|Meeting PAUSED|BrainDecision)\b/gi;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!visible(node.parentElement)) continue;
      if (node.parentElement.closest('code, pre, .code, .technical, details:not([open])')) continue;
      for (const match of node.textContent.matchAll(forbidden)) residualLabels.add(match[0]);
      if (/^\s*(?:Brain|Agents)\s*$/.test(node.textContent)) residualLabels.add(node.textContent.trim());
    }
    return {
      documentReady: document.readyState,
      contentWidth: innerWidth, contentHeight: innerHeight,
      documentScrollWidth: document.documentElement.scrollWidth,
      documentClientWidth: document.documentElement.clientWidth,
      documentScrollHeight: document.documentElement.scrollHeight,
      controlCount: controls.length,
      unreachableControls: controls.filter(control => !control.reachable).map(control => control.id || control.tag),
      overlaps, clipped,
      residualEnglishLabels: [...residualLabels],
      sidebarPresent: Boolean(document.querySelector('.sidebar')),
      mainPresent: Boolean(document.querySelector('.main')),
      rightPresent: Boolean(document.querySelector('.right')),
      modalCount: [...document.querySelectorAll('[role="dialog"], dialog')].filter(visible).length,
      modalFits: !activeModal || (() => {
        const rect = activeModal.getBoundingClientRect();
        return rect.left >= 0 && rect.top >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight;
      })(),
    };
}

function assertSealedNativeAudit({sealedBundle, bundledAcceptanceConfiguration, launchConfiguration, paths}) {
  if (!sealedBundle || !bundledAcceptanceConfiguration || !launchConfiguration?.acceptanceMode || !paths?.runtime) {
    throw new Error('NATIVE_ACCEPTANCE_NOT_SEALED');
  }
  if (path.resolve(paths.runtime) !== path.resolve(paths.root, 'runtime')) {
    throw new Error('NATIVE_ACCEPTANCE_RUNTIME_ESCAPE');
  }
}

async function runNativeAcceptanceAudit({window, sealedBundle, bundledAcceptanceConfiguration, launchConfiguration, paths}) {
  assertSealedNativeAudit({sealedBundle, bundledAcceptanceConfiguration, launchConfiguration, paths});
  if (!window || window.isDestroyed() || window.webContents.isDestroyed()) {
    throw new Error('NATIVE_ACCEPTANCE_WINDOW_UNAVAILABLE');
  }
  const originalBounds = window.getBounds();
  const results = [];
  const meetingResults = [];
  const modalResults = [];
  fs.mkdirSync(paths.runtime, {recursive: true});
  const progressPath = path.join(paths.runtime, 'native-layout-progress.json');
  const progress = (stage, size = null) => fs.writeFileSync(progressPath,
    JSON.stringify({stage, size, recordedAt: new Date().toISOString()}) + '\n', {mode: 0o600});
  progress('STARTED');
  try {
    progress('WAITING_FOR_LOCALIZED_RENDERER');
    let settled = false;
    for (let attempt = 0; attempt < 40; attempt++) {
      settled = await window.webContents.executeJavaScript(
        "document.querySelector('.sidebar .eyebrow')?.textContent?.trim()==='会议' && Boolean(document.querySelector('#providers .provider'))",
        true);
      if (settled) break;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    if (!settled) throw new Error('NATIVE_ACCEPTANCE_RENDERER_NOT_SETTLED');
    const captureSizes = async (prefix, destination) => {
      for (const size of SIZES) {
        progress('RESIZING', size);
        window.setBounds({...originalBounds, width: size.width, height: size.height}, false);
        await new Promise(resolve => setTimeout(resolve, 150));
        progress('MEASURING', size);
        const audit = await window.webContents.executeJavaScript(`(${rendererAudit.toString()})()`, true);
        progress('CAPTURING', size);
        const screenshot = await window.webContents.capturePage();
        const imageName = `${prefix}-${size.width}x${size.height}.png`;
        fs.writeFileSync(path.join(paths.runtime, imageName), screenshot.toPNG(), {mode: 0o600});
        destination.push({requestedBounds: size, actualBounds: window.getBounds(), ...audit, screenshot: imageName});
        progress('CAPTURED', size);
      }
    };
    await captureSizes('native-layout', results);
    const workspace = path.join(path.dirname(paths.root), 'workspace');
    fs.mkdirSync(workspace, {recursive: true});
    const meetingName = 'R94 原生长中文布局验收会议 · 安全状态与智能体协作记录';
    progress('CREATING_MEETING_VIA_RENDERER');
    const created = await window.webContents.executeJavaScript(`(async () => {
      const button = document.getElementById('createMeetingButton');
      if (!button) throw Error('NATIVE_CREATE_BUTTON_MISSING');
      button.click();
      const dialog = document.getElementById('createMeetingDialog');
      if (!dialog?.open) throw Error('NATIVE_CREATE_DIALOG_DID_NOT_OPEN');
      const name = document.getElementById('createMeetingName');
      const workspace = document.getElementById('createMeetingWorkspace');
      name.value = ${JSON.stringify(meetingName)};
      workspace.value = ${JSON.stringify(workspace)};
      name.dispatchEvent(new Event('input', {bubbles:true}));
      workspace.dispatchEvent(new Event('input', {bubbles:true}));
      document.getElementById('createMeetingForm').requestSubmit();
      for (let attempt = 0; attempt < 40; attempt++) {
        if (document.querySelector('#detail h1')?.textContent?.trim() === ${JSON.stringify(meetingName)}) break;
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      return {name: document.querySelector('#detail h1')?.textContent?.trim() || null,
        meetingId: document.querySelector('#detail .top .code')?.textContent?.trim() || null};
    })()`, true);
    if (created?.name !== meetingName || !created.meetingId) {
      throw new Error('NATIVE_ACCEPTANCE_CREATE_MEETING_FAILED');
    }
    progress('MEETING_CREATED');
    for (let attempt = 0; attempt < 24; attempt++) {
      const noticeHidden = await window.webContents.executeJavaScript(
        "Boolean(document.getElementById('product-notice')?.hidden)", true);
      if (noticeHidden) break;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    await captureSizes('native-meeting-layout', meetingResults);
    progress('OPENING_NATIVE_MODAL');
    const modalOpened = await window.webContents.executeJavaScript(
      "document.getElementById('createMeetingButton')?.click(); Boolean(document.getElementById('createMeetingDialog')?.open)", true);
    if (!modalOpened) throw new Error('NATIVE_ACCEPTANCE_MODAL_DID_NOT_OPEN');
    await captureSizes('native-modal-layout', modalResults);
    await window.webContents.executeJavaScript(
      "document.querySelector('#createMeetingDialog button.secondary')?.click()", true);
  } finally {
    if (!window.isDestroyed()) window.setBounds(originalBounds, false);
  }
  const payload = {schemaVersion: 'ai-meeting-room.native-acceptance.v1',
    electronAppBundle: true, sealedAcceptanceBuild: true,
    rendererProcessId: window.webContents.getOSProcessId(),
    sizes: results, meetingSizes: meetingResults, modalSizes: modalResults};
  const reportPath = path.join(paths.runtime, 'native-layout-audit.json');
  const temporaryPath = `${reportPath}.tmp`;
  fs.writeFileSync(temporaryPath, JSON.stringify(payload, null, 2) + '\n', {mode: 0o600});
  fs.renameSync(temporaryPath, reportPath);
  progress('COMPLETED');
  return payload;
}

module.exports = {SIZES, rendererAudit, readProductBundleMarker, hasSealedAcceptanceMarker,
  assertSealedNativeAudit, runNativeAcceptanceAudit};
