import pytest
import os
import re

def test_pipeline_bar_markup():
    html_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'index.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()

    # 1. 确保三个点按钮和溢出弹出菜单已被完全移除
    assert 'id="pipeline-more-btn"' not in html, "pipeline-more-btn 应该被删除"
    assert 'id="pipeline-more-menu"' not in html, "pipeline-more-menu 应该被删除"
    assert 'class="pipeline-more-wrap"' not in html, "pipeline-more-wrap 应该被删除"

    # 2. 确保4选1模式开关存在于 index.html 中
    assert 'id="pipeline-selection-checkbox"' in html, "pipeline-selection-checkbox 开关必须存在"
    assert 'id="pipeline-selection-mode-toggle"' in html, "pipeline-selection-mode-toggle 标签容器必须存在"
    assert '4选1' in html

    # 3. 确保生成按钮与开关在同一栏 pipeline-tail 中
    tail_match = re.search(r'<div class="pipeline-tail">(.*?)</div>', html, re.DOTALL)
    assert tail_match is not None, "pipeline-tail 必须存在"
    tail_content = tail_match.group(1)
    assert 'pipeline-selection-checkbox' in tail_content, "开关必须位于 pipeline-tail 栏内"
    assert 'pipeline-next-btn' in tail_content, "下一步/生成按钮必须与开关同在 pipeline-tail 栏内"
