"""Pure domain: the reference signal and the rules that measure error against it.

Nothing here performs I/O or knows what a browser, a model or a filesystem is.
Everything downstream may import this layer; this layer imports only the standard
library and pydantic.

Submodules are imported explicitly (``from ui_servo.domain.contract import
DirectionContract``) rather than re-exported here, so that ``python -m
ui_servo.domain.contract`` executes exactly one module object.
"""
