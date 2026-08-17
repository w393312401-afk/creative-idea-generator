# 爆款复刻与正交受控发散架构技术方案规范书
# (Baseline Replication & Orthogonal Mutation Specification)

- **文档版本**：v2.0-STABLE
- **创建时间**：2026-08-15
- **适用模块**：`replica_pipeline.py`、`prompt_pipeline/`、`js/replica_pipeline.js`、`stepped_pipeline.py`
- **核心定位**：解决 AI 视频复刻中的“结构与材质漂移”痛点，建立从 **1:1 黄金母本精准克隆** 到 **正交多维受控二创发散** 的工业化出片管线。

---

## 1. 架构总览与核心设计哲学

### 1.1 现状核心痛点
1. **多模态语义降维导致“复刻不像”**：纯文本大模型（LLM）看图写话时丢失了 90% 的几何、纹理与坐标信息；文生图模型抽卡时缺乏物理底图约束，造成地标漂移、材质失真。
2. **二次创作缺乏约束导致“骨架坍塌”**：传统二创放任大模型自由重写，导致原有爆款经过实战验证的“工序逻辑、完播率节奏、ASMR 关键点、拍数”被全面打乱，生成成功率跌破 30%。
3. **母本资产未形成数字血统（Lineage）**：没有将 1:1 复刻的成果固化为可继承的基准资产，导致每次衍生新视频都要从零调试镜头。

### 1.2 双阶段演进架构（Two-Phase Dual-Engine Architecture）

```
                                  【输入源视频 (.mp4)】
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 阶段一：1:1 黄金母本克隆引擎 (Ground-Truth Baseline Cloner)                               │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 1. 视觉基准提取：5 列多宫格拼图 (<video_name>_collage.jpg，240px 单帧宽)                │
│ 2. 事实无损转录：13 字段客观事实 + 4-Zone 空间四域差量全覆盖扫描                        │
│ 3. 空间硬性约束：归一化九宫格坐标（NGCS）+ 相机焦段/地平线 50% 绝对锁死                 │
│ 4. 物理单调继承：过门时空单调继承协议（Threshold Monotonic Inheritance）                 │
│ 5. 视频插值与音效：首尾双图物理插值约束 + 60% 物理 ASMR 原声保留（0% BGM）              │
│ 6. 分步渲染验证：通过 Stepped Pipeline 审核通过后，固化加锁为「Gold Baseline 母本」     │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ 
                                            │ 100% 冻结：拍数 N、机位焦段、工序拓扑、ASMR 音效点
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 阶段二：正交矩阵受控发散引擎 (Orthogonal Mutation & Variant Generator)                  │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 1. 骨架硬冻结：工序先后依赖、分镜时长、进出场时间戳（t=0s进/t=7.5s撤）严格不变        │
│ 2. 槽位正交注入：沿四大正交轴进行参数化置换（环境地貌 / 材质工艺 / 空间功用 / 终极生物） │
│ 3. 批量变体派生：从同一母本一键派生 Variant A/B/C/D，生成成功率提升至 90%+            │
│ 4. 双轨拼图快检：母本 5 列拼图 vs 变体 5 列拼图横向比对，肉眼 3 秒判定漂移             │
│ 5. 一键成片交付：保留血统树，无缝推入分步渲染管线                                      │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 阶段一：1:1 黄金母本克隆技术规范

### 2.1 视觉提取与基准多宫格拼图
* **强制提取规则**：只要涉及视频分析，系统必须调用 FFmpeg 的 `tile` 滤镜，在视频同级目录下自动生成一张 **5 列固定、单帧宽 240px、行数向上取整** 的多宫格拼图大图（命名为 `<video_name>_collage.jpg`）。
* **用途**：作为后续所有提示词编写、关键帧生成与分步渲染质量检查的“唯一视觉真理对照表”。

### 2.2 13 字段客观事实契约
Pass A 与 Pass B 必须无损记录以下 13 字段，严禁在下游丢弃：
1. `beat_id`: 节拍唯一编号（如 `B01` ~ `B11`，严格 1:1 映射，严禁擅自增删）。
2. `start_sec` / `end_sec`: 精确起止时间戳。
3. `operation_type`: 工序类型（`demolition` / `structural` / `rough_in` / `enclosure` / `surface` / `floor` / `fixtures` / `furnishing` / `reveal`）。
4. `visible_subject`: 核心受力/改造构件。
5. `visible_details`: 具体材质、型号与几何特征。
6. `state_before`: 本拍开始前的物理空间状态（必须写清具体边界与遗留物）。
7. `state_after`: 本拍结束后的物理空间状态（必须写清完成面积与新结构）。
8. `persistent_traces`: 物理留存痕迹（螺栓头、焊接烧痕、打胶缝、锯末、地平线压痕）。
9. `workers_present`: 在场工人数与着装轮廓。
10. `camera_framing`: 相机焦段（14mm/18mm）、视高（1.2m/1.6m/2.5m）、地平线高度比例（50%）。
11. `grid_anchors`: 九宫格绝对空间定位（Grid A1 ~ C3 占比）。
12. `evidence_frames`: 关联的原视频证据帧编号列表。
13. `audio_asmr_cues`: 对应物理动作的特征音效（铲泥、敲击、电钻、焊接、水下气泡）。

### 2.3 物理守恒与去漂移三大铁律
* **全域 4-Zone 空间扫描守恒 (Full-Field Delta Conservation)**：
  编写任何一拍的图像/视频提示词前，必须强制扫描四大空间区域：
  1. **顶域 (Top / Overhead)**：天花板、梁架、吊灯、通风口、采光井。
  2. **中域 (Middle / Walls & Facade)**：墙面、立柱、门窗、开关、管线、挂件。
  3. **底域 (Bottom / Floor & Approach)**：地面、碎石、找平层、地板、地毯、前廊。
  4. **边际与废料 (Peripherals & Spoil)**：废料堆、工具箱、材料堆、电缆盘。
  *规则*：只要前后两帧发生物理差量，视频中必须 **100% 分配** 对应的工人动作、几何工具与物料消耗，严禁“幽灵凭空变化（Zero Phantom Changes）”。
* **过门时空单调继承协议 (Threshold Monotonic Inheritance)**：
  镜头从室外切入室内（`IMAGE T+1`）时，必须 100% 物理继承室外阶段已完成的所有施工产物（如顶部密封舱盖开孔、梯子位置、已清理地面）。严禁在室内首帧中描写已被室外工序修复的破损（如天花板重新漏水、地面重新出现落叶等状态倒退词）。
* **道具生命周期与材质锁死**：
  * 施工工具（三脚架、测量仪、电缆）仅允许在施工阶段存在，在软装及以后拍必须在负向词添加 `(tripod, construction tools, power cables:1.4)` 强制撤场。
  * 地板铺设完成后，强制锁死温润哑光/半哑光实木质感（`satin matte finish`），严禁产生高光湿水反光（`wet floor, high glossy mirror reflection`）。

### 2.4 音频混音标准规范
* **默认不静音**：保留并混合视频原声中的 **ASMR 音效**，默认音量设为 **60% (`videoVolume: 0.6`)**。
* **背景音乐关闭**：背景音乐伴奏音量设为 **0% (`bgmVolume: 0.0`)**。
* **解说人声**：旁白人声音量设为 **100% (`voiceVolume: 1.0`)**。

---

## 3. 阶段二：四轴正交受控发散技术规范

### 3.1 骨架硬冻结协议 (Skeleton Freeze Protocol)
当 1:1 母本固化为 `Gold Baseline` 后，启动二创发散时，以下维度 **100% 锁定，严禁大模型修改**：
1. **节拍总数 $N$ 恒定**：母本为 11 拍，所有派生变体必须严格为 11 拍，严禁拆拍、合拍。
2. **镜头动力学恒定**：每一拍的相机机位、视角高度、运动方式（静态三脚架 / 推进 / 硬切）完全继承。
3. **工序因果拓扑恒定**：破土 $\to$ 结构入位 $\to$ 密封 $\to$ 回填 $\to$ 进舱 $\to$ 吊顶 $\to$ 铺地 $\to$ 软装 $\to$ 亮灯 $\to$ 终极揭示，先后逻辑 100% 继承。
4. **进出场时间戳恒定**：工人 $t=0\text{s}$ 进场手持工具、$t=7.5\text{s}$ 撤离、$t=8\text{s}$ 纯净帧交接。

### 3.2 四轴正交发散调制矩阵 (4-Axis Mutation Matrix)

在骨架冻结的前提下，仅允许在以下 4 个相互独立（正交）的变量轴上进行词槽注入替换：

```
                              【四轴正交调制空间】
                                      ▲
                                      │ 轴 1：地貌与水体环境 (Environment & Biome)
                                      │   • 荒野泥岸 / 极地冰原 / 火山地热 / 雨林沼泽
                                      │
 轴 2：材质与工艺体系                ┼────────────────────────► 轴 3：空间功用与软装
   • 粗糙竹木 / 侘寂微水泥            │                            • 江景卧室 / 私汤茶室
   • 黑色碳化木 / 工业黄铜            │                            • 极客影音室 / 恒温酒窖
                                      │
                                      ▼ 轴 4：终极生物/事件揭示 (Hero Creature / Reveal)
                                          • 野生淡水大鲟鱼 / 极地独角鲸 / 荧光水母 / 史前巨鳄
```

#### 典型正交发散模板映射表：

| 变体代号 | 轴 1：地貌环境 | 轴 2：材质工艺 | 轴 3：空间功用 | 轴 4：终极揭示 (Hero) |
| :--- | :--- | :--- | :--- | :--- |
| **母本 Baseline** | 荒野河流泥岸 + 浑绿江水 | 蓝色钢构 + 暖橡木板 + 条形灯槽 | 水下全景江景卧房 + 白棉麻床品 | 2 米巨型野生淡水大鲟鱼游弋 |
| **变体 A (极地深潜)** | 极地厚冰原 + 剔透深蓝冰海 | 钛合金深潜舱 + 侘寂微水泥 + 碳化木 | 极地防风雪观测站 + 极地羽绒大床 | 4 米巨型北极独角鲸缓缓掠过窗前 |
| **变体 B (火山私汤)** | 火山热泉泥地 + 冒泡地热水体 | 哑光黑碳钢舱 + 黑色火山石 + 原木 | 恒温天然私汤茶室 + 悬浮实木榻榻米 | 温泉深水巨型荧光发光蝾螈群 |
| **变体 C (赛博极客)** | 暴雨热带雨林 + 泥泞黑水沼泽 | 迷彩复合舱 + 碳纤维蜂窝板 + RGB灯槽 | 独立极客电竞影音室 + 升降工作台 | 窗外巨型黑凯门鳄贴窗凝视 |

### 3.3 槽位正交注入算法 (Slot-Filling Injection)

后端算法严格执行以下流水线：
```python
def generate_orthogonal_variant(baseline_job: dict, mutation_axes: dict) -> dict:
    """
    通过词槽正交映射生成二创变体，确保物理结构零漂移。
    """
    variant_beats = []
    for beat in baseline_job["timelapse_beats"]:
        v_beat = copy.deepcopy(beat)
        
        # 1. 继承硬约束
        v_beat["camera_framing"] = beat["camera_framing"]
        v_beat["grid_anchors"] = beat["grid_anchors"]
        v_beat["duration_sec"] = beat["duration_sec"]
        
        # 2. 正交槽位替换
        v_beat["visible_subject"] = apply_slot_replacement(beat["visible_subject"], mutation_axes)
        v_beat["state_before"] = apply_slot_replacement(beat["state_before"], mutation_axes)
        v_beat["state_after"] = apply_slot_replacement(beat["state_after"], mutation_axes)
        v_beat["persistent_traces"] = apply_trace_mapping(beat["persistent_traces"], mutation_axes)
        
        # 3. 动态刷新 ASMR 音效映射
        v_beat["audio_asmr_cues"] = map_asmr_audio(beat["operation_type"], mutation_axes["material"])
        
        variant_beats.append(v_beat)
        
    return build_prompt_pack_from_beats(variant_beats, is_variant=True)
```

---

## 4. 数据结构与后端 API 接口契约

### 4.1 Job 核心数据模型升级

```json
{
  "job_id": "rep_20260815_001",
  "job_type": "baseline",                     // "baseline" (1:1母本) | "variant" (二创变体)
  "is_locked_baseline": true,                 // 是否已锁定为不可修改黄金母本
  "parent_baseline_id": null,                 // 若为变体，指向父母本 ID
  "source_video": "/path/to/source.mp4",
  "collage_path": "/path/to/source_collage.jpg",
  "media_metadata": {
    "duration_sec": 35.97,
    "width": 576,
    "height": 1024,
    "fps": 30.0,
    "aspect_ratio": "9:16"
  },
  "beats_count": 11,
  "timelapse_beats": [ /* 13 字段客观事实列表 */ ],
  "prompt_pack": {
    "title": "岸边沉水集装箱改造成全景水下江景卧房",
    "image_prompts": [ /* 11 张关键帧提示词 */ ],
    "video_prompts": [ /* 11 段视频插值提示词 */ ],
    "negative_prompt": "(tripod left in frame, construction tools after furnishing:1.4), (wet floor, high glossy mirror reflection:1.3)...",
    "audio_spec": {
      "videoVolume": 0.6,
      "bgmVolume": 0.0,
      "voiceVolume": 1.0,
      "audioStrategy": "asmr_preservation"
    }
  },
  "mutation_config": {
    "enabled": false,
    "selected_preset": null,
    "axes": {
      "environment": null,
      "material": null,
      "function": null,
      "hero_reveal": null
    }
  },
  "lineage_variants": [ "rep_20260815_var_001", "rep_20260815_var_002" ]
}
```

### 4.2 后端 API 路由清单

| 路由端点 | HTTP 方法 | 请求参数 | 响应与业务说明 |
| :--- | :--- | :--- | :--- |
| `/api/replica/upload` | `POST` | `FormData(video_file)` | 上传视频，启动抽帧，生成 5 列多宫格拼图 `<name>_collage.jpg` |
| `/api/replica/pass_a` | `POST` | `{ job_id, sample_mode }` | 执行 Pass A 逐帧客观事实无损提取 |
| `/api/replica/pass_b` | `POST` | `{ job_id }` | 执行 Pass B 聚类生成 13 字段节拍清单（`timelapse_beats.json`） |
| `/api/replica/save_beats`| `POST` | `{ job_id, beats }` | 保存人工核验后的节拍数据并触发 4-Zone 守恒与单调继承机械校验 |
| `/api/replica/compose` | `POST` | `{ job_id }` | 100% 字段级绑定合成 1:1 标准提示词包与音频规范 |
| `/api/replica/lock_baseline`| `POST`| `{ job_id }` | **[NEW]** 将验证通过的 1:1 Job 加锁固化为 `Gold Baseline` 并入库 |
| `/api/replica/mutate_orthogonal`| `POST`| `{ baseline_job_id, mutation_axes }` | **[NEW]** 基于已加锁母本执行四轴正交替换，瞬间派生二创 Job |
| `/api/replica/lineage` | `GET` | `?baseline_id=xxx` | **[NEW]** 查询某个母本派生出的所有二创变体树状关系 |
| `/api/replica/handoff` | `POST` | `{ job_id }` | 将 1:1 母本或二创变体一键递交给分步渲染管线（`/api/stepped/start`） |

---

## 5. 前端工作台交互与 UI 重构规范

### 5.1 双栏联动工作台布局

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 爆款复刻与正交发散工作台                                        [母本: 水下集装箱卧房 (11拍)] │
├─────────────────────────────────────────┬───────────────────────────────────────────────┤
│ ◀ 左栏：1:1 黄金母本视窗 (Baseline View)  │ ▶ 右栏：正交发散调制器 (Orthogonal Mutator)   │
├─────────────────────────────────────────┼───────────────────────────────────────────────┤
│ [ 5 列原片拼图快检组件 ]                │ [ 预置正交变体选择: 极地 / 火山 / 赛博 / 自定义 ]│
│ ┌───┬───┬───┬───┬───┐                   │                                               │
│ │01 │02 │03 │04 │05 │ (240px 单帧宽)    │ 四轴参数化词槽配置:                           │
│ ├───┼───┼───┼───┼───┤                   │ 1. 地貌与水体: [ 极地冰原与深蓝冰海        ▼ ] │
│ │06 │07 │08 │09 │10 │                   │ 2. 材质与工艺: [ 侘寂微水泥 + 黑色碳化木   ▼ ] │
│ ├───┼───┴───┴───┴───┤                   │ 3. 空间与软装: [ 极地防风雪深潜观测站      ▼ ] │
│ │11 │ (已锁死 🔒)    │                   │ 4. 终极生物物: [ 4米巨型野生北极独角鲸     ▼ ] │
│ └───┴───────────────┘                   │                                               │
│ • 状态: [ 已固化为 Gold Baseline 🔒 ]   │ 约束保障:                                     │
│ • 拍数: 11 拍 (不可篡改)                │ • 拍数严格保持: 11 拍 (1:1 同构)              │
│ • ASMR 原声保留: 60% (BGM: 0%)          │ • 机位焦段: 100% 继承母本坐标                 │
│                                         │                                               │
│ [ 🚀 送去分步管线渲染 1:1 母本 ]        │ [ ⚡ 一键生成二创变体提示词包 (Variant) ]     │
│ [ ➕ 以此母本为基准派生变体 ───────────►│ [ 👁 双轨 5 列拼图横向对比快检 ]             │
└─────────────────────────────────────────┴───────────────────────────────────────────────┘
```

### 5.2 核心交互组件说明
1. **双轨 5 列拼图对比器 (Dual-Track 5-Column Comparator)**：
   * 左侧渲染原视频 5 列拼图，右侧动态加载分步渲染或变体效果图，横向肉眼 3 秒比对光影色调、地板哑光质感与背景空间结构。
2. **母本加锁开关 (Baseline Lock)**：
   * 母本核验通过后点击加锁，节拍编辑器自动转为只读预览模式，彻底防止误修改导致骨架与变体脱节。
3. **血统树导航卡片 (Lineage Tree Nav)**：
   * 变体详情页顶部显式展示面包屑：`母本 [水下集装箱 1:1] ➔ 变体 A [极地深潜观测站]`，支持任意切换。

---

## 6. 实施路线图与验收指标

### 6.1 分期落地排期

| 阶段 | 周期 | 核心开发任务 | 交付物 |
| :--- | :--- | :--- | :--- |
| **P0：母本固化与数据流升级** | 2 天 | 1. 扩展 `replica_pipeline.py` 支持 `baseline` 锁定机制。<br>2. 修复 `beats_to_dimensions` 字段收窄问题，实现 100% 字段级绑定。<br>3. 默认配置 60% ASMR 混音参数与 0% BGM。 | 跑通任意真视频 1:1 复刻，生成标准 5 列拼图与 11 拍固化母本。 |
| **P1：正交发散调制引擎** | 2 天 | 1. 实现 `prompt_pipeline/mutate.py` 的词槽正交注入算法。<br>2. 建立四轴预置矩阵模板库（极地、火山、赛博、洞穴等）。<br>3. 增加变体拍数与工序拓扑不变性校验门禁。 | 能够基于 1:1 母本一键派生 4 套零漂移二创变体。 |
| **P2：双栏 UI 与血统树重构** | 1.5 天 | 1. 重构前端工作台为左母本 / 右发散双栏界面。<br>2. 实现双轨 5 列拼图横向对比器。<br>3. 落地血统树管理与一键分步渲染递交。 | 完整前端可视化交互与操作流跑通。 |

### 6.2 验收指标（Checklist）
* [x] **5 列拼图持久化**：视频上传分析后，原视频同级目录必有 `<video_name>_collage.jpg`。
* [x] **1:1 拍数强约束**：$N$ 拍原片 = $N$ 张关键帧 = $N$ 段视频，严禁多帧少帧。
* [x] **4-Zone 差量平衡**：顶域/中域/底域/边际 100% 动作-工具-物料-痕迹闭环。
* [x] **二创骨架零坍塌**：变体生成后，机位高度、工序前后顺序、分镜时长与母本 100% 同构。
* [x] **ASMR 混音契约**：所有产出成片默认声明 `videoVolume: 0.6`、`bgmVolume: 0.0`、`voiceVolume: 1.0`。
* [x] **测试回归**：现有 93 项单元测试继续 100% PASS，新增正交发散单测 15 项。
