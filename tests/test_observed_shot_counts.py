"""原片观察到的剪辑节奏 → 多镜头复刻单里的镜头梯。

盯的是一条会静默失效的接力：**一拍排三镜还是四镜，由原片切了几刀决定，不由片长决定**。

失效时的样子：抽帧脚本一直在算剪辑切点（analyze_timelapse_video 的高阈值那一遍），
也一直写进 video_overview.json 的 cut_points，但在这条接力搭起来之前全线没有读者——
复刻线只取了聚合后的 pace_metrics 三个数拿去显示。于是不管原片这一拍是一镜到底还是
切了三刀，交付出来的镜头梯都只看片长，「1:1 复刻」在剪辑节奏这一维上完全没有发生。

四段接力，每一段断了都会让镜头梯静默退回按片长排：
  1. 派生层：cut_points 按拍窗切出 observed_cuts / observed_shot_count（reverse）；
  2. 关一：beats_to_dimensions 把它写进清单条目（shot_count）；
  3. 关二：_outline_normalized_entries 认这个键——**漏在这里不报任何错**；
  4. 关三：apply_observed_shot_plan 按下标贴回梯子，composer 据此取梯。

偏差按**镜长**判而不是按镜头数：原片一拍平均三秒半、交付一拍是固定片长（八秒），
拿镜头数直接比的是两个拍长不同的东西——实测一条 77 秒片会把 22 拍里的 7 拍误标成偏差
（原片 0.26 刀/秒 vs 交付三镜 0.25 刀/秒，节奏其实是对上的），同时放过真正被放慢
一倍多的那一两个快切拍（原片每镜 0.9 秒 → 交付每镜 2.0 秒）。

方案 A（2026-08-23 定）：合法档位只有三镜与四镜。原片 ≥3 镜排四镜，1~2 镜排三镜下限，
落在区间外的偏差如实记进 parsed_brief['observed_shot_deviations']，不假装 1:1。
"""

import unittest

import os

import prompt_pipeline as pp
import server_common
from prompt_pipeline import reverse
from prompt_pipeline.composers import get_composer, MiniatureComposer


def _doc(*windows):
    """windows: (start, end) 序列。"""
    return {'beats': [{'id': f'B{i:02d}', 'start': s, 'end': e,
                       'visible_action': 'work', 'visible_result': 'done'}
                      for i, (s, e) in enumerate(windows, 1)]}


# 片长必须在测试里钉死。begin_run({}) 会让 clip_duration() 回落到机器上的
# server_config.json——那份配置用户在设置页随时会改（本次实测：改成 Omni Flash / 10 秒
# 之后，一条断言 delivered_shot_seconds == 2.0 的用例当场变成 2.5）。测试期望不能由
# 一个跑测试的人随手改得动的文件决定。
CONFIG_8S = {'videoModel': 'omni-flash', 'videoDuration': 8}


def _composer(profile, beats=None):
    composer = get_composer(profile)
    composer.begin_run(dict(CONFIG_8S), {})
    return composer


class TestDerivedShotCounts(unittest.TestCase):
    def test_cut_points_become_a_per_beat_shot_count(self):
        doc = _doc((0.0, 6.0), (6.0, 12.0))
        reverse.attach_shot_cuts(doc, {'cut_points': [2.0, 4.0, 6.0]})
        self.assertEqual(doc['beats'][0]['observed_shot_count'], 3)
        self.assertEqual(doc['beats'][1]['observed_shot_count'], 1)


class TestTheThreeGates(unittest.TestCase):
    """三道关任意一道漏配，镜头梯都会静默退回按片长排。"""

    def _outline(self):
        doc = _doc((0.0, 6.0), (6.0, 12.0))
        reverse.attach_shot_cuts(doc, {'cut_points': [2.0, 4.0, 6.0]})
        return reverse.beats_to_dimensions(doc, {})['beat_outline']

    def test_gate_one_puts_the_count_on_the_outline_entry(self):
        outline = self._outline()
        self.assertEqual([e.get('shot_count') for e in outline], ['3', '1'])

    def test_gate_two_carries_it_through_normalization(self):
        """这一道是最容易漏的：gate 1 照发、gate 3 不渲染它，漏了不会有任何信号。"""
        normalized = pp._outline_normalized_entries(self._outline())
        self.assertEqual([e.get('shot_count') for e in normalized], ['3', '1'])

    def test_gate_three_pins_it_back_onto_the_ladder(self):
        brief = {'beat_outline': pp._outline_normalized_entries(self._outline())}
        ladder = [{'operation': 'build'}, {'operation': 'build'}]
        applied, deviations = pp.apply_observed_shot_plan(ladder, brief)
        self.assertEqual(applied, 2)
        self.assertEqual([b['observed_shot_count'] for b in ladder], [3, 1])
        # 每镜时长一起贴上：偏差判据要用它
        self.assertEqual([b['observed_shot_seconds'] for b in ladder], [2.0, 6.0])
        # 没给 composer 就不判偏差——交付几镜、每镜多长只有它知道
        self.assertEqual(deviations, [])

    def test_the_insert_subject_rides_the_same_relay(self):
        doc = _doc((0.0, 6.0), (6.0, 12.0))
        reverse.attach_shot_cuts(doc, {'cut_points': [2.0, 4.0]})
        doc['beats'][0]['insert_subject'] = 'the tweezer tip pressing a roof tile'
        outline = reverse.beats_to_dimensions(doc, {})['beat_outline']
        self.assertEqual(outline[0]['insert'], 'the tweezer tip pressing a roof tile')
        brief = {'beat_outline': pp._outline_normalized_entries(outline)}
        self.assertEqual(brief['beat_outline'][0]['insert'],
                         'the tweezer tip pressing a roof tile')
        ladder = [{'operation': 'build'}, {'operation': 'build'}]
        pp.apply_observed_shot_plan(ladder, brief, composer=_composer('miniature'))
        self.assertEqual(ladder[0]['insert_subject'], 'the tweezer tip pressing a roof tile')
        self.assertNotIn('insert_subject', ladder[1])

        # 单镜链路一个字都不贴：它的一拍就是一条不间断的镜头，给它一个「切进特写拍这个」
        # 的指令是它的语法切不出来的。
        base_ladder = [{'operation': 'build'}, {'operation': 'build'}]
        pp.apply_observed_shot_plan(base_ladder, brief, composer=_composer('base'))
        self.assertNotIn('insert_subject', base_ladder[0])

    def test_a_ladder_that_does_not_match_the_plan_is_left_alone(self):
        """规划四轮全灭退回兜底梯子时，条数对不上——按下标硬贴等于把 A 拍的剪辑节奏
        贴到 B 拍上，与 apply_observed_space_sequence 同一条纪律。"""
        brief = {'beat_outline': pp._outline_normalized_entries(self._outline())}
        ladder = [{'operation': 'build'}]
        self.assertEqual(pp.apply_observed_shot_plan(ladder, brief), (0, []))
        self.assertNotIn('observed_shot_count', ladder[0])

    def test_a_legacy_plan_without_the_field_changes_nothing(self):
        brief = {'beat_outline': [{'text': 'a'}, {'text': 'b'}]}
        ladder = [{'operation': 'build'}, {'operation': 'build'}]
        self.assertEqual(pp.apply_observed_shot_plan(ladder, brief), (0, []))
        self.assertIsNone(pp.observed_shot_count_of(ladder[0]))


class TestLadderSelection(unittest.TestCase):
    """方案 A 的档位映射，两条多镜头链路口径必须一致。"""

    PROFILES = ('omni', 'miniature')

    def test_a_beat_inside_the_legal_range_is_reproduced_shot_for_shot(self):
        """原片三镜就排三镜、四镜就排四镜——落在合法区间里的部分是真的 1:1。"""
        for profile in self.PROFILES:
            with self.subTest(profile=profile):
                composer = _composer(profile)
                for observed in (3, 4):
                    ladder = composer.ladder_for_beat({'operation': 'build',
                                                       'observed_shot_count': observed})
                    self.assertEqual(len(ladder), observed)

    def test_a_beat_cut_finer_than_the_legal_range_takes_the_ceiling(self):
        for profile in self.PROFILES:
            with self.subTest(profile=profile):
                composer = _composer(profile)
                ladder = composer.ladder_for_beat({'operation': 'build',
                                                   'observed_shot_count': 9})
                self.assertEqual(len(ladder), 4)

    def test_a_single_shot_beat_falls_back_to_the_three_shot_minimum(self):
        """多镜头契约没有单镜档位——一镜到底是硬禁令。最接近的合法值是三镜。"""
        for profile in self.PROFILES:
            with self.subTest(profile=profile):
                composer = _composer(profile)
                ladder = composer.ladder_for_beat({'operation': 'build',
                                                   'observed_shot_count': 1})
                self.assertEqual(len(ladder), 3)

    def test_two_observed_shots_still_take_the_minimum(self):
        for profile in self.PROFILES:
            with self.subTest(profile=profile):
                composer = _composer(profile)
                self.assertEqual(
                    len(composer.ladder_for_beat({'operation': 'build',
                                                  'observed_shot_count': 2})), 3)

    def test_without_the_field_the_ladder_is_still_chosen_by_duration(self):
        """原创单、老 job、二创变体、抽帧异常都走这一支，行为必须逐字不变。"""
        for profile in self.PROFILES:
            with self.subTest(profile=profile):
                composer = _composer(profile)
                by_duration = composer.ladder_for_kind(composer.clip_duration(), 'construction')
                self.assertEqual(composer.ladder_for_beat({'operation': 'build'}), by_duration)

    def test_reveal_and_reward_ignore_the_observed_count(self):
        """揭示拍与兑现拍的三个工位是由职责定的；原片多切几刀只是把同一件事切碎。"""
        for profile in self.PROFILES:
            with self.subTest(profile=profile):
                composer = _composer(profile)
                for operation in ('threshold', 'reward'):
                    ladder = composer.ladder_for_beat({'operation': operation,
                                                       'observed_shot_count': 6})
                    self.assertEqual(len(ladder), 3)

    def test_the_timeline_sentence_follows_the_chosen_ladder(self):
        """切点表是确定性注入的，选错梯就等于给这条片子发了一张错的切点表。"""
        composer = _composer('omni')
        beat = {'operation': 'build', 'observed_shot_count': 1}
        text = composer.normalize_omni_video(
            'The worker sets blocks along the course.', beat=beat)
        self.assertIn('a returning wide shot', text)
        self.assertNotIn('extreme close-up insert', text)


if __name__ == '__main__':
    unittest.main()


class TestDurationStillCapsTheLadder(unittest.TestCase):
    """片长是硬上限：4/6 秒排不下第二个插入镜。

    原片切得再碎也不能把四镜塞进四秒——每镜不足一秒就是闪帧，正是 2026-08-09 废掉
    景别轮换梯的那个失败模式。观察值只在片长允许的范围内说话。
    """

    def test_a_short_clip_never_takes_the_four_shot_ladder(self):
        composer = get_composer('omni')
        composer.begin_run({'videoModel': 'omni-flash', 'videoDuration': 4}, {})
        self.assertEqual(composer.clip_duration(), 4)
        self.assertEqual(
            len(composer.ladder_for_beat({'operation': 'build', 'observed_shot_count': 9})), 3)

    def test_a_long_clip_honours_the_observed_count(self):
        composer = get_composer('omni')
        composer.begin_run({'videoModel': 'omni-flash', 'videoDuration': 10}, {})
        self.assertEqual(
            len(composer.ladder_for_beat({'operation': 'build', 'observed_shot_count': 3})), 3)
        self.assertEqual(
            len(composer.ladder_for_beat({'operation': 'build', 'observed_shot_count': 4})), 4)


class TestDeviationIsJudgedByShotLength(unittest.TestCase):
    """偏差按镜长判，不按镜头数判。

    实测反例（一条 77.276 秒的微缩片，22 拍拍内共 20 刀）：按镜头数判会把 7 个「原片
    一镜」的拍全部标红——而它们的镜长是 3.5 秒，交付三镜是 8÷3≈2.7 秒，读起来是同一个
    节奏（拍内切点率 0.26 刀/秒 vs 0.25 刀/秒）。真正被改掉的是原片把四镜压进三秒半的
    那种快切拍：每镜 0.9 秒被摊成 2.0 秒，慢了一倍多，而它按数量判完全对得上、一条告警
    都不会有。判据错在维度上，就会同时误报和漏报。
    """

    def _apply(self, profile, entries, ops=None):
        composer = _composer(profile)
        brief = {'beat_outline': entries}
        ladder = [{'operation': op} for op in (ops or ['build'] * len(entries))]
        return composer, ladder, pp.apply_observed_shot_plan(ladder, brief, composer=composer)

    def test_a_single_shot_beat_at_a_comparable_pace_is_not_flagged(self):
        # 原片一镜、镜长 3.5s；交付 8s 三镜 = 每镜 2.67s，1.3 倍，不算改节奏
        _c, _l, (applied, deviations) = self._apply(
            'miniature', [{'text': 'a', 'shot_count': '1', 'shot_seconds': '3.5'}])
        self.assertEqual(applied, 1)
        self.assertEqual(deviations, [])

    def test_a_fast_cut_beat_is_flagged(self):
        # 原片四镜压进 3.5s = 每镜 0.88s；交付 8s 四镜 = 每镜 2.0s，2.3 倍
        _c, _l, (_applied, deviations) = self._apply(
            'miniature', [{'text': 'a', 'shot_count': '4', 'shot_seconds': '0.88'}])
        self.assertEqual(len(deviations), 1)
        self.assertEqual(deviations[0]['observed_shots'], 4)
        self.assertEqual(deviations[0]['delivered_shots'], 4)
        self.assertEqual(deviations[0]['observed_shot_seconds'], 0.88)
        self.assertEqual(deviations[0]['delivered_shot_seconds'], 2.0)

    def test_a_very_long_source_shot_is_flagged_the_other_way(self):
        # 原片一镜跑了 20 秒，交付切成三镜每镜 2.67s —— 长镜被切碎，同样是改了节奏
        _c, _l, (_applied, deviations) = self._apply(
            'miniature', [{'text': 'a', 'shot_count': '1', 'shot_seconds': '20'}])
        self.assertEqual(len(deviations), 1)
        self.assertEqual(deviations[0]['observed_shot_seconds'], 20.0)

    def test_reveal_and_reward_beats_are_never_flagged(self):
        """它们的镜头梯由职责定（逼近/门槛/落定、细部/拉开/终局），原片多切几刀
        只是把同一件事切碎。"""
        _c, _l, (applied, deviations) = self._apply(
            'miniature',
            [{'text': 'a', 'shot_count': '6', 'shot_seconds': '0.4'},
             {'text': 'b', 'shot_count': '6', 'shot_seconds': '0.4'}],
            ops=['threshold', 'reward'])
        self.assertEqual(applied, 2)
        self.assertEqual(deviations, [])

    def test_a_single_shot_profile_attaches_data_but_judges_nothing(self):
        """base/Veo 的一拍就是一条不间断镜头，「剪辑节奏偏差」这个概念不成立。"""
        _c, ladder, (applied, deviations) = self._apply(
            'base', [{'text': 'a', 'shot_count': '4', 'shot_seconds': '0.5'}])
        self.assertEqual(applied, 1)
        self.assertEqual(ladder[0]['observed_shot_count'], 4)
        self.assertEqual(deviations, [])


class TestInsertSubjectReachesTheWriter(unittest.TestCase):
    """原片这一拍的插入镜拍的是什么 → 交付时那一镜拍的就是它。

    不给这一栏时，插入镜落回通用职责（工具接触点 / 持久痕迹）——那是这条片子里任何
    一拍都能写的话，不是这一拍的画面。
    """

    def test_it_is_pinned_onto_the_close_up_rung(self):
        for profile in ('omni', 'miniature'):
            with self.subTest(profile=profile):
                composer = _composer(profile)
                ladder = composer.ladder_for_kind(8, 'construction')
                roles = composer_roles(composer, ladder, 'the tweezer tip pressing a roof tile')
                self.assertIn('the tweezer tip pressing a roof tile', roles)
                # 钉在插入镜那一行上，不是随便找个地方塞
                insert_line = [l for l in roles.split('\n') if 'close-up insert' in l][0]
                self.assertIn('the tweezer tip pressing a roof tile', insert_line)

    def test_without_it_the_role_text_is_unchanged(self):
        composer = _composer('omni')
        ladder = composer.ladder_for_kind(8, 'construction')
        self.assertEqual(composer_roles(composer, ladder, None),
                         composer_roles(composer, ladder, ''))

    def test_the_single_beat_prompt_carries_it(self):
        composer = _composer('miniature')
        contract = {
            'beat': {'operation': 'build', 'description': 'roof tiles',
                     'insert_subject': 'mortar squeezing out from under the block'},
            'img_i_lighting': 'ambient', 'img_ip1_lighting': 'ambient',
            'family_contract': '', 'templates_cropped': '', 'anchor_rule': '',
            'stage_scope': '', 'is_first_interior_reveal': False, 'family': 'exterior',
        }
        prompt = composer.single_beat_system_prompt({}, 1, contract, {}, {1: 'IM1'}, {}, '', '')
        self.assertIn('mortar squeezing out from under the block', prompt)


def composer_roles(composer, ladder, insert_subject):
    from prompt_pipeline.composers.omni import ladder_roles
    return ladder_roles(ladder, insert_subject)


class TestTheBatchPathCarriesIt(unittest.TestCase):
    """批量通路才是主通路（composeBatchSize 默认 5），逐拍观测数据必须从这里进去。

    batch_system_prompt 是**每拍共享**的一段，逐拍的插入镜主体在那里没有落脚点；只把它
    交给规划器去织进 description，等于「绑在兜底通路上、主通路上靠运气」——这条线上已经
    栽过一次的形状（2026-08-22 的进度事件、2026-08-11 的 package_operations 都是它）。
    """

    def test_the_per_beat_block_names_the_observed_insert(self):
        contract = {
            'beat': {'operation': 'lay tiles', 'description': 'shingle the roof',
                     'insert_subject': 'the tweezer tip pressing a roof tile'},
            'img_i_lighting': 'ambient', 'img_ip1_lighting': 'ambient',
            'family_contract': 'F', 'anchor_rule': 'A', 'templates_cropped': 'T',
        }
        block = pp._beat_block_text(4, contract)
        self.assertIn('INSERT SHOT SUBJECT', block)
        self.assertIn('the tweezer tip pressing a roof tile', block)

    def test_a_beat_without_one_is_untouched(self):
        contract = {
            'beat': {'operation': 'lay tiles', 'description': 'shingle the roof'},
            'img_i_lighting': 'ambient', 'img_ip1_lighting': 'ambient',
            'family_contract': 'F', 'anchor_rule': 'A', 'templates_cropped': 'T',
        }
        self.assertNotIn('INSERT SHOT SUBJECT', pp._beat_block_text(4, contract))


class TestCastActionReachesTheWriter(unittest.TestCase):
    """画面里的人/人偶在这一拍怎么动 → 交付出来他们真的在动。

    2026-08-23 用户实测反馈：成片里人物完全静止。根因有两层，这里钉的是分析层那一层——
    beat schema 里跟人相关的字段此前只有 workers_present（布尔）与 worker_count（整数），
    姿态、朝向、视线、位移一个字段都没有，原片里那两个小人在做什么从来没被采下来过。
    """

    def test_it_rides_the_relay_onto_the_ladder(self):
        doc = _doc((0.0, 6.0), (6.0, 12.0))
        doc['beats'][0]['cast_action'] = ('the two figurines turn from the moss to face '
                                          'the rising wall, the one in red half a step closer')
        outline = reverse.beats_to_dimensions(doc, {})['beat_outline']
        self.assertIn('turn from the moss', outline[0]['cast'])
        brief = {'beat_outline': pp._outline_normalized_entries(outline)}
        self.assertIn('turn from the moss', brief['beat_outline'][0]['cast'])
        ladder = [{'operation': 'build'}, {'operation': 'build'}]
        pp.apply_observed_shot_plan(ladder, brief, composer=_composer('miniature'))
        self.assertIn('turn from the moss', ladder[0]['cast_action'])
        self.assertNotIn('cast_action', ladder[1])

    def test_single_shot_profiles_get_it_too(self):
        """冻住的不只是微缩人偶——真人线的工人同样需要姿态与视线。
        （插入镜主体是多镜头专属，身体语言不是。）"""
        brief = {'beat_outline': [{'text': 'a', 'cast': 'crouches at the wall foot',
                                   'insert': 'the trowel tip'}]}
        ladder = [{'operation': 'build'}]
        pp.apply_observed_shot_plan(ladder, brief, composer=_composer('base'))
        self.assertEqual(ladder[0]['cast_action'], 'crouches at the wall foot')
        self.assertNotIn('insert_subject', ladder[0])

    def test_the_batch_path_carries_it(self):
        contract = {
            'beat': {'operation': 'lay blocks', 'description': 'course six',
                     'cast_action': 'the two figurines crouch to sight along the new course'},
            'img_i_lighting': 'ambient', 'img_ip1_lighting': 'ambient',
            'family_contract': 'F', 'anchor_rule': 'A', 'templates_cropped': 'T',
        }
        block = pp._beat_block_text(2, contract)
        self.assertIn('CAST IN FRAME', block)
        self.assertIn('crouch to sight along the new course', block)

    def test_the_plan_block_carries_it_on_both_kinds_of_line(self):
        plan = [{'text': 'lay blocks', 'cast': 'the figurines step in closer'}]
        for multishot in (True, False):
            _p, block = pp.build_outline_plan_block(plan, 1, multishot=multishot)
            self.assertIn('CAST: the figurines step in closer', block)
            self.assertIn('reproducing them in the identical pose beat after beat', block)


class TestTheMiniatureContractNoLongerFreezesTheCast(unittest.TestCase):
    """微缩包此前主动把人偶钉死：IMAGE 模板里 12 处「remain / stand where they were」，
    VIDEO 范例 7 条里 5 条根本不提人偶。锁的应该是身份/服装/比例，不是姿态。"""

    def _templates(self):
        path = os.path.join(server_common.skill_dir('miniature'),
                            'references', 'prompt-templates.md')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_no_image_exemplar_freezes_them(self):
        # Anti-Patterns 段是反例陈列区——那几句写死人偶的话正是登记在那里的失败形态，
        # 扫描必须跳过它，否则这条测试会被它自己要防的那段文字判红。
        text = self._templates()
        scanned = text[:text.index('### Anti-Patterns')] + text[text.index('### Final IMAGE'):]
        for phrase in ('figurines remain at', 'stand where they were',
                       'stand at the lower-left as before'):
            self.assertNotIn(phrase, scanned, f'IMAGE 范例又把人偶写死了：{phrase}')

    def test_every_video_exemplar_gives_them_a_micro_action(self):
        text = self._templates()
        video = text[text.index('## VIDEO Templates'):text.index('## Fill-In Checklist')]
        missing = []
        for part in video.split('#### ')[1:]:
            label = part.split('\n')[0]
            body = next(l for l in part.split('\n')[1:] if l.strip() and not l.startswith('---'))
            if 'figurine' not in body:
                missing.append(label)
        assert not missing, '这些 VIDEO 范例一个字都没提人偶，抄它们的拍必然交付冻住的小人：%s' % missing

    def test_the_worldview_override_demands_life(self):
        composer = MiniatureComposer()
        composer.begin_run({'videoModel': 'miniature'}, {})
        prompt = composer.batch_system_prompt({}, {'camera_dna': ''}, '', '')
        self.assertIn('They are ALIVE in every beat', prompt)
        self.assertIn('CAST IN FRAME', prompt)
        # 锁的是身份与比例，不是姿态
        self.assertIn('What is FREE is their pose', prompt)
