"""tapo-cli verbs.

Phase 1a ships only the meta verbs (``auth``, ``config``); the actual camera
verbs land in later phases:

* Phase 1b: ``discover``, ``list``, ``info``
* Phase 1c: ``snapshot``, ``stream``, ``record``
* Phase 1d: ``ptz``, ``preset``, ``motion``, ``alarm``, ``led``, ``privacy``,
  ``night-vision``, ``audio``, ``osd``, ``set``, ``reboot``, ``groups``,
  ``batch``

Camera verbs are intentionally NOT importable from here yet — see
``cli.py`` and the wrapper module for the in-progress contracts.
"""
