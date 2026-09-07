import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import test from 'node:test';
import { completeResearch, validateRequest, classifyError, encodeResult, readRequest, MAX_BYTES } from '../app/mini_hermes/pi_channel/research_completion.mjs';

const env = {PI_CHANNEL_MODEL:'xai/grok-4.6', PI_CHANNEL_THINKING:'low'};
const model = {provider:'xai', id:'grok-4.6', api:'openai-responses', baseUrl:'https://api.x.ai/v1'};
const usage = {input:12,output:8,cacheRead:3,cacheWrite:0,totalTokens:23,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
const user = {role:'user',content:'研究测试标的',timestamp:1};
const request = () => ({systemPrompt:'只做研究',messages:[structuredClone(user)],tools:[]});
const assistant = (changes = {}) => ({role:'assistant',content:[{type:'text',text:'已取得数据',textSignature:'opaque-text'}],
  provider:'xai',model:'grok-4.6',api:'openai-responses',stopReason:'stop',usage:structuredClone(usage),timestamp:2,...changes});
const mock = (message, inspect) => async () => ({getModel:()=>model,completeSimple:async (...args)=>{inspect?.(...args);return message;}});

test('successful completion preserves native response, signatures, usage; requests low and no retries', async () => {
  const native = assistant({content:[{type:'thinking',thinking:'',thinkingSignature:'{"type":"reasoning","encrypted_content":"opaque"}'},
    {type:'text',text:'研究结论',textSignature:'opaque-text'}]});
  const result = await completeResearch(request(), {env,loadRuntime:mock(native, (selected, context, options)=>{
    assert.equal(selected,model); assert.equal(context.tools.length,0);
    assert.equal(options.reasoning,'low'); assert.equal(options.maxTokens,4096);
    assert.equal(options.maxRetries,0); assert.equal(options.timeoutMs,120000);
    assert.ok(options.signal instanceof AbortSignal);
    assert.equal(options.apiKey,undefined); assert.equal(options.headers,undefined);
  })});
  assert.equal(result.ok,true); assert.deepEqual(result.message,native);
  assert.equal(result.message.content,native.content);
});

test('tool-only response is successful and matching results preserve complete IDs for next round', async () => {
  const call={type:'toolCall',id:'call_1|fc_item_1',name:'stock_quote',arguments:{symbol:'600000'}};
  const native=assistant({content:[call],stopReason:'toolUse'});
  const first=request(); first.tools=[{name:'stock_quote',description:'行情',parameters:{type:'object',properties:{symbol:{type:'string'}}}}];
  const result=await completeResearch(first,{env,loadRuntime:mock(native)});
  assert.equal(result.ok,true); assert.equal(result.message.content[0].id,call.id);
  first.messages.push(result.message,{role:'toolResult',toolCallId:call.id,toolName:call.name,content:[{type:'text',text:'工具返回'}],isError:false,timestamp:3});
  assert.equal(validateRequest(first).context.messages[2].toolCallId,call.id);
});

test('payload cannot override runtime/auth/URLs/options or execute functions', async () => {
  for (const field of ['model','provider','baseUrl','apiKey','headers','env','samplingParams','onPayload','action']) {
    const input=request(); input[field]='forbidden';
    const result=await completeResearch(input,{env,loadRuntime:()=>{throw new Error('must not load');}});
    assert.equal(result.error_code,'invalid_request',field);
  }
  const input=request();input.tools=[{name:'tool',description:'x',parameters:{type:'object'},execute:'forbidden'}];
  assert.throws(()=>validateRequest(input));
});

test('user images and tool-result images are rejected at the relay boundary', () => {
  const image={type:'image',data:'aGVsbG8=',mimeType:'image/png'};
  assert.throws(()=>validateRequest({...request(),messages:[{...user,content:[image]}]}));
  const call={type:'toolCall',id:'call_image',name:'stock_quote',arguments:{}};
  assert.throws(()=>validateRequest({...request(),messages:[user,
    assistant({content:[call],stopReason:'toolUse'}),
    {role:'toolResult',toolCallId:call.id,toolName:call.name,content:[image],isError:false,timestamp:3},
  ]}));
});

test('successful replies omit raw diagnostics while preserving replay signatures and IDs', async () => {
  const native=assistant({responseId:'resp_1',providerThinkingLevel:'low',rawStopReason:'private-debug-marker',errorMessage:'',
    diagnostics:[{type:'retry',timestamp:1,error:{message:'private-upstream-marker',stack:'private-stack-marker'},
      details:{headers:{Authorization:'Bearer private-auth-marker'}}}]});
  const result=await completeResearch(request(),{env,loadRuntime:mock(native)});
  assert.equal(result.ok,true);assert.equal(result.message.responseId,'resp_1');
  assert.equal(result.message.providerThinkingLevel,'low');assert.equal(result.message.content,native.content);
  assert.equal(result.message.usage,native.usage);
  for (const field of ['diagnostics','rawStopReason','errorMessage']) assert.equal(field in result.message,false);
  assert.ok(!encodeResult(result).includes('private-'));
  assert.ok(native.diagnostics,'upstream message is not mutated');
});

test('model and thinking environment fail closed before runtime loads', async () => {
  for (const wrongEnv of [{},{...env,PI_CHANNEL_MODEL:'xai/grok-4.5'},{...env,PI_CHANNEL_THINKING:'high'}]) {
    const result=await completeResearch(request(),{env:wrongEnv,loadRuntime:()=>{throw new Error('must not load');}});
    assert.equal(result.error_code,'model_unavailable');
  }
});

test('resolved model cannot change API or endpoint', async () => {
  for (const bad of [undefined,{...model,api:'openai-completions'},{...model,baseUrl:'https://attacker.invalid'}]) {
    const result=await completeResearch(request(),{env,loadRuntime:async()=>({getModel:()=>bad,completeSimple:()=>{throw new Error('must not call');}})});
    assert.equal(result.error_code,'model_unavailable');
  }
});

test('native errors and partial replies are classified without leakage', async () => {
  for (const [error,expected] of [['403 forbidden https://secret.invalid/?token=hidden','auth_required'],['subscription payment past due','quota'],['429 too many requests','rate_limited'],['request timed out','timeout'],['private text','upstream_error']]) {
    const result=await completeResearch(request(),{env,loadRuntime:mock(assistant({stopReason:'error',errorMessage:error}))});
    assert.deepEqual(result,{ok:false,error_code:expected});
    assert.ok(!encodeResult(result).includes('hidden'));
  }
  assert.equal(classifyError(new Error('OAuth refresh failed')),'auth_required');
});

test('empty, thinking-only, truncated and deferred responses are not final successes', async () => {
  for (const [native,code] of [[assistant({content:[]}),'upstream_error'],[assistant({content:[{type:'thinking',thinking:'still reasoning'}]}),'upstream_error'],[assistant({stopReason:'length'}),'output_limit'],[assistant({stopReason:'deferred'}),'upstream_error'],[assistant({stopReason:'aborted'}),'timeout']]) {
    assert.equal((await completeResearch(request(),{env,loadRuntime:mock(native)})).error_code,code);
  }
});

test('overall deadline covers runtime/OAuth initialization and signals cancellation', async () => {
  let receivedSignal;
  const result=await completeResearch(request(),{env,timeoutMs:10,loadRuntime:async(signal)=>{receivedSignal=signal;return new Promise(()=>{});}});
  assert.equal(result.error_code,'timeout'); assert.equal(receivedSignal.aborted,true);
});

test('external cancellation terminates an in-flight completion', async () => {
  const controller=new AbortController();let receivedSignal;
  const pending=completeResearch(request(),{env,signal:controller.signal,loadRuntime:async()=>({getModel:()=>model,completeSimple:async(_m,_c,options)=>{
    receivedSignal=options.signal;controller.abort();return new Promise(()=>{});
  }})});
  assert.equal((await pending).error_code,'timeout');assert.equal(receivedSignal.aborted,true);
});

test('invalid limits, tool definitions, unpaired results and foreign native messages fail', () => {
  for (const maxTokens of [null,0,-1,8193,1.5,'4096']) assert.throws(()=>validateRequest({...request(),maxTokens}));
  assert.equal(validateRequest({...request(),maxTokens:8192}).maxTokens,8192);
  assert.throws(()=>validateRequest({...request(),messages:Array(129).fill(user)}));
  assert.throws(()=>validateRequest({...request(),tools:[{name:'bad tool',description:'x',parameters:{type:'object'}}]}));
  assert.throws(()=>validateRequest({...request(),tools:null}));
  assert.throws(()=>validateRequest({...request(),messages:[user,{role:'toolResult',toolCallId:'unknown',toolName:'x',content:[],isError:false,timestamp:1}]}));
  assert.throws(()=>validateRequest({...request(),messages:[user,{role:'toolResult',toolCallId:'unknown',content:[],isError:false,timestamp:1}]}));
  assert.throws(()=>validateRequest({...request(),messages:[user,assistant({stopReason:'toolUse',content:[{type:'toolCall',id:'call_1',arguments:{}}]})]}));
  assert.throws(()=>validateRequest({...request(),messages:[user,assistant({model:'grok-4.5'})]}));
});

test('stdin accepts exactly one JSON document and enforces 2 MiB byte limit', async () => {
  assert.deepEqual(await readRequest(Readable.from([JSON.stringify(request())+'\n'])),request());
  await assert.rejects(readRequest(Readable.from(['{}\n{}'])));
  await assert.rejects(readRequest(Readable.from([Buffer.from([0x22,0xff,0x22])])));
  await assert.rejects(readRequest(Readable.from([Buffer.alloc(MAX_BYTES+1,32)])));
});

test('stdout never exceeds 2 MiB or serializes unexpected error objects', async () => {
  const large=assistant({content:[{type:'text',text:'中'.repeat(800000)}]});
  const result=await completeResearch(request(),{env,loadRuntime:mock(large)});
  assert.equal(result.error_code,'output_limit');
  assert.equal(encodeResult({ok:false,error_code:'secret exception'}),'{"ok":false,"error_code":"upstream_error"}\n');
  assert.ok(Buffer.byteLength(encodeResult(result))<=MAX_BYTES);
});
