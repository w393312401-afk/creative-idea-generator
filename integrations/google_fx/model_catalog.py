"""Google Flow 当前图片模型目录与旧值迁移。"""

GOOGLE_FX_IMAGE_MODELS = (
    "Nano Banana Pro",
    "Nano Banana 2",
    "Nano Banana 2 Lite",
)
DEFAULT_GOOGLE_FX_IMAGE_MODEL = "Nano Banana 2"

# 旧配置仍可能来自 server_config.json、浏览器 localStorage 或配置版本栈。
LEGACY_GOOGLE_FX_IMAGE_MODELS = {
    "imagen 4": "Nano Banana 2 Lite",
    "imagen4": "Nano Banana 2 Lite",
    "image 4": "Nano Banana 2 Lite",
    "image4": "Nano Banana 2 Lite",
}


def normalize_google_fx_image_model(value, fallback=DEFAULT_GOOGLE_FX_IMAGE_MODEL):
    text = str(value or "").strip()
    if not text:
        return fallback
    for model in GOOGLE_FX_IMAGE_MODELS:
        if text.casefold() == model.casefold():
            return model
    migrated = LEGACY_GOOGLE_FX_IMAGE_MODELS.get(text.casefold())
    return migrated if migrated is not None else fallback


def is_legacy_google_fx_image_model(value):
    return str(value or "").strip().casefold() in LEGACY_GOOGLE_FX_IMAGE_MODELS
