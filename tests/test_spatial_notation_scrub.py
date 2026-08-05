"""网格记号 / 百分比数字不得进入最终提示词正文。

事故来源（2026-07-30 那一单，library.json 的 frameRun）：
  IMAGE 4 的判废原因原文是「画面中出现了多处异常的字母叠加渲染标记（A、A、C）」，
  而同一条提示词正文里写着 "at Grid A2 ... at Grid C2 ... at Grid B1"——渲出来的
  字母就是网格行标。同一次运行的 anchor_recalibrations 还显示声明格位与真实渲染
  对不上（A2→B2、C2→C3），事后回校了五个 slot：这套坐标既锁不住构图，又污染画面。

因此契约反转：Grid 与百分比只活在 packet / TRACES / changed_grid_cells 这些内部结构里，
交给图像与视频模型的正文一律用方位散文与分数措辞。这个文件守的就是这条线。
"""
import re

import pytest

import prompt_pipeline as pp

# 事故帧 IMAGE 4 的真实提示词（library.json → frameRun.frames[3].prompt，原样截取）
SHIPPED_IMAGE_4 = (
    "Static tripod shot, wide 18mm lens feel, camera height 1.2m, camera pitch locked level, "
    "central vanishing axis centered along the longitudinal interior of the cylindrical steel "
    "tanker hull; door frame and entrance are behind camera. Flaking iron scale lies scattered "
    "along the floor contour in Grid C2, mixed with a thick layer of dried grey mineral sludge "
    "crusting the curved lower belly. Directional entry daylight spills from the open end-cap "
    "hatch behind the camera across the raw floor, supplemented by a single portable halogen "
    "work tripod standing unlit near Grid B3; lighting phase: temporary work light active. "
    "Locked anchors: cylindrical steel hull ribbed wall contour at Grid B1 holding 65 percent "
    "of frame height, curved rear dome end-cap bulkhead at Grid A2 holding 40 percent of frame "
    "height, cast-iron circular manhole ring opening at Grid C2 holding 30 percent of frame height."
)

NOTATION_RE = re.compile(r'\bGrid\s+[A-Za-z]\s*\d\b|\d{1,3}\s*(?:percent|%)', re.IGNORECASE)


class TestTheShippedFailureCannotRecur:
    def test_shipped_frame_prompt_carried_the_letters_that_got_rendered(self):
        """先证明事故输入确实带着那三个行标——否则下面的断言证明不了任何事。"""
        cells = set(re.findall(r'\bGrid\s+([A-C])[1-3]\b', SHIPPED_IMAGE_4))
        assert cells == {'A', 'B', 'C'}, cells

    def test_scrub_removes_every_notation_token(self):
        out = pp.scrub_spatial_notation(SHIPPED_IMAGE_4)
        assert not NOTATION_RE.search(out), NOTATION_RE.findall(out)

    def test_scrub_preserves_the_spatial_constraint_it_replaces(self):
        """不是删除约束，是换一种模型能执行的说法：每个格位都要留下对应方位。"""
        out = pp.scrub_spatial_notation(SHIPPED_IMAGE_4)
        assert 'across the lower centre of the frame' in out   # 原 Grid C2
        assert 'the mid-right of the frame' in out             # 原 Grid B3
        assert 'along the mid-left of the frame' in out        # 原 Grid B1
        assert 'across the upper centre of the frame' in out   # 原 Grid A2

    def test_scrub_preserves_landmark_names_and_scene_content(self):
        out = pp.scrub_spatial_notation(SHIPPED_IMAGE_4)
        for kept in ('cylindrical steel hull ribbed wall contour',
                     'curved rear dome end-cap bulkhead',
                     'cast-iron circular manhole ring opening',
                     'dried grey mineral sludge',
                     'temporary work light active'):
            assert kept in out

    def test_validator_now_rejects_what_it_used_to_wave_through(self):
        """旧 check_grid_coordinates 只校验格位落在 A1-C3 范围内，等于给记号发通行证。"""
        assert pp.check_grid_coordinates(SHIPPED_IMAGE_4)
        assert pp.check_grid_coordinates(pp.scrub_spatial_notation(SHIPPED_IMAGE_4)) == []


class TestScrubGrammar:
    """翻译结果必须是能直接送去渲染的通顺英文，不能留下悬空介词或碎句。"""

    @pytest.mark.parametrize('src,expected', [
        ('The polished floor in Grid C1-C3 shows reflections.',
         'The polished floor across the lower third of the frame shows reflections.'),
        ('cleared across Grid A2, B2, and C2 completely.',
         'cleared down the centre column of the frame completely.'),
        ('worker enters from the Grid C1 edge and exits.',
         'worker enters from the lower left edge of the frame and exits.'),
        ('100 percent of interior hull floor is cleared.',
         'the entire interior hull floor is cleared.'),
        ('exposed area grows from 10 percent to 90 percent.',
         'exposed area grows from a narrow strip to most of it.'),
        ('no grid notation here at all, plain prose.',
         'no grid notation here at all, plain prose.'),
    ])
    def test_reads_as_english(self, src, expected):
        assert pp.scrub_spatial_notation(src) == expected

    def test_no_dangling_preposition_or_double_space(self):
        for src in ('at Grid B2', 'in Grid A1 and Grid C3', '55 percent of the wall',
                    'holding 65 percent of frame height'):
            out = pp.scrub_spatial_notation(src)
            assert '  ' not in out
            assert not re.search(r'\b(?:of|at|in|the)\s*$', out), out
            assert not re.search(r'\bthe\s+(?:in|at|across|along|down)\s+the\b', out), out


class TestPacketKeepsItsCoordinates:
    """内部坐标系不受影响——推理与校验照常用 Grid，只是不再渲染给模型。"""

    def test_canonical_clause_reads_grid_from_packet_and_emits_prose(self):
        clause = pp._canonical_anchor_clause([
            {'name': 'brick column', 'grid': 'Grid B1', 'z_depth_scale': '65%'},
        ])
        assert 'along the mid-left of the frame' in clause
        assert 'about two thirds of the frame height' in clause
        assert not NOTATION_RE.search(clause)

    def test_scale_lock_still_catches_real_drift_across_notations(self):
        """三种书写形态都要能读，判据是"落在哪个桶"而不是数字全等。"""
        packet = {'primary_landmarks': [{'name': 'brick column', 'z_depth_scale': 65}]}
        for ok in ('the brick column rising to about two thirds of the frame height.',
                   'the brick column holding sixty-five percent of frame height.',
                   'the brick column holding 65 percent of frame height.'):
            assert pp.check_anchor_scale_lock(ok, packet, family='exterior') == [], ok
        for drifted in ('the brick column rising to about a quarter of the frame height.',
                        'the brick column holding twenty-five percent of frame height.'):
            assert pp.check_anchor_scale_lock(drifted, packet, family='exterior'), drifted

    def test_bucket_tolerance_absorbs_differences_no_viewer_can_see(self):
        """63 与 65 之间的"漂移"过去会触发一轮无意义回炉。"""
        packet = {'primary_landmarks': [{'name': 'brick column', 'z_depth_scale': 65}]}
        near = 'the brick column holding sixty-six percent of frame height.'
        assert pp.check_anchor_scale_lock(near, packet, family='exterior') == []


class TestProactiveFixesAreTheLastGate:
    def test_notation_never_survives_apply_proactive_fixes(self):
        """无论记号从模板、锚点句、还是模型自由发挥进来，都出不了这个函数。"""
        packet = {
            'primary_landmarks': [
                {'name': 'brick column', 'grid': 'Grid B1', 'z_depth_scale': '65%'},
            ],
            'worker_choreography': 'one lone worker in a solid pale shirt',
            'worker_scale_percent': '18%',
        }
        video, image = pp.apply_proactive_fixes(
            2,
            'A worker in Grid C1 sweeps the floor across Grid C1-C3, clearing 80 percent of it.',
            'The brick column at Grid B1 holding 65 percent of frame height stands over debris '
            'in Grid C2.',
            packet, 'Standard', False, False,
            beat={'operation': 'clearing', 'description': 'debris cleared'}, config={},
        )
        assert not NOTATION_RE.search(video), NOTATION_RE.findall(video)
        assert not NOTATION_RE.search(image), NOTATION_RE.findall(image)
