#!/usr/bin/env python3
"""首次运行时从模板生成 server_config.json（run.bat / run.sh 调用）。

不能直接 copy 模板：模板里 apiKey / accessCode / codexApiKey 三项填的是中文
说明文字而不是空值。原样拷过去的话——

* accessCode 是一句"设一个访问码…"，非空即视为已设门禁，于是整个界面被一个
  谁也不知道的口令锁死，比没有配置文件还糟；
* apiKey 是一句"在这里填你…的 API Key"，会被当成真密钥发给上游，
  报错信息变成一条看不懂的 401，而不是"你还没配密钥"。

所以这里把这类占位说明清成空串，再写盘，并把"还需要你自己填什么"打印出来。
已存在 server_config.json 时什么都不做——绝不覆盖用户已经配好的东西。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, 'server_config.json')
TEMPLATE = os.path.join(ROOT, 'server_config.example.json')

# 值里出现这些片段就说明它还是模板里的说明文字，不是真配置
PLACEHOLDER_MARKERS = ('在这里填', '设一个', '留空=', '请填', 'YOUR_', 'your_')

# 必须由用户自己填、填之前功能不完整的键 → 提示语
NEEDS_USER_INPUT = {
    'apiKey': 'LLM 接口密钥，不填则无法「激发创意」与合成提示词',
    'codexApiKey': '本机 Codex 代理密钥，仅在使用 codex 路由的模型时需要',
}


def is_placeholder(value) -> bool:
    if not isinstance(value, str):
        return False
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


def main() -> int:
    if os.path.exists(TARGET):
        print('[SPARK] server_config.json 已存在，保持不动。')
        return 0
    if not os.path.exists(TEMPLATE):
        print('[SPARK] 找不到 server_config.example.json，跳过配置生成。')
        return 0

    with open(TEMPLATE, 'r', encoding='utf-8') as f:
        config = json.load(f)

    cleared = []
    for key, value in list(config.items()):
        if key.startswith('_'):
            continue
        if is_placeholder(value):
            config[key] = ''
            cleared.append(key)

    with open(TARGET, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write('\n')

    print('[SPARK] 已根据模板生成 server_config.json')
    if 'accessCode' in cleared:
        print('        · accessCode 留空 = 本机使用不设访问门禁'
              '（要对外开放时再填一个口令）')
    todo = [k for k in cleared if k in NEEDS_USER_INPUT]
    if todo:
        print('')
        print('        服务可以启动、界面可以打开，但下面这些要你自己填：')
        for key in todo:
            print('        · %-12s %s' % (key, NEEDS_USER_INPUT[key]))
        print('')
        print('        用记事本打开 server_config.json 填好后重启服务即可。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
