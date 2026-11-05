# agent/__main__.py
# 使 `python -m agent` 启动 agent 进程。
#
# 注意：这里【不能】有任何额外的 print，
# 因为 stdout 已被 child.py 让给协议帧。

# -*- coding: utf-8 -*-
# Copyright (C) 2024-2026 SorasakiHina-219
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
import sys

from agent.child import main

if __name__ == "__main__":
    sys.exit(main())
