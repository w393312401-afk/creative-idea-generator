import json

from fx_console import FX_CONFIG_SPEC, FxConfigStore, validate_patch
from frame_generator import _fx_image_model
from integrations.google_fx.config import get_runtime_google_fx_image_model
from integrations.google_fx.model_catalog import (
    GOOGLE_FX_IMAGE_MODELS,
    normalize_google_fx_image_model,
)
from integrations.google_fx.services.google_fx_helpers import (
    _matches_model_status,
    _normalize_model_name,
)


EXPECTED_MODELS = (
    "Nano Banana Pro",
    "Nano Banana 2",
    "Nano Banana 2 Lite",
)


def test_google_fx_catalog_replaces_imagen_4_with_nano_banana_2_lite():
    assert GOOGLE_FX_IMAGE_MODELS == EXPECTED_MODELS
    assert FX_CONFIG_SPEC["googleFxImageModel"]["options"] == list(EXPECTED_MODELS)


def test_legacy_image_model_names_migrate_case_insensitively():
    for old_name in ("Imagen 4", "imagen4", "IMAGE 4", "image4"):
        assert normalize_google_fx_image_model(old_name) == "Nano Banana 2 Lite"
        assert validate_patch({"googleFxImageModel": old_name}) == {
            "googleFxImageModel": "Nano Banana 2 Lite"
        }


def test_runtime_and_generation_boundary_normalize_legacy_model(monkeypatch):
    monkeypatch.setenv("GOOGLE_FX_IMAGE_MODEL", "Imagen 4")
    assert get_runtime_google_fx_image_model() == "Nano Banana 2 Lite"
    assert _fx_image_model({"googleFxImageModel": "Image4"}) == "Nano Banana 2 Lite"


def test_flow_model_normalizer_and_status_matching_distinguish_lite():
    assert _normalize_model_name("Nano Banana 2 Lite") == "Nano Banana 2 Lite"
    assert _normalize_model_name("Imagen 4") == "Nano Banana 2 Lite"
    assert _matches_model_status("🍌 Nano Banana 2 Lite\narrow_drop_down", "Nano Banana 2 Lite")
    assert not _matches_model_status("🍌 Nano Banana 2 Lite\narrow_drop_down", "Nano Banana 2")


def test_config_store_persists_legacy_migration(tmp_path):
    config_path = tmp_path / "server_config.json"
    versions_path = tmp_path / "versions.jsonl"
    config = {"googleFxImageModel": "Imagen 4"}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    applied = []
    audited = []
    store = FxConfigStore(
        config,
        config_path,
        versions_path,
        lambda value: applied.append(dict(value)),
        lambda *args, **kwargs: audited.append((args, kwargs)),
    )

    result = store.migrate_deprecated_values()

    assert result["changed"] == {"googleFxImageModel": "Nano Banana 2 Lite"}
    assert config["googleFxImageModel"] == "Nano Banana 2 Lite"
    assert json.loads(config_path.read_text(encoding="utf-8"))["googleFxImageModel"] == "Nano Banana 2 Lite"
    assert applied[-1]["googleFxImageModel"] == "Nano Banana 2 Lite"


def test_frontend_selectors_offer_lite_not_legacy():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    html = (root / "index.html").read_text(encoding="utf-8")
    state = (root / "js" / "state.js").read_text(encoding="utf-8")

    assert '<option value="Nano Banana 2 Lite">' in html
    assert '<option value="Imagen 4">' not in html
    active_catalog = state.split("const FX_IMAGE_MODELS = [", 1)[1].split("];", 1)[0]
    assert "Nano Banana 2 Lite" in active_catalog
    assert "Imagen 4" not in active_catalog
