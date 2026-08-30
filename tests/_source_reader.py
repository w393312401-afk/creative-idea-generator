# -*- coding: utf-8 -*-
"""按函数名直接从磁盘上的模块文件里取源码，**不走 `inspect.getsource`**。

为什么不用 inspect：`getsource` 靠 `linecache` 拿文件行，而 `linecache` 是进程级共享状态。
这套件里有测试会起后台线程（server / task worker），线程侧一旦格式化 traceback 就同样会
走 `linecache.checkcache` / `updatecache`；与主线程交错时，`getsource` 会按函数的
`co_firstlineno` 去一份被改写过的行表里取，取回文件里另一处的**单行**。

2026-08-30 全套连跑时出现过两次这种随机失败：哨兵报「_beat_contract 里有 0 处逐帧逐字锁」、
`compose_anchor_and_packet` 的源码只有 `errors.extend(check_out_and_in(...))` 一行。同一份
代码随后连跑四轮全过。失败信息指向业务代码，真因却在测试基础设施——这种哨兵最怕的就是
这个：响成随机噪音之后，真出事时没人再信它。

读文件不碰 linecache，也就没有这个耦合。
"""

import re


def top_level_function_source(func):
    """`func` 所在模块文件里，这个顶层函数从 `def` 到下一个顶层定义之间的源码。

    只处理顶层函数（列宽 0 的 `def`）——本仓库的哨兵扫的都是模块级函数。取不到时抛
    AssertionError 而不是返回空串：返回空串会让「找不到源码」伪装成「一处锁都没有」，
    正是上面那次随机失败最难查的地方。
    """
    path = func.__code__.co_filename
    with open(path, 'r', encoding='utf-8') as fh:
        text = fh.read()

    start_re = re.compile(rf'^(?:async\s+)?def\s+{re.escape(func.__name__)}\s*\(', re.M)
    m = start_re.search(text)
    assert m, f'在 {path} 里找不到顶层函数 {func.__name__} 的定义'

    body_start = m.start()
    nxt = re.compile(r'^(?:@|(?:async\s+)?def\s|class\s)', re.M).search(text, m.end())
    return text[body_start:nxt.start()] if nxt else text[body_start:]
