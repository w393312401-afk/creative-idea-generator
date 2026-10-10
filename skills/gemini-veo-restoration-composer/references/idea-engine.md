# Topic Ideation Engine — 选题发动机

> Upstream ideation layer for `gemini-veo-restoration-composer`. Turns "give me ideas" into a ranked batch of novel, deduplicated, buildable time-lapse renovation topic seeds, each one ready to drop straight into the Internal Composition Pipeline (Tier 1 minimal input). Load this file when the user asks for ideas/选题/创意/brainstorm rather than supplying a topic.

---

## 0. Genre DNA (the one formula behind every winning clip)

Every high-performing video in this genre is the same sentence with different nouns:

> **A monumental, improbable RAW SHELL that nobody expects to be habitable — its wild/ruined exterior left visibly intact — is opened up and built out, beat by beat, into a warm, finished, lived-in INTERIOR, carrying exactly ONE signature impossible-but-buildable twist.**

The dopamine comes from **contrast held in tension**:
- Outside stays raw, huge, untouched (bark, rust, rock, hull).
- Inside becomes refined, cozy, human-scaled, softly lit.
- One "how is that even possible?" detail makes THIS clip screenshot-worthy.
- **And the hands are visibly doing it**: something is pried off the shell, de-rusted on camera, and comes back as furniture. Strip that out and the clip stops being a 改造 video — see the SALVAGE-AND-REBUILD POLICY under Axis 4.

The reference video proves the formula: living redwood trunk (shell) → cozy bedroom (interior) → glowing organic root-vein window (twist). The bark, the trunk taper, and the forest outside stay untouched the whole time; only the inside transforms.

**Idea generation = recombining the axes below, then filtering hard for novelty, contrast, and buildability.** Volume ("源源不断") comes from combinatorics; quality ("创新和独特性") comes from the filters and the mandatory signature twist.

---

## 1. Five-Axis Morphological Matrix (组合矩阵 — the endless supply)

Pick one entry per axis. The five axes are orthogonal, so the bank below yields tens of thousands of raw combinations before any twist variation. Rotate the **Carrier family** every batch (natural → man-made → vehicle/vessel → fantasy-grounded) so consecutive batches never feel same-y.

> **载体轴的来源排序（重要）**：当本批有联网趋势参考时，**Axis-1 的载体银行是兜底词源，不是选购清单** —— 载体轴以趋势参考为准，银行只用来命名或变化参考没说死的部分。Axes 2-5（环境/创伤/归宿/twist）照常自由组合。没有趋势参考时（离网/超时/案例库为空）银行才是首要载体来源。
>
> 这条排序原本只写在 `run_ideate` 的代码注释里、从未告诉模型，而下面的 53 条载体里有 21 条（近 40%）是天然壳体、趋势库里却几乎为零 —— 模型"照矩阵组合"完全合规，结果就是 2026-08-08 台账上连续七条岩洞/化石。**注意：无论有没有趋势参考，§2.3b 的天然壳体配额与 SALVAGE-AND-REBUILD 都照常生效**，所以顺着 A 组一路往下选必然超配额，优先用 B 组（废弃人造构筑物）与 C 组（载具/船体）。

### Axis 1 — CARRIER / 载体外壳 (the surprising shell — drives camera + workflow)

**A. Living / natural**
giant redwood or sequoia trunk · ancient hollow oak · baobab · giant saguaro cactus · granite tor / boulder · basalt column cluster · slot-canyon niche · lava tube · petrified log · cliff overhang ledge · collapsed sinkhole bowl · coral head (underwater) · giant clam / whale rib cage · waterfall grotto behind the falls

**B. Abandoned man-made**
lighthouse · water tower · grain silo · windmill · fire-lookout tower · mineshaft headframe · railway tunnel mouth · stone aqueduct arch · dam inspection gallery · derelict chapel · church bell tower · medieval gatehouse · Victorian ice house · brick kiln / oast house · martello tower · concrete pillbox bunker · disused subway platform · clock tower

**C. Vehicles / vessels**
retired diesel submarine · beached shipwreck hull · crashed airliner fuselage · double-decker bus · vintage tram / subway car · steam locomotive · stacked shipping containers · steel oil-storage tank · cement-mixer drum · cable-car / gondola cabin · hot-air-balloon gondola · wooden fishing trawler · airstream-era bus chassis · cargo plane tail section

**D. Fantasy-grounded (still obeys build physics — no magic)**
giant geode half · oversized tree-stump with ringed grain · monumental mushroom stalk · hollow meteorite · giant amber nodule · oversized seashell · carved iceberg block

> Each carrier carries an implicit Space Type + camera default. Map it through `space-workflows.md` (e.g. tree/cave/silo/bunker → `underground space` or `abandoned property`; vehicle/vessel → threshold-friendly interiors; tabletop fantasy object → `custom build object`).

### Axis 2 — ENVIRONMENT / 环境 (where the shell sits — drives passive layer + light)

misty redwood forest · alpine cliff edge · high desert mesa · arctic tundra · tropical coral reef (underwater) · cypress swamp / bayou · volcanic badlands · dense megacity rooftop · misty Nordic fjord · lavender field · bamboo grove · narrow slot canyon · frozen lake surface · salt flat at dusk · terraced rice valley · stormy coastal headland

### Axis 3 — TRAUMA / 创伤态 (the before-state pathology — drives IMAGE 1)

hollow & dry-rotting · rust-flaked & gutted · moss-choked & flooded · sand-buried to the waist · fire-charred & ash-coated · ice-encased & frost-cracked · vine-strangled & root-pierced · barnacle-crusted & salt-pitted · debris-packed & glass-strewn · collapsed-roof & open to sky · guano-caked & cobwebbed · silt-filled & tide-stained

### Axis 4 — DESTINY / 归宿 (the after-identity — drives fit-out beats + reward)

> **SHELTER-ONLY POLICY (硬约束)**: every destiny MUST be a habitable private **shelter / dwelling / refuge** — a place a person can sleep, shelter, and live in. Commercial, public, hospitality, studio, and other non-residential end-uses are **banned**: NO bar / cafe / tea house / speakeasy, NO recording / ceramics / painting / art studio, NO shop / gallery / museum, NO public observatory or attraction, NO commercial spa / sauna / onsen, NO lab, **NO workshop / repair room / 修缮室 of any kind**. Litmus test: "could one person live and sleep here as their own private refuge?" — if no, reject the candidate outright. Enforced by `prompt_pipeline:ideation_shelter_violations`; the 修缮室 clause is explicit because a "钟表与精密机械修缮室" actually shipped through this policy while it was prompt-text only.

> **SALVAGE-AND-REBUILD POLICY (硬约束 — DIY 内核)**: this genre is a **DIY conversion**, not a geology documentary. Every candidate must be able to name **ONE original component of the shell or its site** that is stripped out on camera and rebuilt into the finished interior as something else — brass portholes → backlit shelf lights, a grain chute → a hearth, winch gears → a counterweight bed, shell casings → a flue. Declare it in `salvage_zh` / `salvage_en`, and **spend a real construction beat on it** (拆下·拆解·除锈·翻新·改装·回装). If the only work a shell admits is carving, grinding and polishing its own natural surface, it is the **wrong shell** — that is the failure mode that quietly turned a run of batches into "十几种材质版本的同一个山洞暖阁". Enforced by `prompt_pipeline:ideation_salvage_violations`.

> **REALISM-ONLY POLICY (硬约束 — 写实风格)**: every destiny AND twist must read as a real-world, present-day, documentary-photographable build. **Sci-fi / futuristic themes are banned**: NO "sci-fi", "futuristic", "cyberpunk", "space-age", "capsule pod", "zero-gravity" destinies; NO holograms, force fields, glowing tech panels, LED-neon aesthetics, spacecraft-style seamless surfaces, or any technology that does not exist today. Interiors are warm, tactile, made of real materials (wood, stone, brass, wool, glass, leather). Fantasy-grounded carriers (geode, giant mushroom) stay allowed, but their fit-out must still be realistic craftsmanship — the wonder comes from the shell and the contrast, never from imaginary technology.

cozy bedroom retreat · one-room sleeping cabin · off-grid micro-home · mountaineer's base bunkroom · hermit's woodland hideout · snug winter refuge den · subterranean burrow dwelling · single-room forest cottage · cliffside sleeping loft · weatherproof base-camp shelter · solitary reading-and-sleeping nook · tiny self-sufficient hideaway home · storm-proof survival shelter · cozy off-grid sleeping pod

### Axis 5 — SIGNATURE TWIST / 招牌反差点 (the ONE memorable hook — mandatory, exactly one)

The detail that makes the clip unique and pause-worthy. Must look impossible yet be physically buildable under the engine's Global Causal Trace Rule. Banks:
- a window that is a **cross-section of the carrier's own material** (glowing root-veins, translucent ice, amber, geode crystal)
- a **glass floor** over running water / a koi pool / a glowing mineral seam
- a **bark-camouflaged roof hatch** that slides open to the stars
- a **spiral stair carved into the living grain / rock face**
- **bioluminescent moss or fungus** as the only light source
- a **bed / desk that slides out of the wall** of the shell
- a counter / headboard that is **one single slab of the carrier itself**
- a **waterfall re-routed into a shower** or a window-curtain of falling water
- a fireplace **vented through a natural flue** in the rock/trunk
- a **porthole bank** salvaged from the vessel reused as interior lighting

---

## 2. Novelty & Uniqueness Filters (创新与独特性保障)

Volume is cheap; these gates are what make ideas worth generating. Apply in order; drop any candidate that fails.

1. **Orthogonal-Pairing Rule (反差配对)** — every destiny is now a shelter, so the contrast lives in the **carrier × shelter improbability**: pick shells nobody expects could become a home (missile silo → off-grid micro-home; blue-ice cave → snug winter refuge den; cement-mixer drum → one-room sleeping cabin). Reject on-the-nose pairs (lighthouse → seaside bedroom is too expected) unless rescued by a strong twist.
2. **Mandatory Single Twist** — every surviving idea declares exactly **one** Axis-5 twist. Zero twists = generic = rejected. Two+ twists = cluttered = trim to the strongest one.
3. **Dedup vs. Ledger** — compute a Topic DNA fingerprint `carrier-slug / destiny / twist-family` and reject anything matching, or one edit-step away from, a row in [`used-topic-ledger.md`](used-topic-ledger.md). The redwood-trunk → bedroom → root-vein-window combo from the reference video is already burned and must not be re-proposed.

   **Compare per axis, and compare the twist by ROOT** — the full dedup rules live in the ledger's own "Dedup Rules" section; read them, they are not optional. The short version, because this is where dedup measurably failed:

   - **Twist root** = the first two hyphen-segments. `glass-floor-gears`, `glass-floor-cliff`, `glass-floor-tides`, `glass-floor-veins` are all root `glass-floor` — **one** twist, and it already occupies 12 of the ledger's 167 rows. A candidate whose twist root is burned is rejected even if its carrier and destiny are brand new; changing the shell around a used twist produces a variant, not an idea.
   - **Carrier family** = one of `natural` / `man-made` / `vehicle` / `fantasy` / `living-tree` / `treehouse`. Never a specific carrier name. A specific string is unique by construction and so silently passes every dedup check and every "rotate the shell family" rule.
   - **Never fingerprint a twist as `custom-twist` or `unspecified-twist`.** Those match nothing and block nothing. If you cannot name the twist in two or three hyphenated words, it has failed the mandatory-single-twist filter one step earlier.

   **2026-08-08 起有执行者了.** Twist-root 去重由 `prompt_pipeline:ideation_twist_root_violations` 在产出侧强制（burned root 由 `burned_twist_roots()` 从台账实时算出，并明写进激发 prompt 的 BURNED TWIST ROOTS 一节）。Carrier 家族由 `prompt_pipeline:carrier_family` 判定，只用于**配额**（见下面第 3b 条），不作为 DNA 去重维度。DNA 全串比对仍归 `_dedupe_generated_ideas`。你依然要在生成时自己遵守——门禁只是最后一道网，被它打回等于白烧一次 150s 调用。

3b. **Carrier-Family Quota (载体家族配额)** — 一批里最多 **1/3** 可以是**天然原位壳体**（岩洞/石龛/裂隙/巨石/冰洞/天坑/活体树干/化石·晶洞空腔），其余必须是**废弃人造构筑物**或**载具/船体**。理由不是"天然壳体不好"（参考片本身就是红杉树干），而是天然壳体身上**没有可拆下来再利用的旧构件**，工序会塌成打磨与抛光，直接违反上面的 SALVAGE-AND-REBUILD。

   这条治的是一个"每条单看都合法、连起来才有病"的漂移：2026-08-08 台账里连续 7 条是岩洞/化石。既有的"同批载体互不重复"规则挡不住它——两块不同的石头本来就互不重复。用户在 GUI 里钉死载体（theme_label）时配额不生效，那是他的显式选择。Enforced by `prompt_pipeline:ideation_family_quota_violations`。
4. **Cliché Blocklist** — auto-reject the oversaturated trio unless a fresh twist transforms them: generic "abandoned warehouse → industrial loft", "old van → camper conversion", "shipping container → minimalist tiny home".
5. **Buildability Gate** — must map to a real `space-workflows.md` macro and obey monotonic construction order (demo → structure → rough-in → enclose → finish → fixtures → furnish). The twist must be trace-producible (leaves seams/fasteners/contact marks). Pure magic, teleporting parts, or no-build-logic fantasy = rejected (consistent with SKILL "Do Not Use" rules).
5b. **Realism Gate (写实门)** — enforce the Axis-4 REALISM-ONLY POLICY: reject any candidate whose destiny, twist, materials, or naming leans sci-fi/futuristic (sci-fi capsule, cyberpunk anything, space-age pod, holographic/glowing-tech elements). Everything shown must be buildable today with real trades and real materials, in a documentary photorealistic register.
6. **Scroll-Stop Test** — the reveal must read in a single 1-second frame on a muted vertical feed. If you can't name the one hero frame, the idea is too subtle.

---

## 3. Scoring Rubric (排序打分 — 0-5 each, rank by total /25)

| Dimension | 中文 | 0 | 5 |
|---|---|---|---|
| Novelty | 反差新奇度 | obvious pairing | nobody has paired these before |
| Visual Contrast | 视觉反差 | before≈after | brutal raw shell vs. silk-soft interior |
| Twist Strength | 招牌点冲击 | decorative | the whole video exists for this frame |
| Buildability | 可施工度 | hand-wavy | every beat maps to a real macro + trace |
| Scroll-Stop | 1秒抓人 | needs explanation | hero frame reads instantly muted |

Output the top **N** by total score (default N=8). Ties broken by Twist Strength.

---

## 4. Continuous-Supply Ratchet (源源不断机制 — anti-repeat)

- **Family rotation**: within one batch, vary the Axis-1 carrier *family* so no two ideas share a shell family back-to-back. This is the soft version of the hard quota in §2.3b (天然壳体 ≤ 1/3) — rotation is what you aim for, the quota is what actually gets checked.
- **Ledger ratchet**: after the user picks ideas to build, append their Topic DNA to [`used-topic-ledger.md`](used-topic-ledger.md). The next ideation call is then forced into unused space — the supply never repeats and quietly drifts toward unexplored carriers.
- **Remix mode (系列延伸)**: on request, hold one already-used carrier constant and force a *new* destiny + twist to spin a recognizable series (e.g. the redwood again, now a stargazing observatory loft with a bark roof-hatch).
- **Constraint seeding**: if the user pins any axis ("must be underwater", "must be a vehicle", "for a kids channel"), lock that axis and recombine the other four.

---

## 5. Idea-Engine Output Contract (选题发动机输出格式)

Return a ranked Markdown table (Chinese-facing), one row per surviving idea:

| # | 一句话选题 | Carrier | Environment | Trauma | Destiny | Signature Twist | 旧物再生 | 评分 | Tier-1 一键输入串 |
|---|---|---|---|---|---|---|---|---|---|

- `一句话选题` — a single punchy Chinese line a creator could greenlight on sight.
- `旧物再生` — the SALVAGE-AND-REBUILD declaration (`salvage_zh`): which original component gets stripped out and what it becomes indoors. A row that cannot fill this column is not a valid idea.
- `Tier-1 一键输入串` — a ready-to-paste minimal-input sentence (e.g. `做一个高山悬崖废弃缆车舱改造成观星阁楼`) that drops straight into the composition pipeline with zero extra questions.

Close with one line: **「回复任意编号，我直接把它生成完整的 IMAGE/VIDEO 提示词集合」** — selecting a number hands that seed to Step 1 of the Internal Composition Pipeline and also appends its Topic DNA to the ledger.

---

## 6. Worked Example (derived from the reference video, then pushed forward)

The reference clip's Topic DNA — `living-tree / bedroom / self-material-window` — is now **burned**. The engine rotates off it into fresh, family-varied neighbors:

| # | 一句话选题 | Carrier | Env | Trauma | Destiny | Twist | 旧物再生 | 评分 | Tier-1 串 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 退役潜艇改造成离网单人居所 | retired submarine (vessel) | misty fjord | rust-flaked & gutted | off-grid micro-home | original brass portholes reused as backlit shelf lights | 原黄铜舷窗除锈回装成背光搁板灯 | 23 | `做一个退役潜艇舱改造成离网单人居所` |
| 2 | 废弃导弹井改造成地下隐居卧室 | missile silo (man-made) | high desert mesa | debris-packed & guano-caked | subterranean burrow dwelling | a concrete roof hatch slides open to a circle of sky | 原滑动发射舱门机构翻新复用作屋顶天窗 | 23 | `做一个废弃导弹发射井改造成地下隐居卧室` |
| 3 | 废弃水塔改造成林间独居睡眠阁 | derelict water tower (man-made) | bamboo grove | rust-flaked & silt-stained | solitary reading-and-sleeping nook | the old riveted tank ring becomes the loft's balustrade | 原铆接水箱环切段改装成阁楼护栏 | 22 | `做一个废弃水塔改造成林间独居睡眠阁` |
| 4 | 河畔树皮小屋改造成温润独居栖所 | riverside bark hut (natural) | misty riverbank | hollow & dry-rotting | snug winter refuge den | window cut straight through the bark skin | 原倒伏树干整料截成床台与门槛 | 22 | `做一个河畔树皮小屋改造成温润独居栖所` |

> 这份示例本身就是配额的样子：4 条里只有第 4 条是天然壳体（1/4 ≤ 1/3），前三条都带着可拆下来再用的旧构件。旧版示例里 4 条有 2 条天然、且没有一条填得出「旧物再生」——那正是漂移开始的地方。

Each row is a valid composition input. Pick one and the normal pipeline runs unchanged.
