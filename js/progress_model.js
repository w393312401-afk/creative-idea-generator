// Shared progress event normalizer for browser UI and lightweight Node tests.
(function (root) {
    function clamp(n, min, max) {
        return Math.max(min, Math.min(max, Number.isFinite(n) ? n : min));
    }

    function toPercent(n) {
        return Math.round(clamp(n, 0, 100));
    }

    function copyState(previousState, taskType) {
        const prev = previousState || {};
        return {
            taskType: taskType || prev.taskType || 'compose',
            percent: Number(prev.percent) || 0,
            outlineCount: Number(prev.outlineCount) || 0,
            total: Number(prev.total) || 0,
            slotStatus: { ...(prev.slotStatus || {}) },
            repairCount: Number(prev.repairCount) || 0,
            phase: prev.phase || 'pending',
            label: prev.label || '',
            message: prev.message || ''
        };
    }

    function unwrapEvent(eventType, eventData) {
        if (eventType === 'progress' && eventData && typeof eventData === 'object' && eventData.stage) {
            return { stage: eventData.stage, details: eventData.details };
        }
        return { stage: eventType, details: eventData };
    }

    function inferTaskType(dimensions) {
        const d = dimensions || {};
        const explicit = String(d.type || '').trim();
        if (explicit) return explicit;
        return 'compose';
    }

    function messageFrom(details, fallback) {
        if (typeof details === 'string') return details;
        if (details && typeof details.message === 'string') return details.message;
        return fallback || '';
    }

    function markSlot(state, slot, status) {
        if (slot !== undefined && slot !== null && slot !== '') {
            state.slotStatus[String(slot)] = status;
        }
    }

    function terminalSlotCount(state) {
        return Object.values(state.slotStatus).filter(v => v === 'done' || v === 'failed').length;
    }

    function normalizeCompose(stage, details, state) {
        let phase = stage;
        let label = messageFrom(details, state.label || '准备中...');
        let percent = state.percent;
        let current = null;
        let total = null;
        let status = 'running';
        let slot = null;

        if (stage === 'outline') {
            state.outlineCount += 1;
            const outlinePercents = [5, 15, 28, 38];
            percent = outlinePercents[Math.min(state.outlineCount - 1, outlinePercents.length - 1)];
            phase = 'outline';
        } else if (stage === 'batch') {
            current = Number(details && details.current) || 0;
            total = Number(details && details.total) || state.total || 0;
            state.total = total || state.total;
            const ratio = total ? clamp(current / total, 0, 1) : 0;
            percent = 40 + ratio * 45;
            label = total ? `批量合成提示词 ${current}/${total}` : '批量合成提示词中';
            phase = 'batch';
        } else if (stage === 'audit') {
            const msg = messageFrom(details, '正在运行质量审计...');
            const isRepairRound = /修复|repair|轮/i.test(msg);
            if (isRepairRound) state.repairCount += 1;
            percent = isRepairRound ? 88 : 90;
            label = isRepairRound && state.repairCount > 0 ? `${msg}（第 ${state.repairCount} 次）` : msg;
            phase = 'audit';
        } else if (stage === 'repair') {
            percent = 95;
            label = messageFrom(details, '正在修复并重新组装提示词...');
            phase = 'repair';
        } else if (stage === 'text_chunk') {
            percent = 40;
            label = '流式合成中...';
            phase = 'streaming';
        } else if (stage === 'result') {
            percent = 100;
            label = '生成完成';
            status = 'completed';
            phase = 'result';
        } else if (stage === 'error') {
            label = messageFrom(details, '生成失败');
            status = 'failed';
            phase = 'error';
        }

        return { phase, label, percent, current, total, slot, status };
    }

    function normalizeFrames(stage, details, state) {
        let phase = stage;
        let label = messageFrom(details, state.label || '准备生成帧序列...');
        let percent = state.percent;
        let current = null;
        let total = Number(details && details.total) || state.total || 0;
        let status = 'running';
        let slot = details && (details.sequence || details.slot);

        if (stage === 'queue') {
            percent = 3;
            phase = 'queue';
        } else if (stage === 'start') {
            state.total = total;
            percent = 5;
            label = total ? `开始生成 ${total} 张图片` : '开始生成图片';
        } else if (stage === 'frame_start') {
            state.total = total || state.total;
            percent = Math.max(state.percent, 5);
            label = slot ? `正在生成 IMG ${String(slot).padStart(3, '0')}` : '正在生成图片';
            status = 'active';
        } else if (stage === 'frame_retry') {
            state.total = total || state.total;
            const attempt = details && details.attempt;
            const reason = details && details.reason;
            percent = Math.max(state.percent, 5);
            label = slot ? `IMG ${String(slot).padStart(3, '0')} 质检重试${attempt ? ` ${attempt}` : ''}` : '图片质检重试中';
            if (reason) label += `：${reason}`;
            status = 'retrying';
        } else if (stage === 'upstream_retry') {
            // 上游失败即时广播（毫秒级）：区别于 frame_retry（质检不过重生），
            // 这是 HTTP 层报错后的自动退避重试
            const attempt = details && details.attempt;
            const maxA = details && details.max_attempts;
            percent = Math.max(state.percent, 5);
            label = `上游报错，自动重试中${attempt ? `（第 ${attempt}/${maxA || '?'} 次）` : ''}`;
            status = 'retrying';
        } else if (stage === 'frame_qa') {
            percent = Math.max(state.percent, 5);
            label = slot ? `IMG ${String(slot).padStart(3, '0')} 质检判定中` : '质检判定中';
            status = 'active';
        } else if (stage === 'model_fallback') {
            const fbModel = details && details.to;
            percent = Math.max(state.percent, 5);
            label = `主模型配额耗尽，兜底模型${fbModel ? ` ${fbModel}` : ''}渲染中`;
            status = 'retrying';
        } else if (stage === 'frame') {
            current = Number(details && details.current) || 0;
            total = Number(details && details.total) || state.total || 0;
            state.total = total || state.total;
            slot = details && details.frame && (details.frame.sequence || details.frame.slot);
            markSlot(state, slot, 'done');
            const ratio = total ? clamp(current / total, 0, 1) : 0;
            percent = 5 + ratio * 90;
            label = total ? `图片生成 ${current}/${total}` : '图片生成中';
            status = 'done';
        } else if (stage === 'result') {
            percent = 100;
            label = '图片序列生成完成';
            status = 'completed';
        } else if (stage === 'error') {
            label = messageFrom(details, '图片生成失败');
            status = 'failed';
        }

        return { phase, label, percent, current, total, slot, status };
    }

    function normalizeVideos(stage, details, state) {
        let phase = stage;
        let label = messageFrom(details, state.label || '准备生成视频...');
        let percent = state.percent;
        let current = null;
        let total = Number(details && details.total) || state.total || 0;
        let status = 'running';
        let slot = details && details.index;

        if (stage === 'queue') {
            percent = 3;
            phase = 'queue';
        } else if (stage === 'start') {
            state.total = total;
            percent = 5;
            label = total ? `开始生成 ${total} 段视频` : '开始生成视频';
        } else if (stage === 'video_start') {
            current = Number(details && details.current) || null;
            state.total = total || state.total;
            markSlot(state, slot, 'active');
            percent = Math.max(state.percent, 5);
            label = slot ? `正在生成 VID ${String(slot).padStart(3, '0')}` : '正在生成视频';
            status = 'active';
        } else if (stage === 'video_done') {
            current = Number(details && details.current) || null;
            state.total = total || state.total;
            markSlot(state, slot, 'done');
            const done = terminalSlotCount(state);
            const denom = state.total || total || done || 1;
            percent = 5 + clamp(done / denom, 0, 1) * 83;
            label = `视频段落完成 ${done}/${denom}`;
            status = 'done';
        } else if (stage === 'video_error') {
            current = Number(details && details.current) || null;
            state.total = total || state.total;
            markSlot(state, slot, 'failed');
            const done = terminalSlotCount(state);
            const denom = state.total || total || done || 1;
            percent = 5 + clamp(done / denom, 0, 1) * 83;
            label = slot ? `VID ${String(slot).padStart(3, '0')} 生成失败` : '视频生成失败';
            if (details && details.message) label += `：${details.message}`;
            status = 'failed-slot';
        } else if (stage === 'merge_start') {
            percent = 90;
            label = messageFrom(details, '正在合并并加速视频...');
            phase = 'merge';
        } else if (stage === 'merge_done') {
            percent = 98;
            label = '合并视频完成';
            phase = 'merge';
            status = 'done';
        } else if (stage === 'merge_error') {
            percent = 96;
            label = messageFrom(details, '合并视频失败，分段结果已保留');
            phase = 'merge';
            status = 'failed-slot';
        } else if (stage === 'merge_skip') {
            percent = 96;
            label = messageFrom(details, '已跳过合并，分段结果已保留');
            phase = 'merge';
            status = 'skipped';
        } else if (stage === 'result') {
            percent = 100;
            label = '视频序列生成完成';
            status = 'completed';
        } else if (stage === 'error') {
            label = messageFrom(details, '视频生成失败');
            status = 'failed';
        }

        return { phase, label, percent, current, total, slot, status };
    }

    function normalizeCover(stage, details, state) {
        let label = messageFrom(details, state.label || '封面图生成中...');
        let percent = state.percent || 20;
        let status = 'running';
        if (stage === 'result') {
            percent = 100;
            label = '封面图生成完成';
            status = 'completed';
        } else if (stage === 'error') {
            label = messageFrom(details, '封面图生成失败');
            status = 'failed';
        }
        return { phase: stage, label, percent, current: null, total: null, slot: null, status };
    }

    function normalizeGenerationProgress(eventType, eventData, taskType, previousState) {
        const resolvedTaskType = taskType || (previousState && previousState.taskType) || 'compose';
        const state = copyState(previousState, resolvedTaskType);
        const unwrapped = unwrapEvent(eventType, eventData);
        const stage = unwrapped.stage;
        const details = unwrapped.details;

        let result;
        if (resolvedTaskType === 'frames') {
            result = normalizeFrames(stage, details, state);
        } else if (resolvedTaskType === 'videos') {
            result = normalizeVideos(stage, details, state);
        } else if (resolvedTaskType === 'cover') {
            result = normalizeCover(stage, details, state);
        } else {
            result = normalizeCompose(stage, details, state);
        }

        const monotonicPercent = toPercent(Math.max(state.percent, result.percent || 0));
        const next = {
            ...state,
            taskType: resolvedTaskType,
            phase: result.phase,
            label: result.label,
            message: result.label,
            percent: monotonicPercent,
            total: result.total || state.total,
            slotStatus: state.slotStatus
        };

        return {
            phase: result.phase,
            label: result.label,
            percent: monotonicPercent,
            current: result.current,
            total: result.total || state.total || null,
            slot: result.slot,
            status: result.status,
            message: result.label,
            monotonic: true,
            state: next
        };
    }

    function progressFromEvents(events, taskType, terminalStatus) {
        let state = copyState(null, taskType);
        let current = normalizeGenerationProgress('init', null, taskType, state);
        (events || []).forEach(evt => {
            if (!Array.isArray(evt) || evt.length < 1) return;
            current = normalizeGenerationProgress(evt[0], evt[1], taskType, current.state);
            state = current.state;
        });
        if (terminalStatus === 'completed') {
            current = normalizeGenerationProgress('result', null, taskType, current.state);
        } else if (terminalStatus === 'failed' || terminalStatus === 'cancelled') {
            current = normalizeGenerationProgress('error', null, taskType, current.state);
        }
        return current;
    }

    function createProgressState(taskType) {
        return copyState(null, taskType);
    }

    const api = {
        normalizeGenerationProgress,
        progressFromEvents,
        createProgressState,
        inferTaskType
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
    root.ProgressModel = api;
    root.normalizeGenerationProgress = normalizeGenerationProgress;
})(typeof window !== 'undefined' ? window : globalThis);
