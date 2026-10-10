"""剪影级人物漂移探针的回归（人物一致性 L3 验收层）。

这一层此前完全空白：QC 表第 1～8 项（帽/外套/内搭/裤/靴/手套/胡须/发色）全靠人眼，
而那八项本质是固定的几块颜色 —— 段数一多，人眼逐段核对必然放水。

三件事必须钉住：

1. **颜色容差的判别力**。ΔE 容差是这个探针的全部灵魂：调高了橄榄绿和卡其色变成同一
   个颜色（而 cast-registry.json 的 banned 表点名要区分这一对），调低了同一件衣服换个
   光照就报换装。这里拿真实的 banned 对照组把容差钉死。
2. **"看不见"不等于"漂了"**。某个部位在全片覆盖率都近似为零，最可能是参考色标错了或
   这个部位没入过镜，绝不能报成"每一段都换了装"。把探针测不了的东西报成缺陷，是让人
   从此忽略整份报告的最快方式。
3. **LAB 转换本身**。整个探针建在它上面，错了以上全是假的。拿教科书基准值校验。
"""
import numpy as np
import pytest

import cast_drift as cd


def solid(hex_value, size=32):
    """一张纯色图。"""
    return np.tile(np.array(cd.hex_to_rgb(hex_value), dtype="uint8"), (size, size, 1))


def composite(background_hex, patches, size=100):
    """背景 + 若干色块。patches: [(hex, 占边长比例), ...]，色块横向排开互不重叠。"""
    img = np.tile(np.array(cd.hex_to_rgb(background_hex), dtype="uint8"), (size, size, 1))
    x = 0
    for hex_value, frac in patches:
        w = max(1, int(round(size * frac)))
        img[0:size, x:x + w] = np.array(cd.hex_to_rgb(hex_value), dtype="uint8")
        x += w
    return img


def delta_e(a, b):
    la = cd.srgb_to_lab(np.array(cd.hex_to_rgb(a), dtype="uint8").reshape(1, 1, 3))[0, 0]
    lb = cd.srgb_to_lab(np.array(cd.hex_to_rgb(b), dtype="uint8").reshape(1, 1, 3))[0, 0]
    return float(np.sqrt(((la - lb) ** 2).sum()))


class TestLabConversion:
    """教科书基准值。整个探针建在这上面。"""

    def test_white(self):
        lab = cd.srgb_to_lab(np.array([[[255, 255, 255]]], dtype="uint8"))[0, 0]
        assert lab[0] == pytest.approx(100.0, abs=0.01)
        assert lab[1] == pytest.approx(0.0, abs=0.01)
        assert lab[2] == pytest.approx(0.0, abs=0.01)

    def test_black(self):
        lab = cd.srgb_to_lab(np.array([[[0, 0, 0]]], dtype="uint8"))[0, 0]
        assert lab[0] == pytest.approx(0.0, abs=0.01)

    def test_pure_red_matches_the_published_value(self):
        lab = cd.srgb_to_lab(np.array([[[255, 0, 0]]], dtype="uint8"))[0, 0]
        assert lab[0] == pytest.approx(53.24, abs=0.05)
        assert lab[1] == pytest.approx(80.09, abs=0.05)
        assert lab[2] == pytest.approx(67.20, abs=0.05)

    def test_hex_round_trip(self):
        assert cd.rgb_to_hex(cd.hex_to_rgb("#4a5233")) == "#4a5233"

    def test_bad_hex_is_rejected(self):
        with pytest.raises(ValueError):
            cd.hex_to_rgb("#xyz")


class TestTolerance:
    """默认容差必须分得开 banned 表点名的那些对，同时容得下光照变化。

    这些数字就是 cast_drift.DEFAULT_DELTA_E 那段注释里的表；改容差要先改这里。
    """

    @pytest.mark.parametrize("name,a,b", [
        ("olive vs khaki", "#6b6b47", "#c3b091"),
        ("olive vs army green", "#6b6b47", "#4b5320"),
        ("charcoal vs black", "#36454f", "#1a1a1a"),
        ("dark-brown vs tan boots", "#4a3728", "#d2b48c"),
        ("yellow vs orange gloves", "#e6c229", "#e07b17"),
    ])
    def test_banned_pairs_are_separable(self, name, a, b):
        assert delta_e(a, b) > cd.DEFAULT_DELTA_E, name

    def test_same_garment_under_a_lighting_change_is_not_separated(self):
        """同一件衣服 ±12% 亮度不得判成两件——否则每段日照角度一变就报换装。"""
        assert delta_e("#6b6b47", "#7a7a52") < cd.DEFAULT_DELTA_E


class TestSlotCoverage:
    def test_matching_colour_covers_everything(self):
        assert cd.slot_coverage(solid("#6b6b47"), cd.hex_to_rgb("#6b6b47")) == 1.0

    def test_unrelated_colour_covers_nothing(self):
        assert cd.slot_coverage(solid("#6b6b47"), cd.hex_to_rgb("#ff00ff")) == 0.0

    def test_coverage_tracks_patch_area(self):
        img = composite("#202020", [("#6b6b47", 0.25)])
        assert cd.slot_coverage(img, cd.hex_to_rgb("#6b6b47")) == pytest.approx(0.25, abs=0.02)


def _cast(**overrides):
    base = {
        "id": "test-cast",
        "pipeline_mode": "A",
        "tier1_anchors": [
            {"slot": "jacket", "required": "faded olive-green work jacket",
             "banned": [], "reference_srgb": "#6b6b47"},
            {"slot": "gloves", "required": "yellow leather gloves",
             "banned": [], "reference_srgb": "#e6c229"},
        ],
    }
    base.update(overrides)
    return base


class TestUncalibratedIsNotAPass:
    def test_a_cast_with_no_reference_colours_reports_uncalibrated(self, tmp_path):
        cast = _cast(tier1_anchors=[{"slot": "jacket", "required": "x", "banned": []}])
        report = cd.analyze_project([("VIDEO 1", [])], cast)
        assert report["status"] == "uncalibrated"
        assert "reference_srgb" in report["message"]

    def test_calibrated_slots_skips_uncalibrated_anchors(self):
        cast = _cast(tier1_anchors=[
            {"slot": "jacket", "required": "x", "reference_srgb": "#6b6b47"},
            {"slot": "boots", "required": "y"},
        ])
        assert [s[0] for s in cd.calibrated_slots(cast)] == ["jacket"]

    def test_an_invalid_reference_colour_is_skipped_not_crashed(self, capsys):
        cast = _cast(tier1_anchors=[{"slot": "jacket", "required": "x",
                                     "reference_srgb": "not-a-colour"}])
        assert cd.calibrated_slots(cast) == []
        assert "reference_srgb" in capsys.readouterr().out


class TestCrossSegmentVerdicts:
    """跨段判定：这是整个探针的核心逻辑。"""

    def _segments(self, tmp_path, per_segment_hex):
        """每段一张合成图：登记色块占 8%。

        8% 是刻意选的：人物服装在竖幅中景里大致就是这个量级，而且落在
        MIN_PRESENCE(0.2%) 与 MAX_PRESENCE(15%) 之间。早先这里写的是 20%，超过了
        掺场景上限——夹具本身不真实，反而把新加的防护判成了 bug。"""
        from PIL import Image
        segments = []
        for i, hexes in enumerate(per_segment_hex, start=1):
            img = composite("#303030", [(h, 0.08) for h in hexes])
            path = tmp_path / f"seg{i}.png"
            Image.fromarray(img).save(path)
            segments.append((f"VIDEO {i}", [str(path)]))
        return segments

    def test_a_stable_wardrobe_produces_no_findings(self, tmp_path):
        segs = self._segments(tmp_path, [["#6b6b47"]] * 5)
        cast = _cast(tier1_anchors=[{"slot": "jacket", "required": "faded olive-green work jacket",
                                     "reference_srgb": "#6b6b47"}])
        report = cd.analyze_project(segs, cast)
        assert report["status"] == "ok", report
        assert report["usable_slots"] == 1

    def test_one_segment_changing_the_jacket_is_caught(self, tmp_path):
        """第 4 段把橄榄绿换成卡其色——协议第 8 节的头号失败模式「段间换装」。"""
        segs = self._segments(tmp_path, [["#6b6b47"]] * 3 + [["#c3b091"]] + [["#6b6b47"]] * 2)
        cast = _cast(tier1_anchors=[{"slot": "jacket", "required": "faded olive-green work jacket",
                                     "reference_srgb": "#6b6b47"}])
        report = cd.analyze_project(segs, cast)
        assert report["status"] == "drift_detected"
        assert [f["segment"] for f in report["findings"]] == ["VIDEO 4"]
        assert report["findings"][0]["slot"] == "jacket"

    def test_gloves_disappearing_in_one_segment_is_caught(self, tmp_path):
        """「手套时有时无」：模型在动作段自动脱手套，也是登记在案的失败模式。"""
        from PIL import Image
        segments = []
        for i in range(1, 6):
            patches = [("#6b6b47", 0.08)] + ([("#e6c229", 0.05)] if i != 3 else [])
            path = tmp_path / f"g{i}.png"
            Image.fromarray(composite("#303030", patches)).save(path)
            segments.append((f"VIDEO {i}", [str(path)]))
        report = cd.analyze_project(segments, _cast())
        assert [f["slot"] for f in report["findings"]] == ["gloves"]
        assert report["findings"][0]["segment"] == "VIDEO 3"

    def test_a_slot_invisible_everywhere_is_inconclusive_not_drift(self, tmp_path):
        """核心判断：探针看不见 ≠ 每段都换了装。

        参考色标错（或该部位从没入过镜）时，覆盖率在所有段都是零。把它报成 5 段全部
        漂移，就是让人从此忽略整份报告。
        """
        segs = self._segments(tmp_path, [["#6b6b47"]] * 5)
        cast = _cast(tier1_anchors=[
            {"slot": "jacket", "required": "ok", "reference_srgb": "#6b6b47"},
            {"slot": "boots", "required": "标错的参考色", "reference_srgb": "#ff00ff"},
        ])
        report = cd.analyze_project(segs, cast)
        assert report["status"] == "ok"
        assert [f["slot"] for f in report["findings"]] == []
        boots = next(s for s in report["slots"] if s["slot"] == "boots")
        assert boots["usable"] is False
        assert "判不了" in boots["note"]

    def test_a_colour_that_also_matches_the_scene_is_rejected_not_trusted(self, tmp_path):
        """真实成片实测出来的假阳性，这条钉死它。

        白安全帽的参考色同时命中天空：外景段覆盖率 13~20%、近景段 1~2%，于是
        "基线 1.78%、某段塌到 33%" 被报成换帽子，而真正变的只是构图里的天空占比。
        任一段超过 MAX_PRESENCE 就说明这个色不只属于服装，整个部位不可解释。
        """
        segs = self._segments(tmp_path, [["#6b6b47"]] * 4)
        # 再补两段：登记色占了半张画面（模拟"这个色也是天空/墙面"）
        from PIL import Image
        for i in (5, 6):
            path = tmp_path / f"wide{i}.png"
            Image.fromarray(composite("#303030", [("#6b6b47", 0.5)])).save(path)
            segs.append((f"VIDEO {i}", [str(path)]))
        cast = _cast(tier1_anchors=[{"slot": "jacket", "required": "x",
                                     "reference_srgb": "#6b6b47"}])
        report = cd.analyze_project(segs, cast)
        jacket = next(s for s in report["slots"] if s["slot"] == "jacket")
        assert jacket["usable"] is False
        assert "大块东西" in jacket["note"]
        assert report["findings"] == []

    def test_no_usable_slot_is_inconclusive_never_ok(self, tmp_path):
        """一个可判部位都没有 ≠ 通过。

        报 ok 就是用一句"未见漂移"冒充一次真的检查——和它要取代的人眼放水是同一种
        错误，只是换了个更权威的口气。CLI 为此单开了退出码 2。
        """
        segs = self._segments(tmp_path, [["#6b6b47"]] * 3)
        cast = _cast(tier1_anchors=[{"slot": "boots", "required": "标错的",
                                     "reference_srgb": "#ff00ff"}])
        report = cd.analyze_project(segs, cast)
        assert report["status"] == "inconclusive"
        assert report["usable_slots"] == 0
        assert "不是通过" in cd.format_report(report)

    def test_the_report_states_the_baseline_for_every_slot(self, tmp_path):
        """每个部位的基线覆盖率必须出现在报告里——那是读报告的人判断"这个部位可不可信"
        的唯一依据，也是本探针最大盲区（场景同色物体撑高基线）的自查手段。"""
        segs = self._segments(tmp_path, [["#6b6b47"]] * 3)
        report = cd.analyze_project(segs, _cast())
        assert {s["slot"] for s in report["slots"]} == {"jacket", "gloves"}
        assert all("baseline_coverage" in s for s in report["slots"])


class TestReportFormatting:
    def test_uncalibrated_report_says_so_in_plain_text(self):
        cast = _cast(tier1_anchors=[{"slot": "jacket", "required": "x"}])
        text = cd.format_report(cd.analyze_project([("VIDEO 1", [])], cast))
        assert "uncalibrated" in text


class TestCalibration:
    def test_sample_reference_reads_the_patch_median(self, tmp_path):
        from PIL import Image
        img = composite("#303030", [("#6b6b47", 1.0)], size=64)
        path = tmp_path / "frame.png"
        Image.fromarray(img).save(path)
        assert cd.sample_reference(str(path), 0.5, 0.5) == "#6b6b47"

    def test_write_reference_updates_the_registry_in_place(self, tmp_path):
        import json
        registry = {"cast": [{"id": "c1", "tier1_anchors": [{"slot": "jacket"}]}]}
        path = tmp_path / "cast-registry.json"
        path.write_text(json.dumps(registry), encoding="utf-8")
        cd.write_reference(str(path), "c1", "jacket", "#6b6b47")
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["cast"][0]["tier1_anchors"][0]["reference_srgb"] == "#6b6b47"

    def test_write_reference_rejects_an_unknown_slot(self, tmp_path):
        import json
        path = tmp_path / "r.json"
        path.write_text(json.dumps({"cast": [{"id": "c1", "tier1_anchors": []}]}), encoding="utf-8")
        with pytest.raises(KeyError):
            cd.write_reference(str(path), "c1", "jacket", "#6b6b47")

    def test_write_reference_validates_the_colour_before_touching_the_file(self, tmp_path):
        import json
        path = tmp_path / "r.json"
        original = {"cast": [{"id": "c1", "tier1_anchors": [{"slot": "jacket"}]}]}
        path.write_text(json.dumps(original), encoding="utf-8")
        with pytest.raises(ValueError):
            cd.write_reference(str(path), "c1", "jacket", "nope")
        assert json.loads(path.read_text(encoding="utf-8")) == original
