# 视频人物一致性方案

> 现有资产：[`character-consistency-protocol.md`](../skills/gemini-veo-restoration-composer/references/character-consistency-protocol.md)（Named Cast Lock 协议）、
> [`cast-registry.json`](../skills/gemini-veo-restoration-composer/references/cast-registry.json)（身份块与 T1 词表唯一副本）、
> [`check_character_lock.py`](../skills/gemini-veo-restoration-composer/scripts/check_character_lock.py)（7 道文本门）。
>
> 本方案**不重写**上述内容。它们解决的是"同一组词在每段里逐字重述"，这一层已经做完了。
> 本方案处理的是这一层**够不到的地方**。

---

## 0. 一致性的三个层级（先分清在解决哪一层）

跨段无记忆的流水线里，"人物一致"其实是三个独立问题，现状只覆盖了第一个：

| 层 | 一致性来源 | 能锁住什么 | 现状 |
|---|---|---|---|
| **L1 词表层** | 每段逐字重述固定文本块 | 服装剪影、颜色、胡须形态 —— 观众"认人"的粗特征 | ✅ 已做完，机器可判 |
| **L2 像素层** | 参考图 / seed / image-to-image 把脸钉在图片上 | 脸部结构、五官比例 —— 文本永远描述不精的部分 | ⚠️ 协议里写了"模式 B"，**零工具支撑** |
| **L3 验收层** | 出片后逐帧比对实际画面 | 前两层是否真的生效 | ❌ 完全人眼，14 项 QC 表靠自觉 |

L1 是**必要不充分**的：它保证提示词写对了，不保证模型照做了。
现在从提示词到成片之间没有任何闭环 —— 词表门退出码为 0 之后，人物漂没漂只有人眼知道。

---

## 1. 六个真实缺口

盘出来的、有代码依据的缺口，按危害排序：

1. **唯一执行点是个手动脚本。** 服务端 `prompt_pipeline` 没有具名人物概念（已在
   `contract-registry.json` 的 `named-cast-*` 条目下登记为 `gap`）。聊天内撰写、
   `/api` 出稿路径全程无拦截，只有 agent 记得在交付前跑一次 CLI 才生效。
2. **模式 B 是张空头支票。** 协议第 4、7 节要求参考图 + seed 锁 + image-to-image，
   但仓库里没有生成角色卡的脚本、没有存 seed 的地方、`identity_blocks` 里也没有
   资产字段。选了模式 B 只能纯手工，且下次做同一个角色无法复现。
3. **成片零验证。** L3 全靠 14 项人眼 QC。段数一多必然放水，而且第 1～8 项
   （帽/外套/内搭/裤/靴/手套/胡须/发色）本质是**色块比对**，正是机器该干的活。
4. **Gate 2 绑死施工题材。** `PACING_PHRASE = "continuous construction time-lapse"`
   是硬编码的（[check_character_lock.py:51](../skills/gemini-veo-restoration-composer/scripts/check_character_lock.py#L51)）。
   换任何非施工选题，"身份块前置"这道门直接静默失效 —— 不报错，只是不判。
   这是本方案里**最便宜也最危险**的一条。
5. **两种锁的互斥没人管。** 同一交付集里同时出现 `neon-yellow safety vest`（Hero Agent Lock）
   和具名身份块不会被拦截，已登记为 gap。协议说"混用等于放两个矛盾契约"，但没有执行点。
6. **二创链路完全裸奔。** `replica_pipeline.py` 里 `character`/`cast` 零命中；
   `reverse.py` 的 beat ladder 只有 `visual_subject`，没有人物身份概念。
   `_MUTATE_SYSTEM` 换题材时会把人一起换掉。omni skill 同样没有 cast 概念。

---

## 2. 方案分四阶段

### P0 — 把已有的锁焊死（1～2 天，不引入新依赖）

目标：让 L1 从"记得跑就有效"变成"不跑也躲不掉"，并解掉题材绑定。

- **P0-1 `PACING_PHRASE` 出参。** 移进 `cast-registry.json`，做成 per-cast 的
  `action_onset_markers: [...]` 数组（施工题材保留现值作默认）。同时改判定语义：
  **一个 marker 都没命中时应当告警**，而不是静默跳过 Gate 2。现在的静默跳过让这道门
  在新题材上等于不存在。
- **P0-2 补互斥门（Gate 8）。** registry 增加 `hero_agent_lock_markers`
  （`safety vest`、`hi-vis`、`hardhat`、`solid bright-neon-yellow` …），
  named_cast 模式下命中即 FAIL。填掉已登记的 gap，约 20 行。
- **P0-3 抽出共享模块。** 新建 `prompt_pipeline/cast_lock.py`，把 `check_prompt` 的
  七道（八道）门搬过去，`check_character_lock.py` 退化成薄 CLI 壳。
  **registry 路径仍指向 skill 内那一份**，遵守"唯一副本"铁律，不复制。
  > 注意：与根目录 `contract-registry.json`（server）和 skill 内
  > `skill-local-contracts.json`（CLI）是三套互不相关的东西，别串。
- **P0-4 接进服务端出稿路径。** `server.py` 的提示词出稿接口在
  `agent_lock_mode: named_cast` 时调用 `cast_lock`，违规进 response 的 warnings 而非
  硬拦（先观察一轮误报率再决定是否升级为拦截）。

**验收**：非施工题材的 prompt set 跑 `check_character_lock.py` 能正常判 Gate 2；
故意混入 `hi-vis vest` 退出码为 1；服务端出稿返回里能看到 lock 警告。

---

### P1 — 让模式 B 真的能用（3～5 天，L2 落地）

目标：脸不再由文本承载。这是**唯一**能解决"脸在近景变人"的手段，L1 做到极致也做不到。

- **P1-1 registry 增加 `identity_assets` 段**：

  ```json
  "identity_assets": {
    "sheet_version": "v1",
    "seed": 812394,
    "generator": "google-fx/imagen",
    "aspect": "9:16",
    "frames": {
      "front":   {"library_id": "...", "sha256": "...", "qc_passed": true},
      "profile": {...}, "rear": {...}, "bust": {...}, "hands": {...}
    }
  }
  ```

  没有 seed 和 sha256，角色卡就是一次性的 —— 三个月后要补一段视频，人已经找不回来了。
- **P1-2 `scripts/generate_character_sheet.py`**：读 registry 的 `full` 块，
  按协议第 7 节的五个模板逐字填充并调本地 server 出图，落库后把 library_id / seed
  写回 registry。五张图共用同一句机位光线约束 + 同一 seed，这是"四个角度的同一个人"
  而不是"四个人"的唯一保证。沿用现有 skill 脚本的 stdlib-only + HTTP 客户端形态。
- **P1-3 IMAGE 锚点走 image-to-image**：模式 B 下每张含人物的 IMAGE 锚点把角色卡
  正面图作为参考输入，而不是纯文生图。VIDEO 段靠首尾帧插值，脸由图片承载、文本只兜底。
- **P1-4 模式 B 的 Clean Frame 豁免要有执行点**：协议要求写
  `clean_frame_exemption: named_cast_mode_b`，但服务端 `image-clean-frame` 校验器
  目前不认这个字段，模式 B 会被它误杀。需要在校验器里读这个豁免。

**验收**：`generate_character_sheet.py --cast jake-miller` 一条命令产出五张卡并回写
registry；重跑同一命令（同 seed）产出 sha256 相同的图；模式 B 的 prompt set 能过
Clean Frame 校验。

---

### P2 — 出片后自动测漂移（5～7 天，L3 闭环）

目标：把 14 项 QC 表里能机器判的部分变成退出码。**这是整套方案里价值最高的一段** ——
前两层都是"尽力而为"，只有这一层能告诉你到底成没成。

分两级，刻意分开，因为成本差一个量级：

**P2-a 剪影级（T1 第 1～8 项，零依赖增量）**
帽/外套/内搭/裤/靴/手套本质是**固定位置的色块**。用已有的 Pillow + numpy 就够：

- 抽帧（每段均匀 8～12 帧）→ 人物区域粗定位 → 在 LAB 空间统计主色
- 与角色卡对应部位的参考色算 ΔE，超阈值即判漂
- 仓库里已经有 LAB 颜色匹配的代码路径（`requirements.txt` 注明 Pillow 用于 LAB 颜色匹配），
  可以直接复用，**不新增依赖**

这一级能抓住绝大多数实际事故 —— 协议第 8 节列的失败模式里，"段间换装"、"手套时有时无"、
"胡须变络腮"全是剪影级的。

**P2-b 脸部级（第 9～11 项，需新依赖，可选装）**
人脸 embedding 余弦相似度，与角色卡正面图比对，给 per-segment 分数。

- 必然打破 skill scripts 的 stdlib-only 约束 → **放服务端**（`requirements.txt`），
  不放 `scripts/`
- 依赖缺失时返回 `skipped` 而非报错，但要**在日志里显式吼一声**
  —— `requirements.txt` 里 numpy 那段注释已经踩过这个坑（"静默返回 skipped，
  后果是整套内容级校验悄悄失效而日志上看不出异常"），别再踩一次
- 建议 P2-a 先上、跑一个月看漏检率，再决定要不要 P2-b

**输出**：一份与 14 项 QC 表逐项对齐的报告 + 退出码，接进交付门。

---

### P3 — 覆盖二创与 omni（按需，最后做）

- **replica 链路**：beat ladder 增加 `cast_ref` 字段，`_MUTATE_SYSTEM` 的可变轴
  （`visual_subject`、`visible_details` …）明确**排除**人物身份，换题材不换人。
  > 注意二创用的是 `reference_frames` 方言，按原片口径校验会整条判死 —— cast 校验
  > 接入变体路径时必须走变体口径，不能直接复用 VIDEO/IMAGE 的 slot 切分。
- **omni skill**：目前无 cast 概念。P0-3 抽出 `prompt_pipeline/cast_lock.py` 后
  接进去即可，不需要第二套。

---

## 3. 建议起点

如果只做一件事：**P0-1**（解 `PACING_PHRASE` 硬编码）。
它是半小时的改动，但现在每一个非施工选题的身份块前置检查都在静默失效，
而且失效的方式是"退出码 0、报告干净" —— 这比没有门更危险。

如果做一个阶段：**P0 全套 + P2-a**。
P0 让已有的锁真的锁上，P2-a 第一次给出"成片到底漂没漂"的客观答案，
两者都不引入新依赖。P1 工作量最大且只在"要看脸"的选题里回本，可以等真有这类选题再做。

---

## 4. 明确不做的事

- **不重写 L1 协议。** 词表 + 逐字重述的思路是对的，`check_character_lock.py` 的
  七道门（尤其 `mask_identity_blocks` 那个"禁用词是自身必需词的子串"的处理）质量很好。
- **不把身份块正文复制到第二个地方。** registry 是唯一副本，本方案所有新增代码
  都从它读。
- **不追求"脸 100% 一致"。** 在无记忆流水线里这做不到。目标是让观众认得出是同一个人 ——
  这是剪影级问题（L1 + P2-a），不是像素级问题。P1 只对"脸占画面高度超过六分之一"的
  选题才有必要。
