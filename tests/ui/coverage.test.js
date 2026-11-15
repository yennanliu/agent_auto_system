// Guard: every ui/js module must be imported by at least one UI test file, so a new
// module can't land untested. (Static check — doesn't load the DOM.)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const JS_DIR = fileURLToPath(new URL('../../ui/js/', import.meta.url));

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : e.name.endsWith('.js') ? [join(dir, e.name)] : []);
}

test('every ui/js module is imported by a UI test', () => {
  const modules = walk(JS_DIR).map((f) => relative(JS_DIR, f).split('\\').join('/')).sort();
  const imported = new Set();
  for (const f of readdirSync(HERE).filter((n) => n.endsWith('.test.js'))) {
    const src = readFileSync(join(HERE, f), 'utf8');
    for (const m of src.matchAll(/importUi\(\s*['"`]([^'"`]+)['"`]\s*\)/g)) imported.add(m[1]);
  }
  const untested = modules.filter((m) => !imported.has(m));
  assert.deepEqual(untested, [], `add a tests/ui/*.test.js that calls importUi('<module>') for: ${untested.join(', ')}`);
});
