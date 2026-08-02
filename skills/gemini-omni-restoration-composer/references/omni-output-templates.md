# Omni Output Templates

Use these templates as structural guidance. Final prompt bodies must be natural prose, not field-labeled forms.

## Length Targets

Cut coverage needs more room than a single-shot prompt, but unbounded prompts dilute
attention and start dropping structural elements. The VIDEO budget therefore scales with the
shot count, which is itself set by clip length (see `omni-multishot-language.md`):

| Slot | Target | Hard ceiling |
|---|---|---|
| IMAGE | 120–200 words | 220 |
| VIDEO — per shot | 45–70 words | — |
| VIDEO — whole prompt | `55 × shots + 175` words | `55 × shots + 235` |
| Conversational edit | 30–60 words | 90 |

Which works out to:

| Clip length | Shots | Draft the shots to | VIDEO target | VIDEO ceiling |
|---|---|---|---|---|
| 4s | 3 | 250 | 340 | 400 |
| 6s | 4 | 305 | 395 | 455 |
| 8s | 5 | 360 | 450 | 510 |
| 10s | 6 | 415 | 505 | 565 |

The target and ceiling **include** roughly 130 words of structural sentences that are
appended after the body is drafted: the anchor-binding sentence, the shot timeline, the
pacing declaration, and the in-shot continuity sentence. Draft the shot prose to the third
column, not to the target — a body written up to the ceiling gets trimmed from the middle,
which is exactly where the work shots live.

If a prompt runs long, trim in this order:
1. redundant adjectives and doubled descriptors
2. restated boilerplate already established in an earlier slot
3. secondary scene description outside the work zone

Never trim a required structural element to fit: the anchor-binding sentence, the shot
timeline sentence, the shot scales, the worker silhouette phrase, entry and exit paths, the
tool description, the two persistent traces, the pacing declaration, the in-shot continuity
sentence, the no-text sentence, or the audio clause. If those do not fit, the beat is
overloaded and should be split.

## Notation Ban

Model-facing prompt bodies are prose read by a video model, not a spec sheet. These fail
before delivery:

- percent symbols and numeric ranges — `70%`, `10% to 90%`, `40cm to 0cm`
- arabic digits for counts — write `three roof beams`, never `3 roof beams`
- grid or coordinate notation — `Grid B2`, `x-axis 0.35`
- colons introducing a value inside a descriptive sentence
- internal acronyms and labels of any kind

Counts are still mandatory (see Countable Inventory in `omni-restoration-continuity.md`) —
they are simply written as English words. Progress is expressed in spatial language, not
numbers: `from bare concrete across the left third, to covering the whole floor`.

Rationale: digits and symbols in a prompt are a leading cause of the model rendering
literal text overlays into the frame.

### Timecode Exemption

The shot timeline sentence is the **only** place in a prompt body where arabic digits may
appear, and only as `0.0`-style second marks:

```text
Cut this ten-second clip on these marks and hold no other cuts — an establishing long shot from 0.0 to 1.6, ... and a wide outro shot from 8.3 to 10.0 seconds.
```

Everywhere else the ban stands, including inside the per-shot sentences, which restate their
cut marks in English words (`at the one-and-a-half-second mark`). The no-rendered-text
sentence is mandatory in every prompt that carries a timeline.

Rationale for the carve-out: without stated cut marks the model picks its own and the named
shot scales collapse into two long ones, which is a larger and more certain failure than the
overlay risk the ban guards against. If overlays do appear, the fallback is a word-form
timeline (`about one and a half seconds each, with the medium shot running longest`), which
costs cut precision but carries no digits.

## Fenced Block Format

```text
图片提示词
图片 1:
Generate an image of ...

图片 2:
Generate an image of ...

视频提示词
视频 1:
Use IMAGE 1 as the first-frame anchor and IMAGE 2 as the last-frame anchor. ...

对话微调提示词
编辑 1:
Keep the same location, anchor images, shot order, and physical operation, but ...
```

## IMAGE Prompt Pattern

```text
Generate an image of a locked restoration anchor for Gemini Omni, captured like an unpolished smartphone still from casual UGC worksite footage, with slight handheld framing imbalance, mild wide-angle edge distortion, small blown-highlight patches near [bright source], and faint phone sensor noise in the darker corners. The camera frames [carrier] in [location] from [shot scale and phone-camera feel], with [three stable landmarks] held in the same positions for the whole sequence. The scene is in the [before/progressive/final] state: [state details with realistic weathering, dust layers, uneven surface scuffs, and material grain], with [concrete spatial completion extent, e.g. the left two-thirds of the wall primed while the right third stays bare], [explicit counts of major countable elements, e.g. three exposed roof beams and six stacked wall panels], and all permanent changes and traces from earlier beats still visible: [inherited traces such as pry scars, screw heads, dried roller stipple, staged material stockpiles]. No active workers, machines, captions, subtitles, floating labels, or rendered prompt words appear. The lighting is [specific available light source with phone auto-exposure behavior, realistic shadows, and ambient occlusion], the visual style is [UGC documentary realism with tactile material detail], and the physical materials retain [specific texture, roughness, traces, compression artifacts, or color cast]. Keep this image usable as a stable first or last frame for adjacent Omni video generation.
```

## VIDEO Prompt Pattern

Each VIDEO prompt should be one compact English paragraph. It must carry the shot timeline
sentence and then every shot of this clip's ladder in sentence form. The pattern below is
the 10s / six-shot case; shorter ladders drop the rungs named in
`omni-multishot-language.md` and fold their duties into the neighbouring shot.

```text
Use IMAGE N as the first-frame anchor and IMAGE N+1 as the last-frame anchor; every shot must preserve the same location, object identity, lighting direction, and physical layout while showing only the single operation of [operation]. Cut this ten-second clip on these marks and hold no other cuts — an establishing long shot from 0.0 to 1.6, a full shot from 1.6 to 3.1, a medium shot from 3.1 to 5.3, a close-up from 5.3 to 6.9, an extreme close-up from 6.9 to 8.3, and a wide outro shot from 8.3 to 10.0 seconds. The sequence opens with an establishing long shot captured like casual smartphone footage, slightly off-center with mild wide-angle edge distortion and phone auto-exposure settling as it matches IMAGE N, showing [environment and carrier] under [available lighting]. A clean cut moves to a full shot with handheld phone sway as [worker/machine] enters from [path] carrying [tool/material] while leaning slightly into its physical weight, [setting a ladder or scaffold against the work face if the task is above arm reach,] with a small framing correction as the subject crosses toward [work zone]. A match cut moves into a medium shot where the actor repeatedly [verb] [surface/object] with [specific tool], the first [board/stroke/fastener] shown coming together in full from contact to placement, tensing their muscles with each physical stroke as the changed area grows from nothing to about three quarters of this beat's target while fine dust or debris settles nearby and the phone camera briefly breathes focus before locking again. A close-up isolates [tool contact and raw material physics], with minor motion blur, imperfect focus falloff, and small blown highlights on [bright material/source], capturing the material deformation as [force] bends timber fibers, showers rust flakes, or sprays fine dust, leaving [visible trace]. An extreme close-up lingers on the high-detail textures and evidence left behind: [trace one] and [trace two] remain visible in IMAGE N+1, with low-light noise or compression in shadow areas and natural scratches, wood grain, or concrete porosity on the [surface] texture. A final clean cut, after the remaining [repetitions] are completed the same way, returns to a wide outro shot, matching the exposure and phone-recorded tone of IMAGE N+1, where [worker/machine/tool] exits through [path], temporary tools leave the frame, and the empty completed state matches IMAGE N+1. Every shot opens at the progress level the previous shot ended with, and progress advances only during visible work. Everything visible in the final wide shot already exists in IMAGE N+1, and everything in IMAGE N+1 has an on-screen or stated origin inside this video — no overshoot and no missing elements. Use [available-light exposure dynamics], [UGC documentary realism with tactile material detail], and [location detail]. Keep the scene free of captions, subtitles, floating labels, UI text, and rendered prompt words. SFX and ambient noise follow the visible action.
```

## Conversational Edit Pattern

Write two or three edit prompts after the base pack. Each must preserve continuity and be executable as a follow-up Gemini Omni instruction.

Good edit prompts:

```text
Keep the same IMAGE anchors, location, six-shot order, and physical construction result, but change the installed panels to matte white ceramic while preserving the same screw heads, seam shadows, and dust edges.
```

```text
Keep the same shot order and all object positions, but sync each close-up impact to the beat from <audio>, with hammer taps and gravel drops landing on the strongest pulses.
```

```text
Keep the environment, workers' paths, and final anchor unchanged, but make the lighting warmer by adding practical lamp glow that gradually strengthens only after the visible fixture is installed.
```

## Chinese Audit Table

Immediately below the fenced block, append:

```markdown
| 审核项 | 状态 | 说明 |
|---|---|---|
| Omni六维场景骨架 | 通过 | ... |
| 多镜头轮换 | 通过 | ...（说明本片长对应的镜头梯齐备且按序、无梯外的额外景别、不重复，切点时间线句在位且与片长一致，无一镜到底措辞，含时间基准声明与镜内连续性声明） |
| 相邻锚点绑定 | 通过 | ... |
| 单操作Beat | 通过 | ... |
| 可见里程碑包 | 通过 | ...（说明每个 beat 终结于一个有名字的阶段产物且达到完整声明范围/数量，非局部补丁；同区域捆绑动作不超过三个且共同服务同一终点） |
| 施工顺序与工序依赖 | 通过 | ...（说明 beat 顺序符合 拆除→结构→水电→天花板→墙板→饰面→地板→灯具→家具 的依赖链，无硬性否决项） |
| 供电链完整性 | 通过 | ...（说明任何灯具点亮前均有更早的布线 beat 且在封板之前；离网载体另有可见电源安装 beat） |
| 封闭空间成因 | 通过 | ...（说明壳体开启后露出的内部空间已声明为既有空腔，或自带开挖清渣 beat；内部体积与外壳相符） |
| 体积守恒 | 通过 | ...（说明容器规模/趟数/渣堆增长与移除量匹配；整块切除件有独立撬出与搬出动作） |
| 累积状态与锚点差异 | 通过 | ...（说明每个锚点继承之前所有永久改动，被遮挡对象以显式保持句留存而非省略，相邻锚点差异仅为当前操作结果） |
| 进度流动控制 | 通过 | ...（说明单视频形变量在增量预算内，镜头级进度锁生效，cut 不携带进度，首件在镜头内完整发生，压缩仅省略已示范的重复且在文字中点明，状态无回退） |
| 空间锚定与相对位置锁 | 通过 | ...（说明三个深度带各一个主地标跨全包锁定，远/全/收尾三镜齐现，中近特至少含一个地标；易漂移次要物体已锁到最近主地标） |
| 相机姿态与手持容差 | 通过 | ...（说明分镜族措辞正确——封闭室内未提地平线/天空；手持自由度未导致地标出框或相对关系改变） |
| 可数清单与Reveal零新增 | 通过 | ...（说明主要可数元素有明确数量且跨镜头跨锚点一致，数量以英文单词书写，最终 reveal 不含未经安装的新物体） |
| 物体持久化三态 | 通过 | ...（说明每个物体处于 原位继承／人工移动（含目的地）／人工搬出（含动作）之一；单条视频新增支持物类不超过一个） |
| 因果痕迹 | 通过 | ...（说明痕迹与当前工序匹配，例如 滚涂→roller stipple） |
| 工人身份锁与进出通道 | 通过 | ...（说明工人以纯色轮廓描述且跨镜头跨视频一致、不露脸；镜头二进场路径与镜头六退场路径均已命名，镜头一与镜头六无人） |
| 驻场设施生命周期 | 通过 | ...（说明脚手架/模板/支撑有命名搭设 beat、跨锚点持续存在、有命名拆除 beat 并留痕；未凝固混凝土仍有模板支撑） |
| 物料与登高合理性 | 通过 | ...（说明大宗材料有进场/堆料，废料有去向，湿作业在下一锚点呈干燥态，登高作业有梯子或脚手架） |
| 前置创伤描述质量 | 通过 | ...（说明 IMAGE 1 采用 位置+表面材质状态+损伤类型 三段式，未使用 worn/aged/dirty 等软词，损伤跨多个深度带） |
| 灯光相位阶梯 | 通过 | ...（说明全包一套灯光逻辑，逐锚点仅保持或 +1，相位推进在视频内有可见物理成因） |
| 被动环境层 | 通过 | ...（说明方向跨全包锁定、逐条视频仅递进观察细节；封闭室内未出现天空/云/天气） |
| 音效与环境声 | 通过 | ...（说明音效与当前工序匹配并点名发声材质；最终奖励含地面材质脚步声） |
| UGC去AI画面层 | 通过 | ... |
| 反射面处理 | 通过 | ...（说明反射面为低光泽重失焦，无镜面级清晰倒影） |
| 画面密度与纵深分层 | 通过 | ...（说明 wide/establishing 含前景/中景/背景三层,大平面已破碎,环境陈设层跨锚点冻结、惰性、且在工作区外,密度未遮挡操作点因果） |
| 措辞去重 | 通过 | ...（说明必需结构句逐字复用，其余句式、从句顺序、动词集合逐 beat 变化） |
| 记号禁用与字数 | 通过 | ...（说明正文无百分号、无坐标记号、无内部缩写；阿拉伯数字仅出现在切点时间线句里，其余计数一律英文单词；各槽位字数在按镜头数缩放后的目标区间内） |
| 多模态引用 | 通过 | ... |
| 文本渲染禁用 | 通过 | ... |
| 门槛桥协议 | 不适用 | ...（跨门槛时才填：拆为两条桥视频 + 共享门槛交接帧；窥见地标为既有物且继承为室内主地标；占比单调放大；曝光渐滚无突跳；跨槛系绳存在；桥内无施工） |
| 对话微调可执行性 | 不适用 | ...（仅当用户明确要求对话微调提示词时填写） |
```

Rows marked 不适用 are kept in the table with that status rather than deleted, so a reader
can see the gate was considered and did not apply.

Do not put the audit table inside the fenced `text` block.
