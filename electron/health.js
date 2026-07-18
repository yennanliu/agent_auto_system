'use strict';

const http = require('node:http');

/** GET http://127.0.0.1:<port>/health once; resolve true iff 200 + db_ok. */
function checkOnce(port) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: '/health', timeout: 2000 },
      (res) => {
        let body = '';
        res.on('data', (c) => (body += c));
        res.on('end', () => {
          if (res.statusCode !== 200) return resolve(false);
          try {
            const json = JSON.parse(body);
            // /health returns {"status":"ok","db":bool,...}; DB up == ready.
            resolve(json.db !== false);
          } catch (_e) {
            resolve(res.statusCode === 200);
          }
        });
      }
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

/**
 * Poll /health until ready or timeout.
 * @returns {Promise<boolean>} true once the backend is healthy.
 */
async function waitForHealthy(port, { timeoutMs = 60000, intervalMs = 400 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await checkOnce(port)) return true;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return false;
}

module.exports = { waitForHealthy, checkOnce };
