"""Exercise the dashboard locale scripts with a small DOM and browser storage."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_dashboard_locale_is_restored_after_reload():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    web = Path(__file__).resolve().parents[1] / "sellbird" / "web"
    subprocess.run(
        [node, "-e", r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const web = process.argv[1];
const source = ['locale-es.js', 'locale.js'].map(file =>
  fs.readFileSync(web + '/' + file, 'utf8')).join('\n');
const storage = new Map();
let reloads = 0;

function load() {
  const heading = {data: '  Companies\n', parentElement: {closest: () => null}};
  const code = {data: 'Companies', parentElement: {closest: () => true}};
  const attributes = new Map([['aria-label', 'Sections'], ['placeholder', 'your@email.com']]);
  const document = {
    documentElement: {},
    body: {querySelectorAll: () => [{
      getAttribute: key => attributes.get(key),
      setAttribute: (key, value) => attributes.set(key, value),
    }]},
    createTreeWalker(root, what, filter) {
      const nodes = [heading, code].filter(node => filter.acceptNode(node) === 1);
      return {nextNode: () => nodes.shift()};
    },
    getElementById: () => picker,
  };
  const picker = {addEventListener: (_, handler) => { picker.change = handler; }};
  const context = {
    document,
    NodeFilter: {SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2},
    window: {location: {reload: () => { reloads++; }}},
    localStorage: {
      getItem: key => storage.get(key),
      setItem: (key, value) => storage.set(key, value),
    },
  };
  const locale = vm.runInNewContext(source + '\ndashboardLocale', context);
  return {heading, code, attributes, document, picker, context, ...locale};
}

let page = load();
assert.equal(page.document.documentElement.lang, 'en');
assert.equal(page.heading.data, '  Companies\n');
page.picker.value = 'es';
page.picker.change();
assert.equal(reloads, 1);
page = load();
assert.equal(page.picker.value, 'es');
assert.equal(page.document.documentElement.lang, 'es');
assert.equal(page.heading.data, '  Empresas\n');
assert.equal(page.code.data, 'Companies');
assert.equal(page.attributes.get('aria-label'), 'Secciones');
assert.equal(page.attributes.get('placeholder'), 'usuario@example.com');
assert.equal(page.format('Type {0} to confirm', 'mailbox@example.com'), 'Escribe mailbox@example.com para confirmar');
assert.equal(page.format('Default ({0})', 0), 'Valor predeterminado (0)');
assert.equal(page.format('{0} of {1} done', 2, 6), '2 de 6 completados');
assert.equal(page.format('Sends {0}', '17 nov'), 'Envío programado: 17 nov');
assert.equal(page.format('{0} prospect', 1), '1 prospecto');
assert.equal(page.format('{0} prospects', 2), '2 prospectos');
assert.equal(page.format('Add the redirect URI {0} as "Public client/native".', 'http://localhost:8766'),
  'Agrega la URI de redirección http://localhost:8766 como cliente público/nativo.');
assert.equal(page.format('Connected {0}', 'mailbox@example.com'), 'Buzón conectado: mailbox@example.com');
assert.match(page.message('Run: sellbird train https://yourwebsite.com (or set up through Claude).'),
  /sellbird train URL_DE_TU_SITIO/);
assert.equal(page.text('Unknown string'), 'Unknown string');
assert.equal(page.message('8 of 12 checked · 3 emails found'), '8 de 12 comprobados · 3 direcciones encontradas');
assert.equal(page.message('Sellbird configured (sellbird.local.yaml)'), 'Sellbird está configurado (sellbird.local.yaml)');
assert.equal(page.message('Unrecognized message 37'), 'Unrecognized message 37');
storage.set('sellbird-dashboard-locale', 'es-419');
assert.equal(load().locale, 'es');
assert.equal(storage.get('sellbird-dashboard-locale'), 'es');

(async () => {
  const elements = {'live-status': {}, 'today-activity': {}, 'signals-summary': {}, 'signals-groups': {}};
  page.document.getElementById = id => elements[id];
  Object.assign(page.context, {
    api: async path => ({
      '/api/live-status': {steps: [], cost_usd: 0},
      '/api/activity': [],
      '/api/signals': {
        summary: {confirmed: 0, proposed: 29, rejected: 0},
        groups: [{label: 'Discovery — who exists', blurb: '',
          signals: Array.from({length: 8}, (_, i) => ({code: 'SIGNAL_' + i, status: 'proposed'}))}],
      },
    })[path],
    icon: () => '',
    navCount: () => {},
    sigRow: () => '',
    renderCohortBuilder: () => '',
    escHtml: value => String(value).replaceAll('"', '&quot;'),
  });
  const app = fs.readFileSync(web + '/app.js', 'utf8');
  const functions = ['loadLiveStatus', 'loadTodayActivity', 'loadSignals'].map(name => {
    const start = app.indexOf('async function ' + name + '(');
    return app.slice(start, app.indexOf('\n}', start) + 2);
  }).join('\n');
  await vm.runInNewContext(
    'const {text: uiText, message: uiMessage, format: uiFormat} = dashboardLocale; let _signals; const uiDateLocale = "es";\n' +
    functions + '\nPromise.all([loadLiveStatus(), loadTodayActivity(), loadSignals()])',
    page.context,
  );
  assert.match(elements['live-status'].innerHTML, />Costo acumulado \$0\.00</);
  assert.match(elements['today-activity'].innerHTML,
    />Aún no hay actividad\. Las acciones de Sellbird aparecerán aquí\.</);
  assert.match(elements['signals-summary'].innerHTML, />Confirmadas<.*?>0 <small>\(0%\)/);
  assert.match(elements['signals-summary'].innerHTML, />Pendientes de confirmación<.*?>29 <small>\(100%\)/);
  assert.match(elements['signals-summary'].innerHTML, />Desactivadas<.*?>0 <small>\(0%\)/);
  assert.match(elements['signals-groups'].innerHTML, />Confirmar \(8\)<\/button>/);
  assert.match(elements['signals-groups'].innerHTML, />Omitir \(8\)<\/button>/);
  page.picker.value = 'en';
  page.picker.change();
  assert.equal(load().text('Companies'), 'Companies');
})().catch(error => { console.error(error); process.exitCode = 1; });
""", str(web)],
        check=True,
        timeout=30,
    )
