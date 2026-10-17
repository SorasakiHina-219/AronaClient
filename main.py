import sys

from PySide6.QtWidgets import QApplication

from ui import AronaUI


def main():
    print("================================")
    print("正在启动新版 AronaClient")
    print("main.py 路径：", __file__)
    print("================================")

    app = QApplication(sys.argv)

    window = AronaUI()

    print("AronaUI 创建成功：", type(window))

    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
