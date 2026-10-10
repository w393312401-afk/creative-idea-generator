"""Remember failures observed on already identified Flow cards.

Append this prelude after ``_FLOW_TILE_JS`` inside a page evaluation. Call
``sparkObserveFailures(tileIds)`` before scanning, then consult
``sparkRememberedFailure(id)`` only when the exact stamped node is missing.
The observer neither assigns identities nor clicks anything. A replacement
error card without the registered stamp deliberately remains unidentified.
"""

FLOW_FAILURE_OBSERVER_JS = r"""
    const sparkFailureStateKey = '__sparkObservedFlowFailuresV1';
    const sparkFailureURL = () => String(window.location.href);
    const sparkFailureVisible = el => {
        for (let cur = el; cur; cur = cur.parentElement) {
            const style = window.getComputedStyle(cur);
            if (style.display === 'none' || style.visibility === 'hidden'
                    || style.opacity === '0') return false;
        }
        return true;
    };
    const sparkFailureFromNode = tile => {
        if (!tile || !sparkFailureVisible(tile)) return null;
        const errors = Array.from(tile.querySelectorAll('flow-error-tile'));
        if (tile.matches('flow-error-tile')) errors.unshift(tile);
        const error = errors.find(sparkFailureVisible);
        const failedText = ((error || tile).innerText || (error || tile).textContent || '').trim();
        // Keep the complete reason; an unexpectedly huge node is uncertainty,
        // not permission to retain an unbounded document in the cache.
        if (!failedText || failedText.length > 65536) return null;
        const text = failedText.toLowerCase();
        const credit = /\b(?:out of|insufficient|not enough|run out of)\s+(?:\w+\s+){0,3}credits?\b/i.test(text)
            || /\b(?:credits? (?:exhausted|depleted)|no credits? left|resource_exhausted|quota_exhausted|quota exceeded)\b/i.test(text)
            || /\bget\s+(?:more\s+)?(?:ai\s+|flow\s+|google\s+flow\s+)?credits?\b/i.test(text)
            || /(?<!\d)0\s*(?:(?:google\s+)?flow\s+|ai\s+)?credits?\b/i.test(text)
            || /(?:credits?|credit\s+balance|flow\s+credits?|ai\s+credits?|积分|点数|额度|配额|余额)[:：=为是]\s*0(?!\d)/i.test(text)
            || /(积分不足|没有足够的积分|积分已用完|积分已耗尽|积分耗尽|积分用尽|点数不足|点数已用完|点数已耗尽|点数耗尽|额度不足|额度已用完|额度耗尽|配额不足|配额已用完|配额耗尽|无可用积分|无可用点数|没有可用积分|0\s*积分|0\s*点数)/.test(text);
        const failed = /failed|something went wrong|unusual activity|异常活动|出错了|生成失败|失败|使用人数过多/i.test(text) || credit;
        const warning = Array.from(tile.querySelectorAll('i, mat-icon')).some(icon =>
            /^(warning|error|error_outline)$/.test((icon.innerText || icon.textContent || '').trim().toLowerCase())
            && sparkFailureVisible(icon));
        if (!error && !(warning && failed)) return null;
        return {status:'failed', failedText, videoSrc:null, progress:null,
            isIpBlocked:/unusual activity|异常活动/i.test(text), isCreditExhausted:credit,
            resolvedBy:'observed-failure', needsMediaWake:false, isPending:false,
            isVisiblyFinished:false};
    };
    const sparkObserveFailures = tileIds => {
        try {
            let state = window[sparkFailureStateKey];
            const url = sparkFailureURL();
            if (state && state.url !== url) {
                state.observer.disconnect();
                state.allowed.clear(); state.failures.clear();
                state = null;
                delete window[sparkFailureStateKey];
            }
            if (!state) {
                state = {url, allowed:new Map(), failures:new Map(), observer:null};
                const remember = root => {
                    if (!root || root.nodeType !== 1) return;
                    const selector = SPARK_ID_ATTRS.map(a => '[' + a + ']').join(',');
                    const stamped = Array.from(root.querySelectorAll(selector));
                    if (root.matches(selector)) stamped.unshift(root);
                    for (const node of stamped) {
                        const ids = SPARK_ID_ATTRS.map(a => node.getAttribute(a))
                            .filter(id => id && state.allowed.has(id));
                        if (!ids.length) continue;
                        const tile = node.closest(SPARK_TILE_SEL);
                        if (!tile) continue;
                        const failure = sparkFailureFromNode(tile);
                        if (failure) for (const id of ids) state.failures.set(id, failure);
                    }
                };
                state.consume = mutations => {
                    if (state.url !== sparkFailureURL()) {
                        state.observer.disconnect();
                        state.allowed.clear(); state.failures.clear();
                        return;
                    }
                    // Removed subtrees still expose their original stamps and
                    // textContent even when detached innerText is empty.
                    const roots = new Set();
                    for (const mutation of mutations) {
                        const target = mutation.target.nodeType === 1
                            ? mutation.target : mutation.target.parentElement;
                        if (target) {
                            const tile = target.closest(SPARK_TILE_SEL);
                            if (tile) roots.add(tile);
                        }
                        for (const node of mutation.removedNodes || []) roots.add(node);
                        for (const node of mutation.addedNodes || []) roots.add(node);
                    }
                    for (const root of roots) remember(root);
                };
                state.observer = new MutationObserver(mutations => {
                    // Observation is supplementary: an unsupported DOM shape
                    // must not break the ordinary live status scan.
                    try { state.consume(mutations); } catch (_) {}
                });
                state.observer.observe(document.documentElement, {
                    subtree:true, childList:true, characterData:true, attributes:true,
                    attributeFilter:SPARK_ID_ATTRS.concat(['class', 'style', 'aria-hidden'])
                });
                window[sparkFailureStateKey] = state;
            }
            // Do not retroactively authorize queued events from before an ID
            // was registered. An existing exact node is sampled below instead.
            state.consume(state.observer.takeRecords());
            for (const value of tileIds || []) {
                if (typeof value !== 'string' || !value || value.length > 512) continue;
                state.allowed.delete(value);
                state.allowed.set(value, true);
                while (state.allowed.size > 512) {
                    const oldest = state.allowed.keys().next().value;
                    state.allowed.delete(oldest); state.failures.delete(oldest);
                }
                const node = sparkFindTile(value);
                const failure = node && sparkFailureFromNode(node);
                if (failure) state.failures.set(value, failure);
            }
        } catch (_) {}
    };
    const sparkRememberedFailure = id => {
        try {
            const state = window[sparkFailureStateKey];
            if (!state || state.url !== sparkFailureURL() || !state.allowed.has(id)) return null;
            const failure = state.failures.get(id);
            return failure ? {...failure} : null;
        } catch (_) { return null; }
    };
"""
