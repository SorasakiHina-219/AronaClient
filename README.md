# AronaClient

AronaClient 鏄竴涓熀浜?PySide6 鐨勬闈即渚ｅ簲鐢紝鎻愪緵 AI 瀵硅瘽銆佽闊冲悎鎴愬拰 3D 鍚夌ゥ鐗╂樉绀哄姛鑳姐€?
[![License: GPL-3.0](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-orange)](#)

## 鍔熻兘姒傝

| 鍔熻兘 | 鐘舵€?| 璇存槑 |
|------|------|------|
| AI 瀵硅瘽 | 鉁?鍙敤 | 鏀寔 DeepSeek / OpenRouter API |
| 璇煶鍚堟垚 (TTS) | 鉁?鍙敤 | 鏂囧瓧杞闊宠緭鍑?|
| 3D Arona 鍚夌ゥ鐗?| 鉁?鍙敤 | PMX 鏍煎紡妯″瀷鏄剧ず |
| 鑱婂ぉ鐣岄潰 | 鉁?鍙敤 | 澶氫細璇濈鐞?|
| 璁剧疆绠＄悊 | 鉁?鍙敤 | API 閰嶇疆銆佹ā鍨嬮€夋嫨 |
| 娑堟伅娓叉煋 | 鉁?鍙敤 | Markdown 鏀寔 |
| 妗岄潰瑙嗚鍒嗘瀽 | 鈿狅笍 鍙楅檺 | 渚濊禆鏈疄鐜版ā鍧?|
| 妗岄潰涓诲姩鎰熺煡 | 鈿狅笍 鍙楅檺 | 渚濊禆鏈疄鐜版ā鍧?|
| Agent 杩滅▼鎺у埗 | 鉂?涓嶅彲鐢?| 瀹屾暣鍔熻兘灏氭湭瀹炵幇 |

## 鏋舵瀯

```
AronaClient/
鈹溾攢鈹€ main.py                  # 鍚姩鍏ュ彛
鈹溾攢鈹€ ui.py                    # 涓荤晫闈?(QMainWindow)
鈹溾攢鈹€ config.py                # 閰嶇疆绠＄悊
鈹溾攢鈹€ arona3d_proto.py         # 3D 妯″瀷鍗忚
鈹溾攢鈹€ mascot_3d.py             # 3D 鍚夌ゥ鐗╁姞杞藉櫒
鈹溾攢鈹€ mascot_window.py         # 鍚夌ゥ鐗╃獥鍙ｇ鐞?鈹溾攢鈹€ pmx_model.py             # PMX 妯″瀷瑙ｆ瀽
鈹溾攢鈹€ core/
鈹?  鈹溾攢鈹€ ai_client.py         # AI 瀵硅瘽鏍稿績 (AIClient)
鈹?  鈹溾攢鈹€ conversation.py      # 浼氳瘽绠＄悊 (ConversationManager)
鈹?  鈹溾攢鈹€ speech.py            # 璇煶璇嗗埆 (SpeechRecognizer)
鈹?  鈹溾攢鈹€ computer_control.py  # 鐢佃剳鎺у埗 (PyAutoGUI)
鈹?  鈹溾攢鈹€ desktop_vision.py    # 妗岄潰瑙嗚鍒嗘瀽
鈹?  鈹溾攢鈹€ desktop_awareness.py # 妗岄潰涓诲姩鎰熺煡
鈹?  鈹斺攢鈹€ ...
鈹溾攢鈹€ agent/                   # Agent 瀛愯繘绋嬬郴缁?(閮ㄥ垎瀹炵幇)
鈹溾攢鈹€ computer/                # 鐢佃剳鎺у埗妯″潡 (閮ㄥ垎瀹炵幇)
鈹溾攢鈹€ tests/                   # 娴嬭瘯濂椾欢
鈹斺攢鈹€ docs/                    # 寮€鍙戞枃妗?```

## 瀹夎

### 鐜瑕佹眰

- Python 3.12+
- Windows 10/11 鎴?Linux (X11/Wayland)

### 瀹夎姝ラ

```bash
# 1. 鍏嬮殕浠撳簱
git clone https://github.com/SorasakiHina-219/AronaClient.git
cd AronaClient

# 2. 鍒涘缓铏氭嫙鐜 (鎺ㄨ崘)
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux

# 3. 瀹夎渚濊禆
pip install -r requirements.txt

# 4. 閰嶇疆鐜鍙橀噺
copy .env.example .env  # Windows
# cp .env.example .env  # Linux
# 缂栬緫 .env 骞跺～鍏ヤ綘鐨?API Key
```

### 鐜鍙橀噺閰嶇疆

鍦?`.env` 鏂囦欢涓厤缃細

```ini
# DeepSeek API (蹇呴渶)
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_MODEL=deepseek-chat

# OpenRouter API (鍙€?
OPENROUTER_API_KEY=your_openrouter_api_key_here
OPENROUTER_MODEL=deepseek/deepseek-chat

# 閫夋嫨鎻愪緵鍟?PROVIDER=deepseek
```

## 杩愯

```bash
python main.py
```

## 鍔熻兘璇存槑

### AI 瀵硅瘽

AronaClient 閫氳繃璋冪敤澶栭儴 AI API 瀹炵幇瀵硅瘽鍔熻兘锛?
- **DeepSeek**: 浣跨敤 [deepseek.com](https://platform.deepseek.com) 鐨?API
- **OpenRouter**: 鏀寔澶氱妯″瀷锛岄粯璁や娇鐢?DeepSeek

瀵硅瘽鍔熻兘鍖呮嫭锛?- 澶氫細璇濈鐞?- 鍘嗗彶璁板綍淇濆瓨
- Markdown 娑堟伅娓叉煋
- 涓婁笅鏂囪蹇?
### 璇煶鍚堟垚

- 绯荤粺鍐呯疆 TTS 寮曟搸
- 鏀寔涓枃璇煶杈撳嚭
- 鍙湪璁剧疆涓皟鏁磋閫熷拰闊抽噺

### 3D 鍚夌ゥ鐗?
- 鏀寔 PMX 鏍煎紡妯″瀷锛圡ikuMikuDream 鏍煎紡锛?- 鍙嚜瀹氫箟鍚夌ゥ鐗╀綅缃拰琛屼负
- 榛樿璺緞锛歚data/models/Arona_1.01.pmx`锛堢浉瀵归」鐩牴鐩綍锛?- 鍙€氳繃鐜鍙橀噺 `ARONA_MODEL_DIR` 瑕嗙洊榛樿璺緞

### 妗岄潰鎺у埗

- 灞忓箷鎴浘鍔熻兘
- 榧犳爣鍜岄敭鐩樻帶鍒讹紙閫氳繃 PyAutoGUI锛?- 绐楀彛淇℃伅鑾峰彇锛堥儴鍒嗗姛鑳斤級

## 宸茬煡闄愬埗

### 缂哄け妯″潡

浠ヤ笅妯″潡灏氭湭瀹炵幇锛屽鑷寸浉鍏冲姛鑳藉彈闄愶細

| 缂哄け妯″潡 | 褰卞搷鍔熻兘 | 鐘舵€?|
|---------|---------|------|
| `computer/desktop_access.py` | 妗岄潰瑙嗚鍒嗘瀽 | 寮€鍙戜腑 |
| `computer/screen_capture.py` | 灞忓箷鎴浘 | 寮€鍙戜腑 |
| `computer/window_info.py` | 绐楀彛淇℃伅鑾峰彇 | 寮€鍙戜腑 |
| `agent/authchannel.py` | Agent 璁よ瘉閫氶亾 | 鏈紑濮?|
| `agent/crypto.py` | Agent 鍔犲瘑閫氫俊 | 鏈紑濮?|
| `agent/protocol.py` | Agent 閫氫俊鍗忚 | 鏈紑濮?|
| `agent/winjob.py` | Agent 杩涚▼绠＄悊 | 鏈紑濮?|

**娉ㄦ剰**: 杩欎簺妯″潡鐨勭己澶变笉褰卞搷鏍稿績鍔熻兘鐨勬甯镐娇鐢ㄣ€傜▼搴忓惎鍔ㄦ椂浼氳嚜鍔ㄨ烦杩囩己澶辨ā鍧楋紝妗岄潰瑙嗚鍜?Agent 鍔熻兘灏嗘樉绀轰负涓嶅彲鐢ㄣ€?
### 鍏朵粬闄愬埗

- 浠呮敮鎸?Windows 鍜?Linux 妗岄潰鐜
- 闇€瑕佺ǔ瀹氱殑缃戠粶杩炴帴浠ヨ皟鐢?AI API
- 3D 鍚夌ゥ鐗╁姛鑳介渶瑕?OpenGL 鏀寔

## 璁稿彲璇?
鏈」鐩噰鐢?**GNU General Public License v3.0** (GPL-3.0) 寮€婧愩€?
- 鉁?鍙嚜鐢变娇鐢ㄣ€佷慨鏀瑰拰鍒嗗彂
- 鉁?淇敼鐗堟湰蹇呴』鍚屾牱浠?GPL-3.0 鍙戝竷
- 鈿狅笍 鍟嗕笟浣跨敤闇€閬靛畧 GPL-3.0 鏉℃

璇︾粏鏉℃璇峰弬闃?[LICENSE](LICENSE) 鏂囦欢銆?
### 绗笁鏂逛緷璧?
鏈」鐩娇鐢ㄤ簡浠ヤ笅绗笁鏂圭粍浠讹細

| 缁勪欢 | 璁稿彲璇?| 鐢ㄩ€?|
|------|--------|------|
| PySide6 | LGPL-3.0 / GPL-2.0 / GPL-3.0 | GUI 妗嗘灦 |
| openai | Apache-2.0 | AI API 瀹㈡埛绔?|
| PyAutoGUI | BSD | 榧犳爣閿洏鎺у埗 |
| Pillow | HPND | 鍥惧儚澶勭悊 |
| opencv-python | Apache-2.0 | 妗岄潰瑙嗚 |
| numpy | BSD-3-Clause | 鏁板€艰绠?|
| keyring | MIT | 瀵嗙爜绠＄悊 |
| python-dotenv | BSD-3-Clause | 鐜鍙橀噺鍔犺浇 |
| Markdown | BSD-2-Clause | 娑堟伅娓叉煋 |
| SpeechRecognition | MIT | 璇煶璇嗗埆 |

瀹屾暣澹版槑璇峰弬闃?[NOTICES](NOTICES) 鏂囦欢銆?
## 璐＄尞

娆㈣繋鎻愪氦 Issue 鍜?Pull Request锛?
### 寮€鍙戞祦绋?
1. Fork 鏈粨搴?2. 鍒涘缓鐗规€у垎鏀?(`git checkout -b feature/YourFeature`)
3. 鎻愪氦鏇存敼 (`git commit -am 'Add some feature'`)
4. 鎺ㄩ€佸埌鍒嗘敮 (`git push origin feature/YourFeature`)
5. 鍒涘缓 Pull Request

### 浠ｇ爜瑙勮寖

- 閬靛惊 PEP 8 缂栫爜瑙勮寖
- 娣诲姞蹇呰鐨勬敞閲婂拰鏂囨。瀛楃涓?- 纭繚鎵€鏈夋祴璇曢€氳繃 (`pytest tests/`)

## 鍏嶈矗澹版槑

鏈蒋浠舵寜"鍘熸牱"鎻愪緵锛屼笉鎻愪緵浠讳綍鏄庣ず鎴栨殫绀虹殑鎷呬繚锛屽寘鎷絾涓嶉檺浜庨€傞攢鎬у拰鐗瑰畾鐢ㄩ€旈€傜敤鎬х殑鎷呬繚銆備綔鑰呬笉瀵逛换浣曠洿鎺ユ垨闂存帴鎹熷璐熻矗銆?
## 鑱旂郴鏂瑰紡

- GitHub: [SorasakiHina-219/AronaClient](https://github.com/SorasakiHina-219/AronaClient)
- 闂鍙嶉: [Issues](https://github.com/SorasakiHina-219/AronaClient/issues)

---

**AronaClient** - 浣犵殑妗岄潰 AI 浼翠荆
