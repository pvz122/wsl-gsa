'use strict';

// Only host-native sockets enter GSA. This server does not decrypt TLS.
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const dns = require('node:dns');
const dgram = require('node:dgram');
const fs = require('node:fs');
const path = require('node:path');

function ipv4Number(address) {
  return address.split('.').reduce((value, part) => (value << 8) | Number(part), 0) >>> 0;
}

function destination(authority) {
  const match = /^([^:/?#@\s]+):([0-9]{1,5})$/.exec(authority);
  if (!match || !net.isIPv4(match[1])) throw new Error('IPv4 CONNECT authority required');
  const port = Number(match[2]);
  if (port < 1 || port > 65535) throw new Error('Invalid TCP port');
  return { host: match[1], port };
}

function report(error) {
  // Error messages can contain destinations; record only a bounded error category.
  const category = /^[A-Z0-9_]+$/.test(error.code || '') ? error.code : error.name;
  const file = path.join(__dirname, 'bridge.log');
  if (fs.existsSync(file) && fs.statSync(file).size > 1024 * 1024) fs.truncateSync(file, 0);
  fs.appendFileSync(file, `${new Date().toISOString()} ${category}\n`);
}

function dnsQuery(query, callback) {
  const upstream = dns.getServers().find(server => net.isIPv4(server));
  if (!upstream) { callback(new Error('No IPv4 DNS server')); return; }
  const socket = dgram.createSocket('udp4');
  let finished = false;
  const timer = setTimeout(() => finish(new Error('DNS timeout')), 5000);
  function finish(error, answer) {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    socket.close();
    callback(error, answer);
  }
  socket.on('error', error => finish(error));
  socket.on('message', answer => {
    if (answer.length < 12 || answer.readUInt16BE(0) !== query.readUInt16BE(0) ||
        !(answer[2] & 0x80)) {
      finish(new Error('Invalid DNS reply'));
    } else finish(null, answer);
  });
  socket.connect(53, upstream, () => {
    socket.send(query, error => { if (error) finish(error); });
  });
}

function createBridge(adapter) {
  const mask = ipv4Number(adapter.netmask);
  const network = (ipv4Number(adapter.address) & mask) >>> 0;
  const allowed = socket => net.isIPv4(socket.remoteAddress) &&
    ((ipv4Number(socket.remoteAddress) & mask) >>> 0) === network;
  let dnsPending = 0;
  const server = http.createServer((request, response) => {
    if (!allowed(request.socket)) { response.writeHead(403).end(); return; }
    if (request.method === 'GET' && request.url === '/__wsl_gsa_health') {
      response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
        .end(JSON.stringify({ application: 'wsl-gsa-proxy', pid: process.pid,
          host: adapter.address, port: 18080, version: 3,
          capabilities: ['tcp-connect', 'dns-wire'] }));
      return;
    }
    if (request.method !== 'POST' || request.url !== '/__wsl_gsa_dns') {
      response.writeHead(404).end();
      return;
    }
    if (dnsPending >= 128) { response.writeHead(503).end(); return; }
    dnsPending++;
    let released = false;
    let forwarding = false;
    const release = () => { if (!released) { released = true; dnsPending--; } };
    response.on('close', () => { if (!forwarding) release(); });
    const chunks = [];
    let size = 0;
    request.on('error', error => { report(error); if (!forwarding) release(); });
    request.on('data', chunk => {
      size += chunk.length;
      if (size > 65535) { request.destroy(); release(); }
      else chunks.push(chunk);
    });
    request.on('end', () => {
      const query = Buffer.concat(chunks);
      if (query.length < 12 || query.readUInt16BE(4) !== 1 || (query[2] & 0x80)) {
        response.writeHead(400).end();
        return;
      }
      forwarding = true;
      dnsQuery(query, (error, answer) => {
        release();
        if (response.destroyed) return;
        if (error) { report(error); response.writeHead(502).end(); }
        else response.writeHead(200, { 'Content-Type': 'application/dns-message' }).end(answer);
      });
    });
  });
  server.requestTimeout = 10000;
  server.headersTimeout = 10000;
  server.maxConnections = 512;
  server.on('connect', (request, client, head) => {
    client.on('error', report);
    if (!allowed(client)) { client.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return; }
    let target;
    try { target = destination(request.url); }
    catch { client.end('HTTP/1.1 400 Bad Request\r\n\r\n'); return; }
    const upstream = net.connect({ ...target, family: 4 });
    const timer = setTimeout(() => upstream.destroy(new Error('Connect timeout')), 10000);
    upstream.once('connect', () => {
      clearTimeout(timer);
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) upstream.write(head);
      client.pipe(upstream);
      upstream.pipe(client);
    });
    upstream.on('error', error => {
      clearTimeout(timer);
      report(error);
      client.destroy();
    });
    upstream.on('close', () => client.destroy());
    client.on('close', () => { clearTimeout(timer); upstream.destroy(); });
    client.on('error', () => upstream.destroy());
  });
  server.on('clientError', (error, socket) => {
    report(error);
    socket.destroy();
  });
  return server;
}

if (require.main === module) {
  const adapter = Object.entries(os.networkInterfaces())
    .filter(([name]) => name.includes('WSL'))
    .flatMap(([, addresses]) => addresses)
    .find(address => address.family === 'IPv4' && !address.internal &&
      address.address === process.argv[2]);
  if (!adapter || adapter.netmask === '0.0.0.0') {
    report(new Error('WSL NAT adapter not found'));
    process.exit(1);
  }
  const server = createBridge(adapter);
  server.on('error', error => { report(error); process.exitCode = 1; });
  server.listen(18080, adapter.address);
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => { server.close(); server.closeAllConnections(); process.exit(0); });
  }
}

module.exports = { ipv4Number, destination, createBridge };
