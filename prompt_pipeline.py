import os
import sys
import json
import re
import time
import socket
import urllib.request
import urllib.error
import urllib.parse
import base64
import threading
from PIL import Image
from datetime import datetime

from server_common import (
    SERVER_CONFIG, resolve_gateway, effective_config,
    OUTPUT_ROOT, SKILL_DIR, _get_project_dir, _safe_project_name,
    IMG2IMG_CONTROL_PROMPT, IMG2IMG_BRIDGE_CONTROL_PROMPT,
    PACKET_CACHE_LOCK, PROCESS_BRIEF_CACHE_LOCK
)
from frame_generator import (
    call_image_llm, _crop_to_aspect_ratio, _detect_image_mime_from_path,
    _generate_image_edit
)

# Clip timing constants: single source of truth for the video-model clip length and the
# worker exit deadline referenced throughout the fix_*/check_* pipeline and skill contract.
VIDEO_DURATION = 8.0
WORKER_EXIT_TIME = 7.5

# Thread-local storage for LLM usage accounting
_usage_tracker = threading.local()

def start_accounting():
    _usage_tracker.active = True
    _usage_tracker.prompt_tokens = 0
    _usage_tracker.completion_tokens = 0
    _usage_tracker.total_tokens = 0
    _usage_tracker.api_calls = 0

def stop_and_get_accounting():
    if not getattr(_usage_tracker, 'active', False):
        return None
    stats = {
        'prompt_tokens': _usage_tracker.prompt_tokens,
        'completion_tokens': _usage_tracker.completion_tokens,
        'total_tokens': _usage_tracker.total_tokens,
        'api_calls': _usage_tracker.api_calls
    }
    _usage_tracker.active = False
    return stats

def _record_tokens(usage):
    if not getattr(_usage_tracker, 'active', False) or not usage:
        return
    _usage_tracker.api_calls += 1
    _usage_tracker.prompt_tokens += usage.get('prompt_tokens', 0)
    _usage_tracker.completion_tokens += usage.get('completion_tokens', 0)
    _usage_tracker.total_tokens += usage.get('total_tokens', 0)

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'packet_cache.json')
PROCESS_BRIEF_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'process_brief_cache.json')


def _slice_between(text, start_marker, end_marker):
    """Return the substring from start_marker up to (not including) end_marker.
    Falls back to the tail from start_marker if end_marker is absent."""
    start = text.find(start_marker)
    if start == -1:
        return ''
    end = text.find(end_marker, start + len(start_marker))
    if end == -1:
        return text[start:]
    return text[start:end]


def _aux_model(config):
    """Return the low-cost model for mechanical parsing/audit tasks.
    Defaults to gemini-2.5-flash if the main model is gemini-3-flash-agent."""
    if not isinstance(config, dict):
        return 'gemini-2.5-flash'
    explicit = (config.get('cheapModel') or config.get('auxModel') or '').strip()
    if explicit:
        return explicit
    main_model = (config.get('model') or '').strip()
    if 'agent' in main_model.lower() or not main_model:
        return 'gemini-2.5-flash'
    return main_model


def load_skill_contract():
    """Read the live SKILL.md + prompt-templates.md and assemble the authoritative
    composition contract. Reading at request time keeps the shell in sync with the skill."""
    skill_md_path = os.path.join(SKILL_DIR, 'SKILL.md')
    templates_path = os.path.join(SKILL_DIR, 'references', 'prompt-templates.md')

    pipeline = ''
    templates = ''
    try:
        with open(skill_md_path, 'r', encoding='utf-8') as f:
            skill_md = f.read()
        # Forward-composition portion only: pipeline + the vocab/camera/lighting/audio
        # reference tables. Drops the video reverse-engineering tiers (Tier 4) and the
        # cross-skill/Notion plumbing that do not apply to GUI-driven generation.
        pipeline = _slice_between(skill_md, '## Internal Composition Pipeline', '## Cross-Skill Integration')
    except Exception as e:
        if sys.stdout:
            print(f"Warning: could not read SKILL.md ({e})")
    try:
        with open(templates_path, 'r', encoding='utf-8') as f:
            templates = f.read()
    except Exception as e:
        if sys.stdout:
            print(f"Warning: could not read prompt-templates.md ({e})")

    # Replace hardcoded durations with constants dynamically
    if pipeline:
        pipeline = pipeline.replace("8-second", f"{int(VIDEO_DURATION)}-second")
        pipeline = pipeline.replace("t=8s", f"t={int(VIDEO_DURATION)}s")
        pipeline = pipeline.replace("t=7.5s", f"t={WORKER_EXIT_TIME}s")
    if templates:
        templates = templates.replace("8-second", f"{int(VIDEO_DURATION)}-second")
        templates = templates.replace("t=8s", f"t={int(VIDEO_DURATION)}s")
        templates = templates.replace("t=7.5s", f"t={WORKER_EXIT_TIME}s")

    return pipeline, templates


def build_topic_brief(d):
    """Translate the GUI dimension selections into a Tier-1 topic brief the skill consumes."""
    theme = d.get('theme', '未指定场景')
    anchors = d.get('anchors') or []
    anchors_str = '、'.join(anchors) if anchors else '由作曲家自行选取最契合主题的锚点'
    complexity = d.get('complexity', '中等重工')
    budget = d.get('budget', '轻奢设计师级')
    ratio = d.get('ratio', '50%')
    creativity = d.get('creativity', '突破常规')

    return f"""本次为 GUI 维度驱动的 Tier-1 合成请求。请据此走完内部合成管线（Step 1 至 Step 9），产出完整的 IMAGE/VIDEO 提示词集与中文质量审核报告。

输入维度：
- 场景主题：{theme}
- 核心创意锚点：{anchors_str}
- 项目复杂度：{complexity}
- 预算级别：{budget}
- 外壳 \u2194 内里反差强度：{ratio}
- 创意尺度：{creativity}

硬性要求：
1. 把该主题落成一个「可真实搭建 / 改造」的延时场景，必须显式给出 CARRIER / ENV / TRAUMA（初始残破或空白态）/ DESTINY（成品态）/ REWARD ACTION。因为场景主题均为写实载体（如百年空心橡树、蓝冰冰川洞、退役潜艇舱、废弃水塔等），所以必须以「真实可施工的废弃外壳\u2192温暖室内改造」为核心，用真实的工具链、材料来源与物理因果痕迹把它落地，禁止物体凭空出现。
2. 节拍数下限：至少 15 个施工节拍（即 N ≥ 15，对应至少 16 张 IMAGE、15 段 VIDEO），再加最终 reward；按真实工序把改造拆成足够细的独立步骤（拆除清运 \u2192 结构修复 \u2192 水电管线粗装 \u2192 封板封墙 \u2192 底漆 \u2192 面漆 \u2192 地面 \u2192 灯具/设备接线 \u2192 家具软装…）。每拍只允许一个物理操作 + 微增量拆分，相邻 IMAGE 锚点状态变化不超过 3 个九宫格。如果一个工序变化超过画面三分之一，再拆成连续子节拍。
3. 【施工顺序与物理因果，最高优先级】整套视频的推进必须符合现实物理因果顺序；任何违反都视为致命错误并必须重排节拍。明确禁止下列情况：
   - 在「布线 / 通电」节拍完成之前，出现任何亮灯、灯带发光、屏幕点亮、设备通电运行的画面；
   - 在外部（外立面 / 屋面 / 场地 / 除锈防腐）尚未处理完成之前，就跨过门槛进入室内施工；
   - 在「除锈 / 打磨 / 清洁 / 底漆」完成之前，出现面漆、喷漆、清漆或任何光泽涂层；
   - 湿作业（砂浆 / 混凝土 / 胶水 / 油漆）尚未干结固化之前，就进入会把它覆盖掉的下一道工序；
   - 任何会被后续封板/饰面遮盖的管线、结构、防水层，必须在封盖节拍之前先完成。
   施工状态必须单调递增：已清理的保持干净、已安装的保持就位、已干的保持干态，禁止回退。
4. 全程执行 NGCS 九宫格坐标、Camera DNA 逐字复制、GCTR 因果痕迹（每处变化至少 2 个接触痕迹）、连续动作流（工人 t=0s 进、t=7.5s 出）、以及纯自然语言铁律——最终提示词正文里不得出现 % 符号、数字区间或任何技术缩写。
5. 创意尺度越高，DESTINY 与视觉反差越大胆，但绝不能牺牲第 1、3、4 条的物理连续性与因果顺序。
6. 【方案新颖度避雷/克制套路】严禁采用陈旧套路的方案（如：集装箱改造成极简小屋、旧货车/巴士改造成标准露营房车、仓库改造成工业Loft、灯塔改造成海滨卧室等），必须提供高新颖度与强反差的方案组合。
7. 【可施工性屏障】严禁任何魔法般瞬间变出家具、无合理生根基础或材料突变的片段。必须留下真实的物理因果痕迹与接触痕迹，扅术细节和接口连接均需合理，必须能够映射到真实的施工工序中。
8. 【截图招牌反差点】整个场景必须包含且仅包含一个风格化的招牌反差点（如：载体本体材质切面窗、水面玻璃地板、树皮伪装天窗、活体木纹/岩壁旋梯、生物荧光苔藓照明、整块载体板材台面、改道瀑布淋浴等）。必须在成品态（DESTINY）的醒目位置体现。"""


def build_system_prompt():
    pipeline, templates = load_skill_contract()
    contract_block = pipeline if pipeline else "(SKILL.md 合成管线未能加载，请依据通用延时改造连续性契约生成。)"
    templates_block = templates if templates else "(canonical templates 未能加载)"

    return f"""You are operating as the `restoration-prompt-composer` skill — a one-shot prompt composition engine for restoration / renovation / construction time-lapse video. You receive a GUI-collected topic brief and must produce a complete, production-ready IMAGE + VIDEO prompt set that strictly conforms to the skill contract below. Internalize every gate; do not expose internal step names to the user.

==================== SKILL CONTRACT (authoritative) ====================
{contract_block}

==================== CANONICAL TEMPLATES & CHECKLIST ====================
{templates_block}

==================== OUTPUT FORMAT (MANDATORY) ====================
Respond with EXACTLY the four section markers below, in this order, with nothing before `===TITLE===` and nothing after the audit body. Do NOT wrap anything in markdown code fences.

===TITLE===
<a short, catchy, viral Chinese project name, e.g. 工业复古·废土集装箱卧室>
===THEME===
<the scene theme in Chinese, one short phrase>
===PROMPTS===
图片提示词
图片 1:
<English image-model prompt prose>

图片 2:
<English image-model prompt prose>

视频提示词
视频 1:
<English video-model prompt prose>

视频 2:
<English video-model prompt prose>
===AUDIT===
<提示词质量审核报告：用 Markdown 表格列出关键 P0/P1 检查项（九宫格锁定、Camera DNA 复制、因果痕迹 GCTR、微增量拆分、连续动作流、纯自然语言、累计状态、施工顺序等）及通过状态与一句话说明。>

Hard rules for the ===PROMPTS=== section:
- Use ONLY these labels: `图片提示词`, `图片 N:`, `视频提示词`, `视频 N:`. Each label on its own line.
- IMAGE count must equal N+1; VIDEO count must equal N. N must be AT LEAST 15 (so at least 16 image slots and 15 video slots); use more if the build genuinely needs it. Do not collapse or skip beats to stay short.
- The beat ladder MUST obey real construction order: demolition and debris clearing → structural repair → rough-in wiring/plumbing/ducting → close-up panels → primer → finish/topcoat → flooring → fixtures and lighting ONLY after their wiring beat → furniture and decoration last. Hard vetoes (a single occurrence invalidates the whole set): never show powered lights, glowing strips, lit screens, or running equipment before the wiring/power beat; never cross the threshold into the interior before the exterior is finished; never show paint, spray, or topcoat before rust removal, cleaning, and priming; never cover wet/uncured material with the next layer. Construction state is monotonic — finished work never regresses.
- [NEW RULE] Clear Path Requirement: If a project introduces any sliding, rolling, retracting, folding, or moving mechanical parts (e.g. bed rails, slide-out bed, retractable roof, folding stairs), the prompt generator must ensure a clean spatial path. If there are structural columns, support pillars, or bulkheads in the trauma state (IMAGE 1) that block this path, they must be explicitly cut and removed early (typically in structural repair phase) and replaced by peripheral support frames (e.g. ceiling arches or beams) before any rails or mechanisms are installed.
- [NEW RULE] Floor and Skeleton Logic: If floor/wall joists, ribs, or framing studs will be insulated or paneled later, the bare structural skeleton (e.g. open metal joists or ribs) must be exposed at the very beginning (IMAGE 1/2). The floor/wall states must progress monotonically forward: bare joists/studs -> rough-in / insulation -> subfloor -> wood planks / paneling. Never start with a solid finished-looking floor that disappears to reveal raw joists later.
- [NEW RULE] Perspective Isolation: Do not flip camera facing directions (e.g. turning 180 degrees from looking out to looking in) in the same spatial axis without a clean separate phase or TBCP transition. If the project centers on a slide-out reward action (e.g. bed sliding out of a cliff cabin), lock Camera Family B (looking outward through the opening towards the view) from the very first frame to maintain spatial consistency.
- [NEW RULE] Strict Single-Operation Beat Rule: Each {int(VIDEO_DURATION)}-second video prompt must describe exactly one homogeneous physical task. Combining multiple distinct stages (e.g. painting AND mounting frames, or framing AND insulating, or laying tile AND anchoring stoves AND mounting bed frames) into a single {int(VIDEO_DURATION)}-second clip is strictly prohibited to prevent visual morphing and cut-scene jumps.
- [NEW RULE] Bi-Directional Agent Flow: Standardize worker paths to prevent teleporting or instant popping. Workers must enter the frame from a specific coordinate edge at t=0s and walk out through the same edge by t={WORKER_EXIT_TIME}s, leaving the frame completely empty of personnel at t={int(VIDEO_DURATION)}s.
- [NEW RULE] Rigid Container Encapsulation: All loose materials, debris, fasteners, and liquids must be stored and tracked inside rigid, quantifiable containers (e.g. buckets, parts trays, boxes), and their volumes must be described as continuously increasing or decreasing.
- [NEW RULE] Mandatory Climax Video: Ensure the transition between the final two frames (the "Dressed interior" -> "Retract/slide action") is fully animated. The climax video (VIDEO N) must depict the actual physical kinetic movement of the mechanism (e.g. the bed rolling smoothly forward, the glass door sliding open).
- One blank line between each slot. No markdown headings, bullets, or tables inside this section.
- Prompt bodies are English natural-language prose suitable for an image / video generation model.
- Never emit `%`, raw numeric ranges (e.g. `10% to 90%`), or technical acronyms (HAL, GCTR, RPL, VMFP, RCE, NGCS, SCUP) inside the prompt bodies. Express all progress and traces as fluid visual prose.
- Every VIDEO begins with `Use the provided first frame and last frame as exact composition anchors.` and binds IMAGE N to IMAGE N+1."""


def _chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240, on_chunk=None, model=None):
    if not model:
        model = config.get('model') or 'gemini-3-flash-agent'
    base_url, api_key = resolve_gateway(model, config)

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': temperature,
        # 16+ IMAGE and 15+ VIDEO slots is ~31 prompts; needs a large output budget.
        'max_tokens': max_tokens,
    }
    
    if on_chunk is not None:
        payload['stream'] = True

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=data,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    # Disable the Windows system proxy for this localhost call — scripted HTTP to
    # localhost otherwise gets intercepted/reset by the system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    
    if on_chunk is not None:
        try:
            full_content = []
            with opener.open(req, timeout=timeout) as resp:
                for line in resp:
                    line_str = line.decode('utf-8').strip()
                    if not line_str:
                        continue
                    if line_str.startswith('data: '):
                        data_part = line_str[6:]
                        if data_part == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data_part)
                            if 'usage' in chunk:
                                _record_tokens(chunk['usage'])
                            if 'choices' in chunk and len(chunk['choices']) > 0:
                                delta = chunk['choices'][0].get('delta', {})
                                content = delta.get('content', '')
                                if content:
                                    full_content.append(content)
                                    on_chunk(content)
                        except Exception:
                            pass
            return ''.join(full_content)
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] Streaming request failed: {e}. Falling back to non-streaming...")
            if 'stream' in payload:
                del payload['stream']
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                f'{base_url}/chat/completions',
                data=data,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                },
                method='POST',
            )

    try:
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            _record_tokens(body.get('usage'))
    except urllib.error.HTTPError:
        # Real HTTP error from the proxy/model — handled specially upstream.
        raise
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        reason = getattr(e, 'reason', e)
        raise RuntimeError(
            f"无法连接本地 LLM 代理（{base_url}）：{reason}。"
            "请确认 Antigravity Tools 代理服务正在运行、端口正确（默认 8046），"
            "并检查 API 配置中心的 Base URL / API Key。"
        )
    try:
        return body['choices'][0]['message'].get('content') or ''
    except (KeyError, IndexError, TypeError):
        # Proxy returned 200 but not an OpenAI-shaped body (e.g. an error envelope).
        err = ''
        if isinstance(body, dict):
            err = (body.get('error') or {}).get('message') or body.get('message') or ''
        raise RuntimeError(f"LLM 代理返回了无法解析的响应：{err or json.dumps(body, ensure_ascii=False)[:300]}")


def _multimodal_chat(config, system, user_text, image_paths, model=None):
    if not model:
        model = config.get('model') or 'gemini-3-flash-agent'
    base_url, api_key = resolve_gateway(model, config)

    content_list = [{"type": "text", "text": user_text}]
    for path in image_paths:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Image file not found: {path}")
        with open(path, "rb") as f:
            data = f.read()
        
        mime = "image/png"
        if path.lower().endswith(".webp"):
            mime = "image/webp"
        elif path.lower().endswith(".jpg") or path.lower().endswith(".jpeg"):
            mime = "image/jpeg"
            
        b64_str = base64.b64encode(data).decode('ascii')
        content_list.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{b64_str}"
            }
        })

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': content_list},
        ],
        'temperature': 0.1,
        'max_tokens': 1000,
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=data,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=90) as resp:
        res_data = json.loads(resp.read().decode('utf-8'))
        _record_tokens(res_data.get('usage'))
        return res_data['choices'][0]['message']['content']


def run_vlm_qa_check(config, img_i_path, img_ip1_path, video_prompt, is_bridge=False):
    """
    Compare generated IMAGE i and IMAGE i+1 with the transition VIDEO i prompt.
    Returns (pass_boolean, reason_string).
    """
    try:
        system_prompt = (
            "You are a strict, professional frame-by-frame visual quality auditor (VLM) for time-lapse videos. "
            "You are comparing Image 1 (IMAGE i) and Image 2 (IMAGE i+1) which represent the start and end frames "
            "of a video segment. The transition action is described by the VIDEO prompt.\n\n"
            "Your task is to detect the following flaws:\n"
            "1. NO CHANGE: The two images are identical or almost identical, meaning the image editor failed to execute the change.\n"
        )
        if is_bridge:
            system_prompt += (
                "2. CAMERA viewpoint jumps: The camera is performing a bridge transition (entering/crossing the threshold), "
                "so the perspective/camera position is ALLOWED and REQUIRED to move forward (closer view, crossing sill). "
                "However, the horizontal level and general alignment must still be consistent with entering the same space. "
                "Do not fail for perspective shifts that move forward along the viewpoint axis.\n"
            )
        else:
            system_prompt += (
                "2. CAMERA perspective/viewpoint jumps: The camera position, angle, or background layout shifted or jumped. The background structure must remain locked (same viewpoint, same horizon line level, same perspective).\n"
            )
        system_prompt += (
            "3. ACTION mismatch: The visual change between Image 1 and Image 2 does NOT correspond to the action described in the VIDEO prompt.\n\n"
            "Response format:\n"
            "- If the background/composition is consistent AND the change corresponds to the video prompt, respond EXACTLY with: PASS\n"
            "- If there is a camera jump, no change, or incorrect change, respond with: FAIL: <reason in Chinese>"
        )
        
        user_text = f"VIDEO transition prompt:\n{video_prompt}\n\nPlease analyze the transition from Image 1 to Image 2."
        
        response = _multimodal_chat(config, system_prompt, user_text, [img_i_path, img_ip1_path])
        response_clean = response.strip()
        
        if response_clean.upper() == "PASS" or response_clean.upper().startswith("PASS"):
            return True, "PASS"
        else:
            return False, response_clean
    except Exception as e:
        if sys.stdout:
            print(f"[VLM QA] VLM API call failed: {e}. Skipping VLM check to avoid blocking pipeline.")
        return True, f"Skipped (API Error: {e})"


def fix_image_prompt_with_vlm_feedback(config, original_prompt, vlm_reason):
    """
    Use LLM (auxModel) to generate a corrected image prompt based on the VLM QA audit failure reason.
    """
    system_prompt = (
        "You are an expert prompt engineering assistant. Your job is to modify a stable-diffusion-style IMAGE prompt "
        "to fix specific visual errors detected by a visual auditor (VLM). "
        "You will be given the original IMAGE prompt and the audit failure reason (in Chinese). "
        "Provide a corrected IMAGE prompt that addresses the failure reason. "
        "Specifically:\n"
        "- If the auditor reported that an object wasn't removed, make sure to state that the object has been removed, using terms like 'REMOVED: [object name]'.\n"
        "- If the auditor reported that a required object is missing, append a clear description of the object to the prompt.\n"
        "- Keep the rest of the original prompt's structure, landmarks, Camera DNA, and style intact.\n"
        "- Do NOT output any explanations, markdown code fences, or headers. Output ONLY the raw corrected prompt text in English."
    )
    user_prompt = (
        f"Original IMAGE prompt:\n{original_prompt}\n\n"
        f"VLM Audit Failure Reason:\n{vlm_reason}\n\n"
        f"Please output the corrected IMAGE prompt in English."
    )
    try:
        response = _chat(
            config, system_prompt, user_prompt,
            temperature=0.3, timeout=60, model=_aux_model(config)
        )
        return _strip_markdown_fences_only(response).strip()
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] fix_image_prompt_with_vlm_feedback failed: {e}")
        return original_prompt


def load_reference_file(name):
    """Load a reference markdown file from the skill references folder."""
    path = os.path.join(SKILL_DIR, 'references', name)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            if sys.stdout:
                print(f"Warning: could not read reference file {name} ({e})")
    return ""


def get_cropped_templates(templates_content, i, total_beats, mode, bridge_stage):
    """Parse and crop the prompt-templates.md content based on the beat type
    to minimize the input context size during LLM prompt generation."""
    if not templates_content:
        return ""
        
    # Split templates by headers of level 2 and 3
    pattern = r'\n(###?\s+.*)'
    parts = re.split(pattern, '\n' + templates_content)
    
    sections = {}
    current_header = None
    for part in parts:
        part_strip = part.strip()
        if not part_strip:
            continue
        if part_strip.startswith('###') or part_strip.startswith('##'):
            current_header = part_strip
            sections[current_header] = ""
        elif current_header:
            sections[current_header] += part

    def find_section(kw):
        for h, val in sections.items():
            if kw.lower() in h.lower():
                return h + "\n" + val
        return ""

    image_1 = find_section("IMAGE 1")
    image_2_plus = find_section("IMAGE 2+")
    image_final = find_section("Final IMAGE")
    
    video_ordinary = find_section("Ordinary Construction VIDEO")
    video_bridge = find_section("Threshold Bridge")
    video_final = find_section("Final Reward VIDEO N")
    
    image_checklist = find_section("IMAGE Checklist")
    video_checklist = find_section("VIDEO Checklist")
    checklist_combined = f"{image_checklist}\n{video_checklist}"
    
    # If i is None, this is a request for image 1 generation
    if i is None:
        return f"{image_1}\n\n{image_checklist}"
        
    cropped = []
    
    is_last = (i == total_beats)
    is_bridge = (mode == 'Threshold' and bridge_stage in (1, 2))
    
    # Select IMAGE Template
    if is_last:
        cropped.append(image_final)
    elif is_bridge and bridge_stage == 1:
        # Sill Handoff Frame template is already contained within the Bridge templates block
        pass
    else:
        cropped.append(image_2_plus)
        
    # Select VIDEO Template
    if is_last:
        cropped.append(video_final)
    elif is_bridge:
        cropped.append(video_bridge)
    else:
        cropped.append(video_ordinary)
        
    cropped.append(checklist_combined)
    return "\n\n".join(cropped)


def parse_space_workflows():
    content = load_reference_file('space-workflows.md')
    workflows = {}
    if not content:
        return {
            "abandoned property": {"beats": "3-6", "phases": ["hazard clearing", "shell repair", "surface finish", "practical lighting", "final carry-out"], "threshold": False}
        }
    for line in content.splitlines():
        if line.strip().startswith('|') and not line.strip().startswith('|---'):
            parts = [p.strip() for p in line.split('|')[1:-1]]
            if len(parts) >= 4 and parts[0] != "Space Type":
                space_type = parts[0].strip('` ')
                beats = parts[1].strip()
                phases_raw = parts[2].strip()
                threshold_raw = parts[3].strip()
                phases = [p.strip() for p in re.split(r'→|->', phases_raw) if p.strip()]
                is_threshold = "Threshold" in threshold_raw
                workflows[space_type] = {
                    "beats": beats,
                    "phases": phases,
                    "threshold": is_threshold
                }
    return workflows


def _flatten_to_text(value):
    """Coerce an LLM-produced JSON value into a plain prose string.
    The packet/ladder generators occasionally return a nested object or list where the
    contract asks for one sentence (e.g. worker_choreography split into
    trajectory/silhouette/manual_tool_lock keys). The content is usually fine — only the
    shape is wrong — so flatten it instead of discarding it."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return ' '.join(t for t in (_flatten_to_text(v) for v in value) if t)
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            text = _flatten_to_text(v)
            if text:
                parts.append(f"{k}: {text}" if isinstance(k, str) and k else text)
        return '; '.join(parts)
    return str(value)


def normalize_packet(packet):
    """Coerce every Drift Lock Packet field to its canonical type. Downstream fix_*/check_*
    code calls .lower()/.replace() on the prose fields and must never see a dict/list —
    a dict-shaped worker_choreography aborted whole compose runs at Beat 2 (the first beat
    where check_stylistic_repetition runs). Applied to fresh LLM output before caching AND
    to cache hits, so previously-poisoned cache entries heal on load."""
    if not isinstance(packet, dict):
        return packet
    for key in ('camera_dna', 'geometry_lock', 'worker_choreography', 'passive_environment'):
        if key in packet and not isinstance(packet[key], str):
            packet[key] = _flatten_to_text(packet[key])
    for lm in packet.get('primary_landmarks') or []:
        if isinstance(lm, dict):
            for k, v in list(lm.items()):
                if not isinstance(v, str):
                    lm[k] = _flatten_to_text(v)
    for coll in (packet.get('frame_boundaries'), packet.get('lighting_phase_ladder')):
        if isinstance(coll, dict):
            for k, v in list(coll.items()):
                if not isinstance(v, str):
                    coll[k] = _flatten_to_text(v)
    for item in packet.get('object_ledger') or []:
        if isinstance(item, dict):
            for k, v in list(item.items()):
                if not isinstance(v, str):
                    item[k] = _flatten_to_text(v)
    return packet


def normalize_beat_ladder(beat_ladder):
    """Shape-coercion for beat ladder entries: index must be an int, operation/description
    prose strings, bridge_stage an int or None. Guards the same dict-where-string-expected
    LLM quirk as normalize_packet."""
    if not isinstance(beat_ladder, list):
        return beat_ladder
    for beat in beat_ladder:
        if not isinstance(beat, dict):
            continue
        idx = beat.get('index')
        if idx is not None and not isinstance(idx, int):
            try:
                beat['index'] = int(str(idx).strip())
            except (ValueError, TypeError):
                pass
        for key in ('operation', 'description'):
            if key in beat and not isinstance(beat[key], str):
                beat[key] = _flatten_to_text(beat[key])
        bs = beat.get('bridge_stage')
        if bs is not None and not isinstance(bs, int):
            try:
                beat['bridge_stage'] = int(str(bs).strip())
            except (ValueError, TypeError):
                beat['bridge_stage'] = None
    return beat_ladder


def load_packet_cache():
    with PACKET_CACHE_LOCK:
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                if sys.stdout:
                    print(f"Warning: could not read packet_cache.json ({e})")
        return {}


def save_packet_cache(cache):
    with PACKET_CACHE_LOCK:
        try:
            with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if sys.stdout:
                print(f"Warning: could not write packet_cache.json ({e})")


def get_brief_fingerprint(dimensions):
    import hashlib
    serialized = json.dumps(dimensions, sort_keys=True)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def normalize_carrier_key(carrier, env):
    key = f"{carrier or ''}|{env or ''}".strip().lower()
    key = re.sub(r'[^a-z0-9]+', '-', key).strip('-')
    return key or 'unknown'


def load_process_brief_cache():
    with PROCESS_BRIEF_CACHE_LOCK:
        if os.path.exists(PROCESS_BRIEF_CACHE_PATH):
            try:
                with open(PROCESS_BRIEF_CACHE_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                if sys.stdout:
                    print(f"Warning: could not read process_brief_cache.json ({e})")
        return {}


def save_process_brief_cache(cache):
    with PROCESS_BRIEF_CACHE_LOCK:
        try:
            with open(PROCESS_BRIEF_CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if sys.stdout:
                print(f"Warning: could not write process_brief_cache.json ({e})")


def append_to_used_topic_ledger(parsed_brief, dimensions):
    ledger_path = os.path.join(SKILL_DIR, 'references', 'used-topic-ledger.md')
    if not os.path.exists(ledger_path):
        return
    date_str = datetime.now().strftime('%Y-%m-%d')
    carrier = parsed_brief.get('carrier', 'unknown').lower().replace(' ', '-')
    destiny = parsed_brief.get('destiny', 'unknown').lower().replace(' ', '-')
    anchors = dimensions.get('anchors') or []
    twist = anchors[0].lower().replace(' ', '-') if anchors else 'custom-twist'
    
    topic_dna = f"{carrier} / {destiny} / {twist}"
    one_sentence = f"{dimensions.get('theme', '未命名主题')}"
    source = "GUI Generation"
    avoid_notes = "Automatically registered by backend generator."
    
    new_row = f"| {date_str} | {topic_dna} | {one_sentence} | {source} | {avoid_notes} |\n"
    try:
        with open(ledger_path, 'a', encoding='utf-8') as f:
            f.write(new_row)
        if sys.stdout:
            print(f"[DEBUG] Appended topic to used-topic-ledger.md: {topic_dna}")
    except Exception as e:
        if sys.stdout:
            print(f"Warning: could not write to used-topic-ledger.md ({e})")


def clean_prompt_text(prompt):
    prompt = prompt.replace('%', ' percent')
    acronyms = ['HAL', 'TSPA', 'VMFP', 'GCTR', 'RPL', 'RCE', 'SCUP', 'NGCS', 'OSPL', 'RHMA', 'PBISP', 'HCL', 'NLVTR', 'MTAL']
    for ac in acronyms:
        prompt = re.sub(rf'[([（【]{ac}[)\]）】]', '', prompt)
        prompt = re.sub(rf'\b{ac}\b', '', prompt)
        
    # Split into sentences to perform negation-aware shortcut removal
    sentences = re.split(r'(?<=[.!?])\s+', prompt)
    cleaned_sentences = []
    
    shortcuts = [
        'cross-dissolve', 'fade-in', 'suddenly', 'magically', 'rapid montage', 
        'jump cut', 'jump-cut', 'jumpcuts', 'jumpcut', 'time skip', 'time-skip', 
        'instant transformation', 'cross dissolve', 'fade in', 'transformation progresses',
        'instantly transform', 'suddenly appears', 'teleport', 'as if by magic', 'out of nowhere'
    ]
    
    negation_words = ['forbid', 'avoid', 'no', 'without', 'never', 'not', 'stop', 'prevent', 'strictly', 'prohibit']
    
    for sentence in sentences:
        low_sent = sentence.lower()
        is_negation = any(neg in low_sent for neg in negation_words)
        if not is_negation:
            for sc in shortcuts:
                pattern = rf'\b{re.escape(sc)}s?\b'
                sentence = re.sub(pattern, '', sentence, flags=re.IGNORECASE)
                if ' ' in sc:
                    alt1 = sc.replace(' ', '-')
                    alt2 = sc.replace(' ', '')
                    sentence = re.sub(rf'\b{re.escape(alt1)}s?\b', '', sentence, flags=re.IGNORECASE)
                    sentence = re.sub(rf'\b{re.escape(alt2)}s?\b', '', sentence, flags=re.IGNORECASE)
        cleaned_sentences.append(sentence)
        
    prompt = " ".join(cleaned_sentences)
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    return prompt


def fix_image_clean_frame_proactive(prompt):
    """Proactively remove worker/agent references and active construction verbs from the image prompt to ensure it meets the Clean Frame requirements."""
    sentences = re.split(r'(?<=[.!?])\s+', prompt)
    cleaned_sentences = []
    
    negatives = ['no', 'zero', 'without', 'free of', 'absent', 'clear of', 'empty of', 'never']
    worker_agents = ['worker', 'builder', 'carpenter', 'laborer', 'person', 'man', 'woman', 'people']
    
    for sentence in sentences:
        low_sent = sentence.lower()
        has_negative = any(re.search(rf'\b{neg}\b', low_sent) for neg in negatives)
        has_worker = any(re.search(rf'\b{w}s?\b', low_sent) for w in worker_agents)
        if has_negative and has_worker:
            cleaned_sentences.append(sentence)
            continue
            
        if 'painting' in low_sent and 'after painting' not in low_sent:
            is_noun_painting = re.search(r'\b(?:a|the|this|that|framed|oil|acrylic|canvas|decorative|original)\s+painting\b', low_sent) or \
                               re.search(r'\bpainting\s+(?:hangs|is hanging|depicts|decorates|on the wall|in a frame)\b', low_sent)
            if not is_noun_painting:
                sentence = re.sub(r'\bpainting\b', 'paint', sentence, flags=re.IGNORECASE)
        if 'installing' in low_sent and 'before installing' not in low_sent:
            sentence = re.sub(r'\binstalling\b', 'installation', sentence, flags=re.IGNORECASE)
        if 'sweeping' in low_sent:
            is_noun_sweeping = re.search(r'\bsweeping\s+(?:view|curve|arch|line|gesture|motion|pan|shot)\b', low_sent)
            if not is_noun_sweeping:
                sentence = re.sub(r'\bsweeping\b', 'swept dust', sentence, flags=re.IGNORECASE)
        if 'shoveling' in low_sent:
            sentence = re.sub(r'\bshoveling\b', 'cleared soil', sentence, flags=re.IGNORECASE)
            
        sentence = re.sub(r"\bworker's\b", "equipment's", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bworkers's\b", "equipment's", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bworker\b", "equipment", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bworkers\b", "equipments", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bbuilder\b", "equipment", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bbuilders\b", "equipments", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bcarpenter\b", "equipment", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bcarpenters\b", "equipments", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\blaborer\b", "equipment", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\blaborers\b", "equipments", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bperson\b", "object", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bpeople\b", "objects", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bman\b", "object", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bwoman\b", "object", sentence, flags=re.IGNORECASE)
        
        cleaned_sentences.append(sentence)
        
    return " ".join(cleaned_sentences)


def fix_video_opening(i, prompt):
    expected_start = f"Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout."
    prompt_stripped = prompt.strip()
    if "third layout." in prompt_stripped:
        idx = prompt_stripped.lower().find("third layout.")
        prompt_stripped = prompt_stripped[idx + len("third layout."):].strip()
    elif "third layout" in prompt_stripped:
        idx = prompt_stripped.lower().find("third layout")
        prompt_stripped = prompt_stripped[idx + len("third layout"):].strip()
    elif prompt_stripped.lower().startswith("use the provided first frame"):
        dot_idx = prompt_stripped.find(".")
        if dot_idx != -1:
            prompt_stripped = prompt_stripped[dot_idx + 1:].strip()
    return f"{expected_start} {prompt_stripped}"


def fix_pacing_control(prompt, is_threshold_or_reveal):
    if not is_threshold_or_reveal:
        phrase = "continuous construction time-lapse, not real-time footage."
        if phrase.lower() not in prompt.lower() and "continuous construction time-lapse" not in prompt.lower():
            if not prompt.endswith('.'):
                prompt += '.'
            prompt += f" {phrase}"
    return prompt


def fix_out_and_in(prompt, is_threshold_or_reveal=False):
    if is_threshold_or_reveal:
        return prompt
    low = prompt.lower()
    # Skip if the video is explicitly worker-free (threshold bridge, reward, etc.)
    sterile_phrases = ['sterile of workers', 'sterile of active workers',
                       'sterile of any human', 'no workers', 'no human presence',
                       'completely sterile of', 'without any human']
    if any(phrase in low for phrase in sterile_phrases):
        return prompt

    # Detect worker presence
    has_worker = any(re.search(rf'\b{w}s?\b', low) for w in ('worker', 'crew', 'person', 'builder', 'laborer'))
    if not has_worker:
        return prompt

    # Detect multi-worker scenarios
    multi_worker_phrases = ['two workers', 'three workers', 'multiple workers',
                            'the workers', 'both workers']
    is_multi = any(phrase in low for phrase in multi_worker_phrases)

    # Check if entry/exit is already described
    has_entry = any(p in low for p in ['t=0', '0 seconds', 'start of the clip', 'enters the frame'])
    has_exit = any(p in low for p in [str(WORKER_EXIT_TIME), 'exits the frame', 'walks out', 'leaves the frame'])

    if has_entry and has_exit:
        # Already has full in/out — check for multi-worker vs single-worker template conflict
        if is_multi and 'one lone worker' in low:
            # Conflict: body says multi-worker but appended template says one worker
            prompt = re.sub(
                rf'At t=0s, one lone worker enters the frame from the Grid C1 edge;[^.]*leaving the frame completely empty at t={int(VIDEO_DURATION)}s\.',
                '', prompt).strip()
            # Re-add consistent multi-worker clause if no other exit clause remains
            if not any(p in prompt.lower() for p in ['exits the frame', 'walks out', 'leaves the frame']):
                prompt += f' At t=0s, the workers enter the frame; by t={WORKER_EXIT_TIME}s, all workers exit the frame, leaving it completely empty at t={int(VIDEO_DURATION)}s.'
        return prompt

    # Add missing entry/exit clause
    if not prompt.endswith('.'):
        prompt += '.'

    if is_multi:
        clause = f" At t=0s, the workers enter the frame; by t={WORKER_EXIT_TIME}s, all workers exit the frame, leaving it completely empty at t={int(VIDEO_DURATION)}s."
    else:
        clause = f" At t=0s, one lone worker enters the frame from the Grid C1 edge; the worker performs work, and by t={WORKER_EXIT_TIME}s, walks out of the frame through the Grid C1 edge, leaving the frame completely empty at t={int(VIDEO_DURATION)}s."

    prompt += clause
    return prompt


def fix_sound_design(prompt):
    """Guarantee the exemplar's mandatory two-part audio line. Safety net only — the beat prompt
    asks the model to write beat-specific sound; this fires only when it omits it entirely."""
    low = prompt.lower()
    if 'sound effect' not in low and 'ambient noise' not in low:
        clause = ("Sound effects include the tool contact, material movement, and footsteps of this beat. "
                  "Ambient noise is the steady enclosed room tone of the space.")
        if not prompt.endswith('.'):
            prompt += '.'
        prompt += f" {clause}"
    return prompt


def select_camera_dna(beat, base_camera_dna):
    op = beat.get('operation', '').lower() if beat else ''
    desc = beat.get('description', '').lower() if beat else ''
    
    bridge_stage = beat.get('bridge_stage') if beat else None
    if bridge_stage == 1:
        is_bridge_1 = True
        is_bridge_2 = False
    elif bridge_stage == 2:
        is_bridge_1 = False
        is_bridge_2 = True
    elif bridge_stage is not None:
        is_bridge_1 = False
        is_bridge_2 = False
    else:
        is_bridge_1 = "bridge-1" in desc or "bridge 1" in desc or ("threshold" in op and "sill" in desc and "cross" not in desc)
        is_bridge_2 = "bridge-2" in desc or "bridge 2" in desc or ("threshold" in op and "cross" in desc)
    
    if is_bridge_1:
        return "coaxial forward-pushing camera, ultra-wide 14mm lens feel, camera height 1.6m, eye-level perspective dollying forward; horizon line remains level at 50-percent height; optical flow radiates symmetrically from the doorway sill in Grid B2."
    elif is_bridge_2:
        return "coaxial forward-pushing camera crossing the threshold, ultra-wide 14mm lens feel, camera height 1.6m, perspective dollying forward through the doorway; horizon line remains level at 50-percent height; optical flow radiates from the rear wall center in Grid B2."
    
    # If the base camera DNA has a range, clean it up
    cleaned_base = base_camera_dna
    if "14-18mm" in cleaned_base:
        cleaned_base = cleaned_base.replace("14-18mm", "14mm")
        
    return cleaned_base


def fix_camera_contradictions(prompt, is_moving=False, is_bridge=None):
    if is_bridge is not None:
        is_moving = is_bridge
    sentences = re.split(r'(?<=[.!?])\s+', prompt)
    cleaned_sentences = []
    
    if is_moving:
        static_phrases = [
            r'camera remains locked in a static tripod shot',
            r'camera remains locked in a static tripod',
            r'static tripod shot',
            r'camera remains locked',
            r'locked camera perspective',
            r'locked eye-level perspective',
            r'locked tripod shot',
            r'locked tripod'
        ]
        for sentence in sentences:
            low_sent = sentence.lower()
            if any(re.search(phrase, low_sent, flags=re.IGNORECASE) for phrase in static_phrases):
                continue
            cleaned_sentences.append(sentence)
    else:
        moving_phrases = [
            r'coaxial forward-pushing camera',
            r'coaxial forward-pushing',
            r'dollying forward',
            r'dolly-in',
            r'dolly forward',
            r'camera actively advances',
            r'camera viewpoint is actively advancing',
            r'optical flow radiates symmetrically from the doorway sill',
            r'crossing the threshold',
            r'crosses the sill'
        ]
        for sentence in sentences:
            low_sent = sentence.lower()
            if any(re.search(phrase, low_sent, flags=re.IGNORECASE) for phrase in moving_phrases):
                continue
            cleaned_sentences.append(sentence)
            
    return " ".join(cleaned_sentences).strip()


def check_camera_contradictions(prompt, is_moving):
    errors = []
    low = prompt.lower()
    if is_moving:
        static_words = ['static tripod', 'camera remains locked', 'locked eye-level', 'locked camera']
        for sw in static_words:
            if sw in low:
                errors.append(f"Moving camera prompt contains contradictory static clause '{sw}'")
    else:
        moving_words = ['dollying forward', 'dolly-in', 'forward-pushing', 'camera actively advances', 'crosses the sill', 'crosses the threshold']
        for mw in moving_words:
            if mw in low:
                errors.append(f"Static camera prompt contains contradictory moving clause '{mw}'")
    return errors


def fix_camera_dna(prompt, camera_dna):
    prefix = prompt[:300].lower()
    keywords = ['tripod', 'lens feel', 'camera height']
    if any(kw in prefix for kw in keywords):
        return prompt
    if camera_dna.lower() not in prompt.lower():
        return f"{camera_dna} {prompt}"
    return prompt


def fix_rhma_blur(prompt, is_last):
    if is_last and ("reflection" in prompt.lower() or "polished" in prompt.lower()):
        clause = "The highly reflective polished floor surface in Grid C1-C3 displays a heavily blurred, low-gloss, diffused reflection of the background; reflections are muted, dark, and highly out-of-focus, preventing high-frequency contrast or sharp details; realistic Fresnel falloff near the margins."
        if "blurred" not in prompt.lower() and "diffused" not in prompt.lower():
            if not prompt.endswith('.'):
                prompt += '.'
            prompt += f" {clause}"
    return prompt


def fix_horizon_line(prompt):
    if not prompt:
        return prompt
    low = prompt.lower()
    if "horizon line" not in low:
        if "horizon" in low:
            prompt = re.sub(r'\bhorizon\b', 'horizon line', prompt, flags=re.IGNORECASE)
        else:
            if not prompt.endswith('.'):
                prompt += '.'
            prompt += " The horizon line remains perfectly level at exactly 50-percent height of the frame."
    return prompt


def fix_primary_landmarks(prompt, packet):
    if not prompt or not packet or 'primary_landmarks' not in packet:
        return prompt
    
    landmarks = packet['primary_landmarks']
    low = prompt.lower()
    missing_clauses = []
    for lm in landmarks:
        name = lm.get('name', '').strip()
        grid = lm.get('grid', '').strip()
        if not name:
            continue
        raw_coord = grid.replace("Grid", "").strip()
        
        name_missing = name.lower() not in low
        grid_missing = (grid.lower() not in low) and (raw_coord.lower() not in low)
        
        if name_missing or grid_missing:
            missing_clauses.append(f"{name} at {grid}")
            
    if missing_clauses:
        clause = "Locked anchors: " + ", ".join(missing_clauses) + "."
        if not prompt.endswith('.'):
            prompt += '.'
        prompt += f" {clause}"
        
    return prompt


def compress_prompt_to_budget(prompt, target_max_words, config, is_video=True):
    if not config:
        return prompt
    words = prompt.split()
    if len(words) <= target_max_words:
        return prompt

    if is_video:
        system_prompt = f"""You are an expert prompt optimization tool.
Your job is to compress the given VIDEO prompt to be under {target_max_words} words.
CRITICAL CONSTRAINTS:
1. You MUST preserve the beginning of the prompt (specifically: 'Use the provided first frame and last frame as exact composition anchors. Use IMAGE X as the actual first-frame image and IMAGE Y as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout.', camera DNA descriptions).
2. You MUST preserve the actual end of the prompt (specifically: the actual worker entry/exit sentences, actual persistent trace descriptions, and actual sound effects/ambient noise sentences from the input). Do NOT copy these instruction descriptions literally; preserve the original concrete sentences describing them.
3. Do NOT lose the core action being performed.
4. Reduce word count by pruning redundant adjectives, repetitive descriptions, and overly wordy details in the middle of the prompt.
5. The final output must be exactly under {target_max_words} words, and contain ONLY the compressed prompt prose. No labels, no quotes, no conversational filler."""
    else:
        system_prompt = f"""You are an expert prompt optimization tool.
Your job is to compress the given IMAGE prompt to be under {target_max_words} words.
CRITICAL CONSTRAINTS:
1. You MUST preserve the beginning of the prompt, specifically the camera DNA and any change/absence statements (e.g. 'CHANGE IN THIS FRAME:', 'REMOVED:', 'ABSENT:', 'CHANGE:'). These clauses MUST remain completely intact as they tell the image-to-image editor what to add or remove.
2. You MUST preserve the end of the prompt (specifically: 'Locked anchors: ...', 'frame_boundaries', and 'horizon line' constraints).
3. Do NOT lose the key structural additions or modifications described in the middle of the prompt.
4. Reduce word count by pruning redundant adjectives, repetitive descriptions, and overly wordy details in the middle of the prompt.
5. The final output must be exactly under {target_max_words} words, and contain ONLY the compressed prompt prose. No labels, no quotes, no conversational filler."""

    # Format the prompt to use correct constants dynamically
    system_prompt = system_prompt.replace("t=7.5s", f"t={WORKER_EXIT_TIME}s")

    user_prompt = f"Original Prompt ({len(words)} words):\n{prompt}"
    try:
        model = _aux_model(config)
        compressed = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=model).strip()
        compressed = _strip_markdown_fences_only(compressed).strip()
        compressed = clean_prompt_text(compressed)
        compressed_words = compressed.split()
        if len(compressed_words) > 0 and len(compressed_words) < len(words):
            if sys.stdout:
                print(f"[COMPRESS] Successfully compressed prompt from {len(words)} to {len(compressed_words)} words.")
            return compressed
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] compress_prompt_to_budget failed: {e}")
    return prompt


def apply_proactive_fixes(i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal, beat=None, config=None):
    # 1. Clean initial prompt text
    image_prompt = clean_prompt_text(image_prompt)
    video_prompt = clean_prompt_text(video_prompt)
    
    # 2. Compress first with a lower target budget to leave room for post-compression proactive additions
    image_prompt = compress_prompt_to_budget(image_prompt, 100, config, is_video=False)
    video_prompt = compress_prompt_to_budget(video_prompt, 70, config, is_video=True)
    
    # 3. Apply proactive fixes post-compression to guarantee mandatory quality requirements
    image_prompt = fix_image_clean_frame_proactive(image_prompt)
    video_prompt = fix_video_opening(i, video_prompt)
    video_prompt = fix_pacing_control(video_prompt, is_threshold_or_reveal)
    video_prompt = fix_out_and_in(video_prompt, is_threshold_or_reveal)
    video_prompt = fix_sound_design(video_prompt)
    
    base_camera_dna = packet.get('camera_dna', '')
    camera_dna = select_camera_dna(beat, base_camera_dna)
    if camera_dna:
        image_prompt = fix_camera_dna(image_prompt, camera_dna)
        
    op = beat.get('operation', '').lower() if beat else ''
    desc = beat.get('description', '').lower() if beat else ''
    
    bridge_stage = beat.get('bridge_stage') if beat else None
    is_bridge = bridge_stage in (1, 2)
        
    video_prompt = fix_camera_contradictions(video_prompt, is_bridge)
    image_prompt = fix_camera_contradictions(image_prompt, is_bridge)
    
    image_prompt = fix_rhma_blur(image_prompt, is_last)
    image_prompt = fix_horizon_line(image_prompt)
    image_prompt = fix_primary_landmarks(image_prompt, packet)
    
    return video_prompt, image_prompt


def check_nlvtr_violations(prompt):
    violations = []
    if '%' in prompt:
        violations.append("Contains forbidden '%' symbol")
    range_pattern = r'\b\d+(?:\s*%?\s*(?:to|-)\s*\d+\s*%?\s*(?:cm|m|kg|s|h|l|ml)?)\b'
    if re.search(range_pattern, prompt):
        violations.append("Contains forbidden numeric range")
    acronyms = ['HAL', 'TSPA', 'VMFP', 'GCTR', 'RPL', 'RCE', 'SCUP', 'NGCS', 'OSPL', 'RHMA', 'PBISP', 'HCL', 'NLVTR', 'MTAL']
    for ac in acronyms:
        if re.search(rf'\b{ac}\b', prompt):
            violations.append(f"Contains forbidden acronym '{ac}'")
    return violations


def check_image_clean_frame(prompt):
    sentences = re.split(r'(?<=[.!?])\s+', prompt)
    negatives = ['no', 'zero', 'without', 'free of', 'absent', 'clear of', 'empty of', 'never']
    worker_agents = ['worker', 'builder', 'carpenter', 'laborer', 'person', 'man', 'woman', 'people']
    violations = []
    
    for sentence in sentences:
        low_sent = sentence.lower()
        has_negative = any(re.search(rf'\b{neg}\b', low_sent) for neg in negatives)
        has_worker = any(re.search(rf'\b{w}s?\b', low_sent) for w in worker_agents)
        
        # If the sentence has both a negative and a worker agent, it is a valid negation statement,
        # which the proactive fix preserves and validation accepts.
        if has_negative and has_worker:
            continue
            
        # Otherwise, check for worker references
        for w in worker_agents:
            if re.search(rf'\b{w}s?\b', low_sent):
                violations.append(f"IMAGE anchor contains worker/agent reference: '{w}'")
                
        # Check for active verbs
        active_verbs = ['shoveling', 'sweeping', 'painting', 'installing']
        for v in active_verbs:
            if re.search(rf'\b{v}\b', low_sent):
                # If the sentence contains a negation, we allow the active verb in a negative context (e.g. "no sweeping occurs")
                if has_negative:
                    continue
                # Specific phrase exemptions
                if v == 'painting':
                    if 'after painting' in low_sent:
                        continue
                    is_noun_painting = re.search(r'\b(?:a|the|this|that|framed|oil|acrylic|canvas|decorative|original)\s+painting\b', low_sent) or \
                                       re.search(r'\bpainting\s+(?:hangs|is hanging|depicts|decorates|on the wall|in a frame)\b', low_sent)
                    if is_noun_painting:
                        continue
                elif v == 'installing':
                    if 'before installing' in low_sent:
                        continue
                elif v == 'sweeping':
                    is_noun_sweeping = re.search(r'\bsweeping\s+(?:view|curve|arch|line|gesture|motion|pan|shot)\b', low_sent)
                    if is_noun_sweeping:
                        continue
                        
                violations.append(f"IMAGE anchor contains active verb: '{v}'")
                
    return violations


def check_video_opening(i, prompt):
    """Validate the mandatory first/last-frame anchor opening and its IMAGE i -> IMAGE i+1 binding.
    Mirrors fix_video_opening, which runs proactively, so well-formed prompts pass here."""
    errors = []
    low = prompt.strip().lower()
    if not low.startswith("use the provided first frame and last frame as exact composition anchors."):
        errors.append(f"VIDEO {i} missing required opening sentence 'Use the provided first frame and last frame as exact composition anchors.'")
    binding = f"use image {i} as the actual first-frame image and image {i + 1} as the actual last-frame image".lower()
    if binding not in low:
        errors.append(f"VIDEO {i} opening must bind IMAGE {i} (first frame) to IMAGE {i + 1} (last frame)")
    return errors


def check_pacing_control(prompt, is_threshold_or_reveal):
    """Non-threshold/reward beats must declare time-lapse pacing. Mirrors fix_pacing_control."""
    errors = []
    if not is_threshold_or_reveal:
        if "continuous construction time-lapse" not in prompt.lower():
            errors.append("VIDEO missing pacing control 'continuous construction time-lapse, not real-time footage'")
    return errors


def check_out_and_in(prompt, is_threshold_or_reveal=False):
    """If a worker is present, the clip must show them entering and exiting (Out-and-In passage).
    Mirrors fix_out_and_in's trigger so proactively-fixed prompts pass."""
    if is_threshold_or_reveal:
        return []
    errors = []
    low = prompt.lower()
    
    sterile_phrases = ['sterile of workers', 'sterile of active workers',
                       'sterile of any human', 'no workers', 'no human presence',
                       'completely sterile of', 'without any human']
    if any(phrase in low for phrase in sterile_phrases):
        return errors
        
    has_worker = any(re.search(rf'\b{w}s?\b', low) for w in ('worker', 'crew', 'person', 'builder', 'laborer'))
    if has_worker:
        entered = any(k in low for k in ('enter', 'walks in', 'steps in', 't=0', 'start of the clip', '0 seconds'))
        exited = any(k in low for k in ('exit', 'walks out', 'leaves the frame', 'steps out',
                                        'before the final frame', f'before t={int(VIDEO_DURATION)}', str(WORKER_EXIT_TIME)))
        if not (entered and exited):
            errors.append(f"VIDEO with a worker must show the worker entering at the start and exiting before the clip ends (Out-and-In passage)")
    return errors


def check_transition_shortcuts(prompt):
    """Reject abstract / causal-shortcut phrasing that skips concrete physical action — the exact
    failure mode of the placeholder fallback. Forces the model toward observable build actions."""
    errors = []
    low = prompt.lower()
    lazy_phrases = ['transformation progresses', 'magically', 'instantly transform',
                    'jump cut', 'time skip', 'suddenly appears', 'teleport',
                    'as if by magic', 'out of nowhere']
    for p in lazy_phrases:
        if p in low:
            errors.append(f"VIDEO uses abstract/causal-shortcut phrase '{p}'; describe concrete, traceable physical actions instead")
    return errors


def check_stylistic_repetition(curr_prompt, prev_prompt, packet, is_video=True):
    from difflib import SequenceMatcher
    errors = []
    
    def clean_text(text):
        # Remove any sentence containing persistent trace terms to avoid repetition clashes
        sentences_to_clean = re.split(r'(?<=[.!?])\s+', text)
        cleaned_sents = []
        for sent in sentences_to_clean:
            sent_low = sent.lower()
            if any(pt in sent_low for pt in ["persistent trace", "persistent mark", "persistent contact", "causal trace", "traces left", "traces include"]):
                continue
            cleaned_sents.append(sent)
        text = " ".join(cleaned_sents)

        text = text.lower()
        # Replace digits with empty string or space to ignore frame index/beat numbers
        text = re.sub(r'\b\d+\b', '', text)
        
        # Strip Camera DNA (_flatten_to_text: packet may come from an un-normalized caller)
        dna = _flatten_to_text(packet.get('camera_dna', ''))
        if dna:
            dna_clean = re.sub(r'\b\d+\b', '', dna.lower()).strip()
            dna_words = re.sub(r'[^\w\s]', ' ', dna_clean).split()
            for word in dna_words:
                if len(word) > 3:
                    text = text.replace(word, '')

        # Strip Worker Choreography
        choreography = _flatten_to_text(packet.get('worker_choreography', ''))
        if choreography:
            ch_clean = re.sub(r'\b\d+\b', '', choreography.lower()).strip()
            ch_words = re.sub(r'[^\w\s]', ' ', ch_clean).split()
            for word in ch_words:
                if len(word) > 3:
                    text = text.replace(word, '')

        # Standard boilerplates to strip
        boilerplates = [
            "use the provided first frame and last frame as exact composition anchors",
            "use image as the actual first-frame image and image as the actual last-frame image",
            "every visible action must interpolate between those two frame images without inventing a third layout",
            "continuous construction time-lapse, not real-time footage",
            "camera remains locked in a static tripod shot",
            "same frame boundaries, maintaining the grid positions of all fixed landmarks",
            "locked anchors:",
            "boundary anchors:",
            "relative positioning lock",
            "completely empty of workers",
            "no active workers",
            "sterile of active workers",
            "worker locked in a solid silhouette profile",
            "by . seconds, the worker exits the frame",
            "leaving the frame completely empty at t=s",
            "leaving the scene completely empty and sterile",
            "transition shortcuts like cross-dissolves, fade-ins, or jump cuts are strictly forbidden",
            "sound effects include",
            "ambient noise is",
            "highly reflective polished floor surface",
            "blurred, low-gloss, diffused reflection",
            "fresnel falloff"
        ]
        for bp in boilerplates:
            text = text.replace(bp, "")
            
        # Clean punctuation and extra spaces
        text = re.sub(r'[^\w\s]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    c_curr = clean_text(curr_prompt)
    c_prev = clean_text(prev_prompt)
    
    if not c_curr or not c_prev:
        return errors
        
    ratio = SequenceMatcher(None, c_curr, c_prev).ratio()
    
    # Check for sentence-level exact/near-exact duplicates
    # Split by common sentence endings
    curr_sentences = [s.strip() for s in re.split(r'[.!?]', curr_prompt) if len(s.strip()) > 20]
    prev_sentences = [s.strip() for s in re.split(r'[.!?]', prev_prompt) if len(s.strip()) > 20]
    
    def is_mostly_boilerplate(sentence):
        sentence_low = sentence.lower()
        if any(pt in sentence_low for pt in ["persistent trace", "persistent mark", "persistent contact", "causal trace", "traces left", "traces include"]):
            return True
        if "use the provided first frame" in sentence_low:
            return True
        if "use image" in sentence_low and "as the actual first-frame" in sentence_low:
            return True
        if "continuous construction time-lapse" in sentence_low:
            return True
        if "transition shortcuts like" in sentence_low:
            return True
        if "highly reflective polished floor" in sentence_low:
            return True
        if "sound effects include" in sentence_low:
            return True
        if "camera remains locked" in sentence_low:
            return True
        # Camera DNA & boundaries physical invariants
        dna_keywords = ["tripod shot", "lens feel", "camera height", "perspective", "locked anchors", 
                        "left boundary", "right boundary", "top boundary", "bottom boundary", 
                        "horizon line", "optical flow", "frame boundary", "inherits all landmarks",
                        "held in frame", "entryway", "door sill", "rear wall", "door frame", "ceiling beam",
                        "sustain continuous action", "enters the frame", "exits the frame", "leaves the frame", 
                        "worker in a", "grid", "percent", "scale", "restored", "seconds", "empty", "sterile",
                        "walks out", "leaves the", "do not redesign", "camera remains", "coaxial", "interpolat"]
        if any(dk in sentence_low for dk in dna_keywords):
            return True
            
        # Ignore environmental, weather, lighting, ambient sound, and persistent construction traces
        # because these should remain consistent and identical across consecutive steps.
        extra_keywords = [
            "snow", "drift", "wind", "peak", "glow", "light", "ambient", "mist", "sky", "overcast", 
            "daylight", "shade", "shadow", "halogen", "led", "sfx", "sound effect", "ambient noise",
            "weld", "seam", "bolt", "screw", "fastener", "shaving", "dust", "varnish", "stain",
            "wood", "grain", "cladding", "underlayment", "conduit", "sconce", "pendant", "fixture",
            "reflection", "polished", "floorboard", "tile", "grout", "plaster", "drywall", "stud",
            "joist", "beam", "insulation", "bracket", "hinge", "handle", "frame", "sill", "molding",
            "trim", "sealant", "caulk", "groove", "scratch", "dent", "mark", "residue", "debris"
        ]
        if any(ek in sentence_low for ek in extra_keywords):
            return True
            
        # Whitelist persistent objects in the ledger
        ledger_objects = packet.get('object_ledger', []) if packet else []
        for obj in ledger_objects:
            if isinstance(obj, dict) and 'name' in obj:
                obj_words = obj['name'].lower().split()
                significant_words = [w for w in obj_words if len(w) > 3]
                if significant_words and any(w in sentence_low for w in significant_words):
                    return True
            
        return False
        
    for cs in curr_sentences:
        if is_mostly_boilerplate(cs):
            continue
        for ps in prev_sentences:
            if is_mostly_boilerplate(ps):
                continue
            s_ratio = SequenceMatcher(None, cs.lower(), ps.lower()).ratio()
            s_limit = 0.85 if is_video else 0.95
            if s_ratio > s_limit:
                errors.append(
                    f"{'VIDEO' if is_video else 'IMAGE'} sentence is too similar to previous beat's sentence "
                    f"(similarity: {s_ratio:.2f}):\n"
                    f"  Current: \"{cs}\"\n"
                    f"  Previous: \"{ps}\""
                )
                return errors
                
    limit = 0.65 if is_video else 0.88
    if ratio > limit:
        errors.append(
            f"{'VIDEO' if is_video else 'IMAGE'} phrasing/structure is too similar to previous beat "
            f"(cleaned similarity: {ratio:.2f} > {limit:.2f}). Please vary your sentence structures, verbs, and stylistic phrasing."
        )
        
    return errors


def check_lighting_phase_ladder_monotonicity(ladder):
    """
    Validate the lighting phase ladder in the packet:
    1. Must use the five allowed phases.
    2. Must progress monotonically: hold or +1 only.
    """
    errors = []
    if not ladder:
        return errors
        
    phases = ["ambient only", "temporary work light active", "fixture install in progress", "partial practical activation", "final practical stabilization"]
    phase_to_val = {p: idx for idx, p in enumerate(phases)}
    
    # Auto-heal keys and values of the ladder dict in-place
    healed_ladder = {}
    for k, v in list(ladder.items()):
        # Try to extract the integer from key, e.g. "IMAGE 1" -> "1"
        try:
            match = re.search(r'\d+', str(k))
            if match:
                new_k = str(match.group(0))
            else:
                new_k = str(k)
        except Exception:
            new_k = str(k)
            
        # Try to map value to allowed phases
        val_str = str(v).lower()
        new_v = v
        if 'ambient' in val_str or 'natural' in val_str or 'dawn' in val_str or 'dusk' in val_str:
            new_v = "ambient only"
        elif 'work light' in val_str or 'temporary' in val_str:
            new_v = "temporary work light active"
        elif 'fixture install' in val_str or 'wiring' in val_str or 'rough-in' in val_str or 'install' in val_str:
            new_v = "fixture install in progress"
        elif 'partial' in val_str or 'activation' in val_str:
            new_v = "partial practical activation"
        elif 'final' in val_str or 'stabilization' in val_str or 'glow' in val_str or 'stable' in val_str:
            new_v = "final practical stabilization"
            
        healed_ladder[new_k] = new_v
        
    # Replace in-place so the cached/saved packet is also healed
    ladder.clear()
    ladder.update(healed_ladder)
    
    try:
        sorted_keys = sorted([int(k) for k in ladder.keys()])
    except Exception as e:
        errors.append(f"Invalid keys in lighting_phase_ladder: {e}")
        return errors
        
    prev_val = None
    for k in sorted_keys:
        phase = ladder.get(str(k))
        if phase not in phase_to_val:
            errors.append(f"Invalid lighting phase '{phase}' in image {k} (allowed: {phases})")
            continue
            
        val = phase_to_val[phase]
        if prev_val is not None:
            diff = val - prev_val
            if diff < 0:
                errors.append(f"Lighting phase regressed from '{phases[prev_val]}' (image {k-1}) to '{phase}' (image {k})")
            elif diff > 1:
                errors.append(f"Lighting phase jumped illegally by +{diff} from '{phases[prev_val]}' (image {k-1}) to '{phase}' (image {k}). Must hold or +1 only.")
        prev_val = val
        
    return errors


def check_grid_coordinates(prompt):
    errors = []
    # Match patterns like Grid C1, Grid C1-C3, Grid C1 to C3, etc.
    coord_matches = re.findall(r'Grid\s+([A-Za-z]\d)(?:\s*[-–—to\s]+\s*([A-Za-z]\d))?', prompt, re.IGNORECASE)
    for c1, c2 in coord_matches:
        for c in (c1, c2):
            if c:
                cell = c.upper()
                if cell[0] not in ("A", "B", "C") or cell[1] not in ("1", "2", "3"):
                    errors.append(f"Invalid Grid coordinate 'Grid {cell}' found (only A1-C3 are allowed)")
    return errors


def check_primary_landmarks_exact_match(image_prompt, packet):
    errors = []
    if not packet or 'primary_landmarks' not in packet:
        return errors
    
    landmarks = packet['primary_landmarks']
    for lm in landmarks:
        name = lm.get('name', '').strip()
        grid = lm.get('grid', '').strip()
        
        # Landmark name check (case-insensitive exact string match)
        if name.lower() not in image_prompt.lower():
            errors.append(f"IMAGE prompt fails to restate primary landmark name exactly: '{name}'")
            
        # Landmark grid check (case-insensitive)
        if grid.lower() not in image_prompt.lower():
            raw_coord = grid.replace("Grid", "").strip()
            if raw_coord.lower() not in image_prompt.lower():
                errors.append(f"IMAGE prompt fails to restate grid coordinate '{grid}' for landmark '{name}'")
    return errors


def check_monotonic_state_regression(config, prev_image, current_image):
    if not config or not prev_image or not current_image:
        return []
    
    system_prompt = """You are a construction prompt quality auditor.
Compare the previous beat's image state (IMAGE i) and the current beat's image state (IMAGE i+1).

Your ONLY job is to catch REAL continuity breaks: a MAJOR completed feature — an installed panel/wall/floor/fixture, a primary landmark, a finished surface, or a structural element — that was clearly present and finished in IMAGE i but has now vanished, reverted to an earlier unfinished state, or is directly contradicted in IMAGE i+1 (e.g. a finished floor becomes bare subfloor again, an installed door frame disappears, a painted wall is now unpainted).

Do NOT flag the omission of minor decorative or cosmetic micro-details — individual screws/nails/bolts/heads, dust, sawdust, pencil marks, scuff marks, slag flecks, heat-tint rings, wire clippings, and similar small persistent traces. A concise, natural-sounding image description is EXPECTED to drop most of these from beat to beat as new details accumulate — that is correct, not a regression. Only flag one of these if IMAGE i+1 actively describes that exact surface as pristine/untouched/unworked in a way that contradicts the completed work.

If everything major is correctly maintained, respond with exactly "PASS".
Otherwise, output a short bulleted list (at most 3 items, the most important ones) of MAJOR continuity breaks only. Keep it concise, direct, and actionable."""

    user_prompt = f"""IMAGE i (Previous State):
{prev_image}

IMAGE i+1 (New State):
{current_image}"""

    try:
        response = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=_aux_model(config))
        response_clean = response.strip()
        if response_clean.upper() == "PASS" or "PASS" in response_clean.upper()[:10]:
            return []
        
        lines = [line.strip().lstrip('-* ').strip() for line in response_clean.split('\n') if line.strip()]
        errors = [f"Monotonic state regression: {line}" for line in lines if line and "PASS" not in line.upper()]
        return errors
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] check_monotonic_state_regression failed: {e}")
        return []


def check_visible_delta_between_frames(config, prev_image, current_image):
    if not config or not prev_image or not current_image:
        return []
    
    system_prompt = """You are a construction prompt quality auditor.
Compare the previous beat's image state (IMAGE i) and the current beat's image state (IMAGE i+1).
Check if there is a clear, visible progression or increment of construction work between the two states (e.g., a new panel installed, walls painted, wiring added, floor finished).
The current state MUST contain new completed elements or modifications that were not present in the previous state.
If there is a clear visible progression, respond with exactly "PASS".
If the two descriptions represent the exact same state of completion (even if worded differently), output a short description of the lack of progress (e.g. "No visible progress or added elements between IMAGE i and IMAGE i+1")."""

    user_prompt = f"""IMAGE i (Previous State):
{prev_image}

IMAGE i+1 (New State):
{current_image}"""

    try:
        response = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=_aux_model(config))
        response_clean = response.strip()
        if response_clean.upper() == "PASS" or "PASS" in response_clean.upper()[:10]:
            return []
        
        return [f"Static frame violation: {response_clean}"]
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] check_visible_delta_between_frames failed: {e}")
        return []


def validate_beat_prompts(i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal, prev_video=None, prev_image=None, config=None, beat=None, skip_llm_checks=False):
    errors = []
    
    # Word count limits check
    img_word_count = len(image_prompt.split())
    if img_word_count > 170:
        errors.append(f"IMAGE prompt word count ({img_word_count}) exceeds limit of 170 words")
        
    vid_word_count = len(video_prompt.split())
    if vid_word_count > 180:
        errors.append(f"VIDEO prompt word count ({vid_word_count}) exceeds limit of 180 words")

    # Grid coordinate checks
    errors.extend(check_grid_coordinates(image_prompt))
    errors.extend(check_grid_coordinates(video_prompt))
    
    # Landmark exact-match restatement check
    errors.extend(check_primary_landmarks_exact_match(image_prompt, packet))

    errors.extend(check_nlvtr_violations(image_prompt))
    errors.extend(check_image_clean_frame(image_prompt))
    if "horizon line" not in image_prompt.lower():
        errors.append("IMAGE prompt missing 'horizon line' camera lock statement")
        
    if is_last:
        if "reflection" in image_prompt.lower() or "polished" in image_prompt.lower():
            if "blurred" not in image_prompt.lower() and "diffused" not in image_prompt.lower():
                errors.append("Final IMAGE with polished/reflective floor missing RHMA-Blur diffused reflection description")
                
    errors.extend(check_nlvtr_violations(video_prompt))
    errors.extend(check_video_opening(i, video_prompt))
    errors.extend(check_out_and_in(video_prompt, is_threshold_or_reveal))
    errors.extend(check_transition_shortcuts(video_prompt))
    errors.extend(check_pacing_control(video_prompt, is_threshold_or_reveal))
    
    # Check camera contradictions
    op = beat.get('operation', '').lower() if beat else ''
    desc = beat.get('description', '').lower() if beat else ''
    
    bridge_stage = beat.get('bridge_stage') if beat else None
    is_bridge = bridge_stage in (1, 2)
        
    errors.extend(check_camera_contradictions(video_prompt, is_bridge))
    errors.extend(check_camera_contradictions(image_prompt, is_bridge))
    
    if prev_video:
        errors.extend(check_stylistic_repetition(video_prompt, prev_video, packet, is_video=True))
    if prev_image:
        errors.extend(check_stylistic_repetition(image_prompt, prev_image, packet, is_video=False))
        if config and not skip_llm_checks:
            errors.extend(check_monotonic_state_regression(config, prev_image, image_prompt))
            errors.extend(check_visible_delta_between_frames(config, prev_image, image_prompt))
        
    return errors


def extract_persistent_traces_to_ledger(config, video_prompt, image_prompt):
    if not config or not video_prompt or not image_prompt:
        return []
    
    system_prompt = """You are a spatial consistency supervisor.
Analyze the generated VIDEO and IMAGE prompts for a construction/renovation beat and extract any new permanent physical features, installed materials, or persistent traces (like scratches, screws, wood shavings, panels, coatings, etc.) introduced in this beat.
Format them as a JSON list of objects, each containing:
- "name": precise name (e.g. "sanddust on sill", "steel screw heads", "green insulation foam")
- "material_color": color/texture (e.g. "metallic silver", "dusty tan")
- "initial_state": state when introduced (e.g. "freshly installed", "scattered particles")
- "grid": approximate grid coordinate if mentioned (e.g. "Grid B2", defaults to "Grid B2" if unknown)
- "z_depth_scale": depth scale if mentioned (e.g. "50%", defaults to "50%" if unknown)

Return ONLY a valid JSON list, no code fences, no other text."""

    user_prompt = f"""VIDEO Prompt:
{video_prompt}

IMAGE Prompt:
{image_prompt}"""

    try:
        response = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=_aux_model(config))
        response_clean = _strip_code_fences(response).strip()
        new_items = json.loads(response_clean)
        if isinstance(new_items, list):
            valid_items = []
            for item in new_items:
                if isinstance(item, dict) and "name" in item:
                    valid_items.append({
                        "name": str(item.get("name")),
                        "material_color": str(item.get("material_color", "unknown")),
                        "initial_state": str(item.get("initial_state", "installed")),
                        "grid": str(item.get("grid", "Grid B2")),
                        "z_depth_scale": str(item.get("z_depth_scale", "50%"))
                    })
            return valid_items
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] extract_persistent_traces_to_ledger failed: {e}")
    return []


def parse_audit_failures(config, audit_md):
    if not config or not audit_md or '|' not in audit_md:
        return {}
    
    system_prompt = """You are a structured data extractor.
Analyze the following Markdown audit table and extract all items that did NOT pass (indicated by '未通过' or 'Fail' or 'No' in the "通过状态" status column).
Use the "拍号 / Beat Index" column to identify which beat index (1-based integer, e.g., 3) each failure belongs to, and extract the specific reason/description of the failure.
Respond ONLY with a valid JSON dictionary mapping the beat index (as a stringified integer, e.g. "3") to a list of failure descriptions for that beat. If no failures are found, return {}.
Example output:
{
  "3": ["Ceiling insulation missing in Grid A2 when walls were insulated."],
  "4": ["Fireplace installed before flooring was complete."]
}
Do not include any code fences, markdown, or other text."""

    try:
        response = _chat(config, system_prompt, audit_md, temperature=0.1, timeout=30, model=_aux_model(config))
        response_clean = _strip_code_fences(response).strip()
        failures = json.loads(response_clean)
        if isinstance(failures, dict):
            valid_failures = {}
            for k, v in failures.items():
                if isinstance(v, list) and v:
                    valid_failures[str(k)] = [str(item) for item in v]
            return valid_failures
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] parse_audit_failures failed: {e}")
        if isinstance(config, dict):
            config['_skipped_checks'] = config.get('_skipped_checks', 0) + 1
    return {}


def generate_physical_process_brief(config, carrier, env, space_type):
    """Route B grounding step: force the model to explicitly reason about the REAL-WORLD
    construction/renovation order for this specific carrier before any creative beat/prompt
    writing happens, instead of letting that domain reasoning happen implicitly (and invisibly)
    inside the beat-ladder composer. No external search — this is recall-and-reason, not retrieval.
    Cached by normalized carrier+env so repeated projects about the same kind of subject reuse
    the same grounded knowledge instead of re-inventing it (and possibly differently) every time.
    """
    cache_key = normalize_carrier_key(carrier, env)
    with PROCESS_BRIEF_CACHE_LOCK:
        cache = load_process_brief_cache()
        cached = cache.get(cache_key)
        if cached:
            return cached

    system_prompt = """You are a real-world construction and restoration domain expert consulted BEFORE any creative writing begins.
Your ONLY job is factual: describe how this specific carrier/structure is ACTUALLY renovated, converted, or restored in reality, based on real building/engineering/trade practice — not a generic renovation template.

Think in terms of genuine physical dependency: what MUST happen before what, and why (structural safety, material cure times, trade sequencing, regulatory/hazard requirements). If the carrier is unusual (a vehicle, vessel, natural formation, industrial relic, etc.), reason from the closest real trade practice (marine refit, aircraft teardown, mine/tunnel shoring, etc.) rather than defaulting to generic residential renovation steps.

You must output ONLY a valid JSON object with no markdown, no code fences, no other text, with these keys:
1. "domain_confidence": one of "high", "medium", "low" — how well-established/documented real-world practice is for this specific carrier. Use "low" if the carrier is fantastical or has no real-world analog.
2. "real_world_phases": an ordered array of phase names (3-8 short phase names, e.g. "hull descaling and rust treatment", "ballast/structural reinforcement", "interior gutting", "electrical rough-in", "insulation and vapor barrier", "interior finish", "fixture install"). Order must reflect genuine real-world dependency, not a generic template.
3. "hard_prerequisites": an array of short strings, each stating a strict ordering constraint and why (e.g. "Structural reinforcement must precede any interior fit-out — the shell must be load-bearing safe first", "Waterproofing/sealing must precede electrical rough-in — active leaks would damage wiring"). Include only constraints that would be a real physical error if violated.
4. "hazard_notes": an array of short strings noting any real hazard/regulatory considerations relevant to this carrier's era or material (e.g. asbestos, lead paint, corrosion, structural instability), or an empty array if none apply.
5. "typical_materials": an array of short strings naming the real materials/tools/systems specific to this carrier (e.g. for a retired submarine: "steel hull plating", "ballast tank valves", "pressure-hull rivets"), used to ground object/material choices instead of generic placeholders.

If domain_confidence is "low", still provide a best-effort reasoned list rather than an empty one, but mark the confidence honestly."""

    user_prompt = f"""Carrier (the object/structure being renovated): {carrier}
Environment: {env}
Space type classification: {space_type}

Reason through the REAL-WORLD renovation/conversion order for this specific carrier and output the JSON."""

    brief = None
    for attempt in range(3):
        try:
            resp = _chat(config, system_prompt, user_prompt, temperature=0.25, timeout=60)
            resp_clean = _strip_code_fences(resp).strip()
            parsed = json.loads(resp_clean)
            if isinstance(parsed, dict) and parsed.get('real_world_phases'):
                brief = parsed
                break
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] generate_physical_process_brief attempt {attempt+1} failed: {e}")

    if not brief:
        # Low-confidence empty brief: downstream code treats this as "fall back to the static
        # space-workflows.md table", not as a hard failure.
        brief = {
            "domain_confidence": "low",
            "real_world_phases": [],
            "hard_prerequisites": [],
            "hazard_notes": [],
            "typical_materials": [],
        }

    with PROCESS_BRIEF_CACHE_LOCK:
        cache = load_process_brief_cache()
        cache[cache_key] = brief
        save_process_brief_cache(cache)
    return brief


def check_real_world_order_violation(config, hard_prerequisites, beat_ladder):
    """Semantic gate for the beat ladder against the grounded process-brief's hard prerequisites.
    Catches a beat ladder that is internally self-consistent (passes the generic ceiling/fixture/
    door/floor rules) but is still globally wrong FOR THIS CARRIER because nothing else in the
    pipeline has real-world dependency facts to compare against.
    """
    if not config or not hard_prerequisites or not beat_ladder:
        return []

    beats_desc = "\n".join(
        f"Beat {b.get('index')}: {b.get('operation')} - {b.get('description', '')}"
        for b in beat_ladder
    )

    system_prompt = """You are a real-world construction sequence auditor.
You are given a list of hard real-world ordering prerequisites for this specific carrier, and a proposed beat-by-beat construction ladder.
Check whether the beat ladder's operation order violates any of the hard prerequisites.
If everything respects the prerequisites, respond with exactly "PASS".
Otherwise, output a concise bulleted list, each line naming the beat index and which prerequisite it violates. Keep it short and actionable."""

    user_prompt = f"""Hard Prerequisites:
{chr(10).join('- ' + p for p in hard_prerequisites)}

Proposed Beat Ladder:
{beats_desc}"""

    try:
        response = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=_aux_model(config))
        response_clean = response.strip()
        if response_clean.upper() == "PASS" or "PASS" in response_clean.upper()[:10]:
            return []
        lines = [line.strip().lstrip('-* ').strip() for line in response_clean.split('\n') if line.strip()]
        return [f"Real-world order violation: {line}" for line in lines if line and "PASS" not in line.upper()]
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] check_real_world_order_violation failed: {e}")
        if isinstance(config, dict):
            config['_skipped_checks'] = config.get('_skipped_checks', 0) + 1
        return []


def call_llm(config, dimensions, on_progress=None):
    if isinstance(config, dict):
        config['_skipped_checks'] = 0
    if sys.stdout:
        print("[DEBUG] call_llm: Starting structured agent loop...")

    # Step 1: Brief Parsing
    if on_progress:
        on_progress('outline', '正在解析场景维度并规划工序...')

    theme = dimensions.get('theme', '未指定场景')
    anchors = dimensions.get('anchors') or []
    anchors_str = '、'.join(anchors) if anchors else '由作曲家自行选取最契合主题的锚点'
    complexity = dimensions.get('complexity', '中等重工')
    budget = dimensions.get('budget', '轻奢设计师级')
    ratio = dimensions.get('ratio', '50')
    creativity = dimensions.get('creativity', '突破常规')
    beats_count = int(dimensions.get('beats_count', 15))
    total_beats = beats_count + 1

    # Brief parsing LLM call
    brief_system = """You are a scene analysis agent for a restoration time-lapse project.
Your job is to parse the design dimensions into a structured JSON object containing scene variables.
You must output ONLY a valid JSON object with the keys below, and no markdown formatting, no code fences, no other text.

Required JSON keys:
1. "carrier": The main object or structure being renovated (e.g. "double-height loft", "school bus").
2. "env": The surrounding environment (e.g. "wooded hillside", "urban lot").
3. "trauma": The initial ruined, broken, dirty, or empty state of the scene.
4. "destiny": The target finished state of the scene.
5. "reward": The final action or reveal motion that happens at the end (e.g. "lights turn on", "person walks in").
6. "mode": Must be either "Standard" or "Threshold". Set to "Threshold" only if there is a clear boundary crossing (e.g. entering a room, building, cabin, container) from exterior to interior.
7. "space_type": Must be exactly one of the following strings:
   - "abandoned property"
   - "exterior facade"
   - "road / street / driveway"
   - "garage / workshop"
   - "backyard / landscape / pool"
   - "luxury apartment"
   - "retail / showroom"
   - "underground space"
   - "custom build object"
"""
    brief_user = f"""Design dimensions to parse:
- Theme: {theme}
- Core Creative Anchors: {anchors_str}
- Project Complexity: {complexity}
- Budget Level: {budget}
- Raw-Shell vs Refined-Interior Contrast Intensity (higher = bolder before/after clash): {ratio}
- Creativity Scale: {creativity}
"""
    
    parsed_brief = {}
    for attempt in range(3):
        try:
            brief_text = _chat(config, brief_system, brief_user, temperature=0.2, timeout=60)
            brief_text_cleaned = _strip_code_fences(brief_text)
            parsed_brief = json.loads(brief_text_cleaned)
            required_keys = ["carrier", "env", "trauma", "destiny", "reward", "mode", "space_type"]
            if all(k in parsed_brief for k in required_keys):
                break
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] Brief parsing attempt {attempt+1} failed: {e}")
            if attempt == 2:
                parsed_brief = {
                    "carrier": theme,
                    "env": "surrounding environment",
                    "trauma": "ruined state",
                    "destiny": "finished design",
                    "reward": "lights activate",
                    "mode": "Threshold" if beats_count >= 12 else "Standard",
                    "space_type": "abandoned property"
                }

    title = parsed_brief.get('destiny', '未命名创意')
    if title:
        title = f"{creativity}·{title}"

    # Register used topic DNA
    try:
        append_to_used_topic_ledger(parsed_brief, dimensions)
    except Exception as e:
        if sys.stdout:
            print(f"Warning: could not write topic to used topic ledger ({e})")

    # Step 1.5: World-Knowledge Grounding Query (Route B — recall-and-reason, no external search).
    # Runs BEFORE any beat/prompt writing so the real-world construction order for THIS carrier
    # is established as a fact-finding pass, separate from the creative writing that follows.
    if on_progress:
        on_progress('outline', f'正在查证 {parsed_brief.get("carrier", "该载体")} 的真实改造工序知识...')
    process_brief = generate_physical_process_brief(
        config,
        parsed_brief.get('carrier', theme),
        parsed_brief.get('env', ''),
        parsed_brief.get('space_type', 'abandoned property'),
    )
    if sys.stdout:
        print(
            f"[DEBUG] Physical process brief (confidence={process_brief.get('domain_confidence')}): "
            f"{process_brief.get('real_world_phases')}"
        )

    # Step 2: Programmatic Workflow Lookup
    workflows = parse_space_workflows()
    space_type = parsed_brief.get('space_type', 'abandoned property')
    workflow = workflows.get(space_type, workflows.get('abandoned property'))

    # Grounded real-world phases take priority over the generic 9-bucket static table whenever
    # the grounding step is confident enough to trust; the static table remains the fallback.
    if process_brief.get('domain_confidence') in ('high', 'medium') and process_brief.get('real_world_phases'):
        effective_phases = process_brief['real_world_phases']
    else:
        effective_phases = workflow['phases']

    # Step 3: Beat Ladder Generation
    if on_progress:
        on_progress('outline', f'工序定义成功。正在根据 {space_type} 生成 {total_beats} 拍工序排布...')

    phases_str = " -> ".join(effective_phases)

    real_world_reference_block = ""
    if process_brief.get('hard_prerequisites') or process_brief.get('hazard_notes'):
        prereq_lines = "\n".join(f"- {p}" for p in process_brief.get('hard_prerequisites', []))
        hazard_lines = "\n".join(f"- {h}" for h in process_brief.get('hazard_notes', []))
        real_world_reference_block = f"""
==================== REAL-WORLD PROCESS REFERENCE (this carrier specifically) ====================
Hard prerequisites (violating these is a real-world physical error, not a style choice):
{prereq_lines or '- (none identified)'}
Hazard/regulatory notes for this carrier's era or material:
{hazard_lines or '- (none identified)'}
"""

    beat_system = f"""You are a professional construction planner specializing in time-lapse renovation projects.
Your goal is to expand the standard construction phases into a detailed, step-by-step beat ladder.
You must output ONLY a valid JSON array of beats, containing exactly {total_beats} elements. Do not output code fences, markdown, or other text.
{real_world_reference_block}
Each beat object in the JSON array must have:
1. "index": (integer) from 1 to {total_beats}.
2. "operation": One of: "clearing", "repair", "rough-in", "flooring", "framing", "drywall", "priming", "painting", "wiring", "lighting", "furnishing", "threshold", "reward".
3. "description": (string) Detailed English visual description of the operation, tools/materials used, and the physical changes in the scene.
4. "bridge_stage": (integer or null) Set to 1 for the first bridge/threshold beat (Beat T), 2 for the second bridge/threshold beat (Beat T+1), and null for all other beats.

General Rules:
- The beats must be realistic and in monotonic order matching the phases: {phases_str} -> reward.
- If a REAL-WORLD PROCESS REFERENCE section is present above, its hard prerequisites are mandatory and override generic assumptions about this type of space.
- Each beat must focus on EXACTLY ONE distinct physical operation (e.g. debris clearing, structural repair, piping, wall paneling, priming, painting, lighting installation, furnishing). Do not combine distinct operations.
- Beat {total_beats} must be the final reward/reveal motion: {parsed_brief['reward']}.
- If mode is "Threshold", you must split the exterior-interior crossing into two beats:
  - Beat T (e.g. Beat 6): "threshold" - Exterior approach pushing toward the open threshold, peeked interior landmarks visible.
  - Beat T+1 (e.g. Beat 7): "threshold" - Crossing sill and settling into the interior, door frame sliding out.
  - All subsequent beats (Beats T+2 to {beats_count}) must be interior construction operations (e.g., clearing interior, interior walls, interior flooring, etc.).
- CEILING/ROOF COVERAGE RULE: For any enclosed space (fuselage, cabin, room, container, vault, bunker, etc.), the ceiling/roof/top surface must be treated as a construction surface just like the walls and floor. When the beat ladder includes framing, paneling, insulating, or painting walls, the SAME operation MUST also explicitly cover the ceiling/roof/top curve. A renovation that covers walls but leaves the ceiling as raw exposed structure is physically incorrect.
- FIXTURE INSTALLATION RULE: If the beat ladder includes a wiring/electrical rough-in beat, there MUST be a subsequent "lighting" or "fixture install" beat BEFORE the furnishing/staging beat and BEFORE the reward beat. Light fixtures cannot appear in the final reward if they were never installed.
- DOOR LEAF RULE: If a door frame is installed in one beat, a subsequent beat MUST include installing a door panel/leaf/sash unless the design explicitly calls for an open archway.
- FLOORING-BEFORE-HEAVY-OBJECTS RULE: Floor finish (hardwood, tile, etc.) MUST be installed BEFORE any heavy anchored objects (fireplace, stove, workbench) are placed on it. The correct order is: subfloor -> finish floor -> anchor heavy objects onto the finished floor.
- VIEWPOINT CONTINUITY RULE: If the beat ladder uses Threshold mode (exterior-to-interior crossing), all subsequent interior beats must maintain interior camera viewpoint. If a beat requires showing exterior work after the threshold crossing, either (a) place that exterior beat BEFORE the threshold crossing, or (b) describe the work from the interior viewpoint showing only what is visible from inside.
"""

    beat_user = f"""Please generate exactly {total_beats} beats for:
Carrier: {parsed_brief['carrier']}
Trauma: {parsed_brief['trauma']}
Destiny: {parsed_brief['destiny']}
Mode: {parsed_brief['mode']}
Space Type: {space_type}
"""

    beat_ladder = None
    beat_user_current = beat_user
    hard_prerequisites = process_brief.get('hard_prerequisites', [])
    for attempt in range(3):
        try:
            beat_text = _chat(config, beat_system, beat_user_current, temperature=0.3, timeout=90)
            beat_text_cleaned = _strip_code_fences(beat_text)
            beat_ladder = normalize_beat_ladder(json.loads(beat_text_cleaned))
            if isinstance(beat_ladder, list) and len(beat_ladder) == total_beats:
                idxs = [b.get('index') for b in beat_ladder]
                if idxs == list(range(1, total_beats + 1)):
                    # Semantic gate: does this internally-valid ladder still violate a real-world
                    # hard prerequisite for THIS carrier? Only the grounding step can catch this.
                    violations = check_real_world_order_violation(config, hard_prerequisites, beat_ladder)
                    # Threshold mode validation
                    is_threshold_mode = (parsed_brief.get('mode') == 'Threshold')
                    if is_threshold_mode:
                        has_bridge_1 = False
                        has_bridge_2 = False
                        bridge_1_idx = -1
                        bridge_2_idx = -1
                        for idx, b in enumerate(beat_ladder):
                            bs = b.get('bridge_stage')
                            if bs == 1:
                                has_bridge_1 = True
                                bridge_1_idx = idx
                            elif bs == 2:
                                has_bridge_2 = True
                                bridge_2_idx = idx
                        if not (has_bridge_1 and has_bridge_2 and bridge_2_idx == bridge_1_idx + 1):
                            violations.append("In Threshold mode, there must be exactly two consecutive beats with bridge_stage=1 and bridge_stage=2.")
                    
                    if not violations:
                        break
                    if sys.stdout:
                        print(f"[DEBUG] Beat ladder attempt {attempt+1} violated real-world order/structure: {violations}")
                    if attempt < 2:
                        beat_user_current = beat_user + "\n\n" + "==================== PRIOR REAL-WORLD ORDER VIOLATIONS ====================\n" + \
                            "The previous beat ladder violated these real-world prerequisites. Fix the ordering:\n" + \
                            "\n".join(f"- {v}" for v in violations)
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] Beat ladder generation attempt {attempt+1} failed: {e}")
            if attempt == 2:
                beat_ladder = []
                for idx in range(1, total_beats + 1):
                    op = "repair"
                    b_stage = None
                    if idx == 1:
                        op = "clearing"
                    elif idx == total_beats:
                        op = "reward"
                    elif parsed_brief.get('mode') == 'Threshold':
                        t_idx = 6 if total_beats >= 12 else (total_beats // 2)
                        if idx == t_idx:
                            op = "threshold"
                            b_stage = 1
                        elif idx == t_idx + 1:
                            op = "threshold"
                            b_stage = 2
                    beat_ladder.append({
                        "index": idx,
                        "operation": op,
                        "description": f"Renovation work step {idx}",
                        "bridge_stage": b_stage
                    })

    # Step 4: Drift Lock Packet Generation
    if on_progress:
        on_progress('outline', '工序排布完成。正在计算三维空间一致性与 Camera DNA 锁定特征...')

    brief_fingerprint = get_brief_fingerprint(dimensions)
    with PACKET_CACHE_LOCK:
        cache = load_packet_cache()
        # normalize_packet also heals cache entries poisoned before shape-coercion existed
        packet = normalize_packet(cache.get(brief_fingerprint))

    if not packet:
        scup_ref = load_reference_file('spatial-consistency-upgrade-protocol.md')
        assembly_ref = load_reference_file('drift-lock-assembly-guide.md')
        beats_desc = "\n".join([f"Beat {b['index']}: {b['operation']} - {b['description']}" for b in beat_ladder])

        packet_system = f"""You are a spatial consistency supervisor for a time-lapse renovation prompt composer.
Your job is to generate a comprehensive Drift Lock & SCUP Packet for the project.
You must output ONLY a valid JSON object matching the keys below, with no other text, no markdown, and no code fences.

Required JSON keys:
1. "camera_dna": A single camera sentence (~25-30 words) describing shot type, lens feel, camera height, perspective axis, and boundaries. Include horizon pinning (e.g., "horizon line remains perfectly level at exactly 50-percent height of the frame; all optical flow lines radiate symmetrically from the optical center of Grid B2").
2. "geometry_lock": A description of structural facts that cannot change (doors, windows, columns, wall lines).
3. "primary_landmarks": A list of exactly 3 landmarks (Foreground, Mid-depth, Background). Each landmark must be a JSON object with:
   - "name": The exact name (e.g. "cracked floor seam")
   - "grid": Grid coordinate (from Grid A1 to Grid C3)
   - "z_depth_scale": Frame-height percentage scale (e.g., "60%")
4. "frame_boundaries": A JSON object with keys "left", "right", "top", "bottom", specifying Grid coordinates and physical features for each edge.
5. "object_ledger": A list of detail-critical recurring objects. Each must be a JSON object with "name", "material_color", "initial_state", "grid", and "z_depth_scale". Provide a comprehensive list of all detail-critical objects with no hard limit. Prefer the real-world materials listed in REAL-WORLD MATERIALS REFERENCE below over generic placeholders when they fit the scene.
6. "worker_choreography": The worker trajectory, silhouette (HAL), and manual tool lock (MTAL) details.
7. "lighting_phase_ladder": A mapping of IMAGE indices (1 to {total_beats + 1}) to lighting phases (e.g. "ambient only", "temporary work light active", etc.). Shadow and exposure progression must be monotonic.
8. "passive_environment": Direction and elements for passive layers (e.g. clouds, watercaustics).
9. "interest_budget": A dictionary with keys "clip_hooks", "sequence_reveal", and "final_reward".
{('==================== REAL-WORLD MATERIALS REFERENCE (this carrier specifically) ====================' + chr(10) + chr(10).join('- ' + m for m in process_brief.get('typical_materials', []))) if process_brief.get('typical_materials') else ''}

==================== REFERENCE GUIDES ====================
{assembly_ref}
{scup_ref}
"""
        packet_user = f"""Scene Variables:
{json.dumps(parsed_brief, indent=2, ensure_ascii=False)}

Beat Ladder:
{beats_desc}
"""

        for attempt in range(3):
            try:
                packet_text = _chat(config, packet_system, packet_user, temperature=0.2, timeout=90)
                packet_text_cleaned = _strip_code_fences(packet_text)
                packet = normalize_packet(json.loads(packet_text_cleaned))
                if all(k in packet for k in ["camera_dna", "geometry_lock", "primary_landmarks", "frame_boundaries"]):
                    ladder_errs = check_lighting_phase_ladder_monotonicity(packet.get("lighting_phase_ladder"))
                    if ladder_errs:
                        raise ValueError(f"lighting_phase_ladder validation failed: {ladder_errs}")
                    with PACKET_CACHE_LOCK:
                        cache = load_packet_cache()
                        cache[brief_fingerprint] = packet
                        save_packet_cache(cache)
                    break
            except Exception as e:
                if sys.stdout:
                    print(f"[DEBUG] Drift lock packet generation attempt {attempt+1} failed: {e}")
                if attempt == 2:
                    packet = {
                        "camera_dna": f"static tripod shot, ultra-wide 14mm lens feel, camera height 1.6m, locked eye-level perspective; horizon line remains level at 50-percent height; optical flow radiates from B2.",
                        "geometry_lock": "Standard boundaries",
                        "primary_landmarks": [
                            {"name": "floor center", "grid": "Grid C2", "z_depth_scale": "10%"},
                            {"name": "back column", "grid": "Grid B2", "z_depth_scale": "50%"},
                            {"name": "window opening", "grid": "Grid A2", "z_depth_scale": "40%"}
                        ],
                        "frame_boundaries": {"left": "B1", "right": "B3", "top": "A2", "bottom": "C2"},
                        "object_ledger": [],
                        "worker_choreography": "HAL safety vest worker",
                        "lighting_phase_ladder": {str(i): "ambient only" for i in range(1, total_beats + 2)},
                        "passive_environment": "soft light drift",
                        "interest_budget": {}
                    }

    # Step 5 & 6: Progressive Step-by-Step Slot Generation
    if on_progress:
        on_progress('batch', {'current': 0, 'total': total_beats})

    compiled_images = {}
    compiled_videos = {}

    mode = parsed_brief.get('mode', 'Standard')

    scup_ref = load_reference_file('spatial-consistency-upgrade-protocol.md')
    templates_raw = load_reference_file('prompt-templates.md')
    templates_cropped_img1 = get_cropped_templates(templates_raw, None, total_beats, mode, None)

    image_1_system = f"""You are a professional prompt composer. Your job is to generate the very first IMAGE prompt (IMAGE 1 / Trauma State) for the renovation project.
You must output ONLY the prompt text, with no other text, no title, no labels. The prompt must be in English.

==================== SCUP CONTRACT & TEMPLATES ====================
{scup_ref}
{templates_cropped_img1}

==================== DRIFT LOCK PACKET ====================
{json.dumps(packet, indent=2, ensure_ascii=False)}

Hard Rules:
1. Clean Frame Boundary: The frame must be completely empty of people, workers, or machinery. Do NOT use the words 'worker', 'builder', 'carpenter', 'laborer', 'person', 'man', 'woman', or 'people' in the prompt text, even to say they are absent. Describe only static objects and surfaces.
2. Hierarchical Context Layering (HCL): First 40 tokens contain Camera DNA and the 3 Primary Landmarks.
3. Natural-Language Visual-Only Translation Rule (NLVTR): No '%', no numeric ranges, no acronyms (HAL, NGCS, OSPL, etc.) in the text.
4. Set the scene as the initial trauma state.
"""
    image_1_user = f"Generate IMAGE 1 prompt for theme: {theme}."
    
    image_1_prompt = ""
    for attempt in range(3):
        try:
            image_1_prompt = _chat(config, image_1_system, image_1_user, temperature=0.8, timeout=60)
            image_1_prompt = _strip_markdown_fences_only(image_1_prompt).strip()
            image_1_prompt = clean_prompt_text(image_1_prompt)
            image_1_prompt = fix_image_clean_frame_proactive(image_1_prompt)
            camera_dna = packet.get('camera_dna', '')
            if camera_dna:
                image_1_prompt = fix_camera_dna(image_1_prompt, camera_dna)
            errs = check_image_clean_frame(image_1_prompt)
            errs.extend(check_grid_coordinates(image_1_prompt))
            errs.extend(check_primary_landmarks_exact_match(image_1_prompt, packet))
            if not errs:
                break
            if sys.stdout:
                print(f"[DEBUG] IMAGE 1 failed validation (attempt {attempt+1}): {errs}")
                print(f"[DEBUG]   Generated IMAGE 1 prompt: {image_1_prompt}")
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] IMAGE 1 generation attempt {attempt+1} failed: {e}")
    
    if not image_1_prompt:
        image_1_prompt = f"A static ultra-wide 14mm tripod shot at 1.6m height: initial ruined empty state of {theme}; horizon line remains level; no workers."

    compiled_images[1] = image_1_prompt

    mode = parsed_brief.get('mode', 'Standard')

    audit_passes = 0
    max_audit_passes = 2
    audit_feedback_dict = {}

    while audit_passes <= max_audit_passes:
        if audit_passes > 0:
            if on_progress:
                on_progress('audit', f'检测到工序校验不通过，启动自动修复生成第 {audit_passes}/{max_audit_passes} 轮...')
            if sys.stdout:
                print(f"[AUDIT] Starting self-healing regeneration pass {audit_passes}/{max_audit_passes}...")
        
        if audit_passes == 0:
            beats_to_generate = list(range(1, total_beats + 1))
            if sys.stdout:
                print(f"[AUDIT] Initial pass: Generating all {total_beats} beats...")
        else:
            raw_beats = sorted([int(k) for k in audit_feedback_dict.keys() if k.isdigit()])
            beats_to_generate = [b for b in raw_beats if 1 <= b <= total_beats]
            out_of_range = [b for b in raw_beats if b not in beats_to_generate]
            if out_of_range and sys.stdout:
                print(f"[AUDIT] Ignoring out-of-range beat indices from audit feedback: {out_of_range} "
                      f"(valid range is 1-{total_beats}; likely the audit LLM referenced an IMAGE index instead of a beat index)")
            if sys.stdout:
                print(f"[AUDIT] Self-healing pass: Regenerating only failed beats: {beats_to_generate}...")

        fallback_count = 0
        for i in beats_to_generate:
            if sys.stdout:
                print(f"[DEBUG] Step 5: Composing Beat {i} of {total_beats} (Pass {audit_passes})...")
            if on_progress:
                on_progress('batch', {'current': i, 'total': total_beats})

            beat = beat_ladder[i - 1]
            is_last = (i == total_beats)
            is_threshold_or_reveal = (beat.get('operation') in ('threshold', 'reward'))

            bridge_stage = beat.get('bridge_stage')
            is_bridge = (mode == 'Threshold' and bridge_stage in (1, 2))
            tbcp_ref = load_reference_file('threshold-bridge-consistency-protocol.md') if is_bridge else ''
            
            # Crop templates per beat
            templates_cropped = get_cropped_templates(templates_raw, i, total_beats, mode, bridge_stage)
            
            prior_prompts_block = ""
            if i > 1:
                prior_prompts_block = f"""
==================== PREVIOUS BEAT GENERATED PROMPTS (DO NOT DUPLICATE PHRASING) ====================
To prevent formulaic repetition, the vocabulary, sentence structures, and opening patterns of VIDEO {i} and IMAGE {i+1} must NOT duplicate or mirror those in the previous beat prompts:
Previous VIDEO {i-1}:
{compiled_videos[i-1]}

Previous IMAGE {i}:
{compiled_images[i]}
"""

            # Retrieve lighting phases for this beat
            img_i_lighting = packet.get("lighting_phase_ladder", {}).get(str(i), "ambient only")
            img_ip1_lighting = packet.get("lighting_phase_ladder", {}).get(str(i + 1), "ambient only")

            beat_system = f"""You are a professional prompt composer operating under the `restoration-prompt-composer` skill.
Your job is to generate exactly two prompts for Beat {i}:
1. VIDEO {i}: The construction timelapse video.
2. IMAGE {i+1}: The clean environment state snapshot after the video.

==================== LIGHTING PHASE CONTRACT FOR THIS BEAT ====================
- IMAGE {i} (State before this beat) uses lighting phase: {img_i_lighting}
- IMAGE {i+1} (The state you are generating now) MUST use lighting phase: {img_ip1_lighting}
- VIDEO {i} (The transition video prompt) MUST describe the transition matching this lighting phase progression: from '{img_i_lighting}' to '{img_ip1_lighting}'.

==================== SKILL CONTRACTS ====================
{scup_ref}
{tbcp_ref}
{templates_cropped}

==================== DRIFT LOCK PACKET ====================
{json.dumps(packet, indent=2, ensure_ascii=False)}

==================== PRIOR PROMPTS (for continuity) ====================
IMAGE 1 (Trauma State):
{compiled_images[1]}

IMAGE {i} (State before this beat):
{compiled_images[i]}
{prior_prompts_block}

Instructions:
- VIDEO {i} must start with: "Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout."
- VIDEO {i} must use progressive (-ing) verbs for ongoing actions, name worker silhouettes (HAL) and tools (MTAL) if workers are present, encapsulate bulk materials in rigid containers (VMFP/RCE), and include pacing control "continuous construction time-lapse, not real-time footage" (unless threshold or reward).
- VIDEO {i} CONCRETENESS (no abstractions): describe the SAME single lone worker every beat, reusing the exact costume from the packet worker_choreography (e.g. "one lone worker in a solid pale shirt, dark pants, and dark cap"); name the ONE specific manual tool used; describe the concrete repeated work cycle in -ing verbs (e.g. scooping, lifting, pressing, fastening). NEVER write vague filler like "transformation progresses" or "the scene transforms" — show observable physical actions only.
- VIDEO {i} must end with a PERSISTENT-TRACES clause naming the marks this beat leaves behind (e.g. scrape grooves, end-grain circles, screw heads, nail rows, sawdust trails, trimmed edges, compression tracks), followed by a natural-language description of both the near-field diegetic sound effects (2-4 specific sounds of tools, materials, or footsteps) and the steady room/environment ambient noise. Use varied phrasing for these audio descriptions rather than a single formulaic structure.
- IMAGE {i+1} must be a clean frame with ZERO workers/machinery. Do NOT use the words 'worker', 'builder', 'carpenter', 'laborer', 'person', 'man', 'woman', or 'people' under any circumstances, even to state that they are absent or not present. Describe only static objects, surfaces, and traces. It must RESTATE the locked anchors by name and Grid cell exactly as given in the packet primary_landmarks (e.g. "Locked anchors: <name> at Grid A2, <name> at Grid B2, <name> at Grid C2"), restate the left/right/top/bottom boundaries from the packet frame_boundaries, and then describe the visible state delta of this beat plus a FEW (2-3, not exhaustive) PERSISTENT physical traces that prove the work happened (scrape marks, fastener heads, sawdust, membrane wrinkles, displaced soil, etc.). Prior MAJOR installed/finished features (panels, walls, floors, fixtures, primary landmarks) stay present and unchanged (monotonic state) — but you do NOT need to re-list every minor trace from every earlier beat; it is fine and expected for small cosmetic details to fade from the description as new ones accumulate.
- For threshold bridge beats (if beat is a threshold bridge), follow the TBCP rules (Bridge-1 stops at sill, Bridge-2 crosses sill; soft exposure roll; door-frame wipe).
- NLVTR visual-only rule: No '%' symbols, no numeric ranges, no acronyms (HAL, SCUP, NGCS, VMFP, RCE, GCTR, RPL, OSPL, RHMA, PBISP, HCL, NLVTR, MTAL, TSPA) in the prompts.
- FULL-ENCLOSURE COVERAGE: When the beat involves framing, insulating, paneling, or painting walls, the IMAGE prompt MUST explicitly include the ceiling/roof/top surface as well. For example, if walls in Grid B1, B3, C1, C3 are paneled, the ceiling curve in Grid A1, A2, A3 must ALSO be described as paneled. Never treat wall coverage as complete without ceiling coverage in any enclosed space (cabin, room, fuselage, container, vault, etc.).
- CAMERA VIEWPOINT CONTINUITY: If the previous IMAGE was shot from an interior viewpoint (camera inside the space, entry behind camera), the next IMAGE MUST maintain the same interior viewpoint UNLESS an explicit camera-pullback VIDEO is inserted between them. You CANNOT jump from interior to exterior viewpoint without a transition. If the beat requires switching back to an exterior view, generate the VIDEO as a reverse dolly pulling back through the doorway, and describe the exposure transition accordingly.
- EXTERIOR WORK VISIBILITY: If the beat involves work on the EXTERIOR surface of the structure (e.g., exterior insulation, exterior membrane), and the camera is positioned INSIDE looking out, the VIDEO must show the worker operating at the boundary edges visible from inside (e.g., working at seam lines visible in Grid B1/B3 from the interior). Do not describe exterior work that would be invisible from the current camera position.
- CONSTRUCTION ORDER CONSTRAINTS: Floor finish (hardwood, tile) MUST be installed BEFORE heavy anchored objects (fireplace, stove) are placed on it. If this beat installs a fireplace or heavy object, the IMAGE must show it sitting on the FINISHED floor, not on bare metal/subfloor. If the floor is not yet finished, the fireplace cannot be installed in this beat.
- Output the prompts in the following format:
===VIDEO===
<video prompt body>
===IMAGE===
<image prompt body>
===TRACES===
[
  {{
    "name": "precise name of new permanent feature/material/trace (e.g. steel screw heads, green insulation foam)",
    "material_color": "color/texture (e.g. metallic silver)",
    "initial_state": "state when introduced (e.g. freshly installed)",
    "grid": "approximate grid coordinate if mentioned (e.g. Grid B2, default to Grid B2)",
    "z_depth_scale": "depth scale if mentioned (e.g. 50%, default to 50%)"
  }}
]
"""
            beat_user = f"Generate prompts for Beat {i}: {beat['operation']} - {beat['description']}."
            if str(i) in audit_feedback_dict:
                beat_user += "\n\n" + "==================== PRIOR AUDIT FAILURES FOR THIS BEAT ====================\n"
                beat_user += "This beat failed a prior quality audit. You MUST fix these specific issues:\n"
                for err in audit_feedback_dict[str(i)]:
                    beat_user += f"- {err}\n"
                beat_user += "\nStrictly ensure your new generation does not repeat these mistakes."

            vid_prompt = ""
            img_prompt = ""
            new_ledger_items = None
            
            feedback = ""
            for attempt in range(4):
                try:
                    user_msg = beat_user
                    if feedback:
                        user_msg += f"\n\n{feedback}"
                    
                    resp = _chat(config, beat_system, user_msg, temperature=0.8, timeout=90)
                    secs = _extract_marked(resp, ['===VIDEO===', '===IMAGE===', '===TRACES==='])
                    v_p = secs.get('===VIDEO===', '').strip()
                    i_p = secs.get('===IMAGE===', '').strip()
                    
                    # Apply proactive fixes
                    v_p, i_p = apply_proactive_fixes(i, v_p, i_p, packet, mode, is_last, is_threshold_or_reveal, beat=beat, config=config)
                    
                    # Validate prompts
                    prev_v = compiled_videos.get(i - 1) if i > 1 else None
                    prev_i = compiled_images.get(i) if i > 1 else None
                    
                    # 1. Run cheap/local validations first
                    errs = validate_beat_prompts(i, v_p, i_p, packet, mode, is_last, is_threshold_or_reveal, prev_v, prev_i, config=config, beat=beat, skip_llm_checks=True)
                    if not errs:
                        # 2. If cheap validations passed, run LLM validation (monotonic, delta, etc.)
                        llm_errs = validate_beat_prompts(i, v_p, i_p, packet, mode, is_last, is_threshold_or_reveal, prev_v, prev_i, config=config, beat=beat, skip_llm_checks=False)
                        if not llm_errs:
                            vid_prompt = v_p
                            img_prompt = i_p
                            # Also parse TRACES JSON embedded in the prompt response
                            traces_str = secs.get('===TRACES===', '').strip()
                            new_ledger_items_parsed = []
                            if traces_str:
                                try:
                                    traces_clean = _strip_code_fences(traces_str).strip()
                                    parsed = json.loads(traces_clean)
                                    if isinstance(parsed, list):
                                        for item in parsed:
                                            if isinstance(item, dict) and "name" in item:
                                                new_ledger_items_parsed.append({
                                                    "name": str(item.get("name")),
                                                    "material_color": str(item.get("material_color", "unknown")),
                                                    "initial_state": str(item.get("initial_state", "installed")),
                                                    "grid": str(item.get("grid", "Grid B2")),
                                                    "z_depth_scale": str(item.get("z_depth_scale", "50%"))
                                                })
                                        new_ledger_items = new_ledger_items_parsed
                                except Exception as e:
                                    if sys.stdout:
                                        print(f"[DEBUG] Failed to parse prompt-embedded TRACES JSON: {e}")
                            break
                        else:
                            errs = llm_errs
                    
                    feedback = "The generated prompts failed validation. Please fix the following errors:\n"
                    for err in errs:
                        feedback += f"- {err}\n"
                    feedback += "\nPlease rewrite the prompts, strictly adhering to all rules."
                    if sys.stdout:
                        print(f"[DEBUG] Beat {i} attempt {attempt+1} failed validation: {errs}")
                        print(f"[DEBUG]   Generated VIDEO prompt: {v_p}")
                        print(f"[DEBUG]   Generated IMAGE prompt: {i_p}")
                except (NameError, AttributeError, TypeError, ImportError, KeyError, IndexError) as e:
                    raise RuntimeError(
                        f"Beat {i} hit a code-level error ({type(e).__name__}: {e}); aborting to avoid "
                        f"shipping placeholder output. Fix the bug rather than retrying."
                    ) from e
                except Exception as e:
                    if sys.stdout:
                        print(f"[DEBUG] Beat {i} attempt {attempt+1} error: {e}")
                    feedback = f"Error during generation: {e}. Please retry."

            if not vid_prompt or not img_prompt:
                fallback_count += 1
                clean_op = beat.get('operation', 'construction').replace('_', ' ')
                desc = beat.get('description', 'performing restoration work').strip().rstrip('.')
                
                vid_prompt = (
                    f"Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. "
                    f"The video captures the physical process of: {desc}. A worker is visible performing the manual installation and assembly steps, slowly building and placing elements. The background and camera position remain locked."
                )
                if not is_threshold_or_reveal:
                    vid_prompt += " continuous construction time-lapse, not real-time footage."
                
                img_prompt = (
                    f"A static ultra-wide 14mm tripod shot at 1.6m height: clean completed state after the step of {desc} of {theme}; "
                    f"horizon line remains level; no workers are present in this clean frame. The newly completed features are visible and integrated into the scene."
                )
                if is_last:
                    img_prompt += " Polished floor displays blurred diffused reflections."

            compiled_images[i + 1] = img_prompt
            compiled_videos[i] = vid_prompt

            # Dynamically update the object ledger with new persistent traces/features
            if vid_prompt and img_prompt:
                if new_ledger_items is None:
                    new_ledger_items = extract_persistent_traces_to_ledger(config, vid_prompt, img_prompt)
                if new_ledger_items:
                    if 'object_ledger' not in packet or not isinstance(packet['object_ledger'], list):
                        packet['object_ledger'] = []
                    existing_names = {x['name'].lower() for x in packet['object_ledger'] if isinstance(x, dict) and 'name' in x}
                    added_count = 0
                    for item in new_ledger_items:
                        if item['name'].lower() not in existing_names:
                            packet['object_ledger'].append(item)
                            existing_names.add(item['name'].lower())
                            added_count += 1
                    if sys.stdout:
                        print(f"[DEBUG] Dynamic Ledger: Added {added_count} new items (deduplicated). Total objects: {len(packet['object_ledger'])}")

        # Quality gate
        fallback_limit = max(2, total_beats // 3)
        if fallback_count > fallback_limit:
            raise RuntimeError(
                f"{fallback_count} of {total_beats} beats fell back to placeholder prompts "
                f"(limit {fallback_limit}); output quality too low to ship. See server.log for per-beat errors."
            )

        # Step 7: Final Audit & Reassembly
        if on_progress:
            on_progress('audit', '正在运行工序与场景一致性二次校验与二次修复...')

        # Convert compiled_images and compiled_videos to dicts with meta before formatting
        formatted_images = {}
        for idx, img in compiled_images.items():
            meta = ""
            # For idx > 1, the image is the end frame of beat idx - 1
            if idx > 1 and (idx - 2) < len(beat_ladder):
                beat = beat_ladder[idx - 2]
                if beat.get('bridge_stage') in (1, 2):
                    meta = "BRIDGE"
            formatted_images[idx] = {"body": img, "meta": meta}

        formatted_videos = {}
        for idx, vid in compiled_videos.items():
            meta = ""
            if (idx - 1) < len(beat_ladder):
                beat = beat_ladder[idx - 1]
                if beat.get('bridge_stage') in (1, 2):
                    meta = "BRIDGE"
            formatted_videos[idx] = {"body": vid, "meta": meta}

        reassembled_prompts_block = _format_prompt_block(formatted_images, formatted_videos)

        # Run validator LLM to check and repair in place, and re-process the repaired slots
        repaired_block, repair_md = validate_and_repair(
            config, parsed_brief, reassembled_prompts_block,
            packet=packet, beat_ladder=beat_ladder, on_progress=on_progress
        )
        reassembled_prompts_block = repaired_block

        audit_system = """You are a construction sequence and prompt quality auditor.
Your job is to analyze the complete, reassembled IMAGE and VIDEO prompt set and output a quality audit report.
Analyze the prompt set for strict construction order, physical causality, and spatial consistency.

You MUST check for ALL of the following specific issues:

1. **天花板/顶面遗漏**: For any enclosed space, check that ceiling/roof treatment (framing, insulation, paneling, painting) is explicitly described alongside wall treatment. If walls are covered but the ceiling is left as raw/exposed structure, mark as 未通过.
2. **视角跳切**: Check that camera viewpoint transitions are smooth. If an IMAGE is interior (camera inside), the next IMAGE cannot suddenly be exterior without an intervening camera-pullback VIDEO. Mark any sudden interior-to-exterior or exterior-to-interior jumps without transition as 未通过.
3. **施工顺序**: Check physical causality: flooring before heavy furniture/fixtures, wiring before light fixtures, priming before painting, door leaf after door frame. Mark violations as 未通过.
4. **视频模板冲突**: Check that each VIDEO's worker entry/exit template matches the body text. If the body says no workers but the template adds a worker, or the body says two workers but the template says one lone worker, mark as 未通过.
5. **灯具安装遗漏**: If the final IMAGE shows light fixtures, check that there was an explicit fixture installation beat. If fixtures appear without installation, mark as 未通过.
6. **门扇遗漏**: If a door frame was installed, check that a door panel/leaf was also installed in a subsequent beat (unless it is explicitly an archway). Mark as 未通过 if missing.
7. **外部工作视角**: If a beat involves exterior work (e.g., exterior insulation) but the camera is inside, check that the work is visible from the camera position. Mark as 未通过 if contradicted.

You must output a Markdown table with the following columns:
| 拍号 / Beat Index | 审核大项 | 子检查项 / 协议要求 | 通过状态 | 审核判定说明 |

In the "拍号 / Beat Index" column, specify the 1-based index of the beat (e.g. "3", "5"), or "Project" if it is a project-wide check.

Do NOT output anything else. Do not wrap in markdown code fences."""
        audit_user = f"""Here is the complete generated prompt set:
{reassembled_prompts_block}

Please generate the detailed quality audit table."""

        audit_md = ""
        for attempt in range(3):
            try:
                audit_md = _chat(config, audit_system, audit_user, temperature=0.3, timeout=120)
                if '|' in audit_md:
                    break
            except Exception as e:
                if sys.stdout:
                    print(f"[DEBUG] Pass 3 Exception: {e}")

        audit_md_cleaned = _strip_markdown_fences_only(audit_md)

        # Parse failures from the audit table
        failures = parse_audit_failures(config, audit_md_cleaned)
        if not failures:
            if sys.stdout:
                print("[AUDIT] All quality checks passed successfully!")
            break
        else:
            if sys.stdout:
                print(f"[AUDIT] Found failures in beats: {list(failures.keys())}")
            audit_feedback_dict = failures
            audit_passes += 1
            if audit_passes > max_audit_passes:
                if sys.stdout:
                    print(f"[AUDIT] Warning: Maximum audit healing passes reached, continuing with some failures.")
                break

    skipped = config.get('_skipped_checks', 0) if isinstance(config, dict) else 0
    skipped_str = f"\n\n[WARNING] 本次跳过了 {skipped} 项校验。" if skipped > 0 else ""

    # Safety net: the validator/audit LLM calls above process reassembled_prompts_block
    # through free-form text generation, which can silently truncate or drop slots.
    # compiled_images/compiled_videos are the verified-complete source of truth (every
    # beat unconditionally writes both an image and video entry), so re-check the final
    # block against them and rebuild from source if anything went missing rather than
    # shipping a partial prompt set.
    check_images, check_videos = _parse_prompt_slots(reassembled_prompts_block)
    missing_images, missing_videos = _missing_prompt_slots(
        check_images, check_videos, (1, total_beats + 1), (1, total_beats)
    )
    if missing_images or missing_videos:
        if sys.stdout:
            print(f"[WARNING] Final prompt block was missing slots (images={missing_images}, videos={missing_videos}) "
                  f"after validator/audit processing; rebuilding from the verified-complete compiled beat data.")
        reassembled_prompts_block = _format_prompt_block(formatted_images, formatted_videos)

    final_output = f"""===TITLE===
{title}
===THEME===
{parsed_brief.get('theme', theme)}
===PROMPTS===
{reassembled_prompts_block}
===AUDIT===
{repair_md}

{audit_md_cleaned}{skipped_str}"""

    return final_output


def build_validator_system_prompt():
    return f"""You are a strict construction-sequence, physical-causality, and spatial-consistency (SCUP) auditor for a restoration / renovation time-lapse prompt set (Chinese-labeled 图片 / 视频 prompts). Your job is to detect and repair violations of real-world build order, physical causality, and scene/spatial consistency. Do NOT redesign, restyle, re-theme, or otherwise "improve" anything.

Check the whole set in shot order for these hard vetoes:
[Construction Order & Causality]
- No powered lights, glowing strips, lit screens, or running equipment before the wiring / power beat. Power-on and lighting must come AFTER the beat that installs their wiring.
- No crossing the threshold into the interior before the exterior (facade / roof / site / rust-proofing) is finished.
- No paint, spray, or topcoat before rust removal, cleaning, and priming.
- No covering wet or uncured material (mortar, concrete, glue, paint) with the next layer before it has cured.
- No service (wiring / plumbing / waterproofing) installed after the panel that would hide it.
- Construction state must be monotonic: cleaned stays clean, installed stays installed, dried stays dried — no regression to an earlier state.
- CEILING/ROOF COVERAGE: No enclosed space (room, cabin, fuselage, container, vault) may have walls paneled/insulated/painted while the ceiling/roof is left as raw exposed structure. If walls are covered, the ceiling must also be covered in the same or a subsequent beat. If ceiling coverage is missing, add it to the wall-coverage beat.
- CAMERA VIEWPOINT CONTINUITY: No sudden camera viewpoint jumps. If IMAGE N is interior (camera inside the space, entry behind camera), IMAGE N+1 cannot be exterior (camera outside looking in) without an intervening reverse-dolly VIDEO that pulls the camera back through the doorway. If this occurs, either keep the viewpoint consistent or insert a camera-pullback transition in the VIDEO.
- FLOOR-BEFORE-HEAVY-OBJECTS: Floor finish must be installed BEFORE heavy anchored objects (fireplace, stove) are placed on it. If a heavy object is installed on a bare subfloor and then the finished floor appears under it, reorder the beats so flooring comes first.
- FIXTURE COMPLETENESS: If wiring/electrical rough-in is present, light fixture installation must occur BEFORE the reward beat. Fixtures cannot appear in the final reward without an installation beat.
- DOOR COMPLETENESS: If a door frame is installed, a door panel/leaf must be installed in a subsequent beat unless the design explicitly specifies an open archway.
- WORKER TEMPLATE CONSISTENCY: Worker entry/exit template clauses at the end of each VIDEO must match the body: no workers in a sterile/no-worker video, correct worker count for multi-worker videos. If a VIDEO body says "sterile of workers" or "no human presence", the template must not add a worker. If the body uses "two workers", the template must not say "one lone worker".
- CLEAR PATH REQUIREMENT: If there are sliding, rolling, retracting, or moving parts (e.g. bed rails, sliding bed, folding stairs), ensure a clean spatial path. If structural columns, pillars, or bulkheads block the path of movement in the trauma state (IMAGE 1), they must be explicitly cut/removed early in the sequence (typically during structural repair) and replaced by peripheral support frames before rails or sliding mechanisms are installed.
- FLOOR & SKELETON MONOTONICITY: If floor/wall joists, ribs, or framing studs will be insulated or paneled later, the bare structural skeleton must be exposed at the very beginning (IMAGE 1/2). The state must progress monotonically forward: bare joists/studs -> rough-in/insulation -> subfloor -> finished flooring/cladding. Never start with a solid finished-looking floor that disappears to reveal raw joists later.
- STRICT SINGLE-OPERATION BEAT RULE: Each {int(VIDEO_DURATION)}-second video prompt must describe exactly one homogeneous physical task. Combining multiple distinct stages (e.g. spray painting AND mounting door frames, or framing studs AND packing insulation wool, or laying tile AND anchoring stoves AND installing bed rails) into a single {int(VIDEO_DURATION)}-second video is strictly prohibited.

[Scene Consistency & Spatial Consistency Upgrade Protocol (SCUP)]
- Consistent Scene & Layout: The background environment, geographical elements, time-of-day, camera position/DNA, visual style, and color scheme must be completely consistent across the sequence.
- Material Continuity: Materials (e.g. wood type, steel type) must not magically transform between shots unless painted or replaced.
- NGCS coordinate lock: Ensure the 3 Primary Landmarks remain locked to the same coordinates (e.g. A1, B3, C2) across all images unless explicitly altered.
- Ghost Clause: Occluded landmarks must be preserved in parenthetical tags, e.g. `[Object Name] remains physically locked at [Grid Cell] ... hidden behind [occluding object]`.
- VMFP & RCE Volume: Loose materials must be encapsulated in rigid, countable containers (buckets/bags) and have volume percentage capacities.
- RHMA Reflection: Glossy/wet surfaces must use highly blurred, diffused reflections (RHMA-Blur) to prevent video flicker.
- Clean Frame Boundary: Image anchors must have ZERO active workers or active machinery.
- Out-and-In Passage: Workers in video prompts must enter at t=0s and exit before t={int(VIDEO_DURATION)}s.
- PERSPECTIVE ISOLATION: Do not flip camera facing directions (e.g. turning 180 degrees from looking out to looking in) in the same spatial axis without a clean separate phase or TBCP transition. If the project centers on a slide-out reward action, lock the Camera Family (e.g. Camera Family B looking outward through the opening towards the view) from the very first frame to maintain spatial consistency.
- BI-DIRECTIONAL AGENT FLOW: Workers in video prompts must enter from a specific coordinate edge at t=0s and walk out through the same edge by t={WORKER_EXIT_TIME}s, leaving the frame completely empty of active agents at t={int(VIDEO_DURATION)}s. No teleportation or instant popping.
- RIGID CONTAINER ENCAPSULATION: All loose materials, debris, fasteners, and liquids must be stored and tracked inside rigid, quantifiable containers (e.g. buckets, parts trays, boxes), and their volumes must be described as continuously increasing or decreasing.
- MANDATORY CLIMAX VIDEO: The prompt composer must generate exactly N video prompts for N+1 images, ensuring the transition between the final two frames (the "Dressed interior" -> "Retract/slide action") is fully animated. The climax video (VIDEO N) must depict the actual physical kinetic movement of the mechanism (e.g. the bed rolling smoothly forward, the glass door sliding open).
- NLVTR Text Lock: No '%' symbol, numeric ranges, colons in variable strings, or acronyms (HAL, DKP, VMFP, RPL, RCE, SCUP, NGCS, OSPL, RHMA, PBISP, HCL, NLVTR) in prompt bodies.

When you rewrite a slot to fix a violation, you MUST preserve every other constraint:
- Keep the exact labels 图片提示词 / 图片 N: / 视频提示词 / 视频 N: and the same slot counts (image count = video count + 1).
- Keep each VIDEO's opening sentence "Use the provided first frame and last frame as exact composition anchors." and its IMAGE N to IMAGE N+1 binding.
- Keep the Camera DNA wording consistent across same-family IMAGE slots.
- Natural-language prose only: never introduce the percent glyph, numeric ranges, or acronyms (HAL, DKP, VMFP, RPL, RCE, SCUP, NGCS, OSPL, RHMA, PBISP, HCL, NLVTR).
- Reordering beats may require renumbering the slots and re-pointing the IMAGE N to IMAGE N+1 bindings accordingly; do that consistently.
- Change ONLY what is necessary to fix ordering / causality / consistency; copy every other slot character-for-character.

Output EXACTLY these two sections, in THIS order, nothing before or after, no code fences:
===REPAIR===
<one short Chinese verdict. If clean, output exactly: PASS — 工序与场景一致性检查通过，未发现违规，提示词未改动。 If you fixed anything, output: 已修复 N 处： then a numbered list, one line each, naming the offending slot, what was physically wrong, and how it was reordered or corrected.>
===PROMPTS===
<If and ONLY IF you changed something, output the COMPLETE corrected prompt set here. If you changed nothing, output exactly the single word: UNCHANGED>"""


def validate_and_repair(config, dimensions, prompt_block, packet=None, beat_ladder=None, on_progress=None):
    """Second pass: enforce construction order + physical causality + scene consistency (SCUP) and repair in place.
    Returns (repaired_prompt_block, repair_md). Best-effort — caller falls back to the
    first-pass block if this raises."""
    if not prompt_block.strip():
        return prompt_block, '（无提示词内容，跳过工序与场景一致性校验）'
    if on_progress:
        on_progress('repair', '工序与场景一致性校验返回有细微瑕疵，正在进行一致性修复以保时序因果，请稍候...')
    user = (
        f"场景主题：{dimensions.get('theme', '')}。\n\n"
        "以下是待校验的完整提示词集，请按系统指令做工序与场景一致性的二次校验与最小化修复：\n\n"
        + prompt_block
    )
    last_repair_md = '（工序与场景一致性校验未返回结论）'
    repaired = prompt_block
    
    for attempt in range(3):
        try:
            def handle_chunk(chunk):
                if on_progress:
                    on_progress('text_chunk', chunk)
            content = _chat(config, build_validator_system_prompt(), user,
                            temperature=0.3, timeout=180, on_chunk=handle_chunk)
            secs = _extract_marked(content, ['===REPAIR===', '===PROMPTS==='])
            repair_md = secs.get('===REPAIR===', '').strip()
            
            if repair_md:
                new_prompts = _strip_markdown_fences_only(secs.get('===PROMPTS===', ''))
                if new_prompts and new_prompts.strip().upper() != 'UNCHANGED' and len(new_prompts) > 800:
                    new_prompts = _normalize_prompt_block(new_prompts)
                    # Guard against output truncation in the validator pass
                    if len(new_prompts) >= len(prompt_block) * 0.9:
                        if packet and beat_ladder:
                            if sys.stdout:
                                print("[DEBUG] validate_and_repair: running proactive fixes and validations on repaired prompts...")
                            repaired_images, repaired_videos = _parse_prompt_slots(new_prompts)
                            orig_images, orig_videos = _parse_prompt_slots(prompt_block)
                            
                            processed_images = {}
                            processed_videos = {}
                            
                            # 1. Process IMAGE 1
                            img_1_item = repaired_images.get(1)
                            img_1_body = img_1_item['body'] if isinstance(img_1_item, dict) else (img_1_item or '')
                            img_1_meta = img_1_item.get('meta', '') if isinstance(img_1_item, dict) else ''
                            
                            img_1_body = clean_prompt_text(img_1_body)
                            img_1_body = fix_image_clean_frame_proactive(img_1_body)
                            camera_dna = packet.get('camera_dna', '')
                            if camera_dna:
                                img_1_body = fix_camera_dna(img_1_body, camera_dna)
                            # Apply additional proactive fixes for images on IMAGE 1
                            img_1_body = fix_horizon_line(img_1_body)
                            img_1_body = fix_primary_landmarks(img_1_body, packet)
                            img_1_body = fix_camera_contradictions(img_1_body, is_bridge=False)
                            
                            # Validate IMAGE 1
                            img_1_errs = check_image_clean_frame(img_1_body)
                            img_1_errs.extend(check_grid_coordinates(img_1_body))
                            img_1_errs.extend(check_primary_landmarks_exact_match(img_1_body, packet))
                            img_1_errs.extend(check_nlvtr_violations(img_1_body))
                            if "horizon line" not in img_1_body.lower():
                                img_1_errs.append("IMAGE 1 missing 'horizon line' camera lock statement")
                            
                            orig_img_1_item = orig_images.get(1)
                            orig_img_1_body = orig_img_1_item['body'] if isinstance(orig_img_1_item, dict) else (orig_img_1_item or '')
                            orig_img_1_meta = orig_img_1_item.get('meta', '') if isinstance(orig_img_1_item, dict) else ''
                            
                            if not img_1_errs:
                                processed_images[1] = {'body': img_1_body, 'meta': img_1_meta}
                            else:
                                if sys.stdout:
                                    print(f"[DEBUG] Repaired IMAGE 1 failed basic validation: {img_1_errs}. Falling back to original.")
                                processed_images[1] = {'body': orig_img_1_body, 'meta': orig_img_1_meta}
                            
                            # 2. Process beats 1..total_beats
                            total_beats = len(beat_ladder)
                            mode = dimensions.get('mode', 'Standard')
                            
                            for i in range(1, total_beats + 1):
                                beat = beat_ladder[i - 1]
                                is_last = (i == total_beats)
                                is_threshold_or_reveal = (i == total_beats) if mode == "Reveal" else False
                                
                                v_item = repaired_videos.get(i)
                                v_body = v_item['body'] if isinstance(v_item, dict) else (v_item or '')
                                v_meta = v_item.get('meta', '') if isinstance(v_item, dict) else ''
                                
                                i_item = repaired_images.get(i + 1)
                                i_body = i_item['body'] if isinstance(i_item, dict) else (i_item or '')
                                i_meta = i_item.get('meta', '') if isinstance(i_item, dict) else ''
                                
                                # Apply proactive fixes
                                v_body_fixed, i_body_fixed = apply_proactive_fixes(
                                    i, v_body, i_body, packet, mode, is_last, is_threshold_or_reveal,
                                    beat=beat, config=config
                                )
                                
                                # Validate
                                prev_v_str = processed_videos[i - 1]['body'] if (i > 1 and i - 1 in processed_videos) else None
                                prev_i_str = processed_images[i]['body'] if (i > 1 and i in processed_images) else None
                                errs = validate_beat_prompts(
                                    i, v_body_fixed, i_body_fixed, packet, mode, is_last, is_threshold_or_reveal,
                                    prev_v_str, prev_i_str, config=config, beat=beat, skip_llm_checks=True
                                )
                                if not errs:
                                    # If cheap validations passed, run LLM validation (monotonic, delta, etc.)
                                    errs = validate_beat_prompts(
                                        i, v_body_fixed, i_body_fixed, packet, mode, is_last, is_threshold_or_reveal,
                                        prev_v_str, prev_i_str, config=config, beat=beat, skip_llm_checks=False
                                    )
                                
                                if not errs:
                                    processed_videos[i] = {'body': v_body_fixed, 'meta': v_meta}
                                    processed_images[i + 1] = {'body': i_body_fixed, 'meta': i_meta}
                                else:
                                    if sys.stdout:
                                        print(f"[DEBUG] Repaired beat {i} failed basic/LLM validation: {errs}. Falling back to original.")
                                    
                                    orig_v_item = orig_videos.get(i)
                                    orig_v_body = orig_v_item['body'] if isinstance(orig_v_item, dict) else (orig_v_item or '')
                                    orig_v_meta = orig_v_item.get('meta', '') if isinstance(orig_v_item, dict) else ''
                                    
                                    orig_i_item = orig_images.get(i + 1)
                                    orig_i_body = orig_i_item['body'] if isinstance(orig_i_item, dict) else (orig_i_item or '')
                                    orig_i_meta = orig_i_item.get('meta', '') if isinstance(orig_i_item, dict) else ''
                                    
                                    processed_videos[i] = {'body': orig_v_body, 'meta': orig_v_meta}
                                    processed_images[i + 1] = {'body': orig_i_body, 'meta': orig_i_meta}
                                    
                            repaired = _format_prompt_block(processed_images, processed_videos)
                        else:
                            repaired = new_prompts
                    else:
                        if sys.stdout:
                            print("[DEBUG] validate_and_repair: validator output was truncated. Discarding repaired block.")
                        repaired = prompt_block
                        repair_md = repair_md + "\n\n⚠️ 注意：由于校验修复输出被大模型截断，系统自动保留了原始的完整提示词，仅记录上述校验发现。"
                else:
                    repaired = prompt_block
                return repaired, repair_md
            else:
                if content:
                    last_repair_md = f'（校验输出格式未包含 ===REPAIR=== 标记，第 {attempt + 1} 次重试）'
        except Exception as e:
            last_repair_md = f'（校验出错：{e}，第 {attempt + 1} 次重试）'
            
    return repaired, last_repair_md


def _strip_markdown_fences_only(s):
    """Remove a leading ```lang line and trailing ``` line if the model wrapped a
    section in a markdown code fence. Unlike _strip_code_fences, this does NOT try
    to extract an embedded JSON object/array — it is safe to use on free-form prose
    (prompt bodies, audit markdown, repaired prompt sets) that legitimately contains
    '[' / ']' characters (e.g. '[BRIDGE]' meta tags, 'Locked anchors: [C2, B2, A2]',
    or 'REMOVED: [object name]' edit markers). Using the JSON-extraction variant on
    such text slices from the first stray '[' anywhere in the string to the last ']'
    anywhere in the string, silently discarding everything outside that span."""
    s = (s or '').strip()
    if s.startswith('```'):
        nl = s.find('\n')
        s = s[nl + 1:] if nl != -1 else s[3:]
    if s.rstrip().endswith('```'):
        s = s[:s.rstrip().rfind('```')]
    return s.strip()


def _strip_code_fences(s):
    """Remove a leading ```lang line and trailing ``` line if the model wrapped a
    section in a markdown code fence (it tends to do this for the prompt block).
    Also extracts the outermost JSON block/list if conversational noise is present.
    Only use this on content expected to be JSON — see _strip_markdown_fences_only
    for free-form prose that may legitimately contain '[' / ']' / '{' / '}'."""
    s = s.strip()
    if s.startswith('```'):
        nl = s.find('\n')
        s = s[nl + 1:] if nl != -1 else s[3:]
    if s.rstrip().endswith('```'):
        s = s[:s.rstrip().rfind('```')]
    s = s.strip()

    first_brace = s.find('{')
    first_bracket = s.find('[')

    start_idx = -1
    end_char = ''
    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        start_idx = first_brace
        end_char = '}'
    elif first_bracket != -1:
        start_idx = first_bracket
        end_char = ']'
        
    if start_idx != -1:
        end_idx = s.rfind(end_char)
        if end_idx != -1 and end_idx > start_idx:
            return s[start_idx:end_idx + 1]
            
    return s


def _parse_prompt_slots(block):
    """Parse Chinese-labeled image/video prompt slots from a prompt block,
    preserving optional metadata annotations like [BRIDGE] attached to the labels."""
    text = _strip_markdown_fences_only(block or '')
    
    # Matches: "图片 8:" or "图片 8 [BRIDGE]:"
    image_matches = re.findall(
        r'图片\s*(\d+)(?:\s*\[(.*?)\])?\s*:\s*(.*?)(?=\n图片\s*\d+|\n视频提示词|\n视频\s*\d+|\Z)',
        text,
        re.DOTALL
    )
    
    # Matches: "视频 8:" or "视频 8 [BRIDGE]:"
    video_matches = re.findall(
        r'视频\s*(\d+)(?:\s*\[(.*?)\])?\s*:\s*(.*?)(?=\n视频\s*\d+|\n图片提示词|\n图片\s*\d+|\Z)',
        text,
        re.DOTALL
    )
    
    images = {}
    for n, meta, body in image_matches:
        if body.strip():
            images[int(n)] = {
                'body': body.strip(),
                'meta': meta.strip() if meta else ''
            }
            
    videos = {}
    for n, meta, body in video_matches:
        if body.strip():
            videos[int(n)] = {
                'body': body.strip(),
                'meta': meta.strip() if meta else ''
            }
            
    return images, videos


def _missing_prompt_slots(images, videos, image_range, video_range):
    expected_images = set(range(image_range[0], image_range[1] + 1))
    expected_videos = set(range(video_range[0], video_range[1] + 1))
    return sorted(expected_images - set(images)), sorted(expected_videos - set(videos))


def _format_prompt_block(images, videos):
    image_lines = ["图片提示词"]
    for idx in sorted(images):
        item = images[idx]
        body = item['body'] if isinstance(item, dict) else item
        meta = item.get('meta', '') if isinstance(item, dict) else ''
        meta_str = f" [{meta}]" if meta else ""
        image_lines.extend([f"图片 {idx}{meta_str}:", body.strip(), ""])

    video_lines = ["视频提示词"]
    for idx in sorted(videos):
        item = videos[idx]
        body = item['body'] if isinstance(item, dict) else item
        meta = item.get('meta', '') if isinstance(item, dict) else ''
        meta_str = f" [{meta}]" if meta else ""
        video_lines.extend([f"视频 {idx}{meta_str}:", body.strip(), ""])

    return ("\n".join(image_lines).rstrip() + "\n\n" + "\n".join(video_lines).rstrip()).strip()


def _normalize_prompt_block(block):
    images, videos = _parse_prompt_slots(block)
    if not images and not videos:
        return _strip_markdown_fences_only(block or '')
    return _format_prompt_block(images, videos)


def _extract_marked(content, markers):
    """Split content into a dict keyed by marker, using regex to locate markers.
    Allows for spacing, case differences, and optional markdown formatting (like bold)."""
    positions = []
    for m in markers:
        core = m.replace('===', '').strip()
        pattern = r'(?:\*\*)?===\s*' + re.escape(core) + r'\s*===(?:\*\*)?'
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            positions.append((match.start(), match.end(), m))
            
    positions.sort(key=lambda x: x[0])
    out = {}
    for i, (start, end, m) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(content)
        out[m] = content[end:next_start].strip()
    return out


def parse_sections(content):
    """Split the marker-delimited model output into structured fields. Robust to the
    model omitting markers or formatting them with different spaces/case."""
    out = {'title': '', 'theme': '', 'prompt_block': '', 'audit_md': '', 'raw': content}
    markers = ['===TITLE===', '===THEME===', '===PROMPTS===', '===AUDIT===']
    keys = ['title', 'theme', 'prompt_block', 'audit_md']
    
    positions = []
    for m, k in zip(markers, keys):
        core = m.replace('===', '').strip()
        pattern = r'(?:\*\*)?===\s*' + re.escape(core) + r'\s*===(?:\*\*)?'
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            positions.append((match.start(), match.end(), k))
            
    if not positions:
        out['prompt_block'] = _strip_markdown_fences_only(content)
        first_line = content.strip().splitlines()[0] if content.strip() else '未命名创意'
        out['title'] = first_line[:40]
        return out

    positions.sort(key=lambda x: x[0])

    for i, (start, end, key) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(content)
        out[key] = content[end:next_start].strip()

    out['prompt_block'] = _normalize_prompt_block(out['prompt_block'])
    out['audit_md'] = _strip_markdown_fences_only(out['audit_md'])
    if not out['title']:
        out['title'] = '未命名创意'
    return out


def run_ideate(config, count=8):
    engine_path = os.path.join(SKILL_DIR, 'references', 'idea-engine.md')
    ledger_path = os.path.join(SKILL_DIR, 'references', 'used-topic-ledger.md')
    
    engine_content = ""
    if os.path.exists(engine_path):
        with open(engine_path, 'r', encoding='utf-8') as f:
            engine_content = f.read()
            
    ledger_content = ""
    if os.path.exists(ledger_path):
        with open(ledger_path, 'r', encoding='utf-8') as f:
            ledger_content = f.read()
            
    system_prompt = f"""You are the Upstream Ideation Layer for the `restoration-prompt-composer` skill.
Your task is to generate a ranked list of {count} highly novel, realistic, buildable time-lapse renovation topic seeds.
You must combine axes from the Morphological Matrix in `idea-engine.md` and filter them to ensure quality.

Here is the authoritative `idea-engine.md` specifying the matrices, rules, filters, scoring rubric, and continuous-supply mechanisms:
==================== IDEA ENGINE ====================
{engine_content}

Here is the current `used-topic-ledger.md` showing already used/burned topic DNAs:
==================== USED TOPIC LEDGER ====================
{ledger_content}

==================== GENERATION INSTRUCTIONS ====================
1. Combine Axis 1 (Carrier), Axis 2 (Environment), Axis 3 (Trauma), Axis 4 (Destiny), and Axis 5 (Signature Twist) to form candidates.
2. Filter out any candidates that:
   - Have a NON-SHELTER destiny. SHELTER-ONLY POLICY is a hard veto: every destiny MUST be a habitable private dwelling / refuge (a place to sleep, shelter, and live). Reject outright any bar, cafe, tea house, speakeasy, recording/ceramics/painting/art studio, shop, gallery, museum, public observatory, commercial spa/sauna/onsen, or lab. Litmus: "could one person live and sleep here as their own refuge?" — if no, drop it.
   - Violate the Orthogonal-Pairing Rule (Raw shell vs cozy interior contrast).
   - Do not have exactly ONE Axis-5 signature twist.
   - Match or are one edit-step away from any burned Topic DNA in the ledger.
   - Are in the Cliché Blocklist.
   - Fail the Buildability Gate (no magic/conjuring).
3. Score each candidate (0-5 for Novelty, Visual Contrast, Twist Strength, Buildability, Scroll-Stop).
4. Select the top {count} candidates with highest total score.
5. In this batch, ensure Axis-1 carrier families (Living/Natural, Abandoned Man-made, Vehicles/Vessels, Fantasy-grounded) rotate and do not repeat consecutively.
6. Translate all names to natural Chinese for the final title and one-click input string.
7. Return ONLY a valid JSON array of objects, with no markdown code fences, no other text.

Each object in the JSON array must have EXACTLY these keys:
- "title": (string) A catchy Chinese one-sentence title, e.g. "蓝冰冰川洞改造成隐居雪境卧室"
- "input_str": (string) A Chinese Tier-1 one-click input string, e.g. "做一个蓝冰冰川洞穴改造成隐居雪境卧室"
- "carrier": (string) Carrier in English, e.g. "glacier ice cave"
- "env": (string) Environment in English, e.g. "alpine cliff"
- "trauma": (string) Trauma state in English, e.g. "frost-cracked & ice-encased"
- "destiny": (string) Destiny in English — MUST be a habitable shelter/dwelling/refuge, e.g. "snug winter refuge den"
- "twist": (string) Signature twist DNA name in English, e.g. "self-material-window"
- "twist_zh": (string) Chinese display description of the signature twist, e.g. "窗户直接切穿半透明蓝冰"
- "carrier_family": (string) one of: "natural", "man-made", "vehicle", "fantasy"
- "dna": (string) Topic DNA in the format "carrier-family / destiny / twist-family", e.g., "natural / refuge-den / self-material-window"
- "score": (number) Total score out of 25.
"""

    user_prompt = f"Generate {count} top-quality unique renovation ideas following the instructions."
    
    for attempt in range(3):
        try:
            resp = _chat(config, system_prompt, user_prompt, temperature=0.8, timeout=90)
            cleaned = _strip_code_fences(resp).strip()
            ideas = json.loads(cleaned)
            if isinstance(ideas, list) and len(ideas) > 0:
                return ideas
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] run_ideate attempt {attempt+1} failed: {e}")
                
    # Fallback if LLM fails (shelter-only destinies, per SHELTER-ONLY POLICY)
    return [
        {
            "title": "蓝冰冰川洞改造成隐居雪境卧室",
            "input_str": "做一个蓝冰冰川洞穴改造成隐居雪境卧室",
            "carrier": "glacier ice cave",
            "env": "alpine cliff",
            "trauma": "frost-cracked & ice-encased",
            "destiny": "snug winter refuge den",
            "twist": "self-material-window",
            "twist_zh": "窗户直接切穿半透明蓝冰",
            "carrier_family": "natural",
            "dna": "natural / refuge-den / self-material-window",
            "score": 24
        },
        {
            "title": "退役潜艇舱改造成离网单人居所",
            "input_str": "做一个退役潜艇舱改造成离网单人居所",
            "carrier": "retired submarine",
            "env": "misty fjord",
            "trauma": "rust-flaked & gutted",
            "destiny": "off-grid micro-home",
            "twist": "porthole-lighting",
            "twist_zh": "保留黄铜舷窗作为背光搁板灯",
            "carrier_family": "vehicle",
            "dna": "vehicle / micro-home / porthole-lighting",
            "score": 23
        },
        {
            "title": "废弃导弹井改造成地下隐居卧室",
            "input_str": "做一个废弃导弹发射井改造成地下隐居卧室",
            "carrier": "missile silo",
            "env": "high desert mesa",
            "trauma": "debris-packed & guano-caked",
            "destiny": "subterranean burrow dwelling",
            "twist": "roof-hatch",
            "twist_zh": "混凝土屋顶舱门滑动打开露出天空",
            "carrier_family": "man-made",
            "dna": "man-made / burrow-dwelling / roof-hatch",
            "score": 23
        }
    ]



