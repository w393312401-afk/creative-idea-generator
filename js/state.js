/* ============================================================
   全局共享状态(主 app,最先加载)
   —— 原先散落在 app.js 顶部,而 app.js 最后加载;早加载的 js/ 模块
   (config/api_client/media_renderer/prompt_pipeline)靠运行时延迟读取才不报错,
   是隐性的加载顺序契约。此处抽出并置于最前加载,使依赖显式、消除该脆弱性。
   注:仅主 app(index.html)加载;console 前端不使用这些状态(console.js 里的
   config 都是对象键/字符串,非全局)。
   本轮搬入 DEFAULT_CONFIG/config/ACCESS_CODE + 运行时可变状态块;大只读常量
   (PRESETS / IMAGE_MODELS_BY_MAIN_MODEL)暂留 app.js,后续可再并入。
   ============================================================ */

// Default Configurations
const DEFAULT_CONFIG = {
    baseUrl: 'http://127.0.0.1:8046/v1',
    // Key intentionally NOT shipped to the browser. In server-managed (external) mode the
    // backend supplies the key from server_config.json. For local self-use, enter your key
    // once in the ⚙️ 配置中心 (it persists in this browser's localStorage).
    apiKey: '',
    model: 'gemini-3.7-flash-high',
    imageModel: 'nano-banana-2',
    // 帧序列生成方式: 'api'（LLM 网关）| 'google_fx'（AdsPower 浏览器 UI 自动化）
    imageBackend: 'api',
    googleFxImageModel: 'Nano Banana 2',
    videoModel: 'Veo 3.1 - Lite [Lower Priority]',
    // 提示词链路（做哪个视频模型的提示词，就读哪个技能包）：
    // 'auto' = 跟随 videoModel 推断（服务端 active_skill_profile 判定，规则表由
    // /api/mode 的 skill_profile_rules 下发）；也可钉死 'base'（Veo 单镜延时）或
    // 'omni'（Gemini Omni 多镜头组接，镜头数随片长）。钉死的用处是把两件事解耦：只想换渲染档位
    // 的人不该被顺手改掉提示词语法，反过来也一样。
    skillProfile: 'auto',
    // 视频时长（仅 Omni Flash 模型面板提供 4s/6s/8s/10s 时长 tab；Veo 系列时长固定，
    // 该项对其无效）。默认 10s：omni 的时间线提示词按秒排镜头切点，10 秒既让主工作镜
    // 有足够长度走完"第一次动作 + 重复循环"，又排得下第二个特写插入；4s/6s 按
    // composers/omni.py 只排一个插入。此项**不再允许留空**——"沿用面板当前时长"是个不可知态，会让
    // 提示词里的切点表与实际生成时长对不上。
    videoDuration: '10',
    // 视频参考模式 = 发起视频前 Flow 面板停在哪个子模式上传参考图：
    // 'VIDEO_FRAMES'（帧/首尾帧，约束运动起止）| 'VIDEO_REFERENCES'（素材，当风格/主体参考）。
    // 与 fx_console.py FX_CONFIG_SPEC 的 videoRefMode 同一项，服务端配置优先。
    videoRefMode: 'VIDEO_FRAMES',
    imageAspectRatio: '9:16',
    imageQuality: '2K',
    // 质量门禁项（frameContinuityMode / qaGateLevel / videoProcessVlmReview / …）
    // 刻意**不**在这里写默认值：唯一真源是 server_common.GATE_SETTINGS，经
    // /api/mode 的 gate_settings 字段下发，配置中心「质量门禁」分区照它渲染
    // （见 js/gate_settings.js）。在这里抄一份就是又开一个会漂移的真相源——
    // 前端默认 balanced、服务端改成 strict，用户看到的和实际跑的就对不上了。
    // 只有用户显式改过的门禁项才会出现在 config 里并随请求带走。
    frameContinuityLocalEdit: 'off',
    strictPromptPipelineV2: true,
    composeBatchSize: 3,
    composeRequestTimeoutSeconds: 120,
    composeBatchRetryCount: 1,
    composeNoProgressTimeoutSeconds: 180,
    composeTaskSoftTimeoutSeconds: 480,
    composeTaskHardTimeoutSeconds: 720,
    allowPlaceholderPrompts: false,
    // 激发参考网址（可选）: 换行/逗号分隔,最多取 5 个;后端抓取正文→aux 模型压成
    // 中文要点注入激发 prompt,与联网搜索趋势通道叠加,6 小时缓存
    ideationTrendUrls: '',
    // 激发联网搜索词（可选）: 留空用默认「爆款延时改造视频」查询;
    // 自定义后按搜索词分别缓存 6 小时,改词立即生效
    ideationSearchQuery: ''
};

// LLM 主模型清单（激发/合成/审核/质检判定共用；网关路由由服务端 resolve_gateway 处理）。
// 按供应商分三组，渲染成激发页脚的分组芯片单选器（见 config.js syncIdeationLlmPicker）：
// - gpt: 模型名含 "gpt-5" 会被 resolve_gateway 自动转发到 codex 网关；只保留当前代
//   gpt-5.5+（旧 gpt-4/gpt-3.5 系列、以及性能较弱的 gpt-5.4 系列都不放进来）。
//   2026-07-13 已用真实请求逐个验证 gpt-5.5/5.6-sol/5.6-terra/5.6-luna 均原生
//   支持联网搜索（{"type":"web_search"} 工具自动执行）。
// - gemini: 走默认网关（config.baseUrl，即 8046）。
// - claude: claude-sonnet-4-6 / claude-opus-4-6-thinking 也走跟 gemini 一样的默认
//   网关（resolve_gateway 没有 claude 专属分支，落进默认分支）——2026-07-13 已用
//   真实请求验证两者都能正常应答。
const LLM_MODEL_GROUPS = {
    gpt: [
        { value: 'gpt-5.5', label: 'gpt-5.5' },
        { value: 'gpt-5.6-sol', label: 'gpt-5.6-sol' },
        { value: 'gpt-5.6-terra', label: 'gpt-5.6-terra' },
        { value: 'gpt-5.6-luna', label: 'gpt-5.6-luna' }
    ],
    gemini: [
        { value: 'gemini-3.7-flash-high', label: 'gemini-3.7-flash-high', recommended: true },
        { value: 'gemini-3.6-flash-high', label: 'gemini-3.6-flash-high' },
        // 2026-07-26 实测：8046 网关直接认这个模型名（内部落到 gemini-pro-agent），
        // 回包带 reasoning_content，正文仍在 choices[0].message.content，链路无需改动。
        { value: 'gemini-3.1-pro-high', label: 'gemini-3.1-pro-high' }
    ],
    claude: [
        { value: 'claude-sonnet-4-6', label: 'claude-sonnet-4-6' },
        { value: 'claude-opus-4-6-thinking', label: 'claude-opus-4-6-thinking' }
    ]
};

// 生图模型清单（与 LLM 模型解耦：resolve_gateway 会按模型名自动路由网关，
// gemini LLM + gpt-image-2 生图这类混搭是合法组合）。
// 注意：这里选 gpt-image-2 是「人明确要它」；系统不会再自己切过去——主模型配额
// 耗尽时自动降级到 gpt-image-2 的机制已整体取消。配额耗尽时系统只会换传输通道、
// 绝不换模型：图生图撞上网关 /images/edits 的号池墙时，同一个模型改走
// /chat/completions 续渲（见 frame_generator.CHAT_TRANSPORT），该帧在 manifest 里
// 标 transport/actual_pixels 留痕（请求 2K/4K 时才另记 degraded_reason，chat 通道
// 固定出 1K 档）；连这条通道也没额度才就地报错。
const IMAGE_MODELS = [
    { value: 'nano-banana-2', label: '🍌 Nano Banana 2 (Gemini)' },
    { value: 'gpt-image-2', label: 'gpt-image-2 (GPT / codex 通道)' }
];

// Google FX（AdsPower 浏览器 UI 自动化）后端的生图模型清单
const FX_IMAGE_MODELS = [
    { value: 'Nano Banana Pro', label: 'Nano Banana Pro' },
    { value: 'Nano Banana 2', label: '🍌 Nano Banana 2' },
    { value: 'Nano Banana 2 Lite', label: '🍌 Nano Banana 2 Lite' }
];

const LEGACY_FX_IMAGE_MODELS = new Set(['imagen 4', 'imagen4', 'image 4', 'image4']);
function normalizeGoogleFxImageModel(value) {
    const current = String(value || '').trim();
    if (LEGACY_FX_IMAGE_MODELS.has(current.toLowerCase())) return 'Nano Banana 2 Lite';
    const matched = FX_IMAGE_MODELS.find(item => item.value.toLowerCase() === current.toLowerCase());
    return matched ? matched.value : DEFAULT_CONFIG.googleFxImageModel;
}

// Global State
let config = { ...DEFAULT_CONFIG };

// 访问码(server-managed 模式);app.js 的 fetch 包装器与 initServerMode 读取/更新它
let ACCESS_CODE = localStorage.getItem('spark_access_code') || '';

let savedIdeas = [];
let currentIdea = null;

// 用户是否在「激发维度」页手动设过节拍数 / 节拍规划模式。
// 置位后有两处行为改变，合起来才让这个设置真的生效：
//   ① 载入灵感卡片不再拿卡片的 recommended_beats 覆盖滑块（旧行为把用户刚设的值
//      静静抹掉，表现为「设了没用」）；
//   ② 卡片上的「一键合成」不走载入维度这一步，此前整份 dimensions 都是硬编码的，
//      节拍数/规划模式压根没读页面——现在以用户的设置为准。
// 与 spark_selection_state 同寿命：设过就一直算数，刷新后仍然认这份设置。
let beatsUserOverridden = localStorage.getItem('spark_beats_user_set') === '1';

/** 记下「节拍数由用户自己定」。只由真实的用户交互调用（拖滑块 / ± 步进 /
 *  切换规划模式 / 应用预设 / 随机配比），程序化回填（读档、载入卡片）绝不调用。 */
function markBeatsUserOverridden() {
    beatsUserOverridden = true;
    try { localStorage.setItem('spark_beats_user_set', '1'); } catch (e) { /* 隐私模式忽略 */ }
}

// 本批灵感注入过 prompt 的联网参考(/api/ideate 返回的 trend_refs:
// 搜索词摘要/自定义网址摘要),渲染在灵感卡片区顶部的可折叠面板
let currentIdeationTrendRefs = [];

let customPresets = {};
// 2026-07-15 多创意后台任务改造：帧序列/视频/封面的生成任务按「所属创意 id」登记，
// 不再是全局单槽位（旧的 activeBackgroundTasks.frames/framesTaskId 等一次只能追踪
// 一个任务，切换创意会把还在后台跑的任务事件误写进当前查看的创意，参见
// ideaTaskHelpers 系列函数 in js/api_client.js）。
// ideaId -> { frames: TaskRecord|null, videos: TaskRecord|null, cover: TaskRecord|null }
// TaskRecord(frames/videos): { taskId, controller, total, meta, progressState, progressInfo, feedLines, live }
// TaskRecord(cover): { taskId, controller }
let ideaTasksById = {};

let generationState = {
    status: 'idle', // idle | composing | error
    startTime: 0,
    timerInterval: null,
    lastParams: null
};

// Global controller for cancelling the (singular, app-wide) idea-composition task.
// Frames/videos/cover no longer use a single global controller — each background
// task's AbortController lives on its own TaskRecord in ideaTasksById instead.
let currentGenerationController = null;

// 流代际守卫：composing 通道仍是全局单例（一次只合成一个创意），保留递增序号守卫。
// frames/videos/cover 改为按创意登记后，各自的"是否仍是当前这次运行"改用
// TaskRecord.taskId 比对（见 js/api_client.js 的 isIdeaTaskCurrent），不再需要全局代数。
const streamEpochs = { compose: 0 };
