// 日志语义映射：把一行行技术日志归成「事件」，用不懂代码的人也能读懂的话描述。
//
// 纯函数、不碰 DOM，浏览器和 Node 单测共用（见 tests/test_log_semantics.js）。
// 规则表改一行不用重启服务端——分类整个在浏览器侧做，服务端送来的仍是原始文本。
//
// severity 的语义（同时决定卡片长相和要不要出现）：
//   'error'   需要你处理，不会自己好
//   'warn'    重试或生成提醒；不等同于任务失败
//   'ignore'  认识它，且确定不用管——不出卡、不计数
//
// 'ignore' 不是可有可无的：实测日志里出现次数最多的 ERROR 是
// 「用户取消了生成任务」（11 次）——那是用户自己点的取消按钮，把它算进"错误 11"
// 恰恰是最典型的无效信息。
(function (global) {
    'use strict';

    // 顺序即优先级，第一条命中的规则生效。
    // 具体规则必须排在宽泛规则前面：「用户取消」在所有任务失败规则之前，
    // 「账号全部用尽」在泛化的「限流」之前（前者是终态，后者还在重试）。
    const RULES = [
        { id: 'log-reader-disconnected', severity: 'ignore', match: /SSE client disconnected/i },
        // ── 认识但不用管 ──────────────────────────────────────────────
        {
            id: 'user-cancelled',
            severity: 'ignore',
            match: /用户取消(?:了)?|已被用户取消|GenerationCancelled|收到取消请求/,
        },

        // ── 需要你处理（不会自愈） ────────────────────────────────────
        {
            id: 'fx-login-required',
            severity: 'error',
            match: /MANUAL_REQUIRED|login_required|需要人工处理/,
            title: 'Google FX 需要你手动登录',
            hint: '生图浏览器已经打开 Google FX，但停在登录页。切到那个浏览器窗口登录完，任务会自己继续。',
            action: { label: '操作说明', href: 'docs/guides/google_fx_console_manual.md' },
        },
        {
            id: 'accounts-exhausted',
            severity: 'error',
            match: /All accounts exhausted|所有账号.{0,4}(?:用尽|耗尽)/,
            title: '生图账号全部被限流，任务已中断',
            hint: '号池里每个账号都撞上了上游的频率限制。等十几分钟通常能恢复；如果反复出现，多半是池子里可用账号太少。',
            action: { label: '打开号池', section: 'pool' },
        },
        {
            id: 'adspower-start-failed',
            severity: 'error',
            match: /AdsPower 启动失败/,
            title: '生图浏览器启动失败',
            hint: '先确认 AdsPower 客户端已经打开并且登录了，然后重试任务。',
        },
        {
            id: 'cv2-missing',
            severity: 'error',
            match: /No module named 'cv2'|帧间调色不可用/,
            title: '帧间调色不可用（缺少 cv2）',
            hint: '成片仍然出得来，但各帧之间的色调一致性会变差。装上 opencv-python 就能恢复。',
        },
        {
            id: 'missing-anchor-frame',
            severity: 'error',
            match: /所需的(?:起始|结束)帧.{0,20}不存在/,
            title: '合成视频缺少所需的帧',
            hint: '要用的那张帧还没生成或已被删掉。先把对应的帧重新生成，再合成视频。',
        },
        {
            id: 'nothing-to-repair',
            severity: 'error',
            match: /当前没有记录待修复的问题/,
            title: '这一帧还没写要修什么',
            hint: '点「修复」之前，先在帧网格里点「描述问题」写清楚哪儿不对，或者先跑一次一致性审查。',
        },
        {
            id: 'flow-restricted',
            severity: 'warn',
            match: /unusual activity|异常活动|风控拦截/i,
            title: 'Flow 暂时限制了这次请求',
            hint: '请查看当前任务是否仍在恢复。只有任务已停止时才需要手动重试。',
        },
        {
            id: 'api-auth',
            severity: 'error',
            match: /Unauthorized|Invalid API key|API ?key.{0,10}(?:invalid|无效|过期)|HTTP (?:401|403)/i,
            title: 'API 密钥无效或已过期',
            hint: '服务端拒绝了这次调用。到「API 配置中心」检查密钥填得对不对、还在不在有效期内。',
            action: { label: '打开配置', section: 'backend' },
        },
        {
            id: 'quota-exhausted',
            severity: 'error',
            match: /insufficient[_ ]?(?:balance|quota)|余额不足|配额(?:不足|已用尽)|exceeded your current quota/i,
            title: '上游配额或余额不足',
            hint: '账号连得上，但没有可用额度了。充值或者换一个账号再重试。',
        },

        // ── 系统正在自愈（通常不用管） ────────────────────────────────
        {
            id: 'rate-limited',
            severity: 'warn',
            match: /Too Many Requests|HTTP (?:Error )?429|限流|限频|rate.?limit/i,
            title: '上游请求暂时受限',
            hint: '一次请求被上游限制；是否仍在自动重试，以当前任务状态为准。',
        },
        {
            id: 'upstream-retry',
            severity: 'warn',
            match: /upstream_retry|(?:外层)?尝试 \d+\/\d+ 失败|后重试/,
            title: '上游调用失败，正在重试',
            hint: '单次请求没成功，系统会按次数上限自动重试；只有重试全用完才会真的中断。',
        },
        {
            id: 'timeout',
            severity: 'warn',
            match: /Request timed out|TimeoutError|timed out|响应超时/i,
            title: '响应超时',
            hint: '某次请求等太久被放弃了。偶发一两次正常，密集出现通常是网络不稳。',
        },
        {
            id: 'review-flagged',
            severity: 'warn',
            match: /未通过一致性审查|sequence_review_flagged/,
            title: '有帧没通过一致性审查',
            hint: '这一帧和前面的画面对不上，已经按你确认的风险强行用了。留意成片里的跳变。',
        },
        {
            id: 'spatial-break',
            severity: 'warn',
            match: /疑似空间断裂|差异过大（MAD/,
            title: '首尾帧差异过大，中间可能拼不顺',
            hint: '这一段的首尾画面差太远，运动会显得生硬。建议改成硬切，或者重渲这两帧。',
        },
        {
            id: 'stale-lineage',
            severity: 'warn',
            match: /血统过期|跨链跳变/,
            title: '帧血统过期，已拦截',
            hint: '上游那张帧被单独重渲过，接着用会导致画面跳变。按提示顺序重渲后续帧即可。',
        },
        {
            id: 'network-drop',
            severity: 'warn',
            match: /WinError 100(?:53|54)|Connection (?:aborted|reset)|连接被中止|Client disconnected/i,
            title: '本机连接被中断了一次',
            hint: '一次连接中断已记入日志。当前是否已恢复，请查看日志同步和任务状态。',
        },
        {
            id: 'fx-watchdog',
            severity: 'warn',
            match: /看门狗|FX_WATCHDOG/,
            title: '生图队列卡住，已自动解锁',
            hint: '有个任务超时后没退出，看门狗强行释放了它占的位置。后续任务不受影响。',
        },
    ];

    // 概览页脚的"完成"计数认这些行。
    const DONE_RE = /帧序列任务完成|视频序列任务完成|\[TASK\]\s+result\b|video_done/;

    const FALLBACK_TITLE_MAX = 60;

    function textOf(entry) {
        if (!entry) return '';
        return entry.raw || entry.text || '';
    }

    function eventTypeOf(entry) {
        const text = (entry && entry.text) || textOf(entry);
        const match = String(text).match(/(?:^|\]\s+)(video_error|video_done|video_warning|video_retry_autonomous|upstream_retry|frame_start|frame|result|error|manual_intervention_detected|manual_intervention_cleared|manual_intervention_timeout|ip_rotating|ip_rotated|ip_rotation_failed)\b/);
        return match ? match[1] : '';
    }

    function slotOf(entry) {
        const match = textOf(entry).match(/(?:^|\s)(?:index|slot|sequence|seq)=(\d+)\b/);
        return match ? Number(match[1]) : null;
    }

    /** Chinese event labels, with terminal failures taking precedence over retry wording. */
    function classify(entry) {
        const text = textOf(entry);
        if (!text) return null;
        const type = eventTypeOf(entry);
        if (/^(manual_intervention_cleared|ip_rotated|video_done|frame|result)$/.test(type)) {
            return { id: 'recovery', severity: 'ignore' };
        }
        const rule = RULES.find(item => item.match.test(text)) || null;
        if (rule && rule.severity === 'ignore') return rule;
        if (type === 'video_error') {
            const slot = slotOf(entry);
            return {
                id: 'video-failed', severity: 'error',
                title: slot ? `第 ${slot} 段视频未生成成功` : '有视频片段未生成成功',
                hint: rule ? rule.hint : '请在视频列表查看这个片段；其他已完成片段会保留，可单独重试失败片段。',
                action: rule && rule.action,
            };
        }
        if (type === 'manual_intervention_detected') {
            return { id: 'fx-login-required', severity: 'error', title: '需要完成登录或验证',
                hint: '切到对应浏览器完成登录或验证，完成后任务会继续。' };
        }
        if (type === 'manual_intervention_timeout') {
            return { id: 'manual-timeout', severity: 'error', title: '等待人工处理已超时',
                hint: '请先完成浏览器中的登录或验证，再从项目里重试。' };
        }
        if (type === 'ip_rotation_failed') {
            return { id: 'ip-rotation-failed', severity: 'error', title: '网络恢复未成功',
                hint: '本次自动恢复已停止，请查看任务详情后重试。' };
        }
        if (type === 'video_retry_autonomous' || type === 'upstream_retry' || type === 'ip_rotating') {
            return { id: 'upstream-retry', severity: 'warn', title: '正在自动恢复',
                hint: '系统正在重试，暂时无需重复点击生成。' };
        }
        if (type === 'video_warning' && !rule) {
            return { id: 'video-warning', severity: 'warn', title: '视频生成有一条提醒',
                hint: '任务仍会继续；需要时展开明细查看原因。' };
        }
        if (rule && rule.severity === 'warn' && (type === 'error'
            || /任务失败|任务已中断|retries exhausted/i.test(text))
            && !/后重试|正在重试|尝试 \d+\/\d+ 失败/.test(text)) {
            return Object.assign({}, rule, { severity: 'error', title: '任务已停止，需要检查',
                hint: '自动处理没有完成任务。查看明细确认原因后，再从项目里重试。' });
        }
        return rule;
    }

    // 未识别的错误按"抹掉数字后的形状"归并：「第 3 帧失败」和「第 7 帧失败」是
    // 同一件事重复，不该占两张卡。与 api_client 的 shapeKey 同一思路。
    function shapeOf(text) {
        return String(text).replace(/\d+(\.\d+)?/g, '#').slice(0, 200);
    }

    function firstSentence(text) {
        const cleaned = String(text)
            .replace(/^\d{2}:\d{2}:\d{2}\.\d{3}\s+/, '')
            .replace(/\[\w+\s*\]\s*/, '')
            .replace(/\[task=[^\]]+\]\s*/, '')
            .trim();
        const cut = cleaned.split(/[。\n]/)[0].trim() || cleaned;
        return cut.length > FALLBACK_TITLE_MAX
            ? cut.slice(0, FALLBACK_TITLE_MAX) + '…'
            : cut;
    }

    /**
     * 一条日志属于哪张事件卡；不进概览的返回 null。
     * 聚合键 = 规则 id（未识别则用形状） + 任务 id：同一件事在同一个任务里重复
     * N 次算一张卡，标 ×N。放宽了"必须连续"的要求——两次限流之间往往夹着别的行。
     *
     * 单独导出，是为了让面板的「查看明细」能精确反查"这张卡的最后一行在哪"，
     * 而不是靠关键词去猜。
     */
    function eventKeyOf(entry) {
        const rule = classify(entry);
        if (rule) {
            if (rule.severity === 'ignore') return null;
            const slot = rule.id === 'video-failed' ? slotOf(entry) : null;
            return rule.id + (slot === null ? '' : ':' + slot) + ' ' + ((entry && entry.task) || '');
        }
        if (!entry || entry.level !== 'ERROR') return null;
        return 'unknown ' + shapeOf(textOf(entry)) + ' ' + (entry.task || '');
    }

    /** 把日志条目聚成事件卡（error 在前，同级最近的在前）。 */
    function aggregate(entries, options = {}) {
        const byKey = new Map();
        const list = Array.isArray(entries) ? entries : [];

        for (let i = 0; i < list.length; i++) {
            const entry = list[i];
            const type = eventTypeOf(entry);
            const task = entry.task || '';
            const slot = slotOf(entry);
            if (task) {
                for (const ev of byKey.values()) {
                    if (ev.task !== task) continue;
                    const videoRecovered = type === 'video_done' && slot !== null
                        && ev.id === 'video-failed' && ev.slot === slot;
                    const loginRecovered = type === 'manual_intervention_cleared'
                        && ev.id === 'fx-login-required';
                    const retryRecovered = /^(frame|video_done|ip_rotated)$/.test(type)
                        && ['upstream-retry', 'rate-limited', 'flow-restricted', 'timeout', 'network-drop', 'ip-rotation-failed'].includes(ev.id);
                    const cancelled = /用户取消(?:了)?|已被用户取消|GenerationCancelled/.test(textOf(entry));
                    const completed = type === 'result'
                        && /completion_state=completed(?:\s|$)/.test(textOf(entry));
                    if (videoRecovered || loginRecovered || retryRecovered || cancelled || completed) ev.resolved = true;
                }
            }
            const rule = classify(entry);
            let key, seed;

            if (rule) {
                if (rule.severity === 'ignore') continue;
                key = eventKeyOf(entry);
                seed = {
                    id: rule.id,
                    severity: rule.severity,
                    title: rule.title,
                    hint: rule.hint,
                    action: rule.action || null,
                    recognized: true,
                };
            } else {
                // 兜底：只有未识别的 ERROR 才进概览。宁可多出一张不好看的卡，
                // 也不能把没人认识的错误静默吞掉。WARN/INFO/OTHER 未命中规则就
                // 不进概览——概览的准入门槛是"值得人看一眼"。
                if (entry.level !== 'ERROR') continue;
                const text = textOf(entry);
                key = eventKeyOf(entry);
                seed = {
                    id: 'unknown',
                    severity: 'error',
                    title: firstSentence(entry.text || text),
                    hint: '这条错误还没有对应的说明，点「查看明细」看完整信息。',
                    action: null,
                    recognized: false,
                };
            }

            let ev = byKey.get(key);
            if (!ev) {
                ev = Object.assign(seed, {
                    key: key,
                    task: entry.task || '',
                    slot: slotOf(entry),
                    resolved: false,
                    unread: false,
                    count: 0,
                    firstTime: entry.time || '',
                    lastTime: entry.time || '',
                    samples: [],
                });
                byKey.set(key, ev);
            }
            // 一行本身可能已经是折叠过的 ×N（api_client 的连续重复折叠）
            Object.assign(ev, seed);
            ev.resolved = false;
            ev.unread = ev.unread || !!entry.unread;
            ev.count += entry.repeatCount || 1;
            if (entry.time) {
                if (!ev.firstTime) ev.firstTime = entry.time;
                ev.lastTime = entry.lastTime || entry.time;
            }
            if (ev.samples.length < 5) ev.samples.push(textOf(entry));
        }

        // error 在前，同级按最近发生的在前。
        // 注意用 hasOwnProperty 而不是 `order[x] || 9`：error 的序号是 0，会被
        // || 当成假值换成 9，排序直接反过来。
        const order = { error: 0, warn: 1 };
        const rank = (s) => (Object.prototype.hasOwnProperty.call(order, s) ? order[s] : 9);
        const tasks = options.tasks || {};
        for (const ev of byKey.values()) {
            const task = tasks[ev.task];
            if (!task) continue;
            const result = task.result || {};
            const outcome = task.outcome || result.completion_state;
            if (task.status === 'cancelled' || (task.status === 'completed'
                && outcome === 'completed' && !result.has_failures)) {
                ev.resolved = true;
            } else if (task.status === 'completed' && outcome === 'completed_with_warnings'
                && !['review-flagged', 'spatial-break', 'stale-lineage', 'video-warning'].includes(ev.id)) {
                ev.resolved = true;
            } else if (task.status === 'completed' && outcome === 'partial_failed'
                && ['upstream-retry', 'rate-limited', 'flow-restricted', 'timeout', 'network-drop'].includes(ev.id)) {
                ev.resolved = true;
            } else if (task.status === 'failed' && ev.severity === 'warn') {
                ev.severity = 'error';
                ev.title = '任务已停止，需要检查';
                ev.hint = '自动处理没有完成任务。请查看任务详情后重试。';
            }
        }
        return Array.from(byKey.values()).filter(ev => options.includeResolved || !ev.resolved).sort(function (a, b) {
            const d = rank(a.severity) - rank(b.severity);
            if (d !== 0) return d;
            return String(b.lastTime).localeCompare(String(a.lastTime));
        });
    }

    /** 页脚统计 + 状态条那句话。 */
    function summarize(entries, events) {
        const list = Array.isArray(entries) ? entries : [];
        const evs = events || aggregate(list);
        let error = 0;
        let warn = 0;
        for (let i = 0; i < evs.length; i++) {
            if (evs[i].resolved) continue;
            if (evs[i].severity === 'error') error++;
            else if (evs[i].severity === 'warn') warn++;
        }
        const completedTasks = new Set();
        let unscopedDone = 0;
        for (const entry of list) {
            if (!DONE_RE.test(textOf(entry))) continue;
            if (entry.task) completedTasks.add(entry.task);
            else unscopedDone++;
        }
        const done = completedTasks.size + unscopedDone;

        let headline;
        let tone;
        if (error > 0) {
            headline = error === 1 ? '有 1 件事需要你处理' : `有 ${error} 件事需要你处理`;
            tone = 'error';
        } else if (warn > 0) {
            headline = '有生成提醒，可查看当前任务进度';
            tone = 'warn';
        } else {
            headline = '日志已同步，暂无待处理事项';
            tone = 'ok';
        }
        return { error: error, warn: warn, done: done, headline: headline, tone: tone };
    }

    function taskIdsForProject(tasks, idea, records = {}) {
        if (!idea) return null; // No selected project: show all tasks.
        const ids = new Set(Object.values(records || {}).map(row => row && row.taskId).filter(Boolean));
        const projectKey = idea.project_key || '';
        for (const task of tasks || []) {
            if (!task || !task.id) continue;
            const dimensions = task.dimensions || {};
            const taskProject = dimensions.project_key || task.result?.project_key || '';
            if (task.id === idea.id || (projectKey && taskProject === projectKey)
                || (!projectKey && !taskProject && idea.title && (dimensions.theme || dimensions.task_label) === idea.title)) {
                ids.add(task.id);
            }
        }
        return ids;
    }

    const api = {
        RULES: RULES,
        classify: classify,
        eventTypeOf: eventTypeOf,
        taskIdsForProject: taskIdsForProject,
        eventKeyOf: eventKeyOf,
        aggregate: aggregate,
        summarize: summarize,
        // 单测用
        _shapeOf: shapeOf,
        _firstSentence: firstSentence,
    };

    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    global.SparkLogSemantics = api;
})(typeof window !== 'undefined' ? window : globalThis);
