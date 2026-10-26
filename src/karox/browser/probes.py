"""JavaScript probes and the one definition of a secret field.

Both the page-side probes and the Python action guard must agree about which
fields hold secrets: a field Python treats as secret but the probe returns
plaintext length for (or vice versa) is a leak or a false refusal. The keyword
list lives here once and is injected into every probe.
"""

from __future__ import annotations

import re

#: Names that mark an input as holding a secret. ``passwd`` and ``iban`` were
#: only in the Python guard before, so the page probes returned their lengths.
SECRET_FIELD_KEYWORDS = (
    r"password|passwd|secret|token|api[_-]?key|credential|cookie|card|cvv|cvc|iban"
)

SECRET_FIELD_HINT = re.compile(rf"(?i)({SECRET_FIELD_KEYWORDS})")

_JS_SECRET_HINT = "/" + SECRET_FIELD_KEYWORDS + "/i"

#: Metadata read from one locator before acting on it. A field already marked
#: ``data-karox-secret`` never returns its text.
LOCATOR_METADATA_JS = """el => {
                    const scope = el.closest('form,section,article,[role=dialog]');
                    const karoxSecret = el.getAttribute('data-karox-secret') === 'true' || el.dataset.karoxSecret === 'true';
                    return {
                        type: (el.getAttribute('type') || el.tagName || '').toLowerCase(),
                        name: el.getAttribute('name') || '',
                        id: el.id || '',
                        aria: el.getAttribute('aria-label') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        secret: karoxSecret ? 'true' : '',
                        text: karoxSecret ? '' : (el.innerText || el.value || '').trim().slice(0, 500),
                        context: (scope && scope.innerText || '').trim().slice(0, 2000)
                    };
                }"""

#: Metadata for the element that currently owns focus. A single-character
#: ``press`` inserts text into that element, so the page-level path must see
#: the same secret classification the locator path sees; otherwise the
#: password guard is bypassed one character at a time.
ACTIVE_FIELD_METADATA_JS = """() => {
                    const el = document.activeElement;
                    if (!el) return {};
                    const tag = (el.tagName || '').toLowerCase();
                    const karoxSecret = el.getAttribute('data-karox-secret') === 'true' || el.dataset.karoxSecret === 'true';
                    return {
                        type: (el.getAttribute('type') || tag).toLowerCase(),
                        name: el.getAttribute('name') || '',
                        id: el.id || '',
                        aria: el.getAttribute('aria-label') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        secret: karoxSecret ? 'true' : '',
                        editable: el.isContentEditable ? 'true' : ''
                    };
                }"""

#: The page text search. Returns matching refs and visibility, never values.
FIND_TEXT_PROBE = """
        ([needle, maxResults]) => {
          const matches = [];
          if (!document.body) return matches;
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          const lowered = needle.toLowerCase();
          let node;
          while ((node = walker.nextNode()) && matches.length < maxResults) {
            const value = (node.nodeValue || '').trim();
            if (!value || !value.toLowerCase().includes(lowered)) continue;
            const el = node.parentElement;
            if (!el) continue;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            let ref = el.getAttribute('data-karox-ref');
            if (!ref) {
              ref = 'k' + (document.querySelectorAll('[data-karox-ref]').length + 1);
              el.setAttribute('data-karox-ref', ref);
            }
            matches.push({
              ref,
              tag: el.tagName.toLowerCase(),
              text: value.slice(0, 300),
              visible: style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0,
              x: Math.round(rect.x), y: Math.round(rect.y)
            });
          }
          return matches;
        }
        """

#: Form state, with every secret field reduced to a protected row.
FORM_STATE_PROBE = """
        scope => {
          const SECRET_HINT = %s;
          const root = document.querySelector(scope) || document.body;
          const forms = [];
          const collect = (form) => {
            const fields = [];
            for (const el of Array.from(form.querySelectorAll('input,select,textarea')).slice(0, 200)) {
              const type = (el.getAttribute('type') || el.tagName).toLowerCase();
              const label = (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.placeholder || '').trim().slice(0, 200);
              const secret = el.getAttribute('data-karox-secret') === 'true' || type === 'password' || SECRET_HINT.test([el.name, el.id, label].join(' '));
              const ref = el.getAttribute('data-karox-ref') || '';
              const row = {ref, name: (el.name || '').slice(0, 100), id: (el.id || '').slice(0, 100), type, label,
                           required: !!el.required, disabled: !!el.disabled, protected: secret,
                           checked: (type === 'checkbox' || type === 'radio') ? !!el.checked : null,
                           value_length: (el.value || '').length};
              if (!secret) row.value = (el.value || '').slice(0, 200);
              if (el.tagName.toLowerCase() === 'select') {
                const opt = el.options[el.selectedIndex];
                row.selected_option = opt ? (opt.value || opt.text || '').slice(0, 200) : null;
              }
              fields.push(row);
            }
            return {action: (form.getAttribute && form.getAttribute('action') || '').slice(0, 300),
                    method: (form.getAttribute && form.getAttribute('method') || 'get').slice(0, 10),
                    fields};
          };
          if (root.tagName && root.tagName.toLowerCase() === 'form') forms.push(collect(root));
          else for (const form of Array.from(root.querySelectorAll('form')).slice(0, 20)) forms.push(collect(form));
          if (!forms.length) forms.push(collect(root));
          return forms;
        }
        """ % (_JS_SECRET_HINT)


def snapshot_probe(max_nodes: int) -> str:
    """The ref-stamped page snapshot, bounded to ``max_nodes`` elements."""

    return """
        () => {
          const SECRET_HINT = %s;
          const out = {headings: [], buttons: [], inputs: [], links: [], dialogs: [], tabs: [], text: ''};
          const text = [];
          let ref = 0;
          const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,button,input,select,textarea,a,[role=dialog],[role=tab]')).slice(0, %d);
          for (const el of nodes) {
            const tag = el.tagName.toLowerCase();
            const label = (el.getAttribute('aria-label') || el.innerText || el.placeholder || '').trim().slice(0, 200);
            ref += 1;
            const refId = 'k' + ref;
            el.setAttribute('data-karox-ref', refId);
            if (/^h[1-6]$/.test(tag)) out.headings.push({level: tag, text: label});
            else if (tag === 'button' || el.getAttribute('role') === 'button') out.buttons.push({ref: refId, name: label, disabled: !!el.disabled});
            else if (['input','select','textarea'].includes(tag)) {
              const type = (el.getAttribute('type') || tag).toLowerCase();
              let lab = '';
              try {
                if (el.labels && el.labels.length) lab = (el.labels[0].innerText || '').trim();
                if (!lab && el.id) {
                  const bound = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                  if (bound) lab = (bound.innerText || '').trim();
                }
              } catch (e) {}
              const fieldLabel = (el.getAttribute('aria-label') || lab || el.placeholder || '').trim().slice(0, 200);
              const secret = el.getAttribute('data-karox-secret') === 'true' || el.dataset.karoxSecret === 'true' || type === 'password' || SECRET_HINT.test([el.name, el.id, fieldLabel].join(' '));
              out.inputs.push({ref: refId, label: fieldLabel, type, name: (el.getAttribute('name') || '').slice(0, 100), disabled: !!el.disabled, protected: secret, value_length: secret ? null : (el.value || '').length});
              if (fieldLabel) text.push(fieldLabel);
            }
            else if (tag === 'a') out.links.push({ref: refId, text: label, href: (el.getAttribute('href') || '').slice(0, 300)});
            else if (el.getAttribute('role') === 'dialog') out.dialogs.push({ref: refId, name: label});
            else if (el.getAttribute('role') === 'tab') out.tabs.push({ref: refId, text: label, selected: el.getAttribute('aria-selected') === 'true'});
            if (label) text.push(label);
          }
          out.text = text.join(' ').slice(0, 20000);
          out.scroll = {width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight,
                        viewport_width: window.innerWidth, viewport_height: window.innerHeight,
                        x: window.scrollX, y: window.scrollY,
                        horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 1};
          return out;
        }
        """ % (_JS_SECRET_HINT, max_nodes)
