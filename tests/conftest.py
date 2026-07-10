"""Pytest 引导:把仓库根钉到 sys.path。

`python -m pytest` 只会把「调用时的工作目录」放进 sys.path,因此从非仓库根目录运行时,
`import prompt_pipeline` / `import server_common` 等会 ModuleNotFoundError。这里显式把仓库根
(本文件的上一级目录)插到 sys.path 最前,确保导入与工作目录无关。

与 pyproject.toml 的 `pythonpath = "."` 互为双保险(conftest 不依赖 pytest 版本、也能在
直接运行单个测试文件时生效),并为后续把巨石文件拆成包(prompt_pipeline/ 等)保驾护航。
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
