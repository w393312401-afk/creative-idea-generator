# Threshold Bridge Consistency Protocol — 微缩剖面版 (Miniature TBCP v1)

> 本文件是 `gemini-miniature-restoration-composer` 自己的过门协议，**取代** base 包
> 的同名文件。
>
> 为什么必须自带一份：base 的 TBCP 整篇都在规范「镜头如何穿过一扇真门走进室内」，
> 并在它的防膨胀负向词库里压制微缩与娃娃屋比例。微缩题材**根本不存在走入式过门**
> ——镜头永远在模型外面，室内是靠敞开的剖面看见的。把 base 那一份喂进来，等于要求
> 模型把相机塞进一个巴掌大的房子里。
>
> 本文件同样刻意不复述 base 的具体负向词串：整篇会被内插进 system prompt。

---

## 核心裁决：过门被剖面揭示取代 (Crossing → Cutaway Reveal)

上游的节拍规划层（Phase 1）与技能包无关，因此一条微缩单里**仍可能被规划出一个
「过门拍」**（`bridge_stage == 1` 或 `hard_cut`）。这一拍不作废，但它的内容整体
改写为**剖面揭示拍**：

| | base（实景） | miniature（微缩） |
| :--- | :--- | :--- |
| 这一拍做什么 | 镜头从室外推进、推开门、穿过门框、落定室内 | 镜头不动；巨人手把外立面板/屋顶整体揭开，露出内部 |
| 视点变化 | 室外视点 → 室内视点 | **视点不变**，始终在模型外部 |
| 曝光变化 | 室外强光 → 室内暗部，白平衡滚动 | 内部由微型 LED 或反光板补光，曝光平滑过渡 |
| 这一拍的产物 | 室内第一张毛坯帧 | 敞开剖面里的毛坯内部，外立面被移到画外或搁在旁边 |

---

## 规则

### 1. 揭示动作必须由巨人手完成 (Hand-Driven Reveal)

剖面不能"自己打开"。这一拍的可见动作就是**手把面板拿走**：

- 手指捏住外立面板的上缘/侧缘，沿着预留的卡槽把整块板平移抽出，或把屋顶部件
  整体向上提起、移出画面。
- 板/屋顶离开后，手退出画面，末帧是一个静止的敞开剖面。
- 被取下的部件要有去处：搁在沙盘基底旁边、靠在树桩侧面、或完全移出画外。
  **不要让它凭空消失**——这是本拍最常见的穿帮。

### 2. 剖面内部保持未动工状态 (Sterile Reveal)

与 base 的「过门片段必须穿过一个未被动过的废墟」同源，这一条一字不改地保留：

- 揭开的瞬间，内部是**原始毛坯**：裸露的椴木骨架、未打磨的切口、积灰的地板片。
- 这一拍里**不做任何施工**：不清理、不安装、不上色。工具、胶水、材料堆都不入画。
- 不要把这一拍写成 time-lapse——它是一次连续的、正常速度的揭示动作。

### 3. 内部继承外部已完成的一切 (Monotonic Inheritance)

剖面揭开后看到的内部，必须 100% 继承此前所有外部工序的成果：

- 屋顶如果已经在外部拍里铺好了瓦，剖面里的天花板内侧就应该看得见那层瓦的底面
  与椽条，**绝不能再出现"屋顶破洞漏光"**。
- 地基如果已经找平，剖面里的地板就是平的。
- 只有**内部专属**的未完成项才保持毛坯：内墙面未处理、无隔断、无水电、无家具。

### 4. 剖面一旦敞开就保持敞开 (Persistent Cutaway)

揭示拍之后的所有室内工序拍，机位、剖面开口、外立面部件的去处都保持一致。不要
在后续某一拍里让外立面无声地装回去，也不要在两拍之间切换剖面的方向（比如这一
拍从正面剖，下一拍从侧面剖）——那等于换了一个模型。

最终揭示拍（Final Reward）是唯一的例外：外立面可以由巨人手装回去，作为完工的
收尾动作，但**装回的必须是同一块板**，并且要写明它是被装回而不是新出现的。

### 5. 严禁出现的措辞 (Banned Phrasings)

门禁 `check_miniature_cutaway_framing` 会拦下这些；写的时候直接绕开：

- `walks across the threshold` / `walks through the threshold`
- `steps inside the room` / `steps inside the interior`
- `pushes through the doorway and settles inside` / `pushes through the door into ...`
- `the camera walks / enters / steps into ...`

正确的替代措辞：

- `an oversized hand lifts the front facade panel clear of the shell, revealing the raw interior`
- `the roof section rises away in the hand's grip, opening the model from above`
- `the open-front cutaway now reads straight through to the back wall`

---

## 微缩 TBCP 审计检查表

| 审计指标 | 检查项目 | 消除的失真风险 |
| :--- | :--- | :--- |
| **手驱动揭示** | 剖面是否由巨人手明确取下面板/屋顶而敞开？ | 面板凭空消失或剖面自行打开 |
| **部件有去处** | 取下的面板/屋顶是否交代了落点（旁边/画外）？ | 物件在两帧之间蒸发 |
| **揭示拍零施工** | 这一拍内部是否完全没有清理、安装、上色？ | 揭示与施工混杂，工序逻辑断裂 |
| **单调继承** | 内部是否继承了外部已完成的屋顶/地基成果？ | 已修好的屋顶在室内帧里重新破洞 |
| **剖面持续敞开** | 后续室内拍的剖面方向与开口是否一致？ | 剖面方向切换 = 观众读成另一个模型 |
| **无走入式措辞** | 是否出现 walk-in / step inside / push through doorway？ | 触发 cutaway 门禁并触发回炉 |
| **视点未变** | 机位是否始终在模型外部？ | 相机被塞进巴掌大的房子里，空间畸变 |
