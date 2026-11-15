// highlight.js — highlightCode(raw, filePath): Python / YAML / plain, always HTML-escaped.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi } from './harness.js';

setupDom();
const { highlightCode } = await importUi('highlight.js');

/** Parse highlighter output into a detached element. */
function render(html) {
  const host = document.createElement('pre');
  host.innerHTML = html;
  return host;
}
/** [className, text] for every span, in order. */
function spans(html) {
  return [...render(html).querySelectorAll('span')].map((s) => [s.className, s.textContent]);
}
const has = (html, cls, text) => spans(html).some(([c, t]) => c === cls && t === text);

const XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>';

describe('dispatch by extension', () => {
  test('unknown extension / no path → plain escaped text, no spans', () => {
    for (const path of ['notes.txt', 'Makefile', '', undefined, null]) {
      const out = highlightCode('a < b && "c"', path);
      assert.equal(out, 'a &lt; b &amp;&amp; &quot;c&quot;', String(path));
    }
  });

  test('extension match is case-insensitive and .yml == .yaml', () => {
    assert.ok(has(highlightCode('def f(): pass', 'src/X.PY'), 'hl-ki', 'def'));
    assert.ok(has(highlightCode('key: 1', 'a.yml'), 'hl-yk', 'key'));
    assert.ok(has(highlightCode('key: 1', 'a.YAML'), 'hl-yk', 'key'));
  });

  test('empty source → empty string', () => {
    assert.equal(highlightCode('', 'a.py'), '');
    assert.equal(highlightCode('', 'a.yaml'), '');
    assert.equal(highlightCode('', 'a.txt'), '');
  });
});

describe('Python', () => {
  const py = (src) => highlightCode(src, 'flow.py');

  test('keywords, italic keywords, builtins', () => {
    const out = py('async def run(self):\n    return len(x) if True else None');
    assert.ok(has(out, 'hl-ki', 'async'));
    assert.ok(has(out, 'hl-ki', 'def'));
    assert.ok(has(out, 'hl-k', 'return'));
    assert.ok(has(out, 'hl-k', 'if'));
    assert.ok(has(out, 'hl-k', 'True'));
    assert.ok(has(out, 'hl-k', 'None'));
    assert.ok(has(out, 'hl-bi', 'self'));
    assert.ok(has(out, 'hl-bi', 'len'));
  });

  test('def / class names and method access are hl-fn; plain identifiers are bare', () => {
    const out = py('class MyCrew:\n    def crew(self):\n        return obj.kickoff(value)');
    assert.ok(has(out, 'hl-fn', 'MyCrew'));
    assert.ok(has(out, 'hl-fn', 'crew'));
    assert.ok(has(out, 'hl-fn', 'kickoff'));
    assert.ok(!spans(out).some(([, t]) => t === 'obj' || t === 'value'));
  });

  test('strings: single, double, escaped quote, unterminated stops at newline', () => {
    const out = py(`a = 'x'\nb = "say \\"hi\\""\nc = 'open\nd = 1`);
    assert.ok(has(out, 'hl-s', "'x'"));
    assert.ok(has(out, 'hl-s', '"say \\"hi\\""'));
    assert.ok(has(out, 'hl-s', "'open"));
    assert.ok(has(out, 'hl-n', '1'), 'code after an unterminated string is still highlighted');
  });

  test('triple-quoted strings span lines; unterminated runs to EOF', () => {
    const out = py('x = """doc\nline2"""\ny = 2');
    assert.ok(has(out, 'hl-s', '"""doc\nline2"""'));
    assert.ok(has(out, 'hl-n', '2'));
    const open = py("z = '''never closed\nreturn 1");
    assert.ok(has(open, 'hl-s', "'''never closed\nreturn 1"));
    assert.ok(!has(open, 'hl-k', 'return'));
  });

  test('keywords inside strings and comments are not highlighted as keywords', () => {
    const out = py('s = "def return"  # import class');
    assert.ok(has(out, 'hl-c', '# import class'));
    assert.ok(!spans(out).some(([c]) => c === 'hl-k' || c === 'hl-ki'));
  });

  test('decorators, numbers (int/float/exp), digits inside identifiers', () => {
    const out = py('@crewai.flow.start\ndef f(): return 3 + 4.5 + 1e-3 + x1');
    assert.ok(has(out, 'hl-d', '@crewai.flow.start'));
    assert.ok(has(out, 'hl-n', '3'));
    assert.ok(has(out, 'hl-n', '4.5'));
    assert.ok(has(out, 'hl-n', '1e-3'));
    assert.ok(!has(out, 'hl-n', '1'), 'the 1 in x1 is part of the identifier');
  });

  test('a bare @ (matrix-mult) is just escaped text', () => {
    const out = py('a @ b');
    assert.equal(spans(out).length, 0);
    assert.equal(render(out).textContent, 'a @ b');
  });

  test('HTML in strings, comments and code is escaped — no elements injected', () => {
    const src = `x = "${XSS}"\n# ${XSS}\ny = 1 < 2 > 0 & 3\n${XSS}`;
    const out = py(src);
    const el = render(out);
    assert.equal(el.querySelectorAll('script,img').length, 0);
    assert.ok(!/<script|<img/i.test(out));
    assert.equal(el.textContent, src, 'text round-trips exactly');
  });
});

describe('YAML', () => {
  const yml = (src) => highlightCode(src, 'agents.yaml');

  test('full-line comment', () => {
    assert.deepEqual(spans(yml('  # a comment')), [['hl-c', '  # a comment']]);
  });

  test('key with each value kind', () => {
    const cases = [
      ['name: "quoted"', 'hl-yv', '"quoted"'],
      ["name: 'single'", 'hl-yv', "'single'"],
      ['verbose: true', 'hl-bi', 'true'],
      ['flag: No', 'hl-bi', 'No'],
      ['empty: ~', 'hl-bi', '~'],
      ['n: -12.5', 'hl-n', '-12.5'],
      ['goal: >', 'hl-k', '>'],
      ['text: |', 'hl-k', '|'],
      ['role: Researcher', 'hl-yv', 'Researcher'],
      ['k: # trailing', 'hl-c', '# trailing'],
    ];
    for (const [src, cls, val] of cases) {
      const out = yml(src);
      assert.ok(has(out, 'hl-yk', src.split(':')[0]), src);
      assert.ok(has(out, cls, val), `${src} → ${cls}`);
    }
  });

  test('key with no value has no value span', () => {
    const s = spans(yml('researcher:'));
    assert.deepEqual(s.map(([c]) => c), ['hl-yk', '']);  // key + separator (inline-styled)
  });

  test('indentation is preserved before the key', () => {
    const out = yml('    role: x');
    assert.ok(out.startsWith('    <span class="hl-yk">role</span>'));
  });

  test('list items and continuation lines', () => {
    assert.deepEqual(spans(yml('  - item one')), [['hl-k', '  - '], ['hl-yv', 'item one']]);
    assert.deepEqual(spans(yml('    folded continuation text')), [['hl-yv', '    folded continuation text']]);
  });

  test('unindented non-key text is escaped plain text', () => {
    assert.equal(yml('just <text>'), 'just &lt;text&gt;');
  });

  test('lines are joined with newlines and text round-trips', () => {
    const src = 'a: 1\n\n# c\nb:\n  - x\n  more';
    const out = yml(src);
    assert.equal(out.split('\n').length, 6);
    assert.equal(render(out).textContent, src);
  });

  test('HTML in keys-values, comments, list items is escaped', () => {
    const src = `goal: ${XSS}\n# ${XSS}\n- ${XSS}\n  ${XSS}\n"${XSS}"`;
    const out = yml(src);
    const el = render(out);
    assert.equal(el.querySelectorAll('script,img').length, 0);
    assert.equal(el.textContent, src);
  });
});
