"""omni 记号禁用的覆盖回归（2026-08-05）。

背景：`omni-output-templates.md` §Notation Ban 说的是 "model-facing prompt bodies"——
两侧都算。实现上却只有 VIDEO 有门禁（_stray_digits）与确定性改写（_digits_to_words），
IMAGE 一侧既不改写也不校验；而 base 的锚点重述句恰恰会往 IMAGE 里写
`holding 45 percent of frame height`。契约的一半从来没人执行过。

叠加第二个洞：_digits_to_words 原来只认两位数字、且词表只到 twenty，于是二十以上的
计数与**全部**画高比例都从改写里漏了过去。

修复的关键约束：比例数字不能一改了之。base 的 SCUP 门禁
（check_anchor_scale_lock / check_worker_scale_lock）要从提示词里把比例解析回来做
漂移比对。所幸 _PERCENT_NEAR_PATTERN 本来就接受词形（`forty-five percent`），所以
拼写出来两边都成立——本测试把这条兼容性钉死，它是"记号禁用"与"漂移门禁"能同时
生效的唯一原因。
"""
import pytest

import prompt_pipeline as pp
from prompt_pipeline.composers import omni


class TestIntegerToWords:
    @pytest.mark.parametrize('n,expected', [
        (0, 'zero'), (3, 'three'), (20, 'twenty'),
        (30, 'thirty'), (45, 'forty-five'), (65, 'sixty-five'),
        (99, 'ninety-nine'), (100, 'one hundred'),
    ])
    def test_covers_zero_through_one_hundred(self, n, expected):
        assert omni._integer_to_words(n) == expected

    def test_out_of_range_returns_none_instead_of_guessing(self):
        """超出范围不硬改——改错一个数比留一个记号瑕疵更糟，交给门禁报。"""
        assert omni._integer_to_words(101) is None
        assert omni._integer_to_words(2400) is None


class TestScalePhrasesStayParseableByTheDriftGates:
    """这是整个修复的承重墙：词形比例必须仍能被 SCUP 解析回同一个数。"""

    @pytest.mark.parametrize('n', [15, 20, 25, 35, 45, 55, 65, 75, 85, 95])
    def test_word_form_scales_round_trip_through_the_percent_parser(self, n):
        phrase = omni._integer_to_words(n)
        assert pp._parse_percent_token(phrase) == n, (
            f'{n} -> {phrase!r} 解析不回原值，SCUP 的比例锁会静默失效')

    @pytest.mark.parametrize('n', [15, 45, 65, 95])
    def test_word_form_scales_are_still_matched_in_a_sentence(self, n):
        sentence = (f'the brick chimney at Grid A2 holding {omni._integer_to_words(n)} '
                    f'percent of frame height')
        m = pp._PERCENT_NEAR_PATTERN.search(sentence.lower())
        assert m, f'{n} 的词形在句子里匹配不到，check_anchor_scale_lock 会看不见它'
        assert pp._parse_percent_token(m.group(1)) == n

    def test_anchor_scale_lock_still_catches_drift_written_in_words(self):
        """词形化之后，漂移仍要被抓到——否则等于用记号禁用换掉了一个 P0。"""
        packet = {'primary_landmarks': [{'name': 'brick chimney', 'z_depth_scale': 45}]}
        drifted = ('A static wide shot. The brick chimney holds sixty-five percent of '
                   'frame height against the overcast sky.')
        errors = pp.check_anchor_scale_lock(drifted, packet, family='exterior')
        assert errors and 'brick chimney' in errors[0]

    def test_anchor_scale_lock_passes_when_the_word_form_matches(self):
        packet = {'primary_landmarks': [{'name': 'brick chimney', 'z_depth_scale': 45}]}
        ok = ('A static wide shot. The brick chimney holds forty-five percent of '
              'frame height against the overcast sky.')
        assert pp.check_anchor_scale_lock(ok, packet, family='exterior') == []


class TestDigitsToWords:
    def test_scale_percentages_are_spelled_out(self):
        out = omni._digits_to_words('the chimney holding 45 percent of frame height')
        assert 'forty-five percent' in out
        assert '45' not in out

    def test_counts_above_twenty_are_spelled_out(self):
        """此前词表只到 twenty，二十一以上原样穿过。"""
        assert 'thirty-two roof battens' in omni._digits_to_words('32 roof battens')

    def test_image_references_are_left_alone(self):
        """IMAGE 编号是锚点引用，不是计数。"""
        assert omni._digits_to_words('matching IMAGE 3 exactly') == 'matching IMAGE 3 exactly'

    def test_timeline_seconds_are_left_alone(self):
        """时间码豁免：切点必须钉在秒上，见 omni-output-templates.md §Timecode Exemption。"""
        text = ('Cut this ten-second clip on these marks and hold no other cuts — an '
                'establishing long shot from 0.0 to 1.6, and a wide outro shot from 8.3 '
                'to 10.0 seconds.')
        assert omni._digits_to_words(text) == text

    def test_measurements_glued_to_units_are_left_alone(self):
        assert omni._digits_to_words('a 14mm lens') == 'a 14mm lens'


class TestImageSideGate:
    def test_stray_digits_in_image_are_flagged(self):
        errs = omni.omni_image_violations('three beams and 45 percent of frame height')
        assert errs and errs[0].startswith(omni.OMNI_IMAGE_STYLE_PREFIX)
        assert '45' in errs[0]

    def test_clean_image_prompt_passes(self):
        assert omni.omni_image_violations(
            'the brick chimney holding forty-five percent of frame height') == []

    def test_grid_cells_do_not_trip_the_gate(self):
        """Grid 是 base 的 SCUP 锚点，omni 剥不掉——登记在案的冲突，不重复报成数字违规。

        见 contract-registry.json 的 omni-grid-notation-ban。
        """
        assert omni.omni_image_violations(
            'the brick chimney at Grid A2 holding forty-five percent of frame height') == []

    def test_image_reference_numbers_do_not_trip_the_gate(self):
        assert omni.omni_image_violations('identical to IMAGE 2 in every respect') == []

    @pytest.mark.parametrize('text', [
        'ultra-wide 14mm lens feel, camera height 1.6m',
        'a static tripod shot, wide-angle 18mm lens feel',
        'shot at 1.6m with a 24mm lens',
    ])
    def test_measurements_glued_to_units_do_not_trip_the_gate(self, text):
        """检查器必须和 _digits_to_words 放行同一批贴单位写法（14mm / 1.6m）。

        2026-08-06：修复稿写成 `\\d+(?:\\.\\d+)?(?![A-Za-z])`，被正则回溯绕开——
        "14mm" 先试 "14"（后面是 m，预查失败），退成 "1"（后面是 4，不是字母，预查
        通过），照样报出一个不存在的残留数字 "1"。实测 35/35 条真实 IMAGE 提示词
        都因为 camera_dna 里的镜头焦距被判违规，而确定性修复器根本不会去动它，
        于是回炉永远修不好。
        """
        assert omni._stray_digits_image(text) == []
        assert omni.omni_image_violations(text) == []

    def test_real_digits_next_to_a_glued_measurement_are_still_flagged(self):
        """放行贴单位写法不等于放行整句：同句里的裸计数必须照样报出来。"""
        assert omni._stray_digits_image('a 14mm lens above 3 stacked crates') == ['3']
