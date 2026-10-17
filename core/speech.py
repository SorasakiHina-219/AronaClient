# core/speech.py
# AronaClient V0.1
# 语音识别模块

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
