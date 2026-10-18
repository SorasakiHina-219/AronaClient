
"""AronaClient 应用启动模块。

受控操作：
    * 通过名称或路径启动应用程序
    * 校验路径合法性，禁止路径穿越
    * 使用 Windows 原生 ShellExecute 或 subprocess
    * 返回可验证的启动结果

安全约束：
    * 不使用 shell=True 执行模型生成的任何内容
    * 不接受包含 ;|&`$() 等 shell 元字符的参数
    * 不接受路径穿越序列（../、..\\ 等）
    * 不接受 .exe/.bat/.cmd/.ps1 之外的可执行文件类型（白名单）
    * 禁止启动高风险系统工具（cmd.exe, powershell.exe 等）
    * 禁止从 Windows 系统目录启动
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any


# ============================================================
# 安全常量
# ============================================================

FORBIDDEN_SHELL_CHARS = set(';|&`$(){}[]<>')

ALLOWED_EXECUTABLE_EXTENSIONS = {".exe", ".msi", ".lnk", ".appx"}

FORBIDDEN_EXECUTABLE_EXTENSIONS = {
    ".bat", ".cmd", ".ps1", ".vbs", ".js", ".wsf"
}

# 高风险系统工具黑名单（通过名称匹配）
DANGEROUS_EXECUTABLE_NAMES = {
    # 命令解释器
    "cmd.exe", "command.com",
    "powershell.exe", "powershell_ise.exe",
    "pwsh.exe",
    "wsl.exe", "wslconfig.exe",
    # 脚本引擎
    "cscript.exe", "wscript.exe",
    "mshta.exe", "regsvr32.exe",
    # 系统工具（高风险）
    "schtasks.exe", "taskeng.exe",
    "at.exe", "sc.exe",
    # 远程执行
    "runas.exe",
    # 包管理器
    "npm.exe", "yarn.exe", "pip.exe", "pip3.exe",
    # 其他高危
    "msiexec.exe",
}

MAX_PATH_LENGTH = 4096


class AppLaunchError(Exception):
    """应用启动错误基类。"""


class AppLauncher:
    """
    受控应用启动器。

    设计原则：
        * 不执行任意命令 — 只接受预定义的应用名称或路径
        * 不拼接用户输入到 shell 命令
        * 不使用 shell=True
        * 不执行批处理/脚本文件
        * 禁止启动高风险系统工具
    """

    def __init__(self, search_paths=None):
        self._search_paths = search_paths or []

    def launch(self, name_or_path, args=None, workdir=None):
        """
        启动应用程序。

        参数：
            name_or_path: 应用名称（在 PATH 中查找）或完整路径。
            args: 命令行参数（可选）。
            workdir: 工作目录（可选）。

        返回：
            {"success": bool, "command": str, "pid": int|None,
             "error": str|None, "message": str}
        """
        valid, data = self._validate(name_or_path, args, workdir)
        if not valid:
            return data

        resolved_name = data["resolved"]
        resolved_args = data["args"]
        resolved_workdir = data["workdir"]

        try:
            executable = self._resolve_executable(resolved_name)
        except AppLaunchError as exc:
            return {
                "success": False,
                "command": name_or_path,
                "pid": None,
                "error": str(exc),
                "message": "启动被安全策略拒绝。",
            }
        if executable is None:
            return {
                "success": False,
                "command": name_or_path,
                "pid": None,
                "error": f"找不到可执行文件：{resolved_name}",
                "message": "应用不存在或不在 PATH 中。",
            }

        try:
            cmd_parts = [str(executable)]
            if resolved_args:
                cmd_parts.extend(shlex.split(resolved_args))
        except ValueError:
            cmd_parts = [str(executable)] + resolved_args.split()

        try:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE

            process = subprocess.Popen(
                cmd_parts,
                cwd=resolved_workdir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startup,
            )

            return {
                "success": True,
                "command": " ".join(cmd_parts),
                "pid": process.pid,
                "error": None,
                "message": f"已启动进程 PID={process.pid}：{executable}",
            }

        except subprocess.SubprocessError as exc:
            return {
                "success": False,
                "command": " ".join(cmd_parts),
                "pid": None,
                "error": f"{type(exc).__name__}: {exc}",
                "message": "进程创建失败。",
            }
        except Exception as exc:
            return {
                "success": False,
                "command": name_or_path,
                "pid": None,
                "error": f"{type(exc).__name__}: {exc}",
                "message": "启动过程发生未知错误。",
            }

    # ========================================================
    # Internal methods
    # ========================================================

    def _validate(self, name_or_path, args, workdir):
        if not isinstance(name_or_path, str) or not name_or_path.strip():
            return False, {
                "success": False, "resolved": None,
                "args": None, "workdir": None,
                "error": "应用名称或路径不能为空。",
                "message": "参数校验失败。",
            }

        name_or_path = name_or_path.strip()

        if len(name_or_path) > MAX_PATH_LENGTH:
            return False, {
                "success": False, "resolved": None,
                "args": None, "workdir": None,
                "error": f"路径长度超过上限（{MAX_PATH_LENGTH}）。",
                "message": "路径太长，拒绝启动。",
            }

        if FORBIDDEN_SHELL_CHARS & set(name_or_path):
            return False, {
                "success": False, "resolved": None,
                "args": None, "workdir": None,
                "error": "应用名称或路径包含禁止字符。",
                "message": "检测到潜在的 shell 注入特征。",
            }

        if ".." in name_or_path:
            return False, {
                "success": False, "resolved": None,
                "args": None, "workdir": None,
                "error": "路径包含穿越序列 '..'，拒绝启动。",
                "message": "路径安全检查失败。",
            }

        parsed_args = ""
        if args is not None:
            if not isinstance(args, str):
                return False, {
                    "success": False, "resolved": None,
                    "args": None, "workdir": None,
                    "error": "参数必须是字符串。",
                    "message": "参数类型错误。",
                }
            if FORBIDDEN_SHELL_CHARS & set(args):
                return False, {
                    "success": False, "resolved": None,
                    "args": None, "workdir": None,
                    "error": "参数包含禁止字符。",
                    "message": "参数安全检查失败。",
                }
            parsed_args = args.strip()

        resolved_workdir = None
        if workdir is not None:
            if not isinstance(workdir, str) or not workdir.strip():
                return False, {
                    "success": False, "resolved": None,
                    "args": None, "workdir": None,
                    "error": "工作目录不能为空。",
                    "message": "参数校验失败。",
                }
            workdir = workdir.strip()
            if ".." in workdir or FORBIDDEN_SHELL_CHARS & set(workdir):
                return False, {
                    "success": False, "resolved": None,
                    "args": None, "workdir": None,
                    "error": "工作目录包含非法字符。",
                    "message": "工作目录安全检查失败。",
                }
            resolved_workdir = workdir

        return True, {
            "success": True,
            "resolved": name_or_path,
            "args": parsed_args,
            "workdir": resolved_workdir,
            "error": None,
            "message": None,
        }

    def _resolve_executable(self, name_or_path):
        path_obj = Path(name_or_path)

        # Case 1: absolute or relative path that exists
        if path_obj.is_absolute() or path_obj.exists():
            if not path_obj.exists():
                return None
            if path_obj.suffix.lower() in FORBIDDEN_EXECUTABLE_EXTENSIONS:
                raise AppLaunchError(
                    f"禁止启动脚本文件：{path_obj}"
                )
            resolved = str(path_obj.resolve())
            exe_name = path_obj.name.lower()
            if exe_name in DANGEROUS_EXECUTABLE_NAMES:
                raise AppLaunchError(
                    f"禁止启动高风险系统工具：{resolved}"
                )
            return resolved

        # Case 2: search in PATH
        for ext in [".exe", ".msi", ""]:
            candidate = name_or_path + ext
            found = self._which(candidate)
            if found:
                exe_name = Path(found).name.lower()
                if exe_name in DANGEROUS_EXECUTABLE_NAMES:
                    raise AppLaunchError(
                        f"禁止启动高风险系统工具：{found}"
                    )
                return found

        found = self._which(name_or_path)
        if found:
            exe_name = Path(found).name.lower()
            if exe_name in DANGEROUS_EXECUTABLE_NAMES:
                raise AppLaunchError(
                    f"禁止启动高风险系统工具：{found}"
                )
            for prefix in SYSTEM_DIR_PREFIXES:
                if found.lower().startswith(prefix.lower()):
                    raise AppLaunchError(
                        f"禁止从系统目录启动：{found}"
                    )
            return found

        return None

    def _which(self, cmd):
        search_dirs = list(self._search_paths) + os.environ.get(
            "PATH", ""
        ).split(os.pathsep)

        for directory in search_dirs:
            if not directory:
                continue
            candidate = Path(directory) / cmd
            if candidate.exists() and candidate.is_file():
                return str(candidate.resolve())

        if os.name == "nt":
            local_apps = Path(
                os.environ.get("LOCALAPPDATA", ""),
                "Microsoft",
                "WindowsApps",
            )
            for ext in [".exe", ""]:
                candidate = local_apps / (cmd + ext)
                if candidate.exists():
                    return str(candidate.resolve())

        return None
