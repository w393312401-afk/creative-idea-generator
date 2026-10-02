"""Execute the observer in an offline DOM/MutationObserver harness (no browser)."""
import json
import shutil
import subprocess

import pytest

from integrations.google_fx.services.flow_tile_observer import FLOW_FAILURE_OBSERVER_JS


DOM = r"""
const assert = require('node:assert/strict');
class El {
    constructor(tag, attrs={}, text='', children=[]) {
        this.nodeType=1; this.tagName=tag.toUpperCase(); this.attrs=attrs;
        this.text=text; this.children=[]; this.parentElement=null;
        this.style={display:'block', visibility:'visible', opacity:'1'};
        children.forEach(c => this.append(c));
    }
    append(c) { c.parentElement=this; this.children.push(c); return c; }
    remove() {
        if (this.parentElement) this.parentElement.children=this.parentElement.children.filter(c => c!==this);
        this.parentElement=null;
    }
    getAttribute(k) { return this.attrs[k] ?? null; }
    get textContent() { return this.text+this.children.map(c=>c.textContent).join('\n'); }
    get innerText() { return this.detachedText ? '' : this.textContent; }
    matches(selector) {
        return selector.split(',').some(s => {
            const m=s.trim().match(/^([\w-]+)?(?:\[([\w-]+)\])?$/);
            if (!m) throw Error('Unsupported test selector: '+s);
            return (!m[1] || m[1].toUpperCase()===this.tagName) && (!m[2] || m[2] in this.attrs);
        });
    }
    closest(s) { for(let e=this;e;e=e.parentElement) if(e.matches(s)) return e; return null; }
    querySelectorAll(s) {
        const out=[];
        for(const c of this.children) { if(c.matches(s)) out.push(c); out.push(...c.querySelectorAll(s)); }
        return out;
    }
}
let observers=[];
class MutationObserver {
    constructor(cb) {this.cb=cb;this.records=[];this.active=true;observers.push(this);}
    observe(target, opts) {this.target=target;this.opts=opts;}
    disconnect() {this.active=false;}
    takeRecords() {const r=this.records;this.records=[];return r;}
}
const window={location:{href:'https://flow.google.com/project/current'}, getComputedStyle:e=>e.style};
const document={documentElement:new El('html')};
const body=document.documentElement.append(new El('body'));
const SPARK_ID_ATTRS=['data-original-tile-id','data-tile-id','data-spark-tile-id'];
const SPARK_TILE_SEL='flow-grid-tile-container, flow-tile-container, div[data-tile-id]';
const sparkFindTile=id => {
    for(const e of document.documentElement.querySelectorAll(SPARK_ID_ATTRS.map(a=>'['+a+']').join(',')))
        if(SPARK_ID_ATTRS.some(a=>e.getAttribute(a)===id)) return e.closest(SPARK_TILE_SEL)||e;
    return null;
};
const unusual='Failed\nWe noticed some unusual activity. Please visit the Help Center for more information.\nYou have not been charged for this generation.';
const tile=(id, text='18%', error=false) => {
    const attrs=id?{'data-spark-tile-id':id}:{};
    const content=new El(error?'flow-error-tile':'flow-pending-tile',{},text);
    const t=new El('flow-grid-tile-container',attrs,'',[new El('flow-video-tile',{},'',[content])]);
    body.append(t);return t;
};
const makeFailed=(t,text=unusual)=>{
    t.children=[];t.append(new El('flow-error-tile',{},text));return t;
};
const emit=records=>{for(const o of observers) if(o.active)o.cb(records);};
const changed=t=>emit([{type:'childList',target:t,addedNodes:t.children,removedNodes:[]}]);
"""


def run_js(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the offline observer harness')
    # Re-evaluate the prelude just like separate Playwright page.evaluate calls;
    # only window state, not local JS declarations, survives between polls.
    source = DOM + '\nconst prelude=' + json.dumps(FLOW_FAILURE_OBSERVER_JS) + r""";
const api=()=>new Function('window','document','MutationObserver','SPARK_ID_ATTRS',
    'SPARK_TILE_SEL','sparkFindTile',prelude+
    ';return {observe:sparkObserveFailures,read:sparkRememberedFailure};')(
    window,document,MutationObserver,SPARK_ID_ATTRS,SPARK_TILE_SEL,sparkFindTile);
""" + scenario
    result = subprocess.run([node], input=source, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_exact_failure_is_synchronously_sampled_with_full_reason():
    run_js("""
        tile('current',unusual,true);
        const a=api(); a.observe(['current']);
        const s=a.read('current');
        assert.equal(s.failedText,unusual);
        assert.equal(s.status,'failed');assert.equal(s.isIpBlocked,true);
        assert.equal(s.isCreditExhausted,false);assert.equal(s.videoSrc,null);
        assert.equal(s.resolvedBy,'observed-failure');
        assert.equal(observers[0].target,document.documentElement);
        assert.equal(observers[0].opts.subtree,true);
    """)


def test_failure_during_wait_is_remembered_after_stamp_and_node_disappear():
    run_js("""
        const t=tile('submitted');api().observe(['submitted']);
        assert.equal(api().read('submitted'),null);
        makeFailed(t);changed(t);
        delete t.attrs['data-spark-tile-id'];t.remove();tile(null,unusual,true);
        api().observe(['submitted']);
        assert.equal(api().read('submitted').failedText,unusual);
        assert.equal(observers.length,1);
    """)


@pytest.mark.parametrize('wrapped', [False, True])
def test_removed_node_and_subtree_are_sampled_before_their_text_is_lost(wrapped):
    run_js("""
        const t=tile('submitted');api().observe(['submitted']);makeFailed(t);
        const root=WRAPPED ? new El('section',{},'',[t]) : t;
        t.remove();if(WRAPPED)root.append(t);
        root.detachedText=true;t.children[0].detachedText=true;
        emit([{type:'childList',target:body,addedNodes:[],removedNodes:[root]}]);
        assert.equal(api().read('submitted').failedText,unusual);
    """.replace('WRAPPED', json.dumps(wrapped)))


def test_unstamped_replacement_and_old_or_unregistered_errors_never_match():
    run_js("""
        const t=tile('new');api().observe(['new']);t.remove();
        const old=tile('old',unusual,true);const replacement=tile(null,unusual,true);
        emit([{type:'childList',target:body,addedNodes:[old,replacement],removedNodes:[t]}]);
        assert.equal(api().read('new'),null);assert.equal(api().read('old'),null);
        api().observe(['retry-new-id']);assert.equal(api().read('retry-new-id'),null);
    """)


def test_stamp_removed_before_failure_can_be_observed_does_not_infer_identity():
    run_js("""
        const t=tile('new');api().observe(['new']);
        delete t.attrs['data-spark-tile-id'];makeFailed(t);changed(t);
        assert.equal(api().read('new'),null);
    """)


def test_hidden_template_and_plain_failure_words_do_not_count():
    run_js("""
        const text=tile('text','a failed attempt at filming a flower');
        const hidden=tile('hidden');const e=hidden.append(new El('flow-error-tile',{},unusual));
        e.style.display='none';
        api().observe(['text','hidden']);
        assert.equal(api().read('text'),null);assert.equal(api().read('hidden'),null);
    """)


def test_warning_icon_is_visual_even_when_hidden_from_accessibility_tree():
    run_js("""
        const t=tile('legacy','Failed: insufficient credits');
        t.append(new El('mat-icon',{'aria-hidden':'true'},'warning'));
        api().observe(['legacy']);
        const s=api().read('legacy');assert.equal(s.isCreditExhausted,true);
        assert.equal(s.isIpBlocked,false);
        t.children[1].style.display='none';api().observe(['another']);
        const help=tile('help','Please visit the Help Center');
        help.append(new El('mat-icon',{},'warning'));api().observe(['help']);
        assert.equal(api().read('help'),null);
    """)


def test_url_change_clears_old_cache_and_cannot_be_repopulated_by_old_callback():
    run_js("""
        const t=tile('old',unusual,true);api().observe(['old']);
        const previous=observers[0];t.remove();
        window.location.href='https://flow.google.com/project/next';
        assert.equal(api().read('old'),null);api().observe(['new']);
        previous.cb([{type:'childList',target:body,removedNodes:[t],addedNodes:[]}]);
        assert.equal(previous.active,false);assert.equal(api().read('old'),null);
        window.location.href='https://flow.google.com/project/current';api().observe(['old']);
        assert.equal(api().read('old'),null);
    """)


def test_bounded_memory_and_new_registration_cannot_authorize_old_queued_removal():
    run_js("""
        const old=tile('first',unusual,true);api().observe(['first']);old.remove();
        const late=tile('late',unusual,true);late.remove();
        observers[0].records.push({type:'childList',target:body,removedNodes:[late],addedNodes:[]});
        api().observe(['late']);assert.equal(api().read('late'),null);
        api().observe(Array.from({length:513},(_,i)=>'id-'+i));
        assert.equal(api().read('first'),null);
        const state=window.__sparkObservedFlowFailuresV1;
        assert.equal(state.allowed.size,512);assert.equal(state.failures.size,0);
    """)


def test_observer_unavailable_safely_leaves_normal_scan_usable():
    run_js("""
        const original=MutationObserver.prototype.observe;
        MutationObserver.prototype.observe=()=>{throw Error('Unsupported');};
        api().observe(['current']);assert.equal(api().read('current'),null);
        MutationObserver.prototype.observe=original;
    """)
