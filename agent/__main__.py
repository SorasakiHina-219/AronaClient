# agent/__main__.py
# 使 `python -m agent` 启动 agent 进程。
#
# 注意：这里【不能】有任何额外的 print，
# 因为 stdout 已被 child.py 让给协议帧。

import sys

from agent.child import main

if __name__ == "__main__":
    sys.exit(main())
