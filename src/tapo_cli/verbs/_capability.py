"""Shared capability-gate helper for Phase 2 verbs (SRD §3.3.1, S4).

Every Phase 2 verb (ptz, preset, alarm, audio, osd) consults the per-feature
capability matrix BEFORE issuing a pytapo call. When the gate fails, we exit
code 5 (``unsupported_feature``) with a structured hint listing the model
prefixes that DO support the feature.

Two utilities live here so the verb modules don't each re-roll the same lookup:

* :func:`resolve_model_for_target` — pull a model string from config (cheap)
  or from a live ``getBasicInfo`` call (when the alias has no model field).
* :func:`require_feature` — assert the model supports a boolean feature, else
  raise :class:`UnsupportedFeatureError` with the standard hint shape.
* :func:`require_ptz` — same pattern for the tri-state ``ptz_mode`` capability.

The model resolution is intentionally tolerant: if we can't determine the
model at all (no config entry AND no successful basic-info probe), we treat
it as "unknown" and fail the gate. Operators get a clear hint to either add
a ``model`` field in config or run ``info`` first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from tapo_cli.device_info import (
    capabilities_for_model,
    feature_supported,
    flatten_basic_info,
    models_supporting,
    ptz_mode_for_model,
)
from tapo_cli.errors import UnsupportedFeatureError

logger = logging.getLogger("tapo_cli")


async def resolve_model_for_target(
    tapo: Any,
    *,
    config_model: str | None,
) -> str:
    """Return the model string for a connected target.

    Prefer the config-resolved value (avoids a network round-trip). If the
    alias has no ``model`` set and no ``getBasicInfo`` succeeds, return
    ``""`` so the gate logic can treat it as "unknown".
    """
    if config_model:
        return config_model

    try:
        raw: object = await asyncio.to_thread(tapo.getBasicInfo)
    except Exception as exc:
        logger.debug("getBasicInfo failed during capability resolve: %s", exc)
        return ""

    info = flatten_basic_info(raw)
    for key in ("device_model", "model"):
        v = info.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def require_feature(
    *,
    model: str,
    target: str,
    feature: str,
    verb_name: str,
) -> None:
    """Raise :class:`UnsupportedFeatureError` if model lacks ``feature``.

    The hint lists model prefixes from the §3.3.1 matrix that DO support the
    feature. Used by ``alarm``, ``audio.tts``, ``audio.mic``, ``audio.speaker``,
    ``osd.text``, ``osd.timestamp``, ``preset``, etc.
    """
    if feature_supported(model, feature):
        return

    supporters = models_supporting(feature)
    model_label = model or "unknown"
    raise UnsupportedFeatureError(
        f"{verb_name} unsupported on {model_label}: feature={feature!r}",
        target=target,
        hint=(
            f"Per SRD §3.3.1, '{feature}' is supported on: "
            f"{', '.join(supporters) if supporters else '(no current models)'}."
        ),
    )


def require_ptz(
    *,
    model: str,
    target: str,
    require_zoom: bool = False,
) -> str:
    """Assert the model supports PTZ; return its ``ptz_mode``.

    When ``require_zoom`` is True, ALSO require the model to carry the
    ``zoom`` capability flag (zoom motors are physically present on a
    smaller subset than pan/tilt motors).

    Returns ``"step"`` or ``"continuous"``. Raises
    :class:`UnsupportedFeatureError` for ``"none"`` or unknown models.
    """
    mode = ptz_mode_for_model(model)
    model_label = model or "unknown"
    if mode == "none":
        supporters = models_supporting("ptz_mode")
        raise UnsupportedFeatureError(
            f"ptz unsupported on {model_label}: model has no PTZ motors",
            target=target,
            hint=(
                "Per SRD §3.3.1, PTZ is supported on: "
                f"{', '.join(supporters) if supporters else '(no current models)'}."
            ),
        )

    if require_zoom:
        caps = capabilities_for_model(model)
        if not caps.get("zoom"):
            supporters = models_supporting("zoom")
            raise UnsupportedFeatureError(
                f"zoom unsupported on {model_label}: no zoom motor",
                target=target,
                hint=(
                    "Per SRD §3.3.1, zoom is supported on: "
                    f"{', '.join(supporters) if supporters else '(no current models)'}."
                ),
            )

    return mode


__all__ = [
    "require_feature",
    "require_ptz",
    "resolve_model_for_target",
]
