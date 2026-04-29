"""Tests for the §3.3.1 per-feature capability matrix (Phase 2, S4)."""

from __future__ import annotations

import pytest

from tapo_cli.device_info import (
    MODEL_CAPABILITIES,
    capabilities_for_model,
    feature_supported,
    models_supporting,
    ptz_mode_for_model,
)

# ---------------------------------------------------------------------------
# Matrix integrity
# ---------------------------------------------------------------------------


def test_matrix_lists_c200_with_step_ptz() -> None:
    caps = MODEL_CAPABILITIES["C200"]
    assert caps["ptz_mode"] == "step"


def test_matrix_lists_c225_with_continuous_ptz() -> None:
    caps = MODEL_CAPABILITIES["C225"]
    assert caps["ptz_mode"] == "continuous"


def test_matrix_lists_c200_preset_yes() -> None:
    assert MODEL_CAPABILITIES["C200"]["preset"] is True


def test_matrix_lists_c200_alarm_yes_trigger_no() -> None:
    """C200 has alarm config but no manual-trigger pytapo verb."""
    caps = MODEL_CAPABILITIES["C200"]
    assert caps["alarm"] is True
    assert caps["alarm_trigger"] is False


def test_matrix_lists_c200_audio_speaker_yes_tts_no() -> None:
    caps = MODEL_CAPABILITIES["C200"]
    assert caps["audio_speaker"] is True
    assert caps["audio_tts"] is False


def test_matrix_lists_c200_osd_text_no_timestamp_yes() -> None:
    caps = MODEL_CAPABILITIES["C200"]
    assert caps["osd_text"] is False
    assert caps["osd_timestamp"] is True


def test_matrix_lists_c520ws_with_tts_yes() -> None:
    """C520WS is one of the v1 TTS-supporting models per §3.3.1."""
    assert MODEL_CAPABILITIES["C520WS"]["audio_tts"] is True


def test_matrix_lists_c225_with_zoom_yes() -> None:
    """C225 is the dual-lens model with a real zoom motor."""
    assert MODEL_CAPABILITIES["C225"]["zoom"] is True


def test_matrix_lists_tc55_with_no_audio() -> None:
    """TC55 indoor camera has neither mic nor speaker."""
    caps = MODEL_CAPABILITIES["TC55"]
    assert caps["audio_mic"] is False
    assert caps["audio_speaker"] is False


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def test_capabilities_for_model_normalizes_region_token() -> None:
    """``"C220 (EU)"`` should match the ``C220`` family entry."""
    caps = capabilities_for_model("C220 (EU)")
    assert caps["ptz_mode"] == "step"


def test_capabilities_for_model_unknown_returns_empty() -> None:
    assert capabilities_for_model("X9999") == {}


def test_capabilities_for_model_none_returns_empty() -> None:
    assert capabilities_for_model(None) == {}


def test_feature_supported_c200_alarm_true() -> None:
    assert feature_supported("C200", "alarm") is True


def test_feature_supported_c200_audio_tts_false() -> None:
    assert feature_supported("C200", "audio_tts") is False


def test_feature_supported_c200_osd_text_false() -> None:
    assert feature_supported("C200", "osd_text") is False


def test_feature_supported_unknown_model_fails_closed() -> None:
    assert feature_supported("XYZ999", "alarm") is False


def test_feature_supported_none_model_fails_closed() -> None:
    assert feature_supported(None, "alarm") is False


def test_feature_supported_unknown_feature_raises() -> None:
    with pytest.raises(KeyError):
        feature_supported("C200", "garbage_feature")


def test_ptz_mode_for_model_c200_step() -> None:
    assert ptz_mode_for_model("C200") == "step"


def test_ptz_mode_for_model_c225_continuous() -> None:
    assert ptz_mode_for_model("C225") == "continuous"


def test_ptz_mode_for_model_c100_none() -> None:
    """C100 has no PTZ motors."""
    assert ptz_mode_for_model("C100") == "none"


def test_ptz_mode_for_model_unknown_none() -> None:
    assert ptz_mode_for_model("xyz") == "none"


def test_models_supporting_audio_tts_lists_known_supporters() -> None:
    supporters = models_supporting("audio_tts")
    assert "C520WS" in supporters
    assert "C530WS" in supporters
    # C200 must NOT appear.
    assert "C200" not in supporters
    # Sorted ascending.
    assert supporters == sorted(supporters)


def test_models_supporting_ptz_mode_lists_step_and_continuous() -> None:
    supporters = models_supporting("ptz_mode")
    assert "C200" in supporters  # step
    assert "C225" in supporters  # continuous
    # Non-PTZ family must NOT appear.
    assert "C100" not in supporters
    assert "TC55" not in supporters


def test_models_supporting_unknown_feature_raises() -> None:
    with pytest.raises(KeyError):
        models_supporting("nonsense")


# ---------------------------------------------------------------------------
# Hint-text invariant — exit-5 hints reference §3.3.1 supporters
# ---------------------------------------------------------------------------


def test_hint_text_for_audio_tts_lists_supported_models() -> None:
    """The capability gate's hint MUST list at least one supporting model."""
    from tapo_cli.errors import UnsupportedFeatureError
    from tapo_cli.verbs._capability import require_feature

    with pytest.raises(UnsupportedFeatureError) as ei:
        require_feature(
            model="C200",
            target="office",
            feature="audio_tts",
            verb_name="audio tts",
        )
    err = ei.value
    assert err.exit_code == 5
    assert err.hint is not None
    assert "C520WS" in err.hint
    assert "C200" not in err.hint  # don't list the unsupported requester


def test_hint_text_for_osd_text_lists_supported_models() -> None:
    from tapo_cli.errors import UnsupportedFeatureError
    from tapo_cli.verbs._capability import require_feature

    with pytest.raises(UnsupportedFeatureError) as ei:
        require_feature(
            model="C200",
            target="office",
            feature="osd_text",
            verb_name="osd set",
        )
    err = ei.value
    assert err.exit_code == 5
    assert "C225" in (err.hint or "")  # C225 is one of the osd_text supporters


def test_hint_text_for_ptz_lists_supported_models() -> None:
    from tapo_cli.errors import UnsupportedFeatureError
    from tapo_cli.verbs._capability import require_ptz

    # C100 has no PTZ motors → exit 5.
    with pytest.raises(UnsupportedFeatureError) as ei:
        require_ptz(model="C100", target="office")
    err = ei.value
    assert err.exit_code == 5
    assert "C200" in (err.hint or "")  # C200 is a PTZ supporter (step mode)


def test_hint_text_for_zoom_excludes_non_zoom_ptz_models() -> None:
    """Zoom requires the ``zoom`` flag, not just any PTZ. C200 has step
    PTZ but no zoom motor — gate fires."""
    from tapo_cli.errors import UnsupportedFeatureError
    from tapo_cli.verbs._capability import require_ptz

    with pytest.raises(UnsupportedFeatureError) as ei:
        require_ptz(model="C200", target="office", require_zoom=True)
    err = ei.value
    assert err.exit_code == 5
    assert "C225" in (err.hint or "")  # C225 has zoom motor
