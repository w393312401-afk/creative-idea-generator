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
    imageQuality: '2K'
};

// Global State
let config = { ...DEFAULT_CONFIG };

// 访问码(server-managed 模式);app.js 的 fetch 包装器与 initServerMode 读取/更新它
let ACCESS_CODE = localStorage.getItem('spark_access_code') || '';

let savedIdeas = [];
let currentIdea = null;
let activeInputTab = 'text';
let selectedVideoFile = null;

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
