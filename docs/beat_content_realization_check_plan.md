# 拍级"图文内容脱节"校验方案

> 状态：方案待实施，未动代码。
> 日期：2026-07-22
> 背景：喀斯特溶洞案例实测——第10拍（家具陈设）的 VIDEO 明确写了"搬入躺椅、石桌、盆栽"，
> 但配套 IMAGE 11 正文完全没提家具，只写了一句空洞的"最终照明稳定已经在所有内墙、拱顶和
> 地板上全部完成"（借用了前面几拍的旧名词拼出的假完工声明）。这是同一次生成里 VIDEO 和
> IMAGE 自相矛盾——模型把"要装的东西"写进了动作描述，却没把"装完之后的样子"写进画面描述。

前置：本次诊断依赖当天已经落地（但**尚未提交 git**，仍是 `refactor/structure` 分支工作区里的
未提交改动）的两处小修：
1. `_stage_scope_ladder_violations` 改成按"同工序连续拍段"分配 large/small/default 配额
   （而非全局只给1拍 large）。
2. `check_stage_scope_wording` 收紧成只认"这拍自己新工作那句话"里的完工关键词，排除带
   `remain/stay/unchanged/persist/inherited/still fixed/already/previously/prior` 等继承性
   措辞线索的句子。

这两处修好后，"大部分拍读起来都完工"这个大方向已经对齐参考案例；但本方案要修的是**另一类
更严重的问题**：不是措辞档位不对，而是**图片正文压根没写这拍真正新增的实物**。

---

## 1. 根因

`compose_remaining_beats` 里每一拍的 VIDEO 和 IMAGE 是**同一次 LLM 调用**里一起产出的
（批量路径见 `prompt_pipeline/__init__.py` 里 `_batch_shared_system_prompt` /
`_beat_block_text` 组装的 `===BEAT N VIDEO===` / `===BEAT N IMAGE===` / `===BEAT N TRACES===`
三段式响应；单拍兜底路径 `_generate_single_beat_with_retries`，约第5375行起，用
`===VIDEO===` / `===IMAGE===` / `===TRACES===` 同款三段式）。

响应里其实**已经带了这拍真正新增了什么**的结构化清单——`===...TRACES===` 段，解析后叫
`new_ledger_items`（每项含 `name`/`material_color`/`initial_state`/`grid`/`z_depth_scale`）。
但目前这份清单只用来事后更新 `packet['object_ledger']`（约第5814-5829行），**从没拿来反查
"这拍自己的 IMAGE 正文是否真的把这些东西写进去了"**。于是就出现了 VIDEO 说"装了躺椅/石桌/
盆栽"、TRACES 里大概率也列了这几项，但 IMAGE 正文一个字都没提的自相矛盾——而现有的
`check_stage_scope_wording` 安全网只负责查"有没有完工关键词"，查到关键词就算过，不管这句话
是不是在讲这拍真正装的东西，所以间接掩盖了这个更深的漏洞（详见喀斯特案例分析，回炉函数在
不知道"应该提到家具"的情况下，只能就近抓墙/天花板/地板这几个旧词拼出一句听起来像完工声明
的空话）。

**结论**：需要新增一道独立的确定性校验——"这拍声明要新增的东西，IMAGE 正文里必须能找到"，
不满足就定向回炉，回炉时把缺失的具体条目喂给 LLM，而不是像 `check_stage_scope_wording` 那样
只给一个笼统的"加一句完工声明"指令。

---

## 2. 最接近的现成范本

`check_signature_anchor_realized` / `rework_missing_anchor_beat`
（`prompt_pipeline/__init__.py:3971-4040`）已经是同一种模式："声明了一批关键短语 → 校验它们
是否字面出现在 IMAGE 正文里 → 缺了就定向回炉，回炉指令里直接点名缺失短语"。新校验可以照着
这一对的结构写，区别是：

- 数据来源不同：`check_signature_anchor_realized` 读 `beat.get('anchor_keywords')`（只有收尾拍
  才有）；新校验读 `new_ledger_items`（每一拍都有，只要这拍产出了 TRACES）。
- 匹配方式要放宽：`anchor_keywords` 是刻意要求逐字出现的营销短语，用精确子串匹配合理；但
  TRACES 的 `name` 字段是从 LLM 自由生成的结构化清单里抠出来的，遣词可能和 IMAGE 正文里的
  说法不完全一致（比如 TRACES 写"beige linen daybed"，IMAGE 正文写成"custom low-profile linen
  lounge daybed with cream beige textiles"）。要求逐字子串匹配会有假阳性，所以改用"关键词
  是否有交集"的宽松匹配——只有**一个关键词都没命中**才判定为真正缺失（喀斯特案例是这种
  100%缺失的情况，不是措辞对不上）。

---

## 3. 具体改动

### 3.1 新增两个函数（建议插入位置：紧跟 `rework_missing_anchor_beat` 之后，
约第4041行前，`_IMAGE_STERILE_PLACEHOLDER_PATTERN` 定义之前）

```python
_TRACE_NAME_STOPWORDS = {
    'a', 'an', 'the', 'of', 'and', 'with', 'on', 'in', 'at', 'to', 'for',
    'is', 'are', 'its', 'new', 'now',
}


def _trace_name_keywords(name):
    """Extract lowercase significant words (len > 2, not a stopword) from a TRACES item's
    'name' field, for lenient overlap matching against free-text IMAGE prose."""
    return [w for w in re.findall(r"[a-zA-Z']+", (name or '').lower())
            if w not in _TRACE_NAME_STOPWORDS and len(w) > 2]


def _missing_trace_items(image_prompt, new_ledger_items):
    """Items from THIS beat's own declared new_ledger_items whose name has ZERO keyword
    overlap with the IMAGE prompt text — i.e. genuinely absent, not just differently phrased
    (a single shared keyword is enough to NOT count as missing; this only catches complete
    omissions like the 2026-07-22 karst-cave furnishing beat, where the resulting IMAGE never
    mentioned the daybed/stone tables/potted plants its own VIDEO had just installed)."""
    if not image_prompt or not new_ledger_items:
        return []
    low = image_prompt.lower()
    missing = []
    for item in new_ledger_items:
        if not isinstance(item, dict):
            continue
        words = _trace_name_keywords(item.get('name'))
        if words and not any(w in low for w in words):
            missing.append(item)
    return missing


def check_image_realizes_traces(image_prompt, new_ledger_items):
    """This beat's own declared new_ledger_items (parsed from its own ===TRACES=== section —
    the same response that produced this beat's VIDEO) must actually appear in the paired
    IMAGE prompt text. A VIDEO can describe installing an object while the paired IMAGE
    forgets to describe it being present — a same-completion self-consistency failure that
    check_stage_scope_wording cannot catch (it only checks for a completion-claim keyword,
    not whether that claim names this beat's actual new content)."""
    missing = _missing_trace_items(image_prompt, new_ledger_items)
    if not missing:
        return []
    names = ', '.join(f'"{m.get("name")}"' for m in missing)
    return [
        f"This beat's own declared new features ({names}) never appear anywhere in the IMAGE "
        f"prompt text — the VIDEO/traces already commit to installing them, but the resulting "
        f"IMAGE forgets to describe them being present. Rewrite the state-delta sentence(s) to "
        f"explicitly name and place every one of these features in the scene."
    ]


def rework_missing_content_image_beat(config, i, image_prompt, new_ledger_items, beat=None):
    """Single-shot IMAGE rework (max one round) when this beat's own declared new_ledger_items
    are entirely absent from its IMAGE prompt (see check_image_realizes_traces). Same
    conservative contract as rework_missing_anchor_beat: camera/geometry/locked-anchor
    sentences survive verbatim, only the state-delta sentence(s) get rewritten to actually
    place the missing features (using their own material/color/grid from the TRACES item so
    the addition reads concretely, not as a vague mention). Returns (image_prompt, adopted)."""
    missing = _missing_trace_items(image_prompt, new_ledger_items)
    if not missing:
        return image_prompt, False
    items_desc = "\n".join(
        f"- {m.get('name')} ({m.get('material_color', 'unknown')}, "
        f"{m.get('initial_state', 'installed')}) at {m.get('grid', 'Grid B2')}"
        for m in missing
    )
    system = (
        "You are repairing ONE content-omission defect in a still-frame construction IMAGE "
        "prompt. This beat's own VIDEO already commits to introducing these NEW features, but "
        "the IMAGE text never describes them:\n" + items_desc + "\n"
        "Hard rules:\n"
        "- KEEP every sentence describing camera position, lens, geometry, locked anchors, grid "
        "coordinates, or frame-height percentages EXACTLY VERBATIM.\n"
        "- REWRITE the state-delta sentence(s) so they explicitly place and describe EVERY "
        "listed missing feature in the scene, using its own material/color and grid position — "
        "do not just gesture at generic completion, name the actual objects.\n"
        "- Do not invent additional new landmarks, change the camera, or contradict the "
        "established structural state. Keep the full prompt under 250 words.\n"
        "Output ONLY the corrected prompt text itself. Do not prefix it with any label, heading, "
        "quotation marks, or repetition of these instructions, and do not add commentary or "
        "markdown fences."
    )
    user = (
        f"Here is the image prompt for beat {i}, delimited by triple quotes:\n"
        f"\"\"\"\n{image_prompt}\n\"\"\""
    )
    try:
        resp = _chat(config, system, user, temperature=0.5, timeout=90)
        fixed = _strip_markdown_fences_only(resp).strip()
        fixed = _strip_leading_label_line(fixed)
        if not fixed or len(fixed.split()) > 300:
            return image_prompt, False
        if _missing_trace_items(fixed, new_ledger_items):
            return image_prompt, False
        return fixed, True
    except GenerationCancelled:
        raise
    except Exception as e:
        if sys.stdout:
            print(f"[DIRECT] Beat {i} 缺失内容回炉调用失败（保留原文）: {e}")
        return image_prompt, False
```

### 3.2 接入两处调用点——都需要先把 TRACES 解析挪到检查链之前

现状：两处调用点（批量路径、单拍兜底路径）都是**先跑完 `check_stage_scope_wording` /
`check_signature_anchor_realized` / `check_image_decay_placeholder` /
`check_first_interior_reveal_decay` 这一串检查+回炉，最后才解析 `===TRACES===`**
（批量路径在约第5776-5795行；单拍路径在约第5556-5578行）。新校验依赖 `new_ledger_items`，
必须在检查链**最前面**就能拿到，所以要把 TRACES 解析挪到 `errs = validate_beat_prompts(...)`
之后、`image_similar` 检查之前。

**批量路径**（`prompt_pipeline/__init__.py`，`errs = validate_beat_prompts(...)` 调用约在
第5700-5705行）：

1. 在 `errs = validate_beat_prompts(...)` 之后、`structural, style_errs = ...` 之前，插入
   TRACES 解析（把原本在第5777-5795行的那段解析逻辑原样搬到这里，赋值给一个新的局部变量，
   例如 `parsed_traces`）。
2. 删除原来在第5777-5795行的解析（现在提前了，不需要重复解析）；那个位置原本是给
   `new_ledger_items` 赋值，现在直接用第1步提前算好的 `parsed_traces` 赋值即可，保持后续
   `packet['object_ledger']` 更新逻辑（约第5817-5829行）不变。
3. 在 `image_similar` 回炉块（约第5724-5732行）之后、`wording_errs`（stage_scope，约第
   5733-5745行）之前，插入：
   ```python
   content_errs = check_image_realizes_traces(i_p, parsed_traces)
   if content_errs:
       if sys.stdout:
           print(f"[DIRECT] Batch beat {i} 图文内容脱节，定向回炉一轮: {content_errs}")
       i_p, content_reworked = rework_missing_content_image_beat(config, i, i_p, parsed_traces, beat=contract['beat'])
       if sys.stdout:
           print(f"[DIRECT] Batch beat {i} 图文内容回炉{'成功，已采用重写稿' if content_reworked else '未通过，保留原稿（仅留痕）'}")
       style_errs = style_errs + content_errs
       image_reworked = content_reworked if image_reworked is None else (image_reworked or content_reworked)
   ```
4. `vid_prompt, img_prompt, beat_succeeded = v_p, i_p, True`（约第5776行）保持在这串检查+回炉
   全部跑完之后赋值，确保拿到的是最终改过的 `i_p`。

**单拍兜底路径**（`_generate_single_beat_with_retries`，`errs = validate_beat_prompts(...)`
约在第5494行）：同样手法——把约第5558-5578行的 TRACES 解析挪到第5503行 `image_similar` 检查
之前，然后在 `image_similar` 回炉块之后插入等价的 `content_errs` / `rework_missing_content_image_beat`
调用，`record_beat_audit` 调用（约第5554行）之前完成。

两处都要注意：`record_beat_audit(config, i, structural, style_errs, reworked, image_reworked)`
调用必须在新校验块**之后**，这样审计记录才会带上这次新增的发现。

---

## 4. 测试

在 `tests/test_structural_beat_rework.py`（已有 `LARGE_IMAGE_MISSING_COVERAGE` 等同类
fixture，风格照抄即可）新增：

1. `test_check_image_realizes_traces_flags_missing_feature`：构造一个 `new_ledger_items =
   [{"name": "beige linen daybed", "material_color": "beige linen", "initial_state":
   "installed", "grid": "Grid B2", "z_depth_scale": "40%"}]`，配一段完全不提 daybed 的
   IMAGE 文本，断言 `check_image_realizes_traces` 返回非空。
2. `test_check_image_realizes_traces_lenient_on_partial_phrasing`：IMAGE 文本里用不同措辞
   但共享至少一个关键词（例如提到"daybed"但不是逐字的"beige linen daybed"），断言返回空
   （不应假阳性）。
3. `test_check_image_realizes_traces_empty_when_no_traces`：`new_ledger_items` 为空列表/None
   时返回空。
4. `test_rework_missing_content_image_beat_adopts_good_rewrite`：mock `_chat` 返回一段补全了
   缺失特征的文本，断言 `adopted=True` 且缺失清单清空。
5. `test_rework_missing_content_image_beat_rejects_bad_rewrite`：mock `_chat` 返回还是没提到
   缺失特征的文本，断言 `adopted=False` 且原文本被保留。

再跑一次全量 `python -m pytest tests/ -q`，确认不影响既有535个用例。

---

## 5. 端到端复验

本方案本身是文本生成阶段的修复，验证方式沿用今天走过的路径（不需要真的出图/出视频）：

1. 重启 `:8085`（`python server.py`，参考 `spark-8085-background-server-restart-gotcha`
   记忆条目——不要用 run.bat/pythonw，会在工具会话里被静默杀掉）。
2. 用同一个喀斯特溶洞主题（或任意包含"家具陈设"类操作的新主题）重新走一遍真实的
   `POST /api/compose`（不要绕过 API 直接调 `compose_remaining_beats`，否则不会写入真实任务
   记录）。
3. 等任务 `completed` 后读 `tasks/<task_id>.json` 的 `result.beat_audit`，确认新增的
   "图文内容脱节" 发现项在真正缺失时出现、且大多数情况下 `image_reworked: true` 之后缺失
   特征确实被写进了最终 `prompt_block`（直接读 `result.prompt_block` 里对应 IMAGE 段肉眼
   核对，就像今天核对喀斯特案例 IMAGE 11 那样）。
4. 特别验证"家具陈设"这类操作：找一拍 `operation` 明显是新增可数实物的（furnishing/
   fixture-install 之类），确认其 IMAGE 正文真的写出了对应实物，而不是只有一句空洞的完工
   声明。
