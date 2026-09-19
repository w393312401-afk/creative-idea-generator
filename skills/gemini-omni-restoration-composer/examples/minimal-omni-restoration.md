# Minimal Omni Restoration Example

## User Input

```text
做一个废弃石屋改造成隐藏工作室的 Omni 视频提示词
```

## Expected Output Shape

This is one clearing-beat excerpt, not the complete renovation pack. Its starting anchor already contains the two staged crates; do not add that staging silently to a previously locked raw-site image. The low collapsed eave makes adjacent-edge contact physically reachable without an invented ladder. Apply `../references/omni-work-first-rhythm.md`.

```text
图片提示词
图片 1:
Generate a locked restoration anchor of a low stone hut in a damp forest clearing, captured like an unpolished smartphone still. The cracked doorway stands left of centre, the collapsed front eave reaches waist height beside the right frame edge, and a mossy boulder anchors the lower left foreground; distant trunks remain fixed behind the hut. Seven short broken roof boards lie across the collapsed front eave among loose slate fragments, while the rear roof remains untouched. Two empty rigid dark-green crates sit directly beneath the low working edge, already staged for this clearing beat. Damp leaf piles, fungal wood fibres and open stone joints distinguish the damaged surfaces. Overcast daylight carries a green-grey foliage cast, wet slate catches small blown highlights, and mild edge softness and shadow noise retain the casual phone-capture texture. The eave is reachable from ground level immediately outside the right frame edge. Preserve this composition, material identity and available-light direction for the next anchor.

图片 2:
Generate the same locked smartphone restoration anchor of the low stone hut in the damp clearing. Preserve the cracked doorway left of centre, the waist-high collapsed front eave beside the right frame edge, the mossy boulder in the lower left foreground and the distant tree line. All seven short broken boards have been removed from the front eave and now lie as countable pieces in the two rigid dark-green crates still directly beneath it, with the loose slate fragments captured between them. The cleared front eave exposes porous stone, fresh pry scars and a pale dust boundary; the rear roof and all unworked surfaces remain unchanged. Retain the damp leaves and fungal wood texture outside the cleared zone. Overcast light, the green-grey foliage cast, slight off-centre framing, soft phone lens edges and faint compression in the shadowed moss match the preceding anchor. No additional clearing, furniture, finished roof covering or new equipment has appeared.

视频提示词
视频 1:
Use IMAGE 1 as the first-frame anchor and IMAGE 2 as the last-frame anchor; preserve the low stone hut, clearing, lighting and locked landmarks while showing only front-eave debris clearing. The clip opens on a wide working shot recorded like casual phone footage, matching the person-free IMAGE 1 with the waist-high eave beside the right frame edge. Immediately afterwards one lone worker in a solid yellow vest and dark-blue trousers leans in from off-frame at that adjacent edge, feet on the ground outside the frame, placing a matte-black steel pry bar beneath the first short board without pausing. The first board separates in full view from pry-bite to lift-away and is lowered directly into a waiting green crate beneath the eave. The worker repeatedly pries and lowers boards one by one, dropping accompanying slate fragments into the same two crates; the exposed front edge grows while the captured load rises, with no walk to a distant disposal point. About five boards are removed before the cutaway. A clean cut to a close-up insert shows the pry bar flexing broken fibres at an already-worked edge, with slight handheld blur and clipped highlights on wet slate. An extreme close-up insert examines fresh pry scars and the pale dust boundary in the same cleared area; faint shadow compression preserves the phone texture, and neither insert advances completion. Cut back to a returning wide shot from the same camera setup as the opening wide working shot. Resume at the same completion level, then compress the last two board-removal cycles the same way. The worker lowers the final board directly into its crate and withdraws the pry bar and all visible hands and torso behind the adjacent right edge in that same motion. The final instant matches the person-free IMAGE 2: all seven boards and slate fragments remain captured in the two crates, and the exposed eave retains its pry scars and dust boundary. No packing-up, departure walk or empty hold is added. Edited construction time-lapse assembled from multiple camera setups, not real-time footage. Inside every shot the frame keeps moving from its first to its last moment — handheld drift, ambient motion, and the subject's own action never freeze — while this beat's change advances only during the work shots. The only compressions in the clip fall exactly on the listed cut marks; no shot contains a hold, a stall, or a deferred step that is then delivered all at once. Tool creaks and short wood-and-slate knocks follow contact over damp forest wind; no music, captions or rendered text.
```

| 审核项 | 状态 | 说明 |
|---|---|---|
| 多镜头结构 | 通过 | wide working shot、close-up insert、extreme close-up insert、returning wide shot；首末镜同机位。 |
| 相邻锚点与动作边界 | 通过 | 首帧瞬间无人，随后工人从紧邻右边缘探身接触低檐，末次落料顺势撤回可见身体与撬杆；没有独立进退场或空镜尾巴。 |
| 单操作与施工信息 | 通过 | 只清理前侧低檐；主过程展示撬离与落料，特写证明材料阻力与撬痕，不加入其他施工任务。 |
| 可达性 | 通过 | 作业面在右画缘且仅及腰高，工人站在紧邻画外的地面上，无跨房间伸臂或凭空登高。 |
| 物料与数量连续性 | 通过 | 起帧已有两个空箱，七块短板与碎石板逐次进入箱内；末帧两个满箱原位保留，不凭空消失或强行搬出。 |
| 进度与因果痕迹 | 通过 | 第一块板完整撬离并落箱；特写不增加完成量，返回时接续原进度再压缩同样的最后两次动作；保留撬痕和粉尘边界。 |
| UGC与声音 | 通过 | 保留手机边缘柔化、阴影压缩、湿石板高光和随接触发生的木石撞击声。 |
| 多模态引用 | 不适用 | 本例没有外部素材标签。 |
| 对话微调提示词 | 不适用 | 用户未明确要求，故不附加。 |

## Validation Notes

- Only the person-free boundary instants are empty; the working shots carry the operation.
- Installed or retained objects are not removed to satisfy a clean-frame rule.
- Changing the next anchor to exclude the loaded crates would require visible removal with a credible path, not merely deleting them from the final paragraph.
- For another trade, change the opening contact, evidence and final gesture rather than copying this pry-and-drop choreography.
