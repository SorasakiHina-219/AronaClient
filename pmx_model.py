# -*- coding: utf-8 -*-
"""
PMX 模型加载（供 3D 渲染使用）

只读原始模型文件，不做任何修改。

包含：
  * PMX 2.0 解析器（顶点 / 面 / 贴图 / 材质 / 骨骼）
  * 供 OpenGL 使用的顶点数据准备（归一化 + 交错数组 + 每材质索引段）
  * 贴图按需降采样加载（内存敏感）

关于 flag 位：PMX 文档里的骨骼 flag 定义与这份模型实际不符，
这里是按「下一条记录必须自洽」扫描反推出来的：
    0x0001 = tail 是骨骼索引
    0x0100 = 付与（索引 + f32）
    0x0200 = 轴固定
    0x0400 = 外部亲
    0x0800 = 局部轴（两个 vec3）
    0x0020 = IK

关于 UV：PMX 沿用 DirectX 约定，V 轴朝下（v=0 在贴图顶部），
因此上传贴图后直接使用原始 v，不做 1-v 翻转。
"""
from __future__ import annotations

import struct
from pathlib import Path

ENC = "utf-16-le"


class PmxError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i8(self):
        v = struct.unpack_from("<b", self.d, self.p)[0]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def i16(self):
        v = struct.unpack_from("<h", self.d, self.p)[0]
        self.p += 2
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def vec(self, n):
        v = struct.unpack_from("<%df" % n, self.d, self.p)
        self.p += 4 * n
        return v

    def skip(self, n):
        self.p += n

    def text(self):
        n = self.i32()
        raw = self.d[self.p:self.p + n]
        self.p += n
        return raw.decode(ENC, errors="replace")


class _Idx:
    def __init__(self, r, size, unsigned_vertex=False):
        self.r = r
        self.size = size
        self.unsigned_vertex = unsigned_vertex

    def __call__(self, vertex=False):
        s = self.size
        if s == 1:
            return self.r.u8() if (vertex and self.unsigned_vertex) else self.r.i8()
        if s == 2:
            return self.r.u16() if (vertex and self.unsigned_vertex) else self.r.i16()
        return self.r.u32() if (vertex and self.unsigned_vertex) else self.r.i32()


class PmxModel:
    """解析结果容器。"""

    def __init__(self):
        self.positions = []
        self.normals = []
        self.uvs = []
        self.faces = []
        self.materials = []
        self.textures = []
        self.bones = []
        self.add_uv = 0


def load(path) -> PmxModel:
    data = Path(path).read_bytes()

    if data[:4] != b"PMX ":
        raise PmxError(f"不是 PMX 文件: {data[:4]!r}")

    r = _Reader(data)
    r.p = 4
    version = r.f32()
    gcount = r.u8()

    if gcount < 8:
        raise PmxError(f"globals 数量异常: {gcount}")

    encoding = r.u8()
    add_uv = r.u8()
    v_idx, t_idx, m_idx, b_idx, mo_idx, rb_idx = (r.u8() for _ in range(6))
    for _ in range(gcount - 8):
        r.u8()

    m = PmxModel()
    m.version = version
    m.add_uv = add_uv
    m.encoding = "utf-16-le" if encoding == 0 else "utf-8"

    m.name = r.text()
    m.name_en = r.text()
    m.comment = r.text()
    m.comment_en = r.text()

    read_v = _Idx(r, v_idx, unsigned_vertex=True)
    read_t = _Idx(r, t_idx)
    read_b = _Idx(r, b_idx)
    read_mo = _Idx(r, mo_idx)
    read_rb = _Idx(r, rb_idx)

    # ---------------- 顶点 ----------------
    vcount = r.i32()
    positions = m.positions
    normals = m.normals
    uvs = m.uvs
    weights = []                                    # 供后续蒙皮使用

    import time as _time

    for _vi in range(vcount):
        # 每 16384 个顶点显式让出 GIL：
        # 这是纯 Python 热循环，跑在后台线程里时会把主线程（界面）饿住。
        if (_vi & 0x3FFF) == 0 and _vi:
            _time.sleep(0.001)

        positions.append(r.vec(3))
        normals.append(r.vec(3))
        uvs.append(r.vec(2))
        r.skip(16 * add_uv)

        wtype = r.u8()
        w = []
        if wtype == 0:
            w = [(read_b(), 1.0)]
        elif wtype == 1:
            b1 = read_b(); b2 = read_b(); k = r.f32()
            w = [(b1, 1.0 - k), (b2, k)]
        elif wtype == 2:
            bs = [read_b() for _ in range(4)]
            ws = r.vec(4)
            w = list(zip(bs, ws))
        elif wtype == 3:                            # SDEF 近似按 BDEF2 处理
            b1 = read_b(); b2 = read_b(); k = r.f32()
            r.skip(12 + 12 + 12)
            w = [(b1, 1.0 - k), (b2, k)]
        elif wtype == 4:
            bs = [read_b() for _ in range(4)]
            ws = r.vec(4)
            w = list(zip(bs, ws))
        else:
            raise PmxError(f"未知权重类型 {wtype}")
        weights.append(w)

        r.f32()                                     # edge scale

    m.weights = weights

    # ---------------- 面 ----------------
    icount = r.i32()
    if icount % 3:
        raise PmxError(f"面索引数不是 3 的倍数: {icount}")
    m.faces = [read_v(vertex=True) for _ in range(icount)]

    # ---------------- 贴图 ----------------
    tcount = r.i32()
    m.textures = [r.text() for _ in range(tcount)]

    # ---------------- 材质 ----------------
    mcount = r.i32()
    mats = []
    for _ in range(mcount):
        mat = {}
        mat["name"] = r.text()
        mat["name_en"] = r.text()
        mat["diffuse"] = r.vec(4)
        mat["specular"] = r.vec(3)
        mat["specular_strength"] = r.f32()
        mat["ambient"] = r.vec(3)
        mat["draw_flag"] = r.u8()
        mat["edge_color"] = r.vec(4)
        mat["edge_size"] = r.f32()
        mat["texture_index"] = read_t()
        mat["sphere_index"] = read_t()
        mat["sphere_mode"] = r.u8()
        sharing = r.u8()
        mat["toon_index"] = read_t() if sharing == 0 else r.u8()
        mat["comment"] = r.text()
        mat["face_count"] = r.i32()
        mats.append(mat)
    m.materials = mats

    # ---------------- 骨骼 ----------------
    bcount = r.i32()
    bones = []
    for _ in range(bcount):
        b = {}
        b["name"] = r.text()
        b["name_en"] = r.text()
        b["position"] = r.vec(3)
        b["parent"] = read_b()
        b["layer"] = r.i32()
        flags = r.u16()
        b["flags"] = flags

        if flags & 0x0001:
            b["tail"] = read_b()
        else:
            b["tail"] = r.vec(3)

        if flags & 0x0100:
            b["grant_parent"] = read_b()
            b["grant_weight"] = r.f32()
        if flags & 0x0200:
            b["fixed_axis"] = r.vec(3)
        if flags & 0x0800:
            b["local_x"] = r.vec(3)
            b["local_z"] = r.vec(3)
        if flags & 0x0400:
            b["external_key"] = r.i32()
        if flags & 0x0020:
            b["ik_target"] = read_b()
            b["ik_loop"] = r.f32()
            b["ik_limit"] = r.f32()
            n = r.i32()
            for _ in range(n):
                read_b()
                if r.u8():
                    r.vec(3); r.vec(3)

        bones.append(b)
    m.bones = bones

    # 一致性自检
    total = sum(x["face_count"] for x in mats)
    if total != len(m.faces):
        raise PmxError(f"材质面数合计 {total} 与总面数 {len(m.faces)} 不一致，解析可能错位")

    return m


# ============================================================
# 供 OpenGL 使用的数据准备
# ============================================================

def build_gpu_data(model: PmxModel, positions=None, normals=None):
    """
    返回 (vertex_bytes, batches, info)

    vertex_bytes: 交错顶点数据 [x,y,z, nx,ny,nz, u,v] float32
                  **已按索引展开**（去索引），这样绘制时只用 glDrawArrays，
                  因为 PySide6 6.11.2 的 glDrawElements 绑定有 bug，
                  传 0 或 None 都会误报 wrong argument values。

    positions / normals: 若提供则使用它们（蒙皮变形后的结果），
                         否则用模型原始数据（T 字姿势）。

    batches     : [{"mat_index", "first", "count", "mat"}, ...]
                  只包含可见材质（diffuse alpha > 0）
    info        : 包围盒、归一化参数、统计
    """
    import numpy as np

    P = np.asarray(model.positions if positions is None else positions, dtype=np.float32)
    N = np.asarray(model.normals if normals is None else normals, dtype=np.float32)
    UV = np.asarray(model.uvs, dtype=np.float32)

    x0, x1 = float(P[:, 0].min()), float(P[:, 0].max())
    y0, y1 = float(P[:, 1].min()), float(P[:, 1].max())
    z0, z1 = float(P[:, 2].min()), float(P[:, 2].max())

    # 归一化：高度 -> 1.0，原点移到几何中心
    height = max(1e-6, y1 - y0)
    scale = 1.0 / height
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    cz = (z0 + z1) / 2.0

    Pn = (P - np.array([cx, cy, cz], dtype=np.float32)) * scale

    interleaved = np.empty((len(Pn), 8), dtype=np.float32)
    interleaved[:, 0:3] = Pn
    interleaved[:, 3:6] = N
    interleaved[:, 6:8] = UV

    faces = np.asarray(model.faces, dtype=np.int64).reshape(-1, 3)

    chunks = []
    batches = []
    cursor = 0
    first = 0
    visible_tris = 0

    for mi, mat in enumerate(model.materials):
        n = mat["face_count"] // 3
        if n <= 0:
            continue
        blk = faces[cursor:cursor + n]
        cursor += n

        if mat["diffuse"][3] <= 0.001:
            continue                                     # MMD 隐藏材质

        vidx = blk.reshape(-1)
        chunks.append(interleaved[vidx])

        batches.append({
            "mat_index": mi,
            "first": first,
            "count": int(vidx.size),
            "mat": mat,
        })
        first += int(vidx.size)
        visible_tris += n

    vertex_bytes = np.concatenate(chunks, axis=0).tobytes() if chunks else b""

    info = {
        "bbox": (x0, x1, y0, y1, z0, z1),
        "norm_w": (x1 - x0) * scale,
        "norm_h": 1.0,
        "norm_d": (z1 - z0) * scale,
        "scale": scale,
        "aspect": (x1 - x0) * scale,
        "vertex_count": int(len(Pn)),
        "triangle_count": int(len(faces)),
        "batch_count": len(batches),
        "visible_triangles": visible_tris,
        "draw_vertices": first,
    }

    return vertex_bytes, batches, info


def load_textures(model: PmxModel, base_dir, max_size=1024):
    """
    按需降采样加载贴图，返回 {texture_index: QImage(RGBA8888)}。

    用 QImageReader.setScaledSize 在「解码阶段」就缩小，
    避免先把 4096x4096 的整图读进内存（单张 RGBA 就有 64MB）。
    """
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage, QImageReader

    out = {}
    report = []

    for i, rel in enumerate(model.textures):
        # 让出 GIL：解码 4096x4096 PNG 是一次很长的 C 调用，
        # 会占住 GIL 导致主线程（界面）卡顿。每张之间短暂 sleep，
        # 给主线程处理事件的机会。实测可把加载期间的界面停顿明显压下来。
        if i > 0:
            import time as _time
            _time.sleep(0.02)

        path = Path(base_dir) / rel
        if not path.is_file():
            report.append((i, rel, "缺失", 0, 0))
            continue

        reader = QImageReader(str(path))
        reader.setAutoTransform(False)
        size = reader.size()
        ow, oh = (size.width(), size.height()) if size.isValid() else (0, 0)

        if ow > max_size or oh > max_size:
            k = max_size / max(ow, oh)
            reader.setScaledSize(QSize(max(1, int(ow * k)), max(1, int(oh * k))))

        img = reader.read()

        if img.isNull():
            report.append((i, rel, f"解码失败: {reader.errorString()}", ow, oh))
            continue

        img = img.convertToFormat(QImage.Format_RGBA8888)

        out[i] = img
        report.append((i, rel, "OK", ow, oh))

    return out, report


def format_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ============================================================
# 骨骼蒙皮（线性混合蒙皮 LBS）
# ============================================================
#
# 坐标系：PMX 为 Y 轴向上、X 向右、Z 向观察者。模型在静止姿势下朝 -Z。
#
# 骨骼变换推导：
#   静止时所有骨骼没有旋转，所以
#       rest[i] = rest[parent] @ T(pos_i - pos_parent) = T(pos_i)
#   摆姿势时在骨骼自身局部坐标系里叠加旋转 R_i：
#       posed[i] = posed[parent] @ T(pos_i - pos_parent) @ R_i
#   蒙皮矩阵：
#       M_i = posed[i] @ inv(rest[i]) = posed[i] @ T(-pos_i)
#   顶点：
#       v' = Σ_k w_k · (M_{b_k} · v)
#
# 这样在「不施加任何旋转」时 M_i 恒为单位矩阵，姿势可无损回退到 T 字基准。


def find_bone_index(model, name, exact=True):
    """按名字找骨骼索引；找不到返回 None。"""
    for i, b in enumerate(model.bones):
        if (b["name"] == name) if exact else (name in b["name"]):
            return i
    return None


def build_skin_arrays(model):
    """把顶点权重整理成定长 numpy 数组（最多 4 个骨骼），便于向量化。"""
    import numpy as np

    n = len(model.weights)
    bi = np.zeros((n, 4), dtype=np.int32)
    bw = np.zeros((n, 4), dtype=np.float32)

    for i, w in enumerate(model.weights):
        for k, (b, wt) in enumerate(w[:4]):
            if b is not None and 0 <= b < len(model.bones):
                bi[i, k] = b
                bw[i, k] = wt

    s = bw.sum(axis=1, keepdims=True)
    s[s <= 0] = 1.0
    bw = bw / s

    return bi, bw


def _T(v):
    import numpy as np
    m = np.eye(4, dtype=np.float64)
    m[0, 3], m[1, 3], m[2, 3] = float(v[0]), float(v[1]), float(v[2])
    return m


def _rot(axis, deg):
    import math

    import numpy as np
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4, dtype=np.float64)
    if axis == "x":
        m[1, 1], m[1, 2], m[2, 1], m[2, 2] = c, -s, s, c
    elif axis == "y":
        m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    else:                                   # z
        m[0, 0], m[0, 1], m[1, 0], m[1, 1] = c, -s, s, c
    return m


def compute_skin_matrices(model, pose=None):
    """
    pose: {骨骼索引: [(轴, 角度), ...]}，轴为 "x"/"y"/"z"。
          不传或传空 => 全部单位矩阵（即原始 T 字姿势）。

    返回 (nb, 4, 4) 的蒙皮矩阵。
    """
    import numpy as np

    nb = len(model.bones)
    pos = [np.asarray(b["position"], dtype=np.float64) for b in model.bones]
    parent = [b["parent"] for b in model.bones]

    rest = [None] * nb
    posed = [None] * nb

    # 关键：rest 与 posed 必须走两条独立的递归。
    #
    # 之前这里犯过一个很隐蔽的错误：用 posed[parent] 去推 rest[i]，
    # 结果祖先的旋转也混进了 rest，M_i = posed_i @ inv(rest_i)
    # 会把旋转自己抵消掉 —— 表现为「被旋转的骨骼有变化，子骨骼全无变化」。
    #
    # 另外：PMX 并不保证「父骨骼索引 < 子骨骼索引」，
    # 所以要按依赖递归解析（带记忆化），不能简单顺序推导。

    def resolve_rest(i):
        if rest[i] is not None:
            return rest[i]

        p = parent[i]
        if p is None or p < 0 or p >= nb or p == i:
            rest[i] = _T(pos[i])
        else:
            rest[i] = resolve_rest(p) @ _T(pos[i] - pos[p])

        return rest[i]

    def resolve_posed(i):
        if posed[i] is not None:
            return posed[i]

        p = parent[i]
        if p is None or p < 0 or p >= nb or p == i:
            base = _T(pos[i])
        else:
            base = resolve_posed(p) @ _T(pos[i] - pos[p])

        r = np.eye(4, dtype=np.float64)
        if pose and i in pose:
            for axis, deg in pose[i]:
                r = r @ _rot(axis, deg)

        posed[i] = base @ r
        return posed[i]

    for i in range(nb):
        resolve_rest(i)
        resolve_posed(i)

    M = np.empty((nb, 4, 4), dtype=np.float64)
    for i in range(nb):
        M[i] = posed[i] @ np.linalg.inv(rest[i])

    return M


def skin(model, M, bi, bw, chunk=16384):
    """按蒙皮矩阵变形顶点，返回 (positions, normals)，float32。"""
    import numpy as np

    P = np.asarray(model.positions, dtype=np.float64)
    N = np.asarray(model.normals, dtype=np.float64)
    n = len(P)

    outP = np.empty((n, 3), dtype=np.float32)
    outN = np.empty((n, 3), dtype=np.float32)

    for s in range(0, n, chunk):
        e = min(n, s + chunk)
        b = bi[s:e]                                     # (m,4)
        w = bw[s:e]                                     # (m,4)

        blended = np.einsum("mk,mkij->mij", w, M[b])     # (m,4,4)

        ph = np.concatenate([P[s:e], np.ones((e - s, 1))], axis=1)
        outP[s:e] = np.einsum("mij,mj->mi", blended, ph)[:, :3]

        nn = np.einsum("mij,mj->mi", blended[:, :3, :3], N[s:e])
        ln = np.linalg.norm(nn, axis=1, keepdims=True)
        ln[ln < 1e-9] = 1.0
        outN[s:e] = nn / ln

    return outP, outN
