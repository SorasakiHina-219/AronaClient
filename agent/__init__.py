# agent/__init__.py
# AronaClient A1' —— 独立执行 agent（最小进程骨架）
#
# ============================================================
# 本包当前的能力边界（A1'）
# ============================================================
#
# 已实现：
#   * 独立子进程（挂起创建 -> 绑定 Job Object -> 恢复）
#   * 匿名管道（stdin / stdout / stderr）
#   * 4 字节长度前缀消息帧
#   * 版本协商 · 心跳 · 优雅停止
#
# 【未】实现（属后续阶段）：
#   * HMAC / 密钥 / 授权票据
#   * 任何真实操作命令 —— 动作白名单当前为【空】
#   * 截屏 / 窗口信息 / 鼠标键盘
#
# ============================================================
# 诚实声明
# ============================================================
#
# * 本包【不】声称实现了用户授权的独立验证。
# * 本包【不】声称能抵御主进程被攻陷。
# * Job Object 消除了「绑定前执行用户代码」这一具体路径，
#   但【未】跨 Windows 版本验证，也【未】处理嵌套 Job 等场景。
#
# 详见工作区的 D0 / D0.1 审查文档。

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
__all__ = ["protocol", "winjob", "child", "launcher"]

__version__ = "0.1.0-a1p"
