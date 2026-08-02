from unittest.mock import patch

import pytest

from integrations.google_fx.services import google_fx_helpers as helpers


def _config_checks(*_args, **_kwargs):
    return {"model": False, "orientation": True, "count": True, "mode": True}


def test_empty_image_model_uses_catalog_default():
    assert helpers._normalize_model_name("", is_video=False) == "Nano Banana 2"


@pytest.mark.parametrize("configured, status", [
    ("1x", "Nano Banana 2 crop_9_16 x1"),
    ("x1", "Nano Banana 2 crop_9_16 1x"),
])
def test_single_image_count_accepts_both_flow_ui_spellings(configured, status):
    """Flow's x1 UI must not be mistaken for x2 or left unchanged at two outputs."""
    number, aliases = helpers._generation_count_aliases(configured)
    assert number == "1"
    assert aliases == {"x1", "1x"}
    assert helpers._matches_generation_count(status, configured)
    assert not helpers._matches_generation_count("Nano Banana 2 crop_9_16 x2", configured)


def test_generation_stops_when_model_click_did_not_change_selection():
    with (
        patch.object(helpers, "find_fx_config_button", return_value=(object(), "Image Portrait 1x")),
        patch.object(helpers, "check_fx_config", side_effect=_config_checks),
        patch.object(
            helpers,
            "fix_fx_config",
            return_value={
                "clicked_keys": ["model"],
                "resolved_keys": [],
                "resolved_model_text": "",
            },
        ),
    ):
        with pytest.raises(RuntimeError, match="model"):
            helpers._verify_and_fix_fx_config(
                object(),
                model="Nano Banana 2",
                ratio="9:16",
                want_video=False,
                context_label="image generation",
            )


def test_generation_continues_after_post_click_model_confirmation():
    with (
        patch.object(helpers, "find_fx_config_button", return_value=(object(), "Image Portrait 1x")),
        patch.object(helpers, "check_fx_config", side_effect=_config_checks),
        patch.object(
            helpers,
            "fix_fx_config",
            return_value={
                "clicked_keys": ["model"],
                "resolved_keys": [],
                "resolved_model_text": "Nano Banana 2",
            },
        ),
    ):
        assert helpers._verify_and_fix_fx_config(
            object(),
            model="Nano Banana 2",
            ratio="9:16",
            want_video=False,
            context_label="image generation",
        ) == "9:16"


def test_video_duration_verification_passes_duration():
    def _duration_checks(*_args, **kwargs):
        assert kwargs.get("duration") == "10"
        return {"model": True, "orientation": True, "count": True, "mode": True, "duration": True}

    with (
        patch.object(helpers, "find_fx_config_button", return_value=(object(), "Omni Flash 9:16 1x 10s")),
        patch.object(helpers, "check_fx_config", side_effect=_duration_checks),
    ):
        assert helpers._verify_and_fix_fx_config(
            object(),
            model="Omni Flash",
            ratio="9:16",
            want_video=True,
            context_label="video generation",
            duration="10",
        ) == "9:16"
