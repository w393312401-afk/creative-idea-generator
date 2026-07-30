# 提示词推进骨架 ↔ 确定性审核 对齐方案

> 状态：四项已落地，**工作区未提交**（分支 `feat/result-slots-refactor`）；四项待办。
> 日期：2026-07-31
> 背景：7/30 那单「废弃木质铁路车厢改造成林间双空间御寒暖阁」11 拍逐拍审核里，
> 11/11 拍报 `jump cut`、6/11 拍报缺料源容器、6/11 拍报 large 档没做全覆盖声明，
> 9 拍被拖进回炉、其中 2 拍回炉未通过保留原稿。逐条追下去，根子不是模型不听话，
> 而是**生成骨架和事后审核不同步**：审核卡的规则，骨架要么没说，要么用了审核匹配
> 不上的措辞，于是模型第一遍必然写错、全靠回炉救。

核心原则：**骨架必须用审核实际匹配的词汇，把审核的每一条讲出来。**
`_milestone_beat_directive` 是唯一出处（批量生成 / 单拍生成 / 回炉重写三条路径都走它），
改一处全线生效。

---

## 一、已落地（未提交）

### D1. `check_transition_shortcuts` 改为逐句否定感知
- 位置：`prompt_pipeline/__init__.py:3445`
- 问题：模板自己会输出 `... or jump cuts are strictly forbidden.`。`clean_prompt_text`
  （`:2460` 起）认得这是规则句、按句做否定感知；旧审核只做整串子串匹配，于是把自家的
  禁令当违规报出来，11/11 拍全中，白白把 9 拍 IMAGE 拖进回炉。
  `:2915` 的注释早就点名过 "the check_transition_shortcuts failure mode"——pan/tilt
  那边修了，它自己没修。
- 现状：与 sanitizer 同源的逐句否定感知 + 同一短语只报一次。
- 测试：`tests/test_transition_shortcut_negation.py`（5 项）

### D2. 里程碑骨架清单化，并压缩到预算内
- 位置：`prompt_pipeline/__init__.py:6883`
- 改法：IMAGE / VIDEO 两段从"塞满从句的长句"改成编号清单，每条对应一个审核函数，
  **用审核匹配的词汇写**——容器名词表、循环措辞、覆盖短语、继承状态提示词。
  按 `stage_scope` 分档反向表述（`large` 要求自有全覆盖声明，`small`/`default` 禁止）。
- 长度预算：原版 1324 字符 → 详版 3728（实测遵守率反而下降）→ **压缩版 2203**。
  压缩手法是**编号项不再复述契约块已有的字段值**，只留审核认的词汇线索和"往上看"的指代。
  这段文本要 ×10~11 拍塞进同一次批量调用，长度会被放大。
- 测试：`tests/test_milestone_directive_mirrors_audit.py`（12 项，含 2600 字符预算护栏
  与一项端到端：按清单写出的 IMAGE/VIDEO 对能一次通过全部四个审核）

### D3. 相机词表统一，消除"审核报得出、清洗器清不掉"
- 位置：`prompt_pipeline/__init__.py:2882`（新增 `_STATIC_CAMERA_AUDIT_PHRASES` /
  `_MOVING_CAMERA_AUDIT_PHRASES`）、`:2951`
- 问题：清洗器只认 `static tripod shot`，审核卡的是裸 `static tripod`；同类缺口还有
  `locked eye-level` / `locked camera` / `forward-pushing` / `crosses the threshold`。
  这类发现**任何回炉都清不掉**，必然活到终稿。
- 现状：两侧共用同一组审核短语常量，清洗器列表恒为审核的超集。
- 测试：`tests/test_camera_phrase_lists_no_drift.py`（21 项，参数化钉死）

### D4. 任务历史误删防呆（事故修复）
- 位置：`server_common.py:1522`（`TASKS_LOADED_FROM_DISK`）、`:2176`
- 事故：2026-07-31 跑全量 `pytest` 时，`tests/test_ledger_activation_registration.py`
  直接调真实 `/api/compose` 处理函数且未隔离工作目录，往项目根 `tasks/` 建了一条内存
  任务；随后 `save_tasks_to_disk` 的孤儿清理认定"内存里只有这一条 ⇒ 磁盘上其余都是
  垃圾"，把 5 个真实任务记录全删了（含 7/30 那单的 `audit_md` / `beat_audit`）。
  `outputs/` 下的帧、视频、合成 mp4、manifest 未受影响；`tasks/` 在 .gitignore 里，
  无 git 副本。旧防呆（`:2166`，2026-07-12 加的）只挡"内存表为空"这一种。
- 现状：孤儿清理新增授权前提——本进程必须成功跑完过一次 `load_tasks_from_disk`；
  并给那个测试补了 `chdir(tmp_path)` 隔离。
- 测试：`tests/test_task_history_not_wiped.py`（3 项，用事故原型钉死）

---

## 二、待办

### T1（P0）IMAGE 回炉单轮上限，是当前残留的唯一瓶颈
- 位置：`prompt_pipeline/__init__.py:4493` `rework_stage_scope_wording_beat`、
  `:4673` `rework_missing_content_image_beat`，均为 "max one round，改不好保留原稿"
- 证据：7/31 三组新载体实测（29 拍）只剩 2 处终稿残留，**两处都卡在 IMAGE 回炉未通过**：
  - 水塔第 4 拍「interior floor cleanout complete」：`stage_scope="large"` 但 IMAGE 没在
    描述自己新增工序的那句里做全覆盖声明
  - 灯塔第 3 拍「exterior to interior threshold crossed」：室内帧提到 `sky` + 首次室内
    揭示只呈现 0 个（要求 3+）衰败类目 + 已写出人工干预痕迹。这三条规则提示词里
    **本来都写了**（`:6584`、`:6667` 一带），属模型偏离既有规则，非骨架缺教
- 建议：给 IMAGE 回炉加第二轮（第二轮把上一轮的失败原文一并喂回去，明确"上次这样改仍未
  通过"），或对可判定的失败类型直接放弃再问模型（见 T2）

### T2（P1）"全覆盖声明"改确定性改写，不必再问 LLM
- 背景：这条失败的本质只是"往描述本拍新增工序的那句里插一个全覆盖短语"，
  判定逻辑已经是确定性的（`_stage_scope_full_coverage_sentences`，`:4436`）
- 建议：定位那句 state-delta 句子后直接插入短语并复核，绕开一次 LLM 往返；
  仅在定位不到目标句时才回退到现有的 LLM 回炉

### T3（P1）评测口径：首轮命中数不可用作质量指标
- 证据：同一题材、同一份代码，"缺重复工作循环"这项在两次跑里是 **0 和 9**；
  把被标记的拍逐条复核，终稿写的全是 `cycle by cycle` / `repeatedly` / `row by row`
  ——首轮命中只反映措辞运气，回炉之后已经修好
- 建议：任何骨架改动的验收都以**终稿残留**为准（把最终 prompt block 逐槽重跑规则），
  首轮命中数只作 token 成本的参考。评分脚本需**豁免奖赏拍**——它本就没有工人、
  料源、工作循环，按里程碑规则打分会稳定误报
- 实测数据（压缩版骨架，纯文本阶段，不渲染）：

  | 题材 | 拍数 | jump cut 误报 | 结构性回炉未通过 | 终稿真实残留 | prompt tokens |
  |---|---|---|---|---|---|
  | 铁路车厢（旧码基线） | 11 | 11 | 2 | 2 | — |
  | 铁路车厢（压缩版复跑） | 11 | 0 | 0 | 0 | 120,592 |
  | 砖砌水塔 | 10 | 0 | 0 | 1 | 128,603 |
  | 高山缆车厢 | 11 | 0 | 0 | 0 | 121,480 |
  | 石砌灯塔 | 8 | 0 | 0 | 1 | 96,129 |

### T4（P2）`tests/test_restore_slot_e2e.py` toast 断言有竞态
- 现象：`test_delete_then_undo_from_the_ui` 在全量套件负载下连挂两次，
  单独跑连过六次。断言是"删除后**立刻**出现撤销出口"（`:160`），未等 toast 渲染
- 建议：改成带超时的等待而非即时断言

---

## 三、验证现状

- 全量 `pytest`：**1504 passed, 1 skipped, 4 subtests passed**
- 新增 4 个测试文件、41 项，全部为回归护栏
- 本轮试跑只走文本阶段（composer + 确定性审核），未渲染任何帧或视频；
  副作用仅 `packet_cache.json`、`compose_checkpoints.json` 的缓存写入
