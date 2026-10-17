# -*- coding: utf-8 -*-
"""
AronaClient 阶段 3 —— 独立 3D 渲染原型（不集成，不影响现有项目）

目标：验证在 Windows 10 + Intel HD Graphics 上，
      能否用 PySide6 的 QOpenGLWidget + GLSL 330
      实时渲染真实 Arona PMX 模型，并且背景真正透明、与桌面融合。

用法：
    .venv\\Scripts\\python.exe arona3d_proto.py              # 打开原型窗口（可拖动旋转）
    .venv\\Scripts\\python.exe arona3d_proto.py --selftest   # 自动透明性实测 + 截图 + 指标

交互：
    左键拖动 = 旋转模型     滚轮 = 缩放     右键 = 关闭

不会写入模型目录；只在 logs/ 下写测试输出。
"""
from __future__ import annotations

import ctypes
import math
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 路径
APP_DIR = Path(__file__).resolve().parent
MODEL_DIR = Path(r"E:\Arona_1.01")
PMX_PATH = MODEL_DIR / "Arona_1.01.pmx"
LOG_DIR = APP_DIR / "logs"

# ---------------------------------------------------------------- 参数
DISPLAY_HEIGHT = 460          # 模型在屏幕上的高度（像素）
TEXTURE_MAX = 1024            # 贴图降采样上限
MARGIN = 0.02

SELFTEST = "--selftest" in sys.argv
SELFTEST_HOLD = 2.5           # 自检时窗口停留时间（秒）


def _arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


SHOT = _arg("--shot")
ROTATECHECK = "--rotatecheck" in sys.argv
YAW = float(_arg("--yaw", "180"))
HEIGHT = int(_arg("--height", str(DISPLAY_HEIGHT)))


def parse_pose(spec):
    """--pose "left:z:-68;right:z:68" -> {name: [(axis, deg)]}"""
    out = {}
    if not spec:
        return out
    for item in spec.split(";"):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3:
            print(f"[pose] skip unparsable item: {item!r}")
            continue
        name, axis, deg = parts[0].strip(), parts[1].strip(), parts[2].strip()
        out.setdefault(name, []).append((axis, float(deg)))
    return out


# 内置姿势预设（避免命令行传中日文骨骼名带来的编码问题）
BUILTIN_POSES = {
    "tpose": {},
    # 沿手臂链条分配旋转，尽量接近真人站姿：
    # 肩部只转一点，上臂承担主要角度，肘部略微弯曲。
    # 这样挂在「左肩」下的袖子骨骼（左袖）也能跟着走，减少撕裂。
    "natural": {
        "左肩": [("z", -10)],
        "左腕": [("z", -32)],
        "左ひじ": [("z", -6)],
        "右肩": [("z", 10)],
        "右腕": [("z", 32)],
        "右ひじ": [("z", 6)],
    },
    "armsdown": {
        "左肩": [("z", -14)],
        "左腕": [("z", -40)],
        "左ひじ": [("z", -8)],
        "右肩": [("z", 14)],
        "右腕": [("z", 40)],
        "右ひじ": [("z", 8)],
    },
}

_pose_arg = _arg("--pose")
POSE_SPEC = BUILTIN_POSES[_pose_arg] if _pose_arg in BUILTIN_POSES else parse_pose(_pose_arg)


# ---- 阶段 C：画面质量开关（各自独立）----
TOON_STEPS = float(_arg("--toon", "0"))       # 0 = 关闭（平滑明暗）
OUTLINE = int(_arg("--outline", "0"))         # 0 = 关闭
EDGE_SCALE = float(_arg("--edge-scale", "0.40"))
SPHERE = int(_arg("--sphere", "0"))           # 0 = 关闭


# ================================================================
# 内存 / 指标
# ================================================================

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def system_memory():
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return st.ullTotalPhys, st.ullAvailPhys


def process_working_set():
    """返回 (当前工作集, 峰值工作集)，字节。失败返回 (0, 0)。"""
    try:
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), ctypes.c_ulong]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int

        c = PROCESS_MEMORY_COUNTERS()
        c.cb = ctypes.sizeof(c)

        h = k32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return 0, 0
        return int(c.WorkingSetSize), int(c.PeakWorkingSetSize)
    except Exception:
        return 0, 0


def mb(n):
    return f"{n / 1024 / 1024:.1f} MB"


# ================================================================
# OpenGL 部件
# ================================================================

VERT = """
#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aUV;

uniform mat4 uMVP;
uniform mat3 uNormalMat;
uniform float uOutline;      // 1 = 正在画描边遍
uniform float uEdgeSize;     // 描边沿法线外扩量（已归一化）

out vec3 vN;
out vec2 vUV;

void main()
{
    vec3 p = aPos;
    if (uOutline > 0.5) {
        // 描边做法与 MMD 一致：顶点沿法线外扩，再剔除正面只留背面，
        // 于是轮廓外侧形成一圈描边
        p += normalize(aNormal) * uEdgeSize;
    }
    gl_Position = uMVP * vec4(p, 1.0);
    vN = uNormalMat * aNormal;
    vUV = aUV;
}
"""

FRAG = """
#version 330 core
in vec3 vN;
in vec2 vUV;

uniform sampler2D uTex;
uniform sampler2D uSphere;

uniform vec4 uDiffuse;
uniform vec3 uEdgeColor;
uniform vec3 uLightDir;

uniform float uHasTex;
uniform float uOutline;
uniform float uToonSteps;    // 0 = 平滑；>=2 = 卡通分段
uniform float uToonSoft;     // 分段边缘柔和度
uniform float uSphereOn;     // 总开关
uniform float uSphereMode;   // 0=关 1=乘算 2=加算 3=贴图替换

out vec4 fragColor;

void main()
{
    // ---------- 描边遍 ----------
    if (uOutline > 0.5) {
        fragColor = vec4(uEdgeColor, uDiffuse.a);
        return;
    }

    vec4 base = texture(uTex, vUV);
    vec3 rgb = base.rgb * uDiffuse.rgb;

    // ---------- 球面贴图 ----------
    // 只对 PMX 里 sphere_mode > 0 的材质启用，不盲目套用到全部材质
    if (uSphereOn > 0.5 && uSphereMode > 0.5) {
        vec3 ns = normalize(vN);
        vec2 suv = ns.xy * 0.5 + 0.5;
        vec4 sp = texture(uSphere, suv);
        if (uSphereMode < 1.5) {
            rgb *= sp.rgb;                       // 1 = 乘算
        } else if (uSphereMode < 2.5) {
            rgb += sp.rgb * sp.a;                // 2 = 加算
        } else {
            rgb = mix(rgb, sp.rgb, sp.a);        // 3 = 贴图替换
        }
    }

    // ---------- 明暗 ----------
    vec3 n = normalize(vN);
    float ndl = dot(n, uLightDir);

    float shade;
    if (uToonSteps < 1.5) {
        // 平滑明暗（半兰伯特）—— 关闭卡通时的原行为
        shade = 0.62 + 0.46 * (0.35 + 0.65 * max(ndl, 0.0));
    } else {
        // 卡通分段：把半兰伯特值量化成若干档，档间用 smoothstep 过渡。
        // 同一档内的三角面颜色完全一致，刻面感随之消失。
        float t = clamp(ndl * 0.5 + 0.5, 0.0, 1.0);
        float sc = t * uToonSteps;
        float f = floor(sc);
        float frac = sc - f;
        float q = (f + smoothstep(0.5 - uToonSoft, 0.5 + uToonSoft, frac)) / uToonSteps;
        shade = mix(0.70, 1.10, q);
    }

    float a = base.a * uDiffuse.a;
    if (a < 0.004) discard;
    fragColor = vec4(clamp(rgb * shade, 0.0, 1.0), a);
}
"""

FRAG_DEBUG_UV = """
#version 330 core
in vec3 vN;
in vec2 vUV;
uniform sampler2D uTex;
uniform sampler2D uSphere;
uniform vec4 uDiffuse;
uniform vec3 uEdgeColor;
uniform vec3 uLightDir;
uniform float uHasTex;
uniform float uOutline;
uniform float uToonSteps;
uniform float uToonSoft;
uniform float uSphereOn;
uniform float uSphereMode;
out vec4 fragColor;
void main()
{
    fragColor = vec4(vUV.x, vUV.y, 0.0, 1.0);
}
"""

FRAG_DEBUG_TEX = """
#version 330 core
in vec3 vN;
in vec2 vUV;
uniform sampler2D uTex;
uniform sampler2D uSphere;
uniform vec4 uDiffuse;
uniform vec3 uEdgeColor;
uniform vec3 uLightDir;
uniform float uHasTex;
uniform float uOutline;
uniform float uToonSteps;
uniform float uToonSoft;
uniform float uSphereOn;
uniform float uSphereMode;
out vec4 fragColor;
void main()
{
    fragColor = vec4(texture(uTex, vUV).rgb, 1.0);
}
"""


def build_gl_widget():
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QMatrix4x4, QSurfaceFormat, QVector3D
    from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLShader,
                                  QOpenGLShaderProgram, QOpenGLTexture,
                                  QOpenGLVertexArrayObject)
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    import numpy as np

    import pmx_model

    class GLView(QOpenGLWidget):

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_AlwaysStackOnTop, True)
            self.setAutoFillBackground(False)

            self.model = None
            self.vertex_bytes = b""
            self.batches = []
            self.info = {}
            self.textures = {}
            self.tex_report = []

            self.vbo = None
            self.vao = None
            self.ibos = []
            self.program = None

            # 模型在 PMX 里朝 -Z，相机在 +Z，要转 180 度才是正面
            self._sphere_batches = 0

            self.yaw = 180.0
            self.pitch = 0.0
            self.zoom = 1.0

            self._last = time.monotonic()
            self.frames = 0

            self.timings = {}

        # ---------------- 数据 ----------------

        def load_model(self, pose_spec=None):
            t0 = time.perf_counter()
            self.model = pmx_model.load(PMX_PATH)
            t1 = time.perf_counter()

            positions = None
            normals = None
            self.pose_applied = {}
            self.skin_ms = 0.0

            if pose_spec:
                pose = {}
                for name, rots in pose_spec.items():
                    idx = pmx_model.find_bone_index(self.model, name)
                    if idx is None:
                        idx = pmx_model.find_bone_index(self.model, name, exact=False)
                    if idx is None:
                        print(f"[pose] bone not found: {name!r}")
                        continue
                    pose[idx] = rots
                    self.pose_applied[self.model.bones[idx]["name"]] = rots

                ts = time.perf_counter()
                M = pmx_model.compute_skin_matrices(self.model, pose)
                bi, bw = pmx_model.build_skin_arrays(self.model)
                positions, normals = pmx_model.skin(self.model, M, bi, bw)
                self.skin_ms = (time.perf_counter() - ts) * 1000

            self.vertex_bytes, self.batches, self.info = pmx_model.build_gpu_data(
                self.model, positions, normals)

            # 描边参数：PMX 的 edge_size 是模型单位，乘归一化系数换算到显示空间；
            # 只有勾选了 draw_flag bit4（0x10）的材质才画描边。
            nscale = float(self.info.get("scale", 1.0)) * EDGE_SCALE
            self.edge_info = []
            for b in self.batches:
                mat = b["mat"]
                es = float(mat["edge_size"]) * nscale
                ec = mat["edge_color"]
                draws = bool(mat["draw_flag"] & 0x10) and es > 1e-7
                self.edge_info.append(
                    (es, (float(ec[0]), float(ec[1]), float(ec[2])), draws))

            t2 = time.perf_counter()

            self.textures, self.tex_report = pmx_model.load_textures(
                self.model, MODEL_DIR, max_size=TEXTURE_MAX)
            t3 = time.perf_counter()

            self.timings = {
                "parse": (t1 - t0) * 1000,
                "build": (t2 - t1) * 1000,
                "textures": (t3 - t2) * 1000,
                "total": (t3 - t0) * 1000,
            }
            return self.timings

        # ---------------- GL ----------------

        def _setf(self, name, value):
            """
            设置单个 float uniform。

            不能用 QOpenGLShaderProgram.setUniformValue(location, float)：
            在 PySide6 6.11.2 上它对标量 float 不生效（矩阵 / vec3 / vec4 / int 都正常），
            本阶段所有效果开关都是 float，踩过一次坑，统一走 glUniform1f。
            """
            loc = self.uni.get(name, -1)
            if loc is None or loc < 0:
                return False
            try:
                self._fns().glUniform1f(int(loc), float(value))
                return True
            except Exception:
                return False

        def _fns(self):
            """优先用 3.3 Core 的版本化函数对象。

            QOpenGLFunctions 的 glDrawElements 在 PySide6 6.11 上会误报
            'wrong argument values'，版本化对象没有这个问题。
            """
            if getattr(self, "gl33", None) is not None:
                return self.gl33
            return self.context().functions()

        def initializeGL(self):
            try:
                from PySide6.QtOpenGL import (QOpenGLVersionFunctionsFactory,
                                              QOpenGLVersionProfile)
                prof = QOpenGLVersionProfile()
                prof.setVersion(3, 3)
                prof.setProfile(QSurfaceFormat.CoreProfile)
                self.gl33 = QOpenGLVersionFunctionsFactory.get(prof, self.context())
            except Exception as exc:
                print("[GL] 版本化函数对象不可用:", repr(exc))
                self.gl33 = None

            f = self._fns()
            print("[GL] 使用的函数对象:", type(f).__name__)

            self.program = QOpenGLShaderProgram()
            if not self.program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERT):
                raise RuntimeError("顶点着色器编译失败: " + self.program.log())
            if os.environ.get("ARONA_DEBUG_UV"):
                _frag = FRAG_DEBUG_UV
            elif os.environ.get("ARONA_DEBUG_TEX"):
                _frag = FRAG_DEBUG_TEX
            else:
                _frag = FRAG
            if not self.program.addShaderFromSourceCode(QOpenGLShader.Fragment, _frag):
                raise RuntimeError("片段着色器编译失败: " + self.program.log())
            if not self.program.link():
                raise RuntimeError("着色器链接失败: " + self.program.log())

            print("[GL] 着色器已链接")
            print("[GL] program log:", repr(self.program.log()))

            # 缓存 uniform location。
            # 注意：PySide6 里按「名字」传值的路径会误报 wrong argument values，
            # 所以统一走 location(int) 这一条路。
            self.uni = {}
            for nm in (b"uMVP", b"uNormalMat", b"uLightDir", b"uTex", b"uHasTex", b"uDiffuse",
                       b"uOutline", b"uEdgeSize", b"uEdgeColor",
                       b"uToonSteps", b"uToonSoft", b"uSphere", b"uSphereOn", b"uSphereMode"):
                self.uni[nm] = self.program.uniformLocation(nm)
                print(f"[GL]   uniform {nm!r} -> {self.uni[nm]}")

            self.vao = QOpenGLVertexArrayObject()
            self.vao.create()
            self.vao.bind()

            self.vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            self.vbo.create()
            self.vbo.bind()
            self.vbo.allocate(self.vertex_bytes, len(self.vertex_bytes))

            stride = 8 * 4
            self.program.enableAttributeArray(0)
            self.program.setAttributeBuffer(0, 0x1406, 0, 3, stride)      # GL_FLOAT
            self.program.enableAttributeArray(1)
            self.program.setAttributeBuffer(1, 0x1406, 12, 3, stride)
            self.program.enableAttributeArray(2)
            self.program.setAttributeBuffer(2, 0x1406, 24, 2, stride)

            self.vbo.release()
            self.vao.release()

            # 贴图
            self.gl_textures = {}
            for ti, img in self.textures.items():
                try:
                    tex = QOpenGLTexture(img)
                    tex.setMinificationFilter(QOpenGLTexture.Linear)
                    tex.setMagnificationFilter(QOpenGLTexture.Linear)
                    tex.setWrapMode(QOpenGLTexture.Repeat)
                    self.gl_textures[ti] = tex
                    if ti < 3 or ti == 5:
                        print(f"[GL] 贴图 {ti}: QImage {img.width()}x{img.height()} "
                              f"fmt={img.format()} null={img.isNull()} -> "
                              f"tex created={tex.isCreated()} "
                              f"{tex.width()}x{tex.height()} "
                              f"fmt={tex.format()} levels={tex.mipLevels()}")
                except Exception as exc:
                    print(f"贴图 {ti} 上传失败: {exc!r}")

            # 球面贴图：只上传 PMX 里 sphere_mode > 0 的材质真正引用的那几张。
            # 本模型只有 hair_back / hair_ura 用（mode=2 加算，贴图 spa2_D.png）。
            sphere_ids = sorted({
                mat["sphere_index"]
                for mat in self.model.materials
                if mat["diffuse"][3] > 0.001
                and mat["sphere_mode"] > 0
                and 0 <= mat["sphere_index"] < len(self.model.textures)
            })

            self.gl_sphere = {}
            for si in sphere_ids:
                img = self.textures.get(si)
                if img is None:
                    continue
                try:
                    t = QOpenGLTexture(img)
                    t.setMinificationFilter(QOpenGLTexture.Linear)
                    t.setMagnificationFilter(QOpenGLTexture.Linear)
                    t.setWrapMode(QOpenGLTexture.ClampToEdge)
                    self.gl_sphere[si] = t
                    print(f"[GL] 球面贴图 [{si}] 已上传: {self.model.textures[si]} "
                          f"({img.width()}x{img.height()})")
                except Exception as exc:
                    print(f"[GL] 球面贴图 [{si}] 上传失败: {exc!r}")

            if not sphere_ids:
                print("[GL] 本模型没有启用球面贴图的材质")

            f.glEnable(0x0B71)          # GL_DEPTH_TEST
            f.glDepthFunc(0x0201)       # GL_LESS
            f.glDisable(0x0BE2)         # 先不用 GL_ALPHA_TEST（核心模式已废弃）

            self.gl_info = {
                "vendor": f.glGetString(0x1F00),
                "renderer": f.glGetString(0x1F01),
                "version": f.glGetString(0x1F02),
            }

        def paintGL(self):
            self.frames += 1
            f = self._fns()

            # ★ 关键：清成完全透明（alpha = 0），让桌面透出来
            f.glClearColor(0.0, 0.0, 0.0, 0.0)
            # 清屏深度默认就是 1.0，不必再调 glClearDepthf（3.3 Core 没有这个函数）
            f.glClear(0x00004000 | 0x00000100)      # GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT

            f.glEnable(0x0BE2)                      # GL_BLEND
            f.glBlendFuncSeparate(0x0302, 0x0303, 0x0001, 0x0303)
            f.glEnable(0x0B71)                      # GL_DEPTH_TEST

            if self.program is None or not self.batches:
                return

            if not getattr(self, "_diag_done", False):
                self._diag_done = True
                print(f"[GL] 贴图对象数 = {len(self.gl_textures)}  "
                      f"keys = {sorted(self.gl_textures)}")
                print("[GL] 前 8 个批次的 texture_index / alpha = "
                      + str([(self.batches[i]['mat']['texture_index'],
                              round(self.batches[i]['mat']['diffuse'][3], 2))
                             for i in range(min(8, len(self.batches)))]))
                print(f"[GL] 顶点缓冲字节 = {len(self.vertex_bytes):,}   "
                      f"绘制顶点 = {self.info.get('draw_vertices')}")

            self.program.bind()

            mvp = self._mvp()
            self.program.setUniformValue(self.uni[b"uMVP"], mvp)

            nm = mvp.normalMatrix()
            self.program.setUniformValue(self.uni[b"uNormalMat"], nm)

            self.program.setUniformValue(self.uni[b"uLightDir"], QVector3D(0.30, 0.50, 0.81).normalized())

            # 明暗 / 球面贴图的全局参数（每个材质再单独设 sphere_mode）
            self._setf(b"uToonSteps", TOON_STEPS)
            self._setf(b"uToonSoft", 0.12)
            self._setf(b"uSphereOn", 1.0 if SPHERE else 0.0)
            self._setf(b"uSphereMode", 0.0)
            self.program.setUniformValue(self.uni[b"uSphere"], 1)

            aspect = self.info.get("aspect", 1.0)
            f.glViewport(0, 0, int(self.width() * self.devicePixelRatio()),
                         int(self.height() * self.devicePixelRatio()))

            # 先画不透明（写深度），再画半透明（测深度不写深度）
            order = sorted(range(len(self.batches)),
                           key=lambda i: 0 if self.batches[i]["mat"]["diffuse"][3] >= 0.999 else 1)

            self.vao.bind()

            # ---------------- 描边遍 ----------------
            # MMD 的做法：顶点沿法线外扩 + 剔除正面，于是只在轮廓外侧留下一圈描边
            if OUTLINE:
                f.glEnable(0x0B44)
                f.glCullFace(0x0404)            # GL_FRONT
                f.glFrontFace(0x0901)           # GL_CCW
                f.glDepthMask(True)

                self._setf(b"uOutline", 1.0)
                self._setf(b"uSphereMode", 0.0)

                n_outline = 0
                for i in order:
                    es, ec, draws = self.edge_info[i]
                    if not draws:
                        continue

                    b = self.batches[i]
                    self._setf(b"uEdgeSize", es)
                    self.program.setUniformValue(
                        self.uni[b"uEdgeColor"], float(ec[0]), float(ec[1]), float(ec[2]))
                    self.program.setUniformValue(
                        self.uni[b"uDiffuse"], 0.0, 0.0, 0.0,
                        float(b["mat"]["diffuse"][3]))

                    f.glDrawArrays(0x0004, b["first"], b["count"])
                    n_outline += 1

                self._setf(b"uOutline", 0.0)
                f.glCullFace(0x0405)            # GL_BACK

                if not getattr(self, "_outline_logged", False):
                    self._outline_logged = True
                    print(f"[GL] 描边遍绘制了 {n_outline} 个批次")

            # ---------------- 主遍 ----------------
            for pass_idx in (0, 1):
                for i in order:
                    b = self.batches[i]
                    mat = b["mat"]
                    a = mat["diffuse"][3]
                    if (pass_idx == 0) != (a >= 0.999):
                        continue

                    f.glDepthMask(pass_idx == 0)

                    if mat["draw_flag"] & 0x01:
                        f.glDisable(0x0B44)         # GL_CULL_FACE
                    else:
                        f.glEnable(0x0B44)
                        f.glCullFace(0x0405)        # GL_BACK
                        f.glFrontFace(0x0901)       # GL_CCW

                    ti = mat["texture_index"]
                    gltex = self.gl_textures.get(ti)
                    has = 1.0 if gltex is not None else 0.0
                    if gltex is not None:
                        gltex.bind(0)

                    self.program.setUniformValue(self.uni[b"uTex"], 0)
                    self._setf(b"uHasTex", has)

                    # 球面贴图：只对 sphere_mode > 0 的材质启用
                    smode = int(mat["sphere_mode"])
                    si = int(mat["sphere_index"])
                    stex = self.gl_sphere.get(si) if (SPHERE and smode > 0) else None
                    if stex is not None:
                        stex.bind(1)
                        self._setf(b"uSphereMode", smode)
                        self._sphere_batches += 1
                    else:
                        self._setf(b"uSphereMode", 0.0)

                    d = mat["diffuse"]
                    self.program.setUniformValue(
                        self.uni[b"uDiffuse"],
                        float(d[0]), float(d[1]), float(d[2]), float(d[3]))

                    # 去索引展开后用 glDrawArrays（绕开 PySide6 glDrawElements 的绑定 bug）
                    f.glDrawArrays(0x0004, b["first"], b["count"])

            self.vao.release()
            self.program.release()

            f.glDepthMask(True)

        def resizeGL(self, w, h):
            self._aspect = (w / h) if h else 1.0

        def cleanup(self):
            """在仍持有当前 GL 上下文时释放资源，避免退出时报
            'destroy() called without a current context'。"""
            try:
                self.makeCurrent()

                for t in list(getattr(self, "gl_sphere", {}).values()):
                    try:
                        t.destroy()
                    except Exception:
                        pass
                self.gl_sphere = {}

                for t in list(getattr(self, "gl_textures", {}).values()):
                    try:
                        t.destroy()
                    except Exception:
                        pass
                self.gl_textures = {}

                for attr in ("vbo", "vao"):
                    o = getattr(self, attr, None)
                    if o is not None:
                        try:
                            o.destroy()
                        except Exception:
                            pass
                        setattr(self, attr, None)

                self.doneCurrent()
            except Exception:
                pass

        # ---------------- 相机 ----------------

        def _mvp(self):
            from PySide6.QtGui import QMatrix4x4

            aspect = self.info.get("aspect", 1.0)
            model_w = self.info.get("norm_w", 1.0)
            model_h = self.info.get("norm_h", 1.0)

            # 视图窗口本身按模型宽高比设置，正交投影直接覆盖模型范围
            half_w = model_w / 2.0 / self.zoom
            half_h = model_h / 2.0 / self.zoom

            # 窗口比模型宽时，按宽度对齐
            win_aspect = (self.width() / self.height()) if self.height() else 1.0
            if win_aspect > aspect:
                half_w = half_h * win_aspect
            else:
                half_h = half_w / win_aspect

            m = QMatrix4x4()
            m.ortho(-half_w, half_w, -half_h, half_h, -8.0, 8.0)

            r = QMatrix4x4()
            r.rotate(self.pitch, 1.0, 0.0, 0.0)
            r.rotate(self.yaw, 0.0, 1.0, 0.0)

            return m * r

    return GLView


def build_window_class():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    GLView = build_gl_widget()

    class ProtoWindow(QWidget):

        def __init__(self):
            super().__init__(None)

            self.setWindowFlags(
                Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_NoSystemBackground, True)
            self.setAutoFillBackground(False)
            self.setWindowTitle("Arona 3D 原型")

            self.view = GLView(self)

            self._drag = None
            self._pos = None

            self.timings = self.view.load_model(POSE_SPEC)
            self.view.yaw = YAW

            aspect = self.view.info.get("aspect", 1.0)
            self.resize(max(80, int(HEIGHT * aspect)), HEIGHT)

            self.view.setGeometry(0, 0, self.width(), self.height())

        def resizeEvent(self, e):
            self.view.setGeometry(0, 0, self.width(), self.height())
            super().resizeEvent(e)

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                self._drag = e.globalPosition().toPoint()
                self._pos = self.pos()
            elif e.button() == Qt.RightButton:
                self.close()

        def mouseMoveEvent(self, e):
            if self._drag is not None:
                d = e.globalPosition().toPoint() - self._drag
                self.move(self._pos + d)

        def mouseReleaseEvent(self, e):
            self._drag = None

        def wheelEvent(self, e):
            self.view.zoom = max(0.4, min(4.0, self.view.zoom * (1.0015 ** e.angleDelta().y())))
            self.view.update()

        def closeEvent(self, e):
            try:
                self.view.cleanup()
            except Exception:
                pass
            super().closeEvent(e)

    return ProtoWindow


# ================================================================
# 透明性自检
# ================================================================

def selftest(app, win):
    from PySide6.QtGui import QGuiApplication

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    screen = QGuiApplication.primaryScreen()
    g = screen.geometry()
    dpr = screen.devicePixelRatio()

    # 放到主屏右下角，留边距
    x = g.right() - win.width() - 30
    y = g.bottom() - win.height() - 60
    win.move(int(x), int(y))

    results = []
    results.append("Arona 3D 原型 —— 透明背景实测")
    results.append("=" * 68)

    total, avail0 = system_memory()
    ws0, _ = process_working_set()

    # ---- 1. 显示窗口前的桌面 ----
    time.sleep(0.4)
    before = screen.grabWindow(0).copy(int(x * dpr), int(y * dpr),
                                       int(win.width() * dpr), int(win.height() * dpr)).toImage()

    # ---- 2. 显示窗口 ----
    win.show()
    win.raise_()
    win.activateWindow()

    t_show = time.monotonic()
    while time.monotonic() - t_show < SELFTEST_HOLD:
        app.processEvents()
        time.sleep(0.02)

    after_full = screen.grabWindow(0)
    after_full_img = after_full.toImage()
    after = after_full_img.copy(int(x * dpr), int(y * dpr),
                                int(win.width() * dpr), int(win.height() * dpr))

    _, avail1 = system_memory()
    ws1, peak1 = process_working_set()

    # ---- 关键探针：直接读 OpenGL 帧缓冲，与屏幕实际像素对比 ----
    from PySide6.QtGui import QImage

    fbo_probe = "（未能读取帧缓冲）"
    try:
        fb = win.view.grabFramebuffer()
        if hasattr(fb, "toImage"):          # 某些 PySide6 版本返回 QPixmap
            fb = fb.toImage()
        fb = fb.convertToFormat(QImage.Format_RGBA8888)

        if fb.width() > 4 and fb.height() > 4:
            fw, fh = fb.width(), fb.height()
            samples = []
            n_alpha255 = 0
            n_total = 0

            for fy in range(int(fh * 0.15), int(fh * 0.95), 7):
                for fx in range(int(fw * 0.25), int(fw * 0.75), 7):
                    c = fb.pixelColor(fx, fy)
                    n_total += 1
                    if c.alpha() > 250:
                        n_alpha255 += 1
                    if len(samples) < 6 and c.alpha() > 250:
                        # 找屏幕上的对应点
                        sx = int(x * dpr) + int(fx / fw * (win.width() * dpr))
                        sy = int(y * dpr) + int(fy / fh * (win.height() * dpr))
                        sc = after_full_img.pixelColor(sx, sy) if \
                            after_full_img.width() > sx and after_full_img.height() > sy else None
                        samples.append((
                            (fx, fy),
                            (c.red(), c.green(), c.blue(), c.alpha()),
                            (sc.red(), sc.green(), sc.blue()) if sc else None,
                        ))

            fbo_probe = (f"帧缓冲 {fw}x{fh}   不透明像素(alpha>250)占比 "
                         f"{n_alpha255 / max(1, n_total):.1%}\n")
            fbo_probe += "    帧缓冲RGBA  vs  屏幕实际RGB\n"
            for (fx, fy), a, s in samples:
                fbo_probe += f"      ({fx:>4},{fy:>4})  {a}   {s}\n"

    except Exception as exc:
        fbo_probe = f"帧缓冲读取失败: {exc!r}"

    # ---- 帧率：强制同步重绘 N 次 ----
    fps_text = "（未测量）"
    try:
        fps_frames_before = win.view.frames
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0:
            win.view.update()
            app.processEvents()
        dt = time.monotonic() - t0
        frames = win.view.frames - fps_frames_before
        if frames:
            fps_text = (f"{frames / dt:.1f} FPS   （{dt:.1f} 秒内实际渲染 {frames} 帧，"
                        f"单帧 {dt / frames * 1000:.1f} ms）")
        else:
            fps_text = "3 秒内没有产生任何重绘"
    except Exception as exc:
        fps_text = f"帧率测量失败: {exc!r}"

    # ---- 闲置 1 秒，确认没有后台空转 ----
    idle_frames_before = win.view.frames
    idle_t0 = time.monotonic()
    while time.monotonic() - idle_t0 < 1.0:
        app.processEvents()
        time.sleep(0.02)
    idle_frames = win.view.frames - idle_frames_before


    # ---- 3. 判定：四角区域是否仍等于桌面 ----
    w, h = after.width(), after.height()
    band = max(6, int(min(w, h) * 0.06))

    def region_stats(img, bx, by, bw, bh):
        tot = 0
        n = 0
        for yy in range(by, min(by + bh, img.height()), 2):
            for xx in range(bx, min(bx + bw, img.width()), 2):
                c = img.pixelColor(xx, yy)
                tot += c.red() + c.green() + c.blue()
                n += 1
        return tot / max(1, n) / 3.0

    corners = [("左上", 0, 0), ("右上", w - band, 0),
               ("左下", 0, h - band), ("右下", w - band, h - band)]

    results.append("")
    results.append("【1】四角区域：显示窗口前后的平均亮度对比")
    results.append(f"    {'角落':<6}{'显示前':>10}{'显示后':>10}{'差值':>10}   判定")
    ok_corners = 0
    for name, bx, by in corners:
        b = region_stats(before, bx, by, band, band)
        a = region_stats(after, bx, by, band, band)
        d = abs(a - b)
        good = d < 12
        ok_corners += 1 if good else 0
        results.append(f"    {name:<6}{b:>10.1f}{a:>10.1f}{d:>10.1f}   "
                       f"{'透出桌面 OK' if good else '★ 被遮挡（黑/白底）'}")

    # ---- 4. 中心区域：模型是否画出来了 ----
    cb = region_stats(before, w // 2 - band, h // 2 - band, band * 2, band * 2)
    ca = region_stats(after, w // 2 - band, h // 2 - band, band * 2, band * 2)

    results.append("")
    results.append("【2】中心区域：模型是否真的画出来")
    results.append(f"    显示前平均亮度 {cb:.1f}  ->  显示后 {ca:.1f}   差值 {abs(ca - cb):.1f}")
    results.append(f"    判定: {'有内容被绘制 OK' if abs(ca - cb) > 3 else '★ 中心区域没有变化'}")

    results.append("")
    results.append("【2b】帧缓冲 alpha 探针（区分「着色器 alpha 问题」与「Qt 合成问题」）")
    for line in fbo_probe.rstrip("\n").splitlines():
        results.append("    " + line)

    # ---- 5. OpenGL 信息 ----
    results.append("")
    results.append("【2c】帧率与闲置负载")
    results.append("    " + fps_text)
    results.append(f"    闲置 1 秒内的重绘次数: {idle_frames}  （0 = 没有后台空转）")

    results.append("")
    results.append("【3】OpenGL 运行时信息")
    for k, v in getattr(win.view, "gl_info", {}).items():
        results.append(f"    {k:<10} {v}")

    # ---- 6. 性能 ----
    t = win.view.timings
    results.append("")
    results.append("【4】性能")
    results.append(f"    模型解析      {t['parse']:>8.0f} ms")
    results.append(f"    GPU 数据构建  {t['build']:>8.0f} ms")
    results.append(f"    贴图加载      {t['textures']:>8.0f} ms")
    results.append(f"    合计          {t['total']:>8.0f} ms")
    results.append(f"    顶点 {win.view.info['vertex_count']:,}   "
                   f"三角形 {win.view.info['triangle_count']:,}   "
                   f"可见批次 {win.view.info['batch_count']}")
    results.append(f"    显示高度 {DISPLAY_HEIGHT}px   贴图上限 {TEXTURE_MAX}px")

    results.append("")
    results.append("【5】内存")
    results.append(f"    进程工作集  显示前 {mb(ws0)}  ->  显示后 {mb(ws1)}   "
                   f"峰值 {mb(peak1)}")
    results.append(f"    系统可用    显示前 {mb(avail0)}  ->  显示后 {mb(avail1)}")
    results.append(f"    系统总量    {mb(total)}")

    # ---- 7. 贴图报告 ----
    results.append("")
    results.append("【6】贴图加载")
    for ti, rel, status, ow, oh in win.view.tex_report:
        results.append(f"    [{ti:>2}] {status:<6} {ow}x{oh} -> 上限{TEXTURE_MAX}   {rel}")

    # ---- 8. 截图 ----
    before.save(str(LOG_DIR / "phase3_before.png"))
    after.save(str(LOG_DIR / "phase3_after.png"))
    results.append("")
    results.append(f"    截图已保存: {LOG_DIR / 'phase3_before.png'}")
    results.append(f"                {LOG_DIR / 'phase3_after.png'}")

    results.append("")
    results.append("=" * 68)
    passed = ok_corners >= 3 and abs(ca - cb) > 3
    results.append("结论: " + ("透明背景通过" if passed else "透明背景未通过，需排查"))
    results.append("=" * 68)

    text = "\n".join(results)

    try:
        print(text)
    except Exception:
        pass

    (LOG_DIR / "phase3_selftest.txt").write_text(text, encoding="utf-8")

    QTimer_single_quit(app)

    return passed


def QTimer_single_quit(app):
    from PySide6.QtCore import QTimer
    QTimer.singleShot(200, app.quit)


def wait_for_model(path, tries=6, delay=1.0):
    """
    等模型文件可读。

    模型放在 E: 可移动盘上，系统可能临时挂起该盘，
    此时 is_file() 会短暂返回 False —— 直接报「不存在」会误导。
    返回 (ok, 详情)
    """
    import time as _t

    for i in range(tries):
        try:
            if path.is_file():
                return True, f"第 {i + 1} 次尝试可读"
        except Exception as exc:
            last = repr(exc)
            _t.sleep(delay)
            continue
        _t.sleep(delay)

    drive = str(path.drive) or "?"
    try:
        import os as _os
        exists_dir = _os.path.isdir(str(path.parent))
    except Exception:
        exists_dir = False

    detail = (f"连续 {tries} 次读不到模型文件。\n"
              f"    路径      : {path}\n"
              f"    所在磁盘  : {drive}（E: 是可移动盘，系统可能临时挂起了它）\n"
              f"    父目录可达: {exists_dir}\n"
              f"    建议      : 在资源管理器里打开一次 {drive}\\ 确认盘还在，然后重试")
    return False, detail


def shutdown(app, win):
    """
    统一的收尾流程。顺序很重要：
      1. 先释放 GL 资源（必须还在当前上下文里）
      2. 再隐藏 / 关闭窗口
      3. 最后处理事件并退出事件循环

    每一步都单独 try，保证前面失败不会阻断后面 ——
    否则一旦漏掉 app.quit()，测试进程就会留在后台。
    """
    if win is not None:
        try:
            win.view.cleanup()
        except Exception:
            pass

        try:
            win.hide()
        except Exception:
            pass

        try:
            win.close()
        except Exception:
            pass

    try:
        app.processEvents()
    except Exception:
        pass

    try:
        app.quit()
    except Exception:
        pass

    try:
        app.processEvents()
    except Exception:
        pass


# ================================================================
# 主流程
# ================================================================

def main():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtWidgets import QApplication

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(24)
    fmt.setSamples(0)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication.instance() or QApplication(sys.argv)

    _ok, _detail = wait_for_model(PMX_PATH)
    if not _ok:
        print("=" * 68)
        print("模型文件读不到")
        print("=" * 68)
        print("    " + _detail)
        return 2

    print("=" * 68)
    print("AronaClient 3D 原型" + ("（自检模式）" if SELFTEST else ""))
    print("=" * 68)

    ProtoWindow = build_window_class()
    win = ProtoWindow()

    total, avail = system_memory()
    ws, _ = process_working_set()

    print(f"模型      : {PMX_PATH}")
    print(f"顶点/三角形: {win.view.info['vertex_count']:,} / {win.view.info['triangle_count']:,}")
    print(f"可见批次  : {win.view.info['batch_count']}")
    print(f"加载耗时  : {win.view.timings['total']:.0f} ms")
    print(f"进程内存  : {mb(ws)}   系统可用 {mb(avail)} / {mb(total)}")
    print(f"窗口尺寸  : {win.width()} x {win.height()}")
    _sm = [(m["name"], m["sphere_mode"], m["sphere_index"])
           for m in win.view.model.materials
           if m["diffuse"][3] > 0.001 and m["sphere_mode"] > 0]
    print(f"          球面贴图材质: {_sm if _sm else '（本模型没有）'}")
    print(f"画面设置  : 卡通明暗={'关' if TOON_STEPS < 1.5 else str(int(TOON_STEPS)) + '档'}  "
          f"描边={'开' if OUTLINE else '关'}"
          + (f"(粗细 x{EDGE_SCALE})" if OUTLINE else "")
          + f"  球面贴图={'开' if SPHERE else '关'}")
    if getattr(win.view, "pose_applied", None):
        print(f"姿势      : {win.view.pose_applied}")
        print(f"蒙皮耗时  : {win.view.skin_ms:.0f} ms")
    else:
        print("姿势      : 未施加（原始 T 字）")
    print()

    try:
        return _run_modes(app, win)
    except Exception:
        import traceback
        traceback.print_exc()
        return 3
    finally:
        shutdown(app, win)


def _run_modes(app, win):
    """三种自动化测试模式 + 交互模式。由 main() 统一负责收尾。"""

    if ROTATECHECK:
        from PySide6.QtGui import QImage

        win.show()
        win.raise_()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.02)
        time.sleep(0.6)

        report = []
        stats = []

        for yaw in range(0, 360, 20):
            win.view.yaw = float(yaw)
            win.view.update()
            for _ in range(12):
                app.processEvents()
                time.sleep(0.015)

            fb = win.view.grabFramebuffer()
            if hasattr(fb, "toImage"):
                fb = fb.toImage()
            fb = fb.convertToFormat(QImage.Format_RGBA8888)

            fw, fh = fb.width(), fb.height()
            opaque = 0
            total = 0
            grey_sum = 0
            for yy in range(0, fh, 4):
                for xx in range(0, fw, 4):
                    c = fb.pixelColor(xx, yy)
                    total += 1
                    if c.alpha() > 200:
                        opaque += 1
                        grey_sum += (c.red() + c.green() + c.blue()) / 3.0

            cov = opaque / max(1, total)
            mean = (grey_sum / opaque) if opaque else 0.0
            stats.append((yaw, cov, mean, opaque))

        report.append("旋转闪烁检查（每 20 度抓一帧 OpenGL 帧缓冲）")
        report.append("=" * 64)
        report.append(f"{'yaw':>5}{'角色占比':>10}{'平均亮度':>10}{'不透明像素':>12}")
        report.append("-" * 64)
        for yaw, cov, mean, op in stats:
            report.append(f"{yaw:>5}{cov:>10.1%}{mean:>10.1f}{op:>12,}")

        covs = [c for _, c, _, _ in stats]
        means = [m for _, _, m, _ in stats if m > 0]

        report.append("")
        report.append(f"角色占比: min={min(covs):.1%}  max={max(covs):.1%}")
        report.append(f"平均亮度: min={min(means):.1f}  max={max(means):.1f}")

        empties = [y for y, c, _, _ in stats if c < 0.02]
        report.append("")
        report.append(f"空白帧（角色占比 < 2%）: {empties if empties else '无'}")

        # 相邻帧亮度突变 = 可能的闪烁
        jumps = []
        for i in range(1, len(stats)):
            d = abs(stats[i][2] - stats[i - 1][2])
            if d > 25:
                jumps.append((stats[i - 1][0], stats[i][0], round(d, 1)))
        report.append(f"相邻帧亮度突变 (>25): {jumps if jumps else '无'}")

        # 逐帧存盘（避免 Qt 的 drawPixmap 绑定问题；拼图交给外部工具）
        try:
            rot_dir = LOG_DIR / "rotate"
            rot_dir.mkdir(parents=True, exist_ok=True)
            for yaw2 in range(0, 360, 40):
                win.view.yaw = float(yaw2)
                win.view.update()
                for _ in range(12):
                    app.processEvents()
                    time.sleep(0.015)
                pm = win.view.grabFramebuffer()
                pm.save(str(rot_dir / f"yaw_{yaw2:03d}.png"))
            report.append("")
            report.append(f"逐帧截图已保存到: {rot_dir}")
        except Exception as exc:
            report.append(f"逐帧存盘失败: {exc!r}")

        text = "\n".join(report)
        try:
            print(text)
        except Exception:
            pass
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "phase_a_rotate.txt").write_text(text, encoding="utf-8")

        return 0

    if SHOT:
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        g = screen.geometry()
        dpr = screen.devicePixelRatio()
        px = g.right() - win.width() - 30
        py = g.bottom() - win.height() - 60
        win.move(int(px), int(py))
        time.sleep(0.4)
        before_img = screen.grabWindow(0).toImage().copy(
            int(px * dpr), int(py * dpr),
            int(win.width() * dpr), int(win.height() * dpr))

        win.show()
        win.raise_()
        t = time.monotonic()
        while time.monotonic() - t < 1.6:
            app.processEvents()
            time.sleep(0.02)

        after = screen.grabWindow(0).toImage().copy(
            int(px * dpr), int(py * dpr),
            int(win.width() * dpr), int(win.height() * dpr))
        after.save(SHOT)

        # 与"未显示窗口时"的桌面逐像素求差，得到角色轮廓掩码
        diff_px = 0
        total = 0
        for yy in range(0, after.height(), 3):
            for xx in range(0, after.width(), 3):
                c1 = before_img.pixelColor(xx, yy)
                c2 = after.pixelColor(xx, yy)
                total += 1
                if (abs(c1.red() - c2.red()) + abs(c1.green() - c2.green())
                        + abs(c1.blue() - c2.blue())) > 12:
                    diff_px += 1
        print(f"已保存 {SHOT}   角色覆盖像素占比 {diff_px / max(1, total):.1%}")

        return 0

    if SELFTEST:
        return 0 if selftest(app, win) else 1

    win.show()
    win.raise_()
    print("已显示。左键拖动=旋转/移动，右键=关闭。")
    return app.exec()


if __name__ == "__main__":
    _rc = main()

    if SHOT or ROTATECHECK or SELFTEST:
        # 自动化测试模式：上面已经做过完整收尾（GL 资源 / 窗口 / 事件循环），
        # 这里再用 os._exit 兜底，保证进程一定退出，不会累积后台实例。
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(_rc)

    sys.exit(_rc)
