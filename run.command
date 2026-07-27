#!/bin/bash
# 双击运行：macOS 的 Finder 只认 .command 后缀为"双击在 Terminal 里执行"，
# .sh 文件双击默认交给文本编辑器打开（这正是 run.sh 打不开、只能选编辑器的原因）。
# 这个文件本身不重复 run.sh 的逻辑，只是转手调用它。
cd "$(dirname "$0")"
exec ./run.sh
