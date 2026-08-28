"""四轴正交受控发散调制引擎 (Orthogonal Mutation & Variant Generator)

规范见 docs/replica_baseline_and_orthogonal_mutation_spec.md。
在 1:1 黄金母本 (Gold Baseline) 骨架硬冻结的前提下，沿四大正交轴进行参数化词槽置换：
  1. 轴 1：地貌与水体环境 (Environment & Biome)
  2. 轴 2：材质与工艺体系 (Material & Craft)
  3. 轴 3：空间功用与软装 (Space Function & Furnishing)
  4. 轴 4：终极生物/事件揭示 (Hero Creature / Reveal)

核心原则：
  - 骨架硬冻结：拍数 N 恒定、镜头机位坐标恒定、工序先后因果拓扑恒定、进出场时间戳恒定
  - 槽位正交注入：仅替换物理材质、环境载体、功用与终极生物
  - 物理 ASMR 动态刷新：根据工序与新材质更新音效特征
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Optional

# 四大正交轴定义与元数据
ORTHOGONAL_AXES = {
    'environment': {
        'key': 'environment',
        'label': '地貌与水体环境',
        'en_label': 'Environment & Biome',
        'hint': '荒野水岸 / 极地峡湾 / 火山地热 / 雨林溪谷 / 崖壁溶洞 / 荒漠绿洲',
    },
    'material': {
        'key': 'material',
        'label': '材质与工艺体系',
        'en_label': 'Material & Craft',
        'hint': '粗石毛石 / 侘寂微水泥 / 哑光黑碳钢+火山石 / 老柚木+黄铜 / 传统夯土',
    },
    'function': {
        'key': 'function',
        'label': '空间功用与软装',
        'en_label': 'Space Function & Furnishing',
        'hint': '水岸隐舍 / 极地观景木屋 / 恒温私汤茶室 / 水上木工坊 / 崖穴酒窖 / 夯土避暑居',
    },
    'hero_reveal': {
        'key': 'hero_reveal',
        'label': '终极生物/事件揭示',
        'en_label': 'Hero Creature / Reveal',
        'hint': '野生大鲟鱼 / 北极白鲸 / 高山马鹿 / 温泉猕猴 / 热带水豚与巨骨舌鱼 / 绿洲双峰驼',
    },
}

# 预置四轴正交调制矩阵模板库（严格遵循写实唯真准则，全面去科幻/去太空）
MUTATION_PRESETS: Dict[str, Dict[str, Any]] = {
    'polar': {
        'key': 'polar',
        'name': '极地防风雪避险庇护所 (Polar Fjord Refuge Shelter)',
        'description': '极地厚积雪 + 芬兰松木原木与保温气凝胶 + 防风雪避难木屋 + 野生北极白鲸',
        'axes': {
            'environment': '极地厚积雪地貌与剔透深蓝峡湾海面',
            'material': '粗犷芬兰松木原木 + 气凝胶保温层 + 侘寂微水泥 + 黑色碳化防腐木',
            'function': '防风雪极地避险庇护所 + 粗石壁炉与防寒羽绒保暖卧榻',
            'hero_reveal': '野生北极白鲸群在窗外深蓝峡湾中缓缓掠过',
        },
        'scene_signature': 'A heavy timber, aerogel insulated and stone refuge shelter cabin embedded in arctic snow, overlooking clear deep-blue fjord waters.',
        'banned_elements': ['sci-fi capsule', 'cyberpunk', 'neon lights', 'spaceship', 'hologram', 'muddy riverbank', 'green river water', 'rusted mild steel container', 'warm yellow household lighting', 'generic cozy homestay'],
    },
    'volcano': {
        'key': 'volcano',
        'name': '火山地热自持庇护所 (Volcanic Geothermal Shelter)',
        'description': '火山热泉泥地 + 哑光黑耐候钢 + 恒温地热能源庇护所 + 高山溪红点鲑群',
        'axes': {
            'environment': '火山热泉泥地与冒泡地热蒸气水体',
            'material': '哑光黑耐候钢构件 + 黑色玄武岩打磨 + 导热紫铜管 + 碳化原木',
            'function': '恒温地热自持能源庇护所 + 地热温差发电与悬浮实木榻榻米',
            'hero_reveal': '地热温泉清澈水体中高山溪红点鲑群缓缓游弋',
        },
        'scene_signature': 'A matte black weathering-steel geothermal energy shelter pavilion nestled in volcanic hot spring terrain with porous black basalt stone walls.',
        'banned_elements': ['sci-fi', 'futuristic', 'neon channels', 'glowing tech', 'iceberg', 'snowfield', 'office desk', 'bright fluorescent tube', 'muddy green river', 'generic cozy homestay'],
    },
    'rainforest': {
        'key': 'rainforest',
        'name': '雨林防洪重工庇护所 (Rainforest Flood Refuge Bastion)',
        'description': '热带雨林水域 + 老柚木防腐榫卯 + 悬空防洪浮水木工庇护所 + 窗外野生水豚与巨骨舌鱼',
        'axes': {
            'environment': '暴雨热带雨林与原生态清澈溪流水域',
            'material': '缅甸老柚木防腐结构 + 悬空抬升毛石基座 + 铜质暗扣',
            'function': '悬空防洪木工庇护所 + 手工皮革制作台与防潮原木搁架',
            'hero_reveal': '窗外清澈水流中野生水豚与巨骨舌鱼缓缓游弋',
        },
        'scene_signature': 'A handcrafted reclaimed teak wood and rough stone flood-refuge craftsman stilt shelter submerged along a tropical rainforest river.',
        'banned_elements': ['sci-fi', 'cyberpunk', 'neon lights', 'rgb lighting', 'carbon fiber', 'hologram', 'polar bear', 'wooden cabin', 'tatami', 'hot spring steam', 'rustic bamboo', 'generic cozy homestay'],
    },
    'cave': {
        'key': 'cave',
        'name': '崖壁溶洞隐秘庇护所 (Cliff Karst Underground Shelter)',
        'description': '天然石灰岩溶洞 + 玄武岩黄铜 + 恒温地下水储能隐蔽庇护所 + 地下暗河野生盲鱼群',
        'axes': {
            'environment': '天然石灰岩溶洞石壁与清冽地下暗河',
            'material': '粗糙玄武岩打磨 + 黄铜暗埋构件 + 老橡木实木梁',
            'function': '隐秘溶洞恒温地下水与给养储藏庇护所',
            'hero_reveal': '溶洞地下清冽暗河中野生岩斑盲鱼群缓缓游弋',
        },
        'scene_signature': 'A subterranean basalt and brass refuge sanctuary built inside a natural karst cave alongside a clear underground river.',
        'banned_elements': ['sci-fi', 'futuristic', 'crystal fantasy', 'glowing crystals', 'neon', 'sunlight', 'skyline', 'traffic noise', 'plastic panels', 'white drywalls', 'generic cozy homestay'],
    },
    'desert': {
        'key': 'desert',
        'name': '荒漠防沙暴地下庇护所 (Desert Sandstorm Underground Shelter)',
        'description': '干旱荒漠红土 + 传统夯土墙胡杨木 + 穹顶采光防沙庇护所 + 绿洲清泉双峰驼',
        'axes': {
            'environment': '干旱荒漠红土与绿洲清泉水体',
            'material': '传统生土夯土墙 + 粗壮胡杨木大梁 + 透气陶土砖 + 防沙重力门',
            'function': '绿洲地下防沙暴储能庇护所 + 穹顶自然采光与亚麻地台',
            'hero_reveal': '绿洲清泉边野生双峰驼低头静静饮水',
        },
        'scene_signature': 'A monolithic rammed-earth and desert timber underground sandstorm refuge shelter integrated into desert oasis terrain with deep shaded openings.',
        'banned_elements': ['sci-fi', 'futuristic', 'neon', 'metal spacecraft', 'high-tech panels', 'blizzard', 'glacier', 'generic cozy homestay'],
    },
}

# 兼容别名：将历史 'cyber' 指向真实雨林重工匠人庇护所
MUTATION_PRESETS['cyber'] = MUTATION_PRESETS['rainforest']

# 工序与材质维度的物理 ASMR 音效映射库
_ASMR_CUES_MAP = {
    'demolition': {
        'default': ['铲掘碎石泥土摩擦声', '重锤破拆沉闷撞击声'],
        'metal': ['角磨机切割火花尖啸声', '液压钳剪切金属嘎吱声'],
        'stone': ['电镐破开坚硬玄武岩石碎裂声', '清理碎石滚落脆响'],
        'ice': ['重镐破开极地厚冰层清脆崩裂声', '冰块滑入深水噗通回响'],
    },
    'structural': {
        'default': ['重型机械轰鸣与构件入位定位沉闷撞击声', '扭力扳手紧固高强螺栓咔哒声'],
        'metal': ['二氧化碳保护焊嗤嗤熔池声', '钛合金厚板吊装就位金属沉闷锁死声'],
        'stone': ['玄武岩石料找平敲击声', '石料密封胶条挤压密实声'],
        'ice': ['热熔锚栓打入冻土层嘶嘶蒸汽声', '防寒结构锁紧卡扣咔嚓声'],
    },
    'rough_in': {
        'default': ['电锤钻孔打穿硬壁突突声', '波纹管穿线摩擦沙沙声'],
        'metal': ['金属穿线管紧固套环咔嗒声', '密封防水接头扳手锁紧声'],
        'stone': ['石壁开槽钻孔高频微颤声', '暗埋管线打胶注入声'],
        'ice': ['低温绝缘保温棉撕开魔术贴声', '伴热电缆卡扣逐段就位咔嗒声'],
    },
    'enclosure': {
        'default': ['气动排钉枪清脆打入骨架嘭嘭声', '密封胶枪挤出胶条均匀附着声'],
        'metal': ['碳纤维蜂窝板卡入铝合金型材清脆入槽声', '防水发泡胶膨胀微小气泡破裂声'],
        'stone': ['火山石板贴合微水泥砂浆沉闷刮平声', '防潮卷材压路平整滚压声'],
        'ice': ['多层真空绝热板卡槽扣合声', '低温硅酮密封胶均匀刮平声'],
    },
    'surface': {
        'default': ['批刀刮抹细腻微水泥沙沙声', '打磨机砂纸由粗至细研磨微尘声'],
        'metal': ['碳化木表面哑光木蜡油涂抹浸润声', '金属防锈涂层细腻喷雾声'],
        'stone': ['火山石哑光固化剂滚涂刷刷声', '天然石材缝隙勾缝抹平声'],
        'ice': ['防结露微水泥抹子推平沙沙声', '耐候哑光面漆精细滚涂声'],
    },
    'floor': {
        'default': ['实木地暖锁扣地板逐块橡胶锤敲击密缝声', '地板防潮静音垫铺展摩擦声'],
        'metal': ['工业级防静电碳纤维地板卡扣入位声', '哑光地砖调平器紧固脆响'],
        'stone': ['玄武岩厚板铺装橡胶锤找平沉闷敲击声', '水性哑光封孔剂均匀刷涂声'],
        'ice': ['极地防滑加厚碳化木地板锁扣敲合声', '地暖垫层铺设平整沙沙声'],
    },
    'fixtures': {
        'default': ['电动螺丝刀精密旋入五金自攻螺丝转鸣声', '暗藏灯带卡入铝槽啪嗒锁死声'],
        'metal': ['精密黄铜五金件旋转旋紧机械手感声', '嵌入式控制面板轻柔卡入声'],
        'stone': ['岩石内嵌黄铜水龙头旋紧沉重手感声', '隐藏式暖光发光灯槽通电轻微电流微鸣'],
        'ice': ['全密封防风雪舱门铰链转动油润低鸣', '双层真空玻璃窗锁死手柄旋转扣合声'],
    },
    'furnishing': {
        'default': ['棉麻布艺床垫落位空气挤压轻微噗声', '实木家具平稳摆放木质接触轻响'],
        'metal': ['人体工学升降台电机静音运转微鸣', '电竞椅滑轮在防静电地面顺滑滚动声'],
        'stone': ['天然原木茶几稳稳安置在石面沉实声', '粗陶茶具轻置茶盘清脆微鸣'],
        'ice': ['极地防寒加厚羽绒被抖开蓬松展开声', '羊毛地毯铺平摩擦暖融沙沙声'],
    },
    'reveal': {
        'default': ['整体暖光渐亮通电轻微柔和电流音', '全景窗外平静水流缓缓流动回响'],
        'metal': ['深蓝水下全景窗外巨大水流波动沉缓水声', '巨型生物缓缓掠过水流微波涌动低鸣'],
        'stone': ['地热温泉水体冒泡咕嘟咕嘟天然水声', '发光生物群在深水中游弋掀起的空灵水流回响'],
        'ice': ['极地深蓝冰海下独角鲸庞大身躯划破水流沉闷低频声', '窗外极光与浮冰泛起的深海静谧回音'],
    },
}


def _detect_material_category(material_text: str) -> str:
    """根据材质文本推断材质大类 (metal / stone / ice / default)。"""
    mat = (material_text or '').lower()
    if any(k in mat for k in ['钛合金', 'titanium', '碳钢', 'carbon steel', '碳纤维', 'carbon fiber', '金属', '合金', '钢构', 'rgb']):
        return 'metal'
    if any(k in mat for k in ['火山石', '玄武岩', 'basalt', '微水泥', 'cement', '石材', 'stone', 'rock']):
        return 'stone'
    if any(k in mat for k in ['冰', '极地', 'arctic', 'ice', 'permafrost', '雪', 'snow']):
        return 'ice'
    return 'default'


STAGE_OP_ALIAS = {
    'clearing': 'demolition',
    'demolition': 'demolition',
    'foundation': 'structural',
    'framing': 'structural',
    'structural': 'structural',
    'rough_in': 'rough_in',
    'plumbing': 'rough_in',
    'electrical': 'rough_in',
    'enclosure': 'enclosure',
    'insulation': 'enclosure',
    'glazing': 'enclosure',
    'surface': 'surface',
    'drywall': 'surface',
    'paint': 'surface',
    'floor': 'floor',
    'flooring': 'floor',
    'fixtures': 'fixtures',
    'furnishing': 'furnishing',
    'furniture': 'furnishing',
    'reveal': 'reveal',
    'reward': 'reveal',
}


def map_asmr_audio(operation_type: str, material: str = '', action: str = '') -> List[str]:
    """根据工序阶段与材质动态刷新物理 ASMR 音效映射。"""
    op = (operation_type or 'demolition').lower().strip()
    stage_key = STAGE_OP_ALIAS.get(op)
    if not stage_key:
        for k, v in STAGE_OP_ALIAS.items():
            if k in op:
                stage_key = v
                break
    if not stage_key:
        stage_key = 'demolition'

    mat_cat = _detect_material_category(material or action)
    stage_cues = _ASMR_CUES_MAP.get(stage_key, _ASMR_CUES_MAP['demolition'])
    cues = stage_cues.get(mat_cat) or stage_cues.get('default') or ['物理施工细节敲击声']
    return list(cues)


def apply_slot_replacement(text: str, mutation_axes: Dict[str, Any]) -> str:
    """对文本中的环境、材质、功用、生物进行正交槽位替换（支持通用母本词汇）。"""
    if not text or not isinstance(text, str):
        return ''

    env = mutation_axes.get('environment') or ''
    mat = mutation_axes.get('material') or ''
    func = mutation_axes.get('function') or ''
    hero = mutation_axes.get('hero_reveal') or ''

    mapping = {}
    if env:
        # 地貌环境词汇
        mapping[r'荒野河流泥岸|浑绿江水|河岸泥地|泥泞河滩|江水|河流泥岸|grassy riverbank|muddy riverbank|green river water|riverbank shoreline|river water|river shoreline|reddish-brown earth|brown loam|loose loam|compacted dirt|forest canopy|woodland clearing|tropical forest|natural soil ground|soil ground'] = env
        mapping[r'室外泥地|岸边坡地|水下岸边|河岸|江边|\bshoreline\b|\briverbank\b|\bclearing\b'] = f'{env}边缘'
    if mat:
        # 结构建筑材料与旧建筑
        mapping[r'蓝色钢构|废弃集装箱|沉水集装箱|集装箱钢板|集装箱波纹板|钢构舱体|shipping container|rusted container|\bcontainer\b'] = mat
        mapping[r'破旧木棚|旧木棚|木结构小屋|石木别墅|木屋|shack|wooden shack|dilapidated shack|cottage|masonry cottage|two-story cottage|concrete block cottage|villa'] = f'{mat}主体结构'
        mapping[r'混凝土砌块|空心砖|红砖|灰浆|砌块|砂浆|concrete masonry units|concrete masonry|concrete block|CMU|grey blockwork|mortar joints|mortar'] = f'{mat}砌体材料'
        mapping[r'松木过梁|实木屋架|人字木屋架|木檩条|timber lintel|timber trusses|rafter trusses|pine timber|pine rafter|roof purlins|timber balcony'] = f'{mat}高强度结构梁架'
        mapping[r'陶瓦|仿古瓦|屋顶瓦片|terracotta-style roof tiles|roof tiles|terracotta tiles'] = f'{mat}耐候顶层覆板'
        mapping[r'米黄抹灰|外墙灰浆|抹灰面层|beige stucco plaster|stucco plaster|beige stucco'] = f'{mat}防护面层'
        mapping[r'暖橡木板|实木地板|橡木地板|木质板材|wood paneling|wood panels|paneled interior|oak wood-grain flooring|light-oak wood'] = f'{mat}配套饰面板'
        mapping[r'条形暖光灯槽|家用日光灯|普通灯带|复古壁灯|carriage lantern|strip lights|warm lighting'] = f'{mat}专用隐形线型暖光灯槽'
    if func:
        # 空间功用与家具
        mapping[r'全景江景卧房|水下江景卧房|水下卧室|江景卧房|卧房|卧室|度假别墅|两层别墅|\bunderwater room\b|\bbedroom\b|\bliving room\b|\bcottage interior\b'] = func
        mapping[r'白棉麻床品|双人床|大床|实木餐桌|餐桌椅|L型橱柜|卫浴设施|\bbedding\b|\bbed\b|dining table|kitchen suite|cabinetry'] = f'{func}核心定制配置'
    if hero:
        # 终极生物与揭示
        mapping[r'野生淡水大鲟鱼|2米巨型野生淡水大鲟鱼|大鲟鱼|鲟鱼|淡水大鱼|wild sturgeon|large sturgeon|giant sturgeon|\bsturgeon\b'] = hero
        mapping[r'大鱼游弋|鱼群掠过|fish swimming'] = f'{hero}游弋'
        mapping[r'花园造景|石板小径|石板路|flagstone walkway|garden beds|flower shrubs'] = f'{hero}周边地貌'

    if not mapping:
        return text

    combined_pattern = re.compile('|'.join(f'({pat})' for pat in mapping.keys()), flags=re.IGNORECASE)
    replacements_list = list(mapping.values())

    def _sub_callback(match):
        for idx, val in enumerate(replacements_list):
            if match.group(idx + 1) is not None:
                return val
        return match.group(0)

    return combined_pattern.sub(_sub_callback, text)


def apply_trace_mapping(traces: Any, mutation_axes: Dict[str, Any]) -> List[str]:
    """将遗留痕迹 (persistent_traces) 根据新材质工艺进行正交映射。"""
    if not traces:
        return []
    if isinstance(traces, str):
        traces = [traces]
    
    out_traces = []
    mat = mutation_axes.get('material') or ''
    mat_cat = _detect_material_category(mat)

    for tr in traces:
        tr_str = str(tr).strip()
        if not tr_str:
            continue
        mapped = apply_slot_replacement(tr_str, mutation_axes)
        # 根据材质体系微调痕迹特征
        if mat_cat == 'metal' and ('水泥' in mapped or '砖' in mapped):
            mapped = mapped.replace('水泥', '碳纤维结构胶').replace('砖', '合金型材')
        elif mat_cat == 'stone' and ('钢板' in mapped or '铁锈' in mapped):
            mapped = mapped.replace('钢板', '玄武岩石板').replace('铁锈', '天然火山石孔隙压痕')
        out_traces.append(mapped)
        
    return out_traces if out_traces else [f'{mat or "主体"}结构安装锁紧留存压痕', f'{mat or "表面"}接缝密封防水胶打胶留存细线']


_LLM_MUTATE_BEATS_SYSTEM = """你是一位顶尖的纪录片级视觉短视频创意总监与极限空间建造设计师。
你的任务是将给定的「N 拍母本工序阶梯 (Beat Ladder)」严格重构为全新的「四轴正交二创变体阶梯」。

【必须严格遵守的铁律 (Strict Constraints)】：
1. 【1:1 节拍同构与因果锁死】：输入几拍就输出几拍（精确 N 拍，Beat 1 到 Beat N），每一拍的物理施工因果阶段（破拆清场 -> 地基基础 -> 结构龙骨 -> 封闭保温 -> 面层饰面 -> 软装设备 -> 终极揭示）必须与原拍 100% 拓扑对齐！
2. 【全量四轴正交置换】：
   - 轴 1 环境地貌 (Environment): 将原环境全部替换为目标新环境（如极地冻土、冰雪裂隙、荒漠红岩等）。
   - 轴 2 材质工艺 (Material): 将原建筑材质（如红砖、水泥砌块、普通原木等）全部替换为目标新材料（如耐寒炭化木、气凝胶保温层、耐候钢、火山玄武岩等）。
   - 轴 3 功能设施 (Function): 将空间功能与软装全部替换为目标新功能（如防寒壁炉、气闸保暖舱、恒温水窖等）。
   - 轴 4 终极奇观 (Hero Reveal): 将终拍的揭示物替换为目标终极生物/自然奇观。
3. 【拍摄尺度与活物继承】：
   - 若母本为微缩沙盘（Miniature Diorama / 巨人手 Craftsman Hands / 微缩人偶 Miniature Figurines），必须严格保留工匠手操作、微距景深与微缩人偶 living cast，但所有建造对象、工具和物料必须完全变成新材质！
   - 若母本为全尺寸真人施工，保留真人与对应工具。
4. 【去科幻与真实写实】：所有施工动作、工具与物料必须符合真实物理工序逻辑。

【输出格式】：
严格返回一个 JSON 数组，包含 N 个对象，对应 Beat 1 到 Beat N，无 Markdown 代码块，无多余文字：
[
  {
    "id": "B01",
    "visual_subject": "简练的一句话英文/中文视觉主体",
    "visible_action": "工人的具体施工动作与工具（如：工匠右手用微型铁镐破开极地冻土...）",
    "visible_result": "本拍完成后的物理交付状态",
    "state_before": "动工前的状态",
    "state_after": "完工后的状态",
    "visible_details": ["具体新材质1", "具体新材质2"],
    "persistent_traces": ["留存物理压痕/接缝胶线"]
  }
]"""


def _llm_mutate_beats(
    config: Optional[Dict[str, Any]],
    source_beats: List[Dict[str, Any]],
    effective_axes: Dict[str, Any],
    baseline_doc: Dict[str, Any],
    brief: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """调用大模型对全量节拍进行四轴正交智能重写。"""
    if not config:
        return None

    import prompt_pipeline as pp
    from prompt_pipeline.reverse import parse_json_reply

    beats_input = []
    for i, b in enumerate(source_beats):
        if not isinstance(b, dict):
            continue
        beats_input.append({
            'index': i + 1,
            'id': b.get('id') or f'B{i+1:02d}',
            'stage': b.get('stage') or b.get('operation_type') or 'construction',
            'operation': b.get('operation') or '',
            'visible_action': b.get('visible_action') or '',
            'visible_result': b.get('visible_result') or '',
            'state_before': b.get('state_before') or '',
            'state_after': b.get('state_after') or '',
            'visible_details': b.get('visible_details') or [],
            'persistent_traces': b.get('persistent_traces') or [],
            'space': b.get('space') or 'exterior',
        })

    carrier = baseline_doc.get('carrier') or baseline_doc.get('video_name') or '母本建筑'
    scene_sig = baseline_doc.get('scene_signature') or ''
    cast_id = baseline_doc.get('cast_identity') or []

    user_prompt = (
        f"【母本背景】\n"
        f"- 载体/项目名: {carrier}\n"
        f"- 原始场景特征: {scene_sig}\n"
        f"- 常驻演员/比例尺: {', '.join(str(c) for c in cast_id) if cast_id else '未特指'}\n"
        f"- 节拍总数: {len(beats_input)} 拍\n\n"
        f"【四轴正交目标设定 (Target 4-Axis Settings)】\n"
        f"- 轴 1 环境地貌 (Environment): {effective_axes.get('environment', '')}\n"
        f"- 轴 2 材质工艺 (Material): {effective_axes.get('material', '')}\n"
        f"- 轴 3 空间功能 (Function): {effective_axes.get('function', '')}\n"
        f"- 轴 4 终极揭示 (Hero Reveal): {effective_axes.get('hero_reveal', '')}\n"
        f"- 创作者补充指示 (Brief): {brief or '（无）'}\n\n"
        f"【母本 {len(beats_input)} 拍工序输入】\n"
        f"{json.dumps(beats_input, ensure_ascii=False, indent=2)}\n\n"
        f"请直接输出包含精确 {len(beats_input)} 拍正交重写结果的 JSON 数组。"
    )

    try:
        raw = pp._chat(
            config=config,
            system=_LLM_MUTATE_BEATS_SYSTEM,
            user=user_prompt,
            temperature=0.7,
            max_tokens=8192,
            timeout=90,
        )
        parsed = parse_json_reply(raw)
        if isinstance(parsed, list) and len(parsed) == len(source_beats):
            return parsed
    except Exception as e:
        if sys.stdout:
            print(f"[mutate] LLM 全节拍重写异常，平滑降级至规则槽位替换: {e}")
    return None


def generate_orthogonal_variant(
    baseline_doc: Dict[str, Any],
    mutation_axes: Optional[Dict[str, Any]] = None,
    preset: Optional[str] = None,
    brief: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """通过词槽正交映射与大模型智能重构生成二创变体，严格确保物理骨架零坍塌、零漂移。

    参数:
        baseline_doc: 母本節拍階梯文档 (timelapse_beats.json 或 job_state)
        mutation_axes: 包含 4 轴取值的字典 (environment, material, function, hero_reveal)
        preset: 预置名称 (polar / volcano / cyber / cave / custom)
        brief: 用户补充指示
        config: LLM 配置 (可选)

    返回:
        variant_beats_doc: 派生的变体節拍阶梯数据字典
    """
    if not baseline_doc:
        raise ValueError('母本節拍阶梯数据不能为空')

    source_beats = baseline_doc.get('beats') or baseline_doc.get('timelapse_beats') or []
    if not source_beats and isinstance(baseline_doc.get('beats'), list):
        source_beats = baseline_doc['beats']
    if not source_beats:
        raise ValueError('母本中未包含有效的節拍列表 (beats)')

    # 合并预置参数
    effective_axes = {}
    preset_data = MUTATION_PRESETS.get(preset or '') or {}
    if preset_data:
        effective_axes.update(preset_data.get('axes') or {})
    
    # 覆盖用户显式指定的轴
    if isinstance(mutation_axes, dict):
        for k, v in mutation_axes.items():
            if v:
                effective_axes[k] = str(v).strip()

    if not effective_axes:
        # 默认回落到极地深潜预置
        effective_axes.update(MUTATION_PRESETS['polar']['axes'])
        preset = 'polar'

    # 优先尝试大模型四轴正交智能重写
    llm_mutated_list = _llm_mutate_beats(
        config=config,
        source_beats=source_beats,
        effective_axes=effective_axes,
        baseline_doc=baseline_doc,
        brief=brief,
    )

    # 1. 骨架硬冻结 (Skeleton Freeze)
    # 节拍总数 N 恒定、相机焦段恒定、工序拓扑先后恒定、时间戳恒定
    variant_beats = []
    for idx, beat in enumerate(source_beats):
        v_beat = copy.deepcopy(beat)
        llm_beat = (llm_mutated_list[idx] if llm_mutated_list and idx < len(llm_mutated_list) and isinstance(llm_mutated_list[idx], dict) else None)
        
        # 1.1 继承硬约束
        v_beat['id'] = beat.get('id') or f'B{idx+1:02d}'
        v_beat['start'] = beat.get('start', beat.get('start_sec', 0.0))
        v_beat['end'] = beat.get('end', beat.get('end_sec', 0.0))
        v_beat['duration_sec'] = beat.get('duration_sec', round(float(v_beat['end'] - v_beat['start']), 3))
        v_beat['stage'] = beat.get('stage', beat.get('operation_type', 'demolition'))
        v_beat['operation'] = (llm_beat.get('operation') if llm_beat and llm_beat.get('operation') else beat.get('operation', v_beat['stage']))
        v_beat['space'] = beat.get('space', 'interior')
        v_beat['camera_framing'] = beat.get('camera_framing', '14mm ultra-wide, eye-level 1.6m, horizon 50%')
        v_beat['grid_anchors'] = beat.get('grid_anchors', 'Grid B2 (Center), Grid B1-B3 across horizontal')
        v_beat['workers_present'] = beat.get('workers_present', beat.get('workers', '1-2 workers in protective workwear'))

        # 1.2 证据帧降级为构图参考帧 (不复制原片事实)
        ev_frames = beat.get('evidence_frames') or beat.get('reference_frames') or []
        v_beat['reference_frames'] = list(ev_frames)
        v_beat.pop('evidence_frames', None)

        # 1.3 正交槽位注入替换 (若有 LLM 重写产物则优先使用，否则走规则替换)
        if llm_beat:
            v_beat['visual_subject'] = llm_beat.get('visual_subject') or apply_slot_replacement(beat.get('visual_subject', ''), effective_axes)
            v_beat['visible_action'] = llm_beat.get('visible_action') or apply_slot_replacement(beat.get('visible_action', ''), effective_axes)
            v_beat['visible_result'] = llm_beat.get('visible_result') or apply_slot_replacement(beat.get('visible_result', ''), effective_axes)
            v_beat['state_before'] = llm_beat.get('state_before') or apply_slot_replacement(beat.get('state_before', ''), effective_axes)
            v_beat['state_after'] = llm_beat.get('state_after') or apply_slot_replacement(beat.get('state_after', ''), effective_axes)
            v_beat['visible_details'] = llm_beat.get('visible_details') or [apply_slot_replacement(d, effective_axes) for d in (beat.get('visible_details') or [])]
            v_beat['persistent_traces'] = llm_beat.get('persistent_traces') or apply_trace_mapping(beat.get('persistent_traces', []), effective_axes)
        else:
            v_beat['visual_subject'] = apply_slot_replacement(
                beat.get('visual_subject', beat.get('visible_subject', '')),
                effective_axes
            )
            v_beat['visible_details'] = [
                apply_slot_replacement(d, effective_axes)
                for d in (beat.get('visible_details') or [])
            ]
            if not v_beat['visible_details']:
                v_beat['visible_details'] = [effective_axes.get('material', '高强度结构材料')]

            v_beat['visible_action'] = apply_slot_replacement(beat.get('visible_action', ''), effective_axes)
            v_beat['visible_result'] = apply_slot_replacement(beat.get('visible_result', ''), effective_axes)
            v_beat['state_before'] = apply_slot_replacement(beat.get('state_before', ''), effective_axes)
            v_beat['state_after'] = apply_slot_replacement(beat.get('state_after', ''), effective_axes)
            
            # 1.4 遗留痕迹映射
            v_beat['persistent_traces'] = apply_trace_mapping(
                beat.get('persistent_traces', []),
                effective_axes
            )

        # 1.5 动态刷新 ASMR 音效特征
        v_beat['sfx'] = map_asmr_audio(
            v_beat['stage'],
            effective_axes.get('material', ''),
            v_beat['visible_action']
        )
        v_beat['audio_asmr_cues'] = list(v_beat['sfx'])

        # 1.6 清空过期的中文对照
        v_beat['zh'] = {}

        variant_beats.append(v_beat)

    # 2. 场景签名与禁用元素重算 (绝不继承母本旧场景特异物)
    scene_sig = (
        preset_data.get('scene_signature')
        or f"A modern {effective_axes.get('material', 'composite')} structure located in {effective_axes.get('environment', 'wilderness')}, serving as a {effective_axes.get('function', 'sanctuary')}."
    )
    banned_elems = list(preset_data.get('banned_elements') or [
        'tripod left in frame', 'power cables after furnishing', 'wet high-gloss mirror floor reflection',
        'fluorescent zigzag lights', 'missing ceilings', 'random floating objects',
    ])

    pipeline_id = baseline_doc.get('job_id') or baseline_doc.get('pipeline_id') or 'baseline'

    _baseline_constants = baseline_doc.get('scene_constants')
    _baseline_constants = _baseline_constants if isinstance(_baseline_constants, dict) else {}
    baseline_carry = {}
    for _key in ('cast', 'grade'):
        _items = [str(x).strip() for x in (_baseline_constants.get(_key) or []) if str(x).strip()]
        if _items:
            baseline_carry[_key] = _items
    variant_doc = {
        'pipeline_id': pipeline_id,
        'variant_of': pipeline_id,
        'job_type': 'variant',
        'is_locked_baseline': False,
        'selected_preset': preset or 'custom',
        'mutation_axes': effective_axes,
        'mutation_brief': brief or '',
        'video_duration_sec': baseline_doc.get('video_duration_sec', 0.0),
        'source_video': baseline_doc.get('source_video', baseline_doc.get('video_name', '')),
        'scene_signature': scene_sig,
        'banned_elements': banned_elems,
        'scene_constants': dict(baseline_carry),
        'beats': variant_beats,
        'validation': [],
    }

    return variant_doc


_AI_DIVERGE_SYSTEM = """你是一位顶尖的纪录片级视觉短视频创意总监与真实极限空间建造设计师（精通 TikTok 爆款叙事心理学、情绪价值曲线与完播率留存钩子）。
你的任务是根据给定的「1:1 黄金母本延时改造视频」的工序骨架和节奏，在确保物理工序拓扑与分镜完全可复用的前提下，进行四轴正交创意发散（AI Orthogonal Mutation），构思出具备【TikTok 爆款叙事灵魂、有血有肉的情感闭环、黄金 3 秒视觉钩子、极致前后反差】，且【100% 真实写实】的【硬核生存庇护所 / 极限避难所 / 微缩神迹工坊 / 治愈系庇护豪宅】新创意方案。

★★★★★ TIKTOK VIRAL NARRATIVE & EMOTION POLICY（爆款叙事与情绪价值准则 — 绝不生成表面无血无肉的冰冷工具展示）：
1. 黄金 3 秒痛点钩子 (Hook Inception): 绝不平淡开场！每一方案开局必须带有“完全毁坏破烂屋子/暴风雨冲垮废墟/流离失所绝境”的极端视觉痛点，瞬间锁住观众前 3 秒！
2. 常驻角色与情感弧线 (Character Stakes & Emotional Arc): 若母本含有角色线（如穷困潦倒夫妇人偶、受助弱小生命），二创方案必须继承“展示设计图纸点燃希望 ➔ 全程满怀期盼注视 ➔ 完工喜极而泣搬入奢华新居”的有血有肉情感链条！
3. 神来之手与降维奇观 (God-Hand Wonder): 若为微缩沙盘题材，保留巨人工匠之手（God Hand）如神迹降临微观世界的宏微视角反差与治愈感！
4. 极致前后反差与多巴胺终局 (Extreme Contrast): 牢牢守住“从 0 分破烂废墟到 100 分奢华庄园/温暖宫殿”的强烈蜕变反差！

★★★★★ REALISM-ONLY POLICY（去科幻 / 严格真实写实硬约束 — 违反直接废弃）：
1. 严禁任何科幻、未来机能、太空宇航、外星异星、赛博朋克、全息投影、RGB霓虹灯带、发光科技面板、失重力、虚构魔法或超自然发光生物！
2. 严禁千篇一律的“小资网红温馨民宿/酒店样板间/咖啡馆/浪漫串灯/反光塑料地板”套路！
3. 所有方案必须是【现实世界中可以真实施工建造的】极限庇护所、防灾掩体、隐蔽哨所、微缩手工庄园或自持空间改造，具有强烈的工匠手作质感、硬核工程防护逻辑、真实物料触感与自然地理险境美感。
4. 四大正交发散轴：
   - 轴 1 地貌险境与水体环境 (Environment & Biome): 真实自然地理极端微气候与环境威胁（如：极地厚积雪冻土裂隙暴风雪、火山熔岩地热热泉与硫磺有毒蒸气、热带雨林红树林季风暴雨洪泛、悬崖岩穴石灰岩溶洞暗河、干旱荒漠红土特大沙尘暴、海边海蚀崖暗礁浪涌）。
   - 轴 2 结构材质与防护工艺 (Material & Craft): 真实硬核建筑与防护施工材料（如：粗犷芬兰松木原木 + 气凝胶保温层、哑光黑耐候钢构件 + 黑色玄武岩打磨、缅甸老柚木防腐榫卯 + 悬空防洪毛石基座、粗糙天然玄武岩 + 黄铜暗埋构件、传统生土夯土厚墙 + 粗壮胡杨木大梁 + 重型防沙重力门、双层中空防爆夹胶钢化玻璃）。严禁碳纤维RGB、太空合金、全息舱。
   - 轴 3 庇护所维生功能与硬核软装 (Shelter Function & Survival Systems): 真实自给自足维生与避险设施（如：极地防寒壁炉与气闸保暖舱、地热温差发电组与硫磺过滤新风塔、悬空防洪木作台与雨水多级净化槽、地下恒温水窖与气密粮仓、荒漠太阳能冷凝集水与防沙地下地堡、隐蔽工作台与防潮储物架）。严禁电竞舱、科研实验舱、太空观测台。
   - 轴 4 终极生物/自然奇观揭示 (Hero Creature / Reveal): 真实自然生态野生动物或宏大自然水景（如：4米野生北极白鲸/独角鲸/极地北极熊、火山熔岩流与清澈溪流高山红点鲑、热带巨骨舌鱼/水豚/黑凯门鳄、溶洞地下暗河岩斑盲鱼群、荒漠绿洲清泉野生双峰驼、高山雪豹）。严禁发光生物、异星怪兽、赛博机械兽。

硬性约束：
- 构思 {count} 组地貌环境与材质工艺完全正交、绝不撞车、极具视觉冲击力与极限生存反差感的写实庇护所方案。
- 方案必须符合工序施工的可视化逻辑（破拆 -> 结构 -> 隐蔽 -> 封板 -> 面层 -> 地面 -> 设备 -> 软装 -> 揭示），充满质感与具象细节，严禁空洞虚浮的抽象词汇。
- 若用户提供了发散方向（User Direction/Brief），必须紧密围绕该方向深度发散出不同层次的方案（若方向中带有科幻词汇，必须将其转化为现实写实建造对应物）；若未提供，则自由发散最吸睛、反差感最强的爆款写实庇护所方向。
{trend_guidance}

输出格式：
严格返回一个 JSON 数组（无任何 Markdown 代码块，无额外废话），包含 {count} 个对象，每个对象结构如下：
[
  {{
    "id": "shelter_unique_id",
    "name": "中文庇护所主题名 (如：极地防风雪避险庇护所)",
    "icon": "❄️",
    "hook": "一句话爆款卖点（20字以内，包含痛点钩子与终极反差，如：废墟破屋开局，神之手打造极地松木豪宅与白鲸）",
    "trend_ref": "说明借鉴了哪条联网参考的哪个要点（如：借鉴极地避难所的双层气凝胶保温木结构与防风雪观察窗）",
    "axes": {{
      "environment": "具体地貌险境与水体环境中文描述",
      "material": "具体结构材质与防护工艺中文描述",
      "function": "具体庇护所维生功能与硬核软装中文描述",
      "hero_reveal": "具体终极生物或事件揭示中文描述"
    }},
    "scene_signature": "One concise English sentence summarizing the final shelter structure, survival materials, and environment.",
    "banned_elements": ["sci-fi", "cyberpunk", "neon", "glowing tech", "spaceship", "alien", "generic cozy homestay", "luxury hotel", "glowing fairy lights", "cheap glossy floor"]
  }}
]"""


def _generate_fallback_ideas(
    baseline_doc: Optional[Dict[str, Any]] = None,
    brief: Optional[str] = None,
    count: int = 4,
    trend_refs: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """当 LLM 不可用或解析失败时的丰富动态写实兜底方案（自适应微缩沙盘与成人庇护所题材）。"""
    ref_ids = [r['id'] for r in (trend_refs or []) if isinstance(r, dict) and r.get('id')]
    
    # 检测母本题材特征
    base_text = ""
    if baseline_doc:
        base_text = f"{baseline_doc.get('title', '')} {baseline_doc.get('video_name', '')} {baseline_doc.get('scene_signature', '')}"
        for b in (baseline_doc.get('beats') or []):
            if isinstance(b, dict):
                base_text += f" {b.get('visual_subject', '')} {b.get('visible_action', '')}"
    base_lower = base_text.lower()
    is_miniature = any(k in base_lower for k in ['微缩', '沙盘', '手作', '人偶', 'miniature', 'diorama', 'tabletop'])
    has_couple = any(k in base_lower for k in ['夫妇', '夫妻', '穷困', '人偶', '看图纸', 'couple', 'poor', 'homeless'])

    if is_miniature or has_couple:
        # 微缩沙盘与常驻夫妇情感题材专属高分兜底方案
        defaults = [
            {
                'id': 'miniature_polar_hearth',
                'name': '微缩极地暖炉庄园',
                'icon': '❄️',
                'hook': '完全毁坏破烂屋子开局，神来之手为穷困夫妇看设计图纸并精雕极地松木豪宅',
                'trend_ref': '借鉴极地避难所的双层气凝胶保温木结构与全景防风雪观察窗',
                'trend_ref_ids': ref_ids,
                'axes': {
                    'environment': '微缩极地雪山微观苔原与清澈冰湖桌面沙盘',
                    'material': '微缩芬兰松木原木 + 迷你微型壁炉 + 微雕石膏罗马柱 + 侘寂微水泥',
                    'function': '微缩极地暖炉避难庄园 + 夫妻人偶双人卧室与微型书房',
                    'hero_reveal': '暖光点亮，夫妻俩喜极而泣搬入奢华卧室并向巨手致谢',
                },
                'scene_signature': 'A handcrafted miniature heavy timber polar refuge villa diorama crafted by giant god hand for a homeless figurine couple.',
                'banned_elements': ['adult full scale', 'sci-fi capsule', 'cyberpunk', 'neon lights', 'spaceship', 'hologram', 'muddy riverbank', 'generic cozy homestay'],
            },
            {
                'id': 'miniature_volcano_spa',
                'name': '微缩火山地热庄园',
                'icon': '🌋',
                'hook': '废墟破屋开局，巨人之手为穷困夫妇看图纸并手工精雕微缩地热黑石庄园',
                'trend_ref': '借鉴火山地热自持庇护所的黑色玄武岩打磨与地热温差发电系统',
                'trend_ref_ids': ref_ids,
                'axes': {
                    'environment': '微缩火山黑石与微观温泉冒泡水池沙盘',
                    'material': '微缩黑色玄武岩微雕 + 迷你紫铜导热管 + 碳化原木构件',
                    'function': '微缩恒温地热疗愈庄园 + 夫妻人偶微型温泉泡池与实木地台',
                    'hero_reveal': '夫妻俩在微型温泉边欢呼拥抱，巨手在屋顶安放微型路灯',
                },
                'scene_signature': 'A miniature matte black basalt and copper hot spring spa villa diorama crafted for a homeless couple.',
                'banned_elements': ['adult full scale', 'sci-fi', 'futuristic', 'neon channels', 'glowing tech', 'iceberg', 'snowfield', 'generic cozy homestay'],
            },
            {
                'id': 'miniature_zen_courtyard',
                'name': '微缩京都枯山水庄园',
                'icon': '🪵',
                'hook': '暴雨冲垮残破草棚开局，神来之手打造微缩日式枯山水原木奢华庄园',
                'trend_ref': '借鉴日式枯山水原木榫卯与微观苔藓青石微雕工法',
                'trend_ref_ids': ref_ids,
                'axes': {
                    'environment': '微缩白砂枯山水与微观苔藓青石沙盘',
                    'material': '微缩缅甸老柚木榫卯 + 迷你日式白墙 + 微型黑瓦构件',
                    'function': '微缩日式禅意豪宅 + 夫妻俩榻榻米茶室与观景推拉门',
                    'hero_reveal': '夫妻人偶携手步入微型庭院，微型灯笼点亮，喜极而泣',
                },
                'scene_signature': 'A handcrafted miniature Japanese Zen courtyard villa diorama for a figurine couple.',
                'banned_elements': ['adult full scale', 'sci-fi', 'cyberpunk', 'neon lights', 'rgb lighting', 'carbon fiber', 'generic cozy homestay'],
            },
            {
                'id': 'miniature_desert_oasis',
                'name': '微缩荒漠绿洲庄园',
                'icon': '🏜️',
                'hook': '暴风沙摧毁破屋开局，神来之手为夫妇手工打造微缩夯土绿洲豪宅',
                'trend_ref': '借鉴传统生土夯土墙的蓄热调温与重型防沙微型重力门结构',
                'trend_ref_ids': ref_ids,
                'axes': {
                    'environment': '微缩干旱红土与绿洲微观清泉沙盘',
                    'material': '微缩生土夯土墙 + 迷你胡杨木梁 + 陶土微砖',
                    'function': '微缩绿洲防沙暴豪宅 + 穹顶自然采光与迷你双人亚麻地台',
                    'hero_reveal': '清泉边微型双峰驼饮水，夫妻人偶幸福入住新居',
                },
                'scene_signature': 'A miniature monolithic rammed-earth and desert timber oasis villa diorama crafted by giant god hand.',
                'banned_elements': ['adult full scale', 'sci-fi', 'futuristic', 'neon', 'metal spacecraft', 'high-tech panels', 'generic cozy homestay'],
            },
        ]
    else:
        # 成人硬核写实生存庇护所高分方案
        defaults = [
            {
                'id': 'polar_cabin',
                'name': '极地防风雪避险所',
                'icon': '❄️',
                'hook': '完全毁坏破烂木屋开局，打造冰封峡湾松木防寒避难所与白鲸',
                'trend_ref': '借鉴极地避难所的双层气凝胶保温木结构与全景防风雪观察窗',
                'trend_ref_ids': ref_ids,
                'axes': {
                    'environment': '极地厚积雪地貌与剔透深蓝峡湾海面',
                    'material': '粗犷芬兰松木原木 + 气凝胶保温层 + 侘寂微水泥 + 黑色碳化防腐木',
                    'function': '防风雪极地避险庇护所 + 粗石壁炉与防寒羽绒保暖卧榻',
                    'hero_reveal': '野生北极白鲸群在窗外深蓝峡湾中缓缓掠过',
                },
                'scene_signature': 'A heavy timber, aerogel insulated and stone refuge shelter cabin embedded in arctic snow, overlooking clear deep-blue fjord waters.',
                'banned_elements': ['sci-fi capsule', 'cyberpunk', 'neon lights', 'spaceship', 'hologram', 'muddy riverbank', 'generic cozy homestay'],
            },
            {
                'id': 'volcanic_spa',
                'name': '火山地热自持所',
                'icon': '🌋',
                'hook': '残破废墟开局，火山黑石地热自持避险所与高山溪红点鲑群',
                'trend_ref': '借鉴火山地热自持庇护所的黑色玄武岩打磨与地热温差发电系统',
                'trend_ref_ids': ref_ids,
                'axes': {
                    'environment': '火山热泉泥地与冒泡地热蒸气水体',
                    'material': '哑光黑耐候钢构件 + 黑色玄武岩打磨 + 导热紫铜管 + 碳化原木',
                    'function': '恒温地热自持能源庇护所 + 地热温差发电与悬浮实木榻榻米',
                    'hero_reveal': '地热温泉清澈水体中高山溪红点鲑群缓缓游弋',
                },
                'scene_signature': 'A matte black weathering-steel geothermal energy shelter pavilion nestled in volcanic hot spring terrain with porous black basalt stone walls.',
                'banned_elements': ['sci-fi', 'futuristic', 'neon channels', 'glowing tech', 'iceberg', 'snowfield', 'generic cozy homestay'],
            },
            {
                'id': 'rainforest_workshop',
                'name': '雨林防洪重工所',
                'icon': '🪵',
                'hook': '暴雨冲垮残破草棚开局，热带雨林悬空防洪浮水工坊与水豚',
                'trend_ref': '借鉴雨林水上高脚屋的老柚木防腐榫卯与离地悬空防洪排湿结构',
                'trend_ref_ids': ref_ids,
                'axes': {
                    'environment': '暴雨热带雨林与原生态清澈溪流水域',
                    'material': '缅甸老柚木防腐结构 + 悬空抬升毛石基座 + 铜质暗扣',
                    'function': '悬空防洪木工庇护所 + 手工皮革制作台与防潮原木搁架',
                    'hero_reveal': '窗外清澈水流中野生水豚与巨骨舌鱼缓缓游弋',
                },
                'scene_signature': 'A handcrafted reclaimed teak wood and rough stone flood-refuge craftsman stilt shelter submerged along a tropical rainforest river.',
                'banned_elements': ['sci-fi', 'cyberpunk', 'neon lights', 'rgb lighting', 'carbon fiber', 'hologram', 'generic cozy homestay'],
            },
            {
                'id': 'desert_sanctuary',
                'name': '荒漠防沙暴地堡',
                'icon': '🏜️',
                'hook': '特大沙尘暴摧毁破屋开局，荒漠红土夯土防风暴地堡与双峰驼',
                'trend_ref': '借鉴传统生土夯土墙的蓄热调温与重型防沙重力门结构',
                'trend_ref_ids': ref_ids,
                'axes': {
                    'environment': '干旱荒漠红土与绿洲清泉水体',
                    'material': '传统生土夯土墙 + 粗壮胡杨木大梁 + 透气陶土砖 + 防沙重力门',
                    'function': '绿洲地下防沙暴储能庇护所 + 穹顶自然采光与亚麻地台',
                    'hero_reveal': '绿洲清泉边野生双峰驼低头静静饮水',
                },
                'scene_signature': 'A monolithic rammed-earth and desert timber underground sandstorm refuge shelter integrated into desert oasis terrain with deep shaded openings.',
                'banned_elements': ['sci-fi', 'futuristic', 'neon', 'metal spacecraft', 'high-tech panels', 'generic cozy homestay'],
            },
        ]
    return defaults[:count]


def ai_diverge_orthogonal_ideas(
    config: Optional[Dict[str, Any]],
    baseline_doc: Dict[str, Any],
    brief: Optional[str] = None,
    count: int = 4,
    on_progress: Optional[Any] = None,
    trend_ref_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """调用大模型为黄金母本结合联网参考智能发散四轴正交二创创意方案（自适应母本叙事与拓扑）。"""
    import prompt_pipeline as pp
    from prompt_pipeline.reverse import parse_json_reply

    source_beats = baseline_doc.get('beats') or baseline_doc.get('timelapse_beats') or []
    if isinstance(source_beats, dict) and 'beats' in source_beats:
        source_beats = source_beats['beats']

    beats_summary = []
    for b in (source_beats or [])[:12]:
        if isinstance(b, dict):
            beats_summary.append({
                'id': b.get('id'),
                'stage': b.get('stage') or b.get('operation_type'),
                'operation': b.get('operation') or b.get('visual_subject'),
                'visible_action': b.get('visible_action'),
                'visible_result': b.get('visible_result'),
            })

    # 读取并处理联网参考案例
    selected_refs = []
    if trend_ref_ids:
        stored = pp.load_trend_refs() or []
        by_id = {e.get('id'): e for e in stored if isinstance(e, dict)}
        selected_refs = [by_id[i] for i in trend_ref_ids if i in by_id]

    if not selected_refs:
        stored = pp.load_trend_refs() or []
        if stored:
            try:
                picked = pp._pick_auto_trend_ref(stored)
                if picked:
                    selected_refs = [picked]
            except Exception:
                selected_refs = [stored[0]]

    trend_guidance = ""
    trend_block = ""
    if selected_refs:
        trend_refs_summary = [{
            'id': e.get('id'),
            'label': e.get('label', ''),
            'text': e.get('text', ''),
            'source': e.get('source', '')
        } for e in selected_refs]
        ref_lines = [f"• [{r['label']}]\n  {r['text']}" for r in trend_refs_summary]
        trend_block = "【联网爆款参考 / 热门趋势取材 (Trending Viral References)】\n" + "\n".join(ref_lines) + "\n\n"
        trend_guidance = (
            "- 联网参考深度取材：本批方案必须深度汲取并重构下方【联网爆款参考】中的前沿爆款元素"
            "（如特色材质体系、惊艳外景地貌、独特空间玩法或反差揭示物），"
            "并在每个方案的 trend_ref 字段中简明扼要说明具体借鉴了哪条参考的哪个要点（中文20字以内）。"
        )

    # 提取母本题材与叙事特征
    base_title = str(baseline_doc.get('title') or baseline_doc.get('video_name') or '')
    base_scene_sig = str(baseline_doc.get('scene_signature') or '')
    base_corpus = f"{base_title} {base_scene_sig}"
    for b in (source_beats or []):
        if isinstance(b, dict):
            base_corpus += f" {b.get('visual_subject', '')} {b.get('visible_action', '')}"
    base_lower = base_corpus.lower()

    is_miniature = any(k in base_lower for k in ['微缩', '沙盘', '手作', '人偶', 'miniature', 'diorama', 'tabletop'])
    has_couple = any(k in base_lower for k in ['夫妇', '夫妻', '穷困', '人偶', '看图纸', 'couple', 'poor', 'homeless'])
    has_god_hand = any(k in base_lower for k in ['神来之手', '巨手', '巨人之手', 'god hand', 'giant hand']) or is_miniature
    has_ruin_hook = any(k in base_lower for k in ['破烂', '完全毁坏', '破烂屋子', '废墟', '残破', 'ruin', 'dilapidated', 'destroyed'])

    narrative_directives = []
    if is_miniature:
        narrative_directives.append("• 【题材约束·微缩沙盘微距手作】：母本为微缩沙盘模型题材，发散方案必须是微缩沙盘/微观手工微雕（Miniature Diorama），严禁写成真人 1.78m 成人建筑施工！")
    if has_god_hand or is_miniature:
        narrative_directives.append("• 【视角约束·神来之手介入】：必须在 hook、axes 中体现巨人工匠之手（God Hand）如神迹降临微缩世界、微距特写精雕的降维神性奇观！")
    if has_couple:
        narrative_directives.append("• 【叙事核心·常驻人偶夫妇情感闭环】：必须严格保留‘为穷困潦倒的夫妻人偶看设计图纸并帮他们打造奢华豪宅’的有血有肉情感线，体现从绝望破屋到喜极而泣搬入新居！")
    if has_ruin_hook or is_miniature:
        narrative_directives.append("• 【开局钩子·黄金3秒极端破败】：方案必须以‘完全毁坏/暴风雨冲垮的废墟破屋’开局，牢牢锁住前3秒完播率！")

    narrative_block = ""
    if narrative_directives:
        narrative_block = "【黄金母本叙事与题材硬性约束 (Narrative Invariants - 必须 100% 继承)】\n" + "\n".join(narrative_directives) + "\n\n"

    user_prompt = (
        f"【黄金母本背景】\n"
        f"- 视频标题/主题：{base_title}\n"
        f"- 节拍总数：{len(source_beats)} 拍\n"
        f"- 场景特征定义：{base_scene_sig or '极限空间改造'}\n"
        f"- 工序阶梯流转：\n{json.dumps(beats_summary, ensure_ascii=False, indent=2)}\n\n"
        f"{narrative_block}"
        f"{trend_block}"
        f"【创作者发散偏好】\n"
        f"{brief.strip() if brief and brief.strip() else '（未指定特定风格，请自由发散最吸睛、反差感极强的 4 种高网感爆款主题）'}\n\n"
        f"请基于以上母本工序骨架并深度结合联网参考与叙事约束，直接输出包含 {count} 个正交方案的 JSON 数组。"
    )

    all_ref_ids = [r['id'] for r in selected_refs if isinstance(r, dict) and r.get('id')]

    try:
        raw = pp._chat(
            config=config or {},
            system=_AI_DIVERGE_SYSTEM.format(count=count, trend_guidance=trend_guidance),
            user=user_prompt,
            temperature=0.85,
            max_tokens=4096,
            timeout=60,
        )
        data = parse_json_reply(raw)
        if isinstance(data, list) and len(data) > 0:
            from .decision_framework import evaluate_variant_compatibility
            cleaned_ideas = []
            for idx, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                axes = item.get('axes') or {}
                if not axes.get('environment') and not axes.get('material'):
                    continue
                
                env_text = str(axes.get('environment') or '')
                mat_text = str(axes.get('material') or '')
                func_text = str(axes.get('function') or '')
                hero_text = str(axes.get('hero_reveal') or '')
                hook_text = str(item.get('hook') or '')
                name_text = str(item.get('name') or f'创意方案 {idx+1}')

                # 自动保证母本核心题材前缀不丢失
                if is_miniature and not any(k in env_text for k in ['微缩', '沙盘', 'miniature', 'diorama']):
                    env_text = f"微缩{env_text}沙盘"
                if is_miniature and not any(k in mat_text for k in ['微缩', '迷你', '微雕']):
                    mat_text = f"微缩{mat_text}"

                axes_dict = {
                    'environment': env_text,
                    'material': mat_text,
                    'function': func_text,
                    'hero_reveal': hero_text,
                }
                idea_entry = {
                    'id': str(item.get('id') or f'idea_{idx+1}'),
                    'name': name_text,
                    'icon': str(item.get('icon') or '✨'),
                    'hook': hook_text,
                    'trend_ref': str(item.get('trend_ref') or (selected_refs[idx % len(selected_refs)].get('label') if selected_refs else '')),
                    'trend_ref_ids': all_ref_ids,
                    'axes': axes_dict,
                    'scene_signature': str(item.get('scene_signature') or ''),
                    'banned_elements': list(item.get('banned_elements') or []),
                }
                try:
                    compat = evaluate_variant_compatibility(baseline_doc, mutation_axes=axes_dict, brief=brief, idea=idea_entry)
                    idea_entry['compatibility'] = compat
                except Exception as _ce:
                    print(f"[mutate] 兼容性评估警告: {_ce}")
                cleaned_ideas.append(idea_entry)
            if cleaned_ideas:
                return cleaned_ideas[:count]
    except Exception as e:
        print(f"[mutate] AI 发散模型调用或解析异常，采用高质量自适应兜底方案: {e}")

    fallback_ideas = _generate_fallback_ideas(baseline_doc=baseline_doc, brief=brief, count=count, trend_refs=selected_refs)
    try:
        from .decision_framework import evaluate_variant_compatibility
        for idea_entry in fallback_ideas:
            idea_entry['compatibility'] = evaluate_variant_compatibility(
                baseline_doc,
                mutation_axes=idea_entry.get('axes'),
                brief=brief,
                idea=idea_entry
            )
    except Exception:
        pass
    return fallback_ideas



