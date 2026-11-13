// Dependency-free Python / YAML syntax highlighting for the System page source viewer.

import { escHtml } from './helpers.js';

// ── Syntax highlighting ────────────────────────────────────────────────────────

const _PY_KW_ITALIC = new Set(['def','class','async','await','lambda']);
const _PY_KW = new Set([
  'False','None','True','and','as','assert','break','continue','del','elif',
  'else','except','finally','for','from','global','if','import','in','is',
  'nonlocal','not','or','pass','raise','return','try','while','with','yield',
]);
const _PY_BI = new Set([
  'abs','all','any','bin','bool','bytes','callable','chr','dict','dir',
  'divmod','enumerate','eval','exec','filter','float','format','frozenset',
  'getattr','globals','hasattr','hash','help','hex','id','input','int',
  'isinstance','issubclass','iter','len','list','locals','map','max','min',
  'next','object','oct','open','ord','pow','print','property','range','repr',
  'reversed','round','set','setattr','slice','sorted','staticmethod','str',
  'sum','super','tuple','type','vars','zip','self','cls',
]);

export function highlightCode(raw, filePath) {
  const ext = (filePath || '').split('.').pop().toLowerCase();
  try {
    if (ext === 'py')              return _hlPython(raw);
    if (ext === 'yaml' || ext === 'yml') return _hlYaml(raw);
  } catch (_) {}
  return escHtml(raw);
}

function _hlPython(code) {
  const out = [];
  let i = 0;
  const n = code.length;

  while (i < n) {
    const ch = code[i];

    // Triple-quoted strings (handle """ and ''')
    if ((ch === '"' || ch === "'") && code[i+1] === ch && code[i+2] === ch) {
      const q = ch.repeat(3);
      const end = code.indexOf(q, i + 3);
      const len = end === -1 ? n - i : end - i + 3;
      out.push(`<span class="hl-s">${escHtml(code.slice(i, i + len))}</span>`);
      i += len;
      continue;
    }

    // Single-line strings
    if (ch === '"' || ch === "'") {
      let j = i + 1;
      while (j < n && code[j] !== ch && code[j] !== '\n') {
        if (code[j] === '\\') j++;
        j++;
      }
      if (j < n && code[j] === ch) j++;
      out.push(`<span class="hl-s">${escHtml(code.slice(i, j))}</span>`);
      i = j;
      continue;
    }

    // Comments
    if (ch === '#') {
      const nl = code.indexOf('\n', i);
      const end = nl === -1 ? n : nl;
      out.push(`<span class="hl-c">${escHtml(code.slice(i, end))}</span>`);
      i = end;
      continue;
    }

    // Decorator
    if (ch === '@') {
      const m = code.slice(i).match(/^@[\w.]+/);
      if (m) {
        out.push(`<span class="hl-d">${escHtml(m[0])}</span>`);
        i += m[0].length;
        continue;
      }
    }

    // Word token
    if (/[a-zA-Z_]/.test(ch)) {
      const m = code.slice(i).match(/^\w+/);
      if (m) {
        const w = m[0];
        let span = '';
        if (_PY_KW_ITALIC.has(w)) {
          span = `<span class="hl-ki">${escHtml(w)}</span>`;
        } else if (_PY_KW.has(w)) {
          span = `<span class="hl-k">${escHtml(w)}</span>`;
        } else if (_PY_BI.has(w)) {
          span = `<span class="hl-bi">${escHtml(w)}</span>`;
        } else {
          // Function/class name after def/class keyword
          const pre = code.slice(Math.max(0, i - 12), i);
          if (/\b(?:def|class)\s+$/.test(pre)) {
            span = `<span class="hl-fn">${escHtml(w)}</span>`;
          } else if (i > 0 && code[i - 1] === '.') {
            // Method access
            span = `<span class="hl-fn">${escHtml(w)}</span>`;
          } else {
            span = escHtml(w);
          }
        }
        out.push(span);
        i += w.length;
        continue;
      }
    }

    // Number
    if (/\d/.test(ch) && (i === 0 || !/\w/.test(code[i-1]))) {
      const m = code.slice(i).match(/^\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/);
      if (m) {
        out.push(`<span class="hl-n">${escHtml(m[0])}</span>`);
        i += m[0].length;
        continue;
      }
    }

    out.push(escHtml(ch));
    i++;
  }

  return out.join('');
}

function _hlYaml(code) {
  return code.split('\n').map(line => {
    // Full-line comment
    if (/^\s*#/.test(line)) {
      return `<span class="hl-c">${escHtml(line)}</span>`;
    }
    // key: value  (skip lines that start with - or are pure values)
    const km = line.match(/^(\s*)([\w][\w _-]*)(\s*:\s*)(.*)?$/);
    if (km) {
      const [, ws, key, sep, val = ''] = km;
      let valHtml = '';
      const vt = val.trim();
      if (!vt) {
        // No value — just a key block
        valHtml = '';
      } else if (vt.startsWith('#')) {
        valHtml = `<span class="hl-c">${escHtml(val)}</span>`;
      } else if (vt.startsWith('"') || vt.startsWith("'")) {
        valHtml = `<span class="hl-yv">${escHtml(val)}</span>`;
      } else if (vt === '>' || vt === '|') {
        valHtml = `<span class="hl-k">${escHtml(val)}</span>`;
      } else if (/^(true|false|yes|no|null|~)$/i.test(vt)) {
        valHtml = `<span class="hl-bi">${escHtml(val)}</span>`;
      } else if (/^-?\d/.test(vt)) {
        valHtml = `<span class="hl-n">${escHtml(val)}</span>`;
      } else {
        valHtml = `<span class="hl-yv">${escHtml(val)}</span>`;
      }
      return `${escHtml(ws)}<span class="hl-yk">${escHtml(key)}</span><span style="color:#5c6370">${escHtml(sep)}</span>${valHtml}`;
    }
    // List item marker
    if (/^\s*-\s/.test(line)) {
      const lm = line.match(/^(\s*-\s)(.*)/);
      if (lm) {
        return `<span class="hl-k">${escHtml(lm[1])}</span><span class="hl-yv">${escHtml(lm[2])}</span>`;
      }
    }
    // Continuation / folded block lines (indented plain text)
    if (/^\s+\S/.test(line)) {
      return `<span class="hl-yv">${escHtml(line)}</span>`;
    }
    return escHtml(line);
  }).join('\n');
}
