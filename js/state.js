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
    model: 'gemini-3-flash-agent',
    imageModel: 'nano-banana-2',
    // 帧序列生成方式: 'api'（LLM 网关）| 'google_fx'（AdsPower 浏览器 UI 自动化）
    imageBackend: 'api',
    googleFxImageModel: 'Nano Banana 2',
    videoModel: 'Veo 3.1 - Lite [Lower Priority]',
    googleFxIpRotateRequests: 5,
    imageAspectRatio: '9:16',
    imageQuality: '2K',
    // 关键点监修模式: 首帧/镜头族交接锚点帧渲染后暂停等人工确认（采用/重渲，超时自动采用）
    supervisedMode: false,
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
        { value: 'gemini-3-flash-agent', label: 'gemini-3-flash-agent', recommended: true }
    ],
    claude: [
        { value: 'claude-sonnet-4-6', label: 'claude-sonnet-4-6' },
        { value: 'claude-opus-4-6-thinking', label: 'claude-opus-4-6-thinking' }
    ]
};

// 生图模型清单（与 LLM 模型解耦：resolve_gateway 会按模型名自动路由网关，
// gemini LLM + gpt-image-2 生图这类混搭是合法且常用的组合——配额兜底正是这么跑的）
const IMAGE_MODELS = [
    { value: 'nano-banana-2', label: '🍌 Nano Banana 2 (Gemini)' },
    { value: 'gpt-image-2', label: 'gpt-image-2 (GPT / codex 通道)' }
];

// Google FX（AdsPower 浏览器 UI 自动化）后端的生图模型清单
const FX_IMAGE_MODELS = [
    { value: 'Nano Banana Pro', label: 'Nano Banana Pro' },
    { value: 'Nano Banana 2', label: '🍌 Nano Banana 2' },
    { value: 'Imagen 4', label: 'Imagen 4' }
];

// Global State
let config = { ...DEFAULT_CONFIG };

// 访问码(server-managed 模式);app.js 的 fetch 包装器与 initServerMode 读取/更新它
let ACCESS_CODE = localStorage.getItem('spark_access_code') || '';

let savedIdeas = [];
let currentIdea = null;

// 本批灵感注入过 prompt 的联网参考(/api/ideate 返回的 trend_refs:
// 搜索词摘要/自定义网址摘要),渲染在灵感卡片区顶部的可折叠面板
let currentIdeationTrendRefs = [];

let customPresets = {};
let activeBackgroundTasks = {
    cover: false,
    frames: false,
    videos: false
};

let generationState = {
    status: 'idle', // idle | composing | error
    startTime: 0,
    timerInterval: null,
    lastParams: null
};

// Global controllers for cancellation
let currentGenerationController = null;
let currentFramesController = null;
let currentVideosController = null;

// 流代际守卫：每个通道一个递增序号。viewTask/retryTask 接管面板时会开启新一代流，
// 旧流被 abort 后其 catch/finally 仍会异步执行——只有仍是最新一代的流才允许触碰 UI，
// 否则旧流的收尾会清掉新任务刚建好的视图。
const streamEpochs = { compose: 0, frames: 0, videos: 0, cover: 0 };
