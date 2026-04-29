"""tapo-cli verbs.

Phase progression:

* Phase 1a: ``auth``, ``config`` (meta verbs).
* Phase 1b: ``discover``, ``list``, ``info``.
* Phase 1c: ``snapshot``, ``stream``, ``record`` (parallel branch).
* Phase 1d: ``privacy``, ``led``, ``night-vision``, ``motion``, ``reboot``.
* Phase 2+: ``ptz``, ``preset``, ``alarm``, ``audio``, ``osd``, ``set``,
  ``groups``, ``batch``, motion ``history``.

Phase 1d state-control verbs are surfaced through :mod:`tapo_cli.cli`;
the per-verb modules live in this package as ``privacy_cmd``,
``led_cmd``, ``night_vision_cmd``, ``motion_cmd``, ``reboot_cmd``.
"""
