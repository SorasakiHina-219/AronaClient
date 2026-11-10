import sys

from PySide6.QtWidgets import QApplication

from ui import AronaUI


def main():
    print("================================")
    print("正在启动新版 AronaClient")
    print("main.py 路径：", __file__)
    print("================================")

    app = QApplication(sys.argv)

    # T4：L1 使用共享 AIClient（services.app_context.AppContext，与 L2 同一实例）；
    # 未落地时回退 AronaUI 自构造行为，L1 功能零变化。
    try:
        from services.app_context import AppContext
        _ctx = AppContext.get()
        window = AronaUI(ai_client=_ctx.ai_client)
    except ImportError:
        window = AronaUI()

    print("AronaUI 创建成功：", type(window))

    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
