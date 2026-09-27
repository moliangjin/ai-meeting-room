"use strict";

const {randomUUID} = require('node:crypto');

const IN_PROGRESS_STAGES = new Set([
  'PREPARING', 'WRITING', 'SENDING', 'WAITING_RESPONSE', 'PARSING',
]);
const TERMINAL_STAGES = new Set(['PASSED', 'FAILED', 'INTERRUPTED']);

class PocRunStateError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'PocRunStateError';
    this.code = code;
  }
}

class DesktopPocRunManager {
  constructor({now = () => new Date().toISOString(), idFactory = randomUUID} = {}) {
    this.now = now;
    this.idFactory = idFactory;
    this.current = null;
    this.last = null;
  }

  begin() {
    if (this.current && IN_PROGRESS_STAGES.has(this.current.stage)) {
      throw new PocRunStateError('POC_ALREADY_RUNNING', 'a Desktop Brain POC is already running');
    }
    const run = {
      runId: this.idFactory(),
      startedAt: this.now(),
      stage: 'PREPARING',
      finishedAt: null,
      result: null,
      failureClass: null,
    };
    this.current = run;
    return {...run};
  }

  transition(runId, stage) {
    this._requireCurrent(runId);
    if (!IN_PROGRESS_STAGES.has(stage)) throw new PocRunStateError('INVALID_POC_STAGE');
    this.current.stage = stage;
    return {...this.current};
  }

  finish(runId, stage, {result = null, failureClass = null} = {}) {
    this._requireCurrent(runId);
    if (!TERMINAL_STAGES.has(stage)) throw new PocRunStateError('INVALID_POC_TERMINAL_STAGE');
    this.current.stage = stage;
    this.current.finishedAt = this.now();
    this.current.result = result;
    this.current.failureClass = failureClass;
    this.last = {...this.current};
    this.current = null;
    return {...this.last};
  }

  restore(snapshot) {
    const candidate = snapshot && snapshot.current;
    if (!candidate || !IN_PROGRESS_STAGES.has(candidate.stage)) {
      this.current = null;
      this.last = snapshot?.last ? {...snapshot.last} : null;
      return this.snapshot();
    }
    const interrupted = {
      ...candidate,
      stage: 'INTERRUPTED',
      finishedAt: this.now(),
      failureClass: 'INTERRUPTED',
    };
    this.current = null;
    this.last = interrupted;
    return this.snapshot();
  }

  snapshot() {
    return {current: this.current ? {...this.current} : null, last: this.last ? {...this.last} : null};
  }

  _requireCurrent(runId) {
    if (!this.current || this.current.runId !== runId) throw new PocRunStateError('POC_RUN_NOT_FOUND');
  }
}

module.exports = {DesktopPocRunManager, PocRunStateError, IN_PROGRESS_STAGES, TERMINAL_STAGES};
