"""逐帧逐字锁的哨兵（2026-08-30）。

「把一个 packet 字段钉进每一帧、要求逐字复述」是这条链上杀伤力最大的一类约束，也是这个
仓库反复栽的形状：字段一旦是**会随时间变的读数**而不是**时不变的常量**，逐字复述就会把
一个过期状态复读到序列末尾，而且没有任何东西会响——校验器照报绿。

这一组不判对错，只做一件事：**逼人登记**。新加一条逐帧逐字锁而不在
`_PER_FRAME_VERBATIM_LOCKS` 里写清「它锁的字段为什么是时不变的」，测试直接失败。

刻意只扫 `_beat_contract` 的函数体，不扫全文件：源码里 "verbatim" 有五十来处，绝大多数是
改写时保留原文的指令，不是字段锁——全量扫描会天天误报，哨兵响成背景噪音就等于没有。
"""
import re
import unittest

import prompt_pipeline as pp
from _source_reader import top_level_function_source


def _beat_contract_source():
    # 不用 inspect.getsource：它靠进程级 linecache，与后台线程交错时会取回文件里另一处的
    # 单行，把「找不到源码」伪装成「一处锁都没有」。见 tests/_source_reader.py。
    return top_level_function_source(pp._beat_contract)


class TestEveryPerFrameLockIsRegistered(unittest.TestCase):

    def test_each_registered_marker_still_exists(self):
        src = _beat_contract_source()
        for lock in pp._PER_FRAME_VERBATIM_LOCKS:
            self.assertIn(lock['marker'], src,
                          f"登记表里的锁 {lock['packet_field']} 在 _beat_contract 里找不到了——"
                          f"契约措辞改了就同步改登记表，别让表和代码对不上")

    def test_no_unregistered_lock_sneaks_in(self):
        """新加一条逐帧逐字锁必须登记。这一条失败时不要改数字了事——去登记表里
        写清新锁的字段是什么、为什么它是时不变的。"""
        src = _beat_contract_source()
        found = len(re.findall(r'word for word', src, re.I))
        self.assertEqual(
            found, len(pp._PER_FRAME_VERBATIM_LOCKS),
            f"_beat_contract 里有 {found} 处逐帧逐字锁，登记表里只有 "
            f"{len(pp._PER_FRAME_VERBATIM_LOCKS)} 条")

    def test_every_lock_states_why_its_field_is_time_invariant(self):
        for lock in pp._PER_FRAME_VERBATIM_LOCKS:
            self.assertTrue(lock.get('time_invariant'),
                            f"{lock['packet_field']} 被标成时变，却仍在逐帧逐字复述——"
                            f"要么改成逐拍绑定，要么说明它为什么其实不变")
            self.assertGreater(len(lock.get('why', '')), 40,
                               f"{lock['packet_field']} 缺少「为什么时不变」的说明")


class TestTheCameraLockCarriesNoFraming(unittest.TestCase):
    """机位句是登记表里第一条锁。它只有在**不含逐拍构图**时才是时不变的——
    构图焊进去，句子就会跨拍过期，还会把逐拍的 fix_subject_placement 判成「已写过」跳过。
    2026-08-30 实测一单 17 拍里有 10 拍的真实构图读数就是这么被自己挡掉的。"""

    SETUP = {'id': 'SETUP_1', 'space': 'primary', 'angle': 'high_angle', 'bearing': 'front',
             'lens': 'macro', 'scale': 'close',
             'placement': 'the hut sits centred, filling four fifths of frame height',
             'beats': [1, 9]}

    def test_the_setup_sentence_describes_only_where_the_camera_stands(self):
        prose = pp.observed_camera_setup_prose(self.SETUP)
        self.assertIn('the camera stands', prose)
        self.assertNotIn('composition:', prose)
        self.assertNotIn('centred', prose, '逐拍构图不该出现在跨拍复用的机位句里')
        self.assertNotIn('four fifths', prose)

    def test_the_framing_is_still_handed_to_the_packet_call_separately(self):
        """摘掉不等于丢掉：构图仍要发给 packet 生成调用，作为 z_depth_scale 与地平线
        钉位的尺寸依据，只是明确标成「不许抄进机位句」。"""
        brief = {'beat_outline': [
            {'text': 'beat 1', 'space': 'yard', 'camera_angle': 'high_angle',
             'camera_bearing': 'front', 'lens_feel': 'macro', 'shot_scale': 'close',
             'placement': 'the hut sits centred, filling four fifths of frame height'},
        ]}
        rule = pp.observed_camera_angle_packet_rule(brief)
        self.assertIn('OBSERVED FRAMING PER SETUP', rule)
        self.assertIn('NEVER copy any of this into a camera sentence', rule)
        self.assertIn('the hut sits centred', rule)

    def test_a_setup_line_itself_no_longer_carries_the_framing(self):
        brief = {'beat_outline': [
            {'text': 'beat 1', 'space': 'yard', 'camera_angle': 'high_angle',
             'camera_bearing': 'front', 'lens_feel': 'macro',
             'placement': 'the hut sits centred'},
        ]}
        rule = pp.observed_camera_angle_packet_rule(brief)
        setup_line = next(l for l in rule.split('\n') if l.startswith('- "SETUP_1"'))
        self.assertNotIn('centred', setup_line)


if __name__ == '__main__':
    unittest.main()
