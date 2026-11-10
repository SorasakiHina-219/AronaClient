"""tools.spatial_preview — AronaClient Spatial UI 预览系统。

模块结构：
  preview_config.py   预览环境配置（路径、参数）
  display_manager.py  显示器枚举与窗口定位
  preview_runtime.py  隔离预览运行时（独立进程内执行）
  file_watcher.py     文件变化检测与防抖
  supervisor.py       预览进程管理器（入口点）

使用方式（笔记本主屏运行）：
  .venv/Scripts/python.exe -m tools.spatial_preview.supervisor \
      [--display <名称或索引>] [--full-screen] [--performance low|medium|high]
"""
__version__ = "0.1.0"
