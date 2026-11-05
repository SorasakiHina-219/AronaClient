# -*- coding: utf-8 -*-
# Copyright (C) 2024-2026 SorasakiHina-219
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

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
