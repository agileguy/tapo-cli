"""Tests for :mod:`tapo_cli.device_info` (shared §3.3.1 / §10.1 helpers)."""

from __future__ import annotations

from tapo_cli.device_info import (
    features_for_model,
    first_str,
    flatten_basic_info,
    format_mac,
    model_supported,
)

# ---------------------------------------------------------------------------
# format_mac
# ---------------------------------------------------------------------------


def test_format_mac_normalizes_dashed_form() -> None:
    assert format_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"


def test_format_mac_normalizes_unseparated_form() -> None:
    assert format_mac("aabbccddeeff") == "AA:BB:CC:DD:EE:FF"


def test_format_mac_preserves_already_normal_form() -> None:
    assert format_mac("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"


def test_format_mac_returns_empty_for_non_string() -> None:
    assert format_mac(None) == ""
    assert format_mac(123) == ""


def test_format_mac_returns_uppercased_garbage_when_unparseable() -> None:
    """An unparseable string is surfaced upper-cased so callers can see what
    the device returned, not silently dropped."""
    assert format_mac("not-a-mac") == "NOT-A-MAC"


# ---------------------------------------------------------------------------
# model_supported / features_for_model
# ---------------------------------------------------------------------------


def test_c225_features_include_dual_lens() -> None:
    feats = features_for_model("C225")
    assert "dual-lens" in feats
    assert "ptz" in feats
    assert "zoom" in feats


def test_c200_features_include_ptz_no_audio() -> None:
    feats = features_for_model("C200")
    assert "ptz" in feats
    assert "audio" not in feats


def test_features_strip_trailing_region_tokens() -> None:
    assert features_for_model("C220 (EU)") == features_for_model("C220")


def test_features_unknown_model_is_empty_list() -> None:
    assert features_for_model("XYZ999") == []
    assert features_for_model("") == []
    assert features_for_model(None) == []


def test_model_supported_for_verified_list() -> None:
    assert model_supported("C200") is True
    assert model_supported("C225") is True
    assert model_supported("D230") is True


def test_model_supported_for_readme_only_models() -> None:
    """Models on the §3.3 verified list but not the §3.3.1 capability matrix."""
    assert model_supported("C310") is True
    assert model_supported("C420S2") is True


def test_model_supported_for_unknown_returns_false() -> None:
    assert model_supported("XYZ999") is False
    assert model_supported("") is False
    assert model_supported(None) is False


# ---------------------------------------------------------------------------
# flatten_basic_info / first_str
# ---------------------------------------------------------------------------


def test_flatten_legacy_shape() -> None:
    payload = {"device_info": {"basic_info": {"device_model": "C200"}}}
    assert flatten_basic_info(payload) == {"device_model": "C200"}


def test_flatten_klap_shape() -> None:
    payload = {"device_info": {"device_model": "C220", "fw_version": "1.0"}}
    assert flatten_basic_info(payload) == {
        "device_model": "C220",
        "fw_version": "1.0",
    }


def test_flatten_already_flat() -> None:
    payload = {"device_model": "C200", "mac": "AA:BB:CC:DD:EE:01"}
    assert flatten_basic_info(payload) == payload


def test_flatten_non_dict_returns_empty() -> None:
    assert flatten_basic_info(None) == {}
    assert flatten_basic_info("nope") == {}


def test_first_str_picks_first_non_empty() -> None:
    info = {"a": "", "b": "value", "c": "other"}
    assert first_str(info, "a", "b", "c") == "value"


def test_first_str_returns_empty_when_all_missing() -> None:
    assert first_str({}, "x", "y") == ""


def test_first_str_skips_non_string_values() -> None:
    info = {"a": 42, "b": "value"}
    assert first_str(info, "a", "b") == "value"
