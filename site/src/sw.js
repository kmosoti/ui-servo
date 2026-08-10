/* sw.js — the offline half of the site, as a template.
 *
 * This file is never served as it stands. The version token below is stamped by
 * whoever hands it to a browser: the exporter replaces it with a fingerprint of
 * everything else in dist/, and the dev server replaces it with "dev". The
 * cache name is derived from that, so a new build is a new cache and the old
 * one is evicted on activate rather than being patched in place. (The token is
 * written once, on the VERSION line — a second copy of it in this comment would
 * be rewritten too, and would then read as a sentence about a hex string.)
 *
 * It lives at the document root, and that is not a preference. A worker's
 * maximum scope is its own directory unless the host sends
 * Service-Worker-Allowed, and GitHub Pages sends no such header — a worker
 * under /assets/ could never answer a navigation to /, which is the only thing
 * this file exists to do.
 *
 * Nothing here is bundled, imported, or transpiled: a build step between the
 * source and the worker is a build step that can ship a worker nobody read. */

var VERSION = '__UI_SERVO_SW_VERSION__';
var CACHE_PREFIX = 'ui-servo-';
var CACHE = CACHE_PREFIX + VERSION;

/* Only a stamped build holds anything.
 *
 * `cargo run` without UI_SERVO_DEV=1 is a *non-dev* server, so its pages carry
 * the registration and this worker installs on 127.0.0.1. If it cached there it
 * would cache under one constant name — the version is the literal "dev", not a
 * fingerprint — and every later edit to portfolio.css would be invisible behind
 * a cache-first rule with nothing to invalidate it. So the local worker keeps
 * nothing and answers nothing. Activate still runs its eviction pass, which
 * means a visit to localhost also *clears* whatever an earlier build left
 * there; offline is exercised against the export, which is the artefact that
 * ships. */
var EPHEMERAL = VERSION === 'dev';

/* Everything the site needs to open cold, in the form a browser asks for it —
 * URLs, not the `about/index.html` paths the exporter writes. `/404.html` is
 * here because it is the one page a visitor reaches by mistake, and offline is
 * exactly when mistakes look like the site being broken.
 *
 * Two things are deliberately absent. The `.woff2` binaries: they are the
 * heaviest bytes on the site, they are named by fonts.css rather than by any
 * page, and a sibling change renames them — a stale name here would fail the
 * whole addAll and leave the visitor with no cache at all. And
 * manifest.webmanifest, which another unit adds and this one cannot assume
 * exists. Both are picked up by the runtime /assets/ cache below on the first
 * online visit, which is the same visit that installs this worker.
 *
 * `addAll` is all-or-nothing by design — a half-filled cache is worse than
 * none, because it looks like offline support — so the Rust side asserts that
 * every page here is in the route manifest and every asset here is in dist/. */
var PRECACHE = [
  '/',
  '/about',
  '/projects/blackcell',
  '/projects/splunk-dashboard-studio',
  '/projects',
  '/writing',
  '/404.html',
  '/assets/favicon.svg',
  '/assets/portfolio.css',
  '/assets/portfolio.js',
  '/assets/fonts/fonts.css',
  '/assets/tokens.css',
  '/assets/site.css',
  '/assets/htmx.min.js',
  '/assets/islands/loader.js'
];

/* Install: fill the cache named after this build, then take over immediately.
 *
 * skipWaiting rather than waiting for every tab to close, because the version
 * is a fingerprint of the asset set: a worker that keeps serving the previous
 * cache is serving a page whose stylesheet may already have been replaced on
 * the host. One consistent generation at a time is the property worth having. */
self.addEventListener('install', function (event) {
  var filled = EPHEMERAL
    ? Promise.resolve()
    : caches.open(CACHE).then(function (cache) {
        return cache.addAll(PRECACHE);
      });
  event.waitUntil(filled.then(function () {
    return self.skipWaiting();
  }));
});

/* Activate: delete every cache of ours that is not this build's.
 *
 * Not tidiness. Origin storage is a quota, not a wastebasket — a site that
 * added a cache per deploy and never removed one would spend that quota on
 * pages nobody can reach, and the browser would eventually evict the whole
 * origin rather than the stale half. The name is the only version marker, so
 * "ours, and not equal to CACHE" is the whole rule.
 *
 * *Ours* is load-bearing: on 127.0.0.1 an origin is shared with every other
 * local dev server anybody has ever run, and an unprefixed sweep would delete
 * their caches too. */
self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.map(function (name) {
        var ours = name.indexOf(CACHE_PREFIX) === 0;
        return ours && name !== CACHE ? caches.delete(name) : null;
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

/* A cached response a navigation may legally be answered with.
 *
 * A file host answers `/about` with a 301 to `/about/`, so the entry precached
 * under `/about` carries a redirect in its URL list. Handing that back for a
 * navigation is a network error — navigations have redirect mode "manual", and
 * the platform refuses a response that already followed one. Rebuilding the
 * response drops the trail and keeps the bytes, which is the difference between
 * five of the seven pages working offline and only the home page working. */
function navigable(response) {
  if (!response.redirected) {
    return response;
  }
  return response.blob().then(function (body) {
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers
    });
  });
}

/* The cache keys a navigation could be satisfied by, most specific first.
 *
 * The same 301 that produces a redirected response also moves the visitor: they
 * asked for `/about` once, and every reload, bookmark and history entry
 * afterwards says `/about/`. Both spellings name one page, and only one of them
 * is a key in the cache, so both are tried before giving up. `/` is last —
 * offline on a URL this build never precached is still better answered with the
 * site than with the browser's dinosaur. */
function pageKeys(url) {
  var path = url.pathname;
  var keys = [path];
  if (path.length > 1) {
    keys.push(path.slice(-1) === '/' ? path.slice(0, -1) : path + '/');
  }
  keys.push('/');
  return keys;
}

/* The last resort, and a real Response rather than nothing.
 *
 * `respondWith` handed a promise for `undefined` throws a TypeError, which the
 * browser reports as a network error — strictly worse than the failure the
 * visitor would have had if this worker had never intercepted. */
function offlinePage() {
  return new Response(
    '<!DOCTYPE html><meta charset="utf-8"><title>Offline</title>' +
      '<p>This page is not available offline.</p>',
    { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
  );
}

function cachedPage(url) {
  var keys = pageKeys(url);
  var attempt = function (index) {
    if (index >= keys.length) {
      return offlinePage();
    }
    return caches.match(keys[index]).then(function (hit) {
      return hit ? navigable(hit) : attempt(index + 1);
    });
  };
  return attempt(0);
}

/* Fetch: network-first for pages, cache-first for assets, invisible otherwise.
 *
 * Network-first for navigations because this site is edited far more often than
 * it is read offline, and a visitor who is online should never be shown
 * yesterday's page to save a round trip. Cache-first for /assets/ because those
 * are fingerprint-free URLs whose contents change only when a deploy changes
 * them — and a deploy changes the cache name, which discards them anyway.
 *
 * Anything that is not a same-origin GET is passed through untouched: a POST
 * has no cache semantics worth inventing, and another origin's response is not
 * this worker's to keep. */
self.addEventListener('fetch', function (event) {
  if (EPHEMERAL) {
    return;
  }
  var request = event.request;
  if (request.method !== 'GET') {
    return;
  }
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(function () {
        return cachedPage(url);
      })
    );
    return;
  }

  if (url.pathname.indexOf('/assets/') === 0) {
    event.respondWith(
      caches.match(request).then(function (hit) {
        if (hit) {
          return hit;
        }
        return fetch(request).then(function (response) {
          /* Only a real, complete, same-origin answer is worth keeping. An
           * opaque or partial response put into the cache would be served
           * back forever as if it were the asset. The write is held open with
           * waitUntil, or the worker can be terminated between returning the
           * response and the put landing. */
          if (response && response.ok && response.type === 'basic') {
            var copy = response.clone();
            event.waitUntil(caches.open(CACHE).then(function (cache) {
              return cache.put(request, copy);
            }));
          }
          return response;
        });
      })
    );
  }
});
