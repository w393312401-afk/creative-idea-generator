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
        'name': '极地雪屋 (Polar Fjord Cabin)',
        'description': '极地厚积雪 + 芬兰松木原木 + 防风雪观景木屋 + 野生北极白鲸',
        'axes': {
            'environment': '极地厚积雪地貌与剔透深蓝峡湾海面',
            'material': '粗犷芬兰松木原木 + 侘寂微水泥 + 黑色碳化防腐木',
            'function': '防风雪极地观景木屋 + 粗石壁炉与羊毛卧榻',
            'hero_reveal': '野生北极白鲸群在窗外深蓝峡湾中缓缓掠过',
        },
        'scene_signature': 'A heavy timber and stone insulated refuge cabin embedded in arctic snow, overlooking clear deep-blue fjord waters.',
        'banned_elements': ['sci-fi capsule', 'cyberpunk', 'neon lights', 'spaceship', 'hologram', 'muddy riverbank', 'green river water', 'rusted mild steel container', 'warm yellow household lighting'],
    },
    'volcano': {
        'key': 'volcano',
        'name': '火山私汤 (Volcanic Hot Spring)',
        'description': '火山热泉泥地 + 哑光黑碳钢 + 恒温天然私汤茶室 + 高山溪红点鲑群',
        'axes': {
            'environment': '火山热泉泥地与冒泡地热蒸气水体',
            'material': '哑光黑碳钢构件 + 黑色玄武岩打磨 + 碳化原木',
            'function': '恒温天然私汤茶室 + 悬浮实木榻榻米',
            'hero_reveal': '地热温泉清澈水体中高山溪红点鲑群缓缓游弋',
        },
        'scene_signature': 'A matte black carbon-steel geothermal bath pavilion nestled in volcanic hot spring terrain with porous black basalt stone walls.',
        'banned_elements': ['sci-fi', 'futuristic', 'neon channels', 'glowing tech', 'iceberg', 'snowfield', 'office desk', 'bright fluorescent tube', 'muddy green river'],
    },
    'rainforest': {
        'key': 'rainforest',
        'name': '雨林工坊 (Rainforest Stilt Workshop)',
        'description': '热带雨林水域 + 老柚木防腐榫卯 + 浮水木工坊 + 窗外野生水豚与巨骨舌鱼',
        'axes': {
            'environment': '暴雨热带雨林与原生态清澈溪流水域',
            'material': '缅甸老柚木防腐结构 + 粗糙毛石基座 + 铜质暗扣',
            'function': '实木榫卯木工坊 + 手工皮革制作台与原木搁架',
            'hero_reveal': '窗外清澈水流中野生水豚与巨骨舌鱼缓缓游弋',
        },
        'scene_signature': 'A handcrafted reclaimed teak wood and rough stone craftsman stilt pavilion submerged along a tropical rainforest river.',
        'banned_elements': ['sci-fi', 'cyberpunk', 'neon lights', 'rgb lighting', 'carbon fiber', 'hologram', 'polar bear', 'wooden cabin', 'tatami', 'hot spring steam', 'rustic bamboo'],
    },
    'cave': {
        'key': 'cave',
        'name': '崖壁石窖 (Cliff Karst Den)',
        'description': '天然石灰岩溶洞 + 玄武岩黄铜 + 恒温酒窖冥想室 + 地下暗河野生盲鱼群',
        'axes': {
            'environment': '天然石灰岩溶洞石壁与清冽地下暗河',
            'material': '粗糙玄武岩打磨 + 黄铜暗埋构件 + 老橡木实木梁',
            'function': '隐秘溶洞恒温酒窖与茶歇冥想室',
            'hero_reveal': '溶洞地下清冽暗河中野生岩斑盲鱼群缓缓游弋',
        },
        'scene_signature': 'A subterranean basalt and brass sanctuary built inside a natural karst cave alongside a clear underground river.',
        'banned_elements': ['sci-fi', 'futuristic', 'crystal fantasy', 'glowing crystals', 'neon', 'sunlight', 'skyline', 'traffic noise', 'plastic panels', 'white drywalls'],
    },
    'desert': {
        'key': 'desert',
        'name': '荒漠隐庐 (Desert Rammed-Earth Retreat)',
        'description': '干旱荒漠红土 + 传统夯土墙胡杨木 + 穹顶采光茶室 + 绿洲清泉双峰驼',
        'axes': {
            'environment': '干旱荒漠红土与绿洲清泉水体',
            'material': '传统生土夯土墙 + 粗壮胡杨木大梁 + 透气陶土砖',
            'function': '绿洲避暑茶室 + 穹顶自然采光与亚麻地台',
            'hero_reveal': '绿洲清泉边野生双峰驼低头静静饮水',
        },
        'scene_signature': 'A monolithic rammed-earth and desert timber pavilion integrated into desert oasis terrain with deep shaded openings.',
        'banned_elements': ['sci-fi', 'futuristic', 'neon', 'metal spacecraft', 'high-tech panels', 'blizzard', 'glacier'],
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
    """对文本中的环境、材质、功用、生物进行正交槽位替换。"""
    if not text or not isinstance(text, str):
        return ''

    env = mutation_axes.get('environment') or ''
    mat = mutation_axes.get('material') or ''
    func = mutation_axes.get('function') or ''
    hero = mutation_axes.get('hero_reveal') or ''

    # 替换规则映射：将母本常见词槽置换为目标正交词槽 (单遍扫描，防止递归嵌套污染)
    mapping = {}
    if env:
        mapping[r'荒野河流泥岸|浑绿江水|河岸泥地|泥泞河滩|江水|河流泥岸|grassy riverbank|muddy riverbank|green river water|riverbank shoreline|river water|river shoreline'] = env
        mapping[r'室外泥地|岸边坡地|水下岸边|河岸|江边|\bshoreline\b|\briverbank\b'] = f'{env}边缘'
    if mat:
        mapping[r'蓝色钢构|废弃集装箱|沉水集装箱|集装箱钢板|集装箱波纹板|钢构舱体|shipping container|rusted container|\bcontainer\b'] = mat
        mapping[r'暖橡木板|实木地板|橡木地板|木质板材|wood paneling|wood panels|paneled interior'] = f'{mat}配套饰面板'
        mapping[r'条形暖光灯槽|家用日光灯|普通灯带|strip lights|warm lighting'] = f'{mat}专用隐形线型暖光灯槽'
    if func:
        mapping[r'全景江景卧房|水下江景卧房|水下卧室|江景卧房|卧房|卧室|\bunderwater room\b|\bbedroom\b|\bliving room\b'] = func
        mapping[r'白棉麻床品|双人床|大床|\bbedding\b|\bbed\b'] = f'{func}核心定制配置'
    if hero:
        mapping[r'野生淡水大鲟鱼|2米巨型野生淡水大鲟鱼|大鲟鱼|鲟鱼|淡水大鱼|wild sturgeon|large sturgeon|giant sturgeon|\bsturgeon\b'] = hero
        mapping[r'大鱼游弋|鱼群掠过|fish swimming'] = f'{hero}游弋'

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


def generate_orthogonal_variant(
    baseline_doc: Dict[str, Any],
    mutation_axes: Optional[Dict[str, Any]] = None,
    preset: Optional[str] = None,
    brief: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """通过词槽正交映射生成二创变体，严格确保物理骨架零坍塌、零漂移。

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

    # 1. 骨架硬冻结 (Skeleton Freeze)
    # 节拍总数 N 恒定、相机焦段恒定、工序拓扑先后恒定、时间戳恒定
    variant_beats = []
    for idx, beat in enumerate(source_beats):
        v_beat = copy.deepcopy(beat)
        
        # 1.1 继承硬约束
        v_beat['id'] = beat.get('id') or f'B{idx+1:02d}'
        v_beat['start'] = beat.get('start', beat.get('start_sec', 0.0))
        v_beat['end'] = beat.get('end', beat.get('end_sec', 0.0))
        v_beat['duration_sec'] = beat.get('duration_sec', round(float(v_beat['end'] - v_beat['start']), 3))
        v_beat['stage'] = beat.get('stage', beat.get('operation_type', 'demolition'))
        v_beat['operation'] = beat.get('operation', v_beat['stage'])
        v_beat['space'] = beat.get('space', 'interior')
        v_beat['camera_framing'] = beat.get('camera_framing', '14mm ultra-wide, eye-level 1.6m, horizon 50%')
        v_beat['grid_anchors'] = beat.get('grid_anchors', 'Grid B2 (Center), Grid B1-B3 across horizontal')
        v_beat['workers_present'] = beat.get('workers_present', beat.get('workers', '1-2 workers in protective workwear'))

        # 1.2 证据帧降级为构图参考帧 (不复制原片事实)
        ev_frames = beat.get('evidence_frames') or beat.get('reference_frames') or []
        v_beat['reference_frames'] = list(ev_frames)
        v_beat.pop('evidence_frames', None)

        # 1.3 正交槽位注入替换
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

        # 1.5 动态刷新 ASMR 音效特征。落到 `sfx` 这个契约键上：`audio_asmr_cues` 是这里
        # 一直在写、而全链路一处也没在读的键，写完就断在这儿。`sfx` 才有出口
        # （reverse.beats_to_dimensions → build_outline_plan_block 的 SFX 规则）。
        # 旧键同步保留，存量变体文档读它的地方不至于突然空掉。
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
        'scene_constants': {},
        'beats': variant_beats,
        'validation': [],
    }

    return variant_doc


_AI_DIVERGE_SYSTEM = """你是一位顶尖的纪录片级视觉短视频创意总监与真实空间建造设计师。
你的任务是根据给定的「1:1 黄金母本延时改造视频」的工序骨架和节奏，在确保物理工序拓扑与分镜完全可复用的前提下，进行四轴正交创意发散（AI Orthogonal Mutation），为创作者构思出具备极高网感、视觉奇观反差、高完播率但【100% 真实写实】的新创意方案。

★★★★★ REALISM-ONLY POLICY（去科幻 / 严格真实写实硬约束 — 违反直接废弃）：
1. 严禁任何科幻、未来机能、太空宇航、外星异星、赛博朋克、全息投影、RGB霓虹灯带、发光科技面板、失重力、虚构魔法或超自然发光生物！
2. 所有方案必须是【现实世界中可以真实施工建造的】建筑、庇护所、工坊或空间改造，具有强烈的工匠手作质感、真实物料触感与自然地理美感。
3. 四大正交发散轴：
   - 轴 1 地貌与水体环境 (Environment & Biome): 真实自然地理与微气候水体（如：雪山松林积雪与清澈冰溪、火山地热私汤、热带雨林水域、悬崖岩穴石壁、荒漠绿洲清泉、高山峡谷瀑布、海边礁石海湾）。
   - 轴 2 材质与工艺体系 (Material & Craft): 真实建筑与手工施工材料（如：老柚木防腐原木、侘寂微水泥、哑光黑碳钢、粗石毛石、天然玄武岩打磨、传统夯土、手工红砖、黄铜五金、双层中空钢化玻璃）。严禁碳纤维RGB、太空合金、全息舱。
   - 轴 3 空间功能与软装 (Space Function & Furnishing): 真实生活、手作与度假功能（如：雪山暖炉观景木屋、恒温地热私汤茶室、水上木工坊与皮具案台、崖穴恒温酒窖、荒漠避暑隐庐、林间画室）。严禁电竞舱、科研实验舱、太空观测台。
   - 轴 4 终极生物/自然奇观揭示 (Hero Creature / Reveal): 真实自然生态野生动物或宏大自然水景（如：4米野生北极白鲸/独角鲸、高山马鹿缓缓走过、热带巨骨舌鱼/水豚、温泉猕猴群、野生双峰驼、高原雪豹）。严禁发光生物、异星怪兽、赛博机械兽。

硬性约束：
- 构思 {count} 组风格截然不同、极具视觉冲击力的写实正交二创方案。
- 方案必须符合工序施工的可视化逻辑（破拆 -> 结构 -> 隐蔽 -> 封板 -> 面层 -> 地面 -> 设备 -> 软装 -> 揭示），充满质感与具象细节，严禁空洞虚浮的抽象词汇。
- 若用户提供了发散方向（User Direction/Brief），必须紧密围绕该方向深度发散出不同层次的方案（若方向中带有科幻词汇，必须将其转化为现实写实建造对应物）；若未提供，则自由发散最吸睛、反差感最强的爆款写实建造方向。
{trend_guidance}

输出格式：
严格返回一个 JSON 数组（无任何 Markdown 代码块，无额外废话），包含 {count} 个对象，每个对象结构如下：
[
  {{
    "id": "theme_unique_id",
    "name": "中文创意主题名 (如：极地峡湾观景木屋)",
    "icon": "❄️",
    "hook": "一句话爆款卖点（20字以内，如：冰封极地下的厚重松木木屋与北极白鲸）",
    "trend_ref": "说明借鉴了哪条联网参考的哪个要点（如：借鉴极地避难所的双层保温木结构与全景防风雪窗）",
    "axes": {{
      "environment": "具体地貌与水体环境中文描述",
      "material": "具体材质与工艺体系中文描述",
      "function": "具体空间功能与软装中文描述",
      "hero_reveal": "具体终极生物或事件揭示中文描述"
    }},
    "scene_signature": "One concise English sentence summarizing the final structure and environment.",
    "banned_elements": ["sci-fi", "cyberpunk", "neon", "glowing tech", "spaceship", "alien", "negative_keyword_1", "negative_keyword_2"]
  }}
]"""


def _generate_fallback_ideas(brief: Optional[str] = None, count: int = 4, trend_refs: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """当 LLM 不可用或解析失败时的丰富动态写实兜底方案（严格去科幻）。"""
    ref_ids = [r['id'] for r in (trend_refs or []) if isinstance(r, dict) and r.get('id')]
    defaults = [
        {
            'id': 'polar_cabin',
            'name': '极地雪屋',
            'icon': '❄️',
            'hook': '冰封峡湾边的厚重防风雪木屋与北极白鲸',
            'trend_ref': '借鉴极地避难所的双层保温木结构与全景防风雪窗',
            'trend_ref_ids': ref_ids,
            'axes': {
                'environment': '极地厚积雪地貌与剔透深蓝峡湾海面',
                'material': '粗犷芬兰松木原木 + 侘寂微水泥 + 黑色碳化防腐木',
                'function': '防风雪极地观景木屋 + 粗石壁炉与羊毛卧榻',
                'hero_reveal': '野生北极白鲸群在窗外深蓝峡湾中缓缓掠过',
            },
            'scene_signature': 'A heavy timber and stone insulated refuge cabin embedded in arctic snow, overlooking clear deep-blue fjord waters.',
            'banned_elements': ['sci-fi capsule', 'cyberpunk', 'neon lights', 'spaceship', 'hologram', 'muddy riverbank', 'green river water', 'rusted mild steel container'],
        },
        {
            'id': 'volcanic_spa',
            'name': '火山私汤茶室',
            'icon': '🌋',
            'hook': '火山黑石地热热泉与高山溪红点鲑群',
            'trend_ref': '借鉴火山地热私汤的黑色玄武岩打磨与暗调隐形透光',
            'trend_ref_ids': ref_ids,
            'axes': {
                'environment': '火山热泉泥地与冒泡地热蒸气水体',
                'material': '哑光黑碳钢构件 + 黑色玄武岩打磨 + 碳化原木',
                'function': '恒温天然私汤茶室 + 悬浮实木榻榻米',
                'hero_reveal': '地热温泉清澈水体中高山溪红点鲑群缓缓游弋',
            },
            'scene_signature': 'A matte black carbon-steel geothermal bath pavilion nestled in volcanic hot spring terrain with porous black basalt stone walls.',
            'banned_elements': ['sci-fi', 'futuristic', 'neon channels', 'glowing tech', 'iceberg', 'snowfield', 'office desk', 'bright fluorescent tube'],
        },
        {
            'id': 'rainforest_workshop',
            'name': '雨林水上工坊',
            'icon': '🪵',
            'hook': '热带雨林水上实木工坊与野生水豚',
            'trend_ref': '借鉴雨林水上高脚屋的老柚木防腐榫卯与自然通风排湿结构',
            'trend_ref_ids': ref_ids,
            'axes': {
                'environment': '暴雨热带雨林与原生态清澈溪流水域',
                'material': '缅甸老柚木防腐结构 + 粗糙毛石基座 + 铜质暗扣',
                'function': '实木榫卯木工坊 + 手工皮革制作台与原木搁架',
                'hero_reveal': '窗外清澈水流中野生水豚与巨骨舌鱼缓缓游弋',
            },
            'scene_signature': 'A handcrafted reclaimed teak wood and rough stone craftsman stilt pavilion submerged along a tropical rainforest river.',
            'banned_elements': ['sci-fi', 'cyberpunk', 'neon lights', 'rgb lighting', 'carbon fiber', 'hologram', 'polar bear', 'tatami', 'hot spring steam'],
        },
        {
            'id': 'cliff_cellar',
            'name': '崖壁溶洞茶窖',
            'icon': '🪨',
            'hook': '天然溶洞石壁恒温茶窖与地下清泉盲鱼',
            'trend_ref': '借鉴天然石灰岩溶洞的恒温微气候与粗粝玄武岩黄铜工法',
            'trend_ref_ids': ref_ids,
            'axes': {
                'environment': '天然石灰岩溶洞石壁与清冽地下暗河',
                'material': '粗糙玄武岩打磨 + 黄铜暗埋构件 + 老橡木实木梁',
                'function': '隐秘溶洞恒温酒窖与茶歇冥想室',
                'hero_reveal': '溶洞地下清冽暗河中野生岩斑盲鱼群缓缓游弋',
            },
            'scene_signature': 'A subterranean basalt and brass sanctuary built inside a natural karst cave alongside a clear underground river.',
            'banned_elements': ['sci-fi', 'futuristic', 'crystal fantasy', 'glowing crystals', 'neon', 'sunlight', 'skyline', 'traffic noise', 'white drywalls'],
        },
        {
            'id': 'desert_sanctuary',
            'name': '荒漠夯土隐庐',
            'icon': '🏜️',
            'hook': '荒漠红土绿洲夯土避暑居与野生双峰驼',
            'trend_ref': '借鉴传统生土夯土墙的蓄热调温与胡杨木大梁结构',
            'trend_ref_ids': ref_ids,
            'axes': {
                'environment': '干旱荒漠红土与绿洲清泉水体',
                'material': '传统生土夯土墙 + 粗壮胡杨木大梁 + 透气陶土砖',
                'function': '绿洲避暑茶室 + 穹顶自然采光与亚麻地台',
                'hero_reveal': '绿洲清泉边野生双峰驼低头静静饮水',
            },
            'scene_signature': 'A monolithic rammed-earth and desert timber pavilion integrated into desert oasis terrain with deep shaded openings.',
            'banned_elements': ['sci-fi', 'futuristic', 'neon', 'metal spacecraft', 'high-tech panels', 'blizzard', 'glacier'],
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
    """调用大模型为黄金母本结合联网参考智能发散四轴正交二创创意方案。"""
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

    user_prompt = (
        f"【黄金母本背景】\n"
        f"- 视频标题/源文件：{baseline_doc.get('video_name', '母本视频')}\n"
        f"- 节拍总数：{len(source_beats)} 拍\n"
        f"- 原始场景特征：{baseline_doc.get('scene_signature', '户外江岸集装箱改造')}\n"
        f"- 工序阶梯流转：\n{json.dumps(beats_summary, ensure_ascii=False, indent=2)}\n\n"
        f"{trend_block}"
        f"【创作者发散偏好】\n"
        f"{brief.strip() if brief and brief.strip() else '（未指定特定风格，请自由发散最吸睛、反差感极强的 4 种高网感爆款主题）'}\n\n"
        f"请基于以上母本工序骨架并深度结合联网参考，直接输出包含 {count} 个正交方案的 JSON 数组。"
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
            cleaned_ideas = []
            for idx, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                axes = item.get('axes') or {}
                if not axes.get('environment') and not axes.get('material'):
                    continue
                cleaned_ideas.append({
                    'id': str(item.get('id') or f'idea_{idx+1}'),
                    'name': str(item.get('name') or f'创意方案 {idx+1}'),
                    'icon': str(item.get('icon') or '✨'),
                    'hook': str(item.get('hook') or ''),
                    'trend_ref': str(item.get('trend_ref') or (selected_refs[idx % len(selected_refs)].get('label') if selected_refs else '')),
                    'trend_ref_ids': all_ref_ids,
                    'axes': {
                        'environment': str(axes.get('environment') or ''),
                        'material': str(axes.get('material') or ''),
                        'function': str(axes.get('function') or ''),
                        'hero_reveal': str(axes.get('hero_reveal') or ''),
                    },
                    'scene_signature': str(item.get('scene_signature') or ''),
                    'banned_elements': list(item.get('banned_elements') or []),
                })
            if cleaned_ideas:
                return cleaned_ideas[:count]
    except Exception as e:
        print(f"[mutate] AI 发散模型调用或解析异常，采用高质量兜底方案: {e}")

    return _generate_fallback_ideas(brief=brief, count=count, trend_refs=selected_refs)


