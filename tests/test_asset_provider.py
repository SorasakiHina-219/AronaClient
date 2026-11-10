# -*- coding: utf-8 -*-
"""M0-D 验收测试：ModelAssetProvider / 资源路径统一 / 降级 / 隔离（12 项）。

原则：
  * 全部使用 tmp_path 构造的隔离资源包 + Mock，绝不读写/覆盖正式模型资产；
  * 只读扫描源码与目录用于静态断言（不改任何文件）；
  * 不依赖网络、不依赖真实 GPU、不需要显示（L1/L2 导入测试用 offscreen）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from runtime.model3d.asset_provider import (  # noqa: E402
    ENV_MODEL_DIR,
    AssetStatus,
    ModelAssetError,
    ModelAssetProvider,
    clear_hash_cache,
    parse_version,
    sha256_file,
)

MODEL_REL = "arona/Arona_1.01/Arona_1.01.pmx"
MODEL_ID = "arona_1.01"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_pack(
    root: Path,
    *,
    data: bytes = b"PMX fake model payload for tests\n",
    declared_sha: str | None = None,
    declared_bytes: int | None = None,
    rel: str = MODEL_REL,
    license_status: str = "UNVERIFIED",
    with_model: bool = True,
    manifest: bool = False,
) -> Path:
    """在 root 下构造一个合法的资源包（ASSET_VERSION.json [+ 模型] [+ manifest]）。"""
    root.mkdir(parents=True, exist_ok=True)
    target = root / rel
    if with_model:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    version = {
        "schema": 1,
        "default_model_id": MODEL_ID,
        "models": {
            MODEL_ID: {
                "path": rel,
                "bytes": declared_bytes if declared_bytes is not None else len(data),
                "sha256": declared_sha if declared_sha is not None else _sha(data),
            }
        },
        "license": {"status": license_status},
    }
    if manifest:
        files = {"a.txt": b"alpha", "sub/b.txt": b"beta"}
        rows = []
        for relp, body in files.items():
            f = root / relp
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(body)
            rows.append("| `%s` | %d | `%s` |" % (relp, len(body), _sha(body)))
        mf = root / "manifest.md"
        mf.write_text("\n".join(rows) + "\n", encoding="utf-8")
        version["manifest"] = {"path": "manifest.md", "files": len(rows)}
    (root / "ASSET_VERSION.json").write_text(
        json.dumps(version, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


# ── 1. 默认本地模型路径 ────────────────────────────────────────
def test_01_default_local_path(tmp_path):
    pack = tmp_path / "repo_assets"
    target = _write_pack(pack)
    p = ModelAssetProvider(repo_asset_root=pack, env={})
    res = p.resolve()
    assert res.ok, res.message
    assert res.status is AssetStatus.OK
    assert res.source == "repo"
    assert res.path == target.resolve()
    assert res.sha256 == _sha(target.read_bytes())
    assert res.license_status == "UNVERIFIED"
    assert any("UNVERIFIED" in w for w in res.warnings)


# ── 2. 环境变量覆盖路径 ────────────────────────────────────────
def test_02_env_override(tmp_path):
    repo_pack = tmp_path / "repo_assets"
    _write_pack(repo_pack, data=b"repo model")
    ext_pack = tmp_path / "external_pack"
    ext_target = _write_pack(ext_pack, data=b"external model")
    p = ModelAssetProvider(repo_asset_root=repo_pack, env={ENV_MODEL_DIR: str(ext_pack)})
    res = p.resolve()
    assert res.ok, res.message
    assert res.source == "env"
    assert res.path == ext_target.resolve()


# ── 3. manifest 正确性 ─────────────────────────────────────────
def test_03_manifest_correct(tmp_path):
    pack = tmp_path / "pack"
    _write_pack(pack, manifest=True)
    p = ModelAssetProvider(repo_asset_root=pack, env={})
    ok, rows, msg, root = p.verify_pack(deep=True)
    assert ok, msg
    assert len(rows) == 2
    assert all(r[1] == "OK" for r in rows)


def test_03b_manifest_detects_corruption(tmp_path):
    pack = tmp_path / "pack"
    _write_pack(pack, manifest=True)
    (pack / "sub" / "b.txt").write_bytes(b"tampered")
    p = ModelAssetProvider(repo_asset_root=pack, env={})
    ok, rows, msg, _ = p.verify_pack(deep=True)
    assert not ok
    kinds = {r[1] for r in rows}
    assert "SHA" in kinds or "SIZE" in kinds


# ── 4. SHA256 正确性 ───────────────────────────────────────────
def test_04_sha256_correct(tmp_path):
    data = b"hash me"
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    clear_hash_cache()
    assert sha256_file(f) == _sha(data)
    pack = tmp_path / "pack"
    _write_pack(pack, data=data)
    res = ModelAssetProvider(repo_asset_root=pack, env={}).resolve()
    assert res.ok and res.sha256 == _sha(data)


# ── 5. 模型缺失 → 失败 + require 抛错（安全降级信号）────────────
def test_05_missing_model(tmp_path):
    pack = tmp_path / "pack"
    _write_pack(pack, with_model=False)
    res = ModelAssetProvider(repo_asset_root=pack, env={}).resolve()
    assert not res.ok
    assert res.status is AssetStatus.MISSING_MODEL
    assert "不存在" in res.message
    with pytest.raises(ModelAssetError):
        res.require()


# ── 6. SHA256 不匹配 → 失败 ────────────────────────────────────
def test_06_sha_mismatch(tmp_path):
    pack = tmp_path / "pack"
    _write_pack(pack, declared_sha="0" * 64)
    res = ModelAssetProvider(repo_asset_root=pack, env={}).resolve()
    assert not res.ok
    assert res.status is AssetStatus.HASH_MISMATCH
    assert "SHA256" in res.message


def test_06b_size_mismatch(tmp_path):
    pack = tmp_path / "pack"
    _write_pack(pack, declared_bytes=1)
    res = ModelAssetProvider(repo_asset_root=pack, env={}).resolve()
    assert not res.ok and res.status is AssetStatus.HASH_MISMATCH


# ── 7. 外部资源包（含 env 无效时回落 repo）──────────────────────
def test_07_external_pack_and_fallback(tmp_path):
    repo_pack = tmp_path / "repo_assets"
    repo_target = _write_pack(repo_pack, data=b"repo fallback model")
    p = ModelAssetProvider(repo_asset_root=repo_pack, env={ENV_MODEL_DIR: str(tmp_path / "nope")})
    res = p.resolve()
    assert res.ok, res.message
    assert res.source == "repo"
    assert res.path == repo_target.resolve()
    assert res.warnings
    assert any("env" in w for w in res.warnings)


# ── 8. 旧 E 盘路径不再被使用（运行时 3D 链路 + 唯一例外说明）──────
def test_08_no_e_drive_hardcode_runtime_chain():
    """运行时 3D 链路上不得再出现 E: 盘硬编码（ADR-004 §7；M0-A）。"""
    pattern = re.compile(r"[Ee]:[\\/]{1,2}Arona_1\.01")
    runtime_targets = [
        "mascot_3d.py",
        "arona3d_proto.py",
        "spatial_ui/model_runtime.py",
        "runtime/model3d/asset_provider.py",
    ]
    offenders = []
    for rel in runtime_targets:
        text = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append("%s:%d: %s" % (rel, i, line.strip()))
    assert offenders == [], "运行时链路仍存在 E 盘硬编码：\n" + "\n".join(offenders)

    # 唯一已知例外：导入工具读取"导入源"（U 盘），不是运行时加载路径。
    # 该文件不在 M0-A 作用域内，且其 E: 仅作为一次性导入源；此处仅断言其用途未变。
    importer = (REPO / "tools_import_model.py")
    if importer.is_file():
        text = importer.read_text(encoding="utf-8", errors="ignore")
        hits = [ln.strip() for ln in text.splitlines() if pattern.search(ln)]
        for hit in hits:
            assert "SRC_ROOT" in hit, "导入工具中出现非 SRC_ROOT 的 E 盘路径：%s" % hit


# ── 9. 两处旧硬编码同时解决（mascot_3d + arona3d_proto）─────────
def test_09_both_hardcodes_fixed_together():
    for rel in ("mascot_3d.py", "arona3d_proto.py"):
        text = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
        assert "ModelAssetProvider" in text, rel + " 未接入统一 Provider"
        assert not re.search(r"[Ee]:[\\/]{1,2}Arona_1\.01", text), rel + " 仍有 E 盘硬编码"
        assert ("resolve_model_asset" in text) or ("require_model_asset" in text), rel + " 未调用解析入口"
    # L2 侧也必须是 Provider 驱动
    mr = (REPO / "spatial_ui" / "model_runtime.py").read_text(encoding="utf-8")
    assert "ModelAssetProvider" in mr
    assert not re.search(r"[Ee]:[\\/]{1,2}Arona_1\.01", mr)


# ── 10. 资源失败时 2D 降级（Runtime 状态机 + 信号）──────────────
def test_10_degrade_to_2d(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
    import spatial_ui.model_runtime as mr

    pack = tmp_path / "empty_pack"
    _write_pack(pack, with_model=False)
    bad = ModelAssetProvider(repo_asset_root=pack, env={}).resolve()
    assert not bad.ok

    monkeypatch.setattr(mr, "resolve_model_asset", lambda model_id=None: bad)
    runtime = mr.AronaModelRuntime(host_widget=None, performance_mode="low")
    seen = {"failed": [], "state": []}
    runtime.failed.connect(lambda msg: seen["failed"].append(msg))
    runtime.state_changed.connect(lambda st: seen["state"].append(st))
    runtime.start()
    assert runtime.state == "failed"
    assert seen["failed"], "未发出 failed 信号"
    assert "降级" in seen["failed"][0]


# ── 11. Preview 不访问正式 API / 正式用户数据 ────────────────
def test_11_preview_isolation():
    preview_dir = REPO / "tools" / "spatial_preview"
    if not preview_dir.is_dir():
        pytest.skip("tools/spatial_preview 不存在（可选产物）")
    forbidden_imports = ("import keyring", "from keyring", "import openai", "from openai",
                         "import cv2", "from cv2", "import pyautogui", "from pyautogui")
    forbidden_calls = ("keyring.get_password", "keyring.set_password", "os.environ[\"APPDATA\"",
                       "os.getenv(\"APPDATA\"", "Path.home() / \"AppData\"", "VideoCapture(",
                       "cv2.VideoCapture", "openai.OpenAI(")
    bad = []
    for path in preview_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for frag in forbidden_imports + forbidden_calls:
            if frag in text:
                bad.append("%s: %s" % (path.name, frag))
    assert bad == [], "Preview 触及正式依赖/用户数据：\n" + "\n".join(bad)


# ── 12. 不破坏现有 L1 / L2 入口（导入 + 实例化冒烟）──────────
def test_12_l1_l2_import_smoke():
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    script = (
        "import mascot_3d; "
        "import arona3d_proto; "
        "import spatial_ui.model_runtime as m; "
        "from runtime.model3d.asset_provider import ModelAssetProvider; "
        "print(\"IMPORT-OK\")"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=240,
    )
    assert proc.returncode == 0, "L1/L2 导入失败：\nSTDOUT:%s\nSTDERR:%s" % (proc.stdout, proc.stderr)
    assert "IMPORT-OK" in proc.stdout


# ── 附：gitignore 3D 规则（M0-C）──────────────────────────────
def test_13_gitignore_protects_binaries():
    cases = {
        "assets/3d/arona/Arona_1.01/Arona_1.01.pmx": True,
        "assets/3d/arona/Arona_1.01/Arona_1.01.fbx": True,
        "assets/3d/arona/Arona_1.01/texture/body_D.png": True,
        "assets/3d/arona/MODEL_IMPORT_MANIFEST.md": False,
        "assets/3d/ASSET_VERSION.json": False,
    }
    for rel, should_ignore in cases.items():
        proc = subprocess.run(["git", "check-ignore", "-q", rel], cwd=str(REPO), capture_output=True)
        ignored = proc.returncode == 0
        assert ignored is should_ignore, "%s: expected ignored=%s got %s" % (rel, should_ignore, ignored)


# ── 附：版本文件格式与路径越界防护 ───────────────────────────
def test_14_version_parsing_and_traversal(tmp_path):
    with pytest.raises(ValueError):
        parse_version({"schema": 1})
    pack = tmp_path / "pack"
    _write_pack(pack, rel="../outside.pmx")
    res = ModelAssetProvider(repo_asset_root=pack, env={}).resolve()
    assert not res.ok
    assert res.status is AssetStatus.INVALID_VERSION
    assert "越出" in res.message

def test_15_missing_version_file(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    res = ModelAssetProvider(repo_asset_root=pack, env={}).resolve()
    assert not res.ok and res.status is AssetStatus.MISSING_VERSION

def test_16_invalid_version_json(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "ASSET_VERSION.json").write_text("{not json", encoding="utf-8")
    res = ModelAssetProvider(repo_asset_root=pack, env={}).resolve()
    assert not res.ok and res.status is AssetStatus.INVALID_VERSION
