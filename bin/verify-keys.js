#!/usr/bin/env node
/**
 * verify-keys.js
 * Performs live chat completion tests on every configured LLM provider.
 * Redacts any sensitive tokens from errors and outputs.
 */

const https = require('https');

function redact(str) {
  if (typeof str !== 'string') return String(str || '');
  return str.replace(/[a-zA-Z0-9_-]{20,}/g, '[REDACTED_KEY]');
}

function doRequest(options, body) {
  return new Promise((resolve) => {
    const startTime = Date.now();
    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        const latency = Date.now() - startTime;
        let parsed = null;
        try {
          parsed = JSON.parse(data);
        } catch (e) {}
        resolve({
          statusCode: res.statusCode,
          latency,
          raw: data,
          json: parsed
        });
      });
    });

    req.on('error', (err) => {
      resolve({
        statusCode: 0,
        latency: Date.now() - startTime,
        error: err.message
      });
    });

    req.setTimeout(25000, () => {
      req.destroy();
      resolve({
        statusCode: 408,
        latency: 25000,
        error: 'Request timed out after 25s'
      });
    });

    if (body) {
      req.write(typeof body === 'string' ? body : JSON.stringify(body));
    }
    req.end();
  });
}

function extractText(choice) {
  if (!choice || !choice.message) return null;
  const m = choice.message;
  return m.content || m.reasoning || m.reasoning_content || 'OK';
}

async function testGroq() {
  const key = process.env.GROQ_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'openai/gpt-oss-120b',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'api.groq.com',
    path: '/openai/v1/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testMistral() {
  const key = process.env.MISTRAL_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'codestral-latest',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'api.mistral.ai',
    path: '/v1/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testGemini() {
  const key = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'gemini-3.8-flash',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'generativelanguage.googleapis.com',
    path: '/v1beta/openai/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testKilo() {
  const key = process.env.KILO_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'kilo-auto/free',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'api.kilo.ai',
    path: '/api/gateway/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: res.json?.model || body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testGonka() {
  const key = process.env.GONKA_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'deepseek-ai/DeepSeek-V4-Flash-0731',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'proxy.gonkabroker.com',
    path: '/v1/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testNvidia() {
  const key = process.env.NVIDIA_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'openai/gpt-oss-20b',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'integrate.api.nvidia.com',
    path: '/v1/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testAion() {
  const key = process.env.AION_API_KEY || process.env.AIONLABS_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'aion-labs/aion-2.0',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'api.aionlabs.ai',
    path: '/v1/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testOpenRouter() {
  const key = process.env.OPENROUTER_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'openrouter/free',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'openrouter.ai',
    path: '/api/v1/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: res.json?.model || body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testCohere() {
  const key = process.env.COHERE_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'command-r-plus-08-2024',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }]
  };

  const res = await doRequest({
    hostname: 'api.cohere.com',
    path: '/v2/chat',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  if (res.statusCode === 200 && res.json?.message?.content?.[0]?.text) {
    return { status: 'PASS', model: body.model, latency: res.latency, output: res.json.message.content[0].text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.message || (res.raw || '').slice(0, 100)) };
}

async function testZai() {
  const key = process.env.ZAI_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'glm-4.7-flash',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'open.bigmodel.cn',
    path: '/api/paas/v4/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testRequesty() {
  const key = process.env.REQUESTY_API_KEY;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };

  const body = {
    model: 'policy/pennytree-free',
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'router.requesty.ai',
    path: '/v1/chat/completions',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = extractText(res.json?.choices?.[0]);
  if (res.statusCode === 200 && text !== null) {
    return { status: 'PASS', model: body.model, latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.error?.message || (res.raw || '').slice(0, 100)) };
}

async function testCloudflare() {
  const key = process.env.CLOUDFLARE_API_KEY || process.env.CF_API_KEY;
  const accountId = process.env.CF_ACCOUNT_ID || process.env.CLOUDFLARE_ACCOUNT_ID;
  if (!key) return { status: 'SKIPPED', msg: 'Key not set' };
  if (!accountId) return { status: 'SKIPPED', msg: 'CF_ACCOUNT_ID not found in .env (required by Cloudflare)' };

  const body = {
    messages: [{ role: 'user', content: 'Say hello in 3 words.' }],
    max_tokens: 30
  };

  const res = await doRequest({
    hostname: 'api.cloudflare.com',
    path: `/client/v4/accounts/${accountId}/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast`,
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` }
  }, body);

  const text = res.json?.result?.response || (res.json?.result?.choices?.[0]?.message?.content);
  if (res.statusCode === 200 && text) {
    return { status: 'PASS', model: '@cf/meta/llama-3.3-70b', latency: res.latency, output: text.trim().slice(0, 60) };
  }
  return { status: 'FAIL', code: res.statusCode, latency: res.latency, error: redact(res.json?.errors?.[0]?.message || (res.raw || '').slice(0, 100)) };
}

async function main() {
  console.log('\n===============================================================');
  console.log('       🧪 Live LLM API Key & Inference Verification           ');
  console.log('===============================================================\n');

  const tests = [
    { name: 'Groq', fn: testGroq },
    { name: 'Mistral AI', fn: testMistral },
    { name: 'Kilo Code', fn: testKilo },
    { name: 'Google Gemini', fn: testGemini },
    { name: 'Gonka Broker', fn: testGonka },
    { name: 'NVIDIA NIM', fn: testNvidia },
    { name: 'Aion Labs', fn: testAion },
    { name: 'OpenRouter', fn: testOpenRouter },
    { name: 'Cohere', fn: testCohere },
    { name: 'Zhipu AI (Z AI)', fn: testZai },
    { name: 'Requesty', fn: testRequesty },
    { name: 'Cloudflare Workers AI', fn: testCloudflare }
  ];

  for (const t of tests) {
    process.stdout.write(`Testing ${t.name.padEnd(18)} ... `);
    const r = await t.fn();
    if (r.status === 'PASS') {
      console.log(`\x1b[1;32m[PASS]\x1b[0m (${r.latency}ms) [${r.model}]`);
      console.log(`  └─ Output: "${r.output.replace(/\n/g, ' ')}"`);
    } else if (r.status === 'SKIPPED') {
      console.log(`\x1b[2m[SKIPPED]\x1b[0m ${r.msg}`);
    } else {
      console.log(`\x1b[1;31m[FAIL]\x1b[0m (HTTP ${r.code || 0}, ${r.latency}ms)`);
      console.log(`  └─ Error: ${r.error}`);
    }
  }

  console.log('\n===============================================================\n');
}

main();
