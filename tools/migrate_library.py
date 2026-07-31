# -*- coding: utf-8 -*-
"""把单文件的 library.json 迁移成拆分库 library/（索引 + 逐条正文）。

服务端在第一次访问创意库时会**自动**做同样的事（server_common._ensure_library_split），
所以正常情况下你不需要跑这个脚本。它存在是为了三种场合：

  1. 想在启动服务前先看一眼迁移结果（--dry-run 只报告不落盘）；
  2. 想确认迁移是否已经发生（不带参数跑一次，它会告诉你当前形态）；
  3. 拆分库出了问题、要从 library.json 重来一遍（--force）。

为什么要拆：老形态是"客户端始终持有完整数组、整份 POST 回来覆盖"。实测 2 条创意
就 208KB（单条 164KB——prompt_block / prompt_slots / audit_md / repair_md / frameRun
正文全塞在条目里），改一个字段要上传并重写全库。而"整表覆盖"这个动作引发过两次
数据事故，逼出了三道防线（空库拒写 / 缩量闸门 409 / .bak 轮换），用户日常撞到的
就是那句"保存失败，请刷新页面后重试"。拆开之后写一条只碰一个文件，那些洞在结构上
就不存在了。详见 docs/project_workbench_refactor_plan.md。

迁移**不删除** library.json，而是把它备份成 library.json.pre-split —— 出任何问题
时那是唯一一份完整的老数据。

用法：
    python tools/migrate_library.py               # 需要就迁移，已迁移则只报告
    python tools/migrate_library.py --dry-run     # 只报告会发生什么
    python tools/migrate_library.py --force       # 丢弃现有拆分库，从 library.json 重来
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# import server_common 会把 sys.stdout 换成一个 _Tee：只有连着终端时才往控制台写，
# 否则全部落进 server.log。对服务进程那是对的，对一次性 CLI 工具就成了"输出凭空
# 消失"（`python tools/migrate_library.py > out.txt` 会得到一个空文件）。所以先
# 抓住真正的 stdout，本脚本的报告一律往它写。
_REAL_STDOUT = sys.stdout

import server_common as sc  # noqa: E402


def emit(line=''):
    try:
        _REAL_STDOUT.write(f'{line}\n')
        _REAL_STDOUT.flush()
    except Exception:
        pass


def _human_bytes(n):
    units = ['B', 'KB', 'MB', 'GB']
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f'{v:.1f} {units[i]}'


def describe():
    """当前存储形态的一份体检报告。"""
    root, index_path, items_dir = sc._library_paths()
    report = {
        'legacy_file': sc.DB_FILE,
        'legacy_exists': os.path.exists(sc.DB_FILE),
        'legacy_bytes': os.path.getsize(sc.DB_FILE) if os.path.exists(sc.DB_FILE) else 0,
        'split_dir': root,
        'split_ready': os.path.exists(index_path),
        'index_bytes': os.path.getsize(index_path) if os.path.exists(index_path) else 0,
        'item_count': 0,
        'items_bytes': 0,
    }
    if os.path.isdir(items_dir):
        names = [n for n in os.listdir(items_dir) if n.endswith('.json')]
        report['item_count'] = len(names)
        report['items_bytes'] = sum(os.path.getsize(os.path.join(items_dir, n)) for n in names)
    return report


def print_report(report):
    emit('── 创意库存储形态 ─────────────────────────────')
    emit(f"  老单文件 : {report['legacy_file']}"
          f"{'（存在，' + _human_bytes(report['legacy_bytes']) + '）' if report['legacy_exists'] else '（不存在）'}")
    if report['split_ready']:
        emit(f"  拆分库   : {report['split_dir']}/  ✅ 已建立")
        emit(f"    index.json  {_human_bytes(report['index_bytes'])}"
              f"   ← 列表渲染只需要读它")
        emit(f"    items/      {report['item_count']} 条正文，共 {_human_bytes(report['items_bytes'])}"
              f"   ← 点开某条才读")
    else:
        emit(f"  拆分库   : {report['split_dir']}/  ⬜ 尚未建立")


def main():
    parser = argparse.ArgumentParser(description='library.json → library/ 拆分库迁移')
    parser.add_argument('--dry-run', action='store_true', help='只报告，不落盘')
    parser.add_argument('--force', action='store_true',
                        help='拆分库已存在时也重新从 library.json 拆一遍（确知拆分库有问题时才用）')
    args = parser.parse_args()

    before = describe()
    print_report(before)
    emit()

    if before['split_ready'] and not args.force:
        emit('✅ 拆分库已经建立，无需迁移。（要强制重来请加 --force）')
        return 0

    if not before['legacy_exists']:
        if args.force:
            emit(f"❌ 找不到 {sc.DB_FILE}，--force 无从重建。")
            return 1
        emit(f"ℹ️  还没有 {sc.DB_FILE}，也没有拆分库——这是一个全新的库，"
              f"服务端会在第一次收藏时自动建立拆分库。")
        return 0

    # 迁移前先确认老库读得出来。读不出来就必须停手：把损坏的库"当成空库"拆一遍，
    # 等于把一次读取失败固化成一次整库清零。
    legacy = sc.read_library(path=sc.DB_FILE)
    if legacy is None:
        emit(f"❌ {sc.DB_FILE} 读取失败（格式损坏），已停止迁移。")
        emit(f"   请先人工修复，或从 {sc.DB_FILE}.bak 恢复后重试。")
        return 1
    emit(f"读到 {len(legacy)} 条创意，最大一条 "
          f"{_human_bytes(max((len(json.dumps(i, ensure_ascii=False)) for i in legacy), default=0))}。")

    if args.dry_run:
        emit('\n--dry-run：不落盘。实际执行会：')
        emit(f"  · 写 {before['split_dir']}/index.json（{len(legacy)} 条轻量索引）")
        emit(f"  · 写 {before['split_dir']}/items/<id>.json（{len(legacy)} 个正文文件）")
        emit(f"  · 把 {sc.DB_FILE} 备份成 {sc.DB_FILE}.pre-split（不删除原文件）")
        return 0

    result = sc.migrate_library_to_split(force=args.force)
    if result is None:
        emit('ℹ️  没有发生迁移（拆分库已存在）。')
        return 0

    emit(f"\n✅ 已迁移 {result['migrated']} 条创意到 {result['dir']}/")
    if result.get('backup'):
        emit(f"   原文件已备份：{result['backup']}")
    emit()
    print_report(describe())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
