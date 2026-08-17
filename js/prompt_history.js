/* =====================================================================
   提示词版本历史与差异对比（Prompt History & Visual Diff Engine）
   ---------------------------------------------------------------------
   自动记录提示词集的每一次改动快照（初始生成、手动全局编辑、单条就地编辑、
   AI 修复、版本回退等），并提供基于行级 LCS 的可视化 Diff 差异对比（红绿增删）
   与一键安全版本回滚。
   ===================================================================== */

const PROMPT_HISTORY_MAX_VERSIONS = 30;

/**
 * 获取指定创意的提示词历史快照列表（按时间倒序，最新在最前）
 */
function getPromptHistory(ideaId) {
    if (!ideaId) return [];
    try {
        const raw = localStorage.getItem(`spark_prompt_history_${ideaId}`);
        if (!raw) return [];
        const list = JSON.parse(raw);
        return Array.isArray(list) ? list : [];
    } catch (e) {
        console.warn('Failed to load prompt history from localStorage:', e);
        return [];
    }
}

/**
 * 记录一次提示词历史快照
 */
function recordPromptHistory(ideaId, promptBlock, summary) {
    if (!ideaId || !promptBlock || typeof promptBlock !== 'string' || !promptBlock.trim()) return;
    const text = promptBlock.trim();

    try {
        const list = getPromptHistory(ideaId);
        
        // 如果最新的记录与当前内容完全一致，则不重复记录
        if (list.length > 0 && list[0].prompt_block.trim() === text) {
            return;
        }

        // 解析包含的图片和视频槽位数
        let imageCount = 0;
        let videoCount = 0;
        if (typeof parsePromptBlock === 'function') {
            const slots = parsePromptBlock(text);
            imageCount = slots.filter(s => s.type === 'image').length;
            videoCount = slots.filter(s => s.type === 'video').length;
        }

        const snapshot = {
            id: 'ph_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
            timestamp: Date.now(),
            summary: summary || '提示词更新',
            prompt_block: text,
            imageCount: imageCount,
            videoCount: videoCount
        };

        list.unshift(snapshot);
        if (list.length > PROMPT_HISTORY_MAX_VERSIONS) {
            list.length = PROMPT_HISTORY_MAX_VERSIONS;
        }

        localStorage.setItem(`spark_prompt_history_${ideaId}`, JSON.stringify(list));
    } catch (e) {
        console.warn('Failed to save prompt history:', e);
    }
}

/**
 * 清除指定创意的提示词历史
 */
function clearPromptHistory(ideaId) {
    if (!ideaId) return;
    try {
        localStorage.removeItem(`spark_prompt_history_${ideaId}`);
    } catch (e) {}
}

/**
 * 计算两段文本之间的行级差异（基于 LCS 最长公共子序列算法）
 * 返回 Array<{ type: 'context' | 'added' | 'deleted', line: string, oldLineNo?: number, newLineNo?: number }>
 */
function computeLineDiff(oldText, newText) {
    const oldLines = String(oldText || '').replace(/\r\n/g, '\n').split('\n');
    const newLines = String(newText || '').replace(/\r\n/g, '\n').split('\n');

    const m = oldLines.length;
    const n = newLines.length;

    // 构建 LCS 动态规划表
    const dp = Array.from({ length: m + 1 }, () => new Uint32Array(n + 1));
    for (let i = 1; i <= m; i++) {
        for (let j = 1; j <= n; j++) {
            if (oldLines[i - 1] === newLines[j - 1]) {
                dp[i][j] = dp[i - 1][j - 1] + 1;
            } else {
                dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
            }
        }
    }

    // 回溯构造 diff 序列
    const diff = [];
    let i = m;
    let j = n;

    while (i > 0 || j > 0) {
        if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
            diff.push({ type: 'context', line: oldLines[i - 1], oldLineNo: i, newLineNo: j });
            i--;
            j--;
        } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
            diff.push({ type: 'added', line: newLines[j - 1], newLineNo: j });
            j--;
        } else if (i > 0 && (j === 0 || dp[i][j - 1] < dp[i - 1][j])) {
            diff.push({ type: 'deleted', line: oldLines[i - 1], oldLineNo: i });
            i--;
        }
    }

    return diff.reverse();
}

/**
 * 格式化相对或绝对时间
 */
function formatHistoryTime(ts) {
    if (!ts) return '';
    const now = Date.now();
    const diffSec = Math.floor((now - ts) / 1000);
    if (diffSec < 60) return '刚刚';
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
    
    const d = new Date(ts);
    const m = (d.getMonth() + 1).toString().padStart(2, '0');
    const day = d.getDate().toString().padStart(2, '0');
    const hh = d.getHours().toString().padStart(2, '0');
    const mm = d.getMinutes().toString().padStart(2, '0');
    return `${m}-${day} ${hh}:${mm}`;
}

function escapeHtml(str) {
    return String(str || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

let activeHistoryIndex = 0;
let activeDiffMode = 'diff'; // 'diff' | 'raw'

/**
 * 打开提示词历史版本与 Diff 抽屉/模态框
 */
function openPromptHistoryModal() {
    if (!currentIdea || !currentIdea.prompt_block) {
        if (typeof showToast === 'function') showToast('当前未选择创意或该创意暂无提示词', 'warning');
        return;
    }

    let history = getPromptHistory(currentIdea.id);
    if (!history.length) {
        // 若尚无历史快照，将当前版本作为初始版本自动记录
        recordPromptHistory(currentIdea.id, currentIdea.prompt_block, '初始激发生成');
        history = getPromptHistory(currentIdea.id);
    }

    let modal = document.getElementById('prompt-history-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'prompt-history-modal';
        modal.className = 'prompt-history-modal-overlay';
        modal.innerHTML = `
            <div class="prompt-history-modal-container">
                <div class="prompt-history-modal-header">
                    <div class="prompt-history-title-wrap">
                        <span class="prompt-history-icon">📜</span>
                        <h3 class="prompt-history-title">提示词版本历史与差异对比</h3>
                        <span class="prompt-history-count" id="prompt-history-count-badge"></span>
                    </div>
                    <div class="prompt-history-header-actions">
                        <div class="prompt-history-view-toggle">
                            <button type="button" class="view-toggle-btn active" data-mode="diff" id="btn-toggle-diff-mode">Diff 对比</button>
                            <button type="button" class="view-toggle-btn" data-mode="raw" id="btn-toggle-raw-mode">版本全文</button>
                        </div>
                        <button type="button" class="action-btn icon-btn mini-btn prompt-history-close-btn" id="btn-close-prompt-history" title="关闭 (Esc)">
                            <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                        </button>
                    </div>
                </div>
                <div class="prompt-history-modal-body">
                    <div class="prompt-history-sidebar" id="prompt-history-sidebar"></div>
                    <div class="prompt-history-content" id="prompt-history-content"></div>
                </div>
                <div class="prompt-history-modal-footer">
                    <div class="prompt-history-footer-left">
                        <span class="diff-legend-item"><span class="legend-box legend-added"></span> 选中版本新增 (相比当前)</span>
                        <span class="diff-legend-item"><span class="legend-box legend-deleted"></span> 选中版本缺少 (相比当前)</span>
                    </div>
                    <div class="prompt-history-footer-actions">
                        <button type="button" class="action-btn text-btn" id="btn-copy-history-version">📋 复制此版本</button>
                        <button type="button" class="action-btn text-btn primary" id="btn-rollback-history-version">↺ 回退到此版本</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        // 绑定事件
        modal.querySelector('#btn-close-prompt-history').addEventListener('click', closePromptHistoryModal);
        modal.addEventListener('click', (e) => {
            if (e.target === modal) closePromptHistoryModal();
        });

        modal.querySelector('#btn-toggle-diff-mode').addEventListener('click', () => {
            activeDiffMode = 'diff';
            updateDiffViewModeButtons();
            renderPromptHistoryContent();
        });
        modal.querySelector('#btn-toggle-raw-mode').addEventListener('click', () => {
            activeDiffMode = 'raw';
            updateDiffViewModeButtons();
            renderPromptHistoryContent();
        });

        modal.querySelector('#btn-copy-history-version').addEventListener('click', () => {
            const list = getPromptHistory(currentIdea.id);
            const target = list[activeHistoryIndex];
            if (!target) return;
            const doCopy = typeof copyText === 'function' ? copyText(target.prompt_block) : navigator.clipboard.writeText(target.prompt_block);
            Promise.resolve(doCopy).then(() => {
                if (typeof showToast === 'function') showToast(`已复制选中的历史版本提示词`, 'success');
            });
        });

        modal.querySelector('#btn-rollback-history-version').addEventListener('click', () => {
            const list = getPromptHistory(currentIdea.id);
            const target = list[activeHistoryIndex];
            if (!target) return;
            rollbackToHistoryVersion(target);
        });

        window.addEventListener('keydown', (e) => {
            if (modal.classList.contains('active') && e.key === 'Escape') {
                closePromptHistoryModal();
            }
        });
    }

    activeHistoryIndex = 0;
    activeDiffMode = 'diff';
    updateDiffViewModeButtons();
    renderPromptHistorySidebar();
    renderPromptHistoryContent();

    modal.classList.add('active');
}

function updateDiffViewModeButtons() {
    const diffBtn = document.getElementById('btn-toggle-diff-mode');
    const rawBtn = document.getElementById('btn-toggle-raw-mode');
    if (diffBtn) diffBtn.classList.toggle('active', activeDiffMode === 'diff');
    if (rawBtn) rawBtn.classList.toggle('active', activeDiffMode === 'raw');
}

function closePromptHistoryModal() {
    const modal = document.getElementById('prompt-history-modal');
    if (modal) modal.classList.remove('active');
}

/**
 * 渲染历史快照左侧列表
 */
function renderPromptHistorySidebar() {
    const sidebar = document.getElementById('prompt-history-sidebar');
    const badge = document.getElementById('prompt-history-count-badge');
    if (!sidebar || !currentIdea) return;

    const list = getPromptHistory(currentIdea.id);
    if (badge) badge.textContent = `共 ${list.length} 个版本`;

    sidebar.innerHTML = '';
    if (!list.length) {
        sidebar.innerHTML = '<div class="prompt-history-empty">暂无历史快照</div>';
        return;
    }

    list.forEach((item, idx) => {
        const itemEl = document.createElement('div');
        itemEl.className = `prompt-history-item ${idx === activeHistoryIndex ? 'active' : ''}`;
        
        const isCurrent = (item.prompt_block.trim() === (currentIdea.prompt_block || '').trim());
        const timeStr = formatHistoryTime(item.timestamp);
        
        itemEl.innerHTML = `
            <div class="prompt-history-item-top">
                <span class="prompt-history-item-summary">${escapeHtml(item.summary)}</span>
                ${isCurrent ? '<span class="prompt-history-current-tag">当前生效</span>' : ''}
            </div>
            <div class="prompt-history-item-meta">
                <span class="prompt-history-item-time">${timeStr}</span>
                <span class="prompt-history-item-slots">${item.imageCount ? `${item.imageCount} 拍` : ''}${item.videoCount ? ` / ${item.videoCount} 视频` : ''}</span>
            </div>
        `;

        itemEl.addEventListener('click', () => {
            activeHistoryIndex = idx;
            renderPromptHistorySidebar();
            renderPromptHistoryContent();
        });

        sidebar.appendChild(itemEl);
    });
}

/**
 * 渲染右侧 Diff 对比视图或全文预览
 */
function renderPromptHistoryContent() {
    const container = document.getElementById('prompt-history-content');
    if (!container || !currentIdea) return;

    const list = getPromptHistory(currentIdea.id);
    const target = list[activeHistoryIndex];
    if (!target) {
        container.innerHTML = '<div class="prompt-history-empty">未选中任何历史版本</div>';
        return;
    }

    const currentText = currentIdea.prompt_block || '';
    const targetText = target.prompt_block || '';
    const isIdentical = (currentText.trim() === targetText.trim());

    if (activeDiffMode === 'raw') {
        // 全文预览模式
        container.innerHTML = `
            <div class="prompt-history-raw-view">
                <div class="prompt-history-view-banner">
                    <span>版本快照：${escapeHtml(target.summary)} (${formatHistoryTime(target.timestamp)})</span>
                    ${isIdentical ? '<span class="badge-same">与当前生效内容一致</span>' : ''}
                </div>
                <pre class="prompt-history-raw-code">${escapeHtml(targetText)}</pre>
            </div>
        `;
        return;
    }

    // Diff 对比模式 (以当前生效版本对比选定历史版本)
    const diffs = computeLineDiff(currentText, targetText);
    
    let addCount = 0;
    let delCount = 0;
    diffs.forEach(d => {
        if (d.type === 'added') addCount++;
        else if (d.type === 'deleted') delCount++;
    });

    let diffHtml = `
        <div class="prompt-history-diff-view">
            <div class="prompt-history-view-banner">
                <span class="banner-title">对比基准：当前生效版本 ➔ 选中历史版本 (${escapeHtml(target.summary)})</span>
                <span class="banner-stats">
                    ${isIdentical ? '<span class="diff-stat-same">两者内容完全相同</span>' : `<span class="diff-stat-add">+${addCount} 行</span> <span class="diff-stat-del">-${delCount} 行</span>`}
                </span>
            </div>
            <div class="prompt-diff-code-wrapper">
                <table class="prompt-diff-table">
                    <tbody>
    `;

    diffs.forEach(item => {
        const cls = item.type === 'added' ? 'diff-line-add' : (item.type === 'deleted' ? 'diff-line-del' : 'diff-line-ctx');
        const symbol = item.type === 'added' ? '+' : (item.type === 'deleted' ? '-' : ' ');
        const oldNo = item.oldLineNo ? item.oldLineNo : '';
        const newNo = item.newLineNo ? item.newLineNo : '';

        diffHtml += `
            <tr class="diff-row ${cls}">
                <td class="diff-num old-num">${oldNo}</td>
                <td class="diff-num new-num">${newNo}</td>
                <td class="diff-sign">${symbol}</td>
                <td class="diff-text">${escapeHtml(item.line) || '&nbsp;'}</td>
            </tr>
        `;
    });

    diffHtml += `
                    </tbody>
                </table>
            </div>
        </div>
    `;

    container.innerHTML = diffHtml;
}

/**
 * 将提示词回滚至指定历史版本
 */
async function rollbackToHistoryVersion(historyItem) {
    if (!currentIdea || !historyItem) return;
    const timeStr = formatHistoryTime(historyItem.timestamp);

    const isSame = (historyItem.prompt_block.trim() === (currentIdea.prompt_block || '').trim());
    if (isSame) {
        if (typeof showToast === 'function') showToast('当前已是该版本内容，无需回退。', 'info');
        return;
    }

    const confirmRollback = typeof customConfirm === 'function'
        ? await customConfirm(`确定要将提示词回退到 <b>「${escapeHtml(historyItem.summary)} (${timeStr})」</b> 版本吗？<br><br>回退后将触发槽位契约同步，提示词发生变化的画面帧会被标记为待重渲。`)
        : confirm(`确定要回退到 ${historyItem.summary} (${timeStr}) 版本吗？`);

    if (!confirmRollback) return;

    const targetText = historyItem.prompt_block;
    const rollbackSummary = `回退至 ${timeStr} 快照 (${historyItem.summary})`;

    const ok = await mutateSlot({
        what: '回退提示词版本',
        ownerIdea: currentIdea, scope: 'both', requirePrompt: true,
        request: () => slotPostJson('/api/edit_prompts', {
            title: getIdeaSaveTitle(currentIdea),
            prompt_block: targetText,
            prev_prompt_block: currentIdea.prompt_block || '',
        }),
        beforeApply: async (d) => {
            await applyPromptBlockToIdea(currentIdea, d.prompt_block, d.prompt_slots, true);
        },
        patch: (d) => ({ frames: d.frames, videos: d.videos, dropMerged: true }),
        success: (d) => `已成功回退提示词版本（共 ${d.image_count} 拍）。`,
        failure: (e) => `回退提示词失败: ${e.message}`,
    });

    if (ok) {
        recordPromptHistory(currentIdea.id, targetText, rollbackSummary);
        closePromptHistoryModal();
        if (typeof renderPromptDisplay === 'function') {
            renderPromptDisplay(targetText);
        }
    }
}

// 模块暴露
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        computeLineDiff,
        getPromptHistory,
        recordPromptHistory,
        clearPromptHistory,
        formatHistoryTime
    };
}
