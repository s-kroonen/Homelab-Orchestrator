// Render the wireframe sources (.dc.html, the Design canvas format) to static HTML + PNG, so they
// can be viewed without the canvas editor. Needs Playwright with Chromium.
//
//   node docs/design/render_wireframes.js docs/design/wireframes/source docs/design/wireframes
//
// Every artboard listed in source/canvas.json is rendered with its default props.
const { chromium } = require('playwright');
const fs = require('fs'); const path = require('path');
const [src, out] = process.argv.slice(2);
const canvas = JSON.parse(fs.readFileSync(path.join(src, 'canvas.json'), 'utf8'));

function renderInPage(text) {
  // sc-for / sc-if become <template> so the HTML parser keeps them inside tables
  const prepped = text
    .replace(/<sc-for\b/g, '<template data-k="for"').replace(/<\/sc-for>/g, '</template>')
    .replace(/<sc-if\b/g, '<template data-k="if"').replace(/<\/sc-if>/g, '</template>');
  const doc = new DOMParser().parseFromString(prepped, 'text/html');
  const script = doc.querySelector('script[data-dc-script]');
  const propsDecl = JSON.parse(script.getAttribute('data-props') || '{}');
  const props = {};
  for (const [k, v] of Object.entries(propsDecl)) if (k[0] !== '$' && v && 'default' in v) props[k] = v.default;
  const DCLogic = class { constructor(p) { this.props = p; this.state = {}; } setState(s) { Object.assign(this.state, s); } forceUpdate() {} };
  const Component = new Function('DCLogic', script.textContent + '\n;return Component;')(DCLogic);
  const vals = new Component(props).renderVals();
  const lookup = (expr, scope) => {
    expr = expr.trim();
    if (expr === 'true') return true; if (expr === 'false') return false;
    if (/^-?\d+(\.\d+)?$/.test(expr)) return Number(expr);
    return expr.split('.').reduce((o, k) => (o == null ? undefined : o[k]), scope);
  };
  const sub = (s, scope) => s.replace(/\{\{\s*([\w.$]+)\s*\}\}/g, (_, e) => { const v = lookup(e, scope); return v == null ? '' : String(v); });
  const whole = /^\s*\{\{\s*([\w.$]+)\s*\}\}\s*$/;
  function render(nodes, scope, into) {
    for (const n of nodes) {
      if (n.nodeType === 3) { into.appendChild(document.createTextNode(sub(n.textContent, scope))); continue; }
      if (n.nodeType !== 1) continue;
      if (n.tagName === 'TEMPLATE' && n.dataset.k === 'for') {
        const list = lookup(n.getAttribute('list').replace(/[{}]/g, ''), scope) || [];
        const as = n.getAttribute('as');
        list.forEach((item, i) => render(n.content.childNodes, { ...scope, [as]: item, $index: i }, into));
        continue;
      }
      if (n.tagName === 'TEMPLATE' && n.dataset.k === 'if') {
        if (lookup(n.getAttribute('value').replace(/[{}]/g, ''), scope)) render(n.content.childNodes, scope, into);
        continue;
      }
      const el = document.createElement(n.tagName.toLowerCase());
      for (const a of n.attributes) {
        if (/^on/i.test(a.name) || a.name.startsWith('hint-')) continue;
        const m = a.value.match(whole);
        if (m) {
          const v = lookup(m[1], scope);
          if (v === false || v == null || typeof v === 'function') continue;
          el.setAttribute(a.name, v === true ? '' : String(v));
        } else el.setAttribute(a.name, sub(a.value, scope));
      }
      render(n.tagName === 'TEMPLATE' ? n.content.childNodes : n.childNodes, scope, el);
      into.appendChild(el);
    }
  }
  const root = doc.querySelector('x-dc');
  const helmet = root.querySelector('helmet');
  const body = document.createElement('div');
  render([...root.childNodes].filter((c) => c !== helmet), vals, body);
  const title = (doc.querySelector('title') || {}).textContent || 'Wireframe';
  return '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>' + title +
    ' (wireframe)</title>\n' + (helmet ? helmet.innerHTML.trim() : '') + '\n</head>\n<body>\n' + body.innerHTML + '\n</body>\n</html>\n';
}

(async () => {
  const b = await chromium.launch();
  const page = await b.newPage();
  await page.setContent('<!doctype html><html><body></body></html>');
  for (const file of canvas.order) {
    const name = file.replace(/\.dc\.html$/, '');
    const html = await page.evaluate(renderInPage, fs.readFileSync(path.join(src, file), 'utf8'));
    const htmlPath = path.join(out, name + '.html');
    fs.writeFileSync(htmlPath, html);
    const board = canvas.boards[file];
    const shot = await b.newPage({ viewport: { width: board.w, height: Math.min(board.h, 1200) } });
    const errs = []; shot.on('pageerror', (e) => errs.push(String(e)));
    await shot.goto('file://' + path.resolve(htmlPath)); await shot.waitForTimeout(700);
    await shot.screenshot({ path: path.join(out, name + '.png'), fullPage: true });
    const h = await shot.evaluate(() => document.documentElement.scrollHeight);
    console.log(name.padEnd(18), 'h=' + h, errs.length ? 'ERR ' + errs.join(' | ') : 'ok');
    await shot.close();
  }
  await b.close();
})();
