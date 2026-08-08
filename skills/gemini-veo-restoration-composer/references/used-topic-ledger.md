# Used Topic Ledger — 已用选题账本

> Dedup memory for the Topic Ideation Engine and `tiktok-abandoned-rebirth`. Every delivered/built topic is fingerprinted as `carrier-family / destiny / twist-family`. The Idea Engine rejects any candidate matching, or one edit-step away from, a row here (see [`idea-engine.md`](idea-engine.md) §2.3 and §4). Append a row whenever a topic is selected for full prompt generation.

## Dedup Rules (P0 — apply BEFORE scoring, on the DNA, not on the prose)

Dedup runs on the fingerprint. Nothing programmatic enforces it — `contract-registry.json`
registers `used-topic-ledger-dedup` with `enforcer: null` precisely because it is LLM-side
only. That makes the following rules load-bearing rather than advisory.

### 1. Twist-family ROOT match, not exact-string match

Compare the **first two hyphen-segments** of the twist field, not the whole string. Suffixing
a variant onto a used twist does not make a new twist: `glass-floor`, `glass-floor-gears`,
`glass-floor-cliff`, `glass-floor-tides`, `glass-floor-veins`, `glass-floor-water`,
`glass-floor-stream`, `glass-floor-tidal-channel` all share root `glass-floor` and are **one**
idea wearing eight costumes. Measured across the full history (60 live rows + 107 archived):
`glass-floor` accounts for 13 topics and `material-cutaway` for 5 — the single most over-mined
twist in the bank. Same for the cutaway-window family (`material-cutaway-window` /
`fossil-window` / `self-material-window` / `ice-cutaway-window`): "a window cut through the
carrier's own substance" is one twist root regardless of substance.

Root-match against **both** surfaces: the live rows below *and* the Burned Families Summary.
An archived root is exactly as burned as a visible one.

**Rule**: a candidate whose twist root already appears in the ledger is rejected outright.
Rotating the *carrier* does not rescue it — the twist is what the viewer remembers.

### 2. Carrier field must hold a FAMILY, not a specific carrier

The first field is one of exactly: `natural`, `man-made`, `vehicle`, `fantasy`,
`living-tree`, `treehouse`. About a fifth of the historical rows violate this by writing a
specific carrier instead (`abandoned-phone-booth`, `retired-submarine`,
`salvaged-tugboat-wheelhouse`, …). Those rows are effectively invisible to the "rotate the
carrier family so no two ideas in a batch share a shell family" rule, because every specific
string is unique and therefore never collides with anything. When appending, write the family;
put the specific carrier in the 一句话选题 column where it belongs. When reading an old
malformed row, map it to its family yourself before comparing. (The archived rows were mapped
to their families during compaction, so the summary buckets are already correct.)

### 3. `custom-twist` / `unspecified-twist` are not fingerprints

`custom-twist` and `unspecified-twist` are placeholders that match nothing and block nothing;
22 archived rows and a number of live ones carry them, which is why they are absent from the
Burned Families Summary — registering a placeholder as a burned root would burn nothing. Never
append one. If the signature twist cannot be named in two or three hyphenated words, the idea
does not have a single clear twist yet and has already failed the mandatory single-twist
filter one step earlier.

### 4. Archive rhythm (keep this file from eating the prompt)

This file is loaded whole into the ideation system prompt, and it only ever grew. Compact it
whenever the DNA table passes **80 rows**:

- Keep the most recent **60** rows verbatim.
- Collapse everything older into the **Burned Families Summary** below — one line per
  `carrier-family × twist-root` pair with a count and the date of last use. Individual old
  rows are not needed for dedup once their root is registered as burned; the root is what
  rule 1 compares against.
- Never delete a root from the summary. Roots are burned permanently; only per-row detail ages out.
- Move the aged-out rows verbatim into [`used-topic-ledger.archive.md`](used-topic-ledger.archive.md)
  rather than deleting them. That file is **not** loaded into any prompt, so it costs nothing at
  run time while keeping the history auditable.

**Last compaction: 2026-08-08** — 107 rows archived, 60 kept live. In the same pass, 31 live
rows had their carrier field normalized from a specific carrier to its family (the specific
carrier was already spelled out in their 一句话选题 column, so nothing was lost) and 3 had a
Chinese free-text twist normalized to its English root.

### 5. Fingerprints are ASCII kebab-case English, and contain no `/`

Both failure modes below are present in the archived history and both defeat dedup completely:

- **Chinese twist fields.** `水面玻璃地板` is the `glass-floor` twist and `载体本体材质切面窗`
  is the `material-cutaway` twist, but no string comparison will ever connect them to their
  English roots — they read as brand-new ideas forever. Write the fingerprint in English even
  when the 一句话选题 column is Chinese; that column is where the Chinese belongs.
- **A `/` inside a field.** Three rows fingerprinted `活体木纹/岩壁旋梯` split into four
  fields instead of three, so every automated pass over the ledger skipped them silently —
  they were neither deduped against nor counted. `/` is the field separator; use `-` inside a
  field.

### Known-open warnings — do NOT "fix" these by inventing data

`scripts/lint_skill.py` reports two warnings against this file on purpose. Both are true, and
the correct response to each is a future decision, not an edit today:

- **`relief-valve` appears 3 times.** That is real over-mining, now visible: two of the three
  were hidden behind a Chinese free-text fingerprint until the 2026-08-08 normalization. Treat
  the root as burned and stop proposing locomotive-valve-to-fireplace variants.
- **15 rows fingerprint the twist as `custom-twist` / `unspecified-twist`.** Their signature
  twist was never recorded. Do not back-fill a guess — a fabricated fingerprint would block
  ideas that were never actually used and let through the one that was. Rule 3 exists to stop
  more of these being created; the existing ones simply age out at the next compaction.

## Burned Families Summary (compacted history — roots here are permanently blocked)

Covers the 107 DNA rows archived out of this file (full detail preserved in [`used-topic-ledger.archive.md`](used-topic-ledger.archive.md), which is **not** loaded into the ideation prompt). Dedup Rule 1 compares a candidate's twist root against this table exactly as it would against a live row — an archived root is just as burned as a visible one.

| Twist root | Carrier families | Rows | Last used | Variants seen |
|---|---|---|---|---|
| `glass-floor` | fantasy, man-made, natural, vehicle | 11 | 2026-07-16 | `glass-floor`, `glass-floor-cliff`, `glass-floor-gears`, `glass-floor-mineshaft` +5 more, plus the Chinese-language duplicate `水面玻璃地板` |
| `material-cutaway` | man-made, natural, vehicle | 5 | 2026-07-13 | `material-cutaway-window`, plus the Chinese-language duplicate `载体本体材质切面窗` |
| `carrier-slab` | fantasy, vehicle | 3 | 2026-07-15 | `carrier-slab-headboard`, plus the Chinese-language duplicate `整块载体板材台面` |
| `living-wood` | man-made, natural | 5 | 2026-07-12 | `living-wood-staircase`, plus 3 rows fingerprinted `活体木纹/岩壁旋梯` — Chinese, and containing a `/` that broke the three-field DNA parse entirely |
| `porthole-lighting` | man-made, vehicle | 2 | 2026-07-05 | `porthole-lighting`, `porthole-lighting-array` |
| `roof-hatch` | man-made | 2 | 2026-07-05 | `roof-hatch-sky`, `roof-hatch-telescope` |
| `sliding-bed` | man-made, vehicle | 2 | 2026-07-16 | `sliding-bed-rail`, `sliding-bed-wall` |
| `acrylic-window` | man-made | 1 | 2026-07-05 | `acrylic-window-spillway` |
| `bark-camouflaged` | natural | 1 | 2026-07-05 | `bark-camouflaged-skylight` |
| `bark-edge` | man-made | 1 | 2026-07-05 | `bark-edge-window` |
| `bark-hatch` | natural | 1 | 2026-07-01 | `bark-hatch-dome` |
| `bark-star` | treehouse | 1 | 2026-06-22 | `bark-star-cabin` |
| `blade-hub` | man-made | 1 | 2026-07-05 | `blade-hub-window-rail` |
| `boulder-counterweight` | natural | 1 | 2026-07-20 | `boulder-counterweight-pulley-bed` |
| `brass-aperture` | vehicle | 1 | 2026-07-05 | `brass-aperture-skylight` |
| `central-hanging` | man-made | 1 | 2026-07-20 | `central-hanging-stove-spiral-stair` |
| `chimney-flue` | man-made | 1 | 2026-07-16 | `chimney-flue` |
| `cockpit-fireplace` | vehicle | 1 | 2026-07-10 | `cockpit-fireplace-hatch` |
| `console-fireplace` | vehicle | 1 | 2026-07-02 | `console-fireplace-pantograph` |
| `deadwood-stair` | natural | 1 | 2026-06-27 | `deadwood-stair` |
| `engine-bay` | vehicle | 1 | 2026-07-14 | `engine-bay-fireplace` |
| `fire-tool` | man-made | 1 | 2026-07-10 | `fire-tool-rack-fold-down-bed` |
| `fluorite-diffuser` | fantasy | 1 | 2026-07-21 | `fluorite-diffuser-wall` |
| `fossil-window` | fantasy | 1 | 2026-06-24 | `fossil-window` |
| `gill-fiberoptic` | fantasy | 1 | 2026-07-07 | `gill-fiberoptic-light` |
| `glass-rain` | man-made | 1 | 2026-07-05 | `glass-rain-column` |
| `guillotine-arch` | man-made | 1 | 2026-07-06 | `guillotine-arch-window` |
| `gun-port` | man-made | 1 | 2026-07-04 | `gun-port-fireplace` |
| `hammock-balcony` | natural | 1 | 2026-07-02 | `hammock-balcony-door` |
| `hexagonal-basalt` | natural | 1 | 2026-07-20 | `hexagonal-basalt-headboard-alcove` |
| `hydraulic-glass` | vehicle | 1 | 2026-07-11 | `hydraulic-glass-ramp-deck` |
| `hydraulic-platform` | man-made | 1 | 2026-07-04 | `hydraulic-platform-skylight` |
| `hydraulic-sliding` | natural | 1 | 2026-07-04 | `hydraulic-sliding-bed` |
| `ice-cutaway` | natural | 1 | 2026-07-11 | `ice-cutaway-window` |
| `ice-dome` | natural | 1 | 2026-07-05 | `ice-dome-flue` |
| `jet-exhaust` | vehicle | 1 | 2026-06-28 | `jet-exhaust-fireplace` |
| `lava-vent` | natural | 1 | 2026-06-27 | `lava-vent-fireplace` |
| `lens-window` | vehicle | 1 | 2026-06-25 | `lens-window-pool` |
| `living-trunk` | man-made | 1 | 2026-07-13 | `living-trunk-hearth` |
| `pallasite-skylight` | fantasy | 1 | 2026-07-10 | `pallasite-skylight` |
| `pearl-glow` | fantasy | 1 | 2026-07-05 | `pearl-glow-wall` |
| `periscope-360` | vehicle | 1 | 2026-07-02 | `periscope-360-view` |
| `petrified-root` | fantasy | 1 | 2026-07-20 | `petrified-root-heated-bed-frame` |
| `pulley-bed` | man-made | 1 | 2026-07-01 | `pulley-bed-flue` |
| `quartz-skylight` | natural | 1 | 2026-06-29 | `quartz-skylight` |
| `reading-nook` | vehicle | 1 | 2026-06-27 | `reading-nook` |
| `red-brick` | man-made | 1 | 2026-07-04 | `red-brick-well` |
| `relief-valve` | man-made | 2 | 2026-07-20 | `relief-valve-fireplace`, plus a Chinese free-text duplicate describing the same locomotive-valve-to-fireplace twist |
| `river-glass` | man-made | 1 | 2026-07-04 | `river-glass-floor` |
| `rock-vent` | natural | 1 | 2026-07-11 | `rock-vent-fireplace` |
| `rotating-bed` | vehicle | 1 | 2026-07-05 | `rotating-bed-view` |
| `rotating-clock` | man-made | 1 | 2026-07-02 | `rotating-clock-window` |
| `sea-vent` | natural | 1 | 2026-06-28 | `sea-vent-fireplace` |
| `self-material` | living-tree | 1 | 2026-06-22 | `self-material-window` |
| `sliding-door` | man-made | 1 | 2026-07-01 | `sliding-door-window` |
| `thermal-fissure` | fantasy | 1 | 2026-07-20 | `thermal-fissure-heated-coral-bed` |
| `upcycled-valve` | man-made | 1 | 2026-07-20 | `upcycled-valve-suspended-stove` |
| `water-surface` | natural | 1 | 2026-07-11 | `water-surface-glass-floor` |
| `waterfall-shower` | natural | 1 | 2026-06-27 | `waterfall-shower` |
| `winch-hammock` | natural | 1 | 2026-07-06 | `winch-hammock-niche` |
| `wood-table` | man-made | 1 | 2026-07-01 | `wood-table-plants` |

> 22 archived rows carried `custom-twist` / `unspecified-twist` and are deliberately absent from this table: a placeholder fingerprint matches nothing and blocks nothing, so registering it as a burned root would block nothing either. Their one-line topic descriptions survive in the archive file if a human needs them. Dedup Rule 3 exists to stop this class of row being created again.

## Topic DNA Rows

| Date | Topic DNA (carrier / destiny / twist) | 一句话选题 | Source | Avoid Notes |
|---|---|---|---|---|
| 2026-07-21 | man-made / solitary-reading-and-sleeping-nook / waterwheel-gear-rotating-bed | 做一个废弃石砌水磨坊改造成溪畔隐居读书睡眠阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-21 | man-made / weatherproof-base-camp-shelter / lever-winch-suspended-bed | 做一个废弃铁路信号楼改造成林间观景基地避难所 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-21 | man-made / sunroom-shelter / upcycled-pool-ladder-entry-hatch | 做一个荒废高山混凝土泳池改造成地下玻璃天窗独居暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-21 | man-made / off-grid-micro-home / carrier-slab-headboard | 做一个薰衣草田里塌顶的废弃啤酒花烘房改造成离网圆塔小家，并把原烘干层的整块圆形橡木板改成床头板 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | man-made / off-grid-stargazing-cabin / custom-twist | 将一座废弃的森林防火瞭望塔改造成离网观星胶囊小屋 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | natural / snug-rainforest-refuge-den / brass-pulley-counterweight-stone-door | 做一个塌陷喀斯特天坑崖壁石穴改造成雨林避世独居暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | natural / secluded-boutique-cave-resort / custom-twist | 把一座废弃的喀斯特石灰岩洞穴改造成隐世精品度假屋 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | natural / secluded-luxury-cave-resort / custom-twist | 把一座废弃的喀斯特石灰岩洞穴改造成隐世精品度假屋 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | man-made / cliffside-sleeping-loft / portcullis-drawbridge-balcony | 做一个峡谷悬崖废弃石砌古堡门楼改造成离网避世睡眠阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | natural / desert-refuge-den / fissure-flue-pulley-daybed | 做一个沙漠花岗岩悬崖壁龛改造成独居御寒庇护所 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | man-made / off-grid-micro-home / millstone-glass-floor | 做一个废弃中世纪石磨坊改造成溪畔离网独居小屋，将原磨盘整块嵌入地板作为玻璃封面圆形水景 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | natural / snug-winter-refuge-den / carrier-slab-headboard | 做一个美国南方柏树沼泽里一截巨型中空腐烂树兜改造成猎人独居避风暖阁，用原木截面打造厚重床头板 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | natural / weatherproof-base-camp-shelter / whale-rib-leather-hammock-cradle | 做一个盐湖荒原巨型古鲸肋骨化石骨架改造成独居御寒睡眠营地 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | fantasy / cozy-bedroom-retreat / ring-grain-glass-skylight | 做一个荒野草甸巨型腐朽树桩空腔改造成老钱复古独居卧室 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | vehicle / off-grid-micro-home / corrugated-rust-frame-glass-skylight | 做一个热带沼泽树冠高脚锈蚀集装箱叠舱改造成离网微型住宅 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | man-made / hermit-woodland-hideout / blast-door-fold-flat-bed | 做一个废弃混凝土防空洞改造成苔藓林间地下独居避难所 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-22 | natural / undersea-solitary-sleeping-cabin / custom-twist | 做一个珊瑚礁废弃巨砗磲壳改造成海底独居睡眠舱，用沉船黄铜舷窗组成暖光照明墙 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-23 | man-made / snug-winter-refuge-den / custom-twist | 高山废弃铁路蒸汽机车注水塔改造成独居御寒暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-23 | man-made / snug-winter-refuge-den / relief-valve-suspended-stove | 高山废弃铁路蒸汽机车注水塔改造成独居御寒暖阁 | GUI Generation | Automatically registered by backend generator. Twist fingerprint normalized from the Chinese free-text 「利用蒸汽机车原装铸铁注水阀门旧物upcycling改装成悬空柴暖壁炉」 — a Chinese fingerprint can never root-match its English equivalent (here: the already-burned `relief-valve` root). |
| 2026-07-23 | natural / subterranean-burrow-dwelling / underground-stream-micro-waterwheel-lighting | 做一个天坑暗河天然石龛改造成地下隐居避世暖阁，利用暗河水流驱动微型水车为卧室供电照明 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-23 | man-made / snug-winter-refuge-den / mill-shaft-brass-radiant-stove | 做一个高山崖边废弃石风车塔改造成独居御寒暖阁，将原木轴心改造成立柱式黄铜柴暖壁炉 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-23 | vehicle / coastal-sleep-cabin / brass-ship-telegraph-stove-controller | 做一个海岸废弃木质拖船驾驶台改造成独居御寒睡眠小屋，将原装黄铜传令钟改造为柴暖控制器 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-23 | natural / offgrid-micro-home / natural-fissure-heated-sandstone-bed | 做一个红砂岩峡谷风蚀石龛改造成沙漠离网避世暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-23 | vehicle / winter-survival-den / heated-windshield-headboard | 做一个极地废弃履带压雪车舱改造成雪山御寒避难所 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-23 | man-made / off-grid-micro-home / rainwater-waterfall-shower | 做一个风暴海岬塌顶的废弃石砌小教堂改造成离网独居小屋，并将屋顶收集的雨水引入祭坛石墙形成瀑布淋浴 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-23 | natural / self-sufficient-hideaway-home / glass-floor-tidal-channel | 做一个热带珊瑚礁中的巨型古珊瑚头改造成海底单人离网居所，用承压玻璃地板展示下方穿过天然礁沟的潮流 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-24 | man-made / subterranean-dwelling / glass-floor-water | 做一个荒废地下砖砌水窖改造成地下避世暖阁，钢化玻璃地板下显露清澈的地下涌水渠道 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-24 | natural / one-room-sleeping-cabin / petrified-grain-spiral-stair | 做一个高地沙漠半埋硅化巨木改造成沿木纹旋梯而上的独居睡眠小屋 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-24 | vehicle / cliffside-sleeping-loft / salvaged-porthole-lighting-bank | 做一个风暴海岸藤壶斑驳的搁浅沉船船壳改造成悬空避世睡眠阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-26 | man-made / island-refuge-den / scissors-jack-elevating-bed | 做一个废弃海岛电报站爆改成悬崖防风暖光卧室 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-26 | man-made / snug-winter-refuge-den / top-charging-hole-skylight | 做一个高山废弃石砌石灰窑改造成林间御寒暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-26 | man-made / snug-winter-refuge-den / relief-valve-suspended-stove | 高山废弃铁路蒸汽机车注水塔改造成独居御寒暖阁 | GUI Generation | Automatically registered by backend generator. Twist fingerprint normalized from the Chinese free-text 「利用蒸汽机车原装铸铁注水阀门旧物upcycling改装成悬空柴暖壁炉」 — a Chinese fingerprint can never root-match its English equivalent (here: the already-burned `relief-valve` root). |
| 2026-07-26 | man-made / snug-winter-refuge-den / custom-twist | 高山废弃铁路蒸汽机车注水塔改造成独居御寒暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-26 | vehicle / coastal-tram-refuge / trolley-pole-lifting-skylight | 做一个退役海岸复古电车爆改成海滨观海暖光避世卧室 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-26 | man-made / round-tower-sanctuary / wind-vane-synced-cowl-skylight | 做一个废弃啤酒花烘干圆塔爆改成乡村暖光避世卧室 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-26 | vehicle / cliffside-sleeping-loft / destination-blind-gear-skylight | 做一个废弃双层巴士改造成高山云海单人避世睡眠阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-26 | man-made / winter-refuge / saw-blade-window-shutter | 做一个倒塌的林间废弃破木屋改造成御寒避世暖阁，将原有的生锈伐木圆锯片改造成机械旋转窗户挡板 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-26 | man-made / storm-proof-shelter / locking-bar-storm-shutter | 做一个极度锈蚀变形的废弃集装箱改造成海岸防风暴单人庇护所，将原装门锁杆改造成升降防暴风雨窗板的机械联动装置 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-27 | natural / one-room-sleeping-cabin / bamboo-node-diaphragm-skylight | 做一个竹海深处巨型石化竹节空腔改造成独居睡眠小屋 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-27 | natural / refuge-den / self-material-window | 做一个蓝冰冰川洞穴改造成隐居雪境卧室 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-28 | man-made / offgrid-sleeping-retreat / iron-counterweight-stone-shutter | 做一个高山峡谷废弃铁路巡警石塔改造成独居御寒观景暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-28 | man-made / winter-refuge-den / iron-wheel-pulley-sliding-steel-shutter | 做一个高山峡谷废弃铁路扳道工石屋改造成独居御寒避风暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-28 | natural / off-grid-micro-home / petrified-wood-slab-headboard | 做一个高地沙漠半埋硅化木中空树干改造成离网隐居木窟 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-29 | vehicle / micro-home / porthole-lighting | 做一个退役潜艇舱改造成离网单人居所 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-29 | vehicle / off-grid-micro-home / emergency-brake-sliding-bed | 做一个竹海废弃退役地铁车厢爆改成离网隐居双空间微型住宅 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-29 | vehicle / mountaineer-base-bunkroom / cockpit-yoke-folding-table | 做一个悬崖废弃退役货机机身改造成高山双空间避风暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-29 | man-made / buried-two-room-shelter / custom-twist | 废弃集装箱 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-29 | vehicle / wetland-solitary-sleeping-refuge / salvaged-deck-hatch-glass-floor | 做一个搁浅内河钢制驳船分段舱改造成湿地避世独居睡眠小屋 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-30 | vehicle / mountain-base-bunkroom / tank-manhole-copper-skylight | 做一个废弃双舱椭圆罐体改造成高山御寒独居避难所 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-30 | vehicle / winter-refuge-den / brake-wheel-counterweight-table | 做一个废弃木质铁路车厢改造成林间双空间御寒暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-31 | vehicle / insulated-woodland-twin-cabin / custom-twist | 做一个废弃木质铁路车厢改造成林间双空间御寒暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-31 | vehicle / coastal-storm-shelter-cabin / custom-twist | 做一个搁浅生锈渔船船舱改造成海岸避风双空间暖舱 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-31 | man-made / dual-space-winter-stone-cabin / custom-twist | 做一个废弃山间石砌羊圈改造成雪线下双空间越冬石屋 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-31 | vehicle / insulated-dual-zone-winter-retreat / custom-twist | 做一个废弃木质铁路车厢改造成林间双空间御寒暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-31 | man-made / snug-winter-refuge-den / relief-valve-suspended-stove | 高山废弃铁路蒸汽机车注水塔改造成独居御寒暖阁 | GUI Generation | Automatically registered by backend generator. Twist fingerprint normalized from the Chinese free-text 「利用蒸汽机车原装铸铁注水阀门旧物upcycling改装成悬空柴暖壁炉」 — a Chinese fingerprint can never root-match its English equivalent (here: the already-burned `relief-valve` root). |
| 2026-07-31 | man-made / snug-winter-refuge-den / custom-twist | 高山废弃铁路蒸汽机车注水塔改造成独居御寒暖阁 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-31 | man-made / woodland-dual-level-lookout-den / custom-twist | 做一个废弃圆形砖砌水塔改造成林地双空间瞭望暖居 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-31 | vehicle / suspended-alpine-refuge / custom-twist | 做一个退役高山缆车厢改造成雪坡双空间悬空暖舱 | GUI Generation | Automatically registered by backend generator. |
| 2026-07-31 | man-made / cliffside-stargazing-lodge / custom-twist | 做一个海岬废弃石砌灯塔改造成双空间观星宿所 | GUI Generation | Automatically registered by backend generator. |
| 2026-08-01 | vehicle / snug-coastal-refuge / brass-wheel-folding-bunk-rail | 做一个废弃搜救拖船船体改造成双空间沿海防风暖阁 | GUI Generation | Automatically registered by backend generator. |

## Avoid List (cliché — never propose without a transforming twist)

- abandoned warehouse → generic industrial loft
- old van / bus → standard camper conversion
- shipping container → minimalist tiny home
- lighthouse → seaside bedroom (too on-the-nose)
