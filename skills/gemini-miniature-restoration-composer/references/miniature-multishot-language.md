# Miniature Multi-Shot Language — 微缩多镜头组接语法

> 本文件规定本包每一条 VIDEO 提示词的镜头结构。它**取代**「一镜到底」的旧写法：
> 2026-08-23 之前本包交付的是一条锁死机位的单镜片段，而 SKILL.md 的自我描述一直写着
> 「微距多镜头 VIDEO 提示词」——散文与实现分叉了很久。现在以本文件为准。
>
> 与 `gemini-omni-restoration-composer` 的同类文件是**同一套机制、不同世界观**：镜头梯、
> 切点表、一镜到底禁令、镜头名审计全部相同；镜头名与逐镜职责按微距沙盘重写，omni 的
> 过门梯（wide approach → threshold → interior wide）与兑现梯（detail → pull-back →
> final wide）在这里一条都不许用——前者是走入式穿门的镜头名，正是本包 P0 禁止的东西；
> 后者的 pull-back 会在一条靠「机位不动」立住锚点连续性的片子里换掉机位。

## 一条主工作镜，被一到两个特写插入切开

**一拍就是一条贯穿全段的微距主工作镜，中间被一到两个特写插入切开，再切回同一机位收尾。**
整套语法就这一句。

| 片长 | 镜头数 | 结构 |
|---|---|---|
| 4s | 3 | **macro working shot** → close-up insert → returning macro shot |
| 6s | 3 | **macro working shot** → close-up insert → returning macro shot |
| 8s | 4 | **macro working shot** → close-up insert → extreme close-up insert → returning macro shot |
| 10s | 4 | **macro working shot** → close-up insert → extreme close-up insert → returning macro shot |

本链路的面板片长固定 8 秒，因此**默认就是四镜**。片长变长不换来更多景别，只换来第二个
插入镜和更长的主镜。

**第一镜与最后一镜是同一台机位**——同一位置、同一构图、同一焦段，差别只有工序推进到哪。
这正是本包一直以来靠「机位锁死」立住的那件事：首帧锚与尾帧锚天然落在同一构图上，锚点
连续性是相机的属性，不必靠正文反复申明。切回镜必须把这件事写出来：
`the same locked macro setup as the opening macro working shot`。

**相机自身永远不动**：不摇、不推轨、不升降、不拉开、更不进模型。画面里唯一在动的是
巨人手、工具与材料。所谓「切进特写」是**换一个镜头**，不是把相机推过去。

**只有一个插入镜时（4s / 6s），第二个插入的职责并进它**——那一镜同时交代工具接触点
**和**至少两处持久手工艺痕迹。职责不会随着被裁掉的镜头一起消失。

**剖面揭示拍与最终兑现拍走同名的三镜梯**，镜头名一模一样（主镜 / 插入 / 切回），
逐镜职责整套不同：

- **剖面揭示拍**：主镜 = 巨人手把外立面板或整片屋顶匀速抬离并搬走，机位一动不动；
  插入镜 = 面板脱离卡槽的接缝特写与面板的**去处**；切回镜 = 内部在敞开剖面里一次看全，
  毛坯状态原封不动。**本拍零施工**，不清理、不安装、不上色，不写任何 time-lapse 措辞。
- **最终兑现拍**：主镜 = 完工全貌 + 最后一件收尾动作（装回立面板 / 拨亮微型 LED /
  把人偶放上门廊）；插入镜 = 签名细部（灯珠亮起的窗格、门牌、人偶落座）；
  切回镜 = 暖光下的完工全貌，人偶从旁观转为入住，双手已退出画幅。

这两类拍免除下面的节奏声明（它们是揭示与收尾，不压缩劳动），但**都不免除一镜到底禁令**。

## 切点表（Shot Timeline）

只报菜名不够：不把切点钉在秒上，模型会自己挑，插入镜要么消失要么吃掉主镜。因此每一条
VIDEO 正文都带一句切点表，位置紧跟锚定开场句之后。

八秒（本链路默认）：

```text
Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 2.6, a close-up insert from 2.6 to 4.3, an extreme close-up insert from 4.3 to 5.8, and a returning macro shot from 5.8 to 8.0 seconds.
```

八秒的揭示拍与兑现拍（三镜）：

```text
Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 3.2, a close-up insert from 3.2 to 5.3, and a returning macro shot from 5.3 to 8.0 seconds.
```

规则：

- 时长按镜头权重分，不是平均分。主镜最长，切回镜次之，插入镜拿剩下的。插入镜跑到一秒
  上下是正常的——一秒的**插入**读作插入，一秒的**景别**才读作闪帧。
- 切点单调、无缝：每一镜从上一镜结束处开始，第一镜从 `0.0` 起，最后一镜正好落在片长上。
- 每个镜头自己的首句要用**英文单词**复述一次自己的入点
  （`A clean cut at the three-second mark drops into a close-up insert on the tweezer tip ...`），
  与切点表形成冗余绑定。
- **这句切点表是全篇唯一允许出现阿拉伯数字的地方**（见 `miniature-output-templates.md`
  的记号禁用与时间码豁免）。正文其余部分的计数与尺寸一律写成英文单词或比较物。

切点表由 composer 确定性注入并覆写——模型自己编的时间线不作数。

## 节奏声明（Pacing Declaration）

每一条普通工序 VIDEO 都要在正文里声明一次时间基准，用这句原话：

`edited miniature craft time-lapse assembled from multiple macro camera setups, not real-time footage, with oversized human hands entering and withdrawing between passes`

**不要**在这句里用 `continuous`。在一条剪辑过的片子里，那个词会被读成「拍一条一镜到底」，
与切点表直接冲突。本包旧版的 `continuous miniature craft time-lapse (not real-time)` 已作废。

揭示拍与最终兑现拍免除这一句。

## 镜内连续性（In-Shot Continuity）

镜头内部，主导运动从这一镜的第一刻延续到最后一刻：不许静止起手、不许停顿、不许减速收尾。
压缩只发生在切点上，不靠镜内定格。正文里声明一次：

```text
Inside every shot the frame keeps living from its first to its last moment — the giant hand's own motion, drifting dust, and the settling of craft debris never freeze, while the camera itself stays locked — and this beat's change advances only during the working shots. The only compressions in the clip fall exactly on the listed cut marks; no shot contains a hold, a stall, or a deferred step that is then delivered all at once.
```

**不要**改成「整条片子的改动匀速推进」。画面运动与工序推进是两件事：插入镜按契约零推进，
而切回镜恰恰是那处声明过的 same-way 压缩落点。一条片子不可能既「每一刻都在推进」又遵守
自己的镜头级进度锁。

## 巨人手在各镜里的状态

IMAGE 锚点仍然是净帧（手不在画面里），但 VIDEO 不花时间去弥合这条边界：第一帧就已经有
一只手在作业面上并发生有效工具接触。

| 镜头 | 巨人手状态 |
|---|---|
| macro working shot | 一只超大真人手已从画幅某条边缘伸入，零秒发生第一次有效工具接触；随后进入重复动作循环，本拍改动在这一镜推进到约四分之三。另一只手可扶住壳体。 |
| close-up insert | 只有指尖与工具接触点，以及材料物理（胶液铺开、砂浆挤出、木屑翻卷）。零推进。 |
| extreme close-up insert | 只有痕迹与微观质感，手可以完全不在画面里。零推进。 |
| returning macro shot | 剩余重复动作做 same-way 压缩，手完成最后一次操作后**退出画幅**，末帧是净帧，画面落到这一拍的结果 IMAGE。 |

**不要为「手入画」单独安排一个镜头**，也不要写手从画外走近或离开的过程——手在第一帧就已经
在作业面上，最后一镜里退出画幅。人偶全程只旁观、不施工，站位在整条序列里不变。

## 与旧稿的区别（照旧稿抄最容易踩的四条）

1. 旧稿写 `static macro diorama eye-level shot` 一句就结束了镜头声明——现在还必须写出
   四个（或三个）镜头名与切点表。
2. 旧稿写 `continuous miniature craft time-lapse` ——现在是 `edited ... assembled from
   multiple macro camera setups`。
3. 旧稿的 `The camera does not move ... throughout` 仍然成立，但要理解成「**机位**不动」，
   不是「**不剪辑**」。同一台机位 + 剪进剪出特写，两件事并不矛盾。
4. 旧稿把「持久痕迹」写在结尾一句里——现在痕迹是**插入镜的职责**，写在它自己那一镜里。
