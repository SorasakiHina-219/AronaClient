"""T2 (D1) 验证测试 — 应用启动器系统目录保护。

证明：
  1. 根因：修复前 `SYSTEM_DIR_PREFIXES` 全仓无定义（仅 app_launcher.py L291
     一处引用且不可达），任何可达路径引用即 NameError。
  2. 修复后：
     - 常量存在且为小写系统目录前缀元组；
     - Case-2 PATH 命中分支**实际执行**系统目录前缀检查（命中即拒绝）；
     - 普通目录命中放行（不误拒）；
     - Case-1 绝对路径行为不变（对照）；
     - 旧不可达死代码（L284-296 裸名重查块）已删除。

本测试不真实启动外部应用：以 tmp_path 构造目录布局 + 重写模块级
SYSTEM_DIR_PREFIXES 前缀使其指向临时"Windows"根，仅驱动
`AppLauncher._resolve_executable` / `launch` 的解析+校验层，不触发
subprocess 启动。

Run:
  .venv/Scripts/python.exe -m pytest tests/test_app_launcher_d1.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from computer.app_launcher import (
    AppLauncher,
    AppLaunchError,
)
import computer.app_launcher as app_launcher_mod


# ── 根因证明 ─────────────────────────────────────────────────────────

def test_system_dir_prefixes_is_defined():
    """D1 根因修复：常量必须存在（修复前 NameError 地雷）。"""
    prefixes = app_launcher_mod.SYSTEM_DIR_PREFIXES
    assert isinstance(prefixes, tuple)
    assert len(prefixes) > 0
    for p in prefixes:
        assert p == p.lower(), f"前缀必须小写：{p}"


def test_no_unreachable_bare_name_block():
    """旧死代码（Case-3 裸名重查，L284-296）应已删除：
    方法体内 `_which` 调用只出现在 Case-2 扩展名循环内。"""
    import inspect
    src = inspect.getsource(AppLauncher._resolve_executable)
    # Case-2 循环形如 `for ext in [".exe", ".msi", ""]:`
    assert 'for ext in [".exe", ".msi", ""]:' in src
    # 死代码特征：循环外再次出现独立的 `found = self._which(name_or_path)`
    loop_end = src.index("return None")
    body = src[:loop_end]
    bare = [ln for ln in body.splitlines()
            if "self._which(name_or_path)" in ln]
    assert bare == [], f"应已删除的裸名重查仍存在于 Case-2 循环外：{bare}"


# ── 行为测试（monkeypatch PATH + search_paths，不真实启动）──────────

@pytest.fixture
def fake_windows(tmp_path, monkeypatch):
    """构造一个临时 "Windows" 目录布局（Windows/System32 等），并重写模块级
    SYSTEM_DIR_PREFIXES 使其指向该临时根——不依赖真实 C:\\Windows，测试可移植。
    同时设一个 normal 目录放置合法应用。"""
    import computer.app_launcher as m
    ws = tmp_path / "Windows"
    (ws / "System32").mkdir(parents=True)
    (ws / "SysWOW64").mkdir()
    (ws / "Temp").mkdir()
    (ws / "notepad.exe").write_text("fake")
    (ws / "System32" / "evil.exe").write_text("fake")
    normal = tmp_path / "Apps"
    normal.mkdir()
    (normal / "myapp.exe").write_text("fake")
    monkeypatch.setattr(
        m, "SYSTEM_DIR_PREFIXES",
        tuple(str(ws).lower().rstrip("\\") + d for d in
              ("", "\\system32", "\\syswow64", "\\temp")),
    )
    return ws, normal


def test_system_dir_rejected(fake_windows):
    ws, normal = fake_windows
    launcher = AppLauncher(search_paths=[str(ws)])
    with pytest.raises(AppLaunchError, match="系统目录"):
        launcher._resolve_executable("notepad")
    with pytest.raises(AppLaunchError, match="系统目录"):
        launcher._resolve_executable("notepad.exe")


def test_normal_app_allowed(fake_windows):
    ws, normal = fake_windows
    launcher = AppLauncher(search_paths=[str(normal)])
    resolved = launcher._resolve_executable("myapp")
    assert resolved is not None
    assert Path(resolved).name.lower() == "myapp.exe"


def test_absolute_path_case1_unchanged(fake_windows):
    """Case-1（绝对/存在路径）不经前缀检查，行为不变（对照）。"""
    ws, normal = fake_windows
    exe = normal / "myapp.exe"
    launcher = AppLauncher(search_paths=[str(ws)])
    resolved = launcher._resolve_executable(str(exe))
    assert resolved is not None
    assert Path(resolved) == exe.resolve()


def test_dangerous_name_still_blocked(fake_windows):
    """系统目录保护不得削弱既有高风险工具黑名单。"""
    ws, normal = fake_windows
    (normal / "powershell.exe").write_text("fake")
    launcher = AppLauncher(search_paths=[str(normal)])
    with pytest.raises(AppLaunchError):
        launcher._resolve_executable("powershell")


def test_full_launch_rejects_system_dir(fake_windows, monkeypatch):
    """端到端：launch() 对系统目录目标返回 success=False（拒绝语义完整）。
    search_paths 首位放 "Windows" 根，Case-2 扩展名循环即可命中 notepad.exe。"""
    ws, normal = fake_windows
    monkeypatch.setenv("PATH", str(normal))
    launcher = AppLauncher(search_paths=[str(ws)])
    result = launcher.launch("notepad")
    assert result["success"] is False
    assert "系统目录" in result["error"]
