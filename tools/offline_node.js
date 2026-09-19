// Trusted-test guard, loaded before every JS test. Node 24's permission model
// restricts files/subprocesses but does not itself prohibit network access.
'use strict';
const deny = () => { throw new Error('OFFLINE_GUARD: network disabled'); };
const net = require('node:net');
net.connect = deny;
net.createConnection = deny;
net.Socket.prototype.connect = deny;
require('node:tls').connect = deny;
require('node:dgram').createSocket = deny;
for (const name of ['node:http', 'node:https', 'node:http2']) {
    const module = require(name);
    for (const key of ['request', 'get', 'connect']) {
        if (typeof module[key] === 'function') module[key] = deny;
    }
}
const dns = require('node:dns');
for (const key of Object.keys(dns)) {
    if (key === 'lookup' || key === 'lookupService' || key.startsWith('resolve')) {
        dns[key] = deny;
        if (dns.promises[key]) dns.promises[key] = deny;
    }
}
globalThis.fetch = deny;
globalThis.WebSocket = deny;
require('node:module').syncBuiltinESMExports();
