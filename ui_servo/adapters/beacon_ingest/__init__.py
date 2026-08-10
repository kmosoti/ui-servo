"""Beacon ingest: the probe's sendBeacon batches, turned into evidence.

A package rather than a module so the transport quirks of ``sendBeacon`` (see
:mod:`~ui_servo.adapters.beacon_ingest.router`) stay in one place and the rest of
the adapter layer imports a router, not a decoder.
"""

from ui_servo.adapters.beacon_ingest.router import (
    BEACON_PATH,
    HEALTH_PATH,
    KNOWN_PROBE_KINDS,
    MAX_BODY_BYTES,
    MAX_EVENTS,
    PROBE_SOURCE,
    BeaconError,
    BeaconRouter,
    BeaconTooLarge,
    IngestHealth,
    create_beacon_router,
    decode_batch,
    validate_turn_id,
)

__all__ = [
    "BEACON_PATH",
    "HEALTH_PATH",
    "KNOWN_PROBE_KINDS",
    "MAX_BODY_BYTES",
    "MAX_EVENTS",
    "PROBE_SOURCE",
    "BeaconError",
    "BeaconRouter",
    "BeaconTooLarge",
    "IngestHealth",
    "create_beacon_router",
    "decode_batch",
    "validate_turn_id",
]
