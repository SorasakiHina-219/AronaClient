"""runtime.model3d — 3D 能力子包（目录名不能写成 3d：Python 标识符不得以数字开头）。

目标结构（ADR-006 §2.3，M3 归并完成）：
  asset_provider.py  资源发现与校验（M0 已落地）
  pmx_parser.py      PMX 解析（M3，由 pmx_model.py 平移）
  loader.py          工作线程加载器（M3）
  gl_view.py         唯一一份 GL 视图与着色器（M3）
  runtime.py         Model3DRuntime 门面（M3）
"""
