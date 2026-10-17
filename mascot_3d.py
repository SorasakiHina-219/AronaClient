# -*- coding: utf-8 -*-
"""
桌面小人物的真实 3D 渲染（阶段 B 集成）

渲染逻辑（着色器、绘制顺序、描边/卡通明暗/球面贴图）**取自已经过
阶段 3 / A / C 实机验证的独立原型 arona3d_proto.py**，不是另写一套。

与原型的唯一区别是加载方式：
    原型   —— 主线程一次性加载（约 12.6 秒，会冻结界面）
    本模块 —— 拆成两段：
        ① PmxLoadWorker  在工作线程做纯 CPU 的解析 / 归一化 / 贴图解码
        ② MascotGLView   在拥有 GL 上下文的线程里建 VBO / 纹理 / 着色器

这样主窗口和小人物在加载期间都保持可响应。

不修改原始 PMX / 贴图文件，不引入任何第三方依赖。
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QImage, QMatrix4x4, QSurfaceFormat, QVector3D

# ================================================================
# 配置
# ================================================================

MODEL_DIR = Path(r"E:\Arona_1.01")
PMX_PATH = MODEL_DIR / "Arona_1.01.pmx"

TEXTURE_MAX = 1024          # 贴图降采样上限（内存敏感）
DEFAULT_HEIGHT = 320        # 桌面小人物高度（像素）—— 提高以获得更清楚的细节

TOON_STEPS = 0.0            # 0 = 关闭卡通明暗（恢复自然连续明暗）
TOON_SOFT = 0.12
OUTLINE = False             # 描边开关（关闭：不使用法线外扩描边）
EDGE_SCALE = 0.40
SPHERE = False              # 球面贴图开关（未要求，保持关闭）

# 自然站姿：沿手臂链条分配旋转，肩部少转、上臂多转，肘部微弯。
# 这样挂在「左肩」下的袖子骨骼也能跟着走，减少撕裂。
POSE_NATURAL = {
    "左肩": [("z", -10)],
    "左腕": [("z", -32)],
    "左ひじ": [("z", -6)],
    "右肩": [("z", 10)],
    "右腕": [("z", 32)],
    "右ひじ": [("z", 6)],
}

POSE_TPOSE = {}             # 回退用：原始 T 字姿势


# ================================================================
# 着色器（与已验证的原型一致）
# ================================================================

VERT = """
#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aUV;

uniform mat4 uMVP;
uniform mat3 uNormalMat;
uniform float uOutline;
uniform float uEdgeSize;

out vec3 vN;
out vec2 vUV;

void main()
{
    vec3 p = aPos;
    if (uOutline > 0.5) {
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
uniform vec3 uFillDir;

uniform float uHasTex;
uniform float uOutline;
uniform float uToonSteps;
uniform float uToonSoft;
uniform float uSphereOn;
uniform float uSphereMode;

out vec4 fragColor;

void main()
{
    if (uOutline > 0.5) {
        fragColor = vec4(uEdgeColor, uDiffuse.a);
        return;
    }

    vec4 base = texture(uTex, vUV);
    vec3 rgb = base.rgb * uDiffuse.rgb;

    if (uSphereOn > 0.5 && uSphereMode > 0.5) {
        vec3 ns = normalize(vN);
        vec2 suv = ns.xy * 0.5 + 0.5;
        vec4 sp = texture(uSphere, suv);
        if (uSphereMode < 1.5) {
            rgb *= sp.rgb;
        } else if (uSphereMode < 2.5) {
            rgb += sp.rgb * sp.a;
        } else {
            rgb = mix(rgb, sp.rgb, sp.a);
        }
    }

    vec3 n = normalize(vN);

    // 光照：环境光 + 主光 + 柔和补光
    //
    // 旧公式等价于 shade = 0.781 + 0.299 * ndl，
    // 而模型表面平均 ndl 只有约 0.2，实测把贴图整体压暗了 16~23%
    // （脸 175 vs 贴图 217）。这里把环境光提到 0.86，
    // 主光系数 0.30，再加一盏 0.10 的补光提亮背光面，
    // 使平均亮度回到与原始贴图相当，同时保留明暗层次。
    float ndl = dot(n, uLightDir);
    float ndlFill = dot(n, uFillDir);

    float shade;
    if (uToonSteps < 1.5) {
        shade = 0.86 + 0.30 * max(ndl, 0.0) + 0.10 * max(ndlFill, 0.0);
    } else {
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


# ================================================================
# ① 工作线程：纯 CPU 的加载（不碰任何 GL）
# ================================================================

class PmxLoadWorker(QObject):
    """
    在工作线程里完成：
        PMX 解析 -> 蒙皮 -> 归一化 / 去索引 -> 贴图解码
    全部是纯 CPU + QImage 操作，不接触 OpenGL 上下文。
    """

    progress = Signal(str)
    loaded = Signal(object)          # 成功后带 payload
    failed = Signal(str)

    def __init__(self, pmx_path, model_dir, pose=None, height=DEFAULT_HEIGHT,
                 texture_max=TEXTURE_MAX):
        super().__init__()
        self.pmx_path = Path(pmx_path)
        self.model_dir = Path(model_dir)
        self.pose = POSE_NATURAL if pose is None else pose
        self.height = height
        self.texture_max = texture_max
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @Slot()
    def run(self):
        try:
            import pmx_model

            t0 = time.perf_counter()

            if self._cancelled:
                self.failed.emit("已取消")
                return

            # --- 模型文件可读性（E: 是可移动盘，可能临时掉线）---
            ok = False
            for _ in range(5):
                if self.pmx_path.is_file():
                    ok = True
                    break
                time.sleep(0.6)

            if not ok:
                self.failed.emit(
                    f"读不到模型文件：{self.pmx_path}\n"
                    f"（该文件在可移动盘上，请确认磁盘已连接）")
                return

            self.progress.emit("正在解析模型…")

            model = pmx_model.load(self.pmx_path)
            t1 = time.perf_counter()

            if self._cancelled:
                self.failed.emit("已取消")
                return

            # --- 蒙皮 ---
            positions = None
            normals = None
            if self.pose:
                idx_pose = {}
                for name, rots in self.pose.items():
                    i = pmx_model.find_bone_index(model, name)
                    if i is None:
                        i = pmx_model.find_bone_index(model, name, exact=False)
                    if i is not None:
                        idx_pose[i] = rots
                if idx_pose:
                    M = pmx_model.compute_skin_matrices(model, idx_pose)
                    bi, bw = pmx_model.build_skin_arrays(model)
                    positions, normals = pmx_model.skin(model, M, bi, bw)

            self.progress.emit("正在准备顶点数据…")

            vertex_bytes, batches, info = pmx_model.build_gpu_data(
                model, positions, normals)
            t2 = time.perf_counter()

            if self._cancelled:
                self.failed.emit("已取消")
                return

            # --- 贴图（解码阶段就降采样，避免整张 4096x4096 进内存）---
            self.progress.emit("正在加载贴图…")

            textures, tex_report = pmx_model.load_textures(
                model, self.model_dir, max_size=self.texture_max)
            t3 = time.perf_counter()

            if self._cancelled:
                self.failed.emit("已取消")
                return

            # --- 描边参数 ---
            nscale = float(info.get("scale", 1.0)) * EDGE_SCALE
            edge_info = []
            for b in batches:
                mat = b["mat"]
                es = float(mat["edge_size"]) * nscale
                ec = mat["edge_color"]
                draws = bool(mat["draw_flag"] & 0x10) and es > 1e-7
                edge_info.append((es, (float(ec[0]), float(ec[1]), float(ec[2])), draws))

            # --- 球面贴图候选（只取 PMX 真正引用的）---
            sphere_ids = sorted({
                mat["sphere_index"]
                for mat in model.materials
                if mat["diffuse"][3] > 0.001
                and mat["sphere_mode"] > 0
                and 0 <= mat["sphere_index"] < len(model.textures)
            })

            payload = {
                "vertex_bytes": vertex_bytes,
                "batches": batches,
                "info": info,
                "textures": textures,
                "sphere_ids": sphere_ids,
                "edge_info": edge_info,
                "timings": {
                    "parse_ms": (t1 - t0) * 1000,
                    "build_ms": (t2 - t1) * 1000,
                    "textures_ms": (t3 - t2) * 1000,
                    "total_ms": (t3 - t0) * 1000,
                },
                "skin_ms": 0.0,
            }

            self.loaded.emit(payload)

        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ================================================================
# ② GL 部件：在拥有上下文的线程里建资源并绘制
# ================================================================

def build_gl_view_class():
    """延迟导入 QtOpenGL，避免在无 GL 环境导入本模块时失败。"""

    from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLShader,
                                  QOpenGLShaderProgram, QOpenGLTexture,
                                  QOpenGLVertexArrayObject)
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    class MascotGLView(QOpenGLWidget):
        """把真实 Arona 模型渲染到透明背景上。"""

        def __init__(self, parent=None):
            super().__init__(parent)

            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_NoSystemBackground, True)
            self.setAutoFillBackground(False)

            self.payload = None
            self.program = None
            self.vbo = None
            self.vao = None
            self.gl_textures = {}
            self.gl_sphere = {}
            self.uni = {}
            self.ready = False
            self.error = None

            self.yaw = 180.0        # 模型朝 -Z，转 180 度才是正面
            self.pitch = 0.0
            self.frames = 0

        # ---------------- 数据 ----------------

        def set_payload(self, payload):
            """在主线程调用；实际 GPU 上传推迟到 initializeGL。"""
            self.payload = payload
            info = payload["info"]
            w, h = self._size_for(info)
            self.setFixedSize(int(w), int(h))

        def _size_for(self, info):
            aspect = max(0.05, float(info.get("aspect", 1.0)))
            h = int(DEFAULT_HEIGHT)
            return max(24, int(round(h * aspect))), h

        # ---------------- GL ----------------

        def _fns(self):
            if getattr(self, "gl33", None) is not None:
                return self.gl33
            return self.context().functions()

        def _setf(self, name, value):
            """
            设置单个 float uniform。

            不能用 setUniformValue(location, float)：PySide6 6.11.2 上
            对标量 float 静默失效（矩阵 / vec3 / vec4 / int 都正常），
            而本模块所有效果开关都是 float。统一走 glUniform1f。
            """
            loc = self.uni.get(name, -1)
            if loc is None or loc < 0:
                return False
            try:
                self._fns().glUniform1f(int(loc), float(value))
                return True
            except Exception:
                return False

        def initializeGL(self):
            import time as _t
            _t0 = _t.perf_counter()
            try:
                from PySide6.QtOpenGL import (QOpenGLVersionFunctionsFactory,
                                              QOpenGLVersionProfile)
                prof = QOpenGLVersionProfile()
                prof.setVersion(3, 3)
                prof.setProfile(QSurfaceFormat.CoreProfile)
                self.gl33 = QOpenGLVersionFunctionsFactory.get(prof, self.context())
            except Exception:
                self.gl33 = None

            f = self._fns()

            self.program = QOpenGLShaderProgram()
            if not self.program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERT):
                raise RuntimeError("顶点着色器编译失败: " + self.program.log())
            if not self.program.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAG):
                raise RuntimeError("片段着色器编译失败: " + self.program.log())
            if not self.program.link():
                raise RuntimeError("着色器链接失败: " + self.program.log())

            self.uni = {}
            for nm in (b"uMVP", b"uNormalMat", b"uLightDir", b"uFillDir", b"uTex", b"uHasTex",
                       b"uDiffuse", b"uOutline", b"uEdgeSize", b"uEdgeColor",
                       b"uToonSteps", b"uToonSoft", b"uSphere", b"uSphereOn",
                       b"uSphereMode"):
                self.uni[nm] = self.program.uniformLocation(nm)

            p = self.payload
            if p is None:
                return

            self.vao = QOpenGLVertexArrayObject()
            self.vao.create()
            self.vao.bind()

            self.vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            self.vbo.create()
            self.vbo.bind()
            self.vbo.allocate(p["vertex_bytes"], len(p["vertex_bytes"]))

            stride = 8 * 4
            self.program.enableAttributeArray(0)
            self.program.setAttributeBuffer(0, 0x1406, 0, 3, stride)
            self.program.enableAttributeArray(1)
            self.program.setAttributeBuffer(1, 0x1406, 12, 3, stride)
            self.program.enableAttributeArray(2)
            self.program.setAttributeBuffer(2, 0x1406, 24, 2, stride)

            self.vbo.release()
            self.vao.release()

            # 普通贴图
            for ti, img in p["textures"].items():
                try:
                    t = QOpenGLTexture(img)
                    t.setMinificationFilter(QOpenGLTexture.Linear)
                    t.setMagnificationFilter(QOpenGLTexture.Linear)
                    t.setWrapMode(QOpenGLTexture.Repeat)
                    self.gl_textures[ti] = t
                except Exception:
                    pass

            # 球面贴图（只上传 PMX 里 sphere_mode>0 的材质引用的那几张）
            for si in p.get("sphere_ids", []):
                img = p["textures"].get(si)
                if img is None:
                    continue
                try:
                    t = QOpenGLTexture(img)
                    t.setMinificationFilter(QOpenGLTexture.Linear)
                    t.setMagnificationFilter(QOpenGLTexture.Linear)
                    t.setWrapMode(QOpenGLTexture.ClampToEdge)
                    self.gl_sphere[si] = t
                except Exception:
                    pass

            f.glEnable(0x0B71)          # GL_DEPTH_TEST
            f.glDepthFunc(0x0201)       # GL_LESS

            self.ready = True

            # 如实报告抗锯齿是否真的生效（不能只凭「我设置了」就说成功）
            try:
                _fmt = self.context().format()
                print(f"[小人物] 实测上下文: {_fmt.majorVersion()}.{_fmt.minorVersion()} "
                      f"profile={_fmt.profile()} samples={_fmt.samples()} "
                      f"alpha={_fmt.alphaBufferSize()}")
            except Exception:
                pass
            self.init_ms = (_t.perf_counter() - _t0) * 1000
            print(f"[小人物] GPU 资源上传耗时 {self.init_ms:.0f} ms "
                  f"(顶点 {len(p['vertex_bytes'])/1024/1024:.1f} MB, "
                  f"贴图 {len(self.gl_textures)} 张)")

        def paintGL(self):
            self.frames += 1
            f = self._fns()

            f.glClearColor(0.0, 0.0, 0.0, 0.0)
            f.glClear(0x00004000 | 0x00000100)      # COLOR | DEPTH

            if not self.ready or self.program is None or self.payload is None:
                return

            f.glEnable(0x0BE2)                      # GL_BLEND
            f.glBlendFuncSeparate(0x0302, 0x0303, 0x0001, 0x0303)
            f.glEnable(0x0B71)

            p = self.payload
            batches = p["batches"]
            edge_info = p["edge_info"]

            self.program.bind()

            mvp = self._mvp()
            self.program.setUniformValue(self.uni[b"uMVP"], mvp)
            self.program.setUniformValue(self.uni[b"uNormalMat"], mvp.normalMatrix())
            self.program.setUniformValue(
                self.uni[b"uLightDir"], QVector3D(0.30, 0.50, 0.81).normalized())
            # 补光：从下方偏左打，抬亮下巴、裙摆内侧、手臂下缘这些阴影区
            self.program.setUniformValue(
                self.uni[b"uFillDir"], QVector3D(-0.35, -0.45, 0.82).normalized())

            self._setf(b"uToonSteps", TOON_STEPS)
            self._setf(b"uToonSoft", TOON_SOFT)
            self._setf(b"uSphereOn", 1.0 if SPHERE else 0.0)
            self._setf(b"uSphereMode", 0.0)
            self.program.setUniformValue(self.uni[b"uSphere"], 1)

            f.glViewport(0, 0, int(self.width() * self.devicePixelRatio()),
                         int(self.height() * self.devicePixelRatio()))

            order = sorted(range(len(batches)),
                           key=lambda i: 0 if batches[i]["mat"]["diffuse"][3] >= 0.999 else 1)

            self.vao.bind()

            # ---------- 描边遍 ----------
            if OUTLINE:
                f.glEnable(0x0B44)
                f.glCullFace(0x0404)        # GL_FRONT
                f.glFrontFace(0x0901)
                f.glDepthMask(True)
                self._setf(b"uOutline", 1.0)
                self._setf(b"uSphereMode", 0.0)

                for i in order:
                    es, ec, draws = edge_info[i]
                    if not draws:
                        continue
                    b = batches[i]
                    self._setf(b"uEdgeSize", es)
                    self.program.setUniformValue(
                        self.uni[b"uEdgeColor"], float(ec[0]), float(ec[1]), float(ec[2]))
                    self.program.setUniformValue(
                        self.uni[b"uDiffuse"], 0.0, 0.0, 0.0,
                        float(b["mat"]["diffuse"][3]))
                    f.glDrawArrays(0x0004, b["first"], b["count"])

                self._setf(b"uOutline", 0.0)
                f.glCullFace(0x0405)        # GL_BACK

            # ---------- 主遍 ----------
            for pass_idx in (0, 1):
                for i in order:
                    b = batches[i]
                    mat = b["mat"]
                    a = mat["diffuse"][3]
                    if (pass_idx == 0) != (a >= 0.999):
                        continue

                    f.glDepthMask(pass_idx == 0)

                    if mat["draw_flag"] & 0x01:
                        f.glDisable(0x0B44)
                    else:
                        f.glEnable(0x0B44)
                        f.glCullFace(0x0405)
                        f.glFrontFace(0x0901)

                    gltex = self.gl_textures.get(mat["texture_index"])
                    if gltex is not None:
                        gltex.bind(0)
                    self.program.setUniformValue(self.uni[b"uTex"], 0)
                    self._setf(b"uHasTex", 1.0 if gltex is not None else 0.0)

                    smode = int(mat["sphere_mode"])
                    stex = self.gl_sphere.get(int(mat["sphere_index"])) \
                        if (SPHERE and smode > 0) else None
                    if stex is not None:
                        stex.bind(1)
                        self._setf(b"uSphereMode", smode)
                    else:
                        self._setf(b"uSphereMode", 0.0)

                    d = mat["diffuse"]
                    self.program.setUniformValue(
                        self.uni[b"uDiffuse"],
                        float(d[0]), float(d[1]), float(d[2]), float(d[3]))

                    f.glDrawArrays(0x0004, b["first"], b["count"])

            self.vao.release()
            self.program.release()
            f.glDepthMask(True)

        # ---------------- 相机 ----------------

        def _mvp(self):
            info = self.payload["info"]
            model_w = float(info.get("norm_w", 1.0))
            model_h = float(info.get("norm_h", 1.0))

            half_h = model_h / 2.0
            half_w = model_w / 2.0

            win_aspect = (self.width() / self.height()) if self.height() else 1.0
            model_aspect = model_w / model_h if model_h else 1.0

            if win_aspect > model_aspect:
                half_w = half_h * win_aspect
            else:
                half_h = half_w / win_aspect

            m = QMatrix4x4()
            m.ortho(-half_w, half_w, -half_h, half_h, -8.0, 8.0)

            r = QMatrix4x4()
            r.rotate(self.pitch, 1.0, 0.0, 0.0)
            r.rotate(self.yaw, 0.0, 1.0, 0.0)

            return m * r

        # ---------------- 清理 ----------------

        def cleanup(self):
            """必须在还有当前上下文时调用。"""
            try:
                self.makeCurrent()

                for d in (getattr(self, "gl_sphere", {}), getattr(self, "gl_textures", {})):
                    for t in list(d.values()):
                        try:
                            t.destroy()
                        except Exception:
                            pass
                    d.clear()

                for attr in ("vbo", "vao"):
                    o = getattr(self, attr, None)
                    if o is not None:
                        try:
                            o.destroy()
                        except Exception:
                            pass
                        setattr(self, attr, None)

                self.ready = False
                self.doneCurrent()
            except Exception:
                pass

    return MascotGLView


MascotGLView = None


def get_gl_view_class():
    """惰性获取 GL 部件类（首次调用时才导入 QtOpenGL）。"""
    global MascotGLView
    if MascotGLView is None:
        MascotGLView = build_gl_view_class()
    return MascotGLView


# ================================================================
# ③ 加载控制器：把线程 / 部件 / 取消 串起来
# ================================================================

class MascotLoader(QObject):
    """
    在后台线程加载模型，完成后在主线程创建 GL 部件。

    调用方（MascotWindow）只需要：
        loader = MascotLoader(parent_window)
        loader.ready.connect(on_ready)      # on_ready(view)
        loader.failed.connect(on_failed)    # on_failed(message)
        loader.start()
        ...
        loader.shutdown()                   # 退出时调用
    """

    ready = Signal(object)          # 参数是 MascotGLView 实例
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, parent_widget, pmx_path=None, model_dir=None,
                 pose="__default__", height=DEFAULT_HEIGHT):
        super().__init__(parent_widget)
        self.parent_widget = parent_widget
        self.pmx_path = Path(pmx_path) if pmx_path else PMX_PATH
        self.model_dir = Path(model_dir) if model_dir else MODEL_DIR
        # pose=None 表示 T 字；未传参（哨兵值）表示用自然站姿
        self.pose = POSE_NATURAL if pose == "__default__" else pose
        self.height = height
        self.thread = None
        self.worker = None
        self.view = None
        self._done = False

    # --------------------------------------------------------

    def start(self):
        if self.thread is not None:
            return

        self.thread = QThread()
        self.worker = PmxLoadWorker(
            self.pmx_path,
            self.model_dir,
            pose=self.pose,
            height=self.height,
        )
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress)
        self.worker.loaded.connect(self._on_loaded)
        self.worker.failed.connect(self._on_failed)

        # 线程正常结束后自动清理
        self.worker.loaded.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_finished)

        self.thread.start()

    # --------------------------------------------------------

    @Slot(object)
    def _on_loaded(self, payload):
        if self._done:
            return
        self._done = True

        try:
            GLView = get_gl_view_class()
            view = GLView(self.parent_widget)
            view.set_payload(payload)
            view.hide()
            self.view = view
            self.ready.emit(view)
        except Exception as exc:
            self.failed.emit(f"创建 3D 部件失败：{type(exc).__name__}: {exc}")

    @Slot(str)
    def _on_failed(self, message):
        if self._done:
            return
        self._done = True
        self.failed.emit(message)

    @Slot()
    def _on_thread_finished(self):
        try:
            if self.worker is not None:
                self.worker.deleteLater()
            if self.thread is not None:
                self.thread.deleteLater()
        except Exception:
            pass
        self.worker = None
        self.thread = None

    # --------------------------------------------------------

    def shutdown(self, wait_ms=4000):
        """
        退出时调用：请求取消 -> 等线程结束 -> 释放 GL 资源。
        不使用 terminate()，也不使用 os._exit()。
        """
        try:
            if self.worker is not None:
                self.worker.cancel()
        except Exception:
            pass

        try:
            if self.thread is not None and self.thread.isRunning():
                self.thread.quit()
                self.thread.wait(wait_ms)
        except Exception:
            pass

        try:
            if self.view is not None:
                self.view.cleanup()
        except Exception:
            pass
