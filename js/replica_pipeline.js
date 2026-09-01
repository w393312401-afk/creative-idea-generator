// --- replica_pipeline.js ---
// 爆款复刻 / 二创面板。后端见 replica_pipeline.py，方案见
// docs/replica_and_variant_pipeline_plan.md。
//
// 页面只有三个真状态：没任务、跑着、停在人工卡点。所有长耗时阶段走 SSE 推进度，
// 停下来之后页面是静止的——所以这里没有轮询。

let replicaJobs = [];
let replicaState = null;      // 当前打开的 job_state
let replicaTaskId = null;
let replicaSSE = null;
let replicaBusy = false;
let replicaPromptEditing = false;   // 提示词包是否处于手动编辑态

// stage → 中文标签的**兜底**副本。真源在 replica_pipeline.py 的 STAGE_LABELS，
// 后端随 job 行下发 stage_label；这里只在拿不到时顶上（例如渲染一条本地拼出来的
// 乐观状态）。projects.js 也读这一份，不再自己抄第三份。
const REPLICA_STAGE_LABELS = {
    ingest: '已上传',
    extract: '抽帧中',
    confirm_cost: '待确认成本',
    review_frames: '逐帧读取',
    cluster_beats: '聚类节拍',
    review_beats: '待人工核对',
    mutate_beats: '二创改写中',
    mutate_failed: '二创失败',
    compose: '合成提示词',
    compose_failed: '合成失败',
    audit: '门禁校验',
    audit_failed: '门禁未过',
    completed: '已完成',
    archived: '已归档',
    cancelled: '已取消',
};

// 用户看得见的四个阶段。后台状态机粒度与 UI 呈现阶段映射。
const REPLICA_PHASES = [
    { key: 'material', label: '素材', stages: ['ingest', 'extract', 'confirm_cost'] },
    { key: 'reverse', label: '反推', stages: ['review_frames', 'cluster_beats', 'mutate_beats', 'mutate_failed'] },
    { key: 'review', label: '核对节拍', stages: ['review_beats'] },
    { key: 'deliver', label: '交付', stages: ['compose', 'compose_failed', 'audit', 'audit_failed', 'completed', 'archived'] },
];

function replicaStageLabel(stageOrRow) {
    if (stageOrRow && typeof stageOrRow === 'object') {
        return stageOrRow.stage_label || REPLICA_STAGE_LABELS[stageOrRow.stage] || stageOrRow.stage || '';
    }
    return REPLICA_STAGE_LABELS[stageOrRow] || stageOrRow || '';
}

function replicaPhaseIndex(stage) {
    const at = REPLICA_PHASES.findIndex(p => p.stages.includes(stage));
    return at < 0 ? 0 : at;
}

// 节拍自己的施工阶段（beat.stage）。与 prompt_pipeline/reverse.py 的 _STAGE_LABELS_ZH
// 同源同义——那九个值是 Pass B 的闭集枚举，直接摆英文等于让核对的人先查一遍词典。
const REPLICA_BEAT_STAGE_LABELS = {
    demolition: '拆除清运',
    structural: '结构修复',
    rough_in: '隐蔽工程',
    enclosure: '封板封闭',
    surface: '面层饰面',
    floor: '地面收尾',
    fixtures: '灯具设备',
    furnishing: '家具软装',
    reveal: '成品揭示',
};

// 节拍卡片上按「一行一条」编辑的数组字段（timelapse-beats.schema.json 里的 array 项）。
const REPLICA_LIST_FIELDS = new Set([
    'package_operations', 'persistent_traces', 'visible_details', 'macro_environment',
    'source_event_ids', 'evidence_frames', 'reference_frames', 'sfx',
    // 微观取证三栏（2026-08-24）。漏在这一道上会静默变形：空着的栏 beat[key] 是
    // undefined，Array.isArray 判假，用户敲进去的多行会被存成一个带换行的字符串，
    // 而下游整条链路只认列表——存了等于没存，且不报错。
    'material_specs', 'fastening_and_bonding', 'micro_traces',
]);

// 景别与运镜是闭集（timelapse-beats.schema.json / reverse.SHOT_SCALES、CAMERA_MOVES）。
// 卡片上给下拉不给输入框：自由文本会被规划器当创作提示接着发挥，闭集值才是照抄。
const REPLICA_SHOT_SCALES = [
    ['', '— 未标注 —'],
    ['extreme_wide', '大远景'],
    ['wide', '远景'],
    ['medium', '中景'],
    ['close', '近景'],
    ['extreme_close', '特写'],
];
// 拍摄角度是两根**互相独立**的轴：同一拍可以既是低角度仰拍、又是从侧面拍的。
// 捏成一栏就得二选一，而被丢掉的那一半正是原片最像自己的地方。
const REPLICA_CAMERA_ANGLES = [
    ['', '— 未标注 —'],
    ['bird_eye', '鸟瞰（正上方俯视，地面铺满画面）'],
    ['high_angle', '高角度俯拍（在主体之上往下看，看得见顶面）'],
    ['eye_level', '平视（站立视高，画面水平）'],
    ['low_angle', '低角度仰拍（在主体之下往上看，看得见底面）'],
    ['worm_eye', '虫视（贴地往上看）'],
    ['dutch_angle', '倾斜角（整幅画面歪着，地平线本身是斜的）'],
];
const REPLICA_CAMERA_BEARINGS = [
    ['', '— 未标注 —'],
    ['front', '正面'],
    ['three_quarter', '前侧四分之三（正面＋一个侧面都看得见）'],
    ['side', '侧面（只看得见侧面）'],
    ['rear_three_quarter', '后侧四分之三'],
    ['back', '背面'],
];
// 焦段感与景别是两件事：14mm 拍中景和 85mm 拍中景，透视、畸变、纵深完全两回事。
const REPLICA_LENS_FEELS = [
    ['', '— 未标注 —'],
    ['ultra_wide', '超广（边缘有畸变、近处物体夸张、空间显得更深）'],
    ['wide', '广角（开阔但没有明显畸变）'],
    ['normal', '标准（透视接近肉眼，不拉伸也不压缩）'],
    ['tele', '长焦（背景被压扁贴到主体上）'],
    ['macro', '微距（很小的东西占满画面）'],
];
// 时间处理：此前每一拍都被默认写成「延时加速」，包括最后那个成品巡览拍。
const REPLICA_TIME_TREATMENTS = [
    ['', '— 未标注 —'],
    ['timelapse', '延时加速（工序在飞，人动得比真实快）'],
    ['real_time', '实时（巡览/揭示/一个不紧不慢的动作，原速）'],
    ['slow_motion', '慢动作'],
];
const REPLICA_CAMERA_MOVES = [
    ['', '— 未标注 —'],
    ['static', '固定'],
    ['push_in', '缓推'],
    ['pull_out', '缓拉'],
    ['pan', '横摇'],
    ['tilt', '俯仰摇'],
    ['orbit', '环绕'],
    ['follow', '跟随'],
    ['handheld', '手持'],
    ['crane', '升降'],
];

// 节拍字段的元数据。此前每个字段的说明文是直接写死在 label 里的一整段话（30~90 字），
// 22 个字段加起来 1400 多字常驻在卡片上——说明文的字数是数据本身的 5~10 倍，人要在
// 一屏里找一个值，得先跳过一屏的教程。文案一个字没改，只是从「常驻」挪到了「按需」：
//   name  卡片上常驻的短名（2~5 字）
//   help  完整说明，收进短名后面那枚 ⓘ，悬停/聚焦才出
//   count [min, max] 条数约束；渲染成右上角的小徽章，越界变红。此前它混在说明文里
//         （「须 3~6 条；当前 4 条」），是说明不是状态，越界了也不会变色
//   group 分到哪一组：fact 画面事实 / state 状态与痕迹 / shot 拍摄与声音 / more 更多
//   rows  textarea 初始行数（浏览器不支持 field-sizing 时的回退值）
const REPLICA_FIELD_META = {
    space: {
        name: '所在空间', group: 'fact', rows: 1,
        help: '同一个空间逐字沿用同一个名字；换名字＝机位穿过开口进了另一个空间，会多出一次过门。',
    },
    macro_environment: {
        name: '大环境', group: 'fact', rows: 2,
        help: '地貌水体、气候光照、空间包络；一行一条。只写这地方本来长什么样，本拍挖出来/砌起来的东西写进起始状态。',
    },
    operation: {
        name: '主导工序', group: 'fact', rows: 1,
        help: '1~3 个词的里程碑工序词，如「吊装就位 / seat bus」；别写成带宾语的整句，合成器拿它做相位判定。',
    },
    package_operations: {
        name: '工序包', group: 'fact', rows: 2, count: [2, 3],
        help: '一行一道，须 2~3 道。',
    },
    visible_details: {
        name: '细节识别项', group: 'fact', rows: 2, count: [3, 6],
        help: '一行一条，须 3~6 条、建议顶到 5~6。每条＝材料+颜色/质感/状态+位置；别复述大环境或遗留痕迹。',
    },
    visible_action: {
        name: '可见动作', group: 'fact', rows: 2,
        help: '这一拍里眼睛能看见的工序动作本身。',
    },
    cast_action: {
        name: '人物动作神情', group: 'fact', rows: 2,
        help: '写「从上一拍的什么姿态、动到这一拍的什么姿态」：起身、转向、上前半步、蹲下去看、抬手指。'
            + '别写「还站在原地/保持原样」——那是站位不是动作，下游会把它原样写进每一帧的图，交付出来就是一动不动的小人。'
            + '别把可见动作再写一遍：那一栏是工序，这一栏是人。真的几乎没动，就写那个最小的真实变化。',
    },
    visible_result: {
        name: '可见结果', group: 'state', rows: 2,
        help: '这一下看见了什么：车体沉下去、吊索由紧转松。',
    },
    state_before: {
        name: '起始状态', group: 'state', rows: 2,
        help: '写「量」不是写「样」：范围/比例/齐平关系/高度差。',
    },
    state_after: {
        name: '结束状态', group: 'state', rows: 2,
        help: '完成到哪儿，须带一个量，别把可见结果再写一遍。',
    },
    persistent_traces: {
        name: '遗留痕迹', group: 'state', rows: 2, count: [2, null],
        help: '一行一条，须 ≥2 条。只写本拍新留下的痕迹＋它落在哪个面上，原本就有的落叶青苔不算。',
    },
    light_state: {
        name: '光照时段', group: 'state', rows: 1,
        help: '如「阴天正午、无投影」；延时片跨天，不逐拍声明光就会自己跳。',
    },
    subject_placement: {
        name: '主体构图', group: 'shot', rows: 2,
        help: '主体在画面左/中/右、上/下，占画面高度几分之几，地平线在第几分。'
            + '锚点的位置与占比此前从没在原片上量过，全靠这一栏。分数写汉字，别写数字和百分号。',
    },
    tool: {
        name: '主导工具', group: 'shot', rows: 1,
        help: '动作峰值上那一件：吊车 / 冲击钻 / 橡胶锤。三联绑定的一环，塞在动作句里合成器读不出来。',
    },
    sfx: {
        name: '本拍声音', group: 'shot', rows: 2, count: [1, 3],
        help: '一行一个声源，1~3 条。原声物理音，绝不写配乐——交付口径是 ASMR 60% / BGM 0%。',
    },
    material_specs: {
        name: '材料规格', group: 'more', rows: 2, count: [null, 3],
        help: '一行一条，≤3 条。写「多厚、什么等级、什么面」——9mm OSB 哑光面 / 2x4 SPF 龙骨；'
            + '细节识别项那一栏写的是「什么料、什么颜色、在哪儿」，别重复。',
    },
    fastening_and_bonding: {
        name: '紧固与粘接', group: 'more', rows: 2, count: [null, 3],
        help: '一行一条，≤3 条。沉头自攻钉 / 发泡胶封缝 / 结构胶。'
            + '它决定接缝长什么样，也决定本拍那一下是拧、是钉还是挤。',
    },
    micro_traces: {
        name: '微观痕迹', group: 'more', rows: 2, count: [null, 3],
        help: '一行一条，≤3 条。细木屑、铅笔弹线、过喷飞溅。'
            + '与遗留痕迹分工：那一栏必须被后续帧继承，这一栏不要求。',
    },
    tool_specifics: {
        name: '工具具体型号', group: 'more', rows: 1,
        help: '哪一种、怎么驱动、在用什么刀头批头：18V 无刷冲击钻＋磁性批头 / 气动排钉枪 / 不锈钢齿口抹刀。'
            + '主导工具那一栏答「是什么工具」，这一栏答「是哪一种」。',
    },
    material_flow: {
        name: '物料去向', group: 'more', rows: 1,
        help: '挖出来的土去哪了 / 耗掉的料从哪来。',
    },
    insert_subject: {
        name: '插入镜主体', group: 'more', rows: 1,
        help: '原片这一拍切进特写时拍的是什么，如「镊子尖压住的那片瓦」。'
            + '空着就落回通用职责——工具接触点/持久痕迹，那是任何一拍都能写的话。',
    },
    visual_subject: {
        name: '画面主体', group: 'more', rows: 1,
        help: '派生字段：只在可见动作与可见结果都空着时兜底，另供自动标题取主语；平时不必改。',
    },
};

// 闭集参数压成一行：这七个都是从固定表里选一个值，此前每个都占一整格
// （一行短名 + 一个全宽下拉），七个格子就是半张卡片的高度。改成内联「短名：值」
// 胶囊，同样的信息占一行多一点。顺序按拍摄时真的会一起决定的先后：先景别、
// 再机位、再镜头、最后时间处理。
const REPLICA_SHOT_PARAMS = [
    ['shot_scale', '景别', REPLICA_SHOT_SCALES, '这一拍是远景还是特写'],
    ['camera_angle', '角度', REPLICA_CAMERA_ANGLES, '垂直：机位在主体的上方还是下方'],
    ['camera_bearing', '方位', REPLICA_CAMERA_BEARINGS, '水平：镜头对着主体的哪一面'],
    ['lens_feel', '焦段', REPLICA_LENS_FEELS, '多广的镜头，跟景别是两件事'],
    ['camera_move', '运镜', REPLICA_CAMERA_MOVES, '机位在这一拍里怎么动'],
    ['time_treatment', '时间', REPLICA_TIME_TREATMENTS, '这一拍是加速的还是原速的'],
];

let replicaActivePreset = 'custom';
let replicaAiIdeas = [];
let replicaActiveIdeaIndex = -1;
let replicaDivergeBrief = '';
let replicaDiverging = false;
let replicaDivergeStep = 1;
let replicaDivergeStatusText = '';
let replicaComparatorOpen = false;
let replicaHelpScrollBound = false;
let replicaBeatFoldState = {};
// 节拍卡片**内部**那些 <details> 的开合，键是 `${beat.id}:${字段名}`。
// 服务端会在拆合拍后重排 id，所以它必须跟 replicaBeatFoldState 在同一时刻清空——
// 不清的话，新占用 B05 这个名字的那一拍会带着上一任展开过的可选字段。
let replicaFieldFoldState = {};
// 节拍编辑器里所有会写回 replicaState.beats 的可编辑控件。三处用到：运行中锁只读
// （replicaSetBusy）、脏标记（replicaBindBeatEvents）、Cmd+S 时的失焦提交。
// 下拉框单列一份：readOnly 对 <select> 无效，只能 disabled。
const REPLICA_BEAT_INPUT_SELECTOR =
    'textarea[data-beat][data-key], input[data-beat][data-key], '
    + '#replica-banned, #replica-scene-signature, [data-scene-key]';
const REPLICA_BEAT_SELECT_SELECTOR = 'select[data-beat][data-key]';
// 有未落盘的节拍改动。置位在各 input 处理器里，清零在 replicaSaveBeats 成功之后。
let replicaDirty = false;
let replicaJobListExpanded = false;
let replicaJobListSearchQuery = '';
let replicaVariantFoldState = {};
let replicaExtractExpanded = false;
// 抽帧拼贴图默认收起。它是这一屏最高的一块（近 300px），而绝大多数时候用户只是路过
// 它去下面的节拍阶梯——默认摊开等于每次都要多滚一屏。展开态跨重渲染活下来，
// 与任务列表同一套做法（见 replicaJobListExpanded）。
let replicaCollageExpanded = false;
// 比例条下面那排拍号 chip。条上够宽的块现在自己写着拍号了，chip 条降级成索引，
// 默认收起——它在拍多时能占掉三四行，把下面的节拍卡片全推出屏幕。
let replicaLadderChipsOpen = false;
let replicaRefsDrawerOpen = false;
let replicaRefsDirectionOpen = false;
let replicaRefsFilterQuery = '';

const REPLICA_AXES = [
    { key: 'environment', label: '地貌水体', hint: '荒野水岸 → 极地峡湾 / 火山地热 / 雨林溪谷 / 崖壁溶洞 / 荒漠绿洲' },
    { key: 'material', label: '材质工艺', hint: '钢构原木 → 粗石毛石 / 侘寂微水泥 / 黑碳钢+火山石 / 老柚木+黄铜 / 传统夯土' },
    { key: 'function', label: '空间功用', hint: '江景卧房 → 极地观景木屋 / 恒温私汤茶室 / 水上木工坊 / 崖穴酒窖 / 夯土避暑居' },
    { key: 'hero_reveal', label: '终极生物', hint: '野生大鲟鱼 → 北极白鲸 / 高山马鹿 / 温泉猕猴 / 热带巨骨舌鱼 / 绿洲双峰驼' },
];

const REPLICA_MAX_AXES = 4;

/* --- 反推段的模型与采样档位 ---
 *
 * 反推（Pass A 逐帧识别 + 峰值帧复核）此前只能改配置文件：`frameFactsModel` /
 * `peakVerifyModel` 两个键在 UI 上不存在，页面上的「LLM 模型」选择器管的是激发/
 * 合成那条链路，改它对反推毫无影响。而这两步恰恰是整条复刻线上最吃模型能力的
 * 地方——flash 读不出材料标签和完成范围，节拍就从源头错了。所以把它们摆到成本
 * 确认卡点上，和帧数、调用次数一起看。
 *
 * 模型清单直接复用 js/state.js 的 LLM_MODEL_GROUPS（不抄第二份；那边加了模型这里
 * 自动就有）。state.js 没加载时退回一份最小清单，保证选择器不会变成空下拉。
 */
const REPLICA_FALLBACK_MODELS = [
    { value: 'gemini-3.7-flash-high', label: 'gemini-3.7-flash-high' },
    { value: 'gemini-3.6-flash-high', label: 'gemini-3.6-flash-high' },
    { value: 'gemini-3.1-pro-high', label: 'gemini-3.1-pro-high' },
];

const REPLICA_PASS_A_DEFAULT_MODEL = 'gemini-3.7-flash-high';

function replicaModelChoices() {
    const groups = typeof LLM_MODEL_GROUPS !== 'undefined' ? LLM_MODEL_GROUPS : null;
    if (!groups) return REPLICA_FALLBACK_MODELS.slice();
    return ['gemini', 'gpt', 'claude']
        .flatMap(key => (groups[key] || []).map(m => ({ value: m.value, label: m.label })));
}

// 下拉里出现的值不一定在清单里（配置文件里手写过一个自定义模型名）：把当前值补进去，
// 否则下拉会自己跳到第一项，用户一保存就把配置文件里的选择改掉了。
function replicaModelSelect(id, current, extraOptions) {
    const choices = replicaModelChoices();
    const options = (extraOptions || []).concat(choices);
    const cur = String(current == null ? '' : current);
    if (cur && !options.some(o => o.value === cur)) {
        options.push({ value: cur, label: `${cur}（自定义）` });
    }
    return `<select id="${id}" class="replica-select">${options.map(o => `
        <option value="${escapeHtmlReplica(o.value)}" ${o.value === cur ? 'selected' : ''}
        >${escapeHtmlReplica(o.label)}</option>`).join('')}</select>`;
}

// 反推段的模型选择写回全局 config + localStorage，和激发页脚的模型选择器同一套
// 持久化（'spark_config'）——服务端读的是请求体里的 config，不另开一条存储。
function replicaSetConfigValue(key, value) {
    if (typeof config === 'undefined' || !config) return;
    config[key] = value;
    try {
        localStorage.setItem('spark_config', JSON.stringify(config));
    } catch (e) {
        console.warn('[replica] 配置写入 localStorage 失败', e);
    }
}

function replicaConfigValue(key, fallback) {
    const cfg = typeof config !== 'undefined' && config ? config : {};
    const v = cfg[key];
    return v === undefined || v === null || v === '' ? fallback : v;
}

// 抽帧密度档位。与 replica_pipeline.EXTRACT_FPS_CHOICES 同值同序；服务端会把认不出
// 的值回落到默认档，这里只负责把选项摆出来。
const REPLICA_FPS_CHOICES = [
    { value: 1, label: '1 fps（每秒 1 张，只看大轮廓）' },
    { value: 2, label: '2 fps（默认，延时片调过的基线）' },
    { value: 3, label: '3 fps' },
    { value: 4, label: '4 fps（慢工序推荐：刮腻子、铺砖）' },
    { value: 6, label: '6 fps（最密，抽帧耗时最长）' },
];
const REPLICA_DEFAULT_FPS = 2;

function replicaCurrentFps(state) {
    const v = state && state.sampling && state.sampling.base_fps;
    return REPLICA_FPS_CHOICES.some(c => c.value === Number(v)) ? Number(v) : REPLICA_DEFAULT_FPS;
}

// 单选框的 value → 后端档位名。'full' 这个 value 是三档之前留下来的（那时只有
// 完整/降级两档），后端叫它 'plan'——改 value 会让浏览器里缓存着旧页面的人静默换档，
// 所以在这里翻译，而不是去动 HTML。
const REPLICA_SCOPE_BY_MODE = { all: 'all', full: 'plan', degraded: 'degraded' };

function replicaScopeFromMode(value) {
    return REPLICA_SCOPE_BY_MODE[value] || 'plan';
}

function replicaFpsSelect(id, current) {
    return `<select id="${id}" class="replica-select">${REPLICA_FPS_CHOICES.map(c => `
        <option value="${c.value}" ${c.value === current ? 'selected' : ''}
        >${escapeHtmlReplica(c.label)}</option>`).join('')}</select>`;
}

// 状态跳变检测灵敏度档位。与 replica_pipeline.STATE_DIFF_THRESHOLD_CHOICES 同值同序；
// 服务端会把认不出的值回落到默认档，这里只负责把选项摆出来。这道阈值决定「这一刻算不算
// 发生了一次工序状态变化」——数值越低越敏感（抓得住细微变化，噪声也更多），越高越保守
// （只认明显跳变，容易把渐变工序整段吃掉）。此前完全没有入口，只能改脚本硬编码值。
const REPLICA_THRESHOLD_CHOICES = [
    { value: 0.04, label: '0.04（最敏感，细微渐变也抓，噪声最多）' },
    { value: 0.06, label: '0.06' },
    { value: 0.08, label: '0.08（默认）' },
    { value: 0.12, label: '0.12' },
    { value: 0.16, label: '0.16（最保守，只认明显跳变）' },
];
const REPLICA_DEFAULT_THRESHOLD = 0.08;

function replicaCurrentThreshold(state) {
    const v = state && state.sampling && state.sampling.state_diff_threshold;
    return REPLICA_THRESHOLD_CHOICES.some(c => c.value === Number(v)) ? Number(v) : REPLICA_DEFAULT_THRESHOLD;
}

function replicaThresholdSelect(id, current) {
    return `<select id="${id}" class="replica-select">${REPLICA_THRESHOLD_CHOICES.map(c => `
        <option value="${c.value}" ${c.value === current ? 'selected' : ''}
        >${escapeHtmlReplica(c.label)}</option>`).join('')}</select>`;
}

/* --- API --- */

function replicaHeaders(json = true) {
    const code = typeof ACCESS_CODE !== 'undefined' ? ACCESS_CODE : '';
    const headers = {};
    if (json) headers['Content-Type'] = 'application/json';
    if (code) headers['X-Access-Code'] = code;
    return headers;
}

async function replicaFetch(url, options = {}) {
    const res = await fetch(url, options);
    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON 响应下面统一报错 */ }
    if (!res.ok || (data && data.status === 'error')) {
        throw new Error((data && data.message) || `${res.status} ${res.statusText}`);
    }
    return data;
}

async function replicaLoadJobs() {
    const data = await replicaFetch('/api/replica/jobs', { headers: replicaHeaders() });
    replicaJobs = data.jobs || [];
    return replicaJobs;
}

// 复刻线的阶段变更提醒：每个 job 单独记上一次已提醒的阶段，切任务不会误报，
// 同一阶段轮询多次也只响一次。
const replicaNotifiedStage = {};

function replicaNotifyStageChange(state) {
    if (!state || !state.job_id || typeof NotificationCenter === 'undefined') return;
    const stage = state.stage;
    if (!stage || replicaNotifiedStage[state.job_id] === stage) return;
    const first = !(state.job_id in replicaNotifiedStage);
    replicaNotifiedStage[state.job_id] = stage;
    // 第一次加载已有任务时不补响历史阶段，只有真的发生了推进才提醒
    if (first) return;

    const title = state.title || state.source_title || '复刻任务';
    if (stage === 'completed') {
        NotificationCenter.notify({
            type: 'success',
            title: '复刻任务已完成',
            message: `${title}：全流程已跑完，可以去交付区取片了`
        });
    } else if (stage === 'audit_failed' || stage === 'compose_failed' || stage === 'mutate_failed') {
        NotificationCenter.notify({
            type: 'error',
            title: '复刻任务失败',
            message: `${title}：${state.error || '流程中断，请查看任务详情'}`
        });
    } else if (stage === 'confirm_cost') {
        NotificationCenter.notify({
            type: 'action_required',
            title: '复刻任务等待确认',
            message: `${title}：请确认本次生成的花费后才会继续`
        });
    } else if (stage === 'review_beats') {
        NotificationCenter.notify({
            type: 'action_required',
            title: '复刻任务等待审核',
            message: `${title}：拍点已就绪，请在工作台确认是否通过`
        });
    }
}

async function replicaLoadJob(jobId) {
    // 切走会把 replicaState 整个换掉，内存里没落盘的改动一起没。此前不问一句：改了
    // 二十拍、顺手点了列表里另一条任务，没有任何提示，也回不来。
    // 只在**真的切到另一条**时拦；同一条任务的刷新（SSE 收尾、保存后回读）不该弹窗。
    if (replicaDirty && replicaState && replicaState.job_id !== jobId
        && !window.confirm('当前任务有改动还没保存，切走就没了。确定切换？')) {
        return replicaState;
    }
    const data = await replicaFetch(`/api/replica/status?job_id=${encodeURIComponent(jobId)}`,
        { headers: replicaHeaders() });
    const switched = !replicaState || replicaState.job_id !== jobId;
    if (switched) replicaMarkDirty(false);
    replicaState = data.job_state;
    replicaNotifyStageChange(replicaState);
    // 折叠状态按 beat.id 存，而 B01…B11 在每一条任务里都叫这个名字。不清空的话，
    // 在 A 任务折起来的 B03，切到 B 任务照样是折的——用户没折过它，它却是折的。
    if (switched) { replicaBeatFoldState = {}; replicaFieldFoldState = {}; }
    // 提示词编辑态同理：不清的话，在 A 任务点开的编辑器会带着 B 任务的正文继续开着，
    // 用户以为自己还在改 A。
    if (switched) replicaPromptEditing = false;
    return replicaState;
}

/* --- 帧与拼图的 URL --- */
// 服务端给的是绝对文件路径；静态路由只认 /outputs 下的相对路径。变体 job 自己不存帧，
// 指回源 job 的目录。
function replicaFrameBase(state) {
    if (!state) return `/outputs/replica_jobs`;
    const targetId = state.parent_baseline_id || state.variant_of || state.job_id;
    let rootId = targetId;
    if (typeof replicaJobs !== 'undefined' && Array.isArray(replicaJobs) && replicaJobs.length) {
        let cur = replicaJobs.find(j => j.job_id === targetId);
        let depth = 0;
        while (cur && (cur.parent_baseline_id || cur.variant_of) && depth < 10) {
            const nextParent = cur.parent_baseline_id || cur.variant_of;
            const nextJob = replicaJobs.find(j => j.job_id === nextParent);
            if (!nextJob || nextParent === cur.job_id) break;
            rootId = nextParent;
            cur = nextJob;
            depth++;
        }
    }
    return `/outputs/replica_jobs/${rootId || targetId}`;
}

// 服务端按磁盘上的实际位置给出 frame_urls；拿不到时才退回旧的猜法（老状态文件、
// 或者 /api/replica/jobs 那条精简行）。猜法本身是个隐患：目录布局是抽帧脚本的实现
// 细节，脚本一改目录名，证据帧就在最需要看图的地方碎成一片。
function replicaFrameUrl(state, name) {
    const known = (state.frame_urls || {})[name];
    if (known) return known;
    const dir = /^scene_/.test(name) ? 'storyboard' : 'review_frames';
    return `${replicaFrameBase(state)}/${dir}/${encodeURIComponent(name)}`;
}

function replicaCollageThumbUrl(state) {
    const raw = state.overview && state.overview.collage_thumb;
    if (!raw) return replicaCollageUrl(state);
    const base = raw.split(/[\\/]/).pop();
    return `${replicaFrameBase(state)}/${encodeURIComponent(base)}`;
}

function replicaCollageUrl(state) {
    const raw = state.overview && state.overview.collage;
    if (!raw) return null;
    const base = raw.split(/[\\/]/).pop();
    return `${replicaFrameBase(state)}/${encodeURIComponent(base)}`;
}

/* --- 渲染 --- */

function escapeHtmlReplica(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function replicaRoot() {
    return document.getElementById('replica-root');
}

// 字段说明的浮层。单例，挂在 body 上而不是字段里：卡片有自己的圆角与溢出裁剪，
// 说明文塞在格子里要么被裁掉一半，要么把那一格撑高、把整行推下去。
function replicaHelpTip() {
    let tip = document.getElementById('replica-help-tip');
    if (!tip) {
        tip = document.createElement('div');
        tip.id = 'replica-help-tip';
        tip.className = 'replica-help-tip';
        tip.setAttribute('role', 'tooltip');
        document.body.appendChild(tip);
    }
    return tip;
}

// 中文对照的开合。默认关：英文才是送去合成的事实源，中文只在核对时才需要，
// 而它此前是全卡最跳的一层颜色、还占掉每一格三分之一的高度。
function replicaZhMirrorOn() {
    try {
        return localStorage.getItem('replica_zh_mirror') === '1';
    } catch (e) {
        return false;
    }
}

function replicaSetZhMirror(on) {
    try {
        localStorage.setItem('replica_zh_mirror', on ? '1' : '0');
    } catch (e) { /* 隐私模式下存不住，本次会话内照常生效 */ }
    replicaApplyZhMirror();
}

function replicaApplyZhMirror() {
    const root = replicaRoot();
    if (!root) return;
    const on = replicaZhMirrorOn();
    root.classList.toggle('replica-show-zh', on);
    root.querySelectorAll('[data-zh-toggle]').forEach(btn => {
        btn.textContent = on ? '中文对照 ✓' : '中文对照';
        btn.setAttribute('aria-pressed', String(on));
    });
}

// 这一页真正的滚动容器。全站在任何宽度下都是「单面板 + 面板内滚动」，window 从不滚动
// （见 css/app/base.css 的 .app-main 与 panels-tabs.css 的 .glass-panel 手机规则）——
// 所以任何存位/还位/滚动监听都必须挂在它身上，用 window.scrollY 得到的恒为 0。
function replicaShell() {
    const root = replicaRoot();
    return (root && root.closest('.replica-shell')) || document.querySelector('.replica-shell');
}

function replicaRenderFloatingTools() {
    return `
    <div class="replica-floating-tools" id="replica-floating-tools">
        <button type="button" class="replica-float-btn" data-float-action="top" title="回到顶部">
            <span class="replica-float-icon">⌃</span>
            <span class="replica-float-label">顶部</span>
        </button>
        <button type="button" class="replica-float-btn replica-float-save" data-float-action="save" title="保存并重校验">
            <span class="replica-float-icon">💾</span>
            <span class="replica-float-label">保存</span>
        </button>
    </div>`;
}

// 右侧悬浮区段导航（Notion 目录式：页面右侧竖直居中，收起时只有一列短横线，
// 悬停/聚焦展开成带文字的面板，样式见 css/app/replica.css）。
//
// 每一项的出现条件必须与「那一块到底渲不渲染」逐字一致——导航项指向一个不存在的
// 锚点时，点它是彻底静默的：不滚动、不报错、不给任何反馈。所以这里的判据一律从
// 对应渲染函数里抄同一条，改了那边就得改这边（scratchpad 的冒烟脚本会逐项核对）。
function replicaRenderNavBar(state) {
    const beats = (state && state.beats && state.beats.beats) || [];
    const hasBeats = beats.length > 0;
    // 与 replicaRenderDualWorkbench 的两条门槛同源
    const pastExtract = !!state && state.stage !== 'ingest' && state.stage !== 'extract'
        && state.stage !== 'confirm_cost';

    const items = [];
    items.push({ id: 'replica-sec-uploader', label: '素材上传' });
    if (replicaJobs.length > 0) {
        items.push({ id: 'replica-sec-jobs', label: '已有任务' });
    }
    if (pastExtract && hasBeats) {
        items.push({ id: 'replica-sec-variant', label: '二创发散' });
    }
    if (state && state.overview) {
        items.push({ id: 'replica-sec-extract', label: '抽帧结果' });
    }
    if (hasBeats) {
        const violations = state.validation || (state.beats && state.beats.validation) || [];
        const errCount = violations.filter(v => v.level === 'error').length;
        items.push({
            id: 'replica-sec-beats',
            label: '节拍阶梯',
            count: beats.length,
            errors: errCount,
        });
        // 与 replicaRenderSceneConstants 的空判据同源
        const hasConstants = state.beats.scene_constants && (
            Array.isArray(state.beats.scene_constants)
                ? state.beats.scene_constants.length > 0
                : Object.values(state.beats.scene_constants).some(a => a && a.length)
        );
        if (state.beats.scene_signature || hasConstants) {
            items.push({ id: 'replica-sec-scene', label: '场景恒常' });
        }
    }
    if (state && state.prompt_block) {
        items.push({ id: 'replica-sec-output', label: '提示词包' });
    }

    if (!items.length) return '';

    return `
    <nav class="replica-nav-bar" id="replica-nav-bar" aria-label="区段导航">
        <div class="replica-nav-scroll">
            ${items.map((item, idx) => `
                <button type="button" class="replica-nav-item ${idx === 0 ? 'active' : ''}" data-nav-target="${item.id}"
                        title="${escapeHtmlReplica(item.label)}" aria-label="${escapeHtmlReplica(item.label)}">
                    <span class="replica-nav-dash" aria-hidden="true"></span>
                    <span class="replica-nav-label">${escapeHtmlReplica(item.label)}</span>
                    ${item.count ? `<span class="replica-nav-count">${item.count}</span>` : ''}
                    ${item.errors ? `<span class="replica-nav-badge-err">${item.errors}</span>` : ''}
                </button>
            `).join('')}
        </div>
    </nav>`;
}

function replicaRenderHeaderToolbar(state) {
    if (!replicaJobs.length) return '';
    return `
    <div class="replica-card replica-header-toolbar">
        <label class="replica-header-job">
            <span class="replica-header-job-label">🎬 切换任务</span>
            <select id="replica-quick-job-select" class="replica-select">
                ${replicaJobs.map(j => `
                    <option value="${escapeHtmlReplica(j.job_id)}" ${state && state.job_id === j.job_id ? 'selected' : ''}>
                        ${j.variant_of ? '🧬' : '🎬'} ${escapeHtmlReplica(j.title || j.video_name || j.job_id)} (${escapeHtmlReplica(replicaStageLabel(j))})
                    </option>
                `).join('')}
            </select>
        </label>
        <button type="button" id="replica-new-upload-btn" class="action-btn text-btn replica-mini-btn replica-header-new">
            ＋ 上传新视频
        </button>
    </div>`;
}

// 「清理合成缓存」勾选状态。见 replicaRenderBottomBar 里那段说明。
let replicaResetCache = false;

// 吸底主操作栏：每个阶段只有一个主 CTA，永远够得着。
//
// 这条栏是「保存并重校验 / 合成提示词 / 确认并开始反推 / 存入项目」的**唯一**落点——
// 节拍区底部那一排只留别处没有的动作（重跑聚类、重译中文、AI 修复硬伤）。
// AI 修复不放在这里：硬伤清单在校验横幅里，修复按钮就该挨着那份清单；这条栏上的
// 硬伤计数做成可点，按一下直接把人带到那份清单前面。
function replicaRenderBottomBar(state) {
    if (!state) return '';
    const stage = state.stage || '';
    const violations = state.validation || (state.beats && state.beats.validation) || [];
    const errors = violations.filter(v => v.level === 'error');
    const hasBeats = !!(state.beats && (state.beats.beats || []).length);
    const isRunning = !!replicaSSE;

    // 「清理合成缓存」。合成缓存的键是 brief 指纹（dimensions + 技能 profile +
    // MILESTONE_POLICY_VERSION）——只改了提示词规则而没动节拍时，指纹一个字不变，
    // 「重新合成」照样命中旧断点、直接跳过整个 Phase 1，用户看着进度条走完，拿到的
    // 还是旧规则那一份。勾上它这一轮从头跑（见 replica_pipeline.run_compose 的
    // reset_cache）。默认不勾：断点续传省的是几分钟大模型钱，不该为一个偶发场景
    // 让每一次重试都全额重付。
    // 状态存在模块变量而不是 DOM：这条栏会被 replicaRefreshChrome 整段重建，
    // 存 DOM 里的话用户勾完随便保存一次就被清掉了。
    const resetCacheToggleHtml = `
        <label class="replica-inline-toggle" title="不复用任何上一轮的合成产物（断点存档、空间锁定包、本任务 Phase 1 产物），从头跑一遍。改过提示词规则后要的就是这个——否则指纹没变，会直接续上旧规则那一份。代价是多花一次 Phase 1 的模型调用。">
            <input type="checkbox" id="replica-reset-cache" ${replicaResetCache ? 'checked' : ''}>
            <span>清理合成缓存</span>
        </label>`;

    let mainActionHtml = '';
    if (isRunning) {
        mainActionHtml = `<button type="button" id="replica-bar-cancel-btn" class="action-btn text-btn">中断这一轮</button>`;
    } else if (stage === 'confirm_cost') {
        mainActionHtml = `<button type="button" id="replica-bar-start-btn" class="action-btn primary-btn">确认并开始反推</button>`;
    } else if (stage === 'completed' || stage === 'audit') {
        const hasPrompt = !!state.prompt_block;
        mainActionHtml = `
            <button type="button" id="replica-bar-save-btn"
                    class="action-btn text-btn ${replicaDirty ? 'replica-bar-save-dirty' : ''}"
                    ${replicaDirty ? 'title="有改动还没存下来"' : ''}
                >保存并重校验${replicaDirty ? '<span class="replica-dirty-dot"></span>' : ''}</button>
            ${resetCacheToggleHtml}
            ${hasPrompt
                ? `<button type="button" id="replica-bar-recompose-btn" class="action-btn text-btn" ${errors.length ? 'disabled title="先修掉硬伤"' : ''}>重新合成</button>
                   <button type="button" id="replica-bar-project-btn" class="action-btn primary-btn">存入项目并打开激发结果</button>`
                : `<button type="button" id="replica-bar-compose-btn" class="action-btn primary-btn" ${errors.length ? 'disabled title="先修掉硬伤"' : ''}>合成提示词</button>`}
        `;
    } else if (stage === 'audit_failed' || stage === 'compose_failed') {
        mainActionHtml = `
            <button type="button" id="replica-bar-save-btn"
                    class="action-btn text-btn ${replicaDirty ? 'replica-bar-save-dirty' : ''}"
                    ${replicaDirty ? 'title="有改动还没存下来"' : ''}
                >保存并重校验${replicaDirty ? '<span class="replica-dirty-dot"></span>' : ''}</button>
            ${resetCacheToggleHtml}
            <button type="button" id="replica-bar-recompose-btn" class="action-btn primary-btn" ${errors.length ? 'disabled title="先修掉硬伤"' : ''}>重新合成</button>
        `;
    } else if (hasBeats) {
        mainActionHtml = `
            <button type="button" id="replica-bar-save-btn"
                    class="action-btn text-btn ${replicaDirty ? 'replica-bar-save-dirty' : ''}"
                    ${replicaDirty ? 'title="有改动还没存下来"' : ''}
                >保存并重校验${replicaDirty ? '<span class="replica-dirty-dot"></span>' : ''}</button>
            ${resetCacheToggleHtml}
            <button type="button" id="replica-bar-compose-btn" class="action-btn primary-btn" ${errors.length ? 'disabled title="先修掉硬伤"' : ''}>合成提示词</button>
        `;
    }

    const statusHtml = errors.length
        ? `<button type="button" id="replica-bar-errors-btn" class="replica-chip replica-chip-error replica-bar-errors"
                   title="按一下跳到校验横幅，那里列着每一条硬伤">⚠️ ${errors.length} 项硬伤</button>`
        : (hasBeats ? '<span class="replica-chip replica-chip-ok">✓ 校验通过</span>' : '');

    return `
    <div class="replica-bottom-bar" id="replica-bottom-bar">
        <div class="replica-bottom-bar-info">
            <span class="replica-chip">${escapeHtmlReplica(replicaStageLabel(state))}</span>
            ${statusHtml}
        </div>
        <div class="replica-bottom-bar-actions">
            ${mainActionHtml}
        </div>
    </div>`;
}

function replicaRender() {
    const root = replicaRoot();
    if (!root) return;
    root.innerHTML = `
        ${replicaRenderFloatingTools()}
        ${replicaRenderNavBar(replicaState)}
        ${replicaRenderHeaderToolbar(replicaState)}
        ${replicaRenderUploader()}
        ${replicaRenderJobList()}
        ${replicaState ? replicaRenderJob(replicaState) : ''}
        ${replicaState ? replicaRenderBottomBar(replicaState) : ''}
    `;
    replicaBindEvents();
    replicaInitScrollSpy();
    replicaUpdateTabBadge();
    // 重建 DOM 之后必须立刻按当前滚动位置重算一次导航态。滚动监听只在真的滚动时才
    // 触发，而 innerHTML 换掉的是内容、不是 scrollTop —— 在节拍区点「AI 修复硬伤」
    // 触发整页重渲之后，药丸会停在模板里写死的第一项（「素材上传」）、悬浮工具会
    // 因为丢了 .is-visible 而整个消失，直到用户手动滚一下才恢复。
    replicaHandleScroll(replicaShell());
    // 重建 DOM 会把按钮的 disabled 一起丢掉。跑着的时候重渲染（例如刚开跑要让「中断」
    // 露出来）之后不重新落一次 busy，整排按钮就又可点了——用户能在 Pass A 跑到一半时
    // 再点一次「开始反推」。
    if (replicaBusy) replicaSetBusy(true);
    // 重建 DOM 也会把进度条清空（它是 JS 直接写进去的，不在模板里）。跑着的时候
    // 重渲染之后必须把当前进度重新画一遍，否则一次 replicaRender() 就让进度归零。
    if (replicaSSE && replicaProgress) replicaProgressPaint();
}

function replicaRenderUploader() {
    return `
    <div class="replica-card replica-uploader" id="replica-sec-uploader">
        <div class="replica-card-title">上传成品视频</div>
        <p class="replica-hint">
            抽帧密度默认按延时视频调过（基线 2fps + 状态跳变密采 + 首尾密采）。慢工序
            （刮腻子、铺砖这类两张之间就跨过半个工序的）可以往上调——抽帧是本地 ffmpeg，
            不花模型钱，只是更慢。上传后会先给出待送审帧数与调用次数预估，确认了才开始烧钱。
        </p>
        <div class="replica-row">
            <input type="file" id="replica-file" accept="video/*" class="replica-file-input">
            <label class="replica-inline-field">抽帧密度
                ${replicaFpsSelect('replica-upload-fps', REPLICA_DEFAULT_FPS)}
            </label>
            <label class="replica-inline-field">跳变灵敏度
                ${replicaThresholdSelect('replica-upload-threshold', REPLICA_DEFAULT_THRESHOLD)}
            </label>
            <button type="button" id="replica-upload-btn" class="action-btn primary-btn">上传并抽帧</button>
        </div>
        <div id="replica-upload-status" class="replica-status"></div>
    </div>`;
}

function replicaRenderJobList() {
    if (!replicaJobs.length) return '';

    let list = replicaJobs;
    const q = (replicaJobListSearchQuery || '').trim().toLowerCase();
    if (q) {
        list = list.filter(j => {
            const t = (j.title || '').toLowerCase();
            const vn = (j.video_name || '').toLowerCase();
            const id = (j.job_id || '').toLowerCase();
            const st = (replicaStageLabel(j) || '').toLowerCase();
            return t.includes(q) || vn.includes(q) || id.includes(q) || st.includes(q);
        });
    }

    // 分类到 4 个 Attention 分组
    const groups = {
        waiting_you: [],
        running: [],
        done: [],
        stalled: [],
    };

    for (const job of list) {
        let att = job.attention;
        if (!att) {
            if (job.active_task_id) att = 'running';
            else if (['confirm_cost', 'review_beats', 'audit_failed', 'compose_failed', 'mutate_failed'].includes(job.stage)) att = 'waiting_you';
            else if (job.stage === 'completed') att = 'done';
            else if (job.stage === 'archived' || job.archived) att = 'stalled';
            else att = 'stalled';
        }
        if (att === 'archived') att = 'stalled';
        // 归一化后的分组写回：行上的左侧竖线（待处理/运行中）读的就是这一份，
        // 不能读原始的 job.attention——那一份可能缺席或还是 'archived'。
        job.attention = att;
        if (groups[att]) {
            groups[att].push(job);
        } else {
            groups.stalled.push(job);
        }
    }

    const waitingCount = groups.waiting_you.length;

    function renderJobRow(job, isChild = false, currentGroupList = null) {
        const isCurrent = replicaState && replicaState.job_id === job.job_id;
        const isArchived = job.stage === 'archived' || !!job.archived;
        const displayName = job.title || job.video_name || job.job_id;
        const stageText = replicaStageLabel(job);

        // 成本卡点醒目标签
        let costBadge = '';
        if (job.stage === 'confirm_cost') {
            const est = (job.cost_estimate && job.cost_estimate.full) || {};
            const frames = est.frame_count || '';
            const calls = est.batch_count || '';
            costBadge = `<span class="replica-chip replica-chip-cost" title="待确认反推成本">💰 待确认${frames ? `: ${frames} 帧` : ''}${calls ? ` / 约 ${calls} 次调用` : ''}</span>`;
        }

        // 血缘变体展开按钮（如果是母本且在系统中有子变体）
        let lineageToggleHtml = '';
        const childJobs = replicaJobs.filter(v => v.job_id !== job.job_id && (v.variant_of === job.job_id || v.parent_baseline_id === job.job_id));
        const hasVariants = childJobs.length > 0;
        const isFolded = replicaVariantFoldState[job.job_id] !== false; // 默认折叠

        if (hasVariants && !isChild) {
            lineageToggleHtml = `
                <button type="button" class="replica-job-variant-toggle" data-toggle-variants="${escapeHtmlReplica(job.job_id)}"
                        title="${isFolded ? '展开二创变体' : '收起二创变体'}">
                    👑 ${childJobs.length} 个变体 ${isFolded ? '▼' : '▲'}
                </button>
            `;
        }

        const rowHtml = `
        <div class="replica-job-row ${isCurrent ? 'active' : ''} ${isChild ? 'replica-job-variant-row' : ''}"
             data-att="${escapeHtmlReplica(job.attention || '')}">
            <button type="button" class="replica-job-open" data-job="${escapeHtmlReplica(job.job_id)}"
                    title="${escapeHtmlReplica(displayName)}">
                <span class="replica-job-icon">${job.variant_of ? '🧬' : '🎬'}</span>
                <span class="replica-job-name">
                    <strong>${escapeHtmlReplica(displayName)}</strong>${
                        job.title_locked ? '<span class="replica-job-lock" title="名称已锁定">🔒</span>' : ''}
                </span>
                <span class="replica-job-meta">
                    ${job.error ? '<span class="replica-chip replica-chip-error">出错</span>' : ''}
                    ${costBadge}
                    ${isArchived ? '<span class="replica-chip">📦 已归档</span>' : ''}
                    ${job.beat_count ? `<span class="replica-job-beats">${job.beat_count} 拍</span>` : ''}
                    ${/* 跑着的任务：把它当前在干什么摆到行上。列表行此前只有一个静态 stage
                          chip，而 SSE 只连当前打开的那一条——同时跑两条时，另一条在列表里
                          看不出任何进展，只能靠反复点进去确认。 */''}
                    ${job.active_task_id ? `<span class="replica-job-live" title="${
                        escapeHtmlReplica(job.active_message || '正在后台运行')}">
                        <span class="replica-job-live-dot"></span>${
                        escapeHtmlReplica(job.active_message || '正在后台运行')}</span>`
                    : `<span class="replica-job-stage ${job.stage === 'completed' ? 'is-done' : ''}">${
                        escapeHtmlReplica(stageText)}</span>`}
                </span>
            </button>
            ${/* 变体展开钮不进 ops：它是「这条底下还挂着东西」的指示，不是低频操作，
                  藏进悬停态就等于没有。 */''}
            ${lineageToggleHtml}
            <span class="replica-job-ops">
                <button type="button" class="replica-mini-btn" data-rename="${escapeHtmlReplica(job.job_id)}" title="重命名此任务">改名</button>
                ${!isArchived ? `<button type="button" class="replica-mini-btn" data-archive="${escapeHtmlReplica(job.job_id)}" title="瘦身归档（删除视频与高清帧，释放磁盘空间）">归档</button>` : ''}
                <button type="button" class="replica-mini-btn replica-mini-btn-danger" data-delete="${escapeHtmlReplica(job.job_id)}" title="删除这个任务及其数据">删除</button>
            </span>
        </div>`;

        // 如果是展开状态，渲染挂载的子变体
        let childrenHtml = '';
        if (hasVariants && !isChild && !isFolded) {
            childrenHtml = childJobs.map(cj => renderJobRow(cj, true)).join('');
        }

        return rowHtml + childrenHtml;
    }

    function renderGroupSection(title, icon, jobList, extraBadge = '') {
        if (!jobList.length) return '';
        // 顶层任务判断：
        // 1. 如果搜索 query 存在，所有命中的 job 都显示
        // 2. 如果 parent 也在当前 jobList 里，由 parent 嵌套渲染 child
        // 3. 如果 parent 不在当前 jobList 里（例如 parent 在 done，而 child 在 waiting_you），child 必须作为独立项在当前组直接展示，绝不能被吞掉！
        const topLevelJobs = q ? jobList : jobList.filter(j => {
            const pid = j.variant_of || j.parent_baseline_id;
            if (!pid) return true;
            const parentInThisGroup = jobList.some(pj => pj.job_id === pid);
            return !parentInThisGroup;
        });

        if (!topLevelJobs.length) return '';

        return `
        <div class="replica-job-group">
            <div class="replica-job-group-title">
                <span class="replica-job-group-name">${icon} ${title}</span>
                <span class="replica-job-group-count">${jobList.length}</span>
                ${extraBadge}
            </div>
            ${topLevelJobs.map(j => renderJobRow(j, false, jobList)).join('')}
        </div>`;
    }

    const waitingHtml = renderGroupSection('待你处理', '🔥', groups.waiting_you);
    const runningHtml = renderGroupSection('运行中', '⚡', groups.running);
    const doneHtml = renderGroupSection('已完成', '✅', groups.done);
    const stalledHtml = renderGroupSection('已搁置 / 已归档', '📦', groups.stalled);

    const emptyNotice = (!waitingHtml && !runningHtml && !doneHtml && !stalledHtml)
        ? `<div class="replica-hint" style="text-align:center; padding:12px 0;">未找到匹配的任务</div>` : '';

    return `
    <details class="replica-card" id="replica-sec-jobs" ${replicaJobListExpanded ? 'open' : ''}>
        <summary class="replica-card-title replica-jobs-summary">
            已有任务 <span class="replica-chip">${replicaJobs.length}</span>
            ${waitingCount ? `<span class="replica-chip replica-chip-urgent">🔥 ${waitingCount} 条待处理</span>` : ''}
        </summary>
        <div class="replica-job-list">
            <div class="replica-job-list-header">
                <input type="text" id="replica-job-search-input" class="replica-job-search"
                       placeholder="🔍 搜索任务标题、ID、状态..."
                       value="${escapeHtmlReplica(replicaJobListSearchQuery)}">
            </div>
            ${/* 搜索框在滚动区之外：任务多的时候它不能跟着列表滚走。 */''}
            <div class="replica-job-scroll">
                ${waitingHtml}
                ${runningHtml}
                ${doneHtml}
                ${stalledHtml}
                ${emptyNotice}
            </div>
        </div>
    </details>`;
}

// 秒 → 人话。分钟级以上不再显示秒：反推跑了「23 分」比「1387 秒」有用得多。
function replicaFormatDuration(sec) {
    const n = Math.round(Number(sec) || 0);
    if (n < 60) return `${n} 秒`;
    if (n < 3600) return `${Math.round(n / 60)} 分`;
    return `${(n / 3600).toFixed(1)} 小时`;
}

function replicaRenderPhases(state) {
    // 页面上原先编着 ①②③④⑤，后台却有十二个 stage —— chip 显示「聚类节拍」时用户
    // 在页面上找不到任何一块对应它。这条阶梯把两者对齐：四个阶段，各自对应页面上
    // 真实存在的一块区域。
    const at = replicaPhaseIndex(state.stage);
    const failed = state.stage === 'audit_failed' || !!state.error;
    const durations = state.stage_durations || {};
    return `<ol class="replica-phases">${REPLICA_PHASES.map((p, i) => {
        const cls = i < at ? 'done' : (i === at ? (failed ? 'failed' : 'current') : 'todo');
        // 已经走到过的阶段才能点着跳回去；没到的阶段页面上还没有那一块。
        const canJump = i <= at;
        // 这一大阶段实际花了多久（它底下所有 stage 的累计，见 stage_durations）。
        // 摆出来是为了回答「时间到底花在哪」——在此之前这个问题在页面上无解，
        // 只能去翻 server.log 对时间戳。
        const spent = p.stages.reduce((sum, st) => sum + (Number(durations[st]) || 0), 0);
        const spentText = spent > 0 ? replicaFormatDuration(spent) : '';
        const title = (canJump ? `点击直达「${p.label}」区段` : '尚未进入该阶段')
            + (spentText ? `　·　本阶段累计耗时 ${spentText}` : '');
        return `<li class="replica-phase ${cls} ${canJump ? 'is-jumpable' : 'is-locked'}"
                    data-phase="${p.key}" title="${escapeHtmlReplica(title)}"><span class="replica-phase-dot">${i + 1}</span>
            <span class="replica-phase-label">${p.label}</span>${
                spentText ? `<span class="replica-phase-dur">${spentText}</span>` : ''}</li>`;
    }).join('')}</ol>`;
}

function replicaRenderLineageNav(state) {
    const isVariant = !!state.variant_of || !!state.parent_baseline_id;
    const parentId = state.parent_baseline_id || state.variant_of;
    const baselineId = parentId || state.job_id;

    // 汇总当前母本下的所有有效变体
    const variantIdSet = new Set(state.lineage_variants || []);
    if (typeof replicaJobs !== 'undefined' && Array.isArray(replicaJobs)) {
        replicaJobs.forEach(j => {
            if (j.job_id && j.job_id !== baselineId && (j.variant_of === baselineId || j.parent_baseline_id === baselineId)) {
                variantIdSet.add(j.job_id);
            }
        });
    }
    variantIdSet.delete(baselineId);
    const validVariants = Array.from(variantIdSet).filter(vid => {
        if (typeof replicaJobs === 'undefined' || !replicaJobs.length) return true;
        return replicaJobs.some(j => j.job_id === vid);
    });

    if (!isVariant && !validVariants.length) return '';

    // 获取母本显示名称
    const parentJob = (typeof replicaJobs !== 'undefined' && Array.isArray(replicaJobs)) ? replicaJobs.find(j => j.job_id === baselineId) : null;
    const parentName = (parentJob && (parentJob.title || parentJob.video_name)) || baselineId;

    return `
    <div class="replica-lineage-nav">
        <div class="replica-lineage-breadcrumb">
            ${isVariant ? `
                <span class="replica-lineage-crumb">
                    <button type="button" class="replica-lineage-link" data-open-job="${escapeHtmlReplica(baselineId)}"
                            title="返回 1:1 黄金母本：${escapeHtmlReplica(parentName)}">
                        👑 黄金母本 <strong>${escapeHtmlReplica(parentName.length > 20 ? parentName.slice(0, 20) + '…' : parentName)}</strong>
                    </button>
                </span>
                <span class="replica-lineage-sep">➔</span>
                <span class="replica-lineage-crumb is-current">
                    🧬 当前二创变体 <code>${escapeHtmlReplica(state.job_id)}</code>
                </span>
            ` : `
                <span class="replica-lineage-crumb is-current">
                    👑 黄金母本 <code>${escapeHtmlReplica(state.job_id)}</code> (1:1 Ground-Truth Baseline)
                </span>
            `}
        </div>
        ${validVariants.length ? `
        <div class="replica-lineage-variants">
            <span class="replica-lineage-label">二创变体 (${validVariants.length})：</span>
            ${validVariants.map(vid => {
                const vjob = (typeof replicaJobs !== 'undefined' && Array.isArray(replicaJobs)) ? replicaJobs.find(j => j.job_id === vid) : null;
                const vtitle = (vjob && (vjob.title || vjob.job_id)) || vid;
                let shortName = vtitle;
                if (vtitle.includes(' · ')) {
                    shortName = vtitle.split(' · ').pop().trim();
                } else if (shortName.length > 18) {
                    shortName = shortName.slice(0, 18) + '…';
                }
                const isCurrent = state.job_id === vid;
                return `
                <button type="button" class="replica-chip replica-lineage-chip ${isCurrent ? 'is-active' : ''}"
                        data-open-job="${escapeHtmlReplica(vid)}"
                        title="打开变体任务：${escapeHtmlReplica(vtitle)} (${escapeHtmlReplica(replicaStageLabel(vjob || vid))})">
                    🧬 ${escapeHtmlReplica(shortName)}
                </button>`;
            }).join('')}
        </div>` : ''}
    </div>`;
}

// 双轨拼图对比。右轨此前是**同一张母本拼图**加一个 CSS 滤镜冒充的变体
// （`filter: saturate(1.2) contrast(1.05)`），而标题写着「肉眼 3 秒判定漂移」——
// 那样它永远判不出漂移，比没有对比更糟：用户会以为自己已经查过了。
//
// 变体在这个阶段本来就没有画面可比：`replicaFrameBase` 把变体的帧目录指回源 job
// （变体自己不抽帧，它派生的是提示词而不是素材），要等它走完分步渲染才有自己的成片。
// 所以右轨给的是实话 + 去处，不再造一张假图。
function replicaRenderDualTrackComparator(state) {
    if (!replicaComparatorOpen) return '';
    const baseCollage = replicaCollageUrl(state);
    const isVariant = !!(state.variant_of || state.parent_baseline_id);

    return `
    <div class="replica-comparator-section glass-panel" id="replica-sec-comparator">
        <div class="replica-comparator-header">
            <span class="replica-card-title">👁 双轨 5 列拼图横向对比快检 (Dual-Track 5-Column Comparator)</span>
            <button type="button" class="replica-mini-btn" id="replica-close-comparator-btn">✕ 关闭对比</button>
        </div>
        <p class="replica-hint">
            左轨是原片的 5 列基准拼图（单帧宽 240px），用来核对光影色调、材质质感与空间结构。
            右轨要等变体真的渲染出来才有画面——变体自己不抽帧，它派生的是提示词。
        </p>
        <div class="replica-comparator-tracks">
            <div class="replica-comparator-track">
                <div class="replica-track-tag">◀ 黄金母本视觉基准 (Gold Baseline 5-Column)</div>
                ${baseCollage ? `
                    <img class="replica-comparator-img" src="${baseCollage}" alt="母本 5 列原片拼图"
                         onclick="replicaOpenLightbox([{url: '${baseCollage}', caption: '1:1 母本 5 列拼图'}], 0)">
                ` : '<div class="replica-hint">母本拼图不可用</div>'}
            </div>
            <div class="replica-comparator-track">
                <div class="replica-track-tag">▶ 变体成片 (Rendered Variant)</div>
                <div class="replica-comparator-empty">
                    <div class="replica-empty-icon">🎞</div>
                    <div class="replica-empty-desc">
                        ${isVariant
                            ? '这条变体还没有自己的成片。按「存入项目并打开激发结果」渲染完成后，在项目工作台里对着这张基准图逐帧比对。'
                            : '还没有派生出变体。右栏填好四轴、生成变体提示词包并渲染之后，成片会出现在项目工作台。'}
                    </div>
                </div>
            </div>
        </div>
    </div>`;
}

function replicaRenderTrendRefsDrawer() {
    const storedDirection = (typeof readStoredIdeationDirection === 'function')
        ? readStoredIdeationDirection()
        : { query: '', urls: '' };
    const selCount = (typeof getSelectedTrendRefIds === 'function') ? getSelectedTrendRefIds().length : 0;
    const noteText = selCount > 0 ? `已选 ${selCount} 条（发散优先参考）` : '未勾选：发散自动取材';

    return `
    <div class="control-group spark-drawer replica-trend-refs-drawer ${replicaRefsDrawerOpen ? 'drawer-open' : ''}" id="replica-spark-drawer-refs" data-drawer="replica-refs">
        <div class="spark-drawer-head">
            <button type="button" class="spark-drawer-toggle" id="replica-trend-refs-toggle" aria-expanded="${replicaRefsDrawerOpen ? 'true' : 'false'}" aria-controls="replica-spark-drawer-refs-body">
                <svg class="spark-drawer-chevron" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
                <span class="group-title">🌐 联网参考案例库</span>
                <span class="trend-refs-selected-note spark-drawer-note" id="replica-trend-refs-selected-note">${escapeHtmlReplica(noteText)}</span>
            </button>
            <div class="trend-refs-header-actions">
                <button type="button" id="replica-trend-refs-direction-toggle" class="preset-header-action" title="自定义联网搜索方向与参考网址">🎯 搜索方向</button>
                <button type="button" id="replica-trend-refs-search-btn" class="preset-header-action">🔍 搜一批新参考</button>
            </div>
        </div>
        <div class="spark-drawer-body" id="replica-spark-drawer-refs-body">
            <div class="trend-refs-direction-panel" id="replica-trend-refs-direction-panel" ${replicaRefsDirectionOpen ? '' : 'hidden'}>
                <label for="replica-trend-refs-search-query">搜索方向（留空用默认：最新爆款延时摄影改造视频）</label>
                <input type="text" id="replica-trend-refs-search-query" placeholder="例如：AI 宠物拟人化改造视频 趋势" value="${escapeHtmlReplica(storedDirection.query || '')}">
                <label for="replica-trend-refs-trend-urls">参考网址（每行一个，最多取前 5 个，可选）</label>
                <textarea id="replica-trend-refs-trend-urls" rows="2" placeholder="https://...">${escapeHtmlReplica(storedDirection.urls || '')}</textarea>
                <p class="trend-refs-direction-hint">设置与激发维度同步，点「搜一批新参考」立即生效，助您精准引入目标爆款领域的视觉要点。</p>
            </div>
            <p class="trend-refs-hint">勾选参考后，AI 智能发散将深度结合所选已验证爆款案例；未勾选时发散将自动从案例库或联网趋势中取材。</p>
            <div class="trend-refs-toolbar">
                <input type="text" id="replica-trend-refs-filter" class="trend-refs-filter replica-input" placeholder="筛选案例库…" value="${escapeHtmlReplica(replicaRefsFilterQuery)}">
            </div>
            <div class="trend-refs-list" id="replica-trend-refs-list">
                <div class="trend-refs-empty">正在载入联网参考案例库...</div>
            </div>
            <div class="trend-refs-archive-bar">
                <span class="trend-refs-cap-note" id="replica-trend-refs-cap-note"></span>
                <button type="button" id="replica-trend-refs-manage-open-btn" class="trend-refs-archive-toggle">🗂️ 管理全部</button>
                <button type="button" id="replica-trend-refs-archive-toggle" class="trend-refs-archive-toggle" hidden>查看归档</button>
            </div>
            <div class="trend-refs-list trend-refs-archive-list" id="replica-trend-refs-archive-list" hidden></div>
        </div>
    </div>`;
}

function replicaDiagnoseClientCompatibility(state, axes, activeIdea, brief) {
    if (activeIdea && activeIdea.compatibility) {
        return activeIdea.compatibility;
    }

    const env = (axes.environment || '').toLowerCase();
    const mat = (axes.material || '').toLowerCase();
    const func = (axes.function || '').toLowerCase();
    const hero = (axes.hero_reveal || '').toLowerCase();
    const allText = `${brief || ''} ${env} ${mat} ${func} ${hero} ${(activeIdea && activeIdea.name) || ''} ${(activeIdea && activeIdea.hook) || ''}`;

    let score = 100;
    let level = 'compatible';
    let conflictAlert = '';

    const narrativeDims = [
        { key: 'hook_crisis', label: '黄金 3 秒痛点钩子', icon: '🪝', status: 'pass', detail: '继承母本开局完全毁坏/废墟困境钩子，留存率有保障。' },
        { key: 'character_emotion', label: '常驻角色情感羁绊', icon: '❤️', status: 'pass', detail: '穷困夫妇/受助生命看图纸燃起希望，情感闭环完整。' },
        { key: 'god_hand_wonder', label: '神来之手与降维奇观', icon: '🖐️', status: 'pass', detail: '巨人工匠之手（God Hand）如神迹降临微缩世界，治愈奇观感强烈。' },
        { key: 'contrast_reward', label: '极致前后蜕变反差', icon: '💎', status: 'pass', detail: '从 0 分破烂废墟到 100 分奢华庄园，多巴胺终局爽点拉满。' },
    ];

    const physicalDims = [
        { key: 'spatial_force', label: '空间支撑与受力范式', icon: '🏗️', status: 'pass', detail: '基底载体同构，受力逻辑与建造工序契合。' },
        { key: 'material_phase', label: '材料加工与物理相态', icon: '🪵', status: 'pass', detail: '实体木石钢构装配相态，切削与微雕工序通用。' },
        { key: 'scale_envelope', label: '三维公制尺度与包络', icon: '📐', status: 'pass', detail: '微缩微距/紧凑尺度，机位透视与比例稳定。' },
        { key: 'asmr_acoustic', label: 'ASMR 声画沉浸节拍', icon: '🎧', status: 'pass', detail: '微观撕纸、微型木石拼搭原声与 60% 物理音量动态映射。' },
    ];

    // 空间支撑冲突
    if (['悬崖', '悬空', '挑空', '树屋', '树冠', '太空', '空间站', 'cliff', 'treehouse', 'space'].some(k => allText.includes(k))) {
        physicalDims[0].status = 'fail';
        physicalDims[0].detail = '悬崖/树冠/失重与母本基底受力冲突，无法执行常规建造工序。';
        score -= 45;
        conflictAlert = '空间载体硬冲突：悬崖/高空/太空无法硬套母本基础工序，建议升级为全新黄金母本！';
    }

    // 材料相态冲突
    if (['冰雕', '纯冰', '熔岩琉璃', '3d打印', '增材'].some(k => allText.includes(k))) {
        physicalDims[1].status = 'fail';
        physicalDims[1].detail = '特殊物理相态（冰雕/熔岩/3D打印）与母本实体木石装配根本冲突。';
        score -= 40;
        if (!conflictAlert) conflictAlert = '材料相态冲突：冰雕/熔岩/3D打印无法硬套微缩装配工艺！建议建立专属母本。';
    }

    // 尺度严重膨胀冲突
    if (['大教堂', '大礼堂', '巨型机库', '万平', '万人', 'cathedral', 'grand hall'].some(k => allText.includes(k))) {
        physicalDims[2].status = 'fail';
        physicalDims[2].detail = '空间体量严重膨胀，硬套微缩/紧凑镜头会导致 Cavernous 保龄球道畸变。';
        score -= 40;
        if (!conflictAlert) conflictAlert = '空间尺度严重膨胀：硬套会导致画面被拉伸为深长管道！';
    }

    // 叙事灵魂检查（针对微缩改造题材）
    const isMiniatureTask = (state && (state.title || state.video_name || '')).includes('微缩') || allText.includes('微缩');
    if (isMiniatureTask) {
        if (['真人', '地下掩体', '工人亲自', '1.78m', '成年工人'].some(k => allText.includes(k)) && !['夫妇', '小人', '人偶'].some(k => allText.includes(k))) {
            narrativeDims[1].status = 'fail';
            narrativeDims[1].detail = '丢失穷困夫妇角色线：微缩爆款的核心是受助夫妇的情感共鸣，误套为普通真人工人施工将导致“没血没肉”！';
            narrativeDims[2].status = 'fail';
            narrativeDims[2].detail = '丢失神来之手：微缩沙盘被降级为成人平视，破坏了 God Hand 降维神迹奇观感。';
            score -= 45;
            if (!conflictAlert) conflictAlert = '🎭 叙事灵魂与情绪断层预警：母本核心是“神来之手为穷困夫妇看图纸造豪宅”，二创若丢掉夫妇情感弧线与微缩神之手，将沦为无灵魂的冰冷手工！';
        }
    }

    score = Math.max(0, Math.min(100, score));
    if (score < 60) level = 'incompatible';
    else if (score < 90) level = 'risky';
    else level = 'compatible';

    const verdictTitle = level === 'compatible'
        ? '✅ 允许 100% 骨架硬冻结正交派生 (Safe)'
        : (level === 'risky' ? '⚠️ 需局部工序与叙事适配 (Risky)' : '🚫 严禁表面硬套（物理冲突或叙事灵魂丢失）');

    return {
        compatibility_level: level,
        compatibility_score: score,
        verdict_title: verdictTitle,
        can_inherit_skeleton: level !== 'incompatible',
        summary: level === 'compatible'
            ? '母本与二创在 TikTok 叙事弧线、角色情感羁绊、空间载体与工艺拓扑上 100% 同构，兼具物理真实与爆款灵魂。'
            : (level === 'risky' ? '检测到轻微工艺或叙事跨度，系统将自动适配工具、ASMR 与角色情感弧线。' : conflictAlert),
        conflict_alert: conflictAlert,
        dimensions: [...narrativeDims, ...physicalDims],
        narrative_dimensions: narrativeDims,
        physical_dimensions: physicalDims,
        action_recommendation: {
            action: level === 'incompatible' ? 'create_new_baseline' : (level === 'risky' ? 'adapt_and_mutate' : 'mutate_orthogonal'),
            button_label: level === 'incompatible' ? '👑 升级为全新黄金母本 / 补全叙事灵魂' : '⚡ 一键生成二创变体提示词包 (Variant)',
            explanation: level === 'incompatible' ? '物理规律冲突或严重丢失了母本的核心叙事灵魂（如穷困夫妇看图纸、神之手介入或破败开局钩子）。' : '拓扑同构且叙事灵魂闭环，允许受控发散。'
        }
    };
}

function replicaRenderDecisionMatrix(state, axes, activeIdea, brief) {
    const diag = replicaDiagnoseClientCompatibility(state, axes, activeIdea, brief);
    const lvl = diag.compatibility_level || 'compatible';
    const score = diag.compatibility_score ?? 100;

    const narrativeDims = diag.narrative_dimensions || (diag.dimensions || []).filter(d => ['hook_crisis', 'character_emotion', 'god_hand_wonder', 'contrast_reward'].includes(d.key));
    const physicalDims = diag.physical_dimensions || (diag.dimensions || []).filter(d => !['hook_crisis', 'character_emotion', 'god_hand_wonder', 'contrast_reward'].includes(d.key));

    return `
    <div class="replica-decision-matrix-card is-${lvl}" id="replica-decision-framework-box">
        <div class="replica-decision-header">
            <div class="replica-decision-title-group">
                <span>🛡️ 重构判断矩阵 (TikTok 深度叙事与工艺双轨诊断)</span>
                <span class="replica-decision-badge is-${lvl}">
                    ${escapeHtmlReplica(diag.verdict_title || '')} (${score}分)
                </span>
            </div>
            <span class="replica-hint">${diag.can_inherit_skeleton ? '✓ 骨架与叙事可复用' : '✗ 需独立建母本/补全灵魂'}</span>
        </div>
        <div class="replica-decision-summary">
            ${escapeHtmlReplica(diag.summary || '')}
        </div>
        ${diag.conflict_alert ? `
            <div class="replica-decision-conflict-alert">
                <b>⚠️ 爆款灵魂与硬套红线预警：</b>${escapeHtmlReplica(diag.conflict_alert)}
            </div>
        ` : ''}

        <!-- 🎭 轨一：TikTok 爆款叙事与情绪价值弧线 -->
        <div class="replica-decision-track-section">
            <div class="replica-decision-track-title">🎭 TikTok 爆款叙事与情绪价值弧线 (Narrative & Emotional Soul)</div>
            <div class="replica-decision-dims-grid">
                ${narrativeDims.map(dim => `
                    <div class="replica-decision-dim-item status-${dim.status || 'pass'}">
                        <div class="replica-decision-dim-top">
                            <span>${dim.icon || '🎭'} ${escapeHtmlReplica(dim.label)}</span>
                            <span class="replica-decision-dim-status-icon">
                                ${dim.status === 'pass' ? '🟢 契合' : (dim.status === 'warning' ? '🟡 需适配' : '🔴 缺失/断层')}
                            </span>
                        </div>
                        <div class="replica-decision-dim-detail">${escapeHtmlReplica(dim.detail || '')}</div>
                    </div>
                `).join('')}
            </div>
        </div>

        <!-- 🏗️ 轨二：全域物理工程与真实工艺拓扑 -->
        <div class="replica-decision-track-section" style="margin-top: 10px;">
            <div class="replica-decision-track-title">🏗️ 全域物理工程与真实工艺拓扑 (Physical Craft & Topology)</div>
            <div class="replica-decision-dims-grid">
                ${physicalDims.map(dim => `
                    <div class="replica-decision-dim-item status-${dim.status || 'pass'}">
                        <div class="replica-decision-dim-top">
                            <span>${dim.icon || '📌'} ${escapeHtmlReplica(dim.label)}</span>
                            <span class="replica-decision-dim-status-icon">
                                ${dim.status === 'pass' ? '🟢 契合' : (dim.status === 'warning' ? '🟡 需适配' : '🔴 冲突')}
                            </span>
                        </div>
                        <div class="replica-decision-dim-detail">${escapeHtmlReplica(dim.detail || '')}</div>
                    </div>
                `).join('')}
            </div>
        </div>
    </div>
    `;
}

function replicaRenderAiDivergingState() {
    const steps = [
        { num: 1, title: '工序拓扑解析', desc: '解构母本建造因果链' },
        { num: 2, title: '联网趋势注入', desc: '汲取爆款参考精髓' },
        { num: 3, title: '四轴正交发散', desc: '重构 4 组完全正交方案' },
        { num: 4, title: '相容性诊断', desc: '物理与叙事灵魂校验' },
    ];
    return `
        <div class="replica-diverge-progress-card">
            <div class="replica-diverge-progress-head">
                <div class="replica-diverge-pulse-icon">✨</div>
                <div class="replica-diverge-progress-info">
                    <div class="replica-diverge-progress-title">
                        <span>AI 正在正交发散 4 组写实建造二创方案</span>
                        <span class="replica-chip replica-chip-diverging">⏳ 智能发散中</span>
                    </div>
                    <div class="replica-diverge-progress-status" id="replica-diverge-status-text">
                        ${escapeHtmlReplica(replicaDivergeStatusText)}
                    </div>
                </div>
            </div>
            <div class="replica-diverge-steps-row">
                ${steps.map(s => {
                    const isDone = replicaDivergeStep > s.num;
                    const isCurrent = replicaDivergeStep === s.num;
                    const cls = isDone ? 'step-done' : (isCurrent ? 'step-current' : 'step-pending');
                    const icon = isDone ? '✓' : s.num;
                    return `
                        <div class="replica-diverge-step-item ${cls}">
                            <div class="replica-diverge-step-bubble">${icon}</div>
                            <div class="replica-diverge-step-texts">
                                <div class="replica-diverge-step-title">${s.title}</div>
                                <div class="replica-diverge-step-desc">${s.desc}</div>
                            </div>
                        </div>
                    `;
                }).join('')}
            </div>
            <div class="replica-diverge-progress-bar-wrap">
                <div class="replica-diverge-progress-bar-fill" style="width: ${Math.min(100, Math.max(10, replicaDivergeStep * 25))}%"></div>
            </div>
        </div>
        <div class="replica-ai-ideas-grid replica-ai-ideas-skeleton-grid">
            <div class="replica-ai-idea-skeleton"><div class="skeleton-shimmer"></div></div>
            <div class="replica-ai-idea-skeleton"><div class="skeleton-shimmer"></div></div>
            <div class="replica-ai-idea-skeleton"><div class="skeleton-shimmer"></div></div>
            <div class="replica-ai-idea-skeleton"><div class="skeleton-shimmer"></div></div>
        </div>
    `;
}

// 二创派生看板只在"这一趟真的是在派生变体"时才该露脸。渲染期与 SSE 刷新期必须用
// 同一条判据：replicaProgressPaint 每来一条事件就跑一次，它要是不判，渲染期算出来的
// display:none 立刻就被它改回 block——于是抽帧、聚拍、autofix 跑着的时候，二创面板里
// 会挂出一条跟二创毫不相干的进度。
function replicaIsMutateRunning() {
    return !!(replicaSSE && replicaProgress && (
        replicaProgress.actionLabel === '⚡ 正交二创变体派生' ||
        replicaProgress.actionLabel === '🧬 派生二创变体' ||
        replicaProgress.stage === 'mutate_beats' ||
        (replicaProgress.actionLabel && replicaProgress.actionLabel.includes('变体'))
    ));
}

function replicaRenderMutatorProgress() {
    const isMutateRunning = replicaIsMutateRunning();
    const stage = (replicaProgress && replicaProgress.actionLabel) || '⚡ 正交二创变体派生';
    const label = (replicaProgress && replicaProgress.label) || '正在派生二创变体...';
    const pct = replicaProgress ? Math.round(replicaProgress.percent) : 45;
    return `
    <div id="replica-mutator-progress" class="replica-mutator-live-progress" style="display:${isMutateRunning ? 'block' : 'none'};">
        <div class="replica-mutator-progress-head">
            <span class="replica-chip" id="replica-mutator-progress-stage">${escapeHtmlReplica(stage)}</span>
            <span id="replica-mutator-progress-label" class="replica-mutator-progress-label">${escapeHtmlReplica(label)}</span>
            <span class="replica-progress-percent" id="replica-mutator-progress-percent">${pct}%</span>
        </div>
        <div class="replica-progress-track">
            <div class="replica-progress-fill" id="replica-mutator-progress-fill" style="width:${Math.max(2, Math.min(100, pct))}%;"></div>
        </div>
    </div>`;
}

function replicaRenderAiIdeas(state) {
    if (replicaDiverging) {
        return replicaRenderAiDivergingState();
    }
    const ideas = (state && state.ai_diverged_ideas) || replicaAiIdeas || [];
    if (!ideas || !ideas.length) {
        return `
            <div class="replica-ai-ideas-empty">
                <div class="replica-empty-icon">💡</div>
                <div class="replica-empty-desc">
                    点击上方<b>「✨ AI 智能发散创意」</b>，AI 将结合选中的<b>联网参考案例</b>与母本工序骨架，自动派生 4 组完全正交的爆款二创脑洞；您也可直接在下方自由填写四轴。
                </div>
            </div>`;
    }

    return `
        <div class="replica-ai-ideas-header">
            <span class="replica-preset-label">🎯 AI 发散脑洞方案（点击一键载入四轴）：</span>
            <button type="button" id="replica-ai-diverge-refresh-btn" class="action-btn text-btn replica-mini-btn" title="重新换一批灵感">
                🎲 换一批灵感
            </button>
        </div>
        <div class="replica-ai-ideas-grid">
            ${ideas.map((idea, idx) => {
                const isActive = replicaActiveIdeaIndex === idx;
                const compat = idea.compatibility;
                const compatLvl = compat ? compat.compatibility_level : 'compatible';
                const compatScore = compat ? compat.compatibility_score : 100;
                const compatText = compatLvl === 'compatible' ? '🛡️ 100% 同构' : (compatLvl === 'risky' ? '⚠️ 需适配' : '🚫 物理冲突');

                return `
                    <div class="replica-ai-idea-card ${isActive ? 'active' : ''}" data-idea-idx="${idx}" title="${escapeHtmlReplica(idea.hook || '')}">
                        <div class="replica-ai-idea-top">
                            <span class="replica-ai-idea-name">${escapeHtmlReplica(idea.icon || '✨')} ${escapeHtmlReplica(idea.name || `创意方案 ${idx + 1}`)}</span>
                            <span class="replica-ai-idea-compat-badge is-${compatLvl}" title="重构判断矩阵评估：${compatScore}分">${compatText}</span>
                            ${isActive ? '<span class="replica-ai-idea-badge">已选用</span>' : ''}
                        </div>
                        <div class="replica-ai-idea-hook">${escapeHtmlReplica(idea.hook || '')}</div>
                        ${idea.trend_ref ? `<div class="replica-ai-idea-trend" title="${escapeHtmlReplica(idea.trend_ref)}">🌐 趋势借鉴: ${escapeHtmlReplica(idea.trend_ref)}</div>` : ''}
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function replicaRenderDualWorkbench(state) {
    if (state.stage === 'ingest' || state.stage === 'extract' || state.stage === 'confirm_cost') {
        return '';
    }

    const isLocked = !!state.is_locked_baseline;
    const beats = (state.beats && (state.beats.beats || [])) || [];
    const beatsCount = beats.length || state.beat_count || 0;
    const ov = state.overview || {};

    // 初始化 ideas
    if (state.ai_diverged_ideas && state.ai_diverged_ideas.length && (!replicaAiIdeas || !replicaAiIdeas.length)) {
        replicaAiIdeas = state.ai_diverged_ideas;
    }
    const ideas = (state && state.ai_diverged_ideas) || replicaAiIdeas || [];
    const activeIdea = (replicaActiveIdeaIndex >= 0 && ideas[replicaActiveIdeaIndex]) ? ideas[replicaActiveIdeaIndex] : null;

    const axes = (state.mutation_config && state.mutation_config.axes)
        || (activeIdea && activeIdea.axes)
        || { environment: '', material: '', function: '', hero_reveal: '' };

    // 二创栏的门槛是「有没有节拍」，没有骨架就没有二创
    const canMutate = beatsCount > 0;
    if (!canMutate) {
        return '';
    }

    return `
    <!-- ⚡ 全宽 AI 正交发散与二创控制台 (AI Orthogonal Mutator Studio) -->
    <div class="replica-pane-mutator glass-panel" id="replica-sec-variant">
        <div class="replica-pane-header">
            <div class="replica-pane-title-group">
                <span class="replica-pane-title">⚡ AI 正交发散刺激器 (AI Orthogonal Mutator)</span>
                <span class="replica-chip">✨ AI 智能受控发散</span>
            </div>
            <div class="replica-pane-baseline-meta">
                <span class="replica-baseline-meta-text">🎬 黄金母本基底：<strong>${beatsCount} 拍</strong> · <strong>${ov.duration_sec != null ? `${ov.duration_sec}s` : '—'}</strong></span>
                <span class="replica-badge ${isLocked ? 'is-locked' : 'is-unlocked'}">
                    ${isLocked ? '🔒 Gold Baseline 已加锁' : '🔓 母本待加锁'}
                </span>
                ${isLocked ? `
                    <button type="button" id="replica-unlock-baseline-btn" class="action-btn text-btn replica-mini-btn" title="解除母本只读保护">🔓 解锁母本</button>
                ` : `
                    <button type="button" id="replica-lock-baseline-btn" class="action-btn primary-btn replica-mini-btn" title="核对节拍无误后加锁，锁定后节拍转为只读保护">🔒 固化母本</button>
                `}
                <button type="button" id="replica-pane-handoff-btn" class="action-btn text-btn replica-mini-btn" title="直接推入分步渲染管线渲染母本">
                    🚀 渲染 1:1 母本
                </button>
            </div>
        </div>

        <!-- 🌐 联网参考案例库 -->
        ${replicaRenderTrendRefsDrawer()}

        <!-- AI 创意发散控制台 -->
        <div class="replica-diverge-bar">
            <div class="replica-diverge-input-wrap">
                <input type="text" id="replica-diverge-brief" class="replica-input replica-diverge-input"
                       placeholder="输入发散灵感/偏好（如：雪山隐世木屋 / 崖壁私汤 / 荒漠夯土工坊 / 雨林树屋，写实建造题材）"
                       value="${escapeHtmlReplica(replicaDivergeBrief || '')}">
            </div>
            <button type="button" id="replica-ai-diverge-btn" class="action-btn primary-btn replica-diverge-btn"
                    title="结合联网参考与母本工序骨架，智能发散 4 组完全正交的写实建造二创创意">
                ${replicaDiverging ? '⏳ 正在发散...' : '✨ AI 智能发散创意'}
            </button>
        </div>

        <!-- AI 发散创意卡片集 -->
        <div class="replica-ai-ideas-container">
            ${replicaRenderAiIdeas(state)}
        </div>

        <!-- 四轴正交词槽输入 (支持 AI 填入后自由微调) -->
        <div class="replica-axis-inputs">
            <div class="replica-axis-field">
                <label class="replica-axis-label" for="replica-axis-env">
                    <span class="replica-axis-tag">轴 1</span>
                    <span>地貌与水体环境 (Environment & Biome)</span>
                </label>
                <input type="text" id="replica-axis-env" class="replica-input"
                       value="${escapeHtmlReplica(axes.environment || '')}"
                       placeholder="例如：雪山松林积雪与清澈冰溪 / 荒漠绿洲与红岩峡谷">
            </div>

            <div class="replica-axis-field">
                <label class="replica-axis-label" for="replica-axis-mat">
                    <span class="replica-axis-tag">轴 2</span>
                    <span>材质与工艺体系 (Material & Craft)</span>
                </label>
                <input type="text" id="replica-axis-mat" class="replica-input"
                       value="${escapeHtmlReplica(axes.material || '')}"
                       placeholder="例如：老柚木防腐原木 + 侘寂微水泥 + 哑光黑碳钢">
            </div>

            <div class="replica-axis-field">
                <label class="replica-axis-label" for="replica-axis-func">
                    <span class="replica-axis-tag">轴 3</span>
                    <span>空间功能与软装 (Space Function & Furnishing)</span>
                </label>
                <input type="text" id="replica-axis-func" class="replica-input"
                       value="${escapeHtmlReplica(axes.function || '')}"
                       placeholder="例如：山林暖炉阅读卧榻 + 悬浮实木茶室">
            </div>

            <div class="replica-axis-field">
                <label class="replica-axis-label" for="replica-axis-hero">
                    <span class="replica-axis-tag">轴 4</span>
                    <span>终极生物/事件揭示 (Hero Creature / Reveal)</span>
                </label>
                <input type="text" id="replica-axis-hero" class="replica-input"
                       value="${escapeHtmlReplica(axes.hero_reveal || '')}"
                       placeholder="例如：窗外林间慢步的高山马鹿 / 绿洲饮水的野生双峰驼">
            </div>
        </div>

        <!-- 🛡️ 重构判断矩阵 (Decision Framework) 实时诊断面板 -->
        ${replicaRenderDecisionMatrix(state, axes, activeIdea, replicaDivergeBrief)}

        <div class="replica-guarantee-box">
            <div class="replica-guarantee-title">🛡️ 骨架硬冻结保障 (Zero Drift Guarantee)：</div>
            <div class="replica-guarantee-list">
                <span>✓ 拍数严格保持：${beatsCount || 11} 拍 (1:1 同构)</span>
                <span>✓ 镜头动力学：14mm / 50% 视高与地平线锁死</span>
                <span>✓ ASMR 原声混音：60% 物理细节 (0% BGM / 100% 旁白)</span>
                <span>✓ 进出场时间戳：0s 进场 / 7.5s 撤离 / 8s 交接</span>
            </div>
        </div>

        <!-- ⚡ 二创派生内联实时进度看板 -->
        ${replicaRenderMutatorProgress()}

        <div class="replica-mutator-actions">
            <button type="button" id="replica-mutate-orthogonal-btn" class="action-btn primary-btn"
                    title="按四轴正交矩阵瞬间派生零漂移二创变体">
                ${replicaBusy && replicaTaskId ? '⏳ 正在派生变体...' : '⚡ 一键生成二创变体提示词包 (Variant)'}
            </button>
            <button type="button" id="replica-toggle-comparator-btn" class="action-btn text-btn"
                    title="横向比对母本与变体 5 列拼图">
                ${replicaComparatorOpen ? '收起横向对比器' : '👁 双轨 5 列拼图横向对比快检'}
            </button>
        </div>
    </div>
    ${canMutate ? replicaRenderDualTrackComparator(state) : ''}`;
}

function replicaRenderJob(state) {
    const isComposeFailed = state.stage === 'compose_failed';
    const isMutateFailed = state.stage === 'mutate_failed';
    const isArchived = state.stage === 'archived' || !!state.archived;
    return `
    <div class="replica-card" id="replica-sec-current-job">
        ${replicaRenderLineageNav(state)}
        <div class="replica-card-title replica-job-title-row">
            <span class="replica-job-title-text">
                ${state.variant_of ? '🧬 二创变体' : '🎬 1:1 黄金母本'} · <strong id="replica-current-title">${escapeHtmlReplica(state.title || state.video_name || state.job_id)}</strong>
            </span>
            <button type="button" class="replica-mini-btn" data-rename-current="${escapeHtmlReplica(state.job_id)}" title="重命名此任务">✏️ 改名</button>
            ${state.title_locked ? '<span class="replica-chip replica-chip-locked" title="名称已锁定，不会被后续步骤覆盖">🔒 已锁定名称</span>' : ''}
            <span class="replica-chip">${escapeHtmlReplica(replicaStageLabel(state))}</span>
            ${state.is_locked_baseline ? '<span class="replica-chip replica-chip-locked">🔒 Gold Baseline 已锁定</span>' : ''}
            ${!isArchived ? `<button type="button" class="replica-mini-btn" data-archive-current="${escapeHtmlReplica(state.job_id)}" title="瘦身归档（删除视频与高清帧，释放磁盘空间）">📦 瘦身归档</button>` : '<span class="replica-chip">📦 已瘦身归档</span>'}
        </div>
        ${replicaRenderPhases(state)}
        ${isComposeFailed ? `
            <div class="replica-banner replica-banner-error">
                <b>合成提示词失败</b>：${escapeHtmlReplica(state.error || '模型调用异常或超限')}
                <div style="margin-top:8px;">
                    <button type="button" id="replica-retry-compose-btn" class="action-btn primary-btn">重新合成</button>
                </div>
            </div>` : ''}
        ${isMutateFailed ? `
            <div class="replica-banner replica-banner-error">
                <b>二创生成失败</b>：${escapeHtmlReplica(state.error || '变体改写异常')}
            </div>` : ''}
        ${state.error && !isComposeFailed && !isMutateFailed ? `<div class="replica-banner replica-banner-error">${escapeHtmlReplica(state.error)}</div>` : ''}
        ${replicaRenderDualWorkbench(state)}
        ${replicaRenderExtract(state)}
        ${replicaRenderProgress()}
        <div id="replica-beats-host">${replicaRenderBeats(state)}</div>
        ${replicaRenderOutput(state)}
    </div>`;
}

// 这条任务到目前为止真花掉的 token。
//
// 确认卡点上一直摆着三档预估，却从不回填实际——那个预估因此永远没有被校准过的机会，
// 用户也无从判断「上次那一单到底花了多少」。摆在预估**旁边**是刻意的：这两个数只有
// 并排看才有意义。
function replicaRenderSpend(state) {
    const spend = state.spend || {};
    const rows = [
        ['reverse', '反推（Pass A + 聚类）'],
        ['advance', '卡点动作与合成'],
        ['mutate', '二创派生'],
        ['extract', '抽帧'],
    ].filter(([key]) => (spend[key] || {}).total_tokens);
    if (!rows.length) return '';

    const grand = rows.reduce((sum, [key]) => sum + (spend[key].total_tokens || 0), 0);
    const fmt = (n) => (n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : String(n));
    return `
    <details class="replica-spend">
        <summary class="replica-hint">
            这条任务已实际花费 <b>${fmt(grand)}</b> tokens —— 展开看分布，可与上面的预估对照
        </summary>
        <div class="replica-spend-rows">
            ${rows.map(([key, label]) => {
                const b = spend[key];
                return `<div class="replica-spend-row">
                    <span>${label}</span>
                    <span>${fmt(b.total_tokens || 0)} tokens · ${b.api_calls || 0} 次调用 · 跑过 ${b.runs || 0} 轮</span>
                </div>`;
            }).join('')}
        </div>
        <p class="replica-hint">
            上面的预估算的是「送多少帧、几次视觉调用」，这里是真实 token 用量——
            两者口径不同，但放在一起就能看出这条任务的钱到底花在了哪一段。
        </p>
    </details>`;
}

// 抽帧拼贴图那一块。默认收起成一行摘要，点开才铺开图——它高、也重（整条序列一张大图），
// 而它只在「核对某一帧到底长什么样」时才有人真去看。图本身仍挂着 #replica-collage，
// 点图开灯箱的行为不变。
function replicaRenderCollageFold(collage, frameCount) {
    if (!collage) return '';
    return `
    <details class="replica-collage-fold" id="replica-collage-fold" ${replicaCollageExpanded ? 'open' : ''}>
        <summary class="replica-collage-summary">
            <span class="replica-collage-summary-label">关键帧拼贴图</span>
            <span class="replica-collage-summary-hint">${
                frameCount ? `${frameCount} 张 · ` : ''}展开看整条序列，点图开大图</span>
        </summary>
        <img class="replica-collage" id="replica-collage" src="${collage}"
             alt="关键帧拼贴图" title="点开看大图" loading="lazy">
    </details>`;
}

function replicaRenderExtract(state) {
    const ov = state.overview;
    if (!ov) return '';
    const collage = replicaCollageUrl(state);
    const est = state.cost_estimate || {};
    const full = est.full || {};
    const degraded = est.degraded || {};
    const every = est.all || {};
    // 采样档位选择框任何时候都渲染。
    //
    // 原先的判据是 `canStart = !hasBeats`：一旦跑出过节拍就再也看不到这对单选框，
    // 想换个档位重跑 Pass A 只能删任务重传。更糟的是首跑——上传后代码直接续跑
    // Pass A，这个框在首跑时压根没机会出现，于是「先看预估再决定」从来没发生过，
    // 每一单都默默走了完整档。现在 extract 停在 confirm_cost，这里就是那个卡点。
    const hasBeats = !!(state.beats && (state.beats.beats || []).length);
    // 重跑过一次的（已有帧事实或上次失败），标清楚这是重试而且不重付视觉调用的钱。
    const isRetry = !!state.error || !!state.facts;
    const atCostGate = state.stage === 'confirm_cost';
    const startLabel = atCostGate ? '确认并开始反推'
        : (isRetry ? '重试反推' : (hasBeats ? '换档位重跑反推' : '开始反推'));
    const scope = state.review_scope || (state.degraded ? 'degraded' : 'plan');
    const fps = replicaCurrentFps(state);

    const isCollapsible = !atCostGate && state.stage !== 'extract' && hasBeats;
    const isCollapsed = isCollapsible && !replicaExtractExpanded;

    return `
    <div class="replica-section" id="replica-sec-extract">
        <div class="replica-card-title replica-section-head">
            <span>抽帧结果</span>
            ${isCollapsible ? `
                <button type="button" id="replica-toggle-extract-btn" class="action-btn text-btn replica-mini-btn">
                    ${isCollapsed ? '展开抽帧 / 反推通道 / 模型设置 ⌄' : '收起 ⌃'}
                </button>
            ` : ''}
        </div>
        ${isCollapsed ? `
            <div class="replica-metrics replica-metrics-folded">
                <span>时长 ${ov.duration_sec ?? '—'}s</span>
                <span>抽帧 ${ov.frame_count ?? '—'} 张</span>
                <span>基线 ${fps} fps</span>
                <span>变化事件 ${ov.change_event_count ?? '—'} 个</span>
                <span>送审档位 ${scope}</span>
            </div>
            ${replicaRenderCollageFold(collage, ov.frame_count)}
        ` : `
            <div class="replica-metrics">
                <span>时长 ${ov.duration_sec ?? '—'}s</span>
                <span>抽帧 ${ov.frame_count ?? '—'} 张</span>
                <span>基线 ${fps} fps</span>
                <span>变化事件 ${ov.change_event_count ?? '—'} 个</span>
                <span>送审计划 ${(ov.analysis_plan || {}).mode || '—'}</span>
            </div>
            <div class="replica-reextract">
                <label class="replica-inline-field">抽帧密度
                    ${replicaFpsSelect('replica-base-fps', fps)}
                </label>
                <label class="replica-inline-field">跳变灵敏度
                    ${replicaThresholdSelect('replica-base-threshold', replicaCurrentThreshold(state))}
                </label>
                <button type="button" id="replica-reextract-btn" class="action-btn text-btn">
                    按新档位重抽帧
                </button>
                <p class="replica-hint">
                    抽出来的帧太少（事件之间的推进看不出来）就把密度往上调一档；变化事件漏检
                    （工序接缝没被标出来）就把灵敏度往下调一档。抽帧不花模型钱，但<b>会作废
                    这条任务已有的帧事实与节拍</b>：帧文件名是序号不是时间戳，换了档位同一个
                    名字指向的是另一时刻的画面，所以旧的读数必须全丢。
                </p>
            </div>
            ${collage ? replicaRenderCollageFold(collage, ov.frame_count)
            : `<div class="replica-banner replica-banner-error">
                拼贴图缺失。它是节拍映射的前置门禁，缺了等于没看过整条序列就要定义节拍。</div>`}
            <div class="replica-cost">
                ${atCostGate ? `<p class="replica-hint replica-cost-gate">
                    <b>抽帧已完成，还没有开始花钱。</b>反推是整条线的成本大头——
                    先决定送多少帧给多模态模型，选好再按开始。
                </p>` : ''}
                ${replicaRenderSpend(state)}
                <div id="replica-scope-choices" class="replica-scope-choices">
                <label class="replica-radio">
                    <input type="radio" name="replica-mode" value="all" ${scope === 'all' ? 'checked' : ''}>
                    <span>全部（${every.frame_count || 0} 帧 / 约 ${every.batch_count || 0} 次视觉调用）——
                          抽出来多少送多少，识别最密、也最贵</span>
                </label>
                <label class="replica-radio">
                    <input type="radio" name="replica-mode" value="full" ${scope === 'plan' ? 'checked' : ''}>
                    <span>计划（${full.frame_count || 0} 帧 / 约 ${full.batch_count || 0} 次）——
                          脚本的送审计划：长片只挑约四成 + 每秒至少一张</span>
                </label>
                <label class="replica-radio">
                    <input type="radio" name="replica-mode" value="degraded" ${scope === 'degraded' ? 'checked' : ''}>
                    <span>降级（${degraded.frame_count || 0} 帧 / 约 ${degraded.batch_count || 0} 次）——
                          只读事件的起止与峰值帧，<b>事件之间发生了什么全靠推断，节拍精度更低</b></span>
                </label>
                <p class="replica-hint">
                    觉得「识别的图片太少」时先看这里：抽出来的 ${ov.frame_count ?? '—'} 张里，
                    计划档只送 ${full.frame_count || 0} 张。要更密就选「全部」；抽出来的总数
                    本身不够，则回上面调抽帧密度重抽。
                </p>
                </div>
                <div class="replica-model-picker">
                    <div class="replica-card-subtitle">反推模型</div>
                    <label class="replica-inline-field">逐帧识别（Pass A）
                        ${replicaModelSelect('replica-frame-model',
                            replicaConfigValue('frameFactsModel', REPLICA_PASS_A_DEFAULT_MODEL))}
                    </label>
                    <label class="replica-inline-field" id="replica-peak-field">峰值帧复核
                        ${replicaModelSelect('replica-peak-model',
                            replicaConfigValue('peakVerifyModel', ''),
                            [{ value: '', label: '跟随主模型（默认）' },
                             { value: 'off', label: '关闭复核（省这几次调用）' }])}
                    </label>
                    <p class="replica-hint">
                        逐帧识别读的是材料标签、工具类型、完成范围这类细节，模型弱一档就会
                        读糊，而节拍阶梯完全建在这些读数上。便宜的 flash 打底 + 强模型复核
                        峰值帧是默认组合；对精度不满意就把逐帧识别也换成强模型，代价是
                        ${full.batch_count || 0} 次调用全部按强模型计价。
                        <b>「逐帧识别」两条通道共用</b>（极速直读那一次调用也走它）；
                        <b>峰值帧复核只在标准深度通道生效</b>。
                        ${state.facts && state.facts.model
                            ? `上一轮实际用的是 <code>${escapeHtmlReplica(state.facts.model)}</code>。` : ''}
                    </p>
                </div>
                ${full.peak_frame_count ? `<p class="replica-hint">
                    另加 ${full.peak_batch_count || 0} 次峰值帧复核（${full.peak_frame_count} 张）。
                    节拍边界恰好落在这几帧上，读糊了整条阶梯会整体错位，所以默认开。
                </p>` : ''}
                ${isRetry ? `<p class="replica-hint">
                    ${state.facts ? `已读过 ${state.facts.frame_count || 0} 帧，帧事实走磁盘缓存——
                         同一档、同一个逐帧识别模型重试不重付视觉调用的钱；换到更密的一档只
                         补付新增的那些帧。<b>换了逐帧识别模型则会全部重读</b>（缓存按模型分桶，
                         不然新模型的钱付了、拿到的还是旧模型的读数），换回去仍然免费。`
                        : '上次没跑完，可以直接重试。'}
                </p>` : ''}
                ${hasBeats && !atCostGate ? `<p class="replica-hint">
                    已经有一份节拍阶梯了。换档位重跑会覆盖它——你在下面改过的内容会丢。
                </p>` : ''}
                <button type="button" id="replica-start-btn" class="action-btn primary-btn">
                    ${startLabel}
                </button>
            </div>
        `}
    </div>`;
}

function replicaRenderProgress() {
    // 跑着的时候要有个能按停的东西。后端的 cancel_event 通路一直都在，只是从来没有
    // 按钮去按它——一轮跑错了的 Pass A 此前只能干等它烧完。
    const running = !!replicaSSE;
    return `
    <div id="replica-progress" class="replica-progress" style="display:${running ? 'block' : 'none'};">
        <div class="replica-progress-head">
            <span class="replica-chip" id="replica-progress-stage"></span>
            <span id="replica-progress-label"></span>
            <span class="replica-progress-percent" id="replica-progress-percent"></span>
        </div>
        <div class="replica-progress-track"><div class="replica-progress-fill" id="replica-progress-fill"></div></div>
        <ul class="replica-progress-log" id="replica-progress-log"></ul>
    </div>
    ${running ? `<div class="replica-actions">
        <button type="button" id="replica-cancel-btn" class="action-btn text-btn">中断这一轮</button>
    </div>` : ''}`;
}

/* --- 进度模型 ---
 *
 * 复刻线最长的那一段（合成提示词）此前在这个页面上是**完全静默**的：合成器一路广播
 * outline / batch / batch_generating / beat_ready 这些事件，页面却只监听 replica_stage，
 * 于是用户看到的是「正在按 9 拍阶梯合成提示词…」这一句，然后干等好几分钟。
 * 这里把两路合并成一条进度：replica_stage 决定处在哪个大阶段（给出百分比区间），
 * 合成器的事件在 compose 那一段里给出段内进度，文案直接复用 ProgressModel
 * （js/progress_model.js，与主生成页同一套口径，不另抄一份）。
 */

// 每个 replica stage 在整条进度上的区间。
const REPLICA_STAGE_RANGE = {
    ingest: [0, 3], extract: [3, 15], confirm_cost: [15, 15],
    // review_frames 收窄到 38：峰值帧复核跟逐帧提取同属这个 stage，却发生在它之后。
    // 共用一个区间的话，"只增不减"的进度条在逐帧提取跑到 45 之后就再也动不了，
    // 复核那几十张强模型调用整段静默（见 REPLICA_ACTION_RANGE.peak_verify）。
    review_frames: [15, 38], cluster_beats: [45, 68], mutate_beats: [45, 68],
    review_beats: [68, 68],
    compose: [68, 94], audit: [94, 99], audit_failed: [99, 99], completed: [100, 100],
};

// 人工卡点上的机器活。这四个动作都在 review_beats 这个 stage 下跑，而它的阶段区间是
// 零宽的——所以它们各自需要一段自己的区间，否则进度条在几分钟里一动不动。
// 区间都落在 68 往后：这些动作是在卡点上"往前修"，不是退回上一阶段。
const REPLICA_ACTION_RANGE = {
    peak_verify: [38, 45],
    mutate_orthogonal: [45, 68],
    variant: [45, 68],
    autofix: [68, 82],
    fix_beats: [68, 82],
    refine_craft: [68, 86],
    autobalance: [68, 76],
    translate: [68, 74],
};

// 动作期间 chip 上显示的名字。用动作名而不是阶段名——阶段名是「待人工核对」，
// 在机器跑着的时候把它摆出来，等于给了用户一个错的答案。
const REPLICA_ACTION_LABELS = {
    peak_verify: '强模型复核峰值帧',
    mutate_orthogonal: '⚡ 正交二创变体派生',
    variant: '🧬 派生二创变体',
    autofix: 'AI 修复硬伤',
    fix_beats: 'AI 修复硬伤',
    refine_craft: '工艺精修',
    autobalance: '自动平衡时序',
    translate: '重做中文对照',
};

// 合成器自己的事件（progress_model 认得的那一套）。beat_ready 单独处理：它带的是
// 「第几拍的提示词已经产出」，是这一段里唯一能给出真实分子/分母的信号。
const REPLICA_COMPOSER_EVENTS = [
    'outline', 'batch', 'batch_generating', 'batch_generated', 'batch_retry',
    'batch_failed', 'compose_soft_timeout', 'audit', 'repair', 'beat_ready',
];

let replicaProgress = null;

function replicaResetProgress() {
    replicaProgress = {
        stage: '',
        range: [0, 100],
        percent: 0,
        label: '',
        actionLabel: '',   // 非空时盖过 stage 名（见 replicaHandleStageEvent）
        log: [],
        composeState: (window.ProgressModel && window.ProgressModel.createProgressState('compose')) || null,
    };
}

function replicaProgressPaint() {
    const root = replicaRoot();
    const box = root && root.querySelector('#replica-progress');
    const mutatorBox = root && root.querySelector('#replica-mutator-progress');
    if (!replicaProgress) return;

    const stageText = replicaProgress.actionLabel || replicaStageLabel(replicaProgress.stage) || '进行中';
    const labelText = replicaProgress.label || '';
    const pctText = `${Math.round(replicaProgress.percent)}%`;
    const fillWidth = `${Math.max(2, Math.min(100, replicaProgress.percent))}%`;

    if (box) {
        box.style.display = 'block';
        const set = (id, text) => {
            const el = box.querySelector(id);
            if (el) el.textContent = text;
        };
        set('#replica-progress-stage', stageText);
        set('#replica-progress-label', labelText);
        set('#replica-progress-percent', pctText);
        const fill = box.querySelector('#replica-progress-fill');
        if (fill) fill.style.width = fillWidth;
        const log = box.querySelector('#replica-progress-log');
        if (log) {
            log.innerHTML = replicaProgress.log
                .map(line => `<li>${escapeHtmlReplica(line)}</li>`).join('');
        }
    }

    if (mutatorBox) {
        // 与渲染期同一条判据：不是二创那一趟就收起来，别拿别的阶段的进度冒充派生进度
        if (!replicaIsMutateRunning()) {
            mutatorBox.style.display = 'none';
            return;
        }
        mutatorBox.style.display = 'block';
        const setM = (id, text) => {
            const el = mutatorBox.querySelector(id);
            if (el) el.textContent = text;
        };
        setM('#replica-mutator-progress-stage', stageText);
        setM('#replica-mutator-progress-label', labelText);
        setM('#replica-mutator-progress-percent', pctText);
        const fillM = mutatorBox.querySelector('#replica-mutator-progress-fill');
        if (fillM) fillM.style.width = fillWidth;
    }
}

// 百分比只增不减：几路事件交替到达时来回跳的进度条比没有进度条更让人不安。
function replicaProgressUpdate(percent, label, stage) {
    if (!replicaProgress) replicaResetProgress();
    if (stage) replicaProgress.stage = stage;
    if (Number.isFinite(percent)) {
        replicaProgress.percent = Math.max(replicaProgress.percent, Math.min(100, percent));
    }
    if (label && label !== replicaProgress.label) {
        replicaProgress.label = label;
        // 只留最近 6 条，且不重复上一条——批量合成会连着推很多条同文案的事件。
        if (replicaProgress.log[replicaProgress.log.length - 1] !== label) {
            replicaProgress.log.push(label);
            replicaProgress.log = replicaProgress.log.slice(-6);
        }
    }
    replicaProgressPaint();
}

function replicaHandleStageEvent(detail) {
    const stage = (detail && detail.stage) || '';
    const action = (detail && detail.action) || '';
    // 人工卡点上跑的那几个动作（AI 修硬伤 / 自动平衡 / 工艺精修 / 重做中文对照）都挂在
    // review_beats 底下，而它的区间是零宽的 [68,68]：进度条纹丝不动，chip 上还写着
    // 「待人工核对」——界面在说"等你动手"，其实是机器在跑。带 action 的事件走一段
    // 独立的动作区间，动作结束后自然落回 68 那个静态卡点。
    //
    // action 由后端随事件下发，而不是在按钮点击处于前端记：SSE 重连或事件重放之后
    // 前端那个状态就没了，而那正是最需要它的时刻——用户刷新页面，恰恰因为他觉得卡住了。
    const range = (action && REPLICA_ACTION_RANGE[action])
        || REPLICA_STAGE_RANGE[stage]
        || replicaProgress.range || [0, 100];
    replicaProgress.range = range;

    // 后端多个阶段早就在事件里带了真实的分子/分母，此前这里一个都没用：Pass A 是整条
    // 线最长的一段（占 30%），全程钉死在区间起点 15%，只有日志在滚。
    const done = Number(detail && detail.done);
    const total = Number(detail && detail.total);
    const ratio = (Number.isFinite(done) && total > 0) ? Math.min(1, done / total) : 0;

    // 段内进度由该阶段自己的事件推进；没有分子分母时行为与从前一致——落到区间起点。
    replicaProgressUpdate(range[0] + ratio * (range[1] - range[0]),
                          (detail && detail.message) || '', stage);
    // chip 文案：动作期间显示动作名，否则回到阶段名。后端在这几条流程的**每一条**
    // 事件上都带 action，所以这里可以直接按当前事件覆写，不需要额外的进入/退出信号。
    replicaProgress.actionLabel = action ? (REPLICA_ACTION_LABELS[action] || '') : '';
    replicaProgressPaint();
}

function replicaHandleComposerEvent(type, detail) {
    if (!replicaProgress) replicaResetProgress();
    const [lo, hi] = REPLICA_STAGE_RANGE[replicaProgress.stage] || REPLICA_STAGE_RANGE.compose;

    if (type === 'beat_ready') {
        const total = Number(detail && detail.total) || 0;
        const index = Number(detail && detail.index) || 0;
        const ratio = total ? Math.min(1, index / total) : 0;
        replicaProgressUpdate(lo + ratio * (hi - lo),
            total ? `已产出第 ${index}/${total} 拍的提示词` : '提示词逐拍产出中');
        return;
    }
    if (!window.ProgressModel) return;
    const out = window.ProgressModel.normalizeGenerationProgress(
        type, detail, 'compose', replicaProgress.composeState);
    replicaProgress.composeState = out.state;
    replicaProgressUpdate(lo + (Number(out.percent) || 0) / 100 * (hi - lo), out.label);
}

// 全片概览。此前这里是**两条**：一条「胶卷时间轴」（按时长比例排的块，块里写着
// ID/阶段/起止时间/超长微拍标记），紧挨着一条「跳转 chip 条」（等宽小圆角，带
// 错误/待确认/过门三种状态色）。两条回答的是同一个问题——有几拍、哪一拍有事、
// 点它去那一拍——只是画法不同，加起来吃掉第一屏 147px，而且各缺一半信息：
// 时间轴不知道哪一拍有硬伤，chip 条不知道哪一拍超长。
//
// 合并的办法不是二选一，是让两者各干一件事：
//   · 比例条只回答「时间是怎么分配的」。块里一个字都不写——此前它按比例缩到
//     48px 下限，却还要在里面塞三行文字（"B05" / "封板封闭" / "18s–22.5s (4.5s)"），
//     30 拍往上那三行必然被截断，等于画了一条读不出内容的条。没有文字就没有下限
//     问题，多少拍都成立。
//   · chip 条只回答「哪一拍有事、点它去哪」。它等宽、能换行、状态色齐全，
//     本来就是更好的跳轨控件。
function replicaRenderLadderOverview(doc, errors, warns) {
    const beats = doc.beats || [];
    if (!beats.length) return '';
    const totalDuration = doc.video_duration_sec
        || (beats[beats.length - 1] ? beats[beats.length - 1].end : 10) || 10;
    const speed = doc.speed_multiplier || (beats[0] && beats[0].speed_factor) || 2.0;
    const totalActionSec = (totalDuration * speed).toFixed(1);

    // 一拍的三种「有事」，比例条与 chip 条现在读的是同一份判断。
    const flags = beats.map((b, i) => {
        const span = Math.max(0.1, ((b.end || 0) - (b.start || 0)));
        const prev = beats[i - 1];
        return {
            span,
            isErr: errors.some(e => e.beat_id === b.id),
            isWarn: warns.some(w => w.beat_id === b.id),
            isCrossed: i > 0 && prev && prev.space && b.space
                       && String(prev.space).toLowerCase() !== String(b.space).toLowerCase(),
            isTooLong: span > 6.0,
            isTooShort: span < 2.0,
        };
    });

    const cls = (f, extra) => [
        extra,
        f.isErr ? 'is-error' : (f.isWarn ? 'is-warn' : ''),
        f.isCrossed ? 'is-crossed' : '',
        f.isTooLong ? 'is-toolong' : '',
        f.isTooShort ? 'is-tooshort' : '',
    ].filter(Boolean).join(' ');

    const tip = (b, f) => {
        const label = REPLICA_BEAT_STAGE_LABELS[b.stage] || b.stage || '';
        const bits = [`${b.id} ${b.start}s–${b.end}s（${f.span.toFixed(1)}s，2x 等效动作 ${(f.span * speed).toFixed(1)}s）`];
        if (label) bits.push(label);
        if (b.operation) bits.push(b.operation);
        if (f.isTooLong) bits.push('⚠️ 超长拍（>6.0s）');
        if (f.isTooShort) bits.push('⚠️ 微拍（<2.0s）');
        if (f.isCrossed) bits.push(`过门 → ${b.space}`);
        if (f.isErr) bits.push('有硬伤');
        else if (f.isWarn) bits.push('待人工确认');
        return bits.join(' · ');
    };

    // 比例条。宽度仍按时长占比，但 flex-shrink:0 + min-width 让它在拍数多的时候
    // 真的溢出、真的能横着滚，而不是把每一块继续压扁。
    const segs = beats.map((b, i) => {
        const f = flags[i];
        // 减掉一格 gap：百分比之和恰好是 100%，再加上段间的 2px 就必然溢出，
        // 于是每一份阶梯都挂着一条滚不出东西来的横向滚动条。
        const pct = Math.max(0.6, Math.min(100, (f.span / totalDuration) * 100));
        // 够宽的块直接把拍号与秒数写进去。此前这条比例条一个字都没有，要知道第几块
        // 是哪一拍只能悬停一块试一块——下面那条 chip 条之所以非存在不可，一半原因
        // 就在这里。窄块仍然留白（写不下的字比不写更糟），靠 chip 条与 tooltip 认。
        const label = pct >= 6.5
            ? `<span class="replica-ladder-seg-label"><b>${escapeHtmlReplica(b.id)}</b>${
                pct >= 11 ? `<i>${f.span.toFixed(1)}s</i>` : ''}</span>`
            : '';
        return `<button type="button" class="${cls(f, `replica-ladder-seg stage-${escapeHtmlReplica(b.stage || 'structural')}`)}"
                style="width:calc(${pct.toFixed(2)}% - 2px)" data-jump-beat="${escapeHtmlReplica(b.id)}"
                title="${escapeHtmlReplica(tip(b, f))}" aria-label="${escapeHtmlReplica(tip(b, f))}">${label}</button>`;
    }).join('');

    // 时间刻度。比例条按时长画宽度，却没有任何一处标出「这里是第几秒」——
    // 于是「原片 30 秒那个动作在哪一拍」这种问题在页面上只能靠数块。
    // 刻度按 5/10/15/30/60s 里挑一档，让整条大约落 5~8 根。
    const tickStep = [5, 10, 15, 20, 30, 60, 120].find(st => totalDuration / st <= 8) || 300;
    const ticks = [];
    for (let t = 0; t <= totalDuration + 0.001; t += tickStep) {
        const left = (t / totalDuration) * 100;
        // 末端那一根由 is-end 单独画（它右对齐、写的是真实总时长）。整除时这里会
        // 正好落在 100%，两根字会叠在一起，所以贴边的一律让给它。
        if (left > 96) continue;
        ticks.push(`<span class="replica-ladder-tick" style="left:${left.toFixed(2)}%">${Math.round(t)}s</span>`);
    }
    const rulerHtml = `<div class="replica-ladder-ruler" aria-hidden="true">${ticks.join('')}
        <span class="replica-ladder-tick is-end" style="left:100%">${totalDuration.toFixed(0)}s</span></div>`;

    // 图例。条上那几种画法（斜纹 = 超长、描边 = 微拍、底色条 = 硬伤/待确认、
    // 左侧竖线 = 过门）此前只在 tooltip 里解释，等于没解释。只列出这一条上真出现过的。
    const legendBits = [
        flags.some(f => f.isTooLong) ? '<span class="replica-ladder-legend-item"><i class="lg-toolong"></i>超长拍 &gt;6.0s</span>' : '',
        flags.some(f => f.isTooShort) ? '<span class="replica-ladder-legend-item"><i class="lg-tooshort"></i>微拍 &lt;2.0s</span>' : '',
        flags.some(f => f.isCrossed) ? '<span class="replica-ladder-legend-item"><i class="lg-crossed"></i>过门换空间</span>' : '',
        flags.some(f => f.isErr) ? '<span class="replica-ladder-legend-item"><i class="lg-error"></i>硬伤</span>' : '',
        flags.some(f => f.isWarn && !f.isErr) ? '<span class="replica-ladder-legend-item"><i class="lg-warn"></i>待确认</span>' : '',
    ].filter(Boolean).join('');
    const legendHtml = legendBits
        ? `<div class="replica-ladder-legend">${legendBits}</div>` : '';

    const chips = beats.map((b, i) => {
        const f = flags[i];
        return `<button type="button" class="${cls(f, 'replica-beat-jump-chip')}"
                data-jump-beat="${escapeHtmlReplica(b.id)}" title="${escapeHtmlReplica(tip(b, f))}"
                ><span>${escapeHtmlReplica(b.id)}</span>${f.isErr ? '<span class="dot-err">●</span>' : ''}</button>`;
    }).join('');

    const tooLong = flags.filter(f => f.isTooLong).length;
    const tooShort = flags.filter(f => f.isTooShort).length;
    const rhythmNote = (tooLong || tooShort)
        ? `<span class="replica-overview-flag">${[
            tooLong ? `${tooLong} 拍超长` : '', tooShort ? `${tooShort} 拍微拍` : ''
          ].filter(Boolean).join(' · ')}</span>` : '';

    return `
    <div class="replica-ladder-overview">
        <div class="replica-overview-head">
            <span class="replica-overview-title">🎬 ${beats.length} 拍 · 屏幕 ${totalDuration.toFixed(1)}s · 2x 等效动作 ${totalActionSec}s</span>
            ${rhythmNote}
            <span class="replica-overview-actions">
                <button type="button" id="replica-autobalance-btn" class="action-btn text-btn replica-mini-btn"
                        title="根据 2x 倍速时序规则自动拆解 >6.0s 超长拍并合并微拍">⚡ 自动平衡秒数/拆拍</button>
                <button type="button" id="replica-toggle-fold-all" class="action-btn text-btn replica-mini-btn">全部折叠 / 全部展开</button>
            </span>
        </div>
        <div class="replica-ladder-bar" role="group" aria-label="节拍时长比例条，点一段跳到那一拍">${segs}</div>
        ${rulerHtml}
        ${legendHtml}
        <details class="replica-ladder-chips-fold" ${replicaLadderChipsOpen ? 'open' : ''} id="replica-ladder-chips-fold">
            <summary>拍号索引（${beats.length} 拍）</summary>
            <div class="replica-ladder-chips" role="group" aria-label="节拍跳转">${chips}</div>
        </details>
    </div>`;
}

function replicaRenderBeats(state) {
    const doc = state.beats;
    if (!doc || !(doc.beats || []).length) return '';
    const violations = state.validation || doc.validation || [];
    const errors = violations.filter(v => v.level === 'error');
    const warns = violations.filter(v => v.level !== 'error');

    // 2026-08-10：这里原先把 temporary_object_lingering 特判成"会让合成直接失败"并强制
    // 展开。那条冲突已经在源头修掉（reverse_engineered 让合成器豁免清场规则），现在它
    // 只是一条"原片里确实有、复刻会照实保留"的提示，不再预示失败。

    // 一条校验结论一个 <li>；带 beat_id 的做成可点，点了滚到那一拍并高亮。
    // 原先是一列纯文本：红字说「B08 违反施工依赖顺序」，用户得自己在十几张卡片里
    // 数到 B08。报错指得出是哪一拍，就应该能带人走到那一拍。
    const item = (v) => (v.beat_id
        ? `<li><button type="button" class="replica-jump" data-jump-beat="${escapeHtmlReplica(v.beat_id)}"
                >${escapeHtmlReplica(v.message)}</button></li>`
        : `<li>${escapeHtmlReplica(v.message)}</li>`);

    const banner = `
        ${errors.length ? `<div class="replica-banner replica-banner-error">
            <div class="replica-banner-head">
                <b>${errors.length} 项硬伤必须先修掉才能合成：</b>
                <button type="button" id="replica-banner-autofix-btn" class="action-btn primary-btn replica-mini-btn">🪄 一键 AI 修复全部硬伤</button>
            </div>
            <ul>${errors.map(item).join('')}</ul>
        </div>` : `<div class="replica-banner replica-banner-ok">节拍阶梯已通过全部机械校验。</div>`}
        ${warns.length ? `<details class="replica-banner replica-banner-warn">
            <summary>${warns.length} 项待人工确认</summary>
            <ul>${warns.map(item).join('')}</ul>
        </details>` : ''}`;

    const cards = (doc.beats || []).map((beat, idx) => replicaRenderBeatCard(
        state, beat, idx, (doc.beats[idx - 1] || {}).space)).join('');
    const banned = (doc.banned_elements || []).map(x => escapeHtmlReplica(x)).join('、');

    return `
    <div class="replica-section" id="replica-sec-beats">
        <div class="replica-card-title">节拍阶梯（${doc.beats.length} 拍）——唯一的人工卡点</div>
        <p class="replica-hint">
            这是整条链路上唯一能拦住「模型脑补了一个不存在的工序」的地方。对着证据帧核对，
            改完记得保存——保存会立刻重跑一遍校验。
        </p>
        ${replicaShotRhythmLine(doc)}
        ${banner}
        ${replicaRenderLadderOverview(doc, errors, warns)}
        ${replicaRenderTimeWindows(doc)}
        <div class="replica-beats">${cards}</div>
        <div class="replica-section">
            <label class="replica-field-label">禁用元素（原片里不存在，出现在提示词里即须重写）</label>
            <textarea id="replica-banned" class="replica-textarea" rows="2"
                      placeholder="用、分隔">${banned}</textarea>
        </div>
        ${replicaRenderSceneConstants(doc)}
        ${/* 这一排只放别处没有的动作：保存与合成已经常驻吸底操作栏
              （replicaRenderBottomBar）。同一个动作在一屏之内出现两次，用户得先
              判断这两个是不是同一个按钮才敢点。 */''}
        ${/* 「AI 修复硬伤」与「自动平衡秒数/拆拍」此前在这一排里各有一份，而它们
              在别处已经各有一份了：修复挂在硬伤横幅上（硬伤清单就列在那里），平衡挂在
              比例条头部（超长/微拍的标记就画在那里）。同一个动作在一屏之内出现两次，
              用户得先判断这两个是不是同一个按钮才敢点——这条纪律保存与合成一直守着，
              这两个漏了。
              修复按钮同时还犯了另一条：0 硬伤时横幅里没有它，这一排里却还摆着一枚，
              点下去无事发生。本文件里已有同款判断——「摆一个点了必然报错的按钮比不摆
              更糟」（见下面撤销按钮的条件渲染）。现在它只在有硬伤时、只在硬伤旁边出现。 */''}
        <div class="replica-actions">
            <button type="button" id="replica-refine-craft-btn" class="action-btn text-btn"
                    title="看着证据帧把每一拍的措辞写准：补位置锚、补完成量、拆开结果与状态、补工具/声音/景别/运镜/光照/物料。画面上发生了什么一个字不动，1:1 不受影响。已有的合成提示词会作废，需要重新合成。">✨ 工艺精修（不动 1:1）</button>
            <button type="button" id="replica-recluster-btn" class="action-btn text-btn"
                    title="帧事实走缓存，不会重付视觉调用的钱">重跑聚类</button>
            <button type="button" id="replica-translate-btn" class="action-btn text-btn"
                    title="只翻译，不改英文原文。改过英文的字段中文会先作废，按这里补回来">重译中文</button>
            ${/* 上面这排每一个都是整份覆盖。没有回退的话，模型把一条手工调好的阶梯改坏了
                  只能重跑 Pass B——重新付钱，而且结果还不一样。
                  没有可回退版本时不摆这个按钮：摆一个点了必然报错的按钮比不摆更糟。 */''}
            ${state && state.beats_undo_available ? `
            <button type="button" id="replica-undo-beats-btn" class="action-btn text-btn"
                    title="回到上一版节拍阶梯。撤销本身也可以再撤销回来。已合成的提示词会作废">↩ 撤销上一次改写</button>` : ''}
        </div>
    </div>`;
}

// 施工阶段：卡片上唯一一个由校验器直接判死、却长期改不动的字段。
//
// 它此前是个只读 chip。于是「阶段标错」这类硬伤在人工卡点上无解——红字指着某一拍说
// 它违反施工依赖顺序，而那一拍上偏偏没有任何入口能纠正它，用户只能重跑聚类碰运气
// （2026-08-13：整条阶梯只有一项硬伤，病灶恰好就是这个字段）。校验器指着某个字段说
// 它不对，那个字段就必须能改，否则那条校验对人是不可操作的。
//
// 走 [data-beat][data-key] 那条通用回写通路，不另造一条：select 的 input 事件与
// textarea 同形，写回的是 stage 的英文枚举值（中文只作显示，见 zh 的同一条纪律）。
function replicaStageSelect(beat, idx) {
    const current = beat.stage || '';
    const known = Object.prototype.hasOwnProperty.call(REPLICA_BEAT_STAGE_LABELS, current);
    // 模型偶尔回一个不在九档里的值。保留它作为一个选项，否则渲染时会被下拉框默默
    // 改成第一档——用户没动过的字段被我们改了，还改得无声无息。
    const options = [
        ...(known || !current ? [] : [[current, `${current}（未知档，请重选）`]]),
        ...Object.entries(REPLICA_BEAT_STAGE_LABELS),
    ].map(([value, label]) => `
        <option value="${escapeHtmlReplica(value)}" ${value === current ? 'selected' : ''}
            >${escapeHtmlReplica(label)}</option>`).join('');
    return `
        <select class="replica-chip replica-stage-select" data-beat="${idx}" data-key="stage"
                title="施工阶段。改完点「保存并重校验」立刻看硬伤数变化">${options}</select>`;
}

// 场景恒常特征：与禁用元素对称的另一半。禁用元素说「原片里永远没有的」，这里说
// 「原片里一直都在的」。它随 dimensions 进每一条合成提示词，所以必须可编辑——统计
// 难免把工人的手套、一次性道具算进来，用户删掉之后不能被下一次读状态又加回去
// （后端 attach_scene_constants 因此只在字段为空时才算）。
const REPLICA_SCENE_CONSTANT_FIELDS = [
    ['environment', '常驻大环境与地貌水体'],
    ['materials', '常驻材质与表面'],
    ['traces', '常驻痕迹与风化'],
    ['fixtures_in_shot', '常驻画面的器具'],
    // 全片人物识别项。工序每拍都在变，穿的那件衣服不变，所以它和污渍青苔一样属于恒常
    // 项、在节拍阶梯里没有落脚点。但它的失效方式更刺眼：每一帧都是独立生成的，图像模型
    // 对上一帧没有记忆——外形不写进提示词，同一条片子里就会换人种、换肤色、换发型，
    // 休闲工装变成反光背心加安全帽。人种/肤色写在这里不是标签，是「同一个人」的判据。
    ['cast', '常驻人物与活物（人种/肤色、身形、发型胡须、帽子、上衣颜色、裤子颜色、鞋 —— 全片同一个人，只换姿势不换外形）'],
    // 影调：「像不像那条片子」的第一眼因素。此前整条链路一个字都没有，每一帧的色温、
    // 对比、饱和都由图像模型自己定，十几张图拼起来像十几条片子。只写摄影口径（色温偏向、
    // 对比、饱和、黑位），别写「电影感/大片感」——那不是影调，是让模型自由发挥的口令。
    ['grade', '全片影调（色温偏冷/偏暖、对比高低、饱和、黑位。别写「电影感」这类词）'],
    // 环境底噪：与常驻运动严格对称的另一半——那条管画面里一直在动的，这条管声轨上一直
    // 在响的。空着，每条视频的环境声都是模型现编的，整片声场一拍一个样。
    ['ambient_sound', '常驻环境声（风穿树冠/溪流/远处车流/海浪/空房间的混响 —— 没人干活时这地方的声音）'],
    // 这一栏与其余各栏不同：它不是「一直在」而是「一直在动」，而且**统计不出来**
    // （帧事实是一张张静止画面的清单，看不出水在流）。由 Pass B 读帧序列产出，
    // 或者在这里手补——漏掉它，交付出来的背景就是一张静止照片。
    ['motion', '常驻运动（溪水/烟/火苗/风吹树冠/雨雪/飘尘 —— 一直在动的东西）'],
];

function replicaRenderSceneConstants(doc) {
    const isArray = Array.isArray(doc.scene_constants);
    const sc = (!isArray && doc.scene_constants) ? doc.scene_constants : {};
    const signature = doc.scene_signature || '';
    const arrayItems = isArray ? (doc.scene_constants || []) : [];
    if (!signature && !arrayItems.length && !REPLICA_SCENE_CONSTANT_FIELDS.some(([k]) => (sc[k] || []).length)) return '';
    const arrayRow = arrayItems.length ? `
        <label class="replica-field">
            <span class="replica-field-label">常驻地标与环境特征（固定不动物体/背景地标，用、分隔）</span>
            <textarea class="replica-textarea" rows="2" data-scene-array="true"
                      placeholder="用、分隔">${escapeHtmlReplica(arrayItems.join('、'))}</textarea>
        </label>` : '';
    const rows = REPLICA_SCENE_CONSTANT_FIELDS.map(([key, label]) => `
        <label class="replica-field">
            <span class="replica-field-label">${label}</span>
            <textarea class="replica-textarea" rows="2" data-scene-key="${key}"
                      placeholder="用、分隔">${escapeHtmlReplica((sc[key] || []).join('、'))}</textarea>
        </label>`).join('');
    return `
    <div class="replica-section" id="replica-sec-scene">
        <div class="replica-field-label">场景恒常特征（整片一直存在，会写进每一条提示词）</div>
        <p class="replica-hint">
            由帧事实本地统计得来。节拍只承载每一拍的变化，而这些东西不变化，因此在阶梯里没有落脚点——
            不单独送进去，复刻出来就是工序全对、质感全无。误判的（工人手套、一次性道具）请直接删掉。
        </p>
        <label class="replica-field">
            <span class="replica-field-label">场景一句话（模型写的整体基调，第一帧成立、最后一帧仍成立）</span>
            <textarea class="replica-textarea" rows="2" id="replica-scene-signature"
                      placeholder="例：一座长着青苔的混凝土掩体，位于秋日林地，靠一盏三脚架工作灯照明"
                >${escapeHtmlReplica(signature)}</textarea>
        </label>
        ${arrayRow}
        <div class="replica-beat-fields">${rows}</div>
    </div>`;
}

// 定长时间窗：与节拍并列的第二套读法。
//
// 节拍是模型按「生产里程碑」切的，宽窄不一；这一条按固定 5 秒切，只讲画面里多了
// 什么、少了什么。两者对不上的地方就是要看帧的地方——某一窗有明显增减、而覆盖它的
// 那一拍只字未提，那多半是漏掉了一道工序。全部由帧事实本地统计得来，不花模型钱。
function replicaRenderTimeWindows(doc) {
    const windows = doc.time_windows || [];
    if (!windows.length) return '';

    const tag = (items, cls, label) => (items || []).length
        ? `<span class="replica-win-tag ${cls}">${label}</span>` +
          (items || []).map(x => `<span class="replica-win-item">${escapeHtmlReplica(x)}</span>`).join('')
        : '';

    const rows = windows.map(w => {
        const changed = (w.appeared || []).length + (w.vanished || []).length
            + (w.brief || []).length;
        return `
        <div class="replica-win-row${changed ? '' : ' replica-win-quiet'}">
            <span class="replica-win-time">${w.start}–${w.end}s</span>
            <span class="replica-win-meta">${w.frame_count} 帧${
                w.workers_present_ratio >= 0.5 ? '' : ' · 多为空镜'}</span>
            <span class="replica-win-body">
                ${tag(w.baseline, 'is-base', '起始')}
                ${tag(w.appeared, 'is-new', '新增')}
                ${tag(w.vanished, 'is-gone', '消失')}
                ${tag(w.brief, 'is-brief', '仅此窗')}
                ${changed ? '' : '<span class="replica-hint">画面无显著增减</span>'}
            </span>
        </div>`;
    }).join('');

    return `
    <details class="replica-windows">
        <summary>画面变化时间线（每 ${windows[0].end - windows[0].start}s 一格，共 ${windows.length} 格）</summary>
        <p class="replica-hint">
            由帧事实本地统计，不花模型调用。与上方节拍对照着看：某一格有明显增减、而覆盖它的那一拍
            只字未提，多半是漏掉了一道工序。「消失」比「新增」噪声大——同一样东西换个说法就会被算成消失，
            以画面为准。
        </p>
        <div class="replica-win-list">${rows}</div>
    </details>`;
}

// 这一拍的空间标记。过门是复刻里最容易整段丢掉的东西——原片走廊尽头那道门再进一次，
// 复刻里可能一次都没进（2026-08-14 复盘）。合成期按 space 序列逐处标过门，所以这里把
// 「本拍换空间了」直接写在卡片头上：用户核对时看得见有没有多、有没有少。
// 原片这一拍由几个镜头组成（reverse.attach_shot_cuts 派生自 video_overview.json 的
// cut_points）。字段缺席 = 未知（老 job / 二创变体 / 抽帧异常），什么都不显示——显示
// 一个「一镜」比不显示更误导。多镜头链路按这个数挑三镜梯还是四镜梯。
// 整条阶梯的剪辑节奏概览。与 reverse.observed_shot_stats 同口径（那一份给合成前的
// 偏差告警用，这一份给卡点上的人看），任一侧改判据都要同时改另一侧。
function replicaShotRhythmLine(doc) {
    const beats = (doc.beats || []).filter(b => typeof b.observed_shot_count === 'number');
    if (!beats.length) return '';
    const counts = beats.map(b => b.observed_shot_count);
    const cuts = beats.reduce((n, b) => n + ((b.observed_cuts || []).length), 0);
    const span = beats.reduce((n, b) => n + Math.max(0, (b.end || 0) - (b.start || 0)), 0);
    const lens = beats.map(b => b.observed_shot_seconds).filter(x => typeof x === 'number' && x > 0);
    const avg = (counts.reduce((a, c) => a + c, 0) / counts.length).toFixed(2);
    // 报**切点率与镜长**，不报「几拍是一镜」。原片一拍三秒半、交付一拍八秒，
    // 按镜头数比的是两个拍长不同的东西：实测一条 77 秒片，原片 0.26 刀/秒、
    // 交付三镜 0.25 刀/秒，节奏其实是对上的，而按镜头数会把 7 拍误标成偏差。
    const rate = span > 0 ? (cuts / span).toFixed(2) : null;
    const avgLen = lens.length ? (lens.reduce((a, c) => a + c, 0) / lens.length).toFixed(1) : null;
    const parts = [`${beats.length} 拍内共 ${cuts} 刀，平均 ${avg} 镜/拍，最多 ${Math.max(...counts)} 镜`];
    if (rate) parts.push(`拍内切点率 ${rate} 刀/秒`);
    if (avgLen) parts.push(`平均每镜 ${avgLen}s`);
    return `<p class="replica-hint">🎬 原片剪辑节奏：${parts.join('；')}。交付会把每一拍拉到固定片长，
        所以能对齐的是<b>切点率</b>而不是逐拍镜头数——镜长与原片差一倍以上的拍会在合成日志里单独点名。</p>`;
}

function replicaShotCutChip(beat) {
    const count = beat && beat.observed_shot_count;
    if (typeof count !== 'number' || count < 1) return '';
    const cuts = Array.isArray(beat.observed_cuts) ? beat.observed_cuts : [];
    const marks = cuts.length ? `切点 ${cuts.map(t => `${t}s`).join(' / ')}` : '内部无切点';
    const secs = beat.observed_shot_seconds;
    // 镜长必须跟镜头数一起给：一拍三秒半里的「一镜」和八秒交付里的「三镜」，
    // 每镜时长其实是同一个量级。只看镜头数会把前者误读成节奏对不上。
    const len = (typeof secs === 'number' && secs > 0) ? ` · 每镜 ${secs}s` : '';
    const cls = count > 1 ? ' replica-chip-multishot' : '';
    const label = count > 1 ? `原片 ${count} 镜` : '原片一镜';
    return `<span class="replica-chip${cls}" title="原片这一拍在 ${beat.start}s – ${beat.end}s 内${marks}。镜头梯把它夹进合法区间（三镜/四镜）：原片三镜就排三镜、四镜就排四镜，一到两镜排三镜下限。">🎬 ${label}${len}</span>`;
}

function replicaSpaceChip(beat, idx, previousSpace) {
    const space = String(beat.space || '').trim();
    if (!space) return '';
    const previous = String(previousSpace || '').trim();
    const crossed = idx > 0 && previous && previous.toLowerCase() !== space.toLowerCase();
    return crossed
        ? `<span class="replica-chip replica-chip-cross">过门 → ${escapeHtmlReplica(space)}</span>`
        : `<span class="replica-chip">${escapeHtmlReplica(space)}</span>`;
}

function replicaRenderBeatCard(state, beat, idx, previousSpace) {
    const frames = beat.evidence_frames || beat.reference_frames || [];
    const isRef = !beat.evidence_frames && (beat.reference_frames || []).length;
    // 证据帧原地开灯箱，不再 target="_blank"。核对是「看一眼帧、回来改这一拍」的
    // 来回动作，每看一帧就多一个标签页，用户得自己收拾一地窗口才能回到编辑器。
    // 灯箱走全局的那一份（js/lightbox.js）：点空白处 / Esc / 关闭键都能返回，
    // 同一拍的多张帧还能左右翻。
    // 缩略图外面包一枚 button。此前它是裸 <img>：能点、能开灯箱，但键盘走不到——
    // 整页 152 个可点元素里有 120 个是这种（另 30 个是旧时间轴的 <div>）。这一页的
    // 校验横幅早就把「可点的东西是 button」做对了，缩略图只是漏了。
    // data-* 跟着搬到 button 上，绑定那边按属性查、从同一个元素读 dataset，不受影响。
    const thumbs = frames.map((name, at) => `
        <button type="button" class="replica-thumb-btn"
                data-lightbox-beat="${idx}" data-lightbox-at="${at}"
                title="${escapeHtmlReplica(name)}"
                aria-label="放大证据帧 ${at + 1}／${frames.length}：${escapeHtmlReplica(name)}">
            <img class="replica-thumb" src="${replicaFrameUrl(state, name)}"
                 alt="" loading="lazy">
        </button>`).join('');

    // 覆盖帧：按时间均分铺满整个拍窗（后端 attach_coverage_frames 算好的）。证据帧
    // 最多三张，一拍 10s 的窗光看那三张等于中段全黑；这一排是拿来看「这段时间里
    // 到底发生了什么」的，不参与任何判据，所以标注时间、但不做成可编辑字段。
    const coverage = beat.coverage_frames || [];
    const coverageThumbs = coverage.map((item, at) => `
        <figure class="replica-cov-item">
            <button type="button" class="replica-thumb-btn"
                    data-cov-beat="${idx}" data-cov-at="${at}"
                    title="${escapeHtmlReplica(item.frame)}"
                    aria-label="放大覆盖帧 ${item.timestamp}s：${escapeHtmlReplica(item.frame)}">
                <img class="replica-thumb replica-thumb-cov" src="${replicaFrameUrl(state, item.frame)}"
                     alt="" loading="lazy">
            </button>
            <figcaption class="replica-cov-time">${item.timestamp}s</figcaption>
        </figure>`).join('');

    // 中文对照：反推产出的是英文（下游提示词、相位判定、banned 门禁读的都是它），
    // 但人工卡点是给人看的。zh 只在这里显示，永远不回写英文字段。
    const zh = beat.zh || {};
    const mirror = (key) => {
        const value = zh[key];
        const text = Array.isArray(value) ? value.join(' / ') : value;
        return text ? `<span class="replica-field-zh">${escapeHtmlReplica(text)}</span>` : '';
    };

    // 条数徽章。以前这是说明文里的一句话（「须 3~6 条；当前 4 条」），越界了也还是
    // 同一行灰字——是说明，不是状态。拆出来放右上角，越界就变红，扫一眼卡片能看出
    // 哪一栏没写够。
    const countBadge = (key, meta) => {
        if (!meta.count) return '';
        const [min, max] = meta.count;
        const n = Array.isArray(beat[key])
            ? beat[key].length
            : String(beat[key] || '').split('\n').map(s => s.trim()).filter(Boolean).length;
        const bad = (min && n < min) || (max && n > max);
        const rule = min && max ? `须 ${min}~${max} 条` : (min ? `须 ≥${min} 条` : `≤${max} 条`);
        return `<span class="replica-field-count${bad ? ' is-bad' : ''}"
                      title="${rule}（当前 ${n} 条）">${n}${max ? `/${max}` : ''}</span>`;
    };

    // 一格字段：短名 + ⓘ + 条数徽章 + 输入框 + 中文对照。
    // 外层不再是 <label>——ⓘ 是个 button，交互元素嵌在 label 里既不合法，点它还会
    // 顺带把焦点扔进 textarea。改成 div + for/id 显式关联，可访问性没有丢。
    // helpOverride：大环境识别项按「首拍 / 过门拍 / 其余」三种情形说三句不同的话，
    // 那三句话本来就是写死在 label 里的，这里保留同一套措辞，只是换个位置出现。
    const field = (key, helpOverride) => {
        const meta = REPLICA_FIELD_META[key] || { name: key, help: '', rows: 2 };
        const help = helpOverride || meta.help || '';
        const id = `replica-f-${idx}-${key}`;
        return `
        <div class="replica-field">
            <div class="replica-field-top">
                <label class="replica-field-label" for="${id}">${escapeHtmlReplica(meta.name)}</label>
                ${help ? `<button type="button" class="replica-field-help" tabindex="-1"
                        data-help="${escapeHtmlReplica(help)}"
                        aria-label="${escapeHtmlReplica(`${meta.name}：${help}`)}">?</button>` : ''}
                ${countBadge(key, meta)}
            </div>
            <textarea class="replica-textarea" id="${id}" rows="${meta.rows || 2}"
                      data-beat="${idx}" data-key="${key}"
                >${escapeHtmlReplica(Array.isArray(beat[key]) ? beat[key].join('\n') : beat[key])}</textarea>
            ${mirror(key)}
        </div>`;
    };

    // 闭集字段。收下拉不收输入框，理由见 REPLICA_SHOT_SCALES 上方那段注释。
    // 一枚胶囊 = 一个「短名：值」，横向流式排；不再一人占一整格。
    const paramPill = (key, label, options, title) => {
        const current = String(beat[key] || '');
        return `
        <label class="replica-param" title="${escapeHtmlReplica(title || label)}">
            <span class="replica-param-label">${escapeHtmlReplica(label)}</span>
            <select class="replica-select" data-beat="${idx}" data-key="${key}">
                ${options.map(([value, text]) => `
                    <option value="${escapeHtmlReplica(value)}"${value === current ? ' selected' : ''}
                        >${escapeHtmlReplica(text)}</option>`).join('')}
            </select>
        </label>`;
    };

    // 人数。空着 = 没标注（保留 workers_present 那枚布尔芯片的旧口径）；填了数就以数为准，
    // 后端 normalize_beat_craft_fields 会据此回写 workers_present，两处不会打架。
    const workerPill = () => `
        <label class="replica-param" title="画面里有几个人。0＝清场帧（锚点候选）；空着＝没标注">
            <span class="replica-param-label">工人数</span>
            <input class="replica-number" type="number" min="0" max="12" step="1"
                   data-beat="${idx}" data-key="worker_count" data-num="1"
                   value="${typeof beat.worker_count === 'number' ? beat.worker_count : ''}">
        </label>`;

    // 卡片**内部**那几块 <details> 的开合。此前完全没记过，而 replicaRefreshBeats 会在
    // 每次保存、每次拆合拍、每次 AI 动作跑完之后重建整个节拍区——展开的可选字段全部
    // 啪地合上，用户每存一次就得再翻一遍。
    // 与 replicaBeatFoldState（整卡片折叠）分开存，但清空时机必须一致：见 replicaSplitBeat。
    const foldAttrs = (name, defaultOpen) => {
        const key = `${beat.id}:${name}`;
        const remembered = replicaFieldFoldState[key];
        const open = remembered === undefined ? defaultOpen : remembered;
        return `data-fold-key="${escapeHtmlReplica(key)}"${open ? ' open' : ''}`;
    };

    const violations = state.validation || (state.beats && state.beats.validation) || [];
    const isErr = violations.some(v => v.level === 'error' && v.beat_id === beat.id);
    const isMobile = typeof window !== 'undefined' && typeof window.innerWidth === 'number' && window.innerWidth <= 768;
    let isCollapsed = isMobile ? (!isErr) : false;
    if (replicaBeatFoldState[beat.id] !== undefined) {
        isCollapsed = !!replicaBeatFoldState[beat.id];
    }

    const s_dur = (typeof beat.screen_duration_sec === 'number') ? beat.screen_duration_sec : Math.max(0.1, Math.round(((beat.end || 0) - (beat.start || 0)) * 10) / 10);
    const speed = (state.beats && state.beats.speed_multiplier) || beat.speed_factor || 2.0;
    const a_dur = (typeof beat.action_duration_sec === 'number') ? beat.action_duration_sec : Math.round(s_dur * speed * 10) / 10;
    const quota = beat.voiceover_quota || { max_words: Math.floor(s_dur * 0.8 * 4.2), silence_sec: Math.round(s_dur * 0.2 * 10) / 10 };

    const space = String(beat.space || '').trim();
    const previous = String(previousSpace || '').trim();
    const isCrossed = idx > 0 && previous && space && previous.toLowerCase() !== space.toLowerCase();
    const isFirstBeat = idx === 0;
    const isThreshold = beat.stage === 'transition' || isCrossed;
    const hasMacroEnv = Array.isArray(beat.macro_environment)
        ? beat.macro_environment.length > 0
        : Boolean(String(beat.macro_environment || '').trim());

    // 大环境识别项在三种情形下说三句不同的话，措辞与此前逐字一致，只是从常驻 label
    // 挪进了 ⓘ。第四种情形（非首拍且空着）仍然整块收进「更多」，见下面 moreFields。
    const macroEnvHelp = isFirstBeat
        ? '锚点首拍必填：地貌水体、气候光照、空间包络；一行一条。只写这地方本来长什么样，本拍挖出来/砌起来的东西写进起始状态。'
        : (isThreshold
            ? '过门新空间首拍：新空间地貌、气候光照、空间三维包络；一行一条。不写本拍施工产物。'
            : '非首拍/过门拍建议留空以减少大模型干扰；一行一条。');
    const macroEnvVisible = isFirstBeat || isThreshold || hasMacroEnv;
    const macroEnvField = macroEnvVisible ? field('macro_environment', macroEnvHelp) : '';

    // 「更多」里装的是本拍多半不用动的字段：工艺规格三件套、工具型号、物料去向，
    // 外加两个派生/兜底字段。此前它们各自写了一段 if-else 散在这个函数里，视觉上
    // 也和必填项一模一样重，占掉整整一排格子。
    //
    // 什么时候默认展开：本拍在这些字段上有值、或有值越界、或本拍有硬伤。
    // 「有值却被折起来」是最坏的一种折叠——人会以为那一栏是空的。
    const moreKeys = ['material_specs', 'fastening_and_bonding', 'micro_traces',
                      'tool_specifics', 'material_flow', 'insert_subject', 'visual_subject'];
    // 原片这一拍切过镜头，插入镜主体就不是可选项了，提到上面「拍摄与声音」那组里去。
    const insertIsPrimary = typeof beat.observed_shot_count === 'number' && beat.observed_shot_count >= 2;
    if (insertIsPrimary) moreKeys.splice(moreKeys.indexOf('insert_subject'), 1);
    if (!macroEnvVisible) moreKeys.unshift('macro_environment');
    // 清场帧里没人可写，但「有人偶在旁观」仍然要写——所以是收起来，不是拿掉。
    if (!beat.workers_present) moreKeys.push('cast_action');

    const hasValue = (key) => (Array.isArray(beat[key])
        ? beat[key].length > 0
        : Boolean(String(beat[key] === undefined || beat[key] === null ? '' : beat[key]).trim()));
    const filledCount = moreKeys.filter(hasValue).length;
    const moreOpen = isErr || filledCount > 0;
    const moreFields = `
        <details class="replica-field-more" ${foldAttrs('more', moreOpen)}>
            <summary class="replica-hint">工艺规格与兜底字段（${moreKeys.length} 项，已填 ${filledCount} 项）</summary>
            <div class="replica-beat-fields">${moreKeys.map(k => field(k)).join('')}</div>
        </details>`;

    // 头部此前是八枚同权重的圆角灰 chip（时间/时序/阶段/事件/工人/空间/原片镜数/低置信）。
    // 里面真正要抢眼的只有一件事——这一拍有没有问题——它却和「事件 E04」长得一模一样。
    // 现在头部只留「是哪一拍、多长、有没有事」，其余降到下面一行浅字元信息里；
    // 元信息里只有异常项（过门、低置信、多镜）才会被染色。
    const beatWarn = violations.some(v => v.level !== 'error' && v.beat_id === beat.id);
    const lowConf = typeof beat.confidence === 'number' && beat.confidence < 0.5;
    const statusCls = isErr ? 'is-error' : ((beatWarn || lowConf) ? 'is-warn' : 'is-ok');
    const statusTitle = isErr ? '本拍有硬伤，必须先修掉才能合成'
        : (beatWarn ? '本拍有待人工确认项' : (lowConf ? '模型对本拍的置信度低于 0.5' : '本拍通过全部机械校验'));

    return `
    <div class="replica-beat ${isCollapsed ? 'is-collapsed' : ''}" data-beat-id="${escapeHtmlReplica(beat.id)}" data-beat-index="${idx}">
        <div class="replica-beat-head">
            <button type="button" class="replica-beat-fold-btn" data-beat-fold="${escapeHtmlReplica(beat.id)}" title="折叠/展开本拍">
                ${isCollapsed ? '▾' : '⌃'}
            </button>
            <b class="replica-beat-id">${escapeHtmlReplica(beat.id)}</b>
            <span class="replica-time-chip" title="点击箭头微调时间，或在右侧拆拍/合并">
                <button type="button" class="replica-nudge-btn" data-nudge-beat="${idx}" data-nudge-field="start" data-nudge-delta="-0.5" title="起始时间提早 0.5 秒">◀</button>
                <b class="replica-time-val">${beat.start}s – ${beat.end}s</b>
                <button type="button" class="replica-nudge-btn" data-nudge-beat="${idx}" data-nudge-field="end" data-nudge-delta="+0.5" title="结束时间延后 0.5 秒">▶</button>
            </span>
            <span class="replica-beat-dot ${statusCls}" title="${escapeHtmlReplica(statusTitle)}"></span>
            <span class="replica-beat-timing" title="2x 倍速成片时序：屏幕时长 ${s_dur}s 对应 1.0x 真实物理动作 ${a_dur}s，旁白建议不超过 ${quota.max_words} 字">${s_dur}s · 2x 实拍 ${a_dur}s · 旁白 ≤${quota.max_words} 字</span>
            <span class="replica-beat-tools">
                <button type="button" class="replica-mini-btn" data-zh-toggle="1"
                        title="中文对照只供核对，英文才是送去合成的事实源">中文对照</button>
                <button type="button" class="replica-mini-btn" data-split="${idx}" title="从中点拆成两拍">拆拍</button>
                <button type="button" class="replica-mini-btn" data-merge="${idx}" ${idx === 0 ? 'disabled' : ''}
                        title="并入上一拍">上并</button>
            </span>
        </div>
        <div class="replica-beat-meta">
            ${replicaStageSelect(beat, idx)}
            ${replicaSpaceChip(beat, idx, previousSpace)}
            ${replicaShotCutChip(beat)}
            <span class="replica-meta-item">${beat.workers_present ? '有工人' : '清场帧（锚点候选）'}</span>
            ${beat.source_event_ids && beat.source_event_ids.length
                ? `<span class="replica-meta-item">事件 ${escapeHtmlReplica(beat.source_event_ids.join(','))}</span>` : ''}
            ${lowConf ? '<span class="replica-meta-item is-flag">低置信</span>' : ''}
        </div>
        <div class="replica-beat-summary">
            <span class="replica-summary-tag"><b>${escapeHtmlReplica(beat.id)}</b> · ${beat.start}–${beat.end}s</span>
            <span class="replica-summary-tag"><b>阶段:</b> ${escapeHtmlReplica(REPLICA_BEAT_STAGE_LABELS[beat.stage] || beat.stage || '—')}</span>
            <span class="replica-summary-tag"><b>主体:</b> ${escapeHtmlReplica(beat.visual_subject || '—')}</span>
            <span class="replica-summary-tag"><b>工序:</b> ${escapeHtmlReplica(beat.operation || (beat.package_operations || [])[0] || '—')}</span>
            ${beat.space ? `<span class="replica-summary-tag"><b>空间:</b> ${escapeHtmlReplica(beat.space)}</span>` : ''}
        </div>
        <div class="replica-beat-body">
            <div class="replica-thumbs">${thumbs || '<span class="replica-hint">无证据帧</span>'}</div>
            ${isRef ? '<p class="replica-hint">变体：这些帧只作运镜与构图参考，不再是事实断言。</p>' : ''}
            ${coverage.length ? `
            <details class="replica-coverage" ${foldAttrs('coverage', beat.end - beat.start >= 6)}>
                <summary class="replica-hint">覆盖帧 ${coverage.length} 张（${beat.start}s – ${beat.end}s 内按时间均分，仅供核对）</summary>
                <div class="replica-cov-strip">${coverageThumbs}</div>
            </details>` : ''}
            <div class="replica-field-group" data-group="fact">
                <div class="replica-group-title">画面事实</div>
                <div class="replica-beat-fields">
                    ${field('space')}
                    ${macroEnvField}
                    ${field('operation')}
                    ${field('package_operations')}
                    ${field('visible_details')}
                    ${field('visible_action')}
                    ${beat.workers_present ? field('cast_action') : ''}
                </div>
            </div>
            <div class="replica-field-group" data-group="state">
                <div class="replica-group-title">状态与痕迹</div>
                <div class="replica-beat-fields">
                    ${field('visible_result')}
                    ${field('state_before')}
                    ${field('state_after')}
                    ${field('persistent_traces')}
                    ${field('light_state')}
                </div>
            </div>
            <div class="replica-field-group" data-group="shot">
                <div class="replica-group-title">拍摄与声音</div>
                <div class="replica-beat-params">
                    ${REPLICA_SHOT_PARAMS.map(([key, label, options, title]) =>
                        paramPill(key, label, options, title)).join('')}
                    ${workerPill()}
                </div>
                <div class="replica-beat-fields">
                    ${field('tool')}
                    ${field('sfx')}
                    ${field('subject_placement')}
                    ${insertIsPrimary ? field('insert_subject') : ''}
                </div>
            </div>
            ${moreFields}
        </div>
    </div>`;
}

// 命中词高亮。**一次扫描原文**，命中段包 <mark>、其余段照常转义。
// 不能"先整体转义、再拿每个命中词在结果串上反复 replace"：第二个词会匹配进第一个
// <mark> 标签的属性里（禁用词恰好叫 mark / data / class / hit 就会发生），把 HTML
// 结构撑坏。词边界口径与服务端 reverse.banned_element_hits 对齐——只有紧挨边界的是
// 词字符时才加 \b，这样中文词也能正常命中。
function replicaHighlightPromptBlock(text, hits) {
    if (!text) return '';
    const raw = String(text);
    const names = [];
    const parts = [];
    (hits || []).forEach(hit => {
        const name = String(hit || '').trim();
        if (!name) return;
        const esc = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const prefix = /^[a-zA-Z0-9_]/.test(name) ? '\\b' : '';
        const suffix = /[a-zA-Z0-9_]$/.test(name) ? '\\b' : '';
        names.push(name);
        parts.push(`(${prefix}${esc}${suffix})`);
    });
    if (!parts.length) return escapeHtmlReplica(raw);

    let regex;
    try {
        regex = new RegExp(parts.join('|'), 'gi');
    } catch (e) {
        return escapeHtmlReplica(raw);
    }

    let out = '';
    let last = 0;
    let counter = 0;
    let m;
    while ((m = regex.exec(raw)) !== null) {
        if (m[0] === '') { regex.lastIndex++; continue; }
        out += escapeHtmlReplica(raw.slice(last, m.index));
        const gi = m.slice(1).findIndex(g => g !== undefined);
        const name = gi >= 0 ? names[gi] : m[0];
        out += `<mark class="replica-banned-mark" id="replica-hit-mark-${counter++}"`
             + ` data-hit="${escapeHtmlReplica(name)}">${escapeHtmlReplica(m[0])}</mark>`;
        last = m.index + m[0].length;
    }
    out += escapeHtmlReplica(raw.slice(last));
    return out;
}

function replicaRenderOutput(state) {
    if (!state.prompt_block) return '';
    const hits = state.banned_hits || [];
    const blocked = state.stage === 'audit_failed';
    const highlightedHtml = replicaHighlightPromptBlock(state.prompt_block, hits);

    return `
    <div class="replica-section" id="replica-sec-output">
        <div class="replica-card-title">提示词包${state.title ? ` · ${escapeHtmlReplica(state.title)}` : ''}</div>
        ${blocked ? `<div class="replica-banner replica-banner-error">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
                <div>
                    <b>已拦截交付：命中 ${hits.length} 个禁用元素</b>（原片里并不存在）：
                    ${hits.map(h => `<span class="replica-hit-badge">${escapeHtmlReplica(h)}</span>`).join(' ')}。
                </div>
                <div style="display:flex; gap:8px;">
                    <button type="button" id="replica-purge-banned-btn" class="action-btn primary-btn" style="padding:4px 12px; font-size:12px; background:#ff4d4f; border-color:#ff7875;" title="自动从提示词中剥离所有命中词并重新校验">🪄 一键自动剔除并交付</button>
                </div>
            </div>
            <div style="margin-top:8px; font-size:12.5px; line-height:1.6;">
                这份提示词<b>没有写入创意库</b>，也不能送去渲染。你可以：<br>
                1. 📍 <b>点击下方按钮快速定位到问题点，就地编辑删除该词并保存</b>；<br>
                2. 🪄 <b>点击右上角「一键自动剔除并交付」一秒完成修复</b>；<br>
                3. 或者点击「重新合成」让模型重新写一份。
            </div>
            ${hits.length > 0 ? `
            <div style="margin-top:10px; display:flex; align-items:center; flex-wrap:wrap; gap:6px;">
                <span style="font-size:12px; font-weight:700;">📍 问题跳转：</span>
                ${hits.map(h => `<button type="button" class="replica-jump-hit-btn" data-hit-name="${escapeHtmlReplica(h)}" title="点击直接滚动定位并高亮闪烁该词">定位到「${escapeHtmlReplica(h)}」📍</button>`).join('')}
            </div>` : ''}
        </div>` : `<div class="replica-banner replica-banner-ok">
            已通过禁用元素门禁，并写入创意库${state.library_id
                ? `（项目工作台可见：${escapeHtmlReplica(state.title || state.library_id)}）` : ''}。
            下一步按「存入项目并打开激发结果」：渲染（分步合成 / 一键合成）、手动改提示词、
            补主题与话题，都在那一页上。
        </div>`}
        <div class="replica-actions" style="display:flex; align-items:center; flex-wrap:wrap; gap:8px;">
            <button type="button" id="replica-copy-btn" class="action-btn text-btn">复制全部</button>
            <button type="button" id="replica-toggle-edit-btn" class="action-btn ${replicaPromptEditing ? 'primary-btn' : 'text-btn'}">${replicaPromptEditing ? '👁️ 返回预览' : '✏️ 手动修改提示词'}</button>
            ${replicaPromptEditing ? `<button type="button" id="replica-save-prompt-btn" class="action-btn primary-btn" style="background:#52c41a; border-color:#73d13d;">💾 保存修改并重新校验</button>` : ''}
            <button type="button" id="replica-recompose-btn" class="action-btn text-btn"
                    title="不重跑聚类，直接重新合成提示词">重新合成</button>
            ${blocked
                ? ''
                : `<button type="button" id="replica-project-btn" class="action-btn primary-btn"
                           title="把这份已过门禁的提示词存成一个项目，并立刻打开它的激发结果页">存入项目并打开激发结果</button>`}
        </div>
        ${replicaPromptEditing
            ? `<textarea class="replica-prompt-editor" id="replica-prompt-editor" rows="18">${escapeHtmlReplica(state.prompt_block)}</textarea>`
            : `<pre class="replica-prompt-block" id="replica-prompt-block">${highlightedHtml}</pre>`}
    </div>`;
}

/* --- 事件绑定 --- */

function replicaBindEvents() {
    const root = replicaRoot();
    if (!root) return;

    const on = (sel, fn, evt = 'click') => {
        const el = typeof sel === 'string' ? root.querySelector(sel) : sel;
        if (el) el.addEventListener(evt, fn);
    };

    on('#replica-upload-btn', replicaUpload);
    on('#replica-start-btn', replicaStart);
    on('#replica-reextract-btn', replicaReExtract);
    on('#replica-toggle-extract-btn', () => {
        replicaExtractExpanded = !replicaExtractExpanded;
        replicaRender();
    });
    // 模型选择改一下就落盘，不必等到点「开始反推」
    on('#replica-frame-model', replicaCaptureReverseSettings, 'change');
    on('#replica-peak-model', replicaCaptureReverseSettings, 'change');
    on('#replica-recompose-btn', replicaCompose);
    on('#replica-project-btn', (e) => replicaSaveToProject(e.currentTarget));
    on('#replica-cancel-btn', replicaCancelRun);
    on('#replica-copy-btn', () => {
        const block = root.querySelector('#replica-prompt-block') || root.querySelector('#replica-prompt-editor');
        if (block) {
            const val = block.value !== undefined ? block.value : block.textContent;
            navigator.clipboard.writeText(val).then(
                () => replicaToast('已复制'), () => replicaToast('复制失败', true));
        }
    });

    // 提示词编辑切换与保存
    on('#replica-toggle-edit-btn', () => {
        replicaPromptEditing = !replicaPromptEditing;
        replicaRender();
    });

    on('#replica-save-prompt-btn', async () => {
        const editor = root.querySelector('#replica-prompt-editor');
        if (!editor || !replicaState) return;
        const newText = editor.value.trim();
        if (!newText) {
            replicaToast('提示词内容不能为空', true);
            return;
        }
        try {
            const data = await replicaFetch('/api/replica/save_prompt', {
                method: 'POST',
                headers: replicaHeaders(),
                body: JSON.stringify({ job_id: replicaState.job_id, prompt_block: newText }),
            });
            if (data && data.job_state) {
                replicaState = data.job_state;
                replicaPromptEditing = false;
                replicaRender();
                if (replicaState.stage === 'completed') {
                    replicaToast('✅ 提示词修改已保存并通过门禁校验！已自动写入创意库。');
                } else {
                    replicaToast(`⚠️ 保存成功，但仍命中 ${ (replicaState.banned_hits || []).length } 个禁用元素`, true);
                }
            }
        } catch (err) {
            replicaToast(`保存失败: ${err.message}`, true);
        }
    });

    // 一键剥离禁用词
    on('#replica-purge-banned-btn', async () => {
        if (!replicaState) return;
        try {
            const data = await replicaFetch('/api/replica/purge_banned', {
                method: 'POST',
                headers: replicaHeaders(),
                body: JSON.stringify({ job_id: replicaState.job_id }),
            });
            if (data && data.job_state) {
                replicaState = data.job_state;
                replicaPromptEditing = false;
                replicaRender();
                if (replicaState.stage === 'completed') {
                    replicaToast('🪄 已自动剔除所有禁用元素并校验通过！已可交付/送去渲染。');
                } else {
                    replicaToast('已尝试自动清理，仍有部分词需手动确认', true);
                }
            }
        } catch (err) {
            replicaToast(`自动清理失败: ${err.message}`, true);
        }
    });

    // 问题位置定位跳转
    root.querySelectorAll('.replica-jump-hit-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const hitName = btn.dataset.hitName;
            if (replicaPromptEditing) {
                replicaPromptEditing = false;
                replicaRender();
            }
            const marks = Array.from(root.querySelectorAll(`.replica-banned-mark[data-hit="${hitName}"]`));
            if (!marks.length) {
                replicaToast(`未在文本中找到「${hitName}」`, true);
                return;
            }
            const target = marks[0];
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            marks.forEach(m => {
                m.classList.add('replica-hit-pulse');
                setTimeout(() => m.classList.remove('replica-hit-pulse'), 3500);
            });
            replicaToast(`已定位到「${hitName}」所在位置 📍`);
        });
    });

    // 快捷任务切换与新建
    on('#replica-quick-job-select', async (e) => {
        const jid = e.target.value;
        if (jid) {
            try {
                await replicaLoadJob(jid);
                replicaRender();
            } catch (err) { replicaToast(err.message, true); }
        }
    }, 'change');

    on('#replica-new-upload-btn', () => replicaFocusSection('replica-sec-uploader'));

    // 任务列表的展开态要跨重渲染活下来，否则展开它、点开一条任务、页面重画，它又收上了。
    on('#replica-sec-jobs', (e) => { replicaJobListExpanded = e.currentTarget.open; }, 'toggle');

    // 同理：拼贴图的展开态也要跨重渲染活下来。
    on('#replica-collage-fold', (e) => { replicaCollageExpanded = e.currentTarget.open; }, 'toggle');
    on('#replica-ladder-chips-fold', (e) => { replicaLadderChipsOpen = e.currentTarget.open; }, 'toggle');

    // 阶段指示条点击跳转：四个用户可见阶段各对应页面上真实存在的一块区域。
    root.querySelectorAll('.replica-phase[data-phase]').forEach(li => {
        li.addEventListener('click', () => {
            const phase = li.dataset.phase;
            let targetId = '';
            if (phase === 'material') {
                targetId = document.getElementById('replica-sec-extract') ? 'replica-sec-extract' : 'replica-sec-uploader';
            } else if (phase === 'reverse') {
                targetId = document.getElementById('replica-progress') && replicaSSE ? 'replica-progress' : 'replica-sec-extract';
            } else if (phase === 'review') {
                targetId = 'replica-sec-beats';
            } else if (phase === 'deliver') {
                targetId = 'replica-sec-output';
            }
            replicaFocusSection(targetId);
        });
    });

    replicaBindNavEvents();
    replicaBindBottomBarEvents();

    // 悬浮直达工具
    root.querySelectorAll('[data-float-action="top"]').forEach(btn => {
        btn.addEventListener('click', () => {
            const shell = replicaShell();
            if (shell) shell.scrollTo({ top: 0, behavior: 'smooth' });
            else window.scrollTo({ top: 0, behavior: 'smooth' });
        });
    });
    root.querySelectorAll('[data-float-action="save"]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            replicaSaveBeats(true, e.currentTarget);
        });
    });
    on('#replica-bar-recompose-btn', (e) => replicaCompose(e.currentTarget));
    on('#replica-bar-cancel-btn', replicaCancelRun);

    root.querySelectorAll('.replica-job-open').forEach(btn => {
        btn.addEventListener('click', async () => {
            try {
                await replicaLoadJob(btn.dataset.job);
                replicaRender();
                if (!replicaFocusSection('replica-sec-variant')) replicaFocusSection('replica-sec-current-job');
            } catch (e) { replicaToast(e.message, true); }
        });
    });

    root.querySelectorAll('[data-rename], [data-rename-current]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const jobId = btn.dataset.rename || btn.dataset.renameCurrent;
            const currentJob = replicaJobs.find(j => j.job_id === jobId) || (replicaState && replicaState.job_id === jobId ? replicaState : {}) || {};
            const oldTitle = currentJob.title || currentJob.video_name || jobId;
            const newTitle = window.prompt('请输入新的任务标题（保存后将锁定）：', oldTitle);
            if (!newTitle || !newTitle.trim() || newTitle.trim() === oldTitle) return;
            try {
                await replicaFetch('/api/replica/rename', {
                    method: 'POST', headers: replicaHeaders(),
                    body: JSON.stringify({ job_id: jobId, title: newTitle.trim() }),
                });
                if (replicaState && replicaState.job_id === jobId) {
                    replicaState.title = newTitle.trim();
                    replicaState.title_locked = true;
                }
                await replicaLoadJobs();
                replicaRender();
                replicaToast('任务已重命名并锁定');
            } catch (err) { replicaToast(err.message, true); }
        });
    });

    root.querySelectorAll('[data-archive], [data-archive-current]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const jobId = btn.dataset.archive || btn.dataset.archiveCurrent;
            if (!window.confirm('确认瘦身归档此任务？\n将清理所有高清抽帧、过程拼图与原始视频（释放数百 MB 磁盘），保留核心节拍骨架与提示词资产。')) return;
            try {
                await replicaFetch('/api/replica/archive', {
                    method: 'POST', headers: replicaHeaders(),
                    body: JSON.stringify({ job_id: jobId }),
                });
                if (replicaState && replicaState.job_id === jobId) {
                    await replicaLoadJob(jobId);
                }
                await replicaLoadJobs();
                replicaRender();
                replicaToast('任务已完成瘦身归档');
            } catch (err) { replicaToast(err.message, true); }
        });
    });

    root.querySelectorAll('[data-toggle-variants]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const parentId = btn.dataset.toggleVariants;
            const currentlyFolded = replicaVariantFoldState[parentId] !== false;
            replicaVariantFoldState[parentId] = !currentlyFolded;
            replicaRender();
        });
    });

    const searchInput = root.querySelector('#replica-job-search-input');
    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            replicaJobListSearchQuery = e.target.value;
            replicaRender();
            const nextInput = replicaRoot().querySelector('#replica-job-search-input');
            if (nextInput) {
                nextInput.focus();
                nextInput.setSelectionRange(nextInput.value.length, nextInput.value.length);
            }
        });
    }

    const jobsCard = root.querySelector('#replica-sec-jobs');
    if (jobsCard) {
        jobsCard.addEventListener('toggle', () => {
            replicaJobListExpanded = jobsCard.open;
        });
    }

    root.querySelectorAll('[data-delete]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const jobId = btn.dataset.delete;
            if (!window.confirm('删除这个任务？抽帧产物会一并清掉，不可恢复。')) return;
            try {
                await replicaFetch('/api/replica/delete', {
                    method: 'POST', headers: replicaHeaders(),
                    body: JSON.stringify({ job_id: jobId }),
                });
                if (replicaState && replicaState.job_id === jobId) replicaState = null;
                await replicaLoadJobs();
                replicaRender();
                replicaToast('已删除');
            } catch (e) {
                if (e.message && (e.message.includes('强制删除') || e.message.includes('二创变体'))) {
                    if (window.confirm(e.message + '\n\n是否确认直接【强制删除】该任务？')) {
                        try {
                            await replicaFetch('/api/replica/delete', {
                                method: 'POST', headers: replicaHeaders(),
                                body: JSON.stringify({ job_id: jobId, force: true }),
                            });
                            if (replicaState && replicaState.job_id === jobId) replicaState = null;
                            await replicaLoadJobs();
                            replicaRender();
                            replicaToast('已强制删除');
                            return;
                        } catch (forceErr) {
                            replicaToast(forceErr.message, true);
                            return;
                        }
                    }
                }
                replicaToast(e.message, true);
            }
        });
    });

    on('#replica-lock-baseline-btn', () => replicaToggleBaselineLock(true));
    on('#replica-unlock-baseline-btn', () => replicaToggleBaselineLock(false));
    on('#replica-mutate-orthogonal-btn', (e) => replicaMutateOrthogonal(e.currentTarget));
    on('#replica-toggle-comparator-btn', () => replicaToggleComparator());
    on('#replica-close-comparator-btn', () => replicaToggleComparator(false));
    on('#replica-pane-handoff-btn', (e) => replicaHandoffToStepped(e.currentTarget));
    on('#replica-ai-diverge-btn', (e) => replicaAiDiverge(e.currentTarget));
    on('#replica-ai-diverge-refresh-btn', (e) => replicaAiDiverge(e.currentTarget));

    // 联网参考案例库抽屉事件
    on('#replica-trend-refs-toggle', () => {
        replicaRefsDrawerOpen = !replicaRefsDrawerOpen;
        const drawer = root.querySelector('#replica-spark-drawer-refs');
        if (drawer) {
            drawer.classList.toggle('drawer-open', replicaRefsDrawerOpen);
            const toggle = drawer.querySelector('#replica-trend-refs-toggle');
            if (toggle) toggle.setAttribute('aria-expanded', replicaRefsDrawerOpen ? 'true' : 'false');
        }
    });
    on('#replica-trend-refs-direction-toggle', () => {
        replicaRefsDirectionOpen = !replicaRefsDirectionOpen;
        const panel = root.querySelector('#replica-trend-refs-direction-panel');
        if (panel) panel.hidden = !replicaRefsDirectionOpen;
    });
    on('#replica-trend-refs-search-btn', () => {
        if (typeof searchNewTrendRefs === 'function') searchNewTrendRefs();
    });
    on('#replica-trend-refs-manage-open-btn', () => {
        if (typeof openTrendRefsManageModal === 'function') openTrendRefsManageModal();
    });
    on('#replica-trend-refs-archive-toggle', () => {
        if (typeof toggleArchivePanel === 'function') toggleArchivePanel('replica');
    });
    on('#replica-trend-refs-filter', (e) => {
        replicaRefsFilterQuery = e.target.value;
        if (typeof renderTrendRefs === 'function') renderTrendRefs();
    }, 'input');

    if (typeof initTrendRefsDirectionPanel === 'function') initTrendRefsDirectionPanel();
    // 首次挂载要真的去取一次数（见 trend_refs.js ensureTrendRefsLoaded 的注释）：
    // 只调 renderTrendRefs 就是拿空缓存渲染，抽屉会一直是空的。
    if (typeof ensureTrendRefsLoaded === 'function') ensureTrendRefsLoaded();
    else if (typeof renderTrendRefs === 'function') renderTrendRefs();

    root.querySelectorAll('[data-idea-idx]').forEach(card => {
        card.addEventListener('click', () => {
            const idx = parseInt(card.dataset.ideaIdx, 10);
            if (!isNaN(idx)) replicaSelectIdea(idx);
        });
    });

    root.querySelectorAll('[data-open-job]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const jid = btn.dataset.openJob;
            if (jid) {
                try {
                    await replicaLoadJob(jid);
                    replicaRender();
                    if (!replicaFocusSection('replica-sec-variant')) replicaFocusSection('replica-sec-current-job');
                } catch (e) { replicaToast(e.message, true); }
            }
        });
    });

    on('#replica-baseline-collage-box', () => replicaOpenLightbox([{
        url: replicaCollageUrl(replicaState),
        caption: '1:1 黄金母本 5 列标准拼图 (Ground-Truth Collage)',
    }], 0));

    on('#replica-collage', () => replicaOpenLightbox([{
        url: replicaCollageUrl(replicaState),
        caption: '关键帧拼贴图（整条序列的一览）',
    }], 0));

    replicaBindBeatEvents(root);
}

// 原地开图：点空白处 / Esc / 关闭键返回，多张帧可左右翻。
// 走全局灯箱（js/lightbox.js），不自造第二个——控制台与主页面已经共用它了。
// 灯箱不可用时退回新窗口，宁可多一个标签页也不能点了没反应。
function replicaOpenLightbox(items, index) {
    const usable = (items || []).filter(x => x && x.url);
    if (!usable.length) return;
    if (typeof openLightbox === 'function') {
        openLightbox(usable, Math.max(0, Math.min(index || 0, usable.length - 1)));
        return;
    }
    window.open(usable[Math.max(0, index || 0)].url, '_blank');
}

// 节拍区自己的绑定，单独一函数。
function replicaBindBeatEvents(scope) {
    if (!scope) return;
    const on = (sel, fn, evt = 'click') => {
        const el = scope.querySelector(sel);
        if (el) el.addEventListener(evt, fn);
    };

    // 保存与合成不在这一排里，它们常驻吸底操作栏（见 replicaBindBottomBarEvents）。
    // 精修会作废已有的 prompt_block（beats 一变，旧提示词就是按旧措辞合出来的）。
    // 已经合成过的 job 上这是一次真实的返工，按之前先说清楚。
    on('#replica-refine-craft-btn', (e) => {
        const composed = !!(replicaState && (replicaState.prompt_block || replicaState.stage === 'completed'));
        if (composed && !confirm('工艺精修只改措辞、不动画面内容，但它会让已经合成好的提示词作废，需要重新合成一次。继续？')) return;
        replicaAdvance('refine_craft', {}, e.currentTarget);
    });
    on('#replica-banner-autofix-btn', (e) => replicaAdvance('autofix', {}, e.currentTarget));
    on('#replica-autobalance-btn', (e) => replicaAdvance('autobalance', {}, e.currentTarget));
    // 重跑聚类＝Pass B 整份重新生成节拍阶梯，手工拆合拍、时间微调、改过的措辞一次归零。
    // 这是这一页破坏性最强的动作，此前却是全页唯一不问一句的（重抽帧/归档/删除/精修/中断
    // 都有确认）。第二句同样重要：用户按不按得下去，取决于知不知道这一步要不要重新付钱。
    on('#replica-recluster-btn', (e) => {
        const hasBeats = !!(replicaState && ((replicaState.beats || {}).beats || []).length);
        if (hasBeats && !window.confirm(
            '重跑节拍聚类？当前这份阶梯会被整份重新生成——你手工拆合的拍、微调过的时间、'
            + '改过的措辞都会丢失，且不可恢复。\n\n'
            + '不会重新付逐帧读取（Pass A）的钱：帧事实走缓存，只重跑聚类这一步。')) return;
        replicaAdvance('recluster', {}, e.currentTarget);
    });
    // 落盘同样由 replicaAdvance 统一做（见 REPLICA_LADDER_CONSUMERS），这里不再自己存一次。
    on('#replica-translate-btn', (e) => replicaAdvance('translate', {}, e.currentTarget));
    // 撤销不进 REPLICA_LADDER_CONSUMERS：它要丢掉的正是当前这一版，先落盘等于把
    // 用户想扔的东西先存一遍，还会把「上一版」挤掉一代。
    on('#replica-undo-beats-btn', (e) => {
        if (!window.confirm('回到上一版节拍阶梯？当前这一版会被换下来（但仍可以再点一次撤销换回去）。'
                            + '\n\n已合成的提示词是按当前这一版产出的，会一并作废，需要重新合成。')) return;
        replicaAdvance('undo_beats', {}, e.currentTarget);
    });
    on('#replica-variant-btn', (e) => replicaVariant(e.currentTarget));

    // 全部折叠 / 全部展开
    on('#replica-toggle-fold-all', () => {
        const cards = scope.querySelectorAll('.replica-beat');
        if (!cards.length) return;
        const hasExpanded = Array.from(cards).some(c => !c.classList.contains('is-collapsed'));
        cards.forEach(c => {
            const id = c.dataset.beatId;
            if (hasExpanded) {
                c.classList.add('is-collapsed');
                if (id) replicaBeatFoldState[id] = true;
                const btn = c.querySelector('.replica-beat-fold-btn');
                if (btn) btn.textContent = '▾';
            } else {
                c.classList.remove('is-collapsed');
                if (id) replicaBeatFoldState[id] = false;
                const btn = c.querySelector('.replica-beat-fold-btn');
                if (btn) btn.textContent = '⌃';
            }
        });
        const toggleBtn = scope.querySelector('#replica-toggle-fold-all');
        if (toggleBtn) {
            toggleBtn.textContent = hasExpanded ? '全部展开' : '全部折叠';
        }
    });

    // 单拍折叠 / 展开
    scope.querySelectorAll('.replica-beat-fold-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const card = btn.closest('.replica-beat');
            if (!card) return;
            const isCol = card.classList.toggle('is-collapsed');
            const id = card.dataset.beatId;
            if (id) replicaBeatFoldState[id] = isCol;
            btn.textContent = isCol ? '▾' : '⌃';
        });
    });

    scope.querySelectorAll('.replica-beat-summary').forEach(sumEl => {
        sumEl.addEventListener('click', () => {
            const card = sumEl.closest('.replica-beat');
            if (!card) return;
            card.classList.remove('is-collapsed');
            const id = card.dataset.beatId;
            if (id) replicaBeatFoldState[id] = false;
            const btn = card.querySelector('.replica-beat-fold-btn');
            if (btn) btn.textContent = '⌃';
        });
    });

    // 证据帧：原地开灯箱
    scope.querySelectorAll('[data-lightbox-beat]').forEach(img => {
        img.addEventListener('click', () => {
            const beats = ((replicaState || {}).beats || {}).beats || [];
            const beat = beats[parseInt(img.dataset.lightboxBeat, 10)];
            if (!beat) return;
            const frames = beat.evidence_frames || beat.reference_frames || [];
            replicaOpenLightbox(frames.map(name => ({
                url: replicaFrameUrl(replicaState, name),
                caption: `${beat.id || ''} ${name}`,
            })), parseInt(img.dataset.lightboxAt, 10) || 0);
        });
    });

    // 覆盖帧灯箱
    scope.querySelectorAll('[data-cov-beat]').forEach(img => {
        img.addEventListener('click', () => {
            const beats = ((replicaState || {}).beats || {}).beats || [];
            const beat = beats[parseInt(img.dataset.covBeat, 10)];
            if (!beat) return;
            replicaOpenLightbox((beat.coverage_frames || []).map(item => ({
                url: replicaFrameUrl(replicaState, item.frame),
                caption: `${beat.id || ''} ${item.timestamp}s ${item.frame}`,
            })), parseInt(img.dataset.covAt, 10) || 0);
        });
    });

    // 跳轨 Chip 与硬伤跳转：滚到对应拍并自动展开
    scope.querySelectorAll('[data-jump-beat]').forEach(btn => {
        btn.addEventListener('click', () => {
            const beatId = btn.dataset.jumpBeat;
            const card = scope.querySelector(`[data-beat-id="${beatId}"]`)
                      || scope.querySelector(`[data-beat-index="${parseInt(beatId, 10)}"]`);
            if (!card) { replicaToast(`找不到 ${beatId}，阶梯可能已被改动`, true); return; }
            if (card.classList.contains('is-collapsed')) {
                card.classList.remove('is-collapsed');
                if (beatId) replicaBeatFoldState[beatId] = false;
                const foldBtn = card.querySelector('.replica-beat-fold-btn');
                if (foldBtn) foldBtn.textContent = '⌃';
            }
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
            card.classList.remove('replica-beat-flag', 'replica-section-flash');
            void card.offsetWidth;
            card.classList.add('replica-beat-flag', 'replica-section-flash');
            setTimeout(() => card.classList.remove('replica-section-flash'), 1000);
        });
    });

    scope.querySelectorAll('[data-split]').forEach(btn => {
        btn.addEventListener('click', () => replicaSplitBeat(parseInt(btn.dataset.split, 10)));
    });
    scope.querySelectorAll('[data-merge]').forEach(btn => {
        btn.addEventListener('click', () => replicaMergeBeat(parseInt(btn.dataset.merge, 10)));
    });
    scope.querySelectorAll('[data-nudge-beat]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const beatIdx = parseInt(btn.dataset.nudgeBeat, 10);
            const field = btn.dataset.nudgeField;
            const delta = parseFloat(btn.dataset.nudgeDelta);
            replicaNudgeBeatTime(beatIdx, field, delta);
        });
    });

    // 字段说明的 ⓘ。走委托而不是逐个绑：一张卡片二十来个字段，几十拍就是几百个
    // 监听器，而它们做的是同一件事。tooltip 单例挂在 body 上——挂在字段里会被
    // .replica-beat 的 overflow 裁掉，也会把那一格撑高。
    if (!scope.dataset.helpBound) {
        scope.dataset.helpBound = '1';
        const showHelp = (btn) => {
            const tip = replicaHelpTip();
            tip.textContent = btn.dataset.help || '';
            tip.classList.add('is-on');
            const r = btn.getBoundingClientRect();
            const w = Math.min(360, window.innerWidth - 24);
            tip.style.width = `${w}px`;
            tip.style.left = `${Math.min(Math.max(8, r.left - 6), window.innerWidth - w - 12)}px`;
            const below = r.bottom + 8;
            tip.style.top = `${below + tip.offsetHeight > window.innerHeight - 8
                ? Math.max(8, r.top - tip.offsetHeight - 8) : below}px`;
        };
        const hideHelp = () => {
            const tip = document.getElementById('replica-help-tip');
            if (tip) tip.classList.remove('is-on');
        };
        scope.addEventListener('mouseover', (e) => {
            const btn = e.target.closest && e.target.closest('.replica-field-help');
            if (btn) showHelp(btn);
        });
        scope.addEventListener('mouseout', (e) => {
            if (e.target.closest && e.target.closest('.replica-field-help')) hideHelp();
        });
        // 键盘走到输入框时也把那一栏的说明亮出来——ⓘ 自己是 tabindex="-1"（它不
        // 承载任何动作，塞进 Tab 序列只会让二十几个字段变成四十几站）。
        scope.addEventListener('focusin', (e) => {
            const wrap = e.target.closest && e.target.closest('.replica-field');
            const btn = wrap && wrap.querySelector('.replica-field-help');
            if (btn) showHelp(btn); else hideHelp();
        });
        scope.addEventListener('focusout', hideHelp);
        // 触屏上没有悬停，点一下 ⓘ 也要能看到说明。
        scope.addEventListener('click', (e) => {
            const btn = e.target.closest && e.target.closest('.replica-field-help');
            if (!btn) return;
            e.preventDefault();
            const tip = document.getElementById('replica-help-tip');
            if (tip && tip.classList.contains('is-on') && tip.textContent === btn.dataset.help) hideHelp();
            else showHelp(btn);
        });
        // 滚动就收起来（浮层是 fixed 的，不跟着内容走）。scroll 不冒泡，所以用捕获；
        // 这一页真正滚动的是 .replica-shell 而不是 window。整个会话只绑一次——
        // replicaBindBeatEvents 会被 #replica-root 和 #replica-beats-host 各调一遍。
        if (!replicaHelpScrollBound) {
            replicaHelpScrollBound = true;
            window.addEventListener('scroll', hideHelp, { passive: true, capture: true });
        }
    }

    // 中文对照：整卡一个开关，状态记在 localStorage。写的时候关掉，核对时打开——
    // 此前它和可编辑的英文原文一样常驻，还是全卡最跳的一层颜色。
    scope.querySelectorAll('[data-zh-toggle]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            replicaSetZhMirror(!replicaZhMirrorOn());
        });
    });
    replicaApplyZhMirror();

    scope.querySelectorAll('[data-beat][data-key]').forEach(el => {
        const updateBeat = () => {
            const beat = ((replicaState || {}).beats || {}).beats || [];
            const target = beat[parseInt(el.dataset.beat, 10)];
            if (!target) return;
            const key = el.dataset.key;
            if (el.dataset.num) {
                // 清空 = 撤回标注，不是 0 人。0 是「清场帧」这个真实断言，两者不能混。
                const raw = el.value.trim();
                if (!raw) {
                    delete target[key];
                } else {
                    const n = parseInt(raw, 10);
                    if (!Number.isNaN(n)) target[key] = Math.max(0, Math.min(12, n));
                }
                if (key === 'worker_count' && typeof target.worker_count === 'number') {
                    target.workers_present = target.worker_count > 0;
                }
                return;
            }
            target[key] = (REPLICA_LIST_FIELDS.has(key) || Array.isArray(target[key]))
                ? el.value.split('\n').map(s => s.trim()).filter(Boolean)
                : el.value.trim();
        };
        el.addEventListener('input', updateBeat);
        el.addEventListener('change', updateBeat);
    });

    const banned = scope.querySelector('#replica-banned');
    if (banned) {
        banned.addEventListener('input', () => {
            if (!replicaState || !replicaState.beats) return;
            replicaState.beats.banned_elements = replicaSplitList(banned.value);
        });
    }

    const signature = scope.querySelector('#replica-scene-signature');
    if (signature) {
        signature.addEventListener('input', () => {
            if (!replicaState || !replicaState.beats) return;
            replicaState.beats.scene_signature = signature.value.trim();
        });
    }

    scope.querySelectorAll('[data-scene-array]').forEach(el => {
        el.addEventListener('input', () => {
            if (!replicaState || !replicaState.beats) return;
            replicaState.beats.scene_constants = replicaSplitList(el.value);
        });
    });

    scope.querySelectorAll('[data-scene-key]').forEach(el => {
        el.addEventListener('input', () => {
            if (!replicaState || !replicaState.beats) return;
            if (Array.isArray(replicaState.beats.scene_constants)) {
                replicaState.beats.scene_constants = {};
            }
            const sc = replicaState.beats.scene_constants || (replicaState.beats.scene_constants = {});
            sc[el.dataset.sceneKey] = replicaSplitList(el.value);
        });
    });

    // 记住卡片内部 <details> 的开合。用委托监听 toggle（它不冒泡，所以要用捕获）。
    scope.addEventListener('toggle', (e) => {
        const el = e.target;
        if (!el || !el.dataset || !el.dataset.foldKey) return;
        replicaFieldFoldState[el.dataset.foldKey] = !!el.open;
    }, true);

    // 脏标记走事件委托，而不是往上面那四个处理器里各加一行：新增一类节拍字段时只要它
    // 落在 REPLICA_BEAT_INPUT_SELECTOR 里就自动被算进来，不会再漏一处。
    // 变异轴勾选与二创 brief 也在 scope 内，但它们不是节拍改动，所以这里必须按选择器筛。
    const dirtySel = `${REPLICA_BEAT_INPUT_SELECTOR}, ${REPLICA_BEAT_SELECT_SELECTOR}`;
    ['input', 'change'].forEach(evt => {
        scope.addEventListener(evt, (e) => {
            if (e.target && e.target.matches && e.target.matches(dirtySel)) replicaMarkDirty(true);
        });
    });

    scope.querySelectorAll('.replica-axis-box').forEach(box => {
        box.addEventListener('change', () => {
            const checked = scope.querySelectorAll('.replica-axis-box:checked');
            if (checked.length > REPLICA_MAX_AXES) {
                box.checked = false;
                replicaToast(`最多同时变 ${REPLICA_MAX_AXES} 条轴`, true);
            }
        });
    });
}

// 重建会把焦点连同光标位置一起扔掉——activeElement 落回 <body>。
//
// 这一页的主路径就是「边打字边 Cmd+S」（那个快捷键是这一页自己装的），而保存成功
// 会立刻整份重建节拍区。于是每存一次，正在写的那一栏就失去焦点、光标归零，用户得
// 重新找到那一栏、点回去、再把光标挪到刚才那个位置。滚动位置早就有人管了（见下面
// 那段注释），焦点一直没有。
//
// 认字段用的是 data-beat + data-key 这一对，而不是元素引用——重建之后旧节点已经
// 不在文档里了。禁用元素 / 场景一句话 / 场景恒常特征也在这一区里，它们没有这一对，
// 按 id 或 data-scene-key 认。
function replicaCaptureFocus(host) {
    const el = document.activeElement;
    if (!el || !host.contains(el)) return null;
    const sel = (el.selectionStart !== undefined && el.selectionStart !== null)
        ? { start: el.selectionStart, end: el.selectionEnd } : null;
    if (el.dataset && el.dataset.beat !== undefined && el.dataset.key) {
        return { q: `[data-beat="${el.dataset.beat}"][data-key="${el.dataset.key}"]`, sel };
    }
    if (el.id) return { q: `#${el.id}`, sel };
    if (el.dataset && el.dataset.sceneKey) return { q: `[data-scene-key="${el.dataset.sceneKey}"]`, sel };
    return null;
}

function replicaRestoreFocus(host, saved) {
    if (!saved) return;
    let next;
    try {
        next = host.querySelector(saved.q);
    } catch (e) {
        return;   // id 里有特殊字符时选择器不合法，放弃还位而不是把重建整个炸掉
    }
    if (!next) return;   // 拆合拍会重排 id，那一栏可能已经不在了
    // preventScroll：还位必须交给下面的 scrollTop，focus() 自己滚会把刚存好的位置冲掉。
    next.focus({ preventScroll: true });
    if (saved.sel && next.setSelectionRange) {
        try {
            next.setSelectionRange(saved.sel.start, saved.sel.end);
        } catch (e) { /* select 之类没有选区，忽略 */ }
    }
}

// 只重建节拍区，不动页面其余部分，并把滚动位置与焦点放回去。
//
// 整页重建会把滚动位置一起丢掉，而节拍区恰恰是这一页最长的一块——几十拍改到一半被
// 弹回顶端，等于每保存一次就罚一次。存位/还位必须用 replicaShell()：全站 window
// 从不滚动，用 window.scrollY 存下来的恒为 0，还位就成了空操作。
function replicaRefreshBeats() {
    const host = replicaRoot() && replicaRoot().querySelector('#replica-beats-host');
    if (!host) { replicaRender(); return; }
    const shell = replicaShell();
    const top = shell ? shell.scrollTop : 0;
    const focused = replicaCaptureFocus(host);
    host.innerHTML = replicaRenderBeats(replicaState);
    replicaBindBeatEvents(host);
    replicaRestoreFocus(host, focused);
    // 硬伤数变了，栏外那两块也得跟着变：吸顶导航上的红色角标、吸底栏上的硬伤计数与
    // 「合成提示词」的禁用态，都是从同一份 validation 算出来的。只刷节拍区的话，
    // 保存完硬伤明明清零了，导航还挂着红角标、合成按钮还是灰的。
    replicaRefreshChrome();
    if (replicaBusy) replicaSetBusy(true);
    if (shell) shell.scrollTop = top;
}

// 重画右侧区段导航与吸底操作栏（都在节拍区之外，且都读 validation）。
function replicaRefreshChrome() {
    const navBar = document.getElementById('replica-nav-bar');
    if (navBar) {
        navBar.outerHTML = replicaRenderNavBar(replicaState);
        replicaBindNavEvents();
    }
    const bottomBar = document.getElementById('replica-bottom-bar');
    if (bottomBar) {
        bottomBar.outerHTML = replicaRenderBottomBar(replicaState);
        replicaBindBottomBarEvents();
    }
    replicaUpdateNavActive();
    replicaUpdateTabBadge();
}

// ── 区段直达 ──────────────────────────────────────────────────────────────────────

// 滚到某个区段并闪一下。三个入口（阶段轨、右侧导航、页头「上传新视频」）此前各写了
// 一份一模一样的 scrollIntoView + flash + setTimeout。
function replicaFocusSection(targetId) {
    const target = targetId && document.getElementById(targetId);
    if (!target) return false;
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    target.classList.remove('replica-section-flash');
    void target.offsetWidth;   // 强制回流，否则连点同一项时动画不会重播
    target.classList.add('replica-section-flash');
    setTimeout(() => target.classList.remove('replica-section-flash'), 1000);
    return true;
}

// 右侧区段导航。单独一函数是因为它要在 replicaRefreshBeats 里被重绑——保存一次
// 之后硬伤数会变，那个角标挂在导航药丸上。
function replicaBindNavEvents() {
    const bar = document.getElementById('replica-nav-bar');
    if (!bar) return;
    bar.querySelectorAll('.replica-nav-item').forEach(btn => {
        btn.addEventListener('click', () => {
            if (!replicaFocusSection(btn.dataset.navTarget)) return;
            bar.querySelectorAll('.replica-nav-item').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
        });
    });
}

// 吸底主操作栏。同样要能重绑：栏上的硬伤计数与「合成提示词」的禁用态都跟着校验结果走。
function replicaBindBottomBarEvents() {
    const bar = document.getElementById('replica-bottom-bar');
    if (!bar) return;
    const on = (sel, fn) => {
        const el = bar.querySelector(sel);
        if (el) el.addEventListener('click', fn);
    };
    on('#replica-bar-start-btn', replicaStart);
    on('#replica-bar-save-btn', (e) => replicaSaveBeats(true, e.currentTarget));
    on('#replica-bar-compose-btn', (e) => replicaCompose(e.currentTarget));
    on('#replica-bar-project-btn', (e) => replicaSaveToProject(e.currentTarget));
    on('#replica-bar-recompose-btn', replicaCompose);
    on('#replica-bar-cancel-btn', replicaCancelRun);
    // 硬伤计数本身就是「带我去看」的入口：它是这条栏上唯一说得出问题在哪的东西。
    on('#replica-bar-errors-btn', () => replicaFocusSection('replica-sec-beats'));
    const resetCacheBox = bar.querySelector('#replica-reset-cache');
    if (resetCacheBox) {
        resetCacheBox.addEventListener('change', () => {
            replicaResetCache = resetCacheBox.checked;
        });
    }
}

// ── 滚动监听与 ScrollSpy ──────────────────────────────────────────────────────────

function replicaInitScrollSpy() {
    const shell = replicaShell();
    if (!shell || shell._hasReplicaScrollSpy) return;
    shell._hasReplicaScrollSpy = true;
    shell.addEventListener('scroll', () => {
        replicaHandleScroll(shell);
    }, { passive: true });
}

function replicaHandleScroll(shell) {
    if (!shell) return;
    const top = shell.scrollTop;
    const floatTools = document.getElementById('replica-floating-tools');
    if (floatTools) {
        floatTools.classList.toggle('is-visible', top > 150);
    }
    replicaUpdateNavActive();
}

function replicaUpdateNavActive() {
    const shell = replicaShell();
    if (!shell) return;
    const navItems = document.querySelectorAll('.replica-nav-item');
    if (!navItems.length) return;

    const shellRect = shell.getBoundingClientRect();
    const targets = [];
    navItems.forEach(item => {
        const id = item.dataset.navTarget;
        const el = document.getElementById(id);
        if (el) {
            const rect = el.getBoundingClientRect();
            const relTop = rect.top - shellRect.top;
            targets.push({ id, item, el, relTop });
        }
    });

    // 按「相对滚动容器顶端的距离」排，而不是 offsetTop。offsetTop 是相对各自
    // offsetParent 的，眼下所有锚点恰好共用同一个（谁都没有 position），所以两者
    // currently 等价；但给 .replica-workbench-grid 或 .replica-card 加一次
    // position:relative，offsetTop 的口径就会分裂，排序静默错乱、高亮跳到别的区段。
    // relTop 上面已经算好了，用它没有这个前提。
    targets.sort((a, b) => a.relTop - b.relTop);

    // 最后一个「顶端已经越过吸顶导航条」的区段就是当前区段。
    let best = null;
    for (const t of targets) {
        if (t.relTop <= 160) {
            best = t;
        }
    }
    if (!best && targets.length > 0) {
        best = targets[0];
    }

    if (best) {
        navItems.forEach(item => {
            item.classList.toggle('active', item.dataset.navTarget === best.id);
        });
    }
}

function replicaUpdateTabBadge() {
    const tab = document.getElementById('main-tab-replica');
    if (!tab) return;
    let badge = tab.querySelector('.replica-tab-status-dot');
    const hasRunning = !!replicaSSE;
    const isPausedReview = replicaState && replicaState.stage === 'review_beats';
    const violations = replicaState ? (replicaState.validation || (replicaState.beats && replicaState.beats.validation) || []) : [];
    const hasErrors = violations.some(v => v.level === 'error');

    if (hasRunning || isPausedReview || hasErrors) {
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'replica-tab-status-dot';
            tab.appendChild(badge);
        }
        if (hasErrors) {
            badge.className = 'replica-tab-status-dot is-error';
            badge.title = '存在待修复硬伤';
        } else if (hasRunning) {
            badge.className = 'replica-tab-status-dot is-running';
            badge.title = '正在执行中';
        } else if (isPausedReview) {
            badge.className = 'replica-tab-status-dot is-pause';
            badge.title = '待人工核对节拍';
        }
    } else if (badge) {
        badge.remove();
    }
}

// 禁用元素的分隔符。UI 提示"用、分隔"，而原先的解析是 /[、,\n]/ —— 不含全角逗号，
// 用户打一个「，」整串就塌成一个元素，然后禁用清单静默失效。
function replicaSplitList(text) {
    return String(text || '').split(/[、，,;；\n]/).map(s => s.trim()).filter(Boolean);
}

// 反馈打在固定浮层上，而不是页面顶端上传卡片里的那个 #replica-upload-status。
// 原先所有提示——包括你在页面底部点「保存并重校验」得到的那句——都写进上传卡片，
// 几十拍的编辑器一撑开，它就在视口外了：操作看上去毫无反应。
let replicaToastTimer = null;

function replicaToast(msg, isError) {
    if (isError) console.error('[replica]', msg);
    let el = document.getElementById('replica-toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'replica-toast';
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.className = `replica-toast show ${isError ? 'is-error' : ''}`;
    if (replicaToastTimer) clearTimeout(replicaToastTimer);
    // 报错留久一点：它通常带着"照着改"的指路，一闪而过等于没说。
    replicaToastTimer = setTimeout(() => { el.className = 'replica-toast'; },
                                   isError ? 12000 : 5000);
}

// 转圈只加在**触发操作的那个按钮**上。
// 导航、回到顶部、中断取消、跳轨定位与纯查看折叠等浏览类控件在运行中必须保持可用，
// 不能被全页 busy 误伤成 disabled，否则用户在跑长任务时无法滚动导航与查看进度。
function replicaSetBusy(busy, activeBtn) {
    replicaBusy = busy;
    const root = replicaRoot();
    if (!root) return;
    root.querySelectorAll('button').forEach(b => {
        // 导航类与纯查看/中断交互在运行中保持可用：
        const isNavOrReadOnly = (
            b.id === 'replica-cancel-btn' ||
            b.id === 'replica-bar-cancel-btn' ||
            b.id === 'replica-bar-errors-btn' ||
            b.dataset.floatAction === 'top' ||
            b.dataset.navTarget ||
            b.classList.contains('replica-nav-item') ||
            b.dataset.jumpBeat ||
            b.classList.contains('replica-jump') ||
            b.classList.contains('replica-beat-jump-btn') ||
            b.dataset.toggleVariants ||
            b.dataset.beatFold ||
            b.id === 'replica-toggle-fold-all' ||
            b.id === 'replica-toggle-extract-btn' ||
            b.id === 'replica-close-comparator-btn' ||
            b.id === 'replica-toggle-comparator-btn' ||
            b.classList.contains('spark-drawer-toggle') ||
            b.classList.contains('modal-close') ||
            b.classList.contains('viewer-close')
        );
        if (isNavOrReadOnly) {
            b.disabled = b.hasAttribute('data-perm-disabled');
            return;
        }
        b.disabled = busy || b.hasAttribute('data-perm-disabled');
        b.classList.toggle('is-running', busy && b === activeBtn);
    });

    // 输入框此前完全不受 busy 影响（这个函数只遍历 button）。于是跑着的时候照样能改字，
    // 而 autofix / 工艺精修跑完会整份替换 replicaState.beats——那几分钟里敲的每一个键
    // 都写进一份马上要被丢掉的文档，结束时无声消失。
    //
    // 用 readOnly 而不是 disabled：只读的文本仍可选中复制，禁用的连读都读不清，而用户
    // 在等结果时最常做的事恰恰是回头看自己写了什么。
    root.querySelectorAll(REPLICA_BEAT_INPUT_SELECTOR).forEach(el => {
        el.readOnly = busy;
        el.classList.toggle('is-run-locked', busy);
        if (busy) {
            el.title = '这一轮跑完之前不能改：改动会被这一轮的结果整份覆盖';
        } else if (el.title === '这一轮跑完之前不能改：改动会被这一轮的结果整份覆盖') {
            el.removeAttribute('title');
        }
    });
    root.querySelectorAll(REPLICA_BEAT_SELECT_SELECTOR).forEach(el => {
        el.disabled = busy;
        el.classList.toggle('is-run-locked', busy);
    });
}

/* --- 动作 --- */

async function replicaUpload() {
    const input = replicaRoot().querySelector('#replica-file');
    const file = input && input.files && input.files[0];
    if (!file) { replicaToast('请先选择一个视频文件', true); return; }
    const fpsEl = replicaRoot().querySelector('#replica-upload-fps');
    const baseFps = fpsEl ? Number(fpsEl.value) : REPLICA_DEFAULT_FPS;
    const thresholdEl = replicaRoot().querySelector('#replica-upload-threshold');
    const stateDiffThreshold = thresholdEl ? Number(thresholdEl.value) : REPLICA_DEFAULT_THRESHOLD;

    replicaSetBusy(true);
    replicaToast(`正在上传 ${file.name}（${(file.size / 1048576).toFixed(1)} MB）…`);
    try {
        const form = new FormData();
        form.append('video', file, file.name);
        const data = await replicaFetch('/api/replica/upload', {
            method: 'POST', headers: replicaHeaders(false), body: form,
        });
        replicaState = data.job_state;
        await replicaLoadJobs();
        replicaRender();
        replicaToast(data.reused
            ? '这条视频之前已经抽过帧，直接复用旧任务（抽帧是几分钟的 ffmpeg，不必重跑）'
            : '上传完成，开始抽帧。抽完会先给出成本预估，确认了才进入反推。');
        // 只续抽帧，不续 Pass A。原先这里 `await replicaStart()` 一路跑到 Pass B，
        // 成本预估在中途作为一行文案闪过，用户没有任何机会介入——那正是「先确认再
        // 烧钱」这道卡点形同虚设的原因。
        if (!data.reused) await replicaExtract(baseFps, stateDiffThreshold);
    } catch (e) {
        replicaToast(e.message, true);
    } finally {
        replicaSetBusy(false);
    }
}

async function replicaExtract(baseFps, stateDiffThreshold) {
    if (!replicaState) return;
    replicaSetBusy(true);
    try {
        const data = await replicaFetch('/api/replica/extract', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({
                job_id: replicaState.job_id,
                base_fps: baseFps == null ? undefined : Number(baseFps),
                state_diff_threshold: stateDiffThreshold == null ? undefined : Number(stateDiffThreshold),
                config: replicaConfig(),
            }),
        });
        replicaTaskId = data.task_id;
        replicaOpenSSE(replicaTaskId);
        replicaRender();   // 让「中断这一轮」露出来
    } catch (e) {
        replicaToast(e.message, true);
        replicaSetBusy(false);
    }
}

// 换密度重抽帧。已有的帧事实与节拍会作废（帧名是序号不是时间戳，见后端
// _purge_extract_products），所以先问一句再动。
async function replicaReExtract() {
    if (!replicaState) return;
    const fpsEl = replicaRoot().querySelector('#replica-base-fps');
    const baseFps = fpsEl ? Number(fpsEl.value) : REPLICA_DEFAULT_FPS;
    const thresholdEl = replicaRoot().querySelector('#replica-base-threshold');
    const stateDiffThreshold = thresholdEl ? Number(thresholdEl.value) : REPLICA_DEFAULT_THRESHOLD;
    const fpsChanged = baseFps !== replicaCurrentFps(replicaState);
    const thresholdChanged = stateDiffThreshold !== replicaCurrentThreshold(replicaState);
    if (!fpsChanged && !thresholdChanged) {
        replicaToast('抽帧密度与跳变灵敏度都没变，不必重抽。先在左边选一个新档位。', true);
        return;
    }
    const hasWork = !!(replicaState.facts
        || (replicaState.beats && (replicaState.beats.beats || []).length));
    if (hasWork && !window.confirm(
        `按 ${baseFps}fps / 灵敏度 ${stateDiffThreshold} 重抽帧？这条任务已有的帧事实与节拍`
        + '会一并作废（帧文件名是序号，换了档位就对不上原来的时刻），Pass A 需要重跑、重新付费。')) return;
    await replicaExtract(baseFps, stateDiffThreshold);
}

// 反推段的模型选择：先落进全局 config（写 localStorage），再随请求体发出去。
// 落盘是为了下一条任务、下一次开页面还是这个选择——不落的话每次都回默认值，
// 用户会以为自己选过了。
function replicaCaptureReverseSettings() {
    const root = replicaRoot();
    if (!root) return;
    const frameEl = root.querySelector('#replica-frame-model');
    if (frameEl) replicaSetConfigValue('frameFactsModel', frameEl.value);
    const peakEl = root.querySelector('#replica-peak-model');
    // 空值 = 跟随主模型：写空字符串，reverse._peak_verify_model 会回落到 config.model。
    if (peakEl) replicaSetConfigValue('peakVerifyModel', peakEl.value);
}

async function replicaStart() {
    if (!replicaState) return;
    const modeEl = replicaRoot().querySelector('input[name="replica-mode"]:checked');
    const scope = replicaScopeFromMode(modeEl && modeEl.value);
    replicaCaptureReverseSettings();
    replicaSetBusy(true);
    try {
        const data = await replicaFetch('/api/replica/start', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({
                job_id: replicaState.job_id, scope,
                degraded: scope === 'degraded',   // 老服务端只认这个键
                config: replicaConfig(),
            }),
        });
        replicaTaskId = data.task_id;
        replicaOpenSSE(replicaTaskId);
        replicaRender();   // 让「中断这一轮」露出来
    } catch (e) {
        replicaToast(e.message, true);
        replicaSetBusy(false);
    }
}

// 会「吃掉」当前节拍阶梯的 action：服务端一律从磁盘读 beats（autofix_job_beats 第一件事
// 就是 _load_state），跑完再整份写回、由 SSE 收尾时的 replicaLoadJob 盖掉内存。所以不先
// 落盘就等于拿上一次保存的版本去改，用户这一轮的手工改动会在没有任何报错的情况下蒸发。
//
// recluster 有意不在此列：它按设计就是丢掉这份阶梯重跑 Pass B，先存一次只是白写一遍磁盘。
const REPLICA_LADDER_CONSUMERS = new Set([
    'approve', 'autofix', 'fix_beats', 'autobalance', 'refine_craft', 'translate', 'variant',
]);

async function replicaAdvance(action, payload = {}, btn) {
    if (!replicaState) return;
    // 落盘失败就地中止：宁可让用户看见「保存失败」，也不能让一次静默的旧版本改写跑出去。
    if (REPLICA_LADDER_CONSUMERS.has(action) && (replicaState.beats || {}).beats) {
        if (!(await replicaSaveBeats(false, btn))) {
            replicaToast('改动没能存下来，这一步已中止——请再点一次「保存并重校验」看服务端说了什么', true);
            replicaSetBusy(false);
            return;
        }
    }
    replicaSetBusy(true, btn);
    try {
        const data = await replicaFetch('/api/replica/advance', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id, action, payload, config: replicaConfig() }),
        });
        replicaTaskId = data.task_id;
        replicaOpenSSE(replicaTaskId);
        replicaRender();   // 让「中断这一轮」露出来
    } catch (e) {
        replicaToast(e.message, true);
        replicaSetBusy(false);
    }
}

async function replicaCompose(btn) {
    // 落盘由 replicaAdvance 统一负责（REPLICA_LADDER_CONSUMERS）。这里原先自己存一次，
    // 于是全线只有「合成」这一个入口是对的，另外五个按钮各自漏掉——口径收到一处之后
    // 新增推进动作只要进那张表就自动有保障。
    // 勾选态在这里读一次就用完：清缓存是一次性动作，不该黏在下一次合成上——用户为
    // 一轮改规则的重跑勾了它，接着改一拍再合成时不该又白付一次 Phase 1。
    const payload = replicaResetCache ? { reset_cache: true } : {};
    if (replicaResetCache) {
        replicaResetCache = false;
        const box = document.getElementById('replica-reset-cache');
        if (box) box.checked = false;
    }
    replicaAdvance('approve', payload, btn instanceof HTMLElement ? btn : undefined);
}

// 「存入项目并打开激发结果」。
//
// 这里原先是「送去分步管线渲染」：一按就起一条 stepped 任务，把用户扔进分步管线页。
// 那条路只通向一种渲染方式，而且渲染一旦开跑，这一单在工作台上才刚刚成形——用户
// 想先看看提示词、改一拍、或者改用别的渲染方式，都没有入口。改成先落成项目：
// 项目才是这套系统里所有下游动作（分步合成、一键合成、手动编辑、帧序列）的共同起点。
async function replicaSaveToProject(btn) {
    if (!replicaState) return;
    replicaSetBusy(true, btn);
    try {
        const data = await replicaFetch('/api/replica/to_project', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id }),
        });
        const item = data && data.item;
        if (!item || !item.id) throw new Error('服务端没有回这条项目记录，未打开结果页');

        // 服务端已经落库（write_library_item），这里只把浏览器里那份 savedIdeas 对齐：
        // 不再 persistIdeaItem —— 那会把刚拿到的同一条记录原样写回去一次。
        if (typeof savedIdeas !== 'undefined' && Array.isArray(savedIdeas)) {
            const at = savedIdeas.findIndex(x => String(x.id) === String(item.id));
            if (at >= 0) savedIdeas[at] = item;
            else savedIdeas.unshift(item);
        }
        if (typeof refreshProjects === 'function') refreshProjects({ assets: false });

        if (typeof loadSavedIdea !== 'function' || typeof switchMainTab !== 'function') {
            // 存是存下了，只是这一页打不开它。说清楚"已存入"，别让用户以为白按了。
            replicaToast('已存入项目，但激发结果工作区尚未加载完成——去「激发结果」页手动打开它。',
                         true);
            return;
        }
        switchMainTab('results');
        loadSavedIdea(item, { toast: `已存入项目「${item.title || ''}」，可在这里渲染或继续编辑` });
        // loadSavedIdea 收尾停在「总览」，而这一单此刻只有提示词集——直接落到那一页。
        if (typeof switchTab === 'function') switchTab('prompts');
    } catch (e) {
        replicaToast(e.message, true);
    } finally {
        replicaSetBusy(false);
    }
}

function replicaSelectIdea(ideaIndex) {
    replicaActiveIdeaIndex = ideaIndex;
    const ideas = (replicaState && replicaState.ai_diverged_ideas) || replicaAiIdeas || [];
    const idea = ideas[ideaIndex];
    if (!idea) return;

    replicaActivePreset = idea.id || `idea_${ideaIndex}`;
    const root = replicaRoot();
    if (!root) return;

    root.querySelectorAll('.replica-ai-idea-card').forEach((card, idx) => {
        const active = idx === ideaIndex;
        card.classList.toggle('active', active);
        const top = card.querySelector('.replica-ai-idea-top');
        if (top) {
            let badge = top.querySelector('.replica-ai-idea-badge');
            if (active) {
                if (!badge) top.insertAdjacentHTML('beforeend', '<span class="replica-ai-idea-badge">已选用</span>');
            } else {
                if (badge) badge.remove();
            }
        }
    });

    const setVal = (id, val) => {
        const el = root.querySelector(id);
        if (el) el.value = val || '';
    };

    if (idea.axes) {
        setVal('#replica-axis-env', idea.axes.environment || '');
        setVal('#replica-axis-mat', idea.axes.material || '');
        setVal('#replica-axis-func', idea.axes.function || '');
        setVal('#replica-axis-hero', idea.axes.hero_reveal || '');
    }

    const note = idea.trend_ref ? `（借鉴：${idea.trend_ref}）` : '';
    replicaToast(`已装载「${idea.name}」正交词槽${note}`);
}

async function replicaAiDiverge(btn) {
    if (!replicaState) {
        replicaToast('请先选择或上传一个母本任务', true);
        return;
    }
    const root = replicaRoot() || document;
    const briefInput = root.querySelector('#replica-diverge-brief');
    const brief = briefInput ? briefInput.value.trim() : (replicaDivergeBrief || '');
    replicaDivergeBrief = brief;

    const selTrendRefIds = typeof getSelectedTrendRefIds === 'function' ? getSelectedTrendRefIds() : [];

    replicaDiverging = true;
    replicaDivergeStep = 1;
    replicaDivergeStatusText = '正在深度解析母本工序拓扑与叙事灵魂...';
    replicaSetBusy(true, btn);
    replicaRender();

    let stepTimer = null;
    let currentStep = 1;
    const stepMessages = [
        '正在深度解析母本工序拓扑与叙事灵魂...',
        selTrendRefIds.length > 0 ? `正在汲取 ${selTrendRefIds.length} 条联网爆款参考要素...` : '正在结合趋势案例库与材质体系...',
        '大模型正在正交重构 4 组写实建造方案...',
        '正在进行物理相容性与骨架约束诊断...',
    ];

    stepTimer = setInterval(() => {
        if (!replicaDiverging) {
            clearInterval(stepTimer);
            return;
        }
        if (currentStep < 4) {
            currentStep++;
            replicaDivergeStep = currentStep;
            replicaDivergeStatusText = stepMessages[currentStep - 1] || '正在构思正交创意...';
            const scope = replicaRoot() || document;
            const statusEl = scope.querySelector('#replica-diverge-status-text');
            if (statusEl) statusEl.textContent = replicaDivergeStatusText;
            const barEl = scope.querySelector('.replica-diverge-progress-bar-fill');
            if (barEl) barEl.style.width = `${Math.min(100, currentStep * 25)}%`;
            const stepsRow = scope.querySelector('.replica-diverge-steps-row');
            if (stepsRow) {
                stepsRow.querySelectorAll('.replica-diverge-step-item').forEach((item, idx) => {
                    const num = idx + 1;
                    item.className = `replica-diverge-step-item ${currentStep > num ? 'step-done' : (currentStep === num ? 'step-current' : 'step-pending')}`;
                    const bubble = item.querySelector('.replica-diverge-step-bubble');
                    if (bubble) bubble.textContent = currentStep > num ? '✓' : String(num);
                });
            }
        }
    }, 3200);

    try {
        const data = await replicaFetch('/api/replica/ai_diverge', {
            method: 'POST',
            headers: replicaHeaders(),
            body: JSON.stringify({
                baseline_job_id: replicaState.job_id,
                brief: brief,
                count: 4,
                trend_ref_ids: selTrendRefIds,
                config: replicaConfig(),
            }),
        });
        if (stepTimer) clearInterval(stepTimer);
        replicaDiverging = false;
        if (data.ideas && data.ideas.length) {
            replicaAiIdeas = data.ideas;
            if (replicaState) {
                replicaState.ai_diverged_ideas = data.ideas;
            }
            replicaActiveIdeaIndex = 0;
            replicaRender();
            replicaSelectIdea(0);
            const refNote = selTrendRefIds.length > 0 ? `已结合 ${selTrendRefIds.length} 条联网参考` : '已结合趋势案例库';
            replicaToast(`✨ AI 智能发散成功（${refNote}），已自动装载首选方案！`);
        } else {
            replicaRender();
            replicaToast('未能生成发散方案，请重试', true);
        }
    } catch (e) {
        if (stepTimer) clearInterval(stepTimer);
        replicaDiverging = false;
        replicaRender();
        replicaToast(e.message, true);
    } finally {
        replicaDiverging = false;
        replicaSetBusy(false);
    }
}

function replicaToggleComparator(open) {
    if (typeof open === 'boolean') replicaComparatorOpen = open;
    else replicaComparatorOpen = !replicaComparatorOpen;
    replicaRender();
    if (replicaComparatorOpen) {
        const el = document.getElementById('replica-sec-comparator');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

async function replicaToggleBaselineLock(lock) {
    if (!replicaState) return;
    replicaSetBusy(true);
    try {
        const data = await replicaFetch('/api/replica/lock_baseline', {
            method: 'POST',
            headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id, lock: !!lock }),
        });
        replicaState = data.job_state;
        await replicaLoadJobs();
        replicaRender();
        replicaToast(lock ? '🔒 已成功固化为 Gold Baseline 母本（节拍转为只读保护）' : '🔓 已解锁母本（可继续编辑节拍）');
    } catch (e) {
        replicaToast(e.message, true);
    } finally {
        replicaSetBusy(false);
    }
}

async function replicaMutateOrthogonal(btn) {
    if (!replicaState) return;
    const root = replicaRoot() || document;
    const env = (root.querySelector('#replica-axis-env') || {}).value || '';
    const mat = (root.querySelector('#replica-axis-mat') || {}).value || '';
    const func = (root.querySelector('#replica-axis-func') || {}).value || '';
    const hero = (root.querySelector('#replica-axis-hero') || {}).value || '';

    if (!env && !mat && !func && !hero) {
        replicaToast('请先输入四轴参数或点击「✨ AI 智能发散创意」', true);
        return;
    }

    const mutationAxes = {
        environment: env,
        material: mat,
        function: func,
        hero_reveal: hero,
    };

    const ideas = (replicaState && replicaState.ai_diverged_ideas) || replicaAiIdeas || [];
    const activeIdea = replicaActiveIdeaIndex >= 0 ? ideas[replicaActiveIdeaIndex] : null;
    if (activeIdea) {
        if (activeIdea.scene_signature) mutationAxes.scene_signature = activeIdea.scene_signature;
        if (activeIdea.banned_elements) mutationAxes.banned_elements = activeIdea.banned_elements;
        if (activeIdea.trend_ref) mutationAxes.trend_ref = activeIdea.trend_ref;
        if (activeIdea.trend_ref_ids) mutationAxes.trend_ref_ids = activeIdea.trend_ref_ids;
    }

    replicaSetBusy(true, btn);
    replicaResetProgress();
    replicaProgress.stage = 'mutate_beats';
    replicaProgress.actionLabel = '⚡ 正交二创变体派生';
    replicaProgress.range = [45, 68];
    replicaProgressUpdate(45, '正在启动四轴正交变体派生任务...', 'mutate_beats');
    replicaRender();

    try {
        const data = await replicaFetch('/api/replica/mutate_orthogonal', {
            method: 'POST',
            headers: replicaHeaders(),
            body: JSON.stringify({
                baseline_job_id: replicaState.job_id,
                mutation_axes: mutationAxes,
                preset: (activeIdea && (activeIdea.name || activeIdea.id)) || replicaActivePreset || 'ai_variant',
                brief: replicaDivergeBrief || '',
                config: replicaConfig(),
            }),
        });
        replicaTaskId = data.task_id;
        replicaOpenSSE(replicaTaskId);
        replicaToast('⚡ 正在执行四轴正交派生（100% 锁死母本节拍骨架与机位）...');
    } catch (e) {
        replicaToast(e.message, true);
        replicaSetBusy(false);
    }
}

async function replicaHandoffToStepped(btn) {
    if (!replicaState) return;
    replicaSetBusy(true, btn);
    try {
        const data = await replicaFetch('/api/replica/handoff', {
            method: 'POST',
            headers: replicaHeaders(),
            body: JSON.stringify({
                job_id: replicaState.job_id,
                config: replicaConfig(),
            }),
        });
        replicaToast(`已成功递交至分步渲染管线 (任务: ${data.task_id})`);
        if (typeof switchMainTab === 'function') {
            switchMainTab('stepped');
        }
    } catch (e) {
        replicaToast(e.message, true);
    } finally {
        replicaSetBusy(false);
    }
}

function replicaVariant(btn) {
    const root = replicaRoot();
    const axes = Array.from(root.querySelectorAll('.replica-axis-box:checked')).map(b => b.value);
    if (!axes.length) { replicaToast('至少勾一条变异轴', true); return; }
    const brief = (root.querySelector('#replica-variant-brief') || {}).value || '';
    replicaAdvance('variant', { axes, brief }, btn instanceof HTMLElement ? btn : undefined);
}

function replicaConfig() {
    // `config` 是 js/state.js 的全局配置对象。复刻这条线只用到里面的网关与模型字段；
    // 取不到就交空对象，服务端的 effective_config 会补上服务端权威配置。
    return typeof config !== 'undefined' && config ? config : {};
}

/* --- 节拍编辑 --- */

// model 就是真相：textarea 的 input 事件已经把每一次编辑写回 replicaState.beats
// （见 replicaBindBeatEvents）。这里只做一次深拷贝，不再全量扫 DOM 反推文档。
// 置/清脏标记，并就地更新吸底栏那颗点。
//
// 不走 replicaRefreshChrome：那会把整条吸底栏 outerHTML 重建一遍，而这个函数是挂在
// input 事件上的——每敲一个键重建一次按钮栏，用户按到一半的按钮会在手底下被换掉。
function replicaMarkDirty(dirty) {
    if (replicaDirty === dirty) return;
    replicaDirty = dirty;
    const btn = document.getElementById('replica-bar-save-btn');
    if (!btn) return;
    btn.classList.toggle('replica-bar-save-dirty', dirty);
    btn.title = dirty ? '有改动还没存下来' : '';
    const dot = btn.querySelector('.replica-dirty-dot');
    if (dirty && !dot) {
        const span = document.createElement('span');
        span.className = 'replica-dirty-dot';
        btn.appendChild(span);
    } else if (!dirty && dot) {
        dot.remove();
    }
}

function replicaCollectBeats() {
    if (!replicaState || !replicaState.beats) return null;
    return JSON.parse(JSON.stringify(replicaState.beats));
}

async function replicaSaveBeats(rerender, btn) {
    const doc = replicaCollectBeats();
    if (!doc) return false;
    if (btn) replicaSetBusy(true, btn);
    try {
        const data = await replicaFetch('/api/replica/beats', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id, beats: doc }),
        });
        replicaState = data.job_state;
        replicaMarkDirty(false);
        if (rerender) {
            // 只重建节拍区。整页重建会把用户滚到的位置一起丢掉，而节拍区恰恰是这一页
            // 最长的一块——几十拍改到一半被弹回顶端，等于每保存一次就罚一次。
            replicaRefreshBeats();
            const errors = (data.validation || []).filter(v => v.level === 'error');
            replicaToast(errors.length ? `已保存，仍有 ${errors.length} 项硬伤` : '已保存，校验通过');
        }
        return true;
    } catch (e) {
        replicaToast(e.message, true);
        return false;
    } finally {
        if (btn) replicaSetBusy(false);
    }
}

// 拆拍 / 合拍：改完立刻落盘，并用服务端重排过 id 的那一份回写。
//
// 原先只改本地就 replicaRender()，服务端的 _renumber_beats 没跑过——拆完页面上会
// 并排出现两个 B03，用户还得记着再点一次「保存」。落盘顺带把校验也重跑了，
// 「这一刀拆出了什么问题」当场就能看见。
async function replicaPersistBeats(message) {
    const ok = await replicaSaveBeats(false);
    replicaRefreshBeats();
    replicaToast(ok ? message : '改动只在本地，落盘失败——请再点一次「保存并重校验」', !ok);
}

function replicaSplitBeat(idx) {
    const doc = replicaCollectBeats();
    if (!doc) return;
    const beat = doc.beats[idx];
    const mid = Math.round(((beat.start + beat.end) / 2) * 1000) / 1000;
    if (mid <= beat.start || mid >= beat.end) { replicaToast('拍窗太短，拆不开', true); return; }
    const second = JSON.parse(JSON.stringify(beat));
    beat.end = mid;
    second.start = mid;
    // 事件与证据帧不自动分配：谁属于哪一半只有看过帧才知道，替用户猜等于制造假事实。
    second.source_event_ids = [];
    doc.beats.splice(idx + 1, 0, second);
    replicaState.beats = doc;
    // 服务端会重排 id，折叠状态是按 id 存的：不清掉，旧的 B05 变成 B06 之后，
    // 新占用 B05 这个名字的那一拍会莫名其妙带着上一任的折叠态。
    replicaBeatFoldState = {};
    replicaFieldFoldState = {};
    replicaPersistBeats('已拆成两拍并保存。事件与证据帧没有自动分配——请对着帧手工分给正确的那一半。');
}

function replicaMergeBeat(idx) {
    const doc = replicaCollectBeats();
    if (!doc || idx === 0) return;
    const prev = doc.beats[idx - 1];
    const cur = doc.beats[idx];
    prev.end = cur.end;
    prev.state_after = cur.state_after;
    prev.visible_result = cur.visible_result;
    prev.source_event_ids = [...(prev.source_event_ids || []), ...(cur.source_event_ids || [])];
    // 变体那一份叫 reference_frames。写死 evidence_frames 会同时丢掉参考帧、并给变体
    // 凭空造出一个空的 evidence_frames——两边都会在合成卡点上报错。
    const frameKey = prev.evidence_frames || cur.evidence_frames ? 'evidence_frames' : 'reference_frames';
    prev[frameKey] = [...(prev[frameKey] || []), ...(cur[frameKey] || [])];
    prev.persistent_traces = [...(prev.persistent_traces || []), ...(cur.persistent_traces || [])];
    doc.beats.splice(idx, 1);
    replicaState.beats = doc;
    replicaBeatFoldState = {};   // 同 replicaSplitBeat：id 会重排
    replicaFieldFoldState = {};
    replicaPersistBeats('已合并并保存。若两拍是不同的物理工序，上方校验会告诉你。');
}

function replicaNudgeBeatTime(idx, field, delta) {
    const doc = replicaCollectBeats();
    if (!doc || !doc.beats || !doc.beats[idx]) return;
    const beat = doc.beats[idx];
    if (field === 'start') {
        const newVal = Math.max(0, Math.round((beat.start + delta) * 10) / 10);
        if (newVal >= beat.end) { replicaToast('起始时间不能晚于结束时间', true); return; }
        const oldStart = beat.start;
        beat.start = newVal;
        if (idx > 0 && Math.abs(doc.beats[idx - 1].end - oldStart) < 0.05) {
            doc.beats[idx - 1].end = newVal;
        }
    } else if (field === 'end') {
        const newVal = Math.round((beat.end + delta) * 10) / 10;
        if (newVal <= beat.start) { replicaToast('结束时间不能早于起始时间', true); return; }
        const oldEnd = beat.end;
        beat.end = newVal;
        if (idx < doc.beats.length - 1 && Math.abs(doc.beats[idx + 1].start - oldEnd) < 0.05) {
            doc.beats[idx + 1].start = newVal;
        }
    }
    replicaState.beats = doc;
    replicaPersistBeats(`已微调 ${beat.id} 的${field === 'start' ? '起始' : '结束'}时间至 ${field === 'start' ? beat.start : beat.end}s 并保存。`);
}

/* --- SSE --- */

// 服务端的 SSE 帧统一是 {"type": ..., "data": ...}（见 server._open_sse_stream）。
// 此前这个文件到处直接读 JSON.parse(e.data).stage / .message —— 读的是信封而不是信，
// 恒为 undefined：进度框里那个 chip 一直是空的、失败提示恒为「任务失败」四个字。
// 老格式（不带信封）也一并兼容，免得 replay 的历史事件形态不同就炸。
function replicaEventPayload(event) {
    if (!event || !event.data) return null;
    let parsed;
    try {
        parsed = JSON.parse(event.data);
    } catch (err) {
        return null;
    }
    if (parsed && typeof parsed === 'object' && 'type' in parsed && 'data' in parsed) {
        return parsed.data;
    }
    return parsed;
}

function replicaOpenSSE(taskId) {
    if (replicaSSE) replicaSSE.close();
    const code = typeof ACCESS_CODE !== 'undefined' ? ACCESS_CODE : '';
    replicaSSE = new EventSource(
        `/api/compose-stream?task_id=${encodeURIComponent(taskId)}${code ? '&access_code=' + encodeURIComponent(code) : ''}`);

    replicaResetProgress();

    replicaSSE.addEventListener('replica_stage', (e) => {
        replicaHandleStageEvent(replicaEventPayload(e) || {});
    });

    // 合成器的事件此前没有任何监听器：整个 compose 阶段（复刻线最长的一段）在页面上
    // 表现为一句话之后长时间静止，用户无从判断是在跑还是已经卡死。
    REPLICA_COMPOSER_EVENTS.forEach(type => {
        replicaSSE.addEventListener(type, (e) => {
            replicaHandleComposerEvent(type, replicaEventPayload(e));
        });
    });

    const finish = async (msg, isError, targetJobId) => {
        if (replicaSSE) { replicaSSE.close(); replicaSSE = null; }
        replicaTaskId = null;
        replicaSetBusy(false);
        try {
            await replicaLoadJobs();
            let target = targetJobId;
            if (!target) {
                const newest = replicaJobs[0];
                target = (newest && newest.variant_of === (replicaState || {}).job_id)
                    ? newest.job_id : (replicaState || {}).job_id;
            }
            if (target) await replicaLoadJob(target);
        } catch (err) { /* 列表刷新失败不该盖掉上面的完成/失败提示 */ }
        replicaRender();
        if (msg) replicaToast(msg, isError);
    };

    // 停下来了，但停在哪个卡点决定了下一步该干什么。原先三个卡点共用一句"请核对节拍"，
    // 停在成本确认或门禁未过时那句话是错的。
    const PAUSE_MESSAGES = {
        confirm_cost: '抽帧完成，还没开始花钱。选好采样档位再按「确认并开始反推」。',
        review_beats: '已停在人工卡点，请对着证据帧核对节拍。',
        audit_failed: '命中禁用元素，已拦下交付（未入库）。见下方提示词区。',
    };
    replicaSSE.addEventListener('replica_paused', (e) => {
        const payload = replicaEventPayload(e) || {};
        const stage = payload.stage || '';
        const targetJobId = payload.job_id || (payload.job_state && payload.job_state.job_id);
        finish(PAUSE_MESSAGES[stage] || '任务已暂停，请看当前阶段的操作区。',
               stage === 'audit_failed', targetJobId);
    });
    replicaSSE.addEventListener('result', (e) => {
        const payload = replicaEventPayload(e) || {};
        const targetJobId = payload.job_id || (payload.job_state && payload.job_state.job_id);
        finish('提示词包已生成，并已写入创意库', false, targetJobId);
    });
    replicaSSE.addEventListener('error', (e) => {
        // EventSource 把两种完全不同的东西塞进同一个事件名：服务端发的 `error` 事件
        // （带 data，是真的任务失败），和浏览器的连接层错误（无 data，重连期间也会
        // 触发）。不区分就会在一次网络抖动后把用户的任务报成失败。
        if (!e.data) {
            if (replicaSSE && replicaSSE.readyState === EventSource.CLOSED) {
                finish('与服务端的连接已断开，正在拉取最新状态');
            }
            return;
        }
        finish((replicaEventPayload(e) || {}).message || '任务失败', true);
    });
}

/* --- 入口 --- */

async function replicaTabEntered() {
    try {
        await replicaLoadJobs();
        if (!replicaState && replicaJobs.length) {
            await replicaLoadJob(replicaJobs[0].job_id);
        }
    } catch (e) {
        console.error('replicaTabEntered', e);
    }
    replicaRender();
    replicaReattach();
}

// 刷新页面 / 切走再回来之后，把还在跑的任务接回来。
//
// 在此之前这里什么都不做：一个跑了十五分钟的 Pass A，刷新一次就彻底失联——页面不显示
// 任何"在跑"的迹象，还会摆出「开始反推」按钮，用户再点一次就把同一笔视觉调用付两遍。
// job 行上的 active_task_id 由 /api/replica/jobs 下发（服务端按 replica_job_id 在
// ACTIVE_TASKS 里找 running 的那条）。
function replicaReattach() {
    if (replicaSSE || !replicaState) return;
    const row = replicaJobs.find(j => j.job_id === replicaState.job_id);
    const taskId = row && row.active_task_id;
    if (!taskId) return;
    replicaTaskId = taskId;
    replicaSetBusy(true);
    replicaOpenSSE(taskId);
    replicaRender();   // 让「中断这一轮」和进度条在重连后立刻可见
    replicaToast('这条任务还在后台跑，已重新接上进度');
}

async function replicaCancelRun() {
    if (!replicaState) return;
    if (!window.confirm('中断正在跑的这一轮？已经读过的帧会留在缓存里，重试时不重复付费。')) return;
    try {
        await replicaFetch('/api/replica/cancel', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id }),
        });
        replicaToast('已请求中断，正在等当前这一步收尾…');
    } catch (e) {
        replicaToast(e.message, true);
    }
}

/* --- 全局守卫与快捷键 --- */

// 关页面 / 刷新前的最后一道拦截。节拍改动只落在内存里，浏览器不问的话它们就这么没了。
//
// 只在真的有未保存改动时挂钩子：无条件返回字符串会让每一次正常关页都弹一次确认，
// 用户很快就会学会无脑点「离开」，等到真该拦的那一次也照点不误。
window.addEventListener('beforeunload', (e) => {
    if (!replicaDirty) return;
    e.preventDefault();
    e.returnValue = '';   // Chrome/Safari 仍要求写这一个才弹
});

// Cmd/Ctrl + S：保存并重校验。
//
// 这一页的核心动作是「改一拍、存一次」，几十拍就是几十趟鼠标跑到吸底栏。
// 面板守卫是必须的，不是保险：app.js 里那条 Ctrl+Enter 就是因为没守卫，在图像工坊
// 按一下会静默发起一次隐藏的合成任务（注释还留在 app.js 的监听器里）。这里同理——
// 复刻页不可见时必须原样放行给浏览器的「保存网页」。
document.addEventListener('keydown', (e) => {
    if (!((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's')) return;
    // 可见性按面板的 mobile-active 判，与 app.js 里 Ctrl+Enter 那条守卫同一口径。
    const panel = document.getElementById('panel-replica');
    if (!panel || !panel.classList.contains('mobile-active')) return;
    if (!replicaState || !((replicaState.beats || {}).beats || []).length) return;
    if (replicaBusy) {                                       // 跑着的时候字段是只读的，存了也没意义
        e.preventDefault();
        replicaToast('这一轮还在跑，跑完再存', true);
        return;
    }
    e.preventDefault();
    replicaSaveBeats(true, document.getElementById('replica-bar-save-btn'));
});
