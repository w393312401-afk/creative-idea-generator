"""体量锁（Shell Envelope Consistency）：外壳的净宽/净高/屋面形式/洞口数量在过门之后
不许再变。

geometry_lock 此前是个孤儿字段——packet 里写了，没有任何一处代码读它，于是室内帧的体量
每一帧都由图像模型自由发挥。2026-08-08 实测崩坏：室内帧凭空长出天窗、拱顶和后墙拱窗，
三样都不属于任何一拍声明过的操作，而且错误沿室内链稳定传播到片尾。

覆盖：
1. 默认禁项无条件生效（packet 什么都没声明也跑）——天窗/拱顶/穹顶/后墙拱窗那几样。
2. 声明豁免：geometry_lock 或 aperture_ledger 里真声明过的形状放行（真的筒拱地窖）。
3. 声明豁免不得被 geometry_lock 自带的否定清单反向触发——规范写法里就写着
   "there is no skylight, no vaulted ceiling"，拿它当声明证据会把整张禁单豁免掉。
4. aperture_denylist 压过声明豁免。
5. envelope_signature 逐帧原样重贴；缺省时静默跳过（老 packet 向后兼容）。
6. 只对 family == 'interior' 生效。
7. 室内帧的 IMAGE 字数硬顶单独一档（image_word_limit_for）。
"""
import unittest

import prompt_pipeline as pp

# SKILL.md Step 6 的 Geometry Lock 规范写法：正向度量 + 否定清单写在同一段散文里。
CANONICAL_GEOMETRY_LOCK = (
    "the interior runs about two and a half door widths wall to wall; about three door "
    "heights to the ridge; four rafter pairs deep; single shallow gable, same pitch as the "
    "outside, no second roof form anywhere; there is no skylight, no roof light, no vaulted "
    "or domed ceiling, no rear-wall arched window, no second doorway; the same tarred board "
    "cladding continues on the inner face"
)

PACKET = {
    "geometry_lock": CANONICAL_GEOMETRY_LOCK,
    "aperture_ledger": [
        "the single plank door in the gable end",
        "two small square vents under the eaves",
    ],
    "aperture_denylist": [
        "skylight", "roof light", "vaulted ceiling",
        "rear-wall arched window", "second doorway",
    ],
    "envelope_signature": "about two and a half door widths wall to wall",
}

CLEAN_INTERIOR = (
    "Generate an image of an interior anchor about two and a half door widths wall to wall; "
    "single shallow gable overhead; the entry is fully behind the camera."
)


class TestDefaultDenylist(unittest.TestCase):
    """空 packet 也要拦住：这几样是图像模型在封闭室内最爱无中生有的东西。"""

    def test_undeclared_skylight_fails_on_an_empty_packet(self):
        errs = pp.check_shell_envelope_consistency(
            "Interior lit by a skylight overhead.", {}, "interior")
        self.assertTrue(errs)
        self.assertIn("skylight", errs[0])

    def test_undeclared_vault_fails_on_an_empty_packet(self):
        self.assertTrue(pp.check_shell_envelope_consistency(
            "Interior with a barrel vaulted brick ceiling.", {}, "interior"))

    def test_undeclared_dome_fails_on_an_empty_packet(self):
        self.assertTrue(pp.check_shell_envelope_consistency(
            "Interior under a domed ceiling.", {}, "interior"))

    def test_undeclared_rear_wall_arched_window_fails(self):
        self.assertTrue(pp.check_shell_envelope_consistency(
            "An arched window on the rear wall throws light across the floor.",
            {}, "interior"))


class TestDeclarationExemption(unittest.TestCase):
    """真有的形状不能被永久卡死——否定清单不许反过来禁掉真墙上的窗。"""

    def test_genuine_barrel_vault_passes_when_geometry_lock_declares_it(self):
        packet = {"geometry_lock": (
            "single barrel vault springing from the side walls, about two door heights to "
            "the crown; the only openings are the entry stair")}
        self.assertEqual([], pp.check_shell_envelope_consistency(
            "Interior with a barrel vaulted brick ceiling.", packet, "interior"))

    def test_ledger_registered_skylight_passes(self):
        packet = {"aperture_ledger": ["one skylight in the north roof slope"]}
        self.assertEqual([], pp.check_shell_envelope_consistency(
            "Interior lit from the skylight in the north roof slope.", packet, "interior"))

    def test_canonical_geometry_lock_does_not_exempt_its_own_negations(self):
        """规范 geometry_lock 里就写着 "there is no skylight, no vaulted ceiling" —— 拿这句
        当"声明过"的证据，会把整张否定清单反向豁免掉，这条门就等于没写。"""
        errs = pp.check_shell_envelope_consistency(
            "Interior anchor about two and a half door widths wall to wall, with a skylight "
            "above and a vaulted ceiling.", PACKET, "interior")
        blob = " ".join(errs)
        self.assertIn("skylight", blob)
        self.assertIn("vault", blob)

    def test_denylist_beats_declaration_exemption(self):
        packet = {
            "geometry_lock": "a skylight sits in the roof",   # 自相矛盾的 packet
            "aperture_denylist": ["skylight"],
        }
        self.assertTrue(pp.check_shell_envelope_consistency(
            "Interior lit by a skylight overhead.", packet, "interior"))


class TestEnvelopeSignature(unittest.TestCase):
    """逐帧原样重贴——只在 IMAGE T+1 写一次、后面靠继承，正是房间悄悄变大的路径。"""

    def test_missing_signature_fails(self):
        errs = pp.check_shell_envelope_consistency(
            "Generate an image of an interior anchor; single shallow gable overhead; the "
            "entry is fully behind the camera.", PACKET, "interior")
        self.assertTrue(any("missing the geometry-lock restatement" in e for e in errs))

    def test_verbatim_signature_passes(self):
        self.assertEqual([], pp.check_shell_envelope_consistency(
            CLEAN_INTERIOR, PACKET, "interior"))

    def test_absent_signature_field_is_backward_compatible(self):
        """老 packet 没有 envelope_signature —— 重贴检查静默跳过，禁项检查照跑。"""
        packet = {k: v for k, v in PACKET.items() if k != "envelope_signature"}
        self.assertEqual([], pp.check_shell_envelope_consistency(
            "Generate an image of a plain interior anchor.", packet, "interior"))


class TestFamilyScope(unittest.TestCase):
    def test_exterior_frames_are_never_touched(self):
        self.assertEqual([], pp.check_shell_envelope_consistency(
            "A skylight and a domed roof above the vaulted portico.", PACKET, "exterior"))

    def test_empty_prompt_is_not_an_error(self):
        self.assertEqual([], pp.check_shell_envelope_consistency("", PACKET, "interior"))


class TestPacketNormalization(unittest.TestCase):
    def test_dict_and_string_shapes_are_tolerated(self):
        packet = pp.normalize_packet({
            "aperture_denylist": "skylight, vaulted ceiling",
            "aperture_ledger": {"a": "the plank door"},
            "envelope_signature": ["about two door widths", "wall to wall"],
        })
        self.assertIsInstance(packet["envelope_signature"], str)
        self.assertTrue(pp.check_shell_envelope_consistency(
            "Interior with a skylight.", packet, "interior"))


class TestInteriorWordLimit(unittest.TestCase):
    """室内帧多背一段体量锁散文 + 门框出画句，180 词装不下；模型会挤掉刚加的那段。"""

    def test_interior_family_gets_its_own_ceiling(self):
        self.assertEqual(pp.IMAGE_WORD_LIMIT, pp.image_word_limit_for("exterior"))
        self.assertEqual(pp.INTERIOR_IMAGE_WORD_LIMIT, pp.image_word_limit_for("interior"))
        self.assertGreater(pp.INTERIOR_IMAGE_WORD_LIMIT, pp.IMAGE_WORD_LIMIT)

    def test_unknown_family_falls_back_to_the_strict_ceiling(self):
        self.assertEqual(pp.IMAGE_WORD_LIMIT, pp.image_word_limit_for(None))


if __name__ == "__main__":
    unittest.main()
