// Coverage-only preload for trusted repository JS tests; not a hostile-code sandbox.
const fs = require('node:fs');
const net = require('node:net');
const dns = require('node:dns');
const child = require('node:child_process');
const events = process.env.HARNESS_COVERAGE_NODE_EVENTS;
function refuse(kind) {
  fs.appendFileSync(events, JSON.stringify({kind: 'refused', event: kind, pid: process.pid}) + '\n');
  throw new Error('provider-free coverage refused ' + kind);
}
function loopback(host) {
  return host === undefined || host === 'localhost' || host === '::1'
    || (net.isIP(host) === 4 && host.startsWith('127.'));
}
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const opts = typeof args[0] === 'object' ? args[0] : {host: args[1]};
  if (opts.path || !loopback(opts.host)) refuse('node socket');
  return connect.apply(this, args);
};
for (const name of ['resolve', 'resolve4', 'resolve6', 'reverse']) {
  dns[name] = () => refuse('node dns');
  dns.promises[name] = () => refuse('node dns');
}
for (const target of [dns, dns.promises]) {
  const lookup = target.lookup;
  target.lookup = function (host, ...args) {
    if (!loopback(host)) refuse('node lookup');
    return lookup.call(this, host, ...args);
  };
}
for (const name of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) {
  child[name] = () => refuse('node child');
}
require('node:worker_threads').Worker = class { constructor() { refuse('node worker'); } };
process.dlopen = () => refuse('node native addon');
for (const name of ['connect', 'createConnection']) {
  require('node:tls')[name] = () => refuse('node tls');
}
for (const mod of ['node:http', 'node:https', 'node:http2']) {
  const api = require(mod);
  for (const name of ['request', 'get', 'connect']) {
    if (api[name]) api[name] = () => refuse('node http');
  }
}
globalThis.fetch = () => refuse('node fetch');
require('node:module').syncBuiltinESMExports();
