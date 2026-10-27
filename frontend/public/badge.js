/* Redline badge. See data-redline-trace / -mount / -summary. */
(function () {
  var s = document.currentScript, id = s && s.getAttribute('data-redline-trace');
  if (!id || window.top !== window.self) return;
  var mount = (s.getAttribute('data-redline-mount') || '').replace(/\/+$/, ''), sum = {}, K = 'redline-badge-collapsed', col = 0;
  try { sum = JSON.parse(s.getAttribute('data-redline-summary')) || {}; } catch (e) {}
  try { col = localStorage.getItem(K) === '1'; } catch (e) {}
  var kinds = sum.issue_kinds || [], n = sum.issue_count || 0;
  function el(t, c, x) { var e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; }
  function ms(v) { return v >= 1000 ? (v / 1000).toFixed(2) + ' s' : Math.round(v) + ' ms'; }
  var host = el('div'), root = host.attachShadow && host.attachShadow({ mode: 'closed' });
  if (!root) return;
  host.style.cssText = 'all:initial;position:fixed;right:14px;bottom:14px;z-index:2147483646';
  root.appendChild(el('style', 0,
    '.b{--bg:#fff;--fg:#18181b;--mu:#71717a;--ln:#e4e4e7;--ac:#e5392f;--ok:#16a34a;--wn:#ca6f04;' +
    'display:flex;align-items:center;gap:8px;max-width:calc(100vw - 28px);height:30px;padding:0 6px 0 10px;border-radius:15px;border:1px solid var(--ln);background:var(--bg);color:var(--fg);' +
    'font:12px ui-sans-serif,system-ui,sans-serif;box-shadow:0 2px 10px rgba(0,0,0,.18);cursor:pointer;user-select:none;touch-action:none;white-space:nowrap}' +
    '@media(prefers-color-scheme:dark){.b{--bg:#18181b;--fg:#f4f4f5;--mu:#a1a1aa;--ln:#2b2b32;--ac:#f85149;--ok:#4ade80;--wn:#fbbf24}}' +
    '.b:hover,.b:focus-visible{border-color:var(--ac);outline:0}.d{width:8px;height:8px;border-radius:50%;background:var(--ok);flex:none}.w{background:var(--wn)}.e{background:var(--ac)}' +
    '.t{overflow:hidden;text-overflow:ellipsis}.i{color:var(--wn);font-weight:600}' +
    '.x{all:unset;width:20px;height:20px;border-radius:50%;text-align:center;line-height:20px;color:var(--mu);cursor:pointer}' +
    '.c{padding:0 9px}.c .t,.c .x{display:none}' +
    '.p{position:absolute;right:0;bottom:36px;max-width:300px;padding:6px 10px;border-radius:8px;border:1px solid var(--ln);background:var(--bg);color:var(--fg);' +
    'font:12px/1.4 ui-sans-serif,system-ui,sans-serif;white-space:normal;display:none}.o{display:block}'));
  var w = el('div'), pill = el('div'), dot = el('span'), txt = el('span', 't'), x = el('button', 'x', '×'), tip = el('div', 'p');
  w.style.position = 'relative'; pill.tabIndex = 0; pill.role = 'link'; pill.ariaLabel = 'Open this request in Redline';
  x.title = x.ariaLabel = 'Collapse';
  pill.append(dot, txt, x); w.append(tip, pill); root.appendChild(w);

  function render() {
    var p = [], l;
    if (sum.duration_ms != null) p.push(ms(sum.duration_ms));
    if (sum.query_count != null) p.push(sum.query_count + ' q');
    txt.textContent = p.join(' · ') || 'Redline';
    if (n) {
      l = kinds.length ? (kinds[0] == 'n_plus_one' ? 'N+1' : kinds[0].replace(/_/g, ' ')) + (kinds.length > 1 ? ' +' + (kinds.length - 1) : '') : n + ' issues';
      txt.append(' · ', el('span', 'i', '⚠ ' + l));
    }
    dot.className = 'd' + (sum.status >= 500 ? ' e' : n || sum.status >= 400 ? ' w' : '');
    pill.className = 'b' + (col ? ' c' : '');
  }
  function collapse(v) { col = v; try { localStorage.setItem(K, v ? '1' : '0'); } catch (e) {} render(); }
  function open() { window.open(mount + '/#/requests/' + encodeURIComponent(id), '_blank', 'noopener'); }

  // The trace is stored just after the response is sent: retry a few times.
  var started = 0;
  function load(i) {
    fetch(mount + '/api/traces/' + encodeURIComponent(id), { credentials: 'same-origin' })
      .then(function (r) { if (r.status == 404 && i < 3) { setTimeout(load, 700 * (i + 1), i + 1); return; } return r.ok && r.json(); })
      .then(function (t) {
        if (!t) return;
        var is = t.issues || [];
        n = is.length; kinds = [];
        is.forEach(function (q) { if (kinds.indexOf(q.kind) < 0) kinds.push(q.kind); });
        sum.duration_ms = t.duration_ms; sum.query_count = t.query_count; sum.status = t.status; render();
        tip.textContent = is.length ? '' : 'No issues detected';
        is.slice(0, 5).forEach(function (q) { tip.appendChild(el('div', 0, '• ' + q.title)); });
      })
      .catch(function () { if (i < 3) setTimeout(load, 700 * (i + 1), i + 1); });
  }
  function lazy() { if (!started++ && window.fetch) { tip.textContent = 'Loading…'; load(0); } }
  function hov(v) { tip.className = 'p' + (v && !col ? ' o' : ''); if (v) lazy(); }
  pill.onmouseenter = function () { hov(1); };
  pill.onmouseleave = function () { hov(0); };
  pill.onfocus = lazy;
  pill.onkeydown = function (e) { if (e.key == 'Enter' || e.key == ' ') { e.preventDefault(); col ? collapse(0) : open(); } };
  x.onclick = function (e) { e.stopPropagation(); collapse(1); };
  x.onpointerdown = function (e) { e.stopPropagation(); };

  // A click opens the request (or expands a collapsed badge); a drag of more than 4 px moves it.
  var d, moved;
  pill.onpointerdown = function (e) {
    if (e.button) return;
    var r = host.getBoundingClientRect();
    d = { x: e.clientX, y: e.clientY, l: r.left, t: r.top }; moved = 0;
    pill.setPointerCapture(e.pointerId);
  };
  pill.onpointermove = function (e) {
    if (!d) return;
    var dx = e.clientX - d.x, dy = e.clientY - d.y, r = host.getBoundingClientRect();
    if (!moved && Math.abs(dx) + Math.abs(dy) < 5) return;
    moved = 1; hov(0);
    host.style.left = Math.max(0, Math.min(innerWidth - r.width, d.l + dx)) + 'px';
    host.style.top = Math.max(0, Math.min(innerHeight - r.height, d.t + dy)) + 'px';
    host.style.right = host.style.bottom = 'auto';
  };
  pill.onpointerup = function () { if (!d) return; var m = moved; d = 0; if (!m) col ? collapse(0) : open(); };
  pill.onpointercancel = function () { d = 0; };

  render();
  function add() { (document.body || document.documentElement).appendChild(host); }
  document.body ? add() : document.addEventListener('DOMContentLoaded', add);
})();
