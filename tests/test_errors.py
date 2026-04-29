"""Tests for the structured-error envelope and exit-code constants."""

from __future__ import annotations

import json

import pytest

from tapo_cli import errors


def test_every_exit_code_constant_exists() -> None:
    """SRD §11.1: every documented exit code is exposed as a constant."""
    expected = {
        "EXIT_SUCCESS": 0,
        "EXIT_DEVICE_ERROR": 1,
        "EXIT_AUTH_ERROR": 2,
        "EXIT_NETWORK_ERROR": 3,
        "EXIT_NOT_FOUND": 4,
        "EXIT_UNSUPPORTED": 5,
        "EXIT_CONFIG_ERROR": 6,
        "EXIT_PARTIAL_FAILURE": 7,
        "EXIT_USAGE_ERROR": 64,
        "EXIT_SIGINT": 130,
        "EXIT_SIGTERM": 143,
    }
    for name, value in expected.items():
        assert getattr(errors, name) == value, name


def test_error_names_enum_is_closed() -> None:
    """SRD §11.2: error name set is a closed enum."""
    assert frozenset(
        {
            "device_error",
            "auth_failed",
            "network_error",
            "not_found",
            "unsupported_feature",
            "config_error",
            "partial_failure",
            "usage_error",
            "interrupted",
        }
    ) == errors.ERROR_NAMES


def test_structured_error_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown structured error name"):
        errors.StructuredError(error="totally_made_up", exit_code=1, message="x")


def test_structured_error_envelope_shape() -> None:
    """SRD §11.2: envelope keys match the spec example."""
    err = errors.StructuredError(
        error="auth_failed",
        exit_code=2,
        target="front-door",
        credential="camera_account",
        message="RTSP auth rejected; check camera_account_file",
        hint="Create a camera account in the Tapo app",
    )
    payload = err.to_dict()
    assert payload == {
        "error": "auth_failed",
        "exit_code": 2,
        "message": "RTSP auth rejected; check camera_account_file",
        "target": "front-door",
        "hint": "Create a camera account in the Tapo app",
        "credential": "camera_account",
    }


def test_structured_error_omits_null_optional_fields() -> None:
    err = errors.StructuredError(error="device_error", exit_code=1, message="boom")
    payload = err.to_dict()
    assert payload == {"error": "device_error", "exit_code": 1, "message": "boom"}
    assert "target" not in payload
    assert "hint" not in payload
    assert "mechanism" not in payload
    assert "credential" not in payload
    assert "details" not in payload


def test_structured_error_round_trip_via_to_json() -> None:
    err = errors.StructuredError(
        error="config_error",
        exit_code=6,
        message="bad TOML",
        target="cfg",
        hint="Fix the file",
        extra={"path": "/tmp/x"},
    )
    parsed = json.loads(err.to_json())
    re = errors.StructuredError.from_dict(parsed)
    assert re.error == err.error
    assert re.exit_code == err.exit_code
    assert re.message == err.message
    assert re.target == err.target
    assert re.hint == err.hint
    assert re.extra == err.extra


def test_exception_to_structured_carries_all_fields() -> None:
    exc = errors.AuthError(
        "permission denied",
        target="cam-1",
        hint="chmod 600",
        credential="camera_account",
        mechanism="filesystem",
        extra={"mode": "0644"},
    )
    s = exc.to_structured()
    assert s.error == "auth_failed"
    assert s.exit_code == errors.EXIT_AUTH_ERROR
    assert s.target == "cam-1"
    assert s.credential == "camera_account"
    assert s.mechanism == "filesystem"
    assert s.extra == {"mode": "0644"}


@pytest.mark.parametrize(
    "exc_cls,expected_code,expected_name",
    [
        (errors.DeviceError, 1, "device_error"),
        (errors.AuthError, 2, "auth_failed"),
        (errors.NetworkError, 3, "network_error"),
        (errors.NotFoundError, 4, "not_found"),
        (errors.UnsupportedFeatureError, 5, "unsupported_feature"),
        (errors.ConfigError, 6, "config_error"),
        (errors.PartialFailureError, 7, "partial_failure"),
        (errors.UsageError, 64, "usage_error"),
        (errors.TapoInterruptError, 130, "interrupted"),
    ],
)
def test_exception_hierarchy_maps_to_exit_codes(
    exc_cls: type[errors.TapoCliError], expected_code: int, expected_name: str
) -> None:
    exc = exc_cls("x")
    assert exc.exit_code == expected_code
    assert exc.error_name == expected_name
    assert exc.to_structured().exit_code == expected_code
    assert exc.to_structured().error == expected_name
