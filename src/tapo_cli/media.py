"""Shared media helpers for snapshot and stream verbs.

These helpers were prototyped in ``scripts/smoke.py`` during Phase 0 and are
lifted into ``tapo_cli`` proper so the snapshot and stream verbs (Phase 1c)
can share them without depending on the operator-only smoke harness.

Three helpers, all pure-functions, all unit-testable:

* :func:`build_rtsp_url` — assemble ``rtsp://user:pass@host:port/path`` with
  the username and password percent-encoded so reserved characters in passwords
  (``@``, ``:``, ``/``, ``!``, ``?``, ``#``, ``&``) don't corrupt the URL.
* :func:`mask_url_credentials` — replace any ``scheme://user:pass@host`` payload
  with ``scheme://***:***@host`` for log lines and stderr emission. Works on
  RTSP, HTTP, and any other scheme that uses basic-auth-in-URL form.
* :func:`resolve_onvif_wsdl_dir` — find the on-disk WSDL bundle inside
  ``site-packages/onvif/wsdl/``. Required because ``onvif-zeep-async``'s
  built-in ``_WSDL_PATH`` resolves one directory too shallow on Python 3.14
  (Phase 0 BUG 2) and the constructor needs ``wsdl_dir=`` passed explicitly.

All three were moved here unmodified from ``scripts/smoke.py`` apart from
imports and docstrings; the smoke script will continue to work after re-importing
from this module in a follow-up cleanup.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

# Match ``scheme://user:pass@host`` — works for rtsp/http/https/etc.
# The ``[^/@\s]+`` segments are intentionally restrictive so embedded ``/`` or
# ``@`` in already-encoded forms doesn't match too greedily.
_AUTH_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+:[^/@\s]+@")


def build_rtsp_url(
    ip: str,
    username: str,
    password: str,
    *,
    port: int = 554,
    path: str = "stream1",
) -> str:
    """Construct an RTSP URL ffmpeg / mpv can consume directly.

    ``urllib.parse.quote`` with ``safe=''`` percent-encodes ``@``, ``:``, ``/``,
    ``!``, ``?``, ``#`` and other reserved characters that would otherwise
    corrupt the userinfo segment of the URL.
    """
    return (
        f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{ip}:{port}/{path}"
    )


def mask_url_credentials(value: str) -> str:
    """Replace any ``scheme://user:pass@host`` payload with ``scheme://***:***@host``.

    Used before any URL hits stderr or a log line. Stream's stdout payload
    intentionally contains the credentials (Unix philosophy — operators pipe
    to ffmpeg) so this helper is NOT applied to stdout in the default mode.
    """
    return _AUTH_RE.sub(lambda m: f"{m.group('scheme')}***:***@", value)


def redact_userinfo(url: str) -> str:
    """Replace the userinfo with literal ``<user>:<pass>`` placeholders.

    Used by ``stream --credentials-via-env`` (FR-12f): operators want a URL
    they can copy/paste into a notebook with the structure visible but the
    real credentials stripped.
    """
    return _AUTH_RE.sub(lambda m: f"{m.group('scheme')}<user>:<pass>@", url)


def resolve_onvif_wsdl_dir() -> Path:
    """Resolve the on-disk path of the ONVIF WSDL bundle.

    ``onvif-zeep-async`` 4.0.4 declares::

        _WSDL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "wsdl")

    in ``onvif/client.py``. From ``site-packages/onvif/client.py`` that
    resolves to ``site-packages/wsdl`` — one directory shallower than the
    actual bundle, which lives at ``site-packages/onvif/wsdl/``. We resolve
    the correct directory at runtime via the loaded onvif package's
    ``__file__`` and pass it explicitly via ``ONVIFCamera(..., wsdl_dir=...)``.

    Raises:
        FileNotFoundError: when the WSDL bundle isn't where it's expected,
            which means onvif-zeep-async isn't installed correctly.
    """
    import onvif  # type: ignore[import-untyped]

    pkg_dir = Path(onvif.__file__).resolve().parent
    wsdl = pkg_dir / "wsdl"
    if not wsdl.is_dir():
        raise FileNotFoundError(
            f"ONVIF unavailable — install or pin onvif-zeep-async correctly "
            f"(expected wsdl/ directory at {wsdl})"
        )
    return wsdl


__all__ = [
    "build_rtsp_url",
    "mask_url_credentials",
    "redact_userinfo",
    "resolve_onvif_wsdl_dir",
]
