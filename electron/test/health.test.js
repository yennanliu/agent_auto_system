'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { checkOnce, waitForHealthy } = require('../health');

/** Spin up a throwaway HTTP server that replies with the given status + body. */
function withServer(handler, run) {
  return new Promise((resolve, reject) => {
    const srv = http.createServer(handler);
    srv.listen(0, '127.0.0.1', async () => {
      const { port } = srv.address();
      try {
        const result = await run(port);
        srv.close(() => resolve(result));
      } catch (e) {
        srv.close(() => reject(e));
      }
    });
  });
}

test('checkOnce → true when /health returns 200 with db:true', async () => {
  await withServer(
    (req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', db: true }));
    },
    async (port) => assert.equal(await checkOnce(port), true)
  );
});

test('checkOnce → false when db:false (DB not ready)', async () => {
  await withServer(
    (req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'degraded', db: false }));
    },
    async (port) => assert.equal(await checkOnce(port), false)
  );
});

test('checkOnce → true on 200 with an unparseable body (server up)', async () => {
  await withServer(
    (req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('OK'); // not JSON
    },
    async (port) => assert.equal(await checkOnce(port), true)
  );
});

test('checkOnce → false on a non-200 status', async () => {
  await withServer(
    (req, res) => {
      res.writeHead(503);
      res.end('starting');
    },
    async (port) => assert.equal(await checkOnce(port), false)
  );
});

test('checkOnce → false when nothing is listening on the port', async () => {
  // Port 1 on loopback: connection refused → must resolve false, not throw.
  assert.equal(await checkOnce(1), false);
});

test('waitForHealthy resolves true once the server becomes healthy', async () => {
  let ready = false;
  await withServer(
    (req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', db: ready }));
    },
    async (port) => {
      setTimeout(() => (ready = true), 250); // flip to healthy mid-poll
      const ok = await waitForHealthy(port, { timeoutMs: 4000, intervalMs: 100 });
      assert.equal(ok, true);
    }
  );
});

test('waitForHealthy gives up (false) after the timeout', async () => {
  await withServer(
    (req, res) => {
      res.writeHead(503);
      res.end('never ready');
    },
    async (port) => {
      const ok = await waitForHealthy(port, { timeoutMs: 600, intervalMs: 100 });
      assert.equal(ok, false);
    }
  );
});
