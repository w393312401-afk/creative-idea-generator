"""Pytest 引导:把仓库根钉到 sys.path。

`python -m pytest` 只会把「调用时的工作目录」放进 sys.path,因此从非仓库根目录运行时,
`import prompt_pipeline` / `import server_common` 等会 ModuleNotFoundError。这里显式把仓库根
(本文件的上一级目录)插到 sys.path 最前,确保导入与工作目录无关。

与 pyproject.toml 的 `pythonpath = "."` 互为双保险(conftest 不依赖 pytest 版本、也能在
直接运行单个测试文件时生效),并为后续把巨石文件拆成包(prompt_pipeline/ 等)保驾护航。
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture(autouse=True)
def _isolate_qa_gate_level(monkeypatch):
    """把 qaGateLevel 的两个环境来源从所有测试里剥离：开发机 server_config.json
    配了 lenient/off、或导出了 SPARK_QA_GATE_LEVEL 时，视觉门会提前短路返回，
    大量以 config={} 调门的既有测试（strictGates fail-closed、锚点门 PASS/FAIL
    契约等）会静默反转。测试要什么档位，一律在自己的 config dict 里显式声明。"""
    import server_common
    monkeypatch.delenv('SPARK_QA_GATE_LEVEL', raising=False)
    monkeypatch.delitem(server_common.SERVER_CONFIG, 'qaGateLevel', raising=False)


@pytest.fixture(autouse=True)
def _isolate_repo_root_state_files(tmp_path_factory, monkeypatch):
    """把仓库根的三个「本机运行时状态文件」在所有测试里重定向到临时目录。

    这些常量是模块导入期就算好的绝对路径（基于 _REPO_ROOT），因此 monkeypatch.chdir
    之类的隔离手法穿不透它们：
      - search_snippet_cache.json —— 联网摘要 6 小时缓存
      - trend_refs.json / trend_refs.archive.json —— 联网参考案例库
      - compose_checkpoints.json —— 提示词合成断点续传存档

    不隔离会双向出事：真实缓存/案例库被喂进测试（2026-07-25 实测
    test_trend_ref_compose_usage 里"联网通道应当拿不到东西"的前提被真实缓存击穿），
    以及测试反过来把桩数据写进开发者的真实案例库。多数测试各自 patch 过其中一两个，
    但那是逐个测试的自觉，漏一个就是一个隐蔽 flake —— 这里统一兜底。
    需要断言具体路径行为的测试仍可自行 patch 覆盖本 fixture。"""
    import prompt_pipeline
    state_dir = tmp_path_factory.mktemp('repo_state')
    for attr, filename in (
        ('SEARCH_SNIPPET_CACHE_PATH', 'search_snippet_cache.json'),
        ('TREND_REFS_PATH', 'trend_refs.json'),
        ('TREND_REFS_ARCHIVE_PATH', 'trend_refs.archive.json'),
        ('COMPOSE_CHECKPOINT_PATH', 'compose_checkpoints.json'),
    ):
        monkeypatch.setattr(prompt_pipeline, attr, str(state_dir / filename), raising=True)


@pytest.fixture(autouse=True)
def _isolate_used_topic_ledger(tmp_path_factory, monkeypatch):
    """把可写的历史选题台账（runtime/used-topic-ledger.md）也重定向到临时目录。

    同上一条 fixture 的理由，只是方向更危险：这份台账是**会被追加写**的去重记忆
    （每选中一个选题落一行）。不隔离的话，任何一个跑到 append_to_used_topic_ledger
    的测试都会往开发者的真实台账里塞桩数据，之后真实激发会把这些桩当成"已用选题"
    永久回避掉——一次静默的创意面缩窄，而且没人会想到去台账里翻。
    需要断言台账路径行为的测试（test_skill_profiles.py）自行 patch 覆盖本 fixture。"""
    import server_common
    path = tmp_path_factory.mktemp('ledger') / 'used-topic-ledger.md'
    # 先建成空文件：否则首次读取会从真实技能包里整份播种，把开发机上几百行真实
    # 选题喂进 run_ideate 的 system prompt——大量 ideate 测试（此前靠 patch
    # load_reference_file 拿到空台账）会因为这些真实内容而断言反转。
    path.write_text('', encoding='utf-8')
    monkeypatch.setattr(server_common, 'USED_TOPIC_LEDGER_FILE', str(path))
