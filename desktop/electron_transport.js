"use strict";

class ElectronTransportError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'ElectronTransportError';
    this.code = code;
  }
}

class ElectronWebContentsTransport {
  constructor(brainHost, {timeoutSeconds = 180, pollIntervalMs = 250} = {}) {
    this.brainHost = brainHost;
    this.timeoutSeconds = timeoutSeconds;
    this.pollIntervalMs = pollIntervalMs;
    this.request = null;
    this.response = null;
    this.metrics = null;
  }

  _liveStatus() {
    const status = this.brainHost.status();
    if (!status.webContentsAlive) throw new ElectronTransportError('WEB_CONTENTS_LOST');
    return status;
  }

  _callPage(method, value = null) {
    this._liveStatus();
    const methodJson = JSON.stringify(method);
    const valueJson = JSON.stringify(value);
    return this.brainHost.executeJavaScript(
      `(() => { const host = window.aimrBrainHost; const fn = host && host[${methodJson}]; return typeof fn === 'function' ? fn(${valueJson}) : null; })()`,
    ).then(result => {
      if (result === null || result === undefined) throw new ElectronTransportError('DOM_UNKNOWN');
      return result;
    });
  }

  async connect() {
    const status = this._liveStatus();
    if (status.authState !== 'AUTHENTICATED') {
      const code = status.authState === 'CHALLENGE_REQUIRED' ? 'CHALLENGE_REQUIRED' :
        (status.authState === 'AUTH_REQUIRED' ? 'AUTH_REQUIRED' :
          (status.authState === 'LOADING' ? 'LOADING' :
            (status.authState === 'AUTH_LOST' ? 'AUTH_LOST' : 'DOM_UNKNOWN')));
      throw new ElectronTransportError(code);
    }
    return status;
  }

  async authStatus() {
    return this._liveStatus().authState;
  }

  async openConversation() {
    const status = this._liveStatus();
    if (status.authState !== 'AUTHENTICATED') {
      const code = status.authState === 'CHALLENGE_REQUIRED' ? 'CHALLENGE_REQUIRED' :
        (status.authState === 'AUTH_REQUIRED' ? 'AUTH_REQUIRED' :
          (status.authState === 'LOADING' ? 'LOADING' :
            (status.authState === 'AUTH_LOST' ? 'AUTH_LOST' : 'DOM_UNKNOWN')));
      throw new ElectronTransportError(code);
    }
    return status.url || 'https://chatgpt.com/';
  }

  async prepareRequest(request) {
    this.request = request;
    this.response = null;
    this.metrics = null;
    const result = await this._callPage('prepareDesktopRequest', {
      brainRequestId: request.brainRequestId,
      taskId: request.taskId,
    });
    if (!result.ok) throw new ElectronTransportError(result.error || 'DOM_UNKNOWN');
    return result;
  }

  async sendPrompt(prompt, {onWritten = null} = {}) {
    if (!this.request) throw new ElectronTransportError('REQUEST_NOT_PREPARED');
    if (prompt !== this.request.prompt) throw new ElectronTransportError('PROMPT_MISMATCH');
    const written = await this._callPage('writeDesktopPrompt', {prompt});
    if (!written.ok) throw new ElectronTransportError(written.error || 'COMPOSER_WRITE_FAILED');
    if (typeof onWritten === 'function') onWritten(written);
    const sent = await this._callPage('sendDesktopPrompt');
    if (!sent.ok) throw new ElectronTransportError(sent.error || 'SEND_FAILED');
    const result = {...written, ...sent, composerWrite: true};
    this.metrics = result;
    return result;
  }

  async waitForCompletion(timeoutSeconds = this.timeoutSeconds) {
    if (!this.request) throw new ElectronTransportError('REQUEST_NOT_PREPARED');
    const deadline = Date.now() + (timeoutSeconds * 1000);
    while (Date.now() < deadline) {
      const state = await this._callPage('desktopResponseState');
      if (state.error) throw new ElectronTransportError(state.error);
      if (state.completed) {
        if (!state.rawResponse || !state.responseBoundary) {
          throw new ElectronTransportError('RESPONSE_BOUNDARY_FAILURE');
        }
        this.response = state.rawResponse;
        this.metrics = {...this.metrics, ...state};
        return state;
      }
      await new Promise(resolve => setTimeout(resolve, this.pollIntervalMs));
    }
    throw new ElectronTransportError('RESPONSE_TIMEOUT');
  }

  readResponse() {
    if (!this.response) throw new ElectronTransportError('RESPONSE_NOT_AVAILABLE');
    return this.response;
  }

  close() {
    this.request = null;
    this.response = null;
    this.metrics = null;
  }
}

module.exports = {ElectronWebContentsTransport, ElectronTransportError};
