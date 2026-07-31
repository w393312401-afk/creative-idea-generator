from unittest.mock import patch

import pytest

from integrations.google_fx.services import google_fx_helpers as helpers


def _config_checks(*_args, **_kwargs):
    return {"model": False, "orientation": True, "count": True, "mode": True}


def test_empty_image_model_uses_catalog_default():
    assert helpers._normalize_model_name("", is_video=False) == "Nano Banana 2"


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
