"""runtime.3d.asset_provider — 3D 模型资源的唯一发现与校验入口。

设计依据（Phase 0 冻结口径）：
  * ADR-004 §3.2  单一权威模型文件路径（逻辑权威 = ASSET_VERSION.json）
  * ADR-004 §3.3  U 盘路径依赖修复（ModelAssetProvider + Repo/External Provider）
  * ADR-004 §9    资源身份 / 物理位置 / 分发 三层口径（身份唯一、位置可替换）
  * ADR-006 §2.1  R-1「模型加载」职责归属 3D Runtime

M0 约束：
  * 只读：不复制、不移动、不覆盖任何模型资产；
  * 纯 stdlib，无 Qt 依赖，可被 CLI / 测试 / 渲染层共享；
  * 所有来源（ARONA_3D_MODEL_DIR / 仓库默认）都必须通过 SHA256 校验；
  * 校验失败一律返回失败结果（不抛异常给 UI），由调用方降级 2D；
  * 不接受任何盘符硬编码；路径只能来自版本文件的相对路径 + 根目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

ENV_MODEL_DIR = "ARONA_3D_MODEL_DIR"
VERSION_FILENAME = "ASSET_VERSION.json"
DEFAULT_MODEL_ID = "arona_1.01"

_CHUNK = 1024 * 1024
# (resolved_path, size, mtime_ns) -> sha256；避免每帧/每次启动重复哈希 19MB
_SHA_CACHE: Dict[Tuple[str, int, int], str] = {}


class AssetStatus(str, Enum):
    """资源解析结果状态。仅 OK 表示可加载。"""

    OK = "ok"
    MISSING_VERSION = "missing_version"
    INVALID_VERSION = "invalid_version"
    MISSING_MODEL = "missing_model"
    HASH_MISMATCH = "hash_mismatch"
    IO_ERROR = "io_error"


class ModelAssetError(RuntimeError):
    """resolve() 失败时 require() 抛出；携带完整诊断。"""

    def __init__(self, resolution: "AssetResolution"):
        super().__init__(resolution.message)
        self.resolution = resolution
        self.code = resolution.status


def sha256_file(path: Path) -> str:
    """分块计算 SHA256，带 (size, mtime) 缓存。只读，不修改文件。"""
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), int(st.st_size), int(st.st_mtime_ns))
    cached = _SHA_CACHE.get(key)
    if cached:
        return cached
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while True:
            buf = fh.read(_CHUNK)
            if not buf:
                break
            h.update(buf)
    digest = h.hexdigest()
    _SHA_CACHE[key] = digest
    return digest


def clear_hash_cache() -> None:
    _SHA_CACHE.clear()


@dataclass(frozen=True)
class ModelEntry:
    model_id: str
    path: str          # 相对资源根目录（POSIX 分隔符）
    sha256: str        # 小写十六进制
    bytes: int = 0


@dataclass(frozen=True)
class AssetVersion:
    schema: int
    default_model_id: str
    models: Mapping[str, ModelEntry]
    manifest_path: Optional[str]
    manifest_files: int
    license_status: str
    raw: Mapping[str, object]


@dataclass(frozen=True)
class AssetResolution:
    ok: bool
    status: AssetStatus
    message: str
    model_id: str = DEFAULT_MODEL_ID
    path: Optional[Path] = None
    model_dir: Optional[Path] = None
    root: Optional[Path] = None
    source: str = ""            # "env" | "repo" | "explicit"
    sha256: str = ""
    bytes: int = 0
    license_status: str = "UNVERIFIED"
    warnings: Tuple[str, ...] = ()

    def require(self) -> Path:
        if not self.ok or self.path is None:
            raise ModelAssetError(self)
        return self.path

    def summary(self) -> str:
        if self.ok and self.path is not None:
            return "OK [%s] %s (sha256=%s...)" % (self.source, self.path, self.sha256[:12])
        return "FAILED [%s] %s" % (self.status.value, self.message)


_MANIFEST_ROW = re.compile(
    r"^\|\s*`(?P<rel>[^`]+)`\s*\|\s*(?P<bytes>\d+)\s*\|\s*`(?P<sha>[0-9a-fA-F]{64})`\s*\|\s*$"
)


def default_repo_asset_root() -> Path:
    """默认资源根：<repo>/assets/3d（本文件位于 <repo>/runtime/3d/）。"""
    return Path(__file__).resolve().parents[2] / "assets" / "3d"


def parse_version(data: object, source: str = "") -> AssetVersion:
    """解析 ASSET_VERSION.json；格式非法抛 ValueError（含原因）。"""
    if not isinstance(data, dict):
        raise ValueError("%s: 顶层必须是 JSON 对象" % (source or VERSION_FILENAME))
    schema = int(data.get("schema", 1) or 1)
    default_model_id = str(data.get("default_model_id") or DEFAULT_MODEL_ID)
    raw_models = data.get("models")
    models: Dict[str, ModelEntry] = {}
    if isinstance(raw_models, dict) and raw_models:
        for mid, item in raw_models.items():
            if not isinstance(item, dict):
                raise ValueError("%s: models[%s] 必须是对象" % (source, mid))
            rel = item.get("path")
            sha = item.get("sha256")
            if not rel or not sha:
                raise ValueError("%s: models[%s] 缺少 path/sha256" % (source, mid))
            models[str(mid)] = ModelEntry(
                model_id=str(mid),
                path=str(rel).replace("\\", "/"),
                sha256=str(sha).lower(),
                bytes=int(item.get("bytes", 0) or 0),
            )
    else:
        # 兼容单模型简写：{"pmx": {"path":..., "sha256":..., "bytes":...}}
        pmx = data.get("pmx")
        if not isinstance(pmx, dict) or not pmx.get("path") or not pmx.get("sha256"):
            raise ValueError("%s: 缺少 models 或 pmx 定义" % (source or VERSION_FILENAME))
        models[default_model_id] = ModelEntry(
            model_id=default_model_id,
            path=str(pmx["path"]).replace("\\", "/"),
            sha256=str(pmx["sha256"]).lower(),
            bytes=int(pmx.get("bytes", 0) or 0),
        )
    manifest = data.get("manifest")
    manifest_path = None
    manifest_files = 0
    if isinstance(manifest, dict) and manifest.get("path"):
        manifest_path = str(manifest["path"]).replace("\\", "/")
        manifest_files = int(manifest.get("files", 0) or 0)
    lic = data.get("license")
    if isinstance(lic, dict):
        license_status = str(lic.get("status") or "UNVERIFIED").upper()
    else:
        license_status = "UNVERIFIED"
    return AssetVersion(
        schema=schema,
        default_model_id=default_model_id,
        models=models,
        manifest_path=manifest_path,
        manifest_files=manifest_files,
        license_status=license_status,
        raw=data,
    )


class ModelAssetProvider:
    """唯一的模型资源发现入口。

    发现顺序（ADR-004 §9.2）：
      1. 显式传入的 root（测试/工具用）；
      2. 环境变量 ARONA_3D_MODEL_DIR（外部资源包）；
      3. 仓库默认 <repo>/assets/3d；
      4. 全部失败 → 返回失败结果（调用方降级 2D）。

    任何来源都必须通过 ASSET_VERSION.json 声明的大小 + SHA256 校验。
    """

    def __init__(
        self,
        repo_asset_root: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
        explicit_root: Optional[Path] = None,
    ):
        self._repo_root = Path(repo_asset_root) if repo_asset_root else default_repo_asset_root()
        self._env = dict(os.environ if env is None else env)
        self._explicit = Path(explicit_root) if explicit_root else None

    # ── 根目录 ───────────────────────────────────────────────
    def roots(self) -> List[Tuple[str, Path]]:
        out: List[Tuple[str, Path]] = []
        if self._explicit is not None:
            out.append(("explicit", self._explicit))
        env_dir = (self._env.get(ENV_MODEL_DIR) or "").strip()
        if env_dir:
            out.append(("env", Path(env_dir).expanduser()))
        out.append(("repo", self._repo_root))
        seen: set = set()
        uniq: List[Tuple[str, Path]] = []
        for src, p in out:
            key = str(p).lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append((src, p))
        return uniq

    def version_for(self, root: Path) -> Tuple[Optional[AssetVersion], str, AssetStatus]:
        """返回 (version, 错误说明, 状态)。区分"缺文件"与"格式非法"。"""
        vf = Path(root) / VERSION_FILENAME
        if not vf.is_file():
            return None, "缺少 %s：%s" % (VERSION_FILENAME, vf), AssetStatus.MISSING_VERSION
        try:
            data = json.loads(vf.read_text(encoding="utf-8"))
        except OSError as exc:
            return None, "读取 %s 失败：%s" % (vf, exc), AssetStatus.IO_ERROR
        except ValueError as exc:
            return None, "%s 不是合法 JSON：%s" % (vf, exc), AssetStatus.INVALID_VERSION
        try:
            return parse_version(data, str(vf)), "", AssetStatus.OK
        except ValueError as exc:
            return None, str(exc), AssetStatus.INVALID_VERSION

    # ── 解析 ─────────────────────────────────────────────────
    def resolve(self, model_id: Optional[str] = None) -> AssetResolution:
        warnings: List[str] = []
        first_failure: Optional[AssetResolution] = None
        roots = self.roots()
        if not roots:
            return AssetResolution(
                ok=False,
                status=AssetStatus.MISSING_VERSION,
                message="没有可用资源根目录（未设置 %s，且仓库默认根不存在）" % ENV_MODEL_DIR,
            )
        for source, root in roots:
            res = self._resolve_root(source, root, model_id, warnings)
            if res.ok:
                return res
            warnings.append("%s: %s" % (source, res.message))
            if first_failure is None:
                first_failure = res
        assert first_failure is not None
        return AssetResolution(
            ok=False,
            status=first_failure.status,
            message="所有资源位置均不可用；最后一次尝试：%s" % first_failure.message,
            model_id=first_failure.model_id,
            warnings=tuple(warnings),
        )

    def _resolve_root(
        self,
        source: str,
        root: Path,
        model_id: Optional[str],
        warnings: List[str],
    ) -> AssetResolution:
        version, err, vstatus = self.version_for(root)
        if version is None:
            return AssetResolution(
                ok=False,
                status=vstatus,
                message=err,
                root=root,
                source=source,
            )
        mid = model_id or version.default_model_id
        entry = version.models.get(mid)
        if entry is None:
            return AssetResolution(
                ok=False,
                status=AssetStatus.INVALID_VERSION,
                message="版本文件未声明 model_id=%s（可用：%s）" % (mid, ", ".join(sorted(version.models)) or "无"),
                model_id=mid,
                root=root,
                source=source,
            )
        target = (Path(root) / entry.path).resolve()
        try:
            root_resolved = Path(root).resolve()
        except OSError:
            root_resolved = Path(root)
        try:
            target.relative_to(root_resolved)
        except ValueError:
            return AssetResolution(
                ok=False,
                status=AssetStatus.INVALID_VERSION,
                message="版本文件中的路径越出资源根目录：%s" % entry.path,
                model_id=mid,
                root=root,
                source=source,
            )
        if not target.is_file():
            return AssetResolution(
                ok=False,
                status=AssetStatus.MISSING_MODEL,
                message="模型文件不存在：%s（根=%s，来源=%s）" % (target, root, source),
                model_id=mid,
                root=root,
                source=source,
            )
        try:
            size = target.stat().st_size
            digest = sha256_file(target)
        except OSError as exc:
            return AssetResolution(
                ok=False,
                status=AssetStatus.IO_ERROR,
                message="读取模型失败：%s" % exc,
                model_id=mid,
                root=root,
                source=source,
            )
        if entry.bytes and size != entry.bytes:
            return AssetResolution(
                ok=False,
                status=AssetStatus.HASH_MISMATCH,
                message="模型大小不符：期望 %d 字节，实际 %d 字节（%s）" % (entry.bytes, size, target),
                model_id=mid,
                root=root,
                source=source,
                sha256=digest,
                bytes=size,
                license_status=version.license_status,
            )
        if digest.lower() != entry.sha256.lower():
            return AssetResolution(
                ok=False,
                status=AssetStatus.HASH_MISMATCH,
                message="SHA256 校验失败：期望 %s，实际 %s（%s）" % (entry.sha256, digest, target),
                model_id=mid,
                root=root,
                source=source,
                sha256=digest,
                bytes=size,
                license_status=version.license_status,
            )
        if version.license_status != "VERIFIED":
            warnings.append(
                "资产许可证状态为 %s：仅限本机开发使用，禁止提交 git / 分发（ADR-004 §9.4）"
                % version.license_status
            )
        return AssetResolution(
            ok=True,
            status=AssetStatus.OK,
            message="模型校验通过",
            model_id=mid,
            path=target,
            model_dir=target.parent,
            root=Path(root),
            source=source,
            sha256=digest,
            bytes=size,
            license_status=version.license_status,
            warnings=tuple(warnings),
        )

    # ── 整包校验（tools/verify_3d_assets.py 与启动自检共用）──────
    def verify_pack(self, root: Optional[Path] = None, deep: bool = False):
        """校验 manifest 清单。返回 (ok, rows, message, root)。

        rows: List[(相对路径, 状态, 说明)]，状态属于 {"OK","MISSING","SIZE","SHA"}。
        deep=False 只校验存在性与大小；deep=True 追加 SHA256 校验（慢，仅供 CLI）。
        """
        root = Path(root) if root else self._repo_root
        version, err, _vstatus = self.version_for(root)
        if version is None:
            return False, [], err, root
        if not version.manifest_path:
            return False, [], "版本文件未声明 manifest.path", root
        manifest = root / version.manifest_path
        if not manifest.is_file():
            return False, [], "manifest 不存在：%s" % manifest, root
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError as exc:
            return False, [], "读取 manifest 失败：%s" % exc, root
        rows: List[Tuple[str, str, str]] = []
        for line in text.splitlines():
            m = _MANIFEST_ROW.match(line.strip())
            if not m:
                continue
            rel = m.group("rel")
            want_bytes = int(m.group("bytes"))
            want_sha = m.group("sha").lower()
            # manifest 中的相对路径以 manifest 自身所在目录为基准
            target = (manifest.parent / rel).resolve()
            if not target.is_file():
                rows.append((rel, "MISSING", str(target)))
                continue
            size = target.stat().st_size
            if want_bytes and size != want_bytes:
                rows.append((rel, "SIZE", "期望 %d 实际 %d" % (want_bytes, size)))
                continue
            if deep:
                digest = sha256_file(target).lower()
                if digest != want_sha:
                    rows.append((rel, "SHA", "期望 %s 实际 %s" % (want_sha, digest)))
                    continue
            rows.append((rel, "OK", ""))
        bad = [r for r in rows if r[1] != "OK"]
        expected = version.manifest_files or len(rows)
        msg = "manifest 条目 %d（声明 %d），失败 %d" % (len(rows), expected, len(bad))
        if version.manifest_files and len(rows) != version.manifest_files:
            msg += "；条目数与版本文件声明不一致"
            bad.append(("(manifest)", "COUNT", msg))
        return (len(bad) == 0), rows, msg, root

    def describe(self) -> Dict[str, object]:
        return {
            "roots": [{"source": s, "path": str(p)} for s, p in self.roots()],
            "env": ENV_MODEL_DIR,
            "version_file": VERSION_FILENAME,
            "default_model_id": DEFAULT_MODEL_ID,
        }


def resolve_model_path(model_id: Optional[str] = None, **kwargs) -> AssetResolution:
    """便捷函数：解析一次并返回结果（不抛异常）。"""
    root = kwargs.pop("repo_asset_root", None)
    explicit = kwargs.pop("explicit_root", None)
    env = kwargs.pop("env", None)
    return ModelAssetProvider(repo_asset_root=root, env=env, explicit_root=explicit).resolve(model_id)
