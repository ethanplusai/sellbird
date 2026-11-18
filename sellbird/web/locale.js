// UI preferences belong to the browser and survive updates to the Python
// package. Use the same dashboard host and port to keep the preference.
const dashboardLocale = (() => {
  const key = 'sellbird-dashboard-locale';
  const supported = ['en', 'es'];
  let saved;
  try { saved = localStorage.getItem(key); } catch { /* storage unavailable */ }
  // Carry over the locale used by the first version of the Spanish catalog.
  if (saved === 'es-419') {
    saved = 'es';
    try { localStorage.setItem(key, saved); } catch { /* storage unavailable */ }
  }
  const locale = supported.includes(saved) ? saved : 'en';

  function text(english) {
    return locale === 'es' && Object.hasOwn(dashboardSpanish, english)
      ? dashboardSpanish[english]
      : english;
  }

  // Some endpoints return display text that contains counts. Match only catalog
  // entries, retaining the original server message when there is no translation.
  function message(english) {
    if (locale === 'en' || typeof english !== 'string') return english;
    const exact = text(english);
    if (exact !== english) return exact;
    // Progress details can contain independently translated clauses.
    if (english.includes(' · ')) return english.split(' · ').map(message).join(' · ');
    const context = english.match(/^(Email provider configured|Sellbird configured) \(([^)]+)\)$/);
    if (context) return text(context[1] + ' ({0})').replace('{0}', () => context[2]);
    const values = [];
    const key = english.replace(/\d+(?:\.\d+)?/g, value => {
      values.push(value);
      return '{' + (values.length - 1) + '}';
    });
    const translated = text(key);
    return translated === key ? english : translated.replace(/\{(\d+)\}/g,
      (placeholder, index) => values[index] ?? placeholder);
  }

  function format(english, ...values) {
    return text(english).replace(/\{(\d+)\}/g, (placeholder, index) =>
      values[index] ?? placeholder);
  }

  function translatePage(root) {
    if (locale === 'en') return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        return node.parentElement?.closest('script, style, code, pre, svg, [data-locale-picker]')
          ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const phrase = node.data.trim().replace(/\s+/g, ' ');
      const translated = text(phrase);
      if (translated !== phrase) {
        node.data = node.data.replace(/\S[\s\S]*\S|\S/, () => translated);
      }
    }
    for (const element of root.querySelectorAll('[title], [aria-label], [placeholder]')) {
      for (const attribute of ['title', 'aria-label', 'placeholder']) {
        const value = element.getAttribute(attribute);
        if (value) element.setAttribute(attribute, text(value));
      }
    }
  }

  document.documentElement.lang = locale;
  translatePage(document.body);
  const picker = document.getElementById('dashboard-locale');
  picker.value = locale;
  picker.addEventListener('change', () => {
    if (!supported.includes(picker.value)) return;
    try {
      localStorage.setItem(key, picker.value);
    } catch {
      picker.value = locale;
      picker.setCustomValidity(text('Allow browser storage to change the language.'));
      picker.reportValidity();
      picker.setCustomValidity('');
      return;
    }
    window.location.reload();
  });

  return {locale, text, message, format};
})();
