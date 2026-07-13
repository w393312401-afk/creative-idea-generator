"""规范项目标题（=本地母文件夹命名来源）的契约测试。

2026-07-12 用户固定：生成素材落盘的母文件夹统一为纯中文「{载体}改造成{目标}」
句式（如「废弃导弹井改造成地下隐居卧室」）。标题即目录名来源，禁止再出现
「创意度·英文destiny」等混合形态。
"""
import unittest

from prompt_pipeline import _canonical_title, _title_is_canonical
from server_common import _safe_project_name


class TestCanonicalTitle(unittest.TestCase):
    def test_theme_plus_destiny_zh(self):
        self.assertEqual(
            _canonical_title('废弃导弹井', '地下隐居卧室'),
            '废弃导弹井改造成地下隐居卧室',
        )

    def test_theme_already_full_sentence_kept_verbatim(self):
        t = '沼泽坠落客机客舱中部改造成离网避世小屋'
        self.assertEqual(_canonical_title(t, '别的目标'), t)

    def test_ideation_input_str_prefix_stripped(self):
        self.assertEqual(
            _canonical_title('做一个蓝冰冰川洞穴改造成隐居雪境卧室', ''),
            '蓝冰冰川洞穴改造成隐居雪境卧室',
        )

    def test_missing_destiny_falls_back_to_theme(self):
        self.assertEqual(_canonical_title('百年空心橡树', ''), '百年空心橡树')

    def test_english_destiny_rejected(self):
        # LLM 漂移回英文 destiny 时不得混入标题
        self.assertEqual(
            _canonical_title('百年空心橡树', 'off-grid micro-home'),
            '百年空心橡树',
        )

    def test_runon_destiny_trimmed_to_first_clause(self):
        self.assertEqual(
            _canonical_title('废弃水塔', '离网避世小屋，配有滑动床和光纤照明'),
            '废弃水塔改造成离网避世小屋',
        )

    def test_empty_theme(self):
        self.assertEqual(_canonical_title('', ''), '未命名创意')

    def test_canonical_detection(self):
        self.assertTrue(_title_is_canonical('废弃导弹井改造成地下隐居卧室'))
        self.assertTrue(_title_is_canonical('百年空心橡树'))
        # 废弃的混合形态一律不合格 → 续传时会被归一
        self.assertFalse(_title_is_canonical('突破常规·off-grid swamp cabin'))
        self.assertFalse(_title_is_canonical('脑洞大开·artistic hollow oak retreat'))
        self.assertFalse(_title_is_canonical('INSIDE A SCI-FI TREE CAPSULE'))
        self.assertFalse(_title_is_canonical(''))

    def test_title_maps_to_clean_folder_name(self):
        # 端到端映射：规范标题经 _safe_project_name 后就是红框形式的目录名
        title = _canonical_title('废弃导弹井', '地下隐居卧室')
        self.assertEqual(_safe_project_name(title), '废弃导弹井改造成地下隐居卧室')


if __name__ == '__main__':
    unittest.main()
