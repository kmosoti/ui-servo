# Vendored third-party runtime assets

Pinned on purpose. An audit engine fetched at observation time would make the
loop's verdicts depend on the network and on whatever upstream shipped that
morning, so a turn-over-turn comparison of the evidence stock would be measuring
the engine as much as the UI.

| file | package | version | sha256 | source |
| --- | --- | --- | --- | --- |
| `axe.min.js` | [axe-core](https://github.com/dequelabs/axe-core) (MPL-2.0) | 4.10.2 | `b511cd9dec01c76f4b2ad1723b66b6db37d4c2eb4ed199076e1829d9ee7b75e3` | `https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js` |

`ui_servo.adapters.playwright_sensor` verifies the digest on every first use and
raises `SensorError` on a mismatch, so the pin is enforced rather than documented.

To refresh:

```sh
curl -sSL https://cdn.jsdelivr.net/npm/axe-core@<version>/axe.min.js \
  -o ui_servo/adapters/vendor/axe.min.js
sha256sum ui_servo/adapters/vendor/axe.min.js
```

then move `AXE_VERSION` / `AXE_SHA256` in `playwright_sensor.py` and this table
in the same commit.
