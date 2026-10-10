# 交付数据与验证合同（version 1）

需要结构化保存、验证、导出完整提示词时读此文件。它定义轻量本地 JSON；不依赖其他工程或旧合成器。字段以外的观察、创意、空间约束可直接增补，验证器不删除、不重排、不替模型作语义判断。

## 入口与模式

```sh
python3 scripts/validate_beat_package.py /absolute/task/package.json --report /absolute/task/validation.json
python3 scripts/validate_beat_package.py /absolute/task/package.json --export-dir /absolute/task/delivery-v1
```

以上脚本路径相对技能目录。相对媒体路径以 `package.json` 所在目录为基准；`evidence_file` 内的相对帧路径以证据索引所在目录为基准。

顶层字段：

| 字段 | 约定 |
|---|---|
| `version` | 数字 `1` |
| `title` | 非空标题 |
| `mode` | `reverse` / `ideas` / `prompts` / `full` |
| `source` | `{path,duration_sec}`；从零原创为 `null` |
| `evidence_file` | 证据索引路径，未使用源片证据时为 `null` |
| `observed_beats` | 观察阶梯数组，不因生成展开而改写原片事实 |
| `variants` | 创意对象数组，各项至少有唯一 `id`；内容依创意手册填写 |
| `production_segments` | 生成阶梯数组 |
| `images` | 状态图数组 |
| `spatial_contract` | 下述空间合同 |
| `render_review` | 下述实际渲染复核记录 |

四个产物数组均保留键，没有请求该产物时填 `[]`。`reverse` 只要求源片与观察，不强迫产出提示词；`ideas` 要求非空创意；`prompts` 要求完整生成链；`full` 要求生成链，且有源片时要求观察阶梯。原创可 `source:null`，不能把设计写入 `observed_beats`。

## 观察与证据

每拍至少有：

```json
{
  "id": "B01", "start_sec": 0.0, "end_sec": 0.08,
  "space_id": "entrance", "action": "成品外观短暂闪现",
  "state_before": "成品外观", "state_after": "成品外观",
  "evidence_frame_ids": ["F000001"], "uncertainties": [],
  "evidence_review_status": "reviewed", "candidate_ids": []
}
```

`id` 按原片顺序从 1 连续编号。节拍无最短时长、无固定数量、无必须多动作或必须施工痕迹的规则；重复动作、短闪帧、无施工的揭晓均合法，前后状态也可以相同。

证据索引使用抽帧工具的 `version:1`、`source.duration_sec`、`frames`、`candidates`。每帧有唯一 `id`、实际文件 `file`、`requested_time_sec`、`actual_time_sec`。验证器检查实际文件存在，并使用实际解码时间判定是否落在节拍内，不能拿请求时间替代。当有 `pts_time_sec/pts_time_raw` 时检查 `actual_time_sec = pts_time_sec - source.time_origin_sec`，容差 `1e-5` 秒；该算术检查仍不能独立证明像素与元数据对应。

证据索引有 `source.path` 时，与包中源片路径按各自基准目录解析后必须一致，避免错绑同长度的另一视频；旧索引缺少该字段时保留警告。

确需相邻边界帧时加 `boundary_tolerance_sec`（最多 0.5 秒）和非空 `boundary_reason`。这不是放宽整片抽帧误差的开关。原片全时长须由观察拍及可选 `coverage_explanations:[{start_sec,end_sec,reason}]` 联合覆盖；结尾字幕等明确解释，不能悄悄漏段。并发动作允许区间重叠。

候选每项 `id/time_sec/score/review_status`。`pending` 与 `unreviewed` 均表示待审；接受为 `accepted`，需由一拍或多拍的 `candidate_ids` 引用；`rejected` 或 `motion` 必须有 `reason`。允许一个候选映射多拍、一拍引用多个候选、没有候选但画面有语义变化。不要求候选数等于节拍数。

拍的 `evidence_review_status` 缺失或为 `unreviewed` 时给待审警告；证据索引仍未审或任何候选待审也给警告。仅实际看过对应画面才可填写 `reviewed`。脚本只校验该声明是否齐全，不替人看图。

## 生成阶梯与提示词

图片项为 `{id:1,prompt:"完整英文正文"}`。视频项：

```json
{
  "id": 1, "source_beat_ids": ["B01"],
  "origin": "observed_adaptation", "operation": "该段可见变化",
  "start_image_id": 1, "end_image_id": 2,
  "prompt": "Complete English prompt body for this segment.", "duration_sec": 10
}
```

`images/production_segments` 的规范 id 为从 1 连续的整数；兼容一致前缀的尾数字字符串，但一个类别保持一致。线性 N 段对应 N+1 状态图；第 i 段引用第 i 与第 i+1 图，禁止跳号、空提示词和“同上”“TODO”等待填内容。脚本只能捕捉明显占位符，完整性仍需按输出手册检查。

状态图提示词默认无人。脚本对含人物名词（man、worker、person 等，否定写法如 “no people” 不计）的图片提示词给出 `image_prompt_people` 警告；只有用户明确要求人物入图时才在该图写 `people_allowed: true` 并在审核记录说明。警告不阻止导出，也不代表其余文本一致性已检查，见空间手册的文本一致性自检。

实际渲染入口只能绑定首尾图、为落实用户要求的人物一致性而使用带人物的动作边界时，也可填写 `people_allowed:true`，记录该适配原因、所对应的无人空间状态与实际提交资产，详见 Omni 手册。它不改变纯文本包默认无人状态。任务可另存 `space_state_assets` 与 `reference_bindings`；这些扩展的语义和真实绑定须人工核查，验证器不会替工具上传图片。

`origin` 为 `observed_adaptation`（需非空来源）、`creative_design` 或 `generation_expansion`（另填 `expansion_reason`）。多段可以来自同一原拍。生成时间与原片时间分开记录。原拍是主骨架，允许在其上改进或重构；每个原拍必须有生成映射，或在 `production_coverage_explanations:[{source_beat_id,disposition:"reuse"|"omit",reason}]` 说明后期复用或创意省略。不得为凑映射改写原始观察。

视频可另写 `kind:construction/bridge/reveal`，省略为普通施工。实际跨空间段写 `bridge`，导出器会保留 `视频 N [BRIDGE]:` 标记供现有项目识别；最终奖励写 `reveal`。镜头性质由语义判断，脚本不从正文猜测。

## 视觉叙事扩展（人工审核，兼容 version 1）

创意、优化和完整提示词按 [视觉叙事与回报](visual-storytelling.md) 保存设计；可写在任务 Markdown 中，也可用下列可选字段，不为旧包补空字段或强制升级版本：

- 顶层 `visual_plan`：`hook` 记录首屏具体冲突、焦点和兑现方式；`work_scope` 记录真实作业边界、主机位可见范围、前中后变化及设计来源；`payoff` 记录终景目标、人物是否出现及理由。
- 每段 `visible_change`：变化区域、前后差异、返回宽镜时允许的完成状态、到达尾态的可见操作。桥接／揭晓记录新空间信息或已有成果的新视角，不编造施工。
- 顶层 `editorial_review`：人工检查记录，包含实际审阅范围、问题时间／帧号、证据事实、编辑判断或偏好、修改与复核状态。未渲染时只检查设计，不声称画面通过。

验证器不解析这些字段的语义，也不会把它们自动写入导出的正文。首屏、工作面、特写进度与无人终景等本段关键约束必须展开到相应图片／视频 `prompt`；审核说明另放 `审核记录.md`。结构有效不证明吸引力、可见变化或留存提升。

## 空间比例与冲突隔离

```json
{
  "status": "planned", "reference_height_m": null,
  "reference_height_provenance": "designed: actor height is H; metres unknown",
  "spaces": [{
    "id": "room", "shape": "circular",
    "dimensions_m": {"width": null, "length": null, "height": null},
    "provenance": "designed: preserve one circular envelope",
    "relative_constraints": ["Interior clear height 1.45 H; same actor H throughout."]
  }],
  "checks": [{"id": "design_scale", "scope": "design", "status": "unverified", "reason": "Relative design set; rendered proportions not inspected."}]
}
```

`status` 为 `unplanned/planned/reference_conflict`。纯观察允许 `unplanned`；生成需已规划的空间。所有尺寸为正米数或明确 `null`，每空间有 `width/length/height` 键，可另有 `diameter`。`provenance` 与人物 `reference_height_provenance` 用文字写明来自测量、估计、原创设计或未知。未知米数可用 `relative_constraints` 明确 H 比例，不阻止完整文本交付；会保留待审警告，不冒称已验证。

空间、门洞、家具、相机、朝向、施工层厚度等详细定义依空间手册增补。可选 `contained_rect_m:{width,length}` 只用于已明确圆形容器中完全内含的矩形；给出直径时机器检查矩形对角线不超过圆直径，不把规则套到其他形状。

### 人物合同与逐段引用（兼容 version 1）

新制作的有人提示词包在 `spatial_contract` 中保存 `actors`，无人方案可填 `[]`。旧包没有此键时保留原验证行为，不强制重写历史交付。人物外观和状态的语义要求见 [人物整体一致性](character-consistency.md)。

```json
{
  "actors": [{
    "id": "worker_A",
    "reference_source": "designed: character reference planned, no image generated or uploaded yet",
    "appearance": "Adult with a narrow face, short black hair and medium brown skin; lean build; dark blue work jacket, tan trousers and brown boots.",
    "height_m": null,
    "height_provenance": "designed: H_A is the scale unit; actual metres unknown",
    "state_baseline": "Jacket fastened; sleeves down; no gloves; dry clothes at the start."
  }],
  "reference_actor_id": "worker_A"
}
```

`id` 为唯一非空字符串；`reference_source/appearance/height_provenance/state_baseline` 均为非空正文，`height_m` 必须显式为正米数或 `null`。引用未制作的参考时明确 planned，不冒称有文件或已上传。多人分别定义外观和 H；`reference_actor_id` 可选，用于声明全局 `reference_height_m`／H 对应哪个人物。只在明确指向或仅有一个人物时比较该人物与全局米数；多人未指向时不猜测或强制等高。未知身高仍可用明确的相对关系完整交付。

有非空 `actors` 时，各视频项写 `actor_ids:["worker_A"]`，无人段写 `[]`；遗漏给待审警告。图片需要人物时也写 `actor_ids`，并按用户请求设置 `people_allowed:true`。只要声明引用，就检查其数组类型、非空字符串、重复与未知人物；非法引用是错误。默认无人图片声明了人物而未明确允许时给警告。显式空人物表不能引用不存在的人物。

人物状态变化、机位与尺度参照可增补在段落和审核记录中。字段不会自动拼进导出提示词，正文仍须自含本段相关的识别特征、服装状态及尺度关系。脚本仅验证结构、人物引用和已声明身高的一致性，不识别脸、衣服或画面比例，不判断文本特征是否足以维持人物一致。

`checks` 项有唯一 `id`、`status:pass/fail/unverified`、非空 `reason`，可附 `evidence`。`scope` 为 `source/design`，默认 `design`。生成方案的 `fail` 拒收；原片 `source` 冲突保持 `fail`，不会逼迫篡改原片记录。生成需给 `corrections:[{source_check_id,resolution}]`，说明对应的设计修正。`reference_conflict` 状态同样需修正记录才可交付生成链。修正记录保留原片检查的失败状态。

## 渲染、报告与导出

只制作文字默认 `render_review:{status:"not_run"}`，可照常交付全文。实际渲染为 `fail` 时拒绝标作验收通过。声明实际渲染 `pass` 必须记录：

```json
{
  "status": "pass", "reviewed_frames": ["review/frame-01.png"],
  "review_note": "说明实际观察的方法与覆盖范围。",
  "checks": [{"id": "pixel_scale", "status": "pass", "reason": "描述画面中检查到的关系。", "evidence": ["review/frame-01.png"]}]
}
```

实际复核的帧文件须存在，每条画面检查须引用 `reviewed_frames` 中的帧，检查全部通过，设计空间检查也全部通过。有人画面分别记录身份外观、人体尺度和空间连续性的复核，使用 `identity_continuity/actor_scale/space_continuity` 作为检查 ID，或给自定义 ID 填同名 `category`；已声明人物表且实际引用人物的包，标记渲染 pass 时缺少任何一类会报错。旧包未声明人物表、或实际无人方案不强造人物检查。必须另写实际画面检查，不能拿文字设计检查代替。抽查哪些时刻、镜头、空间关系依人物和空间手册执行；脚本不能知道被列出的图片是否足以覆盖这些要求，填写三类检查仍不等于机器识别了脸或几何。

报告分 `structure_status`、`overall_status`、`evidence_review_status`、`render_review_status`。`structure_status:pass` 只代表结构和已声明数值没有错误；有警告时 `overall_status:needs_review`。即便无警告也仅 `structure_valid`，从不把脚本结果命名为画面通过。未运行渲染保持 `not_run`。

退出码：`0` 无结构错误（仍可能待审），`1` 包含错误，`2` 输入读取、JSON、工具或导出异常。`--export-dir` 仅无错误时执行，输出 `完整提示词.txt`、`图片提示词.txt`、`视频提示词.txt`、`逐段复制/`、`时间索引.tsv`。逐段复制包含单独图片、视频及每段起图+尾图+视频的全套文本。时间索引同时列素材时轴、原拍 ID 和原片时间范围。所有正文直接来自已有完整内容；已有同名目标文件时拒绝覆盖，请选择新交付目录。审阅警告不混进导入正文，另交审核报告。
