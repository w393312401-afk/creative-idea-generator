"""Regression cases for false passes found by the October Omni skill evaluation.

The fixtures contain real actions, then mutate one property at a time. Ordered shot names
alone must not prove editing or continuity, and checking the cap must include every word in
the delivered VIDEO body. These are offline checks; no generation service is called.
"""
import pytest
from unittest.mock import patch

import prompt_pipeline as pp
from prompt_pipeline import composers
from prompt_pipeline.composers import omni


BODY_THREE = (
    "A wide working shot shows the worker pressing the shelf board flush against its two "
    "original brackets with the original screwdriver. A clean cut enters a close-up insert "
    "of the screwdriver turning the first original screw; fresh compression fibers and a "
    "thin scrape stay at the contact point, with no further board movement in the insert. "
    "A match cut returns to a returning wide shot from the same camera setup as the opening "
    "wide working shot, with identical camera position, focal length and framing, as the "
    "worker tightens the second original screw in the same way and the board remains flush."
)

BODY_FOUR = BODY_THREE.replace(
    "A match cut returns",
    "A clean cut enters an extreme close-up insert on those already formed compression "
    "fibers and the screw head scrape, with the board held at the same completion state. "
    "A match cut returns",
)


def structural_errors(text, **kwargs):
    return [e for e in omni.omni_video_violations(text, **kwargs)
            if e.startswith(omni.OMNI_VIDEO_ERROR_PREFIX)]


class TestEditedSequenceEvidence:
    @pytest.mark.parametrize('duration,body', [(4, BODY_THREE), (10, BODY_FOUR)])
    def test_actual_edited_actions_pass_without_a_numeric_timeline(self, duration, body):
        assert omni.omni_video_violations(
            body, ladder=omni.ladder_for(duration), duration=duration) == []

    def test_ordered_bare_shot_labels_do_not_prove_an_edited_action_sequence(self):
        labels = ("A wide working shot. A close-up insert. "
                  "An extreme close-up insert. A returning wide shot.")
        assert structural_errors(labels, ladder=omni.ladder_for(10), duration=10)

    def test_action_prose_without_actual_cut_transitions_is_rejected(self):
        body = BODY_FOUR.replace('A clean cut enters', 'The view shows').replace(
            'A match cut returns to', 'The view becomes')
        assert structural_errors(body, ladder=omni.ladder_for(10), duration=10)

    def test_one_cut_cannot_prove_every_boundary_in_a_four_shot_clip(self):
        body = BODY_FOUR.replace('A clean cut enters an extreme', 'The camera shows an extreme')
        assert structural_errors(body, ladder=omni.ladder_for(10), duration=10)

    @pytest.mark.parametrize('replacement', [
        'a new overhead camera setup with a different focal length and framing',
        'the same camera setup, but with a different focal length and a tighter framing',
        'the same camera setup and focal length, but the camera is repositioned overhead',
    ])
    def test_return_camera_changes_cannot_pass_on_shot_names_or_same_setup_words(self, replacement):
        body = BODY_FOUR.replace(
            'the same camera setup as the opening wide working shot, '
            'with identical camera position, focal length and framing', replacement)
        assert structural_errors(body, ladder=omni.ladder_for(10), duration=10)

    def test_natural_same_camera_synonyms_are_accepted(self):
        body = BODY_FOUR.replace(
            'the same camera setup as the opening wide working shot, '
            'with identical camera position, focal length and framing',
            'the opening camera position, lens and framing, all unchanged')
        assert omni.omni_video_violations(body, ladder=omni.ladder_for(10), duration=10) == []

    def test_variation_in_tool_contact_angle_is_not_a_camera_change(self):
        body = BODY_FOUR + (
            ' Each repeated trowel pass lands at a slightly different angle and pace; '
            'the pressure shifts before the tool touches the material.')
        assert omni.omni_video_violations(body, ladder=omni.ladder_for(10), duration=10) == []

    @pytest.mark.parametrize('transition', ['The last cut returns to a', 'The final cut is a'])
    def test_a_last_cut_returning_to_the_working_camera_is_an_actual_picture_edit(self, transition):
        body = BODY_FOUR.replace('A match cut returns to a', transition)
        assert omni.omni_video_violations(body, ladder=omni.ladder_for(10), duration=10) == []

    def test_negated_cuts_cannot_prove_the_edit_boundaries(self):
        body = BODY_FOUR.replace('A clean cut enters', 'Without a clean cut, the view shows').replace(
            'A match cut returns to', 'Without a match cut, the view shows')
        assert structural_errors(body, ladder=omni.ladder_for(10), duration=10)

    def test_a_soundtrack_edit_cannot_prove_a_picture_cut(self):
        body = BODY_FOUR.replace('A clean cut enters', 'The soundtrack uses a clean cut before').replace(
            'A match cut returns to', 'The audio uses a match cut before')
        assert structural_errors(body, ladder=omni.ladder_for(10), duration=10)


class TestHardVideoCeilings:
    def test_precompression_does_not_destroy_an_already_bounded_complete_sequence(self):
        # A complete user draft is longer than the provisional draft budget but within
        # its delivery ceiling. An aggressive compressor must not erase its actual cuts.
        detail = (' The untouched right wall keeps its plaster pores and the original '
                  'window shadow; only the two original shelf fixings change.')
        body = BODY_THREE + detail * 8
        assert omni.video_draft_budget(3) < len(body.split()) <= 400
        composer = composers.get_composer('omni')
        config = {'videoModel': 'Omni Flash', 'videoDuration': 4}
        composer.begin_run(config, {'parsed_brief': {}})

        def aggressive_compression(text, ceiling, _config, is_video=True):
            return 'The worker completes the shelf.' if ceiling < 400 else text

        with patch.object(pp, 'compress_prompt_to_budget', side_effect=aggressive_compression):
            fixed = composer.fix_omni_video(
                1, body, {}, False, beat={'operation': 'repair'}, config=config)
        assert 'screwdriver turning the first original screw' in fixed
        assert 'A clean cut enters a close-up insert' in fixed
        assert 'same camera setup as the opening wide working shot' in fixed
        assert not omni._shot_structure_errors(fixed, omni.ladder_for(4))

    @pytest.mark.parametrize('duration,body,ceiling', [
        (4, BODY_THREE, 400), (6, BODY_THREE, 400),
        (8, BODY_FOUR, 455), (10, BODY_FOUR, 455),
    ])
    def test_a_single_word_over_the_complete_body_cap_is_a_structural_error(
            self, duration, body, ceiling):
        assert omni.video_word_targets(len(omni.ladder_for(duration)))[1] == ceiling
        at_cap = body + ' ' + ' '.join(['steady'] * (ceiling - len(body.split())))
        assert len(at_cap.split()) == ceiling
        assert omni.omni_video_violations(
            at_cap, ladder=omni.ladder_for(duration), duration=duration) == []
        assert structural_errors(
            at_cap + ' steady', ladder=omni.ladder_for(duration), duration=duration)

    @pytest.mark.parametrize('ceiling', [180, 220])
    def test_image_gate_checks_an_explicit_complete_body_cap(self, ceiling):
        body = 'A fixed static room anchor. ' + ' '.join(['preserved'] * (ceiling - 5))
        assert len(body.split()) == ceiling
        assert omni.omni_image_violations(body, word_limit=ceiling) == []
        assert omni.omni_image_violations(body + ' preserved', word_limit=ceiling)


class TestTransitionAndRewardSequences:
    TRANSITION_BODY = (
        "A transition working shot advances from the open sill into the shallow cabin, "
        "following the original door axis and the same two port windows. A clean cut to a "
        "detail insert observes the fixed seal and forward rib at the current position, "
        "without moving the camera deeper or changing the cavity. A match cut to a landing "
        "shot resumes from that same position and advances only to the planned near rib, "
        "matching the next anchor under unchanged daylight."
    )
    HARDWARE_BODY = (
        "A wide working shot shows the original door resting on its original hinges as "
        "the hand releases its physical latch. A clean cut to a close-up insert shows the "
        "latch tongue withdrawing with a fresh contact scrape and compressed seal; the "
        "door does not advance farther in this insert. A match cut to a returning wide "
        "shot uses the same camera setup as the opening wide working shot, keeping its "
        "lens and framing unchanged, while the door swings on those hinges to the planned "
        "open position. No work happens inside the cabin."
    )
    FULL_TRAVERSAL_BODY = (
        "A wide approach shot begins outside the original door with the left port window "
        "and near rib visible through the opening. A clean cut to a threshold shot moves "
        "across the physical sill as the door frame leaves the view and exposure rolls "
        "toward the indoor daylight. A match cut to an interior wide shot settles inside "
        "the shallow original cavity, retaining the same window and rib as fixed landmarks "
        "and matching the interior result anchor. No construction occurs."
    )
    REWARD_BODY = (
        "A detail shot starts on the completed bed hinge while the user unfolds the "
        "existing reading support. A clean cut to a pull-back shot puts that support into "
        "the daylight cabin as the user sits and opens the original book. A match cut to "
        "a final wide shot finishes the same use action; the user closes and replaces the "
        "book, then exits through the original doorway and leaves the person-free result "
        "anchor. No furnishing or construction appears."
    )

    @pytest.mark.parametrize('stage', ['threshold_partial', 'shaft_descent', 'interior_establish'])
    def test_each_travel_stage_gets_its_own_edited_sequence(self, stage):
        beat = {'operation': 'threshold', 'transition_stage': stage}
        assert omni.ladder_kind(beat) == 'transition_stage'
        composer = composers.get_composer('omni')
        composer.begin_run({'videoModel': 'Omni Flash', 'videoDuration': 10}, {'parsed_brief': {}})
        ladder = composer.ladder_for_beat(beat)
        assert [rung.key for rung in ladder] == [
            'transition_work', 'transition_detail', 'transition_land']
        assert omni.omni_video_violations(
            self.TRANSITION_BODY, ladder=ladder, duration=10) == []
        assert composer.video_profile_violations(
            'The camera approaches the doorway, then stops.', beat=beat)

    def test_hardware_opening_uses_hardware_work_without_repeating_the_whole_crossing(self):
        beat = {'operation': 'threshold', 'transition_stage': 'door_hardware_open'}
        assert omni.ladder_kind(beat) == 'transition_hardware'
        ladder = omni.ladder_for(10, 'transition_hardware')
        assert [rung.key for rung in ladder] == ['main', 'close', 'return']
        assert omni.omni_video_violations(self.HARDWARE_BODY, ladder=ladder, duration=10) == []
        assert structural_errors(
            'The door opens in one continuous take.', ladder=ladder, duration=10)

    @pytest.mark.parametrize('beat', [
        {'operation': 'reframe'},
        {'operation': 'reframe', 'transition_stage': 'camera_reframe'},
        {'transition_stage': 'camera_reframe'},
    ])
    def test_no_work_camera_reframe_uses_distinct_anchor_transition_roles(self, beat):
        assert omni.ladder_kind(beat) == 'transition_stage'
        assert not omni.is_expanded_transition_stage_beat(beat)
        assert not pp.beat_is_crossing_clip(beat)
        composer = composers.get_composer('omni')
        composer.begin_run({'videoModel': 'Omni Flash', 'videoDuration': 10}, {'parsed_brief': {}})
        ladder = composer.ladder_for_beat(beat)
        assert [rung.key for rung in ladder] == [
            'transition_work', 'transition_detail', 'transition_land']
        body = (
            'A transition working shot starts at the near cabin corner and visibly rotates '
            'toward the original window wall without altering the finished room. A clean cut '
            'to a detail insert shows the same near rib from the current camera position; '
            'the camera does not move farther during the insert. A match cut to a landing '
            'shot resumes the visible rotation and settles at the distinct far-wall anchor '
            'with the original bed and seams unchanged. No construction occurs.')
        normalized = composer.normalize_omni_video(body, beat=beat)
        assert 'edited construction time-lapse' not in normalized.lower()
        assert 'returning wide shot' not in normalized.lower()
        assert composer.video_profile_violations(normalized, beat=beat) == []
        assert structural_errors(body.replace('A clean cut to', 'A view of'),
                                 ladder=ladder, duration=10)

    def test_no_entry_reframe_loads_its_spatial_rules_in_both_generation_paths(self):
        composer = composers.get_composer('omni')
        composer.begin_run({'videoModel': 'Omni Flash', 'videoDuration': 10},
                           {'parsed_brief': {}, 'beat_ladder': [{'operation': 'reframe'}]})
        with patch.object(composer, 'required_references_block', return_value='') as refs:
            composer.video_override_block(ladder=omni.ladder_for(10, 'transition_stage'))
            assert refs.call_args.kwargs['include_threshold'] is True
        with patch.object(composer, 'required_references_block', return_value='') as refs:
            composer.batch_system_prompt({}, {}, '', '')
            assert refs.call_args.kwargs['include_threshold'] is True

    def test_landing_framing_descriptor_is_not_a_fourth_edited_shot(self):
        ladder = omni.ladder_for(10, 'transition_stage')
        body = self.TRANSITION_BODY + (
            ' Settle into the usage-detail full shot matching the result anchor.')
        assert omni.omni_video_violations(body, ladder=ladder, duration=10) == []
        extra_cut = body + ' A clean cut enters a full shot of the opposite wall.'
        assert structural_errors(extra_cut, ladder=ladder, duration=10)
        construction = BODY_FOUR + ' Settle into the full shot matching the result anchor.'
        assert structural_errors(construction, ladder=omni.ladder_for(10), duration=10)

    def test_the_legacy_skip_flag_cannot_silently_remove_all_structure_checks(self):
        assert structural_errors(
            'The camera approaches the doorway, then stops.',
            ladder=omni.ladder_for(10, 'transition_stage'), duration=10, skip_shot_list=True)

    @pytest.mark.parametrize('kind,body_name', [
        ('traversal', 'FULL_TRAVERSAL_BODY'), ('reward', 'REWARD_BODY')])
    def test_full_crossing_and_usage_reward_keep_their_three_station_roles(self, kind, body_name):
        ladder = omni.ladder_for(10, kind)
        assert len(ladder) == 3
        body = getattr(self, body_name)
        assert omni.omni_video_violations(body, ladder=ladder, duration=10) == []
        assert structural_errors(body.replace('A match cut to', 'A view of'),
                                 ladder=ladder, duration=10)
        at_cap = body + ' ' + ' '.join(['steady'] * (400 - len(body.split())))
        assert omni.omni_video_violations(at_cap, ladder=ladder, duration=10) == []
        assert structural_errors(at_cap + ' steady', ladder=ladder, duration=10)


class TestExplicitSingleTakeOverride:
    BODY = (
        "One single continuous take uses the original fixed camera position, lens and "
        "framing as the worker tightens one existing screw with the original screwdriver. "
        "The screw turns under visible hand pressure, leaving a compression ring. "
        "The worker exits and the final frame matches IMAGE 2 exactly."
    )

    def test_default_still_requires_edited_structure(self):
        assert structural_errors(self.BODY, ladder=omni.ladder_for(6), duration=6)

    def test_explicit_override_exempts_only_the_shot_ladder_and_one_take_ban(self):
        at_cap = self.BODY + ' ' + ' '.join(['steady'] * (400 - len(self.BODY.split())))
        assert omni.omni_video_violations(
            at_cap, ladder=omni.ladder_for(6), duration=6,
            allow_single_take=True) == []
        assert structural_errors(
            at_cap + ' steady',
            ladder=omni.ladder_for(6), duration=6, allow_single_take=True)

    @pytest.mark.parametrize('override_scope', ['brief', 'beat'])
    def test_composer_preserves_and_validates_the_authorized_one_take(self, override_scope):
        beat = {'operation': 'repair'}
        state = {'parsed_brief': {}}
        if override_scope == 'brief':
            state['parsed_brief']['video_shot_mode'] = 'single_take'
        else:
            beat['user_single_take'] = True
        config = {'videoModel': 'Omni Flash', 'videoDuration': 6}
        composer = composers.get_composer('omni')
        composer.begin_run(config, state)
        fixed = composer.fix_omni_video(1, self.BODY, {}, False, beat=beat, config=config)
        assert omni._one_take_hits(fixed), fixed
        assert 'edited construction time-lapse' not in fixed.lower()
        assert 'close-up insert' not in fixed.lower()
        assert composer.video_profile_violations(fixed, beat=beat) == []

    def test_generated_beat_shot_mode_is_not_user_authorization(self):
        composer = composers.get_composer('omni')
        composer.begin_run({'videoModel': 'Omni Flash', 'videoDuration': 6}, {'parsed_brief': {}})
        assert composer.video_profile_violations(
            self.BODY, beat={'operation': 'repair', 'video_shot_mode': 'single_take'})

    def test_override_does_not_weaken_an_unrelated_default_run(self):
        composer = composers.get_composer('omni')
        composer.begin_run({'videoModel': 'Omni Flash', 'videoDuration': 6},
                           {'parsed_brief': {'video_shot_mode': 'single_take'}})
        assert composer.video_profile_violations(self.BODY, beat={'operation': 'repair'}) == []
        composer.begin_run({'videoModel': 'Omni Flash', 'videoDuration': 6}, {'parsed_brief': {}})
        assert composer.video_profile_violations(self.BODY, beat={'operation': 'repair'})


class TestShotModeFromUserInput:
    @pytest.mark.parametrize('user_text', [
        '六秒一镜到底，只拧紧已有螺钉。',
        '请用一镜到底，固定机位。',
        'Use one continuous single take for this screw repair.',
        'Use one continuous take for this screw repair.',
        'A six-second one-shot, fixed camera.',
        'Make this an unbroken take.',
    ])
    def test_positive_requests_authorize_the_override(self, user_text):
        assert pp.omni_user_shot_mode(theme=user_text) == 'single_take'
        assert pp.omni_user_shot_mode({'theme': user_text}) == 'single_take'

    @pytest.mark.parametrize('user_text', [
        '', '做一条六秒修复视频。',
        '不要一镜到底，保持多镜头。', '禁止一镜到底。',
        '不用一镜到底，普通剪辑即可。', '一镜到底不需要。',
        'No single take; use edited shots.',
        'Avoid a one-shot for this task.',
        'I do not want a single continuous take.',
        'Do not use one continuous take.',
        'Never use an unbroken take.',
    ])
    def test_negated_mentions_keep_the_default_edited_structure(self, user_text):
        assert pp.omni_user_shot_mode(theme=user_text) == 'multishot'

    def test_explicit_user_fields_win_over_theme_wording(self):
        assert pp.omni_user_shot_mode(
            {'video_shot_mode': 'multishot', 'theme': '一镜到底'}) == 'multishot'
        assert pp.omni_user_shot_mode(
            {'video_shot_mode': 'single-take', 'theme': '普通施工'}) == 'single_take'
        assert pp.omni_user_shot_mode({'user_single_take': True}) == 'single_take'
        assert pp.omni_user_shot_mode({'user_single_take': 'true'}) == 'multishot'
