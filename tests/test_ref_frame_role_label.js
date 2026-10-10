/**
 * 对标帧角色标签（refFrameRoleLabel / 候选弹窗的角色措辞）。
 *
 * 三种角色各有各的读法，标错一个用户就会照着一张不该照的图挑毛病：
 *   envelope     过门梯那几格是硬切两侧的包络端点，原片没拍过门槛帧 → 不对机位；
 *   establishing 同空间最近的一张全景，本拍原片全程特写时的退档 → 不对施工进度；
 *   benchmark    原片这一拍的真实交付帧 → 逐像素对标。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const utilsJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'utils.js'), 'utf8');
const appJs = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');

console.log('Testing ref frame role labels...');

// utils.js 是浏览器全局脚本（没有 module.exports），取出函数体单独求值。
const fnSrc = utilsJs.slice(utilsJs.indexOf('function refFrameRoleLabel'));
const refFrameRoleLabel = new Function(
    fnSrc.slice(0, fnSrc.indexOf('\n}') + 2) + '\nreturn refFrameRoleLabel;')();

assert.ok(refFrameRoleLabel({ 3: 'envelope' }, 3).includes('包络端点'));
assert.ok(refFrameRoleLabel({ 3: 'establishing' }, 3).includes('全景'));
assert.ok(refFrameRoleLabel({ 3: 'establishing' }, 3).includes('非本拍时刻'),
    'establishing 必须说清它不是本拍的状态，否则会被当成基准照着对进度');
assert.ok(refFrameRoleLabel({ 3: 'benchmark' }, 3).includes('对标基准'));
// 字符串键：ref_frame_roles 从 JSON 过来时键是字符串。
assert.ok(refFrameRoleLabel({ '3': 'establishing' }, 3).includes('全景'));
// 缺席 = 基准（老单没有 roles 这个字段）。
assert.ok(refFrameRoleLabel({}, 3).includes('对标基准'));
assert.ok(refFrameRoleLabel(null, 3).includes('对标基准'));

// 候选弹窗那块也要认这一档，否则它会被标成「黄金对标基准」。
assert.ok(appJs.includes("refIsEstablishing"), 'app.js 必须区分 establishing 角色');
assert.ok(appJs.includes('同空间全景参考'), 'app.js 必须给 establishing 换措辞');

console.log('✓ All ref frame role label tests passed');
