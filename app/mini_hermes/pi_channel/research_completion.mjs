#!/usr/bin/env node
// Single model completion only. PydanticAI, not this process, executes tools.
// Importing this module must not initialize pi or read any credential file.
import { pathToFileURL } from 'node:url';

export const MAX_BYTES = 2 * 1024 * 1024;
export const DEADLINE_MS = 120_000;
const MODEL = 'grok-4.6';
const PROVIDER = 'xai';
const API = 'openai-responses';
const THINKING_LEVELS = new Set(['low', 'medium', 'high', 'xhigh']);
const PI_ROOT = '/usr/lib/node_modules/@earendil-works/pi-coding-agent';
const AUTH_PATH = '/var/lib/mini-hermes-pi/agent/auth.json';
const TOOL_NAME = /^[A-Za-z0-9_-]{1,64}$/;
const FAILURE_CODES = new Set([
  'auth_required', 'quota', 'rate_limited', 'timeout', 'invalid_request',
  'model_unavailable', 'output_limit', 'upstream_error',
]);

class InvalidRequest extends Error {}
const demand = (condition) => { if (!condition) throw new InvalidRequest(); };
const record = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value, max = 1024 * 1024) => typeof value === 'string' && value.length <= max;
const timestamp = (value) => Number.isSafeInteger(value) && value >= 0;
function keys(value, allowed) {
  demand(record(value));
  demand(Object.keys(value).every((key) => allowed.includes(key)));
}
function optionalString(value, key, max) {
  if (value[key] !== undefined) demand(text(value[key], max));
}
function jsonValue(value, depth = 0, budget = { nodes: 0 }) {
  demand(depth <= 32 && ++budget.nodes <= 100_000);
  if (value === null || typeof value === 'boolean') return;
  if (typeof value === 'string') { demand(text(value)); return; }
  if (typeof value === 'number') { demand(Number.isFinite(value)); return; }
  demand(Array.isArray(value) || record(value));
  for (const child of Object.values(value)) jsonValue(child, depth + 1, budget);
}
function validateContent(content, assistant = false) {
  demand(Array.isArray(content) && content.length <= 128);
  for (const block of content) {
    demand(record(block));
    if (block.type === 'text') {
      keys(block, ['type', 'text', 'textSignature']);
      demand(text(block.text));
      optionalString(block, 'textSignature', 1024 * 1024);
    } else if (assistant && block.type === 'thinking') {
      keys(block, ['type', 'thinking', 'thinkingSignature', 'redacted']);
      demand(text(block.thinking));
      optionalString(block, 'thinkingSignature', 1024 * 1024);
      if (block.redacted !== undefined) demand(typeof block.redacted === 'boolean');
    } else if (assistant && block.type === 'toolCall') {
      keys(block, ['type', 'id', 'name', 'arguments', 'thoughtSignature', 'namespace']);
      demand(text(block.id, 512) && block.id.length > 0 &&
        typeof block.name === 'string' && TOOL_NAME.test(block.name));
      demand(record(block.arguments));
      jsonValue(block.arguments);
      optionalString(block, 'thoughtSignature', 1024 * 1024);
      optionalString(block, 'namespace', 128);
    } else {
      throw new InvalidRequest();
    }
  }
}
function validateAssistant(message) {
  keys(message, ['role', 'content', 'api', 'provider', 'model', 'responseModel',
    'responseId', 'providerThinkingLevel', 'diagnostics', 'usage', 'stopReason',
    'errorMessage', 'rawStopReason', 'endTurn', 'timestamp']);
  demand(message.role === 'assistant' && message.api === API &&
    message.provider === PROVIDER && message.model === MODEL && timestamp(message.timestamp));
  demand(['stop', 'toolUse', 'length', 'error', 'aborted'].includes(message.stopReason));
  validateContent(message.content, true);
  demand(record(message.usage));
  jsonValue(message.usage);
  for (const key of ['responseModel', 'responseId', 'providerThinkingLevel', 'rawStopReason']) {
    optionalString(message, key, 1024);
  }
  optionalString(message, 'errorMessage', 16 * 1024);
  if (message.endTurn !== undefined) demand(typeof message.endTurn === 'boolean');
  if (message.diagnostics !== undefined) {
    demand(Array.isArray(message.diagnostics) && message.diagnostics.length <= 32);
    jsonValue(message.diagnostics);
  }
}

export function validateRequest(input) {
  keys(input, ['systemPrompt', 'messages', 'tools', 'maxTokens', 'thinking']);
  const thinking = input.thinking === undefined ? 'low' : input.thinking;
  demand(typeof thinking === 'string' && THINKING_LEVELS.has(thinking));
  demand(Buffer.byteLength(JSON.stringify(input), 'utf8') <= MAX_BYTES);
  if (input.systemPrompt !== undefined) demand(text(input.systemPrompt, 64 * 1024));
  demand(Array.isArray(input.messages) && input.messages.length >= 1 && input.messages.length <= 128);
  const tools = input.tools === undefined ? [] : input.tools;
  demand(Array.isArray(tools) && tools.length <= 128);
  const names = new Set();
  for (const tool of tools) {
    keys(tool, ['name', 'description', 'parameters']);
    demand(typeof tool.name === 'string' && TOOL_NAME.test(tool.name) && !names.has(tool.name));
    names.add(tool.name);
    demand(text(tool.description, 16 * 1024) && record(tool.parameters));
    demand(tool.parameters.type === 'object');
    jsonValue(tool.parameters);
  }
  const calls = new Map();
  const completed = new Set();
  for (const message of input.messages) {
    demand(record(message));
    if (message.role === 'user') {
      keys(message, ['role', 'content', 'timestamp']);
      demand(timestamp(message.timestamp));
      if (typeof message.content === 'string') demand(text(message.content));
      else validateContent(message.content);
    } else if (message.role === 'assistant') {
      validateAssistant(message);
      demand(!message.errorMessage && !['error', 'aborted'].includes(message.stopReason));
      for (const block of message.content) {
        if (block.type === 'toolCall') {
          demand(!calls.has(block.id));
          calls.set(block.id, block.name);
        }
      }
    } else if (message.role === 'toolResult') {
      keys(message, ['role', 'toolCallId', 'toolName', 'content', 'details', 'usage',
        'addedToolNames', 'isError', 'timestamp']);
      demand(timestamp(message.timestamp) && typeof message.isError === 'boolean');
      demand(text(message.toolCallId, 512) && calls.has(message.toolCallId) &&
        typeof message.toolName === 'string' && calls.get(message.toolCallId) === message.toolName);
      demand(!completed.has(message.toolCallId));
      completed.add(message.toolCallId);
      validateContent(message.content);
      if (message.details !== undefined) jsonValue(message.details);
      if (message.usage !== undefined) { demand(record(message.usage)); jsonValue(message.usage); }
      if (message.addedToolNames !== undefined) {
        demand(Array.isArray(message.addedToolNames) && message.addedToolNames.length <= 128);
        demand(message.addedToolNames.every((name) => names.has(name)));
      }
    } else {
      throw new InvalidRequest();
    }
  }
  // Every historical call must have its matching result before another model request.
  demand(calls.size === completed.size);
  const maxTokens = input.maxTokens === undefined ? 4096 : input.maxTokens;
  demand(Number.isInteger(maxTokens) && maxTokens >= 1 && maxTokens <= 8192);
  return { context: {systemPrompt: input.systemPrompt ?? '', messages: input.messages, tools}, maxTokens, thinking };
}

export function classifyError(error) {
  // Inspect locally for classification only. Never return or log this text.
  const value = typeof error === 'string' ? error :
    (typeof error?.message === 'string' ? error.message : '');
  if (/abort|timed?\s*out|timeout/i.test(value)) return 'timeout';
  if (/\b429\b|rate.?limit|too many requests/i.test(value)) return 'rate_limited';
  if (/quota|billing|payment|past due|insufficient.*credit|credit.*exhaust|\b402\b/i.test(value)) return 'quota';
  if (/\b401\b|\b403\b|unauthori|forbidden|oauth|credential|not configured|api.?key/i.test(value)) return 'auth_required';
  return 'upstream_error';
}

async function productionRuntime(signal) {
  const { ModelRuntime } = await import(pathToFileURL(`${PI_ROOT}/dist/core/model-runtime.js`).href);
  return ModelRuntime.create({authPath: AUTH_PATH, modelsPath: null,
    refreshOnCreate: false, allowModelNetwork: false, signal});
}

export async function completeResearch(input, {
  env = process.env, loadRuntime = productionRuntime, signal: outerSignal,
  timeoutMs = DEADLINE_MS,
} = {}) {
  let request;
  try { request = validateRequest(input); }
  catch { return {ok: false, error_code: 'invalid_request'}; }
  if (env.PI_CHANNEL_MODEL !== `${PROVIDER}/${MODEL}`) {
    return {ok: false, error_code: 'model_unavailable'};
  }
  const controller = new AbortController();
  const abort = () => controller.abort();
  outerSignal?.addEventListener('abort', abort, {once: true});
  if (outerSignal?.aborted) abort();
  const timer = setTimeout(abort, timeoutMs);
  let removeAbort;
  const aborted = new Promise((resolve) => {
    const onAbort = () => resolve({ok: false, error_code: 'timeout'});
    removeAbort = () => controller.signal.removeEventListener('abort', onAbort);
    controller.signal.addEventListener('abort', onAbort, {once: true});
    if (controller.signal.aborted) onAbort();
  });
  const run = async () => {
    try {
      if (controller.signal.aborted) return {ok: false, error_code: 'timeout'};
      const runtime = await loadRuntime(controller.signal);
      if (controller.signal.aborted) return {ok: false, error_code: 'timeout'};
      const model = runtime.getModel(PROVIDER, MODEL);
      if (!model || model.provider !== PROVIDER || model.id !== MODEL ||
          model.api !== API || model.baseUrl !== 'https://api.x.ai/v1') {
        return {ok: false, error_code: 'model_unavailable'};
      }
      const message = await runtime.completeSimple(model, request.context, {
        reasoning: request.thinking, maxTokens: request.maxTokens, maxRetries: 0,
        timeoutMs, signal: controller.signal,
      });
      if (controller.signal.aborted || message?.stopReason === 'aborted') {
        return {ok: false, error_code: 'timeout'};
      }
      if (message?.stopReason === 'error' || message?.errorMessage) {
        return {ok: false, error_code: classifyError(message?.errorMessage)};
      }
      if (message?.stopReason === 'length') return {ok: false, error_code: 'output_limit'};
      if (!['stop', 'toolUse'].includes(message?.stopReason)) {
        return {ok: false, error_code: 'upstream_error'};
      }
      // Native diagnostics can contain raw exception messages/stacks and
      // arbitrary request details. None of these fields is needed for replay.
      // Preserve content signatures/IDs and model/usage metadata unmodified.
      const {diagnostics, rawStopReason, errorMessage, ...safeMessage} = message;
      validateAssistant(safeMessage);
      if (!safeMessage.content.some((block) => block.type === 'toolCall' ||
          (block.type === 'text' && block.text.trim()))) {
        return {ok: false, error_code: 'upstream_error'};
      }
      const result = {ok: true, message: safeMessage};
      if (Buffer.byteLength(JSON.stringify(result) + '\n', 'utf8') > MAX_BYTES) {
        return {ok: false, error_code: 'output_limit'};
      }
      return result;
    } catch (error) {
      return {ok: false, error_code: controller.signal.aborted ? 'timeout' : classifyError(error)};
    }
  };
  try { return await Promise.race([run(), aborted]); }
  finally {
    clearTimeout(timer);
    removeAbort?.();
    outerSignal?.removeEventListener('abort', abort);
  }
}

export function encodeResult(result) {
  try {
    if (result?.ok !== true && !FAILURE_CODES.has(result?.error_code)) throw new Error();
    const encoded = JSON.stringify(result) + '\n';
    if (Buffer.byteLength(encoded, 'utf8') <= MAX_BYTES) return encoded;
    return '{"ok":false,"error_code":"output_limit"}\n';
  } catch { return '{"ok":false,"error_code":"upstream_error"}\n'; }
}

export async function readRequest(stream, signal) {
  const chunks = [];
  let size = 0;
  const aborted = () => stream.destroy(new Error('timeout'));
  signal?.addEventListener('abort', aborted, {once: true});
  try {
    if (signal?.aborted) throw new Error('timeout');
    for await (const chunk of stream) {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += bytes.length;
      if (size > MAX_BYTES) throw new InvalidRequest();
      chunks.push(bytes);
    }
    return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(Buffer.concat(chunks)));
  } finally { signal?.removeEventListener('abort', aborted); }
}

async function main() {
  const controller = new AbortController();
  const stop = () => controller.abort();
  const timer = setTimeout(stop, DEADLINE_MS);
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
  let result;
  try {
    const input = await readRequest(process.stdin, controller.signal);
    result = await completeResearch(input, {signal: controller.signal});
  } catch {
    result = {ok: false, error_code: controller.signal.aborted ? 'timeout' : 'invalid_request'};
  } finally {
    clearTimeout(timer);
    process.removeListener('SIGTERM', stop);
    process.removeListener('SIGINT', stop);
  }
  process.stdout.write(encodeResult(result));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
