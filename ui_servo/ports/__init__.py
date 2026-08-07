"""Ports: the interfaces the control loop needs from the world.

A port is stated as a :class:`typing.Protocol` plus the plain immutable values
that cross it. Nothing here performs I/O, imports an adapter, or depends on a
concrete library -- that is precisely what makes an adapter swappable and a test
double free. Ports may import ``ui_servo.domain``; nothing else inward.

Submodules are imported explicitly (``from ui_servo.ports.sanitizer import
SanitizerPort``) rather than re-exported here, so importing one port never drags
in the rest.
"""
