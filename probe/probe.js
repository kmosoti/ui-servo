/* ui-servo browser sensor. Observes, never corrects. Config, event kinds,
 * beacon protocol, and why each check works the way it does: probe/README.md --
 * the prose lives there because this file is on a wire budget.
 *
 * SIZE, measured 2026-08-06 (see README "Size"):
 *   gzip -n -9 -c probe/probe.js | wc -c   -> 5196   (budget 4096)
 *   same file with comments and indentation stripped -> 3995
 * The budget is met by the served form, not by this readable one; every byte
 * above it is comment and indentation, not sensor. */
(() => {
  'use strict';
  const cfg = window.__UI_SERVO__ ?? {};
  const mot = cfg.motion ?? {};
  const BEACON = cfg.beacon ?? '/beacon';
  const TURN = cfg.turnId ?? null;
  const QUEUE_CAP = 500;    // events; oldest-drop past this
  const BEACON_CAP = 32768; // bytes per sendBeacon call
  const OVER_CAP = 12;      // overflow offenders per event (descendants only)
  const EPS = 0.001;        // ms; float-repr slack, not a tolerance band
  const SLOW = 100;         // ms; interaction latency threshold
  const URGENT = /error|reject|csp|panic/; // flush now: the page may be dying

  const dl = mot.durations ?? mot.durations_ms;
  const dur = dl?.length ? dl : null;
  const eas = new Set(mot.easings ?? []);
  const props = new Set(mot.properties ?? mot.animatable_properties ?? []);
  const noMotion = mot.reducedMotionRequired ?? mot.reduced_motion_required ?? false;

  let queue = [];
  let lost = 0;
  let seq = 0;

  const el = (v) => (v instanceof Element ? v : undefined);
  const cut = (s) => (typeof s === 'string' ? s.slice(0, 800) : undefined);
  const msg = (e) => e?.message ?? String(e);
  const on = (t, fn, o) => addEventListener(t, fn, o);
  const ms = (v) => Math.round(v || 0);
  const sel = (n) => (n ? [n.localName, n.id && `#${n.id}`,
    n.getAttribute?.('class')].filter(Boolean).join(' ') : null);
  const kids = (r) => (r?.querySelectorAll ? [r, ...r.querySelectorAll('*')] : r ? [r] : []);
  const span = (n) => n?.closest?.('[data-span-id]')?.getAttribute('data-span-id')
    || n?.id || cfg.spanId || `anon-${++seq}`;
  const pack = (kind, payload, spanId, node) =>
    ({ spanId, turnId: TURN, kind, ts: ms(performance.now()), node, payload });
  const trim = () => queue.length > QUEUE_CAP && (lost += queue.splice(0, queue.length - QUEUE_CAP).length);
  const emit = (kind, payload, n) => {
    const node = el(n);
    queue.push(pack(kind, payload, typeof n === 'string' ? n : span(node), sel(node)));
    trim();
    if (URGENT.test(kind)) flush();
  };

  // Chunked; failures re-queue and re-chunk, so a batch can never grow unsendable.
  const flush = () => {
    if (lost) { queue.unshift(pack('probe-drop', { dropped: lost }, 'probe')); lost = 0; }
    if (!queue.length) return;
    const pend = queue; const failed = []; let part = []; let bytes = 2;
    queue = [];
    const ship = () => {
      let ok = false;
      try { ok = navigator.sendBeacon?.(BEACON, JSON.stringify(part)) ?? false; } catch { /* */ }
      if (!ok) failed.push(...part);
      part = []; bytes = 2;
    };
    for (const ev of pend) {
      const n = new Blob([JSON.stringify(ev)]).size + 1; // quotas count UTF-8 bytes
      if (part.length && bytes + n > BEACON_CAP) ship();
      part.push(ev); bytes += n;
    }
    if (part.length) ship();
    if (failed.length) { queue = failed.concat(queue); trim(); }
  };

  setInterval(flush, 2000);
  on('visibilitychange', () => document.visibilityState === 'hidden' && flush());

  const gaps = [!dur && 'durations', !eas.size && 'easings', !props.size && 'properties'].filter(Boolean);
  if (gaps.length) emit('probe-config-incomplete', { missing: gaps }, 'probe');

  on('error', (e) => {
    if (e.error || e.message) {
      emit('js-error', { message: e.message ?? msg(e.error), source: e.filename,
        line: e.lineno, stack: cut(e.error?.stack) }, el(e.target) ?? 'probe');
    } else if (el(e.target)) { // resource load failure
      emit('js-error', { message: 'resource load failed', src: e.target.src || e.target.href }, e.target);
    }
  }, true);
  on('unhandledrejection', (e) =>
    emit('rejection', { message: msg(e.reason), stack: cut(e.reason?.stack) }, 'probe'));
  on('securitypolicyviolation', (e) => emit('csp', { blocked: e.blockedURI, source: e.sourceFile,
    directive: e.effectiveDirective || e.violatedDirective, line: e.lineNumber }, 'probe'));

  // Reported by convention; the define() shim below is the backstop.
  on('ui-servo:ce-error', (e) => emit('ce-error', { message: e.detail?.message, phase: e.detail?.phase }, el(e.target)), true);
  on('ui-servo:wasm-panic', (e) => emit('wasm-panic', { message: e.detail?.message, module: e.detail?.module, stack: cut(e.detail?.stack) }, el(e.target)), true);

  const ce = window.customElements;
  if (ce?.define) {
    const native = ce.define.bind(ce);
    ce.define = (name, ctor, options) => {
      for (const hook of ['connectedCallback', 'attributeChangedCallback', 'adoptedCallback']) {
        const fn = ctor?.prototype?.[hook];
        if (typeof fn !== 'function' || fn.$servo) continue;
        const wrap = function (...a) {
          try { return fn.apply(this, a); } catch (err) { // reported, not re-thrown
            emit('ce-error', { message: msg(err), phase: hook, tag: name, stack: cut(err?.stack) }, el(this));
          }
        };
        wrap.$servo = true;
        ctor.prototype[hook] = wrap;
      }
      return native(name, ctor, options);
    };
  }
  for (const m of ['instantiate', 'instantiateStreaming']) {
    const fn = window.WebAssembly?.[m];
    if (typeof fn !== 'function') continue;
    WebAssembly[m] = (...a) => Promise.resolve(fn.apply(WebAssembly, a)).catch((err) => {
      emit('wasm-error', { method: m, message: msg(err) }, 'probe');
      throw err;
    });
  }

  // `element` entries come from the server's elementtiming attribute.
  const observe = (type, fn) => {
    try {
      new PerformanceObserver((l) => l.getEntries().forEach(fn))
        .observe({ type, buffered: true, durationThreshold: SLOW });
    } catch { /* unsupported type: a missing sensor, not a fault */ }
  };
  observe('longtask', (e) => emit('longtask', { duration: ms(e.duration) }, 'probe'));
  observe('layout-shift', (e) => e.hadRecentInput // user-driven reflow is fine
    || emit('layout-shift', { value: Number(e.value.toFixed(5)),
      sources: (e.sources ?? []).slice(0, 5).map((s) => sel(s.node)) }, el(e.sources?.[0]?.node)));
  observe('event', (e) => e.duration > SLOW
    && emit('interaction', { name: e.name, duration: ms(e.duration) }, el(e.target)));
  observe('element', (e) => emit('element-timing',
    { identifier: e.identifier, renderTime: ms(e.renderTime || e.loadTime) }, el(e.element)));

  // Strings and [attr] blocks go first, so a[href$=".pdf"] does not define
  // ".pdf"; a hex escape eats one trailing space, so `.\31 0xl` is "10xl".
  const known = new Set();
  const STRING = /(["'])(?:\\.|(?!\1).)*\1/g;
  const ATTR = /\[[^\]]*\]/g;
  const E = '\\\\(?:[0-9a-fA-F]{1,6}[ \\t\\r\\n\\f]?|[^\\n])';
  const ESCAPE = new RegExp(E, 'g');
  const CLASS = new RegExp(`\\.((?:${E}|[\\w-])+)`, 'g');
  const HEX = /^[0-9a-fA-F]{1,6}[ \t\r\n\f]?$/;
  const plain = (m) => (HEX.test(m.slice(1)) ? String.fromCodePoint(parseInt(m.slice(1), 16)) : m.slice(1));
  const harvest = (rules) => {
    for (const rule of rules ?? []) {
      if (rule.selectorText) {
        for (const [, raw] of rule.selectorText.replace(STRING, '').replace(ATTR, '').matchAll(CLASS)) {
          known.add(raw.replace(ESCAPE, plain));
        }
      }
      if (rule.cssRules) harvest(rule.cssRules); // @media, @layer, @scope
    }
  };
  const vocab = () => {
    known.clear();
    for (const s of document.styleSheets) {
      try { harvest(s.cssRules); } catch { /* cross-origin: unreadable by design */ }
    }
  };

  const report = (root) => {
    const bad = new Map();
    for (const n of kids(root)) for (const c of n.classList ?? []) {
      if (!known.has(c)) bad.set(c, (bad.get(c) ?? 0) + 1);
    }
    if (bad.size) {
      emit('unknown-class', { vocabulary: known.size,
        classes: [...bad].slice(0, 40).map(([name, count]) => ({ name, count })) }, root);
    }
  };

  // A fragment may ship its own CSS: rebuild, and wait for any <link>.
  const SHEETS = 'style, link[rel~="stylesheet"]';
  const classes = (root) => {
    const carriers = root.querySelectorAll ? [...root.querySelectorAll(SHEETS)] : [];
    if (root.matches?.(SHEETS)) carriers.push(root);
    if (carriers.length) vocab();
    const wait = carriers.filter((n) => n.tagName === 'LINK' && !n.sheet);
    let left = wait.length;
    if (!left) return report(root);
    const settled = () => { if (!--left) { vocab(); report(root); } };
    for (const l of wait) for (const t of ['load', 'error']) l.addEventListener(t, settled, { once: true });
  };

  // Any excess at all. The scroller is always probed; the cap is for descendants.
  const overflow = (root) => requestAnimationFrame(() => requestAnimationFrame(() => {
    const scroller = document.scrollingElement;
    const over = (n) => n && n.scrollWidth > n.clientWidth;
    const hits = [];
    const add = (n) => hits.push({ node: sel(n), scrollWidth: n.scrollWidth,
      clientWidth: n.clientWidth, root: n === scroller });
    if (over(scroller)) add(scroller);
    let found = 0;
    for (const n of kids(root)) if (n !== scroller && over(n) && ++found <= OVER_CAP) add(n);
    if (hits.length) emit('overflow', { viewport: innerWidth, offenders: hits }, root);
  }));

  // CSS animations put the timing function on the keyframes, element.animate()
  // on the effect. Keyframes all at the API default "linear" mean the effect
  // easing is the real one; otherwise each keyframe easing is its own segment.
  const META = new Set(['offset', 'computedOffset', 'easing', 'composite']);
  const readEffect = (anim, timing) => {
    const seen = new Set(); const frames = [];
    try {
      for (const f of anim.effect?.getKeyframes?.() ?? []) {
        for (const k of Object.keys(f)) {
          if (!META.has(k)) seen.add(k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`));
        }
        if (f.easing) frames.push(f.easing);
      }
    } catch { /* some engines expose no keyframes for transitions */ }
    if (!seen.size && anim.transitionProperty) seen.add(anim.transitionProperty);
    const segs = frames.some((e) => e !== 'linear') ? frames : [timing?.easing ?? 'linear'];
    return { properties: [...seen], easings: [...new Set(segs)] };
  };

  const motion = (root) => {
    let anims = [];
    try { anims = root.getAnimations?.({ subtree: true }) ?? []; } catch { return; }
    for (const anim of anims) {
      const timing = anim.effect?.getComputedTiming?.();
      if (!timing) continue;
      // Exact: 140.4ms is not the 140ms token, and rounding is how drift hides.
      const raw = Number(timing.duration) || 0;
      const { properties, easings } = readEffect(anim, timing);
      const why = [];
      if (dur && !dur.some((d) => Math.abs(d - raw) <= EPS)) {
        why.push(`duration ${raw}ms off-token`);
      }
      for (const e of easings) if (eas.size && !eas.has(e)) why.push(`easing ${e} off-token`);
      for (const p of properties) if (props.size && !props.has(p)) why.push(`property ${p} not animatable`);
      if (noMotion && matchMedia?.('(prefers-reduced-motion: reduce)')?.matches && raw > 0) {
        why.push('runs under prefers-reduced-motion');
      }
      const ev = { name: anim.animationName ?? anim.transitionProperty ?? anim.id ?? null,
        duration: Math.round(raw * 1000) / 1000, easing: easings[0], easings, properties };
      const target = anim.effect?.target ?? root;
      if (why.length) emit('motion-violation', { ...ev, reasons: why }, target);
      else emit('animation', ev, target);
    }
  };

  const onSwap = (root) => {
    if (!el(root)) return;
    emit('swap-ok', { children: root.children?.length }, root);
    classes(root); motion(root); overflow(root);
  };
  on('htmx:afterSwap', (e) => onSwap(e.detail?.target ?? e.detail?.elt ?? e.target));
  on('htmx:afterSettle', vocab); // settling can bring more stylesheets
  for (const type of ['htmx:responseError', 'htmx:sendError', 'htmx:timeout']) {
    on(type, (e) => {
      const d = e.detail ?? {};
      emit('swap-error', { event: type, status: d.xhr?.status,
        path: d.pathInfo?.requestPath ?? d.requestConfig?.path }, d.elt ?? el(e.target));
    });
  }

  const boot = () => { vocab(); onSwap(document.body); };
  if (document.readyState === 'loading') on('DOMContentLoaded', boot);
  else boot();

  // Escape hatch for fixtures and non-htmx swaps.
  window.__UI_SERVO_PROBE__ = { emit, flush, onSwap, vocab, known, queue: () => queue };
})();
