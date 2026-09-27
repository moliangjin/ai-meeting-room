"use strict";

// Electron IPC accepts structured-clone values, but this boundary is kept
// stricter on purpose: only JSON-like DTOs may leave the main process. This
// prevents Playwright handles and other host-only objects from crossing IPC.

function constructorName(value) {
  return value?.constructor?.name || Object.prototype.toString.call(value).slice(8, -1) || 'UNKNOWN';
}

function findNonSerializableField(value, fieldPath = 'result', ancestors = new WeakSet()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return null;
  if (typeof value === 'number') return Number.isFinite(value) ? null : {path: fieldPath, constructorName: 'NonFiniteNumber'};
  if (typeof value === 'undefined' || typeof value === 'function' || typeof value === 'symbol' || typeof value === 'bigint') {
    return {path: fieldPath, constructorName: typeof value === 'undefined' ? 'undefined' : typeof value};
  }
  if (typeof value !== 'object') return {path: fieldPath, constructorName: typeof value};
  if (ancestors.has(value)) return {path: fieldPath, constructorName: 'CircularReference'};
  if (value instanceof Map || value instanceof Set || value instanceof WeakMap || value instanceof WeakSet || value instanceof Promise) {
    return {path: fieldPath, constructorName: constructorName(value)};
  }
  if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype) {
    return {path: fieldPath, constructorName: constructorName(value)};
  }
  ancestors.add(value);
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const issue = findNonSerializableField(value[index], `${fieldPath}[${index}]`, ancestors);
      if (issue) return issue;
    }
  } else {
    for (const key of Object.keys(value)) {
      const issue = findNonSerializableField(value[key], `${fieldPath}.${key}`, ancestors);
      if (issue) return issue;
    }
  }
  ancestors.delete(value);
  return null;
}

function assertIpcSerializable(value) {
  const issue = findNonSerializableField(value);
  if (issue) {
    const error = new Error(`IPC result is not serializable at ${issue.path}`);
    error.code = 'IPC_RESULT_NOT_SERIALIZABLE';
    error.stage = 'T15_IPC_RESULT_SERIALIZATION';
    error.fieldPath = issue.path;
    error.constructorName = issue.constructorName;
    throw error;
  }
  try {
    // Keep the platform clone probe even after the stricter recursive check.
    globalThis.structuredClone(value);
  } catch (cause) {
    const error = new Error('IPC result failed structured-clone validation');
    error.code = 'IPC_RESULT_NOT_SERIALIZABLE';
    error.stage = 'T15_IPC_RESULT_SERIALIZATION';
    error.fieldPath = 'result';
    error.constructorName = constructorName(cause);
    error.cause = cause;
    throw error;
  }
  return value;
}

function serializeError(error, attemptId = 'UNKNOWN') {
  return {
    name: String(error?.name || 'Error'),
    message: String(error?.message || 'Desktop connection request failed').slice(0, 500),
    code: String(error?.code || 'PRODUCT_SHELL_REQUEST_FAILED'),
    stage: String(error?.stage || 'T1_IPC_REQUEST_SENT'),
    attemptId: String(error?.attemptId || attemptId || 'UNKNOWN'),
  };
}

module.exports = {assertIpcSerializable, findNonSerializableField, serializeError};
