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
