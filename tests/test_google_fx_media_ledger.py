# -*- coding: utf-8 -*-
"""跨任务媒体台账：一个 UUID 被谁下载过一次，就永远不再是"新结果"。

2026-08-05 事故里，提交前的三条基线（_get_panel_uuids / pre_submit_dom_srcs /
pre_submit_tile_ids）全是实时 DOM 采样，而 Flow 画布是虚拟化的——一小时前的 tile
早就卸载了，三条基线一条都没看见它。台账不碰 DOM，所以不受虚拟化影响。
"""

import inspect

from integrations.google_fx.services import google_fx_image as image_service
from integrations.google_fx.utils import media_ledger


ACCOUNT = "k1a01try"
# 事故当事人：榕树树洞任务 18:31 生成，一小时后被悬崖石屋任务当成自己的 IMG 001。
HISTORIC_UUID = "6b857588-55a6-4d8f-bbfe-6987d8592c43"
FRESH_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(media_ledger, "_STATE_FILE", tmp_path / "fx_media_ledger.json")
    monkeypatch.delenv("SPARK_FX_MEDIA_LEDGER", raising=False)


def test_consumed_uuid_survives_into_the_next_task(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    assert media_ledger.consumed_uuids(ACCOUNT) == set()
    assert media_ledger.record_consumed(
        ACCOUNT, HISTORIC_UUID.upper(), context="img_002.webp"
    ) is True

    consumed = media_ledger.consumed_uuids(ACCOUNT)
    assert HISTORIC_UUID in consumed            # 大小写归一
    assert media_ledger.consumed_uuids("k1c4rryj") == set()   # 账号之间互不干扰

    # 这才是事故当天缺的那一步：下一个任务抓到同一个 UUID 时直接拒收。
    assert image_service._is_blocked_media_candidate(
        f"https://flow-content.google/image/{HISTORIC_UUID}?Expires=1785947495",
        consumed,
    )
    assert not image_service._is_blocked_media_candidate(
        f"https://flow-content.google/image/{FRESH_UUID}", consumed
    )


def test_record_is_idempotent_and_bounded(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(media_ledger, "_MAX_UUIDS_PER_ACCOUNT", 3)

    for _ in range(2):
        media_ledger.record_consumed(ACCOUNT, HISTORIC_UUID)
    assert len(media_ledger.consumed_uuids(ACCOUNT)) == 1

    for i in range(4):
        media_ledger.record_consumed(ACCOUNT, f"0000000{i}-0000-4000-8000-000000000000")
    consumed = media_ledger.consumed_uuids(ACCOUNT)
    assert len(consumed) == 3
    assert HISTORIC_UUID not in consumed          # 超出上限后丢最旧的


def test_ledger_can_be_switched_off(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    media_ledger.record_consumed(ACCOUNT, HISTORIC_UUID)

    monkeypatch.setenv("SPARK_FX_MEDIA_LEDGER", "0")
    assert media_ledger.consumed_uuids(ACCOUNT) == set()
    assert media_ledger.record_consumed(ACCOUNT, FRESH_UUID) is False


def test_unreadable_ledger_degrades_to_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "fx_media_ledger.json").write_text("{not json", encoding="utf-8")
    # 台账坏了只能少一层兜底，不能把生成任务带崩（主防线是画布隔离）。
    assert media_ledger.consumed_uuids(ACCOUNT) == set()
    assert media_ledger.record_consumed(ACCOUNT, FRESH_UUID) is True


def test_missing_account_or_uuid_is_not_recorded(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert media_ledger.record_consumed("", HISTORIC_UUID) is False
    assert media_ledger.record_consumed(ACCOUNT, "") is False
    assert media_ledger.consumed_uuids("") == set()


def test_ledger_is_wired_into_the_pre_submit_blacklist():
    source = inspect.getsource(
        image_service._generate_images_batch_google_fx_single_attempt
    )
    assert "consumed_media_uuids = media_ledger.consumed_uuids(" in source
    assert "| consumed_media_uuids" in source

    # 记账必须在近重复校验通过之后：只被捕获、没能落盘的 UUID 不算被消费，
    # 提前记进去会把同一次生成的重试结果误伤成"历史媒体"。
    assert source.index("_images_are_near_duplicates(") < \
        source.index("media_ledger.record_consumed(")

    # 台账读一次就够，且必须在提交循环之外——每个 prompt 都读一次文件纯属浪费。
    assert source.index("media_ledger.consumed_uuids(") < \
        source.index("for idx, prompt_text in enumerate(prompts):")
