/* Vanilla port of the golden path's DCLogic component
 * (experiments/Kennedy Mosoti - Portfolio.dc.html + support.js).
 *
 * Same behaviours, same values, no runtime: the dc template's {{ bindings }}
 * became data-* attributes and ids, the component state became module state,
 * and the client-side screen switching became real routes — the header nav is
 * server-rendered, so this file only ever runs against the page it is on.
 * Colour constants, timings, and copy are the golden file's, verbatim. */
(function () {
  'use strict';

  var EMBER = '#ff7a45', BORDER = '#24282e', DIM = '#9aa0a7', CRIT = '#ef4759';

  var ITEM_TAGS = {
    jpmc: ['splunk', 'logstash', 'kafka', 'salt', 'python', 'bash', 'linux', 'rca', 'drift', 'dr', 'red', 'use', 'eda', 'logging'],
    dat: ['aieval', 'python'],
    aws: ['aws', 'terraform'],
    ncr: ['python'],
    blackcell: ['python', 'selfhealing'],
    praxis: ['rust', 'python'],
    sds: ['python', 'splunk', 'red', 'use'],
    sai: ['python'],
    los: ['python'],
    kernform: ['rust', 'python', 'hexagonal', 'scaffolding']
  };

  var TAG_LABEL = { splunk: 'Splunk', logstash: 'Logstash', kafka: 'Kafka', cribl: 'Cribl', red: 'RED', use: 'USE', rca: 'RCA', logging: 'Logging', python: 'Python', rust: 'Rust', bash: 'Bash', hexagonal: 'Hexagonal Architecture', eda: 'Event-Driven Architecture', selfhealing: 'Self-Healing Systems', salt: 'SaltStack', terraform: 'Terraform', scaffolding: 'Scaffolding & Conformance', linux: 'Linux', aws: 'AWS', vmware: 'VMware / vSphere', dr: 'HA/DR Readiness', drift: 'Configuration-Drift Detection', aieval: 'AI Code Evaluation', contexteng: 'Context Engineering', otel: 'OpenTelemetry & Tracing', harness: 'Harness Engineering' };

  var RESUME = {
    name: 'Kennedy Mosoti',
    title: 'Observability Platform Engineer',
    focus: 'Branching into agentic engineering',
    links: { github: 'https://github.com/kmosoti' },
    summary: 'Observability platform engineer supporting a centralized, multi-tenant Splunk/Logstash/Kafka platform. Automation in Python, Salt, and Terraform; reliability work across HA/DR readiness, configuration-drift detection, and root-cause analysis.',
    experience: [
      { company: 'Netbuilder (JPMC LogA Platform)', role: 'Associate Observability Engineer', start: '2024-08', end: null, highlights: ['Automated search-filter updates across 3,000+ Splunk roles in Python', 'Login-banner orchestration across 100+ search heads in Salt', 'SHC repave design for 15 on-prem nodes', 'Duplicate-detection for configuration drift; DR-readiness investigation; RCA down to SELinux-context drift'], tags: ITEM_TAGS.jpmc },
      { company: 'Data Annotation Tech', role: 'AI Code Evaluation Contractor', start: '2024-01', end: null, highlights: ['Evaluate AI-generated code and reasoning in Python, JavaScript, and SQL for correctness, efficiency, maintainability, and edge-case handling'], tags: ITEM_TAGS.dat },
      { company: 'Amazon Web Services', role: 'Associate Solutions Architect', start: '2023-01', end: '2023-11', highlights: ['Advised enterprise customers on availability, security, scalability, and performance', 'Built a cloud-migration cost-estimation tool and cloud-native prototypes'], tags: ITEM_TAGS.aws },
      { company: 'NCR Voyix / ServiceLink', role: 'Software Engineering Intern & IT Help Desk Support', start: null, end: null, highlights: ['Python automation for mobile-testing workflows', 'Web-application, account-access, and SQL-reporting support'], tags: ITEM_TAGS.ncr }
    ],
    projects: [
      { name: 'BlackCell', status: 'pre-alpha', summary: 'Local-first, evidence-gated control runtime for coding agents.', url: 'https://github.com/kmosoti/blackcell', tags: ITEM_TAGS.blackcell },
      { name: 'splunk-dashboard-studio', status: 'pre-alpha', summary: 'Pydantic 2 compiler/validator for Splunk Dashboard Studio, version-aware 9.4-10.4.', url: 'https://github.com/kmosoti/splunk-dashboard-studio-python', tags: ITEM_TAGS.sds },
      { name: 'Kernform', status: 'pre-alpha', summary: 'Deterministic project scaffolding and repo-conformance tool. Rust core, PyO3 bridge, Python SDK/CLI.', url: null, tags: ITEM_TAGS.kernform },
      { name: 'PraxisLedger', status: 'early bootstrap', summary: 'Provenance and temporal knowledge graph. SQLite + Rust + Python.', url: 'https://github.com/kmosoti/PraxisLedger', tags: ITEM_TAGS.praxis },
      { name: 'SAI', status: 'pre-alpha', summary: 'Agent routing modeled on brain-network dynamics.', url: null, tags: ITEM_TAGS.sai },
      { name: 'learning-os', status: 'active', summary: 'Adaptive personal-learning app. FastAPI + SQLAlchemy.', url: 'https://github.com/kmosoti/learning-os', tags: ITEM_TAGS.los }
    ],
    skills: {
      observability: ['Splunk', 'Logstash', 'Kafka', 'Cribl', 'RED', 'USE', 'SRE', 'RCA', 'Metrics', 'Alerts', 'Traces', 'Events', 'Logging'],
      programming: ['Python', 'Rust', 'Bash'],
      architecture: ['Hexagonal Architecture', 'Event-Driven Architecture', 'Self-Healing Systems'],
      configuration: ['SaltStack', 'Terraform', 'Scaffolding & Conformance'],
      infrastructure: ['Linux', 'AWS', 'VMware / vSphere', 'HA/DR Readiness', 'Configuration-Drift Detection'],
      aiAssisted: ['AI Code Evaluation', 'Context Engineering', 'OpenTelemetry & Tracing', 'Harness Engineering']
    },
    education: [{ degree: 'B.S. Software Engineering', school: 'University of Texas at Arlington' }],
    certifications: ['Splunk Certified Admin', 'Splunk Power User', 'Cribl Certified User', 'AWS Certified Solutions Architect — Associate']
  };

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  /* ---------- shared state --------------------------------------------- */

  var reduced = false;
  try {
    var stored = localStorage.getItem('pf-reduced');
    if (stored !== null) reduced = stored === '1';
    else if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) reduced = true;
  } catch (e) { /* storage may be unavailable; the golden default is fine */ }

  var t0 = Date.now();
  var fps = 0;

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, reduced ? Math.min(ms, 40) : ms); }); }

  function applyReduced() {
    document.documentElement.classList.toggle('pf-reduced', reduced);
    var btn = $('#pf-motion');
    if (btn) { btn.textContent = 'motion ' + (reduced ? 'off' : 'on'); btn.style.color = reduced ? DIM : EMBER; }
    $$('.pf-caret').forEach(function (el) { el.style.animation = reduced ? 'none' : 'km-blink 1s step-end infinite'; });
    $$('[data-token]').forEach(function (el) { el.style.transition = reduced ? 'none' : 'transform .4s cubic-bezier(.4,0,.2,1)'; });
    // batch/canvas-energy, 2026-08-09: toggling ON needs no help (schedule()
    // keeps requesting frames while !reduced); toggling OFF is what would
    // otherwise strand the canvases on whatever half-drawn frame was on
    // screen the instant the switch flipped, so wake() owes the loop the one
    // settled frame reduced mode renders and idles after.
    wake();
  }

  /* ---------- header: motion toggle, résumé menu, dir toggle ------------ */

  var motionBtn = $('#pf-motion');
  if (motionBtn) motionBtn.addEventListener('click', function () {
    reduced = !reduced;
    try { localStorage.setItem('pf-reduced', reduced ? '1' : '0'); } catch (e) {}
    applyReduced();
  });

  var resumeBtn = $('#pf-resume-btn'), resumeMenu = $('#pf-resume-menu');
  if (resumeBtn && resumeMenu) {
    resumeBtn.addEventListener('click', function () {
      resumeMenu.style.display = resumeMenu.style.display === 'block' ? 'none' : 'block';
    });
    document.addEventListener('click', function (e) {
      if (!resumeBtn.contains(e.target) && !resumeMenu.contains(e.target)) resumeMenu.style.display = 'none';
    });
  }
  $$('.pf-print').forEach(function (el) {
    el.addEventListener('click', function (e) {
      e.preventDefault();
      if (resumeMenu) resumeMenu.style.display = 'none';
      setTimeout(function () { window.print(); }, 60);
    });
  });
  $$('.pf-json').forEach(function (el) {
    el.addEventListener('click', function (e) {
      e.preventDefault();
      var payload = Object.assign({}, RESUME, { generatedAt: new Date().toISOString() });
      var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = 'kennedy-mosoti-resume.json';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
      if (resumeMenu) resumeMenu.style.display = 'none';
    });
  });

  /* ---------- typed command --------------------------------------------- */

  (function typeCmd() {
    var els = $$('.pf-typed');
    if (!els.length) return;
    var text = ' whoami', i = 0;
    (function step() {
      els.forEach(function (el) { el.textContent = text.slice(0, i); });
      i++;
      if (i <= text.length) setTimeout(step, 60);
    })();
  })();

  /* ---------- margin canvases (warp starfield) --------------------------- */

  // The warp starfield (owner ruling, 2026-08-09): picked over the golden
  // constellation and two other candidates (orbital chart, signal
  // waterfall). Three parallax depth layers drifting like a window on a
  // moving ship; the cursor shifts the layers at depth-proportional rates
  // instead of repelling anything. Occasional micro warp-streaks, and a
  // rare piece of tumbling wireframe debris.
  function makeWarp(cv) {
    if (!cv) return null;
    var ctx = cv.getContext('2d');
    var W = 0, H = 0, layers = [], streaks = [], debris = null, dTimer = 8, t = 0;
    var offx = 0, offy = 0;
    function size() {
      W = cv.clientWidth; H = cv.clientHeight;
      if (!W || !H) return;
      var dpr = window.devicePixelRatio || 1;
      cv.width = W * dpr; cv.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      layers = [
        { d: 0.25, r: 0.7, al: 0.35, sp: 7, stars: [] },
        { d: 0.55, r: 1.1, al: 0.55, sp: 16, stars: [] },
        { d: 1.0, r: 1.7, al: 0.9, sp: 30, stars: [] }
      ];
      layers.forEach(function (L) {
        var n = Math.max(14, Math.round(W * H / 16000 * L.d));
        for (var i = 0; i < n; i++) L.stars.push({ x: Math.random() * W, y: Math.random() * H, w: 1 + Math.random() * 2.5, ph: Math.random() * Math.PI * 2 });
      });
      streaks = []; debris = null; dTimer = 5 + Math.random() * 10;
    }
    size();
    function draw(dt, still, mx, my) {
      if (cv.clientWidth !== W || cv.clientHeight !== H) size();
      if (!W || !H) return;
      if (!still) {
        t += dt;
        var tx = (mx - window.innerWidth / 2) * 0.02, ty = (my - window.innerHeight / 2) * 0.02;
        if (mx < -999) { tx = 0; ty = 0; }
        offx += (tx - offx) * Math.min(1, dt * 3);
        offy += (ty - offy) * Math.min(1, dt * 3);
        layers.forEach(function (L) {
          for (var i = 0; i < L.stars.length; i++) {
            var s = L.stars[i];
            s.y += L.sp * dt;
            if (s.y > H + 4) { s.y = -4; s.x = Math.random() * W; }
          }
        });
        if (Math.random() < dt * 0.5) streaks.push({ x: 6 + Math.random() * (W - 12), y: -30, sp: 500 + Math.random() * 400, len: 40 + Math.random() * 50, al: 0.5 + Math.random() * 0.3 });
        for (var st = streaks.length - 1; st >= 0; st--) {
          streaks[st].y += streaks[st].sp * dt;
          if (streaks[st].y - streaks[st].len > H) streaks.splice(st, 1);
        }
        dTimer -= dt;
        if (!debris && dTimer <= 0) {
          debris = { x: Math.random() * W, y: -30, vx: (Math.random() - 0.5) * 6, vy: 9 + Math.random() * 6, a1: Math.random() * 6, a2: Math.random() * 6, w1: 0.3 + Math.random() * 0.4, w2: 0.2 + Math.random() * 0.4, s: 11 + Math.random() * 7 };
        }
        if (debris) {
          debris.x += debris.vx * dt; debris.y += debris.vy * dt;
          debris.a1 += debris.w1 * dt; debris.a2 += debris.w2 * dt;
          if (debris.y > H + 40) { debris = null; dTimer = 14 + Math.random() * 18; }
        }
      }
      ctx.clearRect(0, 0, W, H);
      layers.forEach(function (L) {
        for (var i = 0; i < L.stars.length; i++) {
          var s = L.stars[i];
          var tw = 0.75 + 0.25 * Math.sin(t * s.w + s.ph);
          ctx.fillStyle = 'rgba(236,231,221,' + (L.al * tw).toFixed(3) + ')';
          var sx = (s.x + offx * L.d * 10 % W + W) % W;
          ctx.beginPath(); ctx.arc(sx, (s.y + offy * L.d * 10 % H + H) % H, L.r, 0, Math.PI * 2); ctx.fill();
        }
      });
      for (var ds = 0; ds < streaks.length; ds++) {
        var sk = streaks[ds];
        var lg = ctx.createLinearGradient(sk.x, sk.y - sk.len, sk.x, sk.y);
        lg.addColorStop(0, 'rgba(236,231,221,0)');
        lg.addColorStop(1, 'rgba(236,231,221,' + sk.al.toFixed(3) + ')');
        ctx.strokeStyle = lg;
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(sk.x, sk.y - sk.len); ctx.lineTo(sk.x, sk.y); ctx.stroke();
      }
      if (debris) {
        // A tumbling wireframe tetrahedron, projected flat.
        var V = [[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]];
        var c1 = Math.cos(debris.a1), s1 = Math.sin(debris.a1), c2 = Math.cos(debris.a2), s2 = Math.sin(debris.a2);
        var P = V.map(function (v) {
          var x = v[0] * c1 - v[2] * s1, z = v[0] * s1 + v[2] * c1;
          var y = v[1] * c2 - z * s2;
          return [debris.x + x * debris.s, debris.y + y * debris.s];
        });
        ctx.strokeStyle = 'rgba(255,122,69,.4)';
        ctx.lineWidth = 1;
        var E = [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]];
        for (var e = 0; e < E.length; e++) {
          ctx.beginPath(); ctx.moveTo(P[E[e][0]][0], P[E[e][0]][1]); ctx.lineTo(P[E[e][1]][0], P[E[e][1]][1]); ctx.stroke();
        }
      }
    }
    return { size: size, draw: draw };
  }

  /* ---------- black hole (BlackCell hero) -------------------------------- */

  // A gravitationally-lensed accretion disk (owner, 2026-08-09, v2 —
  // "scientific accuracy, as far as humans would see it"). The optics the
  // iconic Interstellar/EHT image is made of, approximated in 2D:
  //
  //  - The NEAR half of the thin disk is seen directly: a flat band
  //    (inclination ~78°, y squashed to 0.22) crossing IN FRONT of the
  //    shadow, drawn last.
  //  - The FAR half is never hidden — its light bends around the hole.
  //    Each far-side particle renders twice: a primary image arched OVER
  //    the shadow, and a fainter secondary image arched UNDER it, both
  //    hugging the photon ring and meeting the band at the sides.
  //  - Doppler beaming: emission from gas approaching the viewer
  //    (cos a > 0 side) is boosted and blue-shifted toward white-amber;
  //    the receding side stays dim ember. One side of the ring glows.
  //  - Additive compositing ('lighter'), so overlapping streaks build up
  //    to a gaseous glow instead of flat strokes.
  function makeBlackhole(cv) {
    if (!cv) return null;
    var ctx = cv.getContext('2d');
    var W = 0, H = 0, parts = [], t = 0;
    var SHADOW = 0.14, TILT = 0.22;
    // Each mote carries its own turbulence phase/frequency and a speed
    // jitter so the disk reads as sheared gas, not lockstep tracer dots;
    // fade ramps 0→1 after a respawn so re-entries never pop in.
    function spawn(rIn, rMax, settled) {
      return {
        r: rIn + Math.pow(Math.random(), 0.75) * (rMax - rIn),
        a: Math.random() * Math.PI * 2,
        w: 0.6 + Math.random() * 1.6,
        ph: Math.random() * Math.PI * 2,
        s: 0.8 + Math.random() * 0.4,
        fade: settled ? 1 : 0
      };
    }
    function size() {
      W = cv.clientWidth; H = cv.clientHeight;
      if (!W || !H) return;
      var dpr = window.devicePixelRatio || 1;
      cv.width = W * dpr; cv.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var m = Math.min(W, H), rsh = m * SHADOW;
      var rIn = rsh * 1.35, rMax = m * 0.5 - 8;
      // 320 was tuned for the 430px desktop canvas. The mobile breakpoints
      // (2026-08-10) render the same disk at 320px and 240px, where the full
      // count is both invisible and wasteful on a phone battery — scale with
      // area so density reads the same. At m=430 this is exactly 320, so the
      // desktop rendering is bit-identical to before.
      var count = Math.max(120, Math.round(320 * (m * m) / (430 * 430)));
      parts = Array.from({ length: count }, function () { return spawn(rIn, rMax, true); });
    }
    size();
    function draw(dt, still) {
      if (cv.clientWidth !== W || cv.clientHeight !== H) size();
      if (!W || !H) return;
      var cx = W / 2, cy = H / 2, m = Math.min(W, H);
      var rsh = m * SHADOW, rIn = rsh * 1.35, rMax = m * 0.5 - 8;
      if (!still) {
        t += dt;
        for (var i = 0; i < parts.length; i++) {
          var p = parts[i];
          p.a += (26 / Math.pow(p.r, 1.5)) * 60 * dt * p.s;
          p.r -= (1.2 + 500 / p.r) * 0.5 * dt;
          if (p.fade < 1) p.fade = Math.min(1, p.fade + dt * 1.4);
          if (p.r <= rIn) {
            var np = spawn(rIn, rMax, false);
            np.r = rMax - Math.random() * 14;
            parts[i] = np;
          }
        }
      }
      ctx.clearRect(0, 0, W, H);
      // Faint thermal halo.
      var glow = ctx.createRadialGradient(cx, cy, rsh, cx, cy, rMax * 1.1);
      glow.addColorStop(0, 'rgba(255,122,69,.08)');
      glow.addColorStop(1, 'rgba(255,122,69,0)');
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, W, H);

      // Beamed colour: dim ember (receding) → hot white-amber (approaching).
      var paint = function (boost, alpha) {
        var r = Math.round(255);
        var g = Math.round(122 + (214 - 122) * boost);
        var b = Math.round(69 + (170 - 69) * boost);
        return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha.toFixed(3) + ')';
      };

      ctx.save();
      ctx.translate(cx, cy);
      ctx.globalCompositeOperation = 'lighter';

      // Pass 1 — lensed images of the FAR half (sin a < 0): primary arch
      // over the shadow, faint secondary arch under it.
      for (var i = 0; i < parts.length; i++) {
        var p = parts[i];
        if (Math.sin(p.a) >= 0) continue;
        var boost = Math.pow((Math.cos(p.a) + 1) / 2, 1.5);
        var bright = (0.25 + 0.75 * boost) * p.fade;
        var pr = p.r + Math.sin(t * p.w + p.ph) * 2.4;
        var heat = Math.min(1, Math.max(0, 1 - (pr - rIn) / (rMax - rIn)));
        // Primary: bent over the top, compressed close to the photon ring so
        // it reads as a continuous arch of light, not a speckle dome. Arc
        // length grows with heat so the inner flow smears into ribbons.
        var rho = rsh * 1.12 + (pr - rIn) * 0.22;
        var xi = Math.cos(p.a) * rho * 0.97;
        var psi = Math.atan2(-Math.sqrt(Math.max(rho * rho - xi * xi, 0.001)), xi);
        ctx.strokeStyle = paint(boost * 0.9, (0.08 + 0.30 * heat) * bright);
        ctx.lineWidth = 0.9 + heat * 1.2;
        ctx.beginPath();
        ctx.arc(0, 0, rho, psi, psi + 0.14 + 0.14 * heat);
        ctx.stroke();
        // Secondary: dimmer, tighter, under the shadow.
        var rho2 = rsh * 1.05 + (pr - rIn) * 0.08;
        var xi2 = Math.cos(p.a) * rho2 * 0.95;
        var psi2 = Math.atan2(Math.sqrt(Math.max(rho2 * rho2 - xi2 * xi2, 0.001)), xi2);
        ctx.strokeStyle = paint(boost * 0.7, (0.04 + 0.14 * heat) * bright);
        ctx.lineWidth = 0.7 + heat * 0.7;
        ctx.beginPath();
        ctx.arc(0, 0, rho2, psi2, psi2 + 0.12 + 0.10 * heat);
        ctx.stroke();
      }
      ctx.restore();

      // The shadow itself — light neither escapes nor composites.
      ctx.save();
      ctx.translate(cx, cy);
      ctx.beginPath();
      ctx.arc(0, 0, rsh, 0, Math.PI * 2);
      ctx.fillStyle = '#000';
      ctx.fill();
      // Photon ring: a thin full circle, plus a beamed bright arc on the
      // approaching side.
      ctx.shadowColor = 'rgba(255,150,100,.8)';
      ctx.shadowBlur = 10;
      ctx.beginPath();
      ctx.arc(0, 0, rsh + 0.8, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(255,170,120,.55)';
      ctx.lineWidth = 1.1;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(0, 0, rsh + 0.8, -0.7, 0.7);
      ctx.strokeStyle = 'rgba(255,224,180,.95)';
      ctx.lineWidth = 1.6;
      ctx.stroke();
      ctx.restore();

      // Pass 2 — the NEAR half seen directly: the thin band, in front.
      ctx.save();
      ctx.translate(cx, cy);
      ctx.globalCompositeOperation = 'lighter';
      ctx.scale(1, TILT);
      for (var k = 0; k < parts.length; k++) {
        var q = parts[k];
        if (Math.sin(q.a) < 0) continue;
        var boost2 = Math.pow((Math.cos(q.a) + 1) / 2, 1.5);
        var bright2 = (0.25 + 0.75 * boost2) * q.fade;
        var qr = q.r + Math.sin(t * q.w + q.ph) * 2.4;
        var heat2 = Math.min(1, Math.max(0, 1 - (qr - rIn) / (rMax - rIn)));
        ctx.strokeStyle = paint(boost2, (0.10 + 0.38 * heat2) * bright2);
        ctx.lineWidth = (1.0 + heat2 * 1.5) / TILT * 0.35;
        ctx.beginPath();
        ctx.arc(0, 0, qr, q.a, q.a + Math.min(0.5, (22 + 14 * heat2) / qr));
        ctx.stroke();
      }
      ctx.restore();
    }
    return { size: size, draw: draw };
  }

  /* --- telemetry pipeline rig (splunk-dashboard-studio) --------------------
     A real queueing model, not a loop of moving dots: Poisson arrivals,
     exponential service, a bounded buffer -> M/M/1/K. The dashboard generated
     below it is compiled from what this actually did, so the numbers have to
     mean something. Exposed as a scene so it inherits the idle-CPU rules
     above (hidden tab, reduced motion, debounced resize) for free. */
  function makePipeline(canvas) {
    if (!canvas) return null;
    var ctx = canvas.getContext('2d', { alpha: false });
    var MU = 40, CAP = 120, STEP = 1 / 120, V = 165, G = 820;
    var ROLL = { hot: 5, warm: 9, cold: 14 };
    var lam = 26, acc = 0, warmed = false;
    var flight = [], buffer = [], drops = [], server = null, left = 0, nextArr = 0;
    var tiers = { hot: [], warm: [], cold: [], frozen: [] };
    var stored = 0, dropped = 0, peak = 0, areaL = 0, areaT = 0, tSim = 0;
    var W = 0, H = 0, P = {};

    function expo(rate) { return -Math.log(1 - Math.random()) / rate; }

    function size() {
      var w = canvas.clientWidth || 720, h = canvas.clientHeight || 270;
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      W = w; H = h;
      // Inner margins: labels are centred on their station and bins straddle
      // theirs, so the outermost of each needs clearance or it draws off-canvas.
      var padX = Math.max(34, Math.min(58, w * 0.05));
      var binW = Math.max(40, Math.min(64, w * 0.072));
      var L = padX, R = w - padX, span = R - L;
      var tR = R - binW / 2, gap = Math.min(span * 0.125, 112), tL = tR - gap * 3;
      var stride = (tL - L) / 4;
      P = {
        lane: Math.max(52, h * 0.26), tier: h * 0.68, binW: binW,
        src: L, agent: L + stride, buf: L + stride * 2, proc: L + stride * 3, idx: tL,
        hot: tL, warm: tL + gap, cold: tL + gap * 2, frozen: tR
      };
    }

    function advance(dt) {
      tSim += dt;
      nextArr -= dt;
      while (nextArr <= 0) {
        flight.push({ x: P.src, y: P.lane, to: 'agent', r: 2.8 });
        nextArr += expo(lam);
      }
      for (var i = flight.length - 1; i >= 0; i--) {
        var p = flight[i];
        var target = p.to === 'agent' ? P.agent : p.to === 'buf' ? P.buf
                   : p.to === 'idx' ? P.idx : P.hot;
        p.x += V * dt;
        if (p.to === 'tier') p.y += (P.tier - p.y) * Math.min(1, dt * 6);
        if (p.x >= target) {
          p.x = target;
          if (p.to === 'agent') { p.to = 'buf'; }
          else if (p.to === 'buf') {
            if (buffer.length >= CAP) { dropped++; drops.push({ x: p.x, y: p.y, vx: -26, vy: -105, life: 1 }); }
            else { buffer.push(p); }
            flight.splice(i, 1);
          } else if (p.to === 'idx') { p.to = 'tier'; p.r = p.r * 0.72; }
          else { stored++; tiers.hot.push({ age: 0 }); flight.splice(i, 1); }
        }
      }
      // Service consumes the step continuously and starts the next job with
      // the time left over. Decrementing once per tick instead rounds every
      // service up to a step boundary and wastes another going idle, which
      // inflates a 25ms mean by a third and puts the queue 3x above M/M/1.
      var rem = dt;
      while (rem > 0) {
        if (!server) { if (!buffer.length) break; server = buffer.shift(); left = expo(MU); }
        var used = Math.min(rem, left);
        left -= used; rem -= used;
        if (left <= 1e-12) { server.to = 'idx'; server.x = P.proc; flight.push(server); server = null; }
      }
      areaL += (buffer.length + (server ? 1 : 0)) * dt; areaT += dt;
      for (var d = drops.length - 1; d >= 0; d--) {
        var q = drops[d];
        q.x += q.vx * dt; q.vy += G * dt; q.y += q.vy * dt; q.life -= dt * 0.5;
        if (q.y > H + 10 || q.life <= 0) drops.splice(d, 1);
      }
      var chain = [['hot', 'warm'], ['warm', 'cold'], ['cold', 'frozen']];
      for (var c = 0; c < chain.length; c++) {
        var arr = tiers[chain[c][0]];
        for (var k = arr.length - 1; k >= 0; k--) {
          arr[k].age += dt;
          if (arr[k].age > ROLL[chain[c][0]]) { arr.splice(k, 1); tiers[chain[c][1]].push({ age: 0 }); }
        }
        while (arr.length > 200) arr.shift();
      }
      while (tiers.frozen.length > 140) tiers.frozen.shift();
      var rho = lam / MU;
      if (rho > peak) peak = rho;
    }

    function lab(x, y, text) {
      ctx.textAlign = 'center';
      ctx.fillStyle = '#6e747b';
      ctx.font = '700 9.5px ' + MONO_STACK;
      ctx.fillText(text, x, y);
    }

    function bin(x, list, colour, name) {
      var w = P.binW, h = 46, y = P.tier;
      ctx.strokeStyle = '#24282e'; ctx.strokeRect(x - w / 2, y - h / 2, w, h);
      var fill = Math.min(1, list.length / 80);
      ctx.fillStyle = colour; ctx.globalAlpha = 0.2;
      ctx.fillRect(x - w / 2, y + h / 2 - fill * h, w, fill * h);
      ctx.globalAlpha = 1;
      lab(x, y - h / 2 - 8, name);
      ctx.fillStyle = '#6e747b'; ctx.textAlign = 'center';
      ctx.font = '400 9px ' + MONO_STACK;
      ctx.fillText(list.length, x, y + h / 2 + 12);
    }

    function draw(dt, still) {
      if (!W || canvas.clientWidth !== W) size();
      if (!still) advance(Math.min(dt, 0.1));
      else if (!warmed) { warmed = true; for (var i = 0; i < 700; i++) advance(STEP); }

      ctx.fillStyle = '#0c0e11'; ctx.fillRect(0, 0, W, H);
      ctx.lineWidth = 1; ctx.strokeStyle = '#24282e';
      ctx.beginPath(); ctx.moveTo(P.src, P.lane); ctx.lineTo(P.idx, P.lane);
      ctx.lineTo(P.hot, P.tier - 26); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(P.hot, P.tier); ctx.lineTo(P.frozen, P.tier); ctx.stroke();

      var stations = [[P.src, 'SOURCES'], [P.agent, 'AGENT'], [P.buf, 'BUFFER'],
                      [P.proc, 'PROCESS'], [P.idx, 'INDEX']];
      for (var s = 0; s < stations.length; s++) {
        ctx.beginPath(); ctx.moveTo(stations[s][0], P.lane - 20); ctx.lineTo(stations[s][0], P.lane + 20); ctx.stroke();
        lab(stations[s][0], P.lane - 28, stations[s][1]);
      }

      var frac = Math.min(1, buffer.length / CAP);
      ctx.strokeStyle = frac > 0.8 ? '#ef4759' : '#24282e';
      ctx.strokeRect(P.buf - 11, P.lane + 20, 22, 44);   // flush with the station tick
      ctx.fillStyle = frac > 0.8 ? '#ef4759' : '#f2b134'; ctx.globalAlpha = 0.35;
      ctx.fillRect(P.buf - 11, P.lane + 64 - frac * 44, 22, frac * 44);
      ctx.globalAlpha = 1;

      ctx.strokeStyle = '#24282e'; ctx.strokeRect(P.proc - 9, P.lane - 9, 18, 18);
      if (server) { ctx.fillStyle = '#ff7a45'; ctx.beginPath(); ctx.arc(P.proc, P.lane, 4, 0, 7); ctx.fill(); }

      bin(P.hot, tiers.hot, '#ff7a45', 'HOT');
      bin(P.warm, tiers.warm, '#f2b134', 'WARM');
      bin(P.cold, tiers.cold, '#9aa3ad', 'COLD');
      bin(P.frozen, tiers.frozen, '#5c636b', 'FROZEN');

      for (var f = 0; f < flight.length; f++) {
        var pp = flight[f];
        ctx.fillStyle = pp.to === 'tier' ? '#ff7a45' : '#9aa0a7';
        ctx.globalAlpha = pp.to === 'tier' ? 0.9 : 0.65;
        ctx.beginPath(); ctx.arc(pp.x, pp.y, pp.r, 0, 7); ctx.fill();
      }
      ctx.globalAlpha = 1;
      for (var g = 0; g < drops.length; g++) {
        ctx.globalAlpha = Math.max(0, drops[g].life); ctx.fillStyle = '#ef4759';
        ctx.beginPath(); ctx.arc(drops[g].x, drops[g].y, 2.4, 0, 7); ctx.fill();
      }
      ctx.globalAlpha = 1;
    }

    size();
    return {
      size: size, draw: draw,
      setLambda: function (v) { lam = v; areaL = 0; areaT = 0; peak = v / MU; },
      metrics: function () {
        return {
          lambda: lam, mu: MU, rho: lam / MU, peak: peak, cap: CAP,
          buffer: buffer.length, dropped: dropped, stored: stored,
          L: areaT > 0 ? areaL / areaT : 0, elapsed: tSim,
          tiers: { hot: tiers.hot.length, warm: tiers.warm.length,
                   cold: tiers.cold.length, frozen: tiers.frozen.length }
        };
      }
    };
  }

  var MONO_STACK = "'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace";
  var pipeline = makePipeline($('#pf-spd-canvas'));
  var scenes = [makeWarp($('#pf-canvas-l')), makeWarp($('#pf-canvas-r')), makeBlackhole($('#pf-bh')), pipeline].filter(Boolean);

  // Idle-CPU orchestration (batch/canvas-energy, 2026-08-09): the rAF loop
  // below used to run forever at display rate — a backgrounded tab kept
  // painting off-screen, and reduced motion re-rasterized an identical frame
  // every tick instead of drawing it once and stopping. Scenes still decide
  // WHAT a frame looks like (untouched); this only decides WHEN one runs, so
  // the one frame reduced mode shows is pixel-identical to before.
  var mx = -9999, my = -9999;
  window.addEventListener('mousemove', function (e) { mx = e.clientX; my = e.clientY; }, { passive: true });

  var rafId = null, needsFrame = true;          // one frame owed: boot, toggle, resize, tab return
  function wake() { needsFrame = true; if (rafId === null && !document.hidden) schedule(); }

  function applyCanvasVisibility() {
    var show = window.innerWidth >= 1280 ? 'block' : 'none';
    $$('.pf-canvas').forEach(function (el) { el.style.display = show; });
  }
  var resizeTimer = null;
  window.addEventListener('resize', function () {
    applyCanvasVisibility();                     // immediate: two style writes, keeps the 1280px flip live
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {       // the re-seed is the expensive half; draw() self-heals any
      scenes.forEach(function (sc) { sc.size(); }); // frame rendered in between, and reduced mode renders its
      wake();                                    // one frame after settle.
    }, 150);
  });
  applyCanvasVisibility();

  var frames = 0, fpsMark = performance.now(), last = performance.now();
  function raf(now) {
    rafId = null;
    frames++;
    if (now - fpsMark >= 1000) { fps = Math.round(frames * 1000 / (now - fpsMark)); frames = 0; fpsMark = now; }
    var dt = Math.min((now - last) / 1000, 0.1);
    last = now;
    scenes.forEach(function (sc) { sc.draw(dt, reduced, mx, my); });
    needsFrame = false;
    schedule();
  }
  function schedule() {
    if (document.hidden) return;                 // resumed by visibilitychange
    if (reduced && !needsFrame) return;          // still frame delivered; idle at zero rAF
    rafId = requestAnimationFrame(raf);
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; } }
    else { last = performance.now(); fpsMark = last; frames = 0; wake(); }
  });
  schedule();

  /* ---------- live metrics (home) ---------------------------------------- */

  function readMetrics() {
    // batch/canvas-energy, 2026-08-09: a hidden tab still ticks this every
    // second (setInterval keeps firing in the background); the DOM walk for
    // node-count is the part worth skipping, so bail before doing any of it.
    if (document.hidden) return;
    if (!$('#pf-m-up')) return;
    var ms = function (v) { return (v == null || !isFinite(v) || v < 0) ? '—' : Math.round(v) + 'ms'; };
    var nav = null;
    try { nav = performance.getEntriesByType('navigation')[0]; } catch (e) { nav = null; }
    var domNodes = 0;
    try { domNodes = document.getElementsByTagName('*').length; } catch (e) {}
    var res = 0;
    try { res = performance.getEntriesByType('resource').length; } catch (e) {}
    var el = Math.floor((Date.now() - t0) / 1000);
    var up = String(Math.floor(el / 60)).padStart(2, '0') + ':' + String(el % 60).padStart(2, '0');
    var set = function (id, v) { var e = $(id); if (e) e.textContent = v; };
    set('#pf-m-ttfb', nav ? ms(nav.responseStart - nav.requestStart) : '—');
    set('#pf-m-nodes', domNodes || '—');
    set('#pf-m-dom', nav ? ms(nav.domContentLoadedEventEnd - nav.startTime) : '—');
    set('#pf-m-fps', fps ? fps + '/s' : '—');
    var fe = $('#pf-m-fps');
    if (fe) fe.style.color = fps && fps < 40 ? '#f2b134' : '#ece7dd';
    set('#pf-m-res', res || '—');
    set('#pf-m-up', up);
  }
  if ($('#pf-m-up')) { readMetrics(); setInterval(readMetrics, 1000); }

  /* ---------- tag cross-reference + job accordion (home) ----------------- */

  var activeTag = null;
  var openJob = 'jpmc';

  function applyFilter() {
    var set = activeTag ? [activeTag] : null;
    $$('.pf-tag').forEach(function (btn) {
      var k = btn.getAttribute('data-tag');
      var on = set ? set.indexOf(k) !== -1 : false;
      if (set) {
        btn.style.borderColor = on ? EMBER : BORDER;
        btn.style.background = on ? EMBER : 'transparent';
        btn.style.color = on ? '#140904' : DIM;
        btn.style.opacity = on ? 1 : 0.3;
      } else {
        btn.style.borderColor = BORDER; btn.style.background = 'transparent';
        btn.style.color = DIM; btn.style.opacity = 1;
      }
    });
    $$('.pf-item').forEach(function (item) {
      var tags = (item.getAttribute('data-tags') || '').split(' ');
      var match = set ? tags.some(function (t) { return set.indexOf(t) !== -1; }) : true;
      item.style.borderColor = set && match ? EMBER : BORDER;
      item.style.opacity = set && !match ? 0.3 : 1;
    });
    var note = $('#pf-filter-note');
    if (note) note.textContent = activeTag ? 'filtering on: ' + (TAG_LABEL[activeTag] || activeTag) + ' — click again to clear.' : '';
  }
  $$('.pf-tag').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var k = btn.getAttribute('data-tag');
      activeTag = activeTag === k ? null : k;
      applyFilter();
    });
  });

  function applyJobs() {
    $$('[data-job-body]').forEach(function (el) {
      el.style.display = el.getAttribute('data-job-body') === openJob ? 'block' : 'none';
    });
    $$('[data-job-icon]').forEach(function (el) {
      el.textContent = el.getAttribute('data-job-icon') === openJob ? '⌄' : '›';
    });
  }
  $$('[data-job-toggle]').forEach(function (head) {
    head.addEventListener('click', function () {
      var id = head.getAttribute('data-job-toggle');
      openJob = openJob === id ? null : id;
      applyJobs();
    });
  });

  /* ---------- flow simulators -------------------------------------------- */

  function nodeStyle(el, v, faded) {
    if (!el) return;
    if (v === 'pass') { el.style.borderColor = EMBER; el.style.boxShadow = 'inset 0 0 0 1px ' + EMBER; el.style.opacity = 1; }
    else if (v === 'fail') { el.style.borderColor = CRIT; el.style.boxShadow = 'inset 0 0 0 1px ' + CRIT; el.style.opacity = 1; }
    else if (v === 'idle') { el.style.borderColor = BORDER; el.style.boxShadow = 'none'; el.style.opacity = 0.35; }
    else { el.style.borderColor = BORDER; el.style.boxShadow = 'none'; el.style.opacity = faded ? 0.4 : 1; }
  }

  function makeSim(prefix) {
    var wrap = $('#pf-' + prefix + '-wrap');
    if (!wrap) return null;
    var token = $('#pf-' + prefix + '-token');
    var logEl = $('#pf-' + prefix + '-log');
    var runT0 = null;
    // Keyed off the generic `data-node="<prefix>:<key>"` every flow node
    // carries, not the per-prefix `data-<prefix>-node` attribute: flow_node
    // only emits that one for a hardcoded list of prefixes, so a new flow got
    // no attribute, no error, and a pipeline that silently never animated.
    function node(id) { return wrap.querySelector('[data-node="' + prefix + ':' + id + '"]'); }
    function moveToken(id) {
      var el = node(id);
      if (!el || !token) return;
      var c = wrap.getBoundingClientRect(), r = el.getBoundingClientRect();
      token.style.transform = 'translate(' + (r.left - c.left + r.width / 2 - 5) + 'px,' + (r.top - c.top + r.height / 2 - 5) + 'px)';
      token.style.opacity = 1;
    }
    function log(msg, kind) {
      if (!logEl) return;
      var el = ((performance.now() - (runT0 || performance.now())) / 1000).toFixed(1);
      var line = document.createElement('div');
      line.style.cssText = 'padding:2px 0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;';
      line.style.color = kind === 'ok' ? EMBER : kind === 'bad' ? CRIT : DIM;
      line.style.animation = reduced ? 'none' : 'km-line .2s ease';
      var ts = document.createElement('span');
      ts.style.cssText = 'opacity:.5; margin-right:9px;';
      ts.textContent = '[' + el + 's]';
      line.appendChild(ts);
      line.appendChild(document.createTextNode(msg));
      logEl.appendChild(line);
      while (logEl.children.length > 60) logEl.removeChild(logEl.firstChild);
      logEl.scrollTop = logEl.scrollHeight;
    }
    function reset(ids, fadedIds) {
      ids.forEach(function (id) { nodeStyle(node(id), null, false); });
      (fadedIds || []).forEach(function (id) { nodeStyle(node(id), null, true); });
      if (logEl) logEl.textContent = '';
      if (token) token.style.opacity = 0;
      runT0 = performance.now();
    }
    return { node: node, moveToken: moveToken, log: log, reset: reset, setRunT0: function () { runT0 = performance.now(); }, wrap: wrap };
  }

  function segStyle(btn, on) {
    if (!btn) return;
    btn.style.color = on ? '#140904' : DIM;
    btn.style.background = on ? EMBER : 'transparent';
  }

  /* --- blackcell --- */
  (function () {
    var sim = makeSim('bc');
    if (!sim) return;
    var scenario = 'clean', running = false;
    var runBtn = $('#pf-bc-run'), verifySub = $('#pf-bc-verifysub');
    $$('[data-bc-scenario]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        scenario = btn.getAttribute('data-bc-scenario');
        $$('[data-bc-scenario]').forEach(function (b) { segStyle(b, b === btn); });
      });
    });
    runBtn.addEventListener('click', function () {
      if (running) return;
      running = true;
      runBtn.disabled = true; runBtn.style.opacity = 0.5;
      (async function () {
        sim.reset(['observe', 'plan', 'execute', 'review', 'verify'], ['record', 'host']);
        if (verifySub) verifySub.textContent = '—';
        await sleep(60);
        sim.log('task submitted');
        var base = ['observe', 'plan', 'execute', 'review', 'verify'];
        var rounds = scenario === 'clean' ? [{ seq: base, attempt: 1, ok: true }]
          : scenario === 'retry' ? [{ seq: base, attempt: 1, ok: false }, { seq: base.slice(1), attempt: 2, ok: true }]
          : [{ seq: base, attempt: 1, ok: false }, { seq: base.slice(1), attempt: 2, ok: false }];
        var final = true;
        for (var r = 0; r < rounds.length; r++) {
          var round = rounds[r];
          for (var i = 0; i < round.seq.length; i++) {
            var key = round.seq[i];
            sim.moveToken(key);
            await sleep(430);
            if (key === 'observe') sim.log('observe → repository state read');
            if (key === 'plan') { if (verifySub) verifySub.textContent = 'attempt ' + round.attempt; sim.log('plan → bounded plan constructed (attempt ' + round.attempt + ')'); }
            if (key === 'execute') sim.log('execute → provider adapter, isolated worktree');
            if (key === 'review') sim.log('review → independent review complete');
            if (key === 'verify') {
              nodeStyle(sim.node('verify'), round.ok ? 'pass' : 'fail');
              if (verifySub) verifySub.textContent = 'attempt ' + round.attempt + ': ' + (round.ok ? 'passed' : 'failed');
              sim.log('verify → attempt ' + round.attempt + ' ' + (round.ok ? 'passed' : 'FAILED, retrying'), round.ok ? 'ok' : 'bad');
              final = round.ok;
            }
          }
        }
        await sleep(280);
        if (final) {
          sim.moveToken('record'); await sleep(300);
          nodeStyle(sim.node('record'), 'pass');
          sim.log('recorded → append-only evidence written', 'ok');
        } else {
          sim.moveToken('host'); await sleep(300);
          nodeStyle(sim.node('host'), 'fail');
          sim.log('returned to host → bounded attempts (2) exhausted, no autonomous retry', 'bad');
        }
        running = false;
        runBtn.disabled = false; runBtn.style.opacity = 1;
      })();
    });
  })();

  /* --- splunk-dashboard-studio --- */
  (function () {
    var sim = makeSim('sp');
    if (!sim) return;
    var version = '10.2.x', running = false;
    var t = { unknown: false, missing: false, type: false, diff: false };
    var runBtn = $('#pf-sp-run'), emitSub = $('#pf-sp-emitsub'), errEl = $('#pf-sp-err'), payloadEl = $('#pf-sp-payload');

    $$('[data-sp-version]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        version = btn.getAttribute('data-sp-version');
        $$('[data-sp-version]').forEach(function (b) { segStyle(b, b === btn); });
      });
    });
    $$('[data-sp-t]').forEach(function (cb) {
      cb.addEventListener('change', function () {
        t[cb.getAttribute('data-sp-t')] = cb.checked;
        renderPayload();
      });
    });

    function payloadRows() {
      var rows = [];
      rows.push({ t: '{', c: DIM });
      rows.push({ t: '  "title": "Revenue",', c: DIM });
      if (t.missing) rows.push({ t: '  // data_source omitted', c: CRIT });
      else rows.push({ t: '  "data_source": "index=sales",', c: DIM });
      rows.push({ t: '  "visualization": "line",', c: DIM });
      rows.push(t.type ? { t: '  "threshold": "high"' + (t.unknown ? ',' : ''), c: CRIT } : { t: '  "threshold": 500000' + (t.unknown ? ',' : ''), c: DIM });
      if (t.unknown) rows.push({ t: '  "laytout": "wide"', c: CRIT });
      rows.push({ t: '}', c: DIM });
      return rows;
    }
    function renderPayload() {
      if (!payloadEl) return;
      payloadEl.textContent = '';
      payloadRows().forEach(function (row) {
        var div = document.createElement('div');
        div.style.whiteSpace = 'pre';
        div.style.color = row.c;
        div.textContent = row.t;
        payloadEl.appendChild(div);
      });
    }
    renderPayload();

    runBtn.addEventListener('click', function () {
      if (running) return;
      running = true;
      runBtn.disabled = true; runBtn.style.opacity = 0.5;
      (async function () {
        sim.reset(['generate', 'valPy', 'valNpm', 'optimize', 'emit']);
        if (emitSub) emitSub.textContent = '—';
        if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
        await sleep(60);
        sim.log('compile → target splunk ' + version);
        var issues = [];
        if (t.unknown) issues.push('unexpected field `laytout` — did you mean `layout`?');
        if (t.missing) issues.push('missing required field `data_source`');
        if (t.type) issues.push('`threshold` expects float, got str');

        sim.moveToken('generate'); await sleep(400);
        nodeStyle(sim.node('generate'), 'pass');
        sim.log('generate → payload built from model.py');

        sim.moveToken('valPy'); await sleep(400);
        if (issues.length) {
          nodeStyle(sim.node('valPy'), 'fail');
          nodeStyle(sim.node('valNpm'), 'idle'); nodeStyle(sim.node('optimize'), 'idle'); nodeStyle(sim.node('emit'), 'idle');
          sim.log('validate:pydantic → ' + issues.length + ' issue(s)', 'bad');
          if (errEl) { errEl.textContent = issues.join(' · '); errEl.style.display = 'block'; }
          running = false; runBtn.disabled = false; runBtn.style.opacity = 1;
          return;
        }
        nodeStyle(sim.node('valPy'), 'pass');
        sim.log('validate:pydantic → clean', 'ok');

        sim.moveToken('valNpm'); await sleep(400);
        if (t.diff) {
          nodeStyle(sim.node('valNpm'), 'fail');
          nodeStyle(sim.node('optimize'), 'idle'); nodeStyle(sim.node('emit'), 'idle');
          sim.log('validate:splunk-npm → rejected by official validator', 'bad');
          if (errEl) { errEl.textContent = "schema-valid, but Splunk's own validator rejects this shape — likely a field this target version doesn't recognize yet."; errEl.style.display = 'block'; }
          running = false; runBtn.disabled = false; runBtn.style.opacity = 1;
          return;
        }
        nodeStyle(sim.node('valNpm'), 'pass');
        sim.log('validate:splunk-npm → clean', 'ok');

        sim.moveToken('optimize'); await sleep(400);
        nodeStyle(sim.node('optimize'), 'pass');
        sim.log('optimize → normalized output');

        sim.moveToken('emit'); await sleep(400);
        nodeStyle(sim.node('emit'), 'pass');
        var bytes = payloadRows().reduce(function (n, r) { return n + r.t.length + 1; }, 0);
        if (emitSub) emitSub.textContent = version + ', ~' + bytes + 'b';
        sim.log('emit → compiled for splunk ' + version, 'ok');
        running = false; runBtn.disabled = false; runBtn.style.opacity = 1;
      })();
    });
  })();

  /* --- splunk-dashboard-studio: dashboard generated from live telemetry --- */
  (function () {
    var sim = makeSim('spd');
    if (!sim || !pipeline) return;
    var running = false;
    var runBtn = $('#pf-spd-run'), emitSub = $('#pf-spd-emitsub'), jsonEl = $('#pf-spd-json');
    var lamIn = $('#pf-spd-lam'), lamV = $('#pf-spd-lamv');
    var readout = {
      rho: $('#pf-spd-rho'), buf: $('#pf-spd-buf'), drop: $('#pf-spd-drop'),
      peak: $('#pf-spd-peak'), stored: $('#pf-spd-stored')
    };

    if (lamIn) {
      lamIn.addEventListener('input', function () {
        var v = +lamIn.value;
        pipeline.setLambda(v);
        if (lamV) lamV.textContent = v + '/s';
        wake();                                   // reduced motion still owes one frame
      });
    }

    setInterval(function () {
      if (document.hidden || !readout.rho) return;
      var m = pipeline.metrics();
      readout.rho.textContent = m.rho.toFixed(2);
      readout.rho.style.color = m.rho >= 1 ? CRIT : m.rho > 0.85 ? '#f2b134' : '#ece7dd';
      readout.buf.textContent = m.buffer + '/' + m.cap;
      readout.drop.textContent = m.dropped;
      readout.drop.style.color = m.dropped ? CRIT : '#ece7dd';
      readout.peak.textContent = m.peak.toFixed(2);
      readout.stored.textContent = m.stored;
    }, 500);

    // A Dashboard Studio definition, in the shape Splunk actually expects:
    // dataSources keyed by id, visualizations referencing them, and an
    // absolute grid layout. Which panels appear is decided by the run.
    function build(m) {
      var ds = {}, viz = {}, structure = [], y = 0;
      function panel(id, title, type, query, w, h, options) {
        ds['ds_' + id] = {
          type: 'ds.search', name: title,
          options: { query: query }
        };
        viz['viz_' + id] = {
          type: type, title: title,
          dataSources: { primary: 'ds_' + id },
          options: options || {}
        };
        structure.push({
          item: 'viz_' + id, type: 'block',
          position: { x: structure.length % 2 ? 720 : 0, y: y, w: w, h: h }
        });
        if (structure.length % 2 === 0) y += h;
      }

      panel('ingest_eps', 'Ingest rate (events/sec)', 'splunk.line',
        '| mstats avg(_value) AS eps WHERE index=telemetry metric_name="pipeline.ingest.eps" span=1m',
        720, 300, { yAxisTitleText: 'events/sec' });

      // Thresholds come from the observed peak, not a default: the amber band
      // opens where this run actually started queueing.
      var amber = Math.max(0.7, Math.min(0.95, m.peak - 0.1)).toFixed(2);
      panel('utilisation', 'Utilisation ρ = λ/μ', 'splunk.singlevalue',
        '| mstats avg(_value) AS rho WHERE index=telemetry metric_name="pipeline.utilisation" span=1m',
        720, 300, {
          majorColor: '> rangeValue | rangeValueDynamic(rangeColors, rangeValues)',
          rangeValues: [+amber, 1],
          rangeColors: ['#118832', '#cba700', '#d41f1f'],
          unit: 'ρ'
        });

      panel('queue_depth', 'Buffer occupancy', 'splunk.area',
        '| mstats max(_value) AS depth WHERE index=telemetry metric_name="pipeline.buffer.depth" span=30s',
        720, 280, { yAxisMax: m.cap });

      panel('tier_mix', 'Events by storage tier', 'splunk.bar',
        'index=telemetry sourcetype=pipeline:lifecycle | stats count BY tier',
        720, 280, {});

      // Only present because this run lost data. A dashboard that always shows
      // a drop panel teaches people to ignore it.
      if (m.dropped > 0) {
        panel('drops', 'Dropped events (buffer full)', 'splunk.column',
          '| mstats sum(_value) AS dropped WHERE index=telemetry metric_name="pipeline.dropped" span=1m',
          720, 260, {});
      }

      return {
        title: 'Pipeline health' + (m.dropped > 0 ? ' — saturated' : ''),
        description: 'Generated from an observed run: λ ' + m.lambda + '/s, μ ' + m.mu
          + '/s, peak ρ ' + m.peak.toFixed(2) + ', ' + m.stored + ' events stored'
          + (m.dropped > 0 ? ', ' + m.dropped + ' dropped' : '') + '.',
        inputs: {
          input_global_trp: {
            type: 'input.timerange', title: 'Global Time Range',
            options: { token: 'global_time', defaultValue: '-24h,now' }
          }
        },
        defaults: {
          dataSources: {
            'ds.search': {
              options: {
                queryParameters: { earliest: '$global_time.earliest$', latest: '$global_time.latest$' }
              }
            }
          }
        },
        dataSources: ds,
        visualizations: viz,
        layout: {
          type: 'grid',
          globalInputs: ['input_global_trp'],
          options: { width: 1440, height: y + 320, display: 'auto-scale' },
          structure: structure
        }
      };
    }

    function paint(text) {
      if (!jsonEl) return;
      jsonEl.textContent = text;
    }

    runBtn.addEventListener('click', function () {
      if (running) return;
      running = true;
      runBtn.disabled = true; runBtn.style.opacity = 0.5;
      (async function () {
        var m = pipeline.metrics();
        sim.reset(['generate', 'valPy', 'valNpm', 'optimize', 'emit']);
        if (emitSub) emitSub.textContent = '—';
        paint('// compiling…');
        await sleep(60);
        sim.log('read telemetry → λ ' + m.lambda + '/s, ρ ' + m.rho.toFixed(2) + ', L ' + m.L.toFixed(1));

        sim.moveToken('generate'); await sleep(360);
        var doc = build(m);
        var panels = Object.keys(doc.visualizations).length;
        nodeStyle(sim.node('generate'), 'pass');
        sim.log('generate → ' + panels + ' panel(s) from observed run');

        sim.moveToken('valPy'); await sleep(340);
        nodeStyle(sim.node('valPy'), 'pass');
        sim.log('validate:pydantic → clean', 'ok');

        sim.moveToken('valNpm'); await sleep(340);
        nodeStyle(sim.node('valNpm'), 'pass');
        sim.log('validate:splunk-npm → clean', 'ok');

        sim.moveToken('optimize'); await sleep(320);
        nodeStyle(sim.node('optimize'), 'pass');
        sim.log('optimize → normalized output');

        sim.moveToken('emit'); await sleep(320);
        nodeStyle(sim.node('emit'), 'pass');
        var text = JSON.stringify(doc, null, 2);
        paint(text);
        if (emitSub) emitSub.textContent = panels + ' panels, ~' + text.length + 'b';
        sim.log('emit → pipeline-health.json (' + panels + ' panels)', 'ok');
        if (m.dropped > 0) sim.log('drop panel included — this run lost ' + m.dropped + ' events', 'bad');
        running = false; runBtn.disabled = false; runBtn.style.opacity = 1;
      })();
    });
  })();

  /* ---------- boot -------------------------------------------------------- */

  applyReduced();
  applyJobs();
  applyFilter();
})();
