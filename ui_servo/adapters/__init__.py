"""Adapters: concrete implementations of the ports, where the world gets in.

This is the only layer allowed to import third-party machinery -- browsers,
model CLIs, sanitisers, stores. Each module here implements one port and is
substitutable for another implementation of the same port without the control
loop noticing.

Submodules are imported explicitly (``from ui_servo.adapters.nh3_sanitizer
import Nh3Sanitizer``) so that importing one adapter never imports the heavy
dependencies of the others.
"""
