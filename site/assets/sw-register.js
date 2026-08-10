/* Prod-only, the mirror of the dev-only probe: a worker that cached the dev
 * loop's edits would serve yesterday's page to today's measurement.
 *
 * Registered on load rather than at parse time so the worker's install — which
 * fetches the whole precache list — competes with nothing the visitor is
 * waiting for. A rejection is swallowed: an unsupported or blocked worker is a
 * site without offline, not a site with an error in its console. */
if ('serviceWorker' in navigator) {
  addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  });
}
