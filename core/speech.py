# core/speech.py
# AronaClient V0.1
# 语音识别模块

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
import speech_recognition as sr


class SpeechRecognizer:

    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.device = None

    def get_device_name(self):
        try:
            microphones = sr.Microphone.list_microphone_names()

            if not microphones:
                return "没有检测到麦克风"

            return "系统默认麦克风"

        except Exception:
            return "系统默认麦克风"

    def listen(self, timeout=8, phrase_time_limit=15):

        try:

            with sr.Microphone(
                device_index=self.device
            ) as source:

                print("🎤 正在监听麦克风……")

                self.recognizer.adjust_for_ambient_noise(
                    source,
                    duration=0.5,
                )

                audio = self.recognizer.listen(
                    source,
                    timeout=timeout,
                    phrase_time_limit=phrase_time_limit,
                )

            print("🔎 正在识别语音……")

            text = self.recognizer.recognize_google(
                audio,
                language="zh-CN",
            )

            return text.strip()

        except sr.WaitTimeoutError:

            return ""

        except sr.UnknownValueError:

            return ""

        except sr.RequestError as exc:

            raise RuntimeError(
                "语音识别服务连接失败。\n\n"
                f"{exc}"
            ) from exc

        except Exception as exc:

            raise RuntimeError(
                "麦克风或语音识别发生错误。\n\n"
                f"{exc}"
            ) from exc
