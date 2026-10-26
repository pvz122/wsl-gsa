'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const { once } = require('node:events');
const { destination, ipv4Number, createBridge } = require('../windows/proxy.cjs');

test('IPv4 CONNECT supports every TCP port, not IPv6/URLs/names/injection', () => {
  for (const port of [1, 22, 80, 443, 5432, 65535]) {
    assert.deepEqual(destination(`192.0.2.1:${port}`), { host: '192.0.2.1', port });
  }
  for (const target of ['192.0.2.1:0', '192.0.2.1:65536', '[::1]:443',
    'example.com:443', 'user@192.0.2.1:443', '192.0.2.1:80/path', '192.0.2.1:80\r\nX:y']) {
    assert.throws(() => destination(target));
  }
});

test('unsigned subnet arithmetic', () => {
  assert.equal(ipv4Number('255.255.255.255'), 4294967295);
  assert.equal((ipv4Number('192.0.2.17') & ipv4Number('255.255.255.0')) >>> 0,
    ipv4Number('192.0.2.0'));
});

async function bridgeTest(fn) {
  const server = createBridge({ address: '127.0.0.1', netmask: '255.255.255.255' });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try { await fn(server.address().port); }
  finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
}

function request(port, path, options = {}, body) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path, ...options }, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString() }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

test('health identity and unknown endpoint', async () => bridgeTest(async port => {
  const result = await request(port, '/__wsl_gsa_health');
  assert.equal(result.status, 200);
  const health = JSON.parse(result.body);
  assert.equal(health.pid, process.pid);
  assert.equal(health.version, 3);
  assert.deepEqual(health.capabilities, ['tcp-connect', 'dns-wire']);
  assert.equal((await request(port, '/unrecognized')).status, 404);
}));

test('requests from outside the configured subnet are denied', async () => bridgeTest(async port => {
  const result = await request(port, '/__wsl_gsa_health', { localAddress: '127.0.0.2' });
  assert.equal(result.status, 403);
}));

test('invalid DNS rejected without forwarding', async () => bridgeTest(async port => {
  const result = await request(port, '/__wsl_gsa_dns', { method: 'POST' }, Buffer.alloc(12));
  assert.equal(result.status, 400);
}));

test('CONNECT transports arbitrary binary bytes on a non-HTTPS port', async () => {
  const echo = net.createServer(socket => socket.pipe(socket));
  echo.listen(0, '127.0.0.1');
  await once(echo, 'listening');
  try {
    await bridgeTest(async port => {
      const req = http.request({ host: '127.0.0.1', port, method: 'CONNECT',
        path: `127.0.0.1:${echo.address().port}` });
      req.end();
      const [response, socket] = await once(req, 'connect');
      assert.equal(response.statusCode, 200);
      try {
        const payload = Buffer.from([0, 1, 2, 127, 128, 255]);
        socket.write(payload);
        const [received] = await once(socket, 'data');
        assert.deepEqual(received, payload);
      } finally { socket.destroy(); }
    });
  } finally { await new Promise(resolve => echo.close(resolve)); }
});
