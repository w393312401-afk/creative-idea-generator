const test = require('node:test');
const assert = require('node:assert/strict');
const { buildCreditBadgeElement } = require('../js/account_pool.js');

function installDocument(t) {
  const previous = global.document;
  global.document = {
    createElement() {
      return { classList: { add() {} }, textContent: '', title: '' };
    }
  };
  t.after(() => {
    if (previous === undefined) delete global.document;
    else global.document = previous;
  });
}

const cooldown = { cooldown_until: '2099-01-01T00:00:00Z' };

test('pool badges show insufficient credits throughout a quota cooldown', (t) => {
  installDocument(t);
  for (const account of [
    { ...cooldown, credit: 1 },
    { ...cooldown, credit: 0, disabled: true, disabled_reason: 'zero_credit' },
    { ...cooldown, credit: 100, cooldown_reason: 'quota_exhausted' },
    { ...cooldown, credit: null, cooldown_reason: 'quota_exhausted' },
    { credit: 19, min_credit: 20 }
  ]) {
    const badge = buildCreditBadgeElement(account);
    assert.match(badge.textContent, /积分不足/);
    assert.doesNotMatch(badge.textContent, /额度冷却中/);
  }
  const unknown = buildCreditBadgeElement({ ...cooldown, credit: null, cooldown_reason: 'quota_exhausted' });
  assert.doesNotMatch(unknown.textContent, /0 积分/, 'unknown balance must not be fabricated as zero');
});

test('pool badges distinguish image daily quota and login cooldown from low credits', (t) => {
  installDocument(t);
  for (const cooldown_reason of ['image_quota_exceeded', 'daily_limit_reached', 'daily_limit', 'image_limit_exceeded']) {
    const badge = buildCreditBadgeElement({ ...cooldown, credit: 0, cooldown_reason });
    assert.equal(badge.textContent, '🚫 图片余额超限');
  }
  assert.equal(buildCreditBadgeElement({ ...cooldown, credit: 0, cooldown_reason: 'login_required' }).textContent, '🔒 登录失效冷却中');
  assert.equal(buildCreditBadgeElement({ credit: null }).textContent, '⚪ 积分未探测');
  assert.doesNotMatch(buildCreditBadgeElement({ credit: 20, min_credit: 20 }).textContent, /不足/);
});
