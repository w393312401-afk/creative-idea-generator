"""视频提示词优化门读得到「提示词链路」这项设置吗（2026-08-30）。

此前读不到。这道门里写的是：

    profile = manifest_data.get('profile') or config.get('profile') or 'base'

而 `manifest['profile']` 与 `config['profile']` **全仓库没有任何写入方**——前端与
server_config.json 用的都是 `skillProfile`，解析口径唯一长在
`server_common.active_skill_profile`（显式值 > videoModel 推断）。于是它恒为 'base'：
用户在配置中心把「提示词链路」切到 Omni / Miniature，这道门一次都没读到过。
与 qaGateLevel / imageEditTransport / skillProfile 栽过的是同一个坑（见
js/gate_settings.js 开头那段），失效方式也一样：不报错，只是永远按默认档跑。

这一组钉住三件事：
  ① 设置真的能到达这道门（显式值与 auto 推断都要能到）；
  ② manifest 上的显式指定优先于本次请求的配置；
  ③ 微缩判定不退化成"只看 profile"——正文证据是第二个真实来源，不是兜底。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server_common
from prompt_pipeline.video_optimizer import (
    _is_miniature_job,
    resolve_optimizer_profile,
)


class TestProfileReachesTheGate(unittest.TestCase):
    def test_an_explicit_pipeline_choice_arrives(self):
        for chosen in ('base', 'omni', 'miniature'):
            with self.subTest(chosen):
                self.assertEqual(
                    resolve_optimizer_profile({'skillProfile': chosen}), chosen)

    def test_auto_follows_the_video_model_like_everywhere_else(self):
        """auto 档的推断必须与 active_skill_profile 同一份规则表，不能在这里另猜一次。"""
        self.assertEqual(
            resolve_optimizer_profile({'skillProfile': 'auto', 'videoModel': 'Omni Flash'}),
            'omni')
        self.assertEqual(
            resolve_optimizer_profile({'skillProfile': 'auto', 'videoModel': 'Veo 3.1'}),
            'base')

    def test_it_agrees_with_the_composer_side_resolver(self):
        """合成侧按 active_skill_profile 分派 composer。这道门若自己另有一套口径，
        就会出现「用 Omni 语法写的提示词、按 Veo 规则优化」——同一件事两个真相源。"""
        for cfg in ({'skillProfile': 'omni'},
                    {'skillProfile': 'auto', 'videoModel': 'Omni Flash'},
                    {'skillProfile': 'auto', 'videoModel': 'Veo 3.1'}):
            with self.subTest(str(cfg)):
                self.assertEqual(resolve_optimizer_profile(cfg),
                                 server_common.active_skill_profile(cfg))

    def test_the_manifest_can_pin_a_profile_for_one_job(self):
        self.assertEqual(
            resolve_optimizer_profile({'skillProfile': 'omni'}, {'profile': 'miniature'}),
            'miniature')

    def test_a_dead_key_is_no_longer_consulted(self):
        """`config['profile']` 是这次要修的那个不存在的键。它若还能左右结果，
        说明旧判据还在某处活着。"""
        self.assertEqual(
            resolve_optimizer_profile({'skillProfile': 'omni', 'profile': 'miniature'}),
            'omni')


class TestMiniatureVerdictKeepsBothSources(unittest.TestCase):
    """微缩判定有意保留两个来源，别在"修好 profile"之后把正文证据删掉。"""

    def test_an_explicit_miniature_profile_needs_no_corroboration(self):
        self.assertTrue(_is_miniature_job('miniature', 'a lone worker fits the joist'))

    def test_a_miniature_body_still_counts_under_another_profile(self):
        """合成侧判微缩走的是题材证据（reverse.detect_miniature_scale），不是 profile：
        一单微缩片配着 Omni Flash 完全合法。只认 profile 的话，这段巨手微距片会被按
        真实尺度优化，往里塞一个 1.78m 的工人。"""
        self.assertTrue(_is_miniature_job('omni', 'a macro diorama shot with giant hands'))
        self.assertTrue(_is_miniature_job('base', 'the miniature residents watch the work'))

    def test_a_full_scale_body_under_a_full_scale_profile_stays_full_scale(self):
        self.assertFalse(_is_miniature_job('omni', 'a lone worker fits the joist'))
        self.assertFalse(_is_miniature_job(None, 'a lone worker fits the joist'))


if __name__ == '__main__':
    unittest.main()
