"""
╔══════════════════════════════════════════════════════════════════╗
║          RapidGet Downloader  v1.0.0  — Production Build        ║
║          © 2026 Stonepit Labs  ·  Developer: Tanjir Ahmed       ║
╠══════════════════════════════════════════════════════════════════╣
║  Features:                                                       ║
║  1. Multi-threaded chunked downloading (resume-safe)            ║
║  2. Reliable Pause / Resume (per-chunk state persisted)         ║
║  3. Download Queue Manager (add / reorder / delete)             ║
║  4. Premium Glass-theme GUI  (PyQt6 + translucent effects)      ║
║  5. Independent Speed-Meter Monitor (ETA, graph)                ║
║  6. In-app Torrent & Magnet (libtorrent, file selector)         ║
║  7. Browser Extension back-end (WebSocket + native-msg stub)    ║
╚══════════════════════════════════════════════════════════════════╝

DEPENDENCIES  (install once):
    pip install PyQt6 requests yt-dlp psutil websockets

OPTIONAL  (strongly recommended):
    pip install python-libtorrent          # torrent support
    # Windows: winreg is built-in
    # macOS:   launchctl plist is written automatically

CROSS-PLATFORM NOTES:
    • rapidget.ico must live next to this script (Windows tray/icon)
    • macOS uses .icns or falls back to QPixmap — no crash
    • winreg is imported lazily (macOS/Linux safe)
"""

# ──────────────────────────────────────────────────────────────────
#  IMPORTS
# ──────────────────────────────────────────────────────────────────
import sys, os, json, threading, time, math, asyncio, struct, uuid
import platform, shutil, subprocess, webbrowser, traceback, socket, re
from pathlib import Path
from collections import deque
from urllib.parse import urlparse, unquote

import requests
import yt_dlp
import psutil

from PyQt6.QtWidgets import *
from PyQt6.QtCore    import *
from PyQt6.QtGui     import *

# Optional: websockets for browser extension
try:
    import websockets
    import websockets.server
    HAS_WS = True
except ImportError:
    HAS_WS = False

# Optional: libtorrent
try:
    import libtorrent as lt
    HAS_LT = True
except ImportError:
    HAS_LT = False

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
# ── libtorrent priority helpers (lt 1.x and 2.x compatible) ──────
def _build_prio_list(num_files, selected_set):
    """Return priority list for params.file_priorities.
    selected_set=None → all files; set() → no files."""
    if selected_set is None:
        return [4] * num_files
    return [4 if i in selected_set else 0 for i in range(num_files)]

def _set_file_priorities(handle, num_files, selected_set):
    """Call handle.prioritize_files() for both lt 1.x and 2.x."""
    prios = _build_prio_list(num_files, selected_set)
    try:
        import libtorrent as _lt
        typed = [_lt.download_priority_t.normal_priority
                 if p else _lt.download_priority_t.dont_download
                 for p in prios]
        handle.prioritize_files(typed)
    except Exception:
        handle.prioritize_files(prios)



if IS_WIN:
    try:
        import winreg
        HAS_WINREG = True
    except ImportError:
        HAS_WINREG = False
else:
    HAS_WINREG = False

def _decode_torrent_text(value):
    """Decode bytes from .torrent metadata with safe fallbacks."""
    if isinstance(value, str):
        return value
    if not isinstance(value, (bytes, bytearray)):
        return str(value)
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return bytes(value).decode(enc)
        except UnicodeDecodeError:
            pass
    return bytes(value).decode("utf-8", "replace")

def _bdecode(data, pos=0):
    if pos >= len(data):
        raise ValueError("Unexpected end of torrent data")
    token = data[pos:pos + 1]
    if token == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if token == b"l":
        pos += 1
        out = []
        while data[pos:pos + 1] != b"e":
            item, pos = _bdecode(data, pos)
            out.append(item)
        return out, pos + 1
    if token == b"d":
        pos += 1
        out = {}
        while data[pos:pos + 1] != b"e":
            key, pos = _bdecode(data, pos)
            val, pos = _bdecode(data, pos)
            out[key] = val
        return out, pos + 1
    if token.isdigit():
        colon = data.index(b":", pos)
        size = int(data[pos:colon])
        start = colon + 1
        end = start + size
        return data[start:end], end
    raise ValueError(f"Invalid torrent data at byte {pos}")

def parse_torrent_metadata(torrent_path):
    """Return (torrent_name, [(path, size), ...]) without requiring libtorrent."""
    with open(torrent_path, "rb") as fh:
        root, pos = _bdecode(fh.read())
    if not isinstance(root, dict) or b"info" not in root:
        raise ValueError("Invalid .torrent file: missing info dictionary")
    info = root[b"info"]
    name = _decode_torrent_text(
        info.get(b"name.utf-8") or info.get(b"name") or Path(torrent_path).stem
    )
    files = []
    if b"files" in info:
        for entry in info[b"files"]:
            parts = entry.get(b"path.utf-8") or entry.get(b"path") or []
            rel = "/".join(_decode_torrent_text(p) for p in parts)
            files.append((rel or name, int(entry.get(b"length", 0))))
    else:
        files.append((name, int(info.get(b"length", 0))))
    return name, files

def split_rapidget_quality(url):
    marker = "#rapidget_quality="
    if marker not in url:
        return url, None
    base, _, quality = url.partition(marker)
    return base, unquote(quality) or None

def _has_video_edit(item):
    return bool(item.clip_start or item.clip_end or item.crop_preset != "Original")

def _will_postprocess_video(item):
    return item.quality != "mp3" and _has_video_edit(item)

def _time_to_seconds(value):
    value = (value or "").strip()
    if not value:
        return None
    parts = value.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Invalid time: {value}")
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    raise ValueError(f"Invalid time: {value}")

def _crop_filter(preset):
    return {
        "16:9": "crop=trunc(min(iw\\,ih*16/9)/2)*2:trunc(min(ih\\,iw*9/16)/2)*2",
        "9:16": "crop=trunc(min(iw\\,ih*9/16)/2)*2:trunc(min(ih\\,iw*16/9)/2)*2",
        "1:1":  "crop=trunc(min(iw\\,ih)/2)*2:trunc(min(iw\\,ih)/2)*2",
    }.get(preset, "")

requests.packages.urllib3.disable_warnings()

APP_VERSION   = "1.0.0"
# PyInstaller frozen support: use _MEIPASS if available
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    APP_DIR = sys._MEIPASS
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_FILE     = os.path.join(os.path.expanduser("~"), ".rapidget_state.json")
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".rapidget_settings.json")
ICO_PATH      = os.path.join(APP_DIR, "rapidget.ico")

YT_SITES = [
    "youtube.com","youtu.be","facebook.com","fb.watch","instagram.com",
    "twitter.com","x.com","tiktok.com","vimeo.com","dailymotion.com","twitch.tv",
    "bilibili.com","nicovideo.jp","rumble.com",
]

# ══════════════════════════════════════════════════════════════════
#  LOCALISATION
# ══════════════════════════════════════════════════════════════════
LANG = {
    "English": {
        "add":"+ Add","resume":"▶ Resume","pause":"⏸ Pause",
        "remove":"🗑 Remove","folder":"📁 Folder","open":"📄 Open",
        "file":"File","type":"Type","size":"Size","progress":"Progress",
        "speed":"Speed","status":"Status","eta":"ETA",
        "ready":"Ready — Extension active","settings":"Settings","about":"About",
        "downloading":"⬇ Downloading","paused":"⏸ Paused",
        "done":"✅ Done","error":"❌ Error","waiting":"⏳ Waiting","queued":"🕒 Queued",
        "url_placeholder":"Paste URL / Magnet / .torrent here…",
        "direct":"⬇ Direct","video":"🎬 Video","torrent":"🧲 Torrent",
        "quality_title":"Select Quality","quality_sub":"Choose download format:",
        "download":"Download","cancel":"Cancel","fetch":"Fetch Formats",
        "settings_title":"Settings","dl_folder":"Download Folder",
        "browse":"Browse","threads":"Download Threads",
        "auto":"Auto (Recommended)","startup":"Launch at system startup",
        "notification":"Notify when download completes",
        "language":"Language","theme":"Theme","mode":"Mode",
        "net_monitor":"Speed Monitor (24/7)","transparency":"Transparency",
        "pin_top":"Always on top","apply":"Apply","close":"Close",
        "about_title":"About RapidGet","show":"Show","exit":"Exit",
        "open_folder":"Open Folder","open_file":"Open File",
        "port":"Extension WS Port","bg_msg":"Running in background.",
        "best":"Best Quality","p2160":"2160p (4K)","p1080":"1080p MP4",
        "p720":"720p MP4","p480":"480p MP4","p360":"360p MP4","mp3":"MP3 Audio",
        "active":"active","total":"Total","move_up":"Move Up","move_down":"Move Down",
        "torrent_files":"Select files to download:",
        "select_all":"Select All","select_none":"Select None",
        "seeding":"🌱 Seeding","connecting":"🔌 Connecting",
        "dl_speed":"↓","ul_speed":"↑","peers":"peers",
        "queue":"Queue","max_concurrent":"Max concurrent downloads",
        "copy_url":"Copy URL",
    },
    "বাংলা": {
        "add":"+ যোগ","resume":"▶ চালু","pause":"⏸ বিরতি",
        "remove":"🗑 সরান","folder":"📁 ফোল্ডার","open":"📄 খুলুন",
        "file":"ফাইল","type":"ধরন","size":"সাইজ","progress":"অগ্রগতি",
        "speed":"গতি","status":"অবস্থা","eta":"বাকি সময়",
        "ready":"প্রস্তুত — Extension চালু","settings":"সেটিংস","about":"পরিচিতি",
        "downloading":"⬇ নামছে","paused":"⏸ বিরতি",
        "done":"✅ সম্পন্ন","error":"❌ ত্রুটি","waiting":"⏳ অপেক্ষা","queued":"🕒 কিউতে",
        "url_placeholder":"URL / Magnet / .torrent এখানে দিন…",
        "direct":"⬇ সরাসরি","video":"🎬 ভিডিও","torrent":"🧲 টরেন্ট",
        "quality_title":"মান বেছে নিন","quality_sub":"কোন format এ নামাবেন?",
        "download":"নামান","cancel":"বাতিল","fetch":"Format আনুন",
        "settings_title":"সেটিংস","dl_folder":"ডাউনলোড ফোল্ডার",
        "browse":"খুঁজুন","threads":"থ্রেড",
        "auto":"স্বয়ংক্রিয়","startup":"স্টার্টআপে চালু হবে",
        "notification":"ডাউনলোড শেষে নোটিফিকেশন",
        "language":"ভাষা","theme":"থিম","mode":"মোড",
        "net_monitor":"নেটওয়ার্ক মনিটর","transparency":"স্বচ্ছতা",
        "pin_top":"সবসময় সামনে","apply":"প্রয়োগ","close":"বন্ধ",
        "about_title":"পরিচিতি","show":"দেখান","exit":"বন্ধ",
        "open_folder":"ফোল্ডার খুলুন","open_file":"ফাইল খুলুন",
        "port":"Extension পোর্ট","bg_msg":"পেছনে চলছে।",
        "best":"সেরা মান","p2160":"২১৬০p (4K)","p1080":"১০৮০p",
        "p720":"৭২০p","p480":"৪৮০p","p360":"৩৬০p","mp3":"MP3",
        "active":"সক্রিয়","total":"মোট","move_up":"উপরে","move_down":"নিচে",
        "torrent_files":"ফাইল বেছে নিন:","select_all":"সব","select_none":"কিছু না",
        "seeding":"🌱 সিডিং","connecting":"🔌 কানেক্ট","dl_speed":"↓","ul_speed":"↑","peers":"পিয়ার",
        "queue":"কিউ","max_concurrent":"সর্বোচ্চ একসাথে","copy_url":"URL কপি",
    },
    "中文": {
        "add":"+ 添加","resume":"▶ 恢复","pause":"⏸ 暂停",
        "remove":"🗑 删除","folder":"📁 文件夹","open":"📄 打开",
        "file":"文件","type":"类型","size":"大小","progress":"进度",
        "speed":"速度","status":"状态","eta":"剩余时间",
        "ready":"就绪","settings":"设置","about":"关于",
        "downloading":"⬇ 下载中","paused":"⏸ 已暂停",
        "done":"✅ 完成","error":"❌ 错误","waiting":"⏳ 等待","queued":"🕒 队列",
        "url_placeholder":"粘贴 URL / Magnet / .torrent…",
        "direct":"⬇ 直接","video":"🎬 视频","torrent":"🧲 种子",
        "quality_title":"选择质量","quality_sub":"选择下载格式:",
        "download":"下载","cancel":"取消","fetch":"获取格式",
        "settings_title":"设置","dl_folder":"下载文件夹",
        "browse":"浏览","threads":"线程",
        "auto":"自动","startup":"开机启动",
        "notification":"下载完成通知",
        "language":"语言","theme":"主题","mode":"模式",
        "net_monitor":"网络监视器","transparency":"透明度",
        "pin_top":"始终置顶","apply":"应用","close":"关闭",
        "about_title":"关于","show":"显示","exit":"退出",
        "open_folder":"打开文件夹","open_file":"打开文件",
        "port":"扩展端口","bg_msg":"后台运行。",
        "best":"最佳质量","p2160":"2160p (4K)","p1080":"1080p",
        "p720":"720p","p480":"480p","p360":"360p","mp3":"仅音频",
        "active":"活跃","total":"总计","move_up":"上移","move_down":"下移",
        "torrent_files":"选择文件:","select_all":"全选","select_none":"全不选",
        "seeding":"🌱 做种","connecting":"🔌 连接","dl_speed":"↓","ul_speed":"↑","peers":"节点",
        "queue":"队列","max_concurrent":"最大并发","copy_url":"复制链接",
    },
}

def _lang_variant(**overrides):
    data = dict(LANG["English"])
    data.update(overrides)
    return data

LANG.update({
    "हिन्दी": _lang_variant(
        add="+ जोड़ें", resume="▶ फिर शुरू", pause="⏸ रोकें", remove="🗑 हटाएं",
        folder="📁 फोल्डर", open="📄 खोलें", file="फाइल", type="प्रकार", size="साइज",
        progress="प्रगति", speed="स्पीड", status="स्थिति", eta="बाकी समय",
        ready="तैयार — Extension active", settings="सेटिंग्स", about="परिचय",
        downloading="⬇ डाउनलोड हो रहा है", paused="⏸ रुका हुआ", done="✅ पूरा",
        error="❌ त्रुटि", waiting="⏳ प्रतीक्षा", queued="🕒 कतार में",
        url_placeholder="URL / Magnet / .torrent यहां पेस्ट करें…",
        direct="⬇ Direct", video="🎬 वीडियो", torrent="🧲 टोरेंट",
        quality_title="Quality चुनें", quality_sub="Download format चुनें:",
        download="डाउनलोड", cancel="रद्द", fetch="Formats लाएं",
        settings_title="सेटिंग्स", dl_folder="Download Folder", browse="Browse",
        threads="Download Threads", auto="Auto (Recommended)",
        startup="System startup पर चलाएं", notification="Download पूरा होने पर बताएं",
        language="भाषा", theme="Theme", mode="Mode", net_monitor="Speed Monitor (24/7)",
        transparency="Transparency", pin_top="हमेशा ऊपर", apply="Apply", close="Close",
        show="Show", exit="Exit", open_folder="Folder खोलें", open_file="File खोलें",
        best="Best Quality", mp3="MP3 Audio", active="active", total="Total",
        move_up="ऊपर", move_down="नीचे", torrent_files="Download files चुनें:",
        select_all="सब चुनें", select_none="कोई नहीं", peers="peers", queue="Queue",
        max_concurrent="Max concurrent downloads", copy_url="URL copy करें"),
    "粵語": _lang_variant(
        add="+ 加入", resume="▶ 繼續", pause="⏸ 暫停", remove="🗑 移除",
        folder="📁 資料夾", open="📄 開啟", file="檔案", type="類型", size="大小",
        progress="進度", speed="速度", status="狀態", eta="剩餘時間",
        ready="準備好 — Extension active", settings="設定", about="關於",
        downloading="⬇ 下載緊", paused="⏸ 暫停咗", done="✅ 完成",
        error="❌ 錯誤", waiting="⏳ 等待", queued="🕒 排隊中",
        url_placeholder="貼上 URL / Magnet / .torrent…", direct="⬇ 直接",
        video="🎬 影片", torrent="🧲 種子", quality_title="揀畫質",
        quality_sub="揀下載格式:", download="下載", cancel="取消", fetch="取得格式",
        settings_title="設定", dl_folder="下載資料夾", browse="瀏覽",
        threads="下載線程", auto="自動 (建議)", startup="開機時啟動",
        notification="下載完成通知", language="語言", theme="主題", mode="模式",
        net_monitor="速度監視器 (24/7)", transparency="透明度", pin_top="永遠置頂",
        apply="套用", close="關閉", show="顯示", exit="離開",
        open_folder="開資料夾", open_file="開檔案", best="最佳畫質",
        mp3="MP3 音訊", active="進行中", total="總計", move_up="上移",
        move_down="下移", torrent_files="選擇下載檔案:", select_all="全選",
        select_none="全不選", peers="peers", queue="隊列",
        max_concurrent="最大同時下載", copy_url="複製 URL"),
    "Português": _lang_variant(
        add="+ Adicionar", resume="▶ Continuar", pause="⏸ Pausar",
        remove="🗑 Remover", folder="📁 Pasta", open="📄 Abrir",
        file="Arquivo", type="Tipo", size="Tamanho", progress="Progresso",
        speed="Velocidade", status="Estado", eta="Tempo restante",
        ready="Pronto — extensão ativa", settings="Configurações", about="Sobre",
        downloading="⬇ Baixando", paused="⏸ Pausado", done="✅ Concluído",
        error="❌ Erro", waiting="⏳ Aguardando", queued="🕒 Na fila",
        url_placeholder="Cole URL / Magnet / .torrent aqui…", direct="⬇ Direto",
        video="🎬 Vídeo", torrent="🧲 Torrent", quality_title="Selecionar qualidade",
        quality_sub="Escolha o formato:", download="Baixar", cancel="Cancelar",
        fetch="Buscar formatos", settings_title="Configurações",
        dl_folder="Pasta de downloads", browse="Procurar",
        threads="Threads de download", auto="Auto (recomendado)",
        startup="Iniciar com o sistema", notification="Avisar ao concluir",
        language="Idioma", theme="Tema", mode="Modo", net_monitor="Monitor de velocidade (24/7)",
        transparency="Transparência", pin_top="Sempre no topo", apply="Aplicar",
        close="Fechar", show="Mostrar", exit="Sair", open_folder="Abrir pasta",
        open_file="Abrir arquivo", best="Melhor qualidade", mp3="Áudio MP3",
        active="ativo", total="Total", move_up="Mover para cima",
        move_down="Mover para baixo", torrent_files="Selecione os arquivos:",
        select_all="Selecionar tudo", select_none="Selecionar nenhum",
        peers="pares", queue="Fila", max_concurrent="Downloads simultâneos",
        copy_url="Copiar URL"),
    "অসমীয়া": _lang_variant(language="ভাষা", settings="ছেটিংছ", settings_title="ছেটিংছ", download="ডাউনলোড", cancel="বাতিল", file="ফাইল", size="আকাৰ", speed="গতি", status="অৱস্থা"),
    "ગુજરાતી": _lang_variant(language="ભાષા", settings="સેટિંગ્સ", settings_title="સેટિંગ્સ", download="ડાઉનલોડ", cancel="રદ કરો", file="ફાઇલ", size="કદ", speed="ઝડપ", status="સ્થિતિ"),
    "ಕನ್ನಡ": _lang_variant(language="ಭಾಷೆ", settings="ಸೆಟ್ಟಿಂಗ್‌ಗಳು", settings_title="ಸೆಟ್ಟಿಂಗ್‌ಗಳು", download="ಡೌನ್‌ಲೋಡ್", cancel="ರದ್ದು", file="ಫೈಲ್", size="ಗಾತ್ರ", speed="ವೇಗ", status="ಸ್ಥಿತಿ"),
    "کٲشُر": _lang_variant(language="زبان", settings="Settings", settings_title="Settings", download="Download", cancel="Cancel", file="File", size="Size", speed="Speed", status="Status"),
    "कोंकणी": _lang_variant(language="भास", settings="Settings", settings_title="Settings", download="Download", cancel="Cancel", file="फायल", size="Size", speed="Speed", status="Status"),
    "मैथिली": _lang_variant(language="भाषा", settings="सेटिंग्स", settings_title="सेटिंग्स", download="डाउनलोड", cancel="रद्द", file="फाइल", size="आकार", speed="गति", status="स्थिति"),
    "മലയാളം": _lang_variant(language="ഭാഷ", settings="ക്രമീകരണങ്ങൾ", settings_title="ക്രമീകരണങ്ങൾ", download="ഡൗൺലോഡ്", cancel="റദ്ദാക്കുക", file="ഫയൽ", size="വലുപ്പം", speed="വേഗം", status="സ്ഥിതി"),
    "মৈতৈলোন্": _lang_variant(language="লোন্", settings="Settings", settings_title="Settings", download="Download", cancel="Cancel", file="File", size="Size", speed="Speed", status="Status"),
    "मराठी": _lang_variant(language="भाषा", settings="सेटिंग्स", settings_title="सेटिंग्स", download="डाउनलोड", cancel="रद्द", file="फाइल", size="आकार", speed="वेग", status="स्थिती"),
    "नेपाली": _lang_variant(language="भाषा", settings="सेटिङहरू", settings_title="सेटिङहरू", download="डाउनलोड", cancel="रद्द", file="फाइल", size="आकार", speed="गति", status="स्थिति"),
    "ଓଡ଼ିଆ": _lang_variant(language="ଭାଷା", settings="ସେଟିଂସ୍", settings_title="ସେଟିଂସ୍", download="ଡାଉନଲୋଡ୍", cancel="ବାତିଲ୍", file="ଫାଇଲ୍", size="ଆକାର", speed="ଗତି", status="ସ୍ଥିତି"),
    "ਪੰਜਾਬੀ": _lang_variant(language="ਭਾਸ਼ਾ", settings="ਸੈਟਿੰਗਾਂ", settings_title="ਸੈਟਿੰਗਾਂ", download="ਡਾਊਨਲੋਡ", cancel="ਰੱਦ", file="ਫਾਈਲ", size="ਆਕਾਰ", speed="ਗਤੀ", status="ਹਾਲਤ"),
    "संस्कृतम्": _lang_variant(language="भाषा", settings="विन्यासाः", settings_title="विन्यासाः", download="अवतारणम्", cancel="निरस्तम्", file="सञ्चिका", size="परिमाणम्", speed="वेगः", status="स्थितिः"),
    "ᱥᱟᱱᱛᱟᱲᱤ": _lang_variant(language="ᱯᱟᱹᱨᱥᱤ", settings="Settings", settings_title="Settings", download="Download", cancel="Cancel", file="File", size="Size", speed="Speed", status="Status"),
    "سنڌي": _lang_variant(language="ٻولي", settings="سيٽنگون", settings_title="سيٽنگون", download="ڊائونلوڊ", cancel="رد", file="فائل", size="سائيز", speed="رفتار", status="حالت"),
    "தமிழ்": _lang_variant(language="மொழி", settings="அமைப்புகள்", settings_title="அமைப்புகள்", download="பதிவிறக்கு", cancel="ரத்து", file="கோப்பு", size="அளவு", speed="வேகம்", status="நிலை"),
    "తెలుగు": _lang_variant(language="భాష", settings="సెట్టింగ్స్", settings_title="సెట్టింగ్స్", download="డౌన్‌లోడ్", cancel="రద్దు", file="ఫైల్", size="పరిమాణం", speed="వేగం", status="స్థితి"),
    "اردو": _lang_variant(language="زبان", settings="سیٹنگز", settings_title="سیٹنگز", download="ڈاؤن لوڈ", cancel="منسوخ", file="فائل", size="سائز", speed="رفتار", status="حالت"),
    "बर'/बड़ो": _lang_variant(language="राव", settings="Settings", settings_title="Settings", download="Download", cancel="Cancel", file="File", size="Size", speed="Speed", status="Status"),
    "डोगरी": _lang_variant(language="भाषा", settings="सेटिंग्स", settings_title="सेटिंग्स", download="डाउनलोड", cancel="रद्द", file="फाइल", size="आकार", speed="रफ्तार", status="स्थिति"),
})

_STONEPIT_LANG_DEFAULTS = {
    "stonepit_sub": "Text command assistant - asks permission before downloading.",
    "stonepit_placeholder": "Example: download Chrome / download relaxing piano from YouTube",
    "stonepit_ask": "Ask",
    "stonepit_enter": "Enter",
    "stonepit_approve": "Approve Download",
    "stonepit_ready": "I am ready. Tell me what you want to download.",
    "stonepit_empty": "Tell me what to download. Example: download Chrome, download VLC, download lo-fi music from YouTube.",
    "stonepit_link_found": "I found your link.",
    "stonepit_found": "Found",
    "stonepit_source": "Source",
    "stonepit_site_size": "Size",
    "stonepit_approve_hint": "Press Approve Download to continue.",
    "stonepit_approved": "Approved. Opening RapidGet's download permission flow.",
    "stonepit_searching_youtube": "Searching YouTube",
    "stonepit_no_youtube": "No YouTube result found. Please write a more specific command.",
    "stonepit_youtube_note": "First YouTube result. Approving will open RapidGet's quality/format permission dialog.",
    "stonepit_youtube_error": "Could not search YouTube",
    "stonepit_torrent_safe": "For torrent downloads, provide a legal/public magnet or .torrent URL. I will not search copyrighted movie/software torrents, but RapidGet can show the file list and ask permission for legal torrent links.",
    "stonepit_unknown": "I currently understand software direct links, pasted URL/magnet links, and YouTube search commands. Example: download VLC, download Chrome, download relaxing piano from YouTube.",
    "stonepit_chrome_note": "Official Google Chrome installer.",
    "stonepit_firefox_note": "Official latest Mozilla Firefox installer.",
    "stonepit_7zip_note": "Latest x64 installer from the official 7-Zip download page.",
    "stonepit_7zip_error": "I could not find the direct 7-Zip installer link. Paste the official page link and I will add it to the download queue.",
    "stonepit_vlc_note": "VLC installer from the official VideoLAN source.",
    "stonepit_vlc_error": "I could not find the direct VLC installer link. Paste the official VideoLAN link and I will add it to the download queue.",
    "stonepit_mic_missing": "Mic not available. Install SpeechRecognition + PyAudio to use voice.",
    "stonepit_mic_ready": "Mic ready - say Stonepit, then tell me what to download.",
    "stonepit_mic_listening": "Listening...",
    "stonepit_wake_reply": "Hmm, ehem, I am here. Tell me.",
    "stonepit_voice_label": "Voice",
    "stonepit_voice_boy": "Boy",
    "stonepit_voice_girl": "Girl",
}
_STONEPIT_LANG_OVERRIDES = {
    "বাংলা": {
        "stonepit_sub": "Text command assistant - download করার আগে সবসময় permission চাইবে।",
        "stonepit_placeholder": "যেমন: Chrome নামাও / YouTube থেকে relaxing piano নামাও",
        "stonepit_ask": "জিজ্ঞেস করুন",
        "stonepit_enter": "Enter",
        "stonepit_approve": "Download অনুমতি দিন",
        "stonepit_ready": "আমি ready। আপনি কী নামাতে চান লিখুন।",
        "stonepit_empty": "কী নামাতে হবে বলুন। যেমন: Chrome নামাও, VLC নামাও, YouTube থেকে lo-fi music নামাও।",
        "stonepit_link_found": "আপনার দেওয়া link পাওয়া গেছে।",
        "stonepit_found": "পাওয়া গেছে",
        "stonepit_source": "Source",
        "stonepit_site_size": "Size",
        "stonepit_approve_hint": "নামাতে চাইলে Approve Download চাপুন।",
        "stonepit_approved": "Approved. RapidGet-এর download permission flow খুলছি।",
        "stonepit_searching_youtube": "YouTube-এ খুঁজছি",
        "stonepit_no_youtube": "কোন YouTube result পাওয়া যায়নি। আরেকটু নির্দিষ্ট করে লিখুন।",
        "stonepit_youtube_note": "প্রথম YouTube result। Approve করলে RapidGet quality/format permission দেখাবে।",
        "stonepit_youtube_error": "YouTube search করা যায়নি",
        "stonepit_torrent_safe": "Torrent নামাতে হলে legal/public magnet বা .torrent URL দিন। Copyrighted movie/software torrent আমি খুঁজে দেব না, কিন্তু legal torrent link দিলে RapidGet file list দেখিয়ে permission নিয়ে নামাবে।",
        "stonepit_unknown": "আমি এখন software direct link, pasted URL/magnet, আর YouTube search command বুঝি। উদাহরণ: VLC নামাও, Chrome নামাও, YouTube থেকে relaxing piano নামাও।",
        "stonepit_chrome_note": "Google-এর official Chrome installer।",
        "stonepit_firefox_note": "Mozilla-এর official latest Firefox installer।",
        "stonepit_7zip_note": "7-Zip official download page থেকে latest x64 installer।",
        "stonepit_7zip_error": "7-Zip-এর direct installer link বের করা যায়নি। Official page থেকে link paste করলে আমি download queue-তে দেব।",
        "stonepit_vlc_note": "VideoLAN official source থেকে VLC installer।",
        "stonepit_vlc_error": "VLC-এর direct installer link বের করা যায়নি। Official VideoLAN link paste করলে আমি download queue-তে দেব।",
        "stonepit_mic_missing": "Mic পাওয়া যায়নি। Voice ব্যবহার করতে SpeechRecognition + PyAudio লাগবে।",
        "stonepit_mic_ready": "Mic ready - Stonepit বলে ডাকুন, তারপর command বলুন।",
        "stonepit_mic_listening": "শুনছি...",
        "stonepit_wake_reply": "হুম, এহেম, আমি আছি, বলো।",
        "stonepit_voice_label": "কণ্ঠ",
        "stonepit_voice_boy": "ছেলে",
        "stonepit_voice_girl": "মেয়ে",
    },
    "हिन्दी": {
        "stonepit_sub": "Text command assistant - download से पहले हमेशा permission मांगेगा।",
        "stonepit_placeholder": "जैसे: Chrome डाउनलोड करो / YouTube से relaxing piano डाउनलोड करो",
        "stonepit_ask": "पूछें",
        "stonepit_enter": "Enter",
        "stonepit_approve": "Download की अनुमति दें",
        "stonepit_ready": "मैं ready हूं। बताइए क्या download करना है।",
        "stonepit_found": "मिला",
        "stonepit_approve_hint": "Download जारी रखने के लिए Approve Download दबाएं।",
    },
    "Português": {
        "stonepit_sub": "Assistente por texto - sempre pede permissão antes de baixar.",
        "stonepit_placeholder": "Exemplo: baixar Chrome / baixar piano relaxante do YouTube",
        "stonepit_ask": "Perguntar",
        "stonepit_enter": "Enter",
        "stonepit_approve": "Aprovar download",
        "stonepit_ready": "Estou pronto. Diga o que você quer baixar.",
        "stonepit_found": "Encontrado",
        "stonepit_approve_hint": "Pressione Aprovar download para continuar.",
    },
    "粵語": {
        "stonepit_sub": "文字指令助手 - 下載之前一定會問你批准。",
        "stonepit_placeholder": "例子: 下載 Chrome / YouTube 下載 relaxing piano",
        "stonepit_ask": "問",
        "stonepit_enter": "Enter",
        "stonepit_approve": "批准下載",
        "stonepit_ready": "我準備好。你想下載乜嘢？",
        "stonepit_found": "搵到",
        "stonepit_approve_hint": "想下載就按批准下載。",
    },
}
for _lname, _ldata in LANG.items():
    for _k, _v in _STONEPIT_LANG_DEFAULTS.items():
        _ldata.setdefault(_k, _v)
    if _lname in _STONEPIT_LANG_OVERRIDES:
        _ldata.update(_STONEPIT_LANG_OVERRIDES[_lname])

THEMES = {
    "Cyan":   {"accent":"#00d4ff","accent2":"#0099cc"},
    "Purple": {"accent":"#a855f7","accent2":"#7c3aed"},
    "Green":  {"accent":"#00b894","accent2":"#00897b"},
    "Orange": {"accent":"#fd7c2a","accent2":"#e55a00"},
    "Red":    {"accent":"#e94560","accent2":"#c0392b"},
    "Pink":   {"accent":"#fd79a8","accent2":"#e84393"},
    "Gold":   {"accent":"#ffd93d","accent2":"#f9a825"},
    "White":  {"accent":"#cccccc","accent2":"#999999"},
    "Icy Acrylic": {"accent":"#8edcff","accent2":"#2c8fbe","frosted":True},
    "Cracked Glass": {"accent":"#a8d8ff","accent2":"#5fa8d3","cracked":True},
    "Frosted Glass": {"accent":"#6fd3e8","accent2":"#3da9c9","frosted":True},
    "macOS":  {"accent":"#0a84ff","accent2":"#0066cc","mac":True},
    "Windows": {"accent":"#0078d4","accent2":"#005a9e","win":True},
}

MODES = {
    "Dark":       {"bg":"#0d1117","bg2":"#161b22","bg3":"#21262d","text":"#e6edf3","text2":"#c9d1d9","text3":"#8b949e","border":"#30363d"},
    "Light":      {"bg":"#f0f4f8","bg2":"#ffffff","bg3":"#e2e8f0","text":"#1a202c","text2":"#2d3748","text3":"#718096","border":"#cbd5e0"},
    "OS Default": {"bg":"#1e1e1e","bg2":"#2d2d2d","bg3":"#3c3c3c","text":"#ffffff","text2":"#dddddd","text3":"#aaaaaa","border":"#555555"},
    "Cracked Glass": {"bg":"#070e1a","bg2":"#0b1628","bg3":"#0f1e36","text":"#dff0ff","text2":"#b8d8f0","text3":"#6a9abf","border":"#1a3a5c"},
    "Frosted Glass": {"bg":"#1a2630","bg2":"#22313d","bg3":"#2b3d4a","text":"#eaf4f8","text2":"#c2dae4","text3":"#8aa9b6","border":"#3a5060"},
    "macOS Dark":  {"bg":"#1c1c1e","bg2":"#2c2c2e","bg3":"#3a3a3c","text":"#ffffff","text2":"#ebebf5","text3":"#8e8e93","border":"#3a3a3c"},
    "macOS Light": {"bg":"#f2f2f7","bg2":"#ffffff","bg3":"#e5e5ea","text":"#000000","text2":"#1c1c1e","text3":"#8e8e93","border":"#c6c6c8"},
    "Windows Dark":  {"bg":"#202020","bg2":"#2d2d2d","bg3":"#383838","text":"#ffffff","text2":"#e0e0e0","text3":"#9d9d9d","border":"#464646"},
    "Windows Light": {"bg":"#f3f3f3","bg2":"#ffffff","bg3":"#ebebeb","text":"#1a1a1a","text2":"#323232","text3":"#767676","border":"#d1d1d1"},
    "Icy Acrylic": {"bg":"#173141","bg2":"#244758","bg3":"#315c70","text":"#f2fbff","text2":"#d7edf6","text3":"#9fc8d8","border":"#5f91a8"},
}

# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════
def get_default_dir():
    d = Path.home() / "Downloads" / "RapidGet"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)

def load_settings():
    defaults = {
        "language":"English","theme":"Cyan","mode":"Dark","opacity":100,
        "download_folder":get_default_dir(),"threads":"auto","startup":False,
        "notification":True,"net_monitor":False,"net_opacity":90,
        "net_pin_top":True,"net_locked":False,"net_pos_x":100,"net_pos_y":100,
        "net_dock_taskbar":False,"ext_port":49152,"max_concurrent":3,
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults

def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

# Map legacy (emoji-prefixed / renamed) theme & mode keys from older
# settings.json files onto the current names so loading never KeyErrors.
_LEGACY_THEME = {
    "💎 Cracked Glass": "Cracked Glass",
    "🧊 Frosted Glass": "Frosted Glass",
    "🍎 macOS": "macOS",
    "🪟 Windows": "Windows",
}
_LEGACY_MODE = {
    "💎 Cracked Glass": "Cracked Glass",
    "🧊 Frosted Glass": "Frosted Glass",
    "🍎 macOS Dark": "macOS Dark",
    "🍎 macOS Light": "macOS Light",
    "🪟 Windows Dark": "Windows Dark",
    "🪟 Windows Light": "Windows Light",
}

def resolve_theme(name):
    """Return a valid THEMES entry, migrating legacy/unknown names."""
    name = _LEGACY_THEME.get(name, name)
    if name in THEMES:
        return name, THEMES[name]
    return "Cyan", THEMES["Cyan"]

def resolve_mode(name):
    """Return a valid MODES entry, migrating legacy/unknown names."""
    name = _LEGACY_MODE.get(name, name)
    if name in ("Cracked Glass", "Frosted Glass", "Icy Acrylic",
                "macOS Dark", "Windows Dark"):
        name = "Dark"
    elif name in ("macOS Light", "Windows Light"):
        name = "Light"
    if name in MODES:
        return name, MODES[name]
    return "Dark", MODES["Dark"]


def hrx(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def is_light_hex(h):
    try:
        r, g, b = hrx(h)
        return (r * 299 + g * 587 + b * 114) / 1000 > 160
    except Exception:
        return False

def readable_on_rgb(r, g, b):
    return "#101820" if (r * 299 + g * 587 + b * 114) / 1000 > 150 else "#ffffff"

def fmt_bytes(b):
    if b <= 0: return "—"
    for unit, thr in [("GB",1<<30),("MB",1<<20),("KB",1<<10)]:
        if b >= thr:
            return f"{b/thr:.2f} {unit}" if unit=="GB" else f"{b/thr:.1f} {unit}"
    return f"{b} B"

def fmt_eta(seconds):
    if seconds <= 0 or seconds > 86400*30: return "—"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    if h: return f"{h}h {m}m"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def probe_url_size(url):
    """Best-effort direct URL size lookup without downloading the file."""
    try:
        r = requests.head(url, verify=False, timeout=10, allow_redirects=True)
        if r.headers.get("content-length"):
            return int(r.headers.get("content-length", 0))
    except Exception:
        pass
    try:
        r = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True,
                         verify=False, timeout=12, allow_redirects=True)
        cr = r.headers.get("content-range", "")
        if "/" in cr:
            return int(cr.rsplit("/", 1)[-1])
        if r.headers.get("content-length") and r.status_code != 200:
            return int(r.headers.get("content-length", 0))
    except Exception:
        pass
    return 0

def parse_size_text(text):
    m = re.search(r"(?:~\s*)?([\d.]+)\s*(GB|MB|KB|B)", text or "", re.I)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"GB": 1 << 30, "MB": 1 << 20, "KB": 1 << 10, "B": 1}.get(unit, 1)
    return int(val * mult)

def detect_threads(url):
    try:
        r = requests.head(url, verify=False, timeout=8, allow_redirects=True)
        if r.headers.get("accept-ranges","") == "bytes":
            sz = int(r.headers.get("content-length", 0))
            if sz > 100 * 1<<20: return 32
            if sz > 50  * 1<<20: return 16
            if sz > 10  * 1<<20: return 8
            return 4
        return 1
    except Exception:
        return 4

def set_startup(enable):
    """Cross-platform startup registration."""
    if IS_WIN and HAS_WINREG:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE)
            if enable:
                winreg.SetValueEx(key,"RapidGet",0,winreg.REG_SZ,
                    f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"')
            else:
                try: winreg.DeleteValue(key,"RapidGet")
                except: pass
            winreg.CloseKey(key)
        except Exception:
            pass
    elif IS_MAC:
        plist_dir  = Path.home() / "Library" / "LaunchAgents"
        plist_path = plist_dir / "com.stonepitlabs.rapidget.plist"
        plist_dir.mkdir(parents=True, exist_ok=True)
        if enable:
            plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.stonepitlabs.rapidget</string>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string>
    <string>{os.path.abspath(sys.argv[0])}</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>"""
            plist_path.write_text(plist)
        else:
            plist_path.unlink(missing_ok=True)

def open_path(path):
    """Open file or folder cross-platform."""
    if not os.path.exists(path):
        return
    if IS_WIN:
        os.startfile(path)
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])

def open_folder_select(path):
    """Open containing folder and select the file."""
    if not os.path.exists(path):
        return
    if IS_WIN:
        subprocess.Popen(["explorer", "/select,", path])
    else:
        open_path(os.path.dirname(path))

def build_style(theme, mode):
    a  = theme["accent"];  a2 = theme["accent2"]
    ar, ag, ab = hrx(a)
    bg  = mode["bg"];   bg2 = mode["bg2"];   bg3  = mode["bg3"]
    txt = mode["text"]; txt2= mode["text2"]; txt3 = mode["text3"]; brd = mode["border"]
    cracked = bool(theme.get("cracked"))
    frosted = bool(theme.get("frosted"))
    mac     = bool(theme.get("mac"))
    win     = bool(theme.get("win"))
    return a, a2, ar, ag, ab, bg, bg2, bg3, txt, txt2, txt3, brd, cracked, frosted, mac, win

def global_style(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked=False,frosted=False,mac=False,win=False):
    # Special glass styles use dark translucent controls. In Light mode,
    # fall back to the generic readable controls while keeping the theme accent.
    if is_light_hex(bg) and (cracked or frosted):
        cracked = False
        frosted = False

    if mac:
        # ── macOS Aqua/San Francisco style ─────────────────────────
        # Rounded corners everywhere, vibrancy-like translucency, SF Pro font stack
        # Subtle shadows, thin borders, traffic-light button feel
        return f"""
    QWidget{{background:{bg};color:{txt};font-family:'-apple-system','SF Pro Display','Helvetica Neue',Arial,sans-serif;font-size:13px;}}
    QLineEdit{{background:{bg2};border:1px solid {brd};border-radius:10px;padding:8px 14px;font-size:13px;color:{txt};selection-background-color:rgba({ar},{ag},{ab},40);}}
    QLineEdit:focus{{border:2px solid rgba({ar},{ag},{ab},200);padding:7px 13px;background:{bg2};}}
    QProgressBar{{background:{bg3};border:none;border-radius:6px;text-align:center;color:{txt};font-size:11px;min-height:18px;}}
    QProgressBar::chunk{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},220),stop:1 rgba({ar},{ag},{ab},180));border-radius:6px;}}
    QScrollBar:vertical{{background:transparent;width:7px;border-radius:3px;margin:2px;}}
    QScrollBar::handle:vertical{{background:rgba(128,128,128,80);border-radius:3px;min-height:24px;}}
    QScrollBar::handle:vertical:hover{{background:rgba(128,128,128,140);}}
    QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
    QMenu{{background:{bg2};border:1px solid rgba(128,128,128,30);border-radius:10px;color:{txt};padding:6px 0px;}}
    QMenu::item{{padding:7px 20px;border-radius:6px;margin:0 6px;}}
    QMenu::item:selected{{background:rgba({ar},{ag},{ab},180);color:#ffffff;}}
    QStatusBar{{background:transparent;color:{txt3};font-size:11px;}}
    QDialog{{background:{bg};color:{txt};border-radius:12px;}}
    QLabel{{color:{txt2};background:transparent;}}
    QComboBox{{background:{bg2};border:1px solid {brd};border-radius:8px;padding:6px 12px;color:{txt};min-width:140px;}}
    QComboBox::drop-down{{border:none;width:20px;}}
    QComboBox QAbstractItemView{{background:{bg};color:{txt};border:1px solid {brd};border-radius:8px;selection-background-color:rgba({ar},{ag},{ab},180);selection-color:#ffffff;outline:0;}}
    QCheckBox{{color:{txt2};font-size:13px;spacing:8px;}}
    QCheckBox::indicator{{width:18px;height:18px;border-radius:9px;border:1.5px solid {brd};background:{bg2};}}
    QCheckBox::indicator:checked{{background:rgba({ar},{ag},{ab},230);border-color:rgba({ar},{ag},{ab},230);image:none;}}
    QSlider::groove:horizontal{{background:{bg3};height:4px;border-radius:2px;}}
    QSlider::handle:horizontal{{background:#ffffff;width:18px;height:18px;border-radius:9px;margin:-7px 0;border:0.5px solid rgba(0,0,0,20);}}
    QSlider::sub-page:horizontal{{background:rgba({ar},{ag},{ab},200);border-radius:2px;}}
    QPushButton{{background:{bg2};border:1px solid {brd};border-radius:8px;padding:7px 16px;color:{txt};font-size:13px;}}
    QPushButton:hover{{background:{bg3};border-color:rgba({ar},{ag},{ab},100);}}
    QPushButton:pressed{{background:{bg3};}}
    QScrollArea{{background:transparent;border:none;}}
    QTableWidget{{background:{bg2};border:1px solid {brd};border-radius:10px;color:{txt};font-size:13px;gridline-color:transparent;outline:0;}}
    QTableWidget::item{{padding:6px 10px;border-bottom:1px solid {brd};color:{txt2};}}
    QTableWidget::item:selected{{background:rgba({ar},{ag},{ab},160);color:#ffffff;}}
    QHeaderView::section{{background:{bg3};border:none;border-bottom:1px solid {brd};padding:7px 10px;color:{txt3};font-size:11px;font-weight:600;letter-spacing:0.5px;}}
    QSpinBox{{background:{bg2};border:1px solid {brd};border-radius:8px;padding:5px 8px;color:{txt};}}
    """

    if win:
        # ── Windows 11 Fluent Design style ────────────────────────
        # Rounded corners (8px), acrylic-like subtle blur tint, Segoe UI Variable
        # Consistent 1px stroke borders, filled accent buttons, reveal highlight effect
        return f"""
    QWidget{{background:{bg};color:{txt};font-family:'Segoe UI Variable','Segoe UI',Arial,sans-serif;font-size:13px;}}
    QLineEdit{{background:{bg2};border:1px solid {brd};border-bottom:2px solid rgba({ar},{ag},{ab},140);border-radius:4px;padding:8px 12px;font-size:13px;color:{txt};}}
    QLineEdit:focus{{border:1px solid rgba({ar},{ag},{ab},180);border-bottom:2px solid rgba({ar},{ag},{ab},255);background:{bg2};}}
    QProgressBar{{background:{bg3};border:none;border-radius:3px;text-align:center;color:{txt};font-size:11px;min-height:18px;}}
    QProgressBar::chunk{{background:rgba({ar},{ag},{ab},220);border-radius:3px;}}
    QScrollBar:vertical{{background:transparent;width:8px;border-radius:4px;margin:0;}}
    QScrollBar::handle:vertical{{background:rgba(128,128,128,70);border-radius:4px;min-height:20px;}}
    QScrollBar::handle:vertical:hover{{background:rgba(128,128,128,130);}}
    QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
    QMenu{{background:{bg2};border:1px solid {brd};border-radius:8px;color:{txt};padding:4px 0;}}
    QMenu::item{{padding:8px 18px;border-radius:4px;margin:2px 4px;}}
    QMenu::item:selected{{background:rgba({ar},{ag},{ab},25);color:{txt};}}
    QMenu::separator{{height:1px;background:{brd};margin:4px 12px;}}
    QStatusBar{{background:transparent;color:{txt3};font-size:11px;}}
    QDialog{{background:{bg};color:{txt};}}
    QLabel{{color:{txt2};background:transparent;}}
    QComboBox{{background:{bg2};border:1px solid {brd};border-bottom:2px solid rgba({ar},{ag},{ab},120);border-radius:4px;padding:6px 10px;color:{txt};min-width:140px;}}
    QComboBox::drop-down{{border:none;width:20px;}}
    QComboBox QAbstractItemView{{background:{bg2};color:{txt};border:1px solid {brd};border-radius:4px;selection-background-color:rgba({ar},{ag},{ab},25);outline:0;}}
    QCheckBox{{color:{txt2};font-size:13px;spacing:8px;}}
    QCheckBox::indicator{{width:18px;height:18px;border-radius:4px;border:1px solid {brd};background:{bg2};}}
    QCheckBox::indicator:checked{{background:rgba({ar},{ag},{ab},230);border-color:rgba({ar},{ag},{ab},230);}}
    QSlider::groove:horizontal{{background:{bg3};height:4px;border-radius:2px;}}
    QSlider::handle:horizontal{{background:rgba({ar},{ag},{ab},230);width:16px;height:16px;border-radius:8px;margin:-6px 0;border:2px solid {bg2};}}
    QSlider::sub-page:horizontal{{background:rgba({ar},{ag},{ab},200);border-radius:2px;}}
    QPushButton{{background:{bg3};border:1px solid {brd};border-radius:6px;padding:7px 16px;color:{txt};font-size:13px;}}
    QPushButton:hover{{background:rgba({ar},{ag},{ab},15);border-color:rgba({ar},{ag},{ab},60);}}
    QPushButton:pressed{{background:rgba({ar},{ag},{ab},25);}}
    QScrollArea{{background:transparent;border:none;}}
    QTableWidget{{background:{bg2};border:1px solid {brd};border-radius:8px;color:{txt};font-size:13px;gridline-color:transparent;outline:0;}}
    QTableWidget::item{{padding:6px 10px;border-bottom:1px solid {brd};color:{txt2};}}
    QTableWidget::item:selected{{background:rgba({ar},{ag},{ab},25);color:{txt};border-left:2px solid rgba({ar},{ag},{ab},200);}}
    QHeaderView::section{{background:{bg3};border:none;border-bottom:1px solid {brd};padding:7px 10px;color:{txt2};font-size:11px;font-weight:600;letter-spacing:0.5px;}}
    QSpinBox{{background:{bg2};border:1px solid {brd};border-bottom:2px solid rgba({ar},{ag},{ab},120);border-radius:4px;padding:5px 8px;color:{txt};}}
    """

    if frosted:
        # ── Frosted Glass — Windows 11 Acrylic style ──────────────
        # Semi-translucent dark glass, soft sky-blue/cyan accent,
        # rounded corners, subtle layered transparency (acrylic feel).
        return f"""
    QWidget{{background:{bg};color:{txt};font-family:'Segoe UI Variable','Segoe UI','.AppleSystemUIFont',Arial,sans-serif;font-size:13px;}}
    QLineEdit{{background:rgba(255,255,255,18);border:1px solid rgba(255,255,255,30);border-bottom:1px solid rgba({ar},{ag},{ab},120);border-radius:8px;padding:9px 14px;font-size:13px;color:{txt};selection-background-color:rgba({ar},{ag},{ab},90);}}
    QLineEdit:focus{{border:1px solid rgba({ar},{ag},{ab},160);border-bottom:2px solid rgba({ar},{ag},{ab},220);background:rgba(255,255,255,28);}}
    QProgressBar{{background:rgba(255,255,255,14);border:1px solid rgba(255,255,255,24);border-radius:7px;text-align:center;color:{txt2};font-size:11px;min-height:19px;}}
    QProgressBar::chunk{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},200),stop:1 rgba({ar},{ag},{ab},120));border-radius:6px;}}
    QScrollBar:vertical{{background:transparent;width:8px;border-radius:4px;margin:2px;}}
    QScrollBar::handle:vertical{{background:rgba(255,255,255,55);border-radius:4px;min-height:24px;}}
    QScrollBar::handle:vertical:hover{{background:rgba(255,255,255,95);}}
    QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
    QMenu{{background:rgba(34,49,61,235);border:1px solid rgba(255,255,255,30);border-radius:10px;color:{txt};padding:6px 0;}}
    QMenu::item{{padding:7px 20px;border-radius:6px;margin:0 6px;}}
    QMenu::item:selected{{background:rgba({ar},{ag},{ab},150);color:#ffffff;}}
    QMenu::separator{{height:1px;background:rgba(255,255,255,25);margin:4px 12px;}}
    QStatusBar{{background:transparent;color:{txt3};font-size:11px;}}
    QDialog{{background:{bg};color:{txt};border-radius:14px;}}
    QLabel{{color:{txt2};background:transparent;}}
    QComboBox{{background:rgba(255,255,255,18);border:1px solid rgba(255,255,255,30);border-radius:8px;padding:6px 12px;color:{txt};min-width:140px;}}
    QComboBox::drop-down{{border:none;width:20px;}}
    QComboBox QAbstractItemView{{background:rgba(34,49,61,245);color:{txt};border:1px solid rgba(255,255,255,30);border-radius:8px;selection-background-color:rgba({ar},{ag},{ab},150);selection-color:#ffffff;outline:0;}}
    QCheckBox{{color:{txt2};font-size:13px;spacing:8px;}}
    QCheckBox::indicator{{width:18px;height:18px;border-radius:6px;border:1px solid rgba(255,255,255,60);background:rgba(255,255,255,18);}}
    QCheckBox::indicator:checked{{background:rgba({ar},{ag},{ab},210);border-color:rgba({ar},{ag},{ab},210);image:none;}}
    QSlider::groove:horizontal{{background:rgba(255,255,255,20);height:5px;border-radius:3px;}}
    QSlider::handle:horizontal{{background:rgba({ar},{ag},{ab},230);width:18px;height:18px;border-radius:9px;margin:-7px 0;border:2px solid rgba(255,255,255,40);}}
    QSlider::sub-page:horizontal{{background:rgba({ar},{ag},{ab},180);border-radius:3px;}}
    QPushButton{{background:rgba(255,255,255,16);border:1px solid rgba(255,255,255,28);border-radius:9px;padding:8px 16px;color:{txt};font-size:13px;}}
    QPushButton:hover{{background:rgba(255,255,255,30);border-color:rgba({ar},{ag},{ab},140);}}
    QPushButton:pressed{{background:rgba(255,255,255,42);}}
    QScrollArea{{background:transparent;border:none;}}
    QTableWidget{{background:rgba(255,255,255,10);border:1px solid rgba(255,255,255,26);border-radius:12px;color:{txt};font-size:13px;gridline-color:transparent;outline:0;}}
    QTableWidget::item{{padding:6px 10px;border-bottom:1px solid rgba(255,255,255,18);color:{txt2};}}
    QTableWidget::item:selected{{background:rgba({ar},{ag},{ab},130);color:#ffffff;}}
    QHeaderView::section{{background:rgba(255,255,255,16);border:none;border-bottom:1px solid rgba(255,255,255,28);padding:7px 10px;color:{txt3};font-size:11px;font-weight:600;letter-spacing:0.5px;}}
    QSpinBox{{background:rgba(255,255,255,18);border:1px solid rgba(255,255,255,30);border-radius:8px;padding:5px 8px;color:{txt};}}
    """

    if cracked:
        # ── Cracked / broken glass visual style ───────────────────
        return f"""
    QWidget{{background:{bg};color:{txt};font-family:'Segoe UI','.AppleSystemUIFont',Arial,sans-serif;}}
    QLineEdit{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(10,28,55,220),stop:0.4 rgba(6,18,38,200),stop:1 rgba(14,34,64,230));border:1px solid rgba({ar},{ag},{ab},160);border-top:2px solid rgba({ar},{ag},{ab},220);border-right:1px solid rgba({ar},{ag},{ab},80);border-radius:2px;padding:9px 14px;font-size:13px;color:{txt};}}
    QLineEdit:focus{{border:2px solid rgba({ar},{ag},{ab},255);border-top:2px solid rgba(255,255,255,180);background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(15,40,75,240),stop:1 rgba(8,22,48,220));}}
    QProgressBar{{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 rgba(8,20,40,200),stop:1 rgba(4,12,28,220));border:1px solid rgba({ar},{ag},{ab},100);border-top:1px solid rgba({ar},{ag},{ab},200);border-radius:1px;text-align:center;color:{txt};font-size:11px;min-height:20px;}}
    QProgressBar::chunk{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},80),stop:0.3 rgba({ar},{ag},{ab},220),stop:0.7 rgba(255,255,255,180),stop:1 rgba({ar},{ag},{ab},200));border-radius:0px;}}
    QScrollBar:vertical{{background:rgba(6,14,30,180);width:6px;border-radius:0px;margin:0;}}
    QScrollBar::handle:vertical{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},60),stop:0.5 rgba({ar},{ag},{ab},160),stop:1 rgba({ar},{ag},{ab},60));border-radius:0px;min-height:20px;}}
    QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
    QMenu{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(8,20,42,245),stop:1 rgba(4,12,28,250));border:1px solid rgba({ar},{ag},{ab},80);border-top:2px solid rgba({ar},{ag},{ab},200);border-radius:0px;color:{txt2};padding:4px;}}
    QMenu::item{{padding:8px 16px;border-radius:0px;color:{txt2};}}
    QMenu::item:selected{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},40),stop:1 rgba({ar},{ag},{ab},10));border-left:2px solid rgba({ar},{ag},{ab},200);}}
    QStatusBar{{background:transparent;color:{txt3};font-size:11px;}}
    QDialog{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {bg},stop:0.5 rgba(8,22,44,255),stop:1 {bg});color:{txt};}}
    QLabel{{color:{txt2};background:transparent;}}
    QComboBox{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(12,28,55,220),stop:1 rgba(6,16,36,220));border:1px solid rgba({ar},{ag},{ab},120);border-top:2px solid rgba({ar},{ag},{ab},200);border-radius:1px;padding:6px 10px;color:{txt};min-width:140px;}}
    QComboBox::drop-down{{border:none;width:20px;}}
    QComboBox QAbstractItemView{{background:{bg};color:{txt};border:1px solid rgba({ar},{ag},{ab},80);selection-background-color:rgba({ar},{ag},{ab},30);outline:0;border-radius:0px;}}
    QCheckBox{{color:{txt2};font-size:13px;spacing:8px;}}
    QCheckBox::indicator{{width:18px;height:18px;border-radius:0px;border:1px solid rgba({ar},{ag},{ab},160);border-top:2px solid rgba({ar},{ag},{ab},220);background:rgba(6,18,40,200);}}
    QCheckBox::indicator:checked{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba({ar},{ag},{ab},240),stop:1 rgba({ar},{ag},{ab},160));border-color:rgba({ar},{ag},{ab},255);}}
    QSlider::groove:horizontal{{background:rgba(6,18,40,200);height:4px;border-radius:0px;border-top:1px solid rgba({ar},{ag},{ab},80);}}
    QSlider::handle:horizontal{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(255,255,255,200),stop:0.4 rgba({ar},{ag},{ab},240),stop:1 rgba({ar},{ag},{ab},160));width:14px;height:14px;border-radius:1px;margin:-5px 0;border:1px solid rgba(255,255,255,120);}}
    QSlider::sub-page:horizontal{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},140),stop:1 rgba({ar},{ag},{ab},240));border-radius:0px;}}
    QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(16,36,68,220),stop:0.5 rgba(8,22,46,200),stop:1 rgba(12,30,60,220));border:1px solid rgba({ar},{ag},{ab},100);border-top:1px solid rgba({ar},{ag},{ab},200);border-radius:2px;padding:8px 16px;color:{txt2};font-size:12px;}}
    QPushButton:hover{{border:1px solid rgba({ar},{ag},{ab},200);border-top:2px solid rgba(255,255,255,160);color:rgba({ar},{ag},{ab},255);background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(20,50,90,230),stop:1 rgba(10,28,60,220));}}
    QScrollArea{{background:transparent;border:none;}}
    QTableWidget{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(8,20,40,240),stop:0.5 rgba(5,14,30,250),stop:1 rgba(10,24,46,240));border:1px solid rgba({ar},{ag},{ab},80);border-top:2px solid rgba({ar},{ag},{ab},180);border-radius:2px;color:{txt2};font-size:12px;gridline-color:rgba({ar},{ag},{ab},15);outline:0;}}
    QTableWidget::item{{padding:5px 10px;border-bottom:1px solid rgba({ar},{ag},{ab},20);color:{txt2};}}
    QTableWidget::item:selected{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},50),stop:1 rgba({ar},{ag},{ab},15));color:{txt};border-left:2px solid rgba({ar},{ag},{ab},220);}}
    QHeaderView::section{{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 rgba({ar},{ag},{ab},30),stop:1 rgba({ar},{ag},{ab},8));border:none;border-bottom:2px solid rgba({ar},{ag},{ab},160);border-right:1px solid rgba({ar},{ag},{ab},30);padding:7px 10px;color:rgba({ar},{ag},{ab},240);font-size:11px;font-weight:bold;letter-spacing:1px;}}
    QSpinBox{{background:rgba(8,20,44,220);border:1px solid rgba({ar},{ag},{ab},120);border-top:2px solid rgba({ar},{ag},{ab},200);border-radius:1px;padding:5px 8px;color:{txt};}}
    """

    # ── Default (original) style ───────────────────────────────────
    return f"""
    QWidget{{background:{bg};color:{txt};font-family:'Segoe UI','.AppleSystemUIFont',Arial,sans-serif;}}
    QLineEdit{{background:{bg2};border:1px solid {brd};border-radius:8px;padding:9px 14px;font-size:13px;color:{txt};}}
    QLineEdit:focus{{border:1px solid rgba({ar},{ag},{ab},180);}}
    QProgressBar{{background:{bg3};border:1px solid {brd};border-radius:5px;text-align:center;color:{txt};font-size:11px;min-height:20px;}}
    QProgressBar::chunk{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},200),stop:1 rgba({ar},{ag},{ab},140));border-radius:4px;}}
    QScrollBar:vertical{{background:{bg2};width:6px;border-radius:3px;margin:0;}}
    QScrollBar::handle:vertical{{background:rgba({ar},{ag},{ab},80);border-radius:3px;min-height:20px;}}
    QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
    QMenu{{background:{bg};border:1px solid rgba({ar},{ag},{ab},60);border-radius:8px;color:{txt2};padding:4px;}}
    QMenu::item{{padding:8px 16px;border-radius:4px;color:{txt2};}}
    QMenu::item:selected{{background:rgba({ar},{ag},{ab},30);}}
    QStatusBar{{background:transparent;color:{txt3};font-size:11px;}}
    QDialog{{background:{bg};color:{txt};}}
    QLabel{{color:{txt2};background:transparent;}}
    QComboBox{{background:{bg2};border:1px solid {brd};border-radius:6px;padding:6px 10px;color:{txt};min-width:140px;}}
    QComboBox::drop-down{{border:none;width:20px;}}
    QComboBox QAbstractItemView{{background:{bg};color:{txt};border:1px solid {brd};selection-background-color:rgba({ar},{ag},{ab},30);outline:0;}}
    QCheckBox{{color:{txt2};font-size:13px;spacing:8px;}}
    QCheckBox::indicator{{width:18px;height:18px;border-radius:4px;border:1px solid {brd};background:{bg2};}}
    QCheckBox::indicator:checked{{background:rgba({ar},{ag},{ab},220);border-color:rgba({ar},{ag},{ab},220);}}
    QSlider::groove:horizontal{{background:{bg3};height:4px;border-radius:2px;}}
    QSlider::handle:horizontal{{background:rgba({ar},{ag},{ab},220);width:16px;height:16px;border-radius:8px;margin:-6px 0;}}
    QSlider::sub-page:horizontal{{background:rgba({ar},{ag},{ab},200);border-radius:2px;}}
    QPushButton{{background:{bg3};border:1px solid {brd};border-radius:8px;padding:8px 16px;color:{txt2};font-size:12px;}}
    QPushButton:hover{{border-color:rgba({ar},{ag},{ab},120);color:rgba({ar},{ag},{ab},220);}}
    QScrollArea{{background:transparent;border:none;}}
    QTableWidget{{background:{bg2};border:1px solid {brd};border-radius:8px;color:{txt2};font-size:12px;gridline-color:transparent;outline:0;}}
    QTableWidget::item{{padding:5px 10px;border-bottom:1px solid {brd};color:{txt2};}}
    QTableWidget::item:selected{{background:rgba({ar},{ag},{ab},35);color:{txt};}}
    QHeaderView::section{{background:rgba({ar},{ag},{ab},18);border:none;border-bottom:2px solid rgba({ar},{ag},{ab},50);padding:7px 10px;color:rgba({ar},{ag},{ab},220);font-size:11px;font-weight:bold;letter-spacing:1px;}}
    QHeaderView::section:horizontal{{border-right:1px solid rgba({ar},{ag},{ab},20);}}
    QSpinBox{{background:{bg2};border:1px solid {brd};border-radius:6px;padding:5px 8px;color:{txt};}}
    """

# ══════════════════════════════════════════════════════════════════
#  DATA MODEL
# ══════════════════════════════════════════════════════════════════
class DownloadItem:
    def __init__(self, url, filename, save_path,
                 size=0, downloaded=0, status="queued",
                 dl_type="direct", quality="best",
                 chunk_states=None, uid=None,
                 clip_start="", clip_end="", crop_preset="Original"):
        self.url        = url
        self.filename   = filename
        self.save_path  = save_path
        self.size       = size
        self.downloaded = downloaded
        self.status     = status        # queued/waiting/downloading/paused/done/error/seeding
        self.dl_type    = dl_type       # direct / yt / torrent
        self.quality    = quality
        self.clip_start = clip_start
        self.clip_end   = clip_end
        self.crop_preset = crop_preset
        # chunk_states: list of (start, end, downloaded_so_far) for resume
        self.chunk_states = chunk_states or []
        self.uid = uid or str(uuid.uuid4())

    def to_dict(self):
        return {
            "url": self.url, "filename": self.filename,
            "save_path": self.save_path, "size": self.size,
            "downloaded": self.downloaded, "status": self.status,
            "dl_type": self.dl_type, "quality": self.quality,
            "clip_start": self.clip_start, "clip_end": self.clip_end,
            "crop_preset": self.crop_preset,
            "chunk_states": self.chunk_states, "uid": self.uid,
        }

    @staticmethod
    def from_dict(d):
        return DownloadItem(
            url=d.get("url",""), filename=d.get("filename",""),
            save_path=d.get("save_path",""), size=d.get("size",0),
            downloaded=d.get("downloaded",0),
            status=d.get("status","paused"),
            dl_type=d.get("dl_type","direct"), quality=d.get("quality","best"),
            clip_start=d.get("clip_start",""), clip_end=d.get("clip_end",""),
            crop_preset=d.get("crop_preset","Original"),
            chunk_states=d.get("chunk_states",[]), uid=d.get("uid", str(uuid.uuid4())),
        )

# ══════════════════════════════════════════════════════════════════
#  WORKER 1 — DIRECT (multi-chunk, pause/resume, state-persisted)
# ══════════════════════════════════════════════════════════════════
class DirectWorker(QThread):
    progress  = pyqtSignal(int, int, int)   # row, downloaded, total
    finished  = pyqtSignal(int)
    error     = pyqtSignal(int, str)

    CHUNK_SZ  = 512 * 1024   # 512 KB per write flush

    def __init__(self, row, item, ts="auto"):
        super().__init__()
        self.row   = row
        self.item  = item
        self.ts    = ts
        self._pause_ev = threading.Event()
        self._pause_ev.set()          # set = running, cleared = paused
        self._stop = False

    def pause(self):  self._pause_ev.clear()
    def resume(self): self._pause_ev.set()
    def stop(self):   self._stop = True; self._pause_ev.set()

    def run(self):
        item = self.item
        try:
            r     = requests.head(item.url, verify=False, timeout=10, allow_redirects=True)
            total = int(r.headers.get("content-length", 0))
            if total <= 0:
                total = probe_url_size(item.url)
            item.size = total
            if total > 0:
                self.progress.emit(self.row, item.downloaded, total)
            supports  = r.headers.get("accept-ranges","") == "bytes"
            n = (detect_threads(item.url) if self.ts == "auto"
                 else int(self.ts) if str(self.ts).isdigit() else 4)

            if total > 5 * (1<<20) and supports:
                self._multi(total, n)
            else:
                self._single(total)
        except Exception as e:
            if not self._stop:
                item.status = "error"
                self.error.emit(self.row, str(e))

    # ── multi-chunk download (resume-aware) ──────────────────────
    def _multi(self, total, n):
        item = self.item
        chunk = total // n

        # Build or restore chunk states
        if item.chunk_states and len(item.chunk_states) == n:
            states = item.chunk_states          # [[start,end,done], ...]
        else:
            states = [[i*chunk, (i+1)*chunk-1 if i < n-1 else total-1, 0]
                      for i in range(n)]
            item.chunk_states = states

        # Pre-allocate file
        if item.downloaded == 0 or not os.path.exists(item.save_path):
            with open(item.save_path, "wb") as f:
                f.seek(total - 1); f.write(b"\x00")

        lock   = threading.Lock()
        dl_ctr = [sum(s[2] for s in states)]

        def fetch(idx):
            s, e, done = states[idx]
            pos  = s + done
            if pos > e: return
            try:
                resp = requests.get(item.url,
                    headers={"Range": f"bytes={pos}-{e}"},
                    stream=True, verify=False, timeout=60)
                with open(item.save_path, "r+b") as f:
                    f.seek(pos)
                    for chunk_data in resp.iter_content(65536):
                        if self._stop: return
                        self._pause_ev.wait()
                        if self._stop: return
                        if chunk_data:
                            f.write(chunk_data)
                            with lock:
                                states[idx][2] += len(chunk_data)
                                dl_ctr[0]      += len(chunk_data)
                                item.downloaded = dl_ctr[0]
                                item.chunk_states = states
                                self.progress.emit(self.row, dl_ctr[0], total)
            except Exception as e:
                if not self._stop:
                    self.error.emit(self.row, str(e))

        threads = [threading.Thread(target=fetch, args=(i,), daemon=True)
                   for i in range(n)]
        for t in threads: t.start()
        for t in threads: t.join()

        if not self._stop:
            item.status = "done"
            item.chunk_states = []
            self.finished.emit(self.row)

    # ── single-stream (resume via Range header) ──────────────────
    def _single(self, total):
        item    = self.item
        headers = {}
        mode    = "wb"
        if item.downloaded > 0 and os.path.exists(item.save_path):
            headers["Range"] = f"bytes={item.downloaded}-"
            mode = "ab"
        r = requests.get(item.url, headers=headers,
                         stream=True, verify=False, timeout=30)
        if total <= 0:
            try:
                total = int(r.headers.get("content-length", 0)) + item.downloaded
            except Exception:
                total = 0
            if total > 0:
                item.size = total
                self.progress.emit(self.row, item.downloaded, total)
        with open(item.save_path, mode) as f:
            for chunk_data in r.iter_content(65536):
                if self._stop: return
                self._pause_ev.wait()
                if self._stop: return
                if chunk_data:
                    f.write(chunk_data)
                    item.downloaded += len(chunk_data)
                    self.progress.emit(self.row, item.downloaded,
                                       total or item.downloaded)
        if not self._stop:
            item.status = "done"
            self.finished.emit(self.row)

# ══════════════════════════════════════════════════════════════════
#  WORKER 2 — YT-DLP (video / audio)
# ══════════════════════════════════════════════════════════════════
class YTWorker(QThread):
    progress    = pyqtSignal(int, int, int)
    finished    = pyqtSignal(int)
    error       = pyqtSignal(int, str)
    name_update = pyqtSignal(int, str)
    formats_ready = pyqtSignal(int, list)   # row, formats list

    def __init__(self, row, item, folder, fetch_only=False):
        super().__init__()
        self.row        = row
        self.item       = item
        self.folder     = folder
        self.fetch_only = fetch_only
        self._stop      = False

    def stop(self): self._stop = True

    def run(self):
        item, row = self.item, self.row

        if self.fetch_only:
            self._fetch_formats()
            return

        def hook(d):
            if self._stop: raise Exception("Stopped")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                dl    = d.get("downloaded_bytes", 0)
                item.downloaded = dl; item.size = total
                self.progress.emit(row, dl, total)
            elif d["status"] == "finished":
                item.downloaded = item.size or item.downloaded
                if not _will_postprocess_video(item):
                    item.status = "done"
                    self.finished.emit(row)

        fmt = item.quality
        token = item.uid[:8]
        outtmpl = f"%(title)s [{token}].%(ext)s" if _will_postprocess_video(item) else "%(title)s.%(ext)s"
        base_opts = {
            "outtmpl": os.path.join(self.folder, outtmpl),
            "progress_hooks": [hook],
            "quiet": True, "no_warnings": True,
            "continuedl": True,
        }

        if fmt == "mp3":
            opts = {**base_opts,
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                    }] if shutil.which("ffmpeg") else []}
        elif "+" in fmt and shutil.which("ffmpeg"):
            opts = {**base_opts,
                    "format": fmt,
                    "merge_output_format": "mp4"}
        else:
            clean = fmt.split("+")[0] if "+" in fmt else fmt
            opts  = {**base_opts, "format": clean}

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(item.url, download=False)
                title = info.get("title", "video")
                item.filename = title
                self.name_update.emit(row, title)

                # ── Update table Size column from metadata ──
                est = self._extract_size(info)
                if est and est > 0:
                    item.size = est
                    self.progress.emit(row, item.downloaded, est)

                if not self._stop:
                    ydl.download([item.url])
                if not self._stop and _will_postprocess_video(item):
                    self._apply_video_edit(token)
                    item.status = "done"
                    self.finished.emit(row)
        except Exception as e:
            if "Stopped" not in str(e):
                item.status = "error"
                self.error.emit(row, str(e))

    def _extract_size(self, info):
        """Find the most accurate filesize from yt-dlp metadata.

        Handles direct filesize keys, filesize_approx, combined
        format strings (bestvideo+bestaudio via requested_formats),
        the selected format id, and a bitrate*duration fallback.
        """
        if not info:
            return 0
        for k in ("filesize", "filesize_approx"):
            v = info.get(k)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        req = info.get("requested_formats")
        if req:
            total, ok = 0, False
            for f in req:
                for k in ("filesize", "filesize_approx"):
                    v = f.get(k)
                    if v:
                        try:
                            total += int(v); ok = True; break
                        except (TypeError, ValueError):
                            pass
            if ok and total > 0:
                return total
        fmt_id = info.get("format_id", "")
        if fmt_id:
            for f in info.get("formats", []):
                if f.get("format_id") == fmt_id:
                    for k in ("filesize", "filesize_approx"):
                        v = f.get(k)
                        if v:
                            try:
                                return int(v)
                            except (TypeError, ValueError):
                                pass
        dur = info.get("duration")
        tbr = info.get("tbr")
        if dur and tbr:
            try:
                return int(float(tbr) * 1000 / 8 * float(dur))
            except (TypeError, ValueError):
                pass
        return 0

    def _apply_video_edit(self, token):
        if not shutil.which("ffmpeg"):
            raise Exception("ffmpeg not found. Install ffmpeg to use clip/crop.")
        item = self.item
        candidates = sorted(
            [p for p in Path(self.folder).iterdir() if p.is_file() and f"[{token}]" in p.name],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return
        src = candidates[0]
        stem = src.stem.replace(f" [{token}]", "")
        out = src.with_name(f"{stem} - edited{src.suffix}")
        args = ["ffmpeg", "-y"]
        start = _time_to_seconds(item.clip_start)
        end = _time_to_seconds(item.clip_end)
        if start is not None:
            args += ["-ss", str(start)]
        args += ["-i", str(src)]
        if end is not None:
            duration = end - (start or 0)
            if duration <= 0:
                raise Exception("Clip end time must be after start time.")
            args += ["-t", str(duration)]
        vf = _crop_filter(item.crop_preset)
        if vf:
            args += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy"]
        else:
            args += ["-c", "copy"]
        args.append(str(out))
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            src.unlink()
        except Exception:
            pass
        item.filename = out.stem
        self.name_update.emit(self.row, out.stem)

    def _fetch_formats(self):
        """Fetch available formats with estimated file sizes and emit them."""
        try:
            with yt_dlp.YoutubeDL({"quiet":True,"no_warnings":True}) as ydl:
                info = ydl.extract_info(self.item.url, download=False)
            all_formats = info.get("formats", [])
            size_cache = {}

            def _probe_stream_size(f):
                url = f.get("url")
                if not url:
                    return 0
                proto = (f.get("protocol") or "").lower()
                if "m3u8" in proto or "manifest" in proto:
                    return 0
                key = f.get("format_id") or url
                if key in size_cache:
                    return size_cache[key]
                headers = dict(f.get("http_headers") or {})
                headers.setdefault("User-Agent", "Mozilla/5.0")
                total = 0
                try:
                    r = requests.head(url, headers=headers, timeout=8,
                                      allow_redirects=True, verify=False)
                    if r.headers.get("content-length"):
                        total = int(r.headers.get("content-length", 0))
                except Exception:
                    pass
                if total <= 0:
                    try:
                        h = dict(headers)
                        h["Range"] = "bytes=0-0"
                        r = requests.get(url, headers=h, stream=True, timeout=8,
                                         allow_redirects=True, verify=False)
                        cr = r.headers.get("content-range", "")
                        if "/" in cr:
                            total = int(cr.rsplit("/", 1)[-1])
                        elif r.headers.get("content-length") and r.status_code != 200:
                            total = int(r.headers.get("content-length", 0))
                        r.close()
                    except Exception:
                        pass
                size_cache[key] = total
                return total

            def _sz(f):
                s = f.get("filesize") or f.get("filesize_approx") or 0
                try:
                    return int(s)
                except (TypeError, ValueError):
                    return 0

            def _sz_info(f):
                if f.get("filesize"):
                    return _sz(f), True
                probed = _probe_stream_size(f)
                if probed > 0:
                    return probed, True
                if f.get("filesize_approx"):
                    return _sz(f), False
                return 0, False

            def _site_size_text(total, exact):
                if total <= 0:
                    return "  | site size: Unknown"
                prefix = "" if exact else "~"
                return f"  | site size: {prefix}{fmt_bytes(total)}"

            # Best audio stream size (video-only must merge with audio)
            audio_sz = 0
            audio_exact = False
            for f in all_formats:
                if f.get("vcodec","none") == "none" and f.get("acodec","none") != "none":
                    s, exact = _sz_info(f)
                    if s > audio_sz:
                        audio_sz, audio_exact = s, exact

            def _size_tag(h):
                """Return website-provided size text for a resolution bucket."""
                best = 0
                exact_best = False
                vonly = True
                for g in all_formats:
                    if g.get("vcodec","none") == "none" or g.get("height") != h:
                        continue
                    s, exact = _sz_info(g)
                    if s > best:
                        best, exact_best = s, exact
                    if g.get("acodec","none") != "none":
                        vonly = False
                if best <= 0:
                    return _site_size_text(0, False)
                total = best + (audio_sz if vonly and audio_sz else 0)
                exact = exact_best and (not vonly or not audio_sz or audio_exact)
                return _site_size_text(total, exact)

            fmts = []
            seen = set()
            for f in all_formats:
                h = f.get("height")
                ext = f.get("ext","")
                vco = f.get("vcodec","none")
                if vco != "none" and h and h not in seen:
                    label = f"{h}p {ext.upper()}{_size_tag(h)}"
                    fmts.append((label, f"bestvideo[height<={h}]+bestaudio/best"))
                    seen.add(h)
            fmts.sort(key=lambda x:-int(x[0].split("p")[0]) if x[0][0].isdigit() else 0)

            # Best Quality size = highest-res bucket; MP3 ≈ best audio
            best_tag = ""
            if fmts:
                top_h = max(
                    (g.get("height") for g in all_formats
                     if g.get("vcodec","none") != "none" and g.get("height")),
                    default=0)
                if top_h:
                    best_tag = _size_tag(top_h)
            mp3_tag = _site_size_text(audio_sz, audio_exact)

            fmts = ([(f"Best Quality{best_tag}", "best")] + fmts
                    + [(f"MP3 Audio{mp3_tag}", "mp3")])
            self.formats_ready.emit(self.row, fmts)
        except Exception as e:
            self.formats_ready.emit(self.row, [
                ("Best Quality  | site size: Unknown","best"),
                ("MP3 Audio  | site size: Unknown","mp3"),
            ])

# ══════════════════════════════════════════════════════════════════
#  WORKER 3 — LIBTORRENT
# ══════════════════════════════════════════════════════════════════
class TorrentWorker(QThread):
    progress     = pyqtSignal(int, int, int)
    finished     = pyqtSignal(int)
    error        = pyqtSignal(int, str)
    name_update  = pyqtSignal(int, str)
    status_text  = pyqtSignal(int, str)      # row, status string
    torrent_stat = pyqtSignal(int, dict)     # row, {seeds,peers,ul_rate,dl_rate,ratio,state_str}

    def __init__(self, row, item, save_dir, selected_files=None):
        super().__init__()
        self.row            = row
        self.item           = item
        self.save_dir       = save_dir
        self.selected_files = selected_files   # None = all
        self._stop          = False
        self._pause_ev      = threading.Event()
        self._pause_ev.set()

    def pause(self):  self._pause_ev.clear()
    def resume(self): self._pause_ev.set()
    def stop(self):   self._stop = True; self._pause_ev.set()

    def run(self):
        if not HAS_LT:
            self.item.status = "error"
            self.error.emit(self.row,
                "libtorrent not installed.\nRun: pip install libtorrent")
            return

        item = self.item
        self._handle = None
        try:
            # ── Session setup — works on both lt 1.x and lt 2.x ──
            ses = lt.session()

            # Settings pack (lt 2.x preferred, lt 1.x may not have all keys)
            try:
                sp = lt.settings_pack()
                sp[lt.settings_pack.listen_interfaces]       = "0.0.0.0:6881,[::]:6881"
                sp[lt.settings_pack.alert_mask]              = int(
                    lt.alert.category_t.error_notification   |
                    lt.alert.category_t.peer_notification    |
                    lt.alert.category_t.status_notification
                )
                sp[lt.settings_pack.enable_dht]              = True
                sp[lt.settings_pack.enable_lsd]              = True
                sp[lt.settings_pack.enable_upnp]             = True
                sp[lt.settings_pack.enable_natpmp]           = True
                sp[lt.settings_pack.connections_limit]       = 200
                sp[lt.settings_pack.download_rate_limit]     = 0   # unlimited
                sp[lt.settings_pack.upload_rate_limit]       = 0
                ses.apply_settings(sp)
            except Exception:
                # lt 1.x fallback
                try:
                    ses.listen_on(6881, 6891)
                    ses.start_dht()
                    ses.start_lsd()
                    ses.start_upnp()
                    ses.start_natpmp()
                except Exception:
                    pass

            url = item.url

            if url.startswith("magnet:"):
                params = lt.parse_magnet_uri(url)
                params.save_path = self.save_dir
                h = ses.add_torrent(params)
                self._handle = h
                self.status_text.emit(self.row, "🔌 Connecting to peers…")

                # Wait up to 120 s for metadata (emit dot animation)
                for tick in range(240):
                    if self._stop: return
                    ses.post_torrent_updates()
                    if h.has_metadata(): break
                    dots = "." * ((tick % 3) + 1)
                    self.status_text.emit(self.row, f"🔌 Fetching metadata{dots}  peers:{h.status().num_peers}")
                    self.msleep(500)
                else:
                    raise Exception("Metadata timeout — no peers responded in 2 minutes.\nCheck your internet connection or try a different magnet link.")

                # Apply file priorities after metadata arrives
                if self.selected_files is not None:
                    n = 0
                    try:
                        ti = h.torrent_file()   # lt 2.x
                        if ti: n = ti.files().num_files()
                    except Exception: pass
                    if not n:
                        try:
                            ti = h.get_torrent_info()   # lt 1.x
                            n  = ti.num_files()
                        except Exception: pass
                    if n:
                        _set_file_priorities(h, n, self.selected_files)

            else:
                # .torrent file path
                try:
                    info = lt.torrent_info(url)
                except Exception as e:
                    raise Exception(f"Cannot read .torrent file:\n{e}")
                params = lt.add_torrent_params()
                params.ti        = info
                params.save_path = self.save_dir
                if self.selected_files is not None:
                    n = info.num_files()
                    params.file_priorities = _build_prio_list(n, self.selected_files)
                h = ses.add_torrent(params)
                self._handle = h

            # Emit torrent name to UI
            try:
                self.name_update.emit(self.row, h.name())
            except Exception:
                pass

            STATE_NAMES = ["queued", "checking", "dl meta", "downloading",
                           "finished", "seeding", "allocating", "checking resume"]

            # ── Main download loop ────────────────────────────────
            while not self._stop:
                self._pause_ev.wait()
                if self._stop: break

                # Sync pause/resume with libtorrent handle
                s_now = h.status()
                if not self._pause_ev.is_set() and not s_now.paused:
                    h.pause()
                elif self._pause_ev.is_set() and s_now.paused:
                    h.resume()

                ses.post_torrent_updates()
                s        = h.status()
                total    = s.total_wanted    if s.total_wanted    > 0 else 1
                done     = s.total_wanted_done
                state    = int(s.state)
                peers    = s.num_peers
                seeds    = s.num_seeds
                dl_rate  = s.download_rate
                ul_rate  = s.upload_rate
                # Ratio: guard against division by zero
                atd = getattr(s, "all_time_download", 0)
                atu = getattr(s, "all_time_upload", 0)
                ratio = round(atu / atd, 3) if atd > 0 else 0.0
                state_str = STATE_NAMES[state] if state < len(STATE_NAMES) else str(state)

                self.torrent_stat.emit(self.row, {
                    "seeds": seeds, "peers": peers,
                    "dl_rate": dl_rate, "ul_rate": ul_rate,
                    "ratio": ratio, "state_str": state_str,
                })

                if state == 5:   # seeding
                    extra = (f"🌱 Seeding  ⬆ {fmt_bytes(ul_rate)}/s  "
                             f"ratio:{ratio:.3f}  S:{seeds}  P:{peers}")
                    item.status = "seeding"
                elif state == 1:  # checking files
                    pct = int(s.progress * 100)
                    extra = f"🔍 Checking… {pct}%"
                elif state == 2:  # downloading metadata
                    extra = f"🔌 Getting metadata…  P:{peers}"
                else:
                    nc  = getattr(s, "num_complete",   seeds)
                    ni  = getattr(s, "num_incomplete", peers)
                    extra = (f"⬇ {fmt_bytes(dl_rate)}/s  ⬆ {fmt_bytes(ul_rate)}/s  "
                             f"S:{seeds}({nc})  P:{peers}({ni})  {state_str}")

                self.status_text.emit(self.row, extra)
                item.downloaded = done
                item.size       = total
                self.progress.emit(self.row, done, total)

                # State 4 = "finished" (pieces all downloaded, may or may not seed)
                if state == 4:
                    item.status = "done"
                    self.finished.emit(self.row)
                    return
                # State 5 = seeding — keep loop alive so stats keep updating
                self.msleep(1000)

        except Exception as e:
            if not self._stop:
                item.status = "error"
                self.error.emit(self.row, str(e))

# ══════════════════════════════════════════════════════════════════
#  MAGNET METADATA FETCHER  (non-blocking background worker)
# ══════════════════════════════════════════════════════════════════
class MetadataFetchWorker(QThread):
    """Fetches torrent metadata from a magnet URI in the background.
    Emits metadata_ready(files_list) when done, or fetch_error(msg) on failure.
    files_list = [ (filename_str, size_int), ... ]
    Also stores self.torrent_info for use by TorrentWorker later."""

    metadata_ready = pyqtSignal(list)   # [(name, size), ...]
    fetch_error    = pyqtSignal(str)

    def __init__(self, magnet_url, save_dir):
        super().__init__()
        self.magnet_url  = magnet_url
        self.save_dir    = save_dir
        self._stop       = False
        self.torrent_info = None   # populated after success
        self._session    = None

    def stop(self):
        self._stop = True
        if self._session:
            try: self._session.abort()
            except Exception: pass

    def run(self):
        if not HAS_LT:
            self.fetch_error.emit("libtorrent not installed.\nRun: pip install libtorrent")
            return
        try:
            ses = lt.session()
            self._session = ses

            # Apply settings — lt 2.x first, lt 1.x fallback
            try:
                sp = lt.settings_pack()
                sp[lt.settings_pack.listen_interfaces]   = "0.0.0.0:6881,[::]:6881"
                sp[lt.settings_pack.enable_dht]          = True
                sp[lt.settings_pack.enable_lsd]          = True
                sp[lt.settings_pack.enable_upnp]         = True
                sp[lt.settings_pack.enable_natpmp]       = True
                sp[lt.settings_pack.connections_limit]   = 200
                ses.apply_settings(sp)
            except Exception:
                try:
                    ses.listen_on(6881, 6891)
                    ses.start_dht(); ses.start_lsd()
                    ses.start_upnp(); ses.start_natpmp()
                except Exception:
                    pass

            params = lt.parse_magnet_uri(self.magnet_url)
            params.save_path = self.save_dir
            h = ses.add_torrent(params)

            # Wait up to 180 s for metadata (0.5s steps for responsive animation)
            for tick in range(360):
                if self._stop: return
                ses.post_torrent_updates()
                if h.has_metadata(): break
                self.msleep(500)
            else:
                self.fetch_error.emit(
                    "Metadata timeout — no peers responded in 3 minutes.\n"
                    "• Check your internet connection\n"
                    "• Try a different magnet link / tracker\n"
                    "• Make sure libtorrent can reach the internet (firewall?)")
                return

            if self._stop: return

            # Extract file list — handle both lt 1.x and 2.x APIs
            files = []
            try:
                # lt 2.x preferred path
                ti = h.torrent_file()
                if ti:
                    self.torrent_info = ti
                    fs = ti.files()
                    for i in range(fs.num_files()):
                        files.append((fs.file_path(i), fs.file_size(i)))
            except Exception:
                pass

            if not files:
                # lt 1.x fallback
                try:
                    ti = h.get_torrent_info()
                    self.torrent_info = ti
                    for i in range(ti.num_files()):
                        f = ti.file_at(i)
                        files.append((f.path, f.size))
                except Exception:
                    pass

            if not files:
                # Emit with empty list — TorrentFileDlg shows "No file info" message
                # but download will still proceed (all files)
                self.metadata_ready.emit([])
                return

            self.metadata_ready.emit(files)

        except Exception as e:
            if not self._stop:
                self.fetch_error.emit(str(e))


# ══════════════════════════════════════════════════════════════════
#  MAGNET LOADING DIALOG  (spinner while fetching metadata)
# ══════════════════════════════════════════════════════════════════
class MagnetLoadingDlg(QDialog):
    """Non-blocking modal spinner shown while metadata is being fetched.
    Emits files_ready(files, worker) when metadata arrives,
    or closes itself on cancel."""

    files_ready = pyqtSignal(list, object)   # files, worker (keeps session alive)

    def __init__(self, magnet_url, save_dir, lang, theme, mode, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setWindowTitle("RapidGet")
        self.setFixedSize(400, 180)
        self.setModal(True)
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = build_style(theme, mode)
        self.setStyleSheet(global_style(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win))

        vl = QVBoxLayout(self); vl.setContentsMargins(28,24,28,20); vl.setSpacing(12)

        title = QLabel("🧲  Fetching Torrent Metadata…")
        title.setStyleSheet(f"color:rgba({ar},{ag},{ab},220);font-size:14px;font-weight:bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(title)

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)   # indeterminate
        vl.addWidget(self._bar)

        self._info = QLabel("Starting DHT / connecting to peers…")
        self._info.setStyleSheet(f"color:{txt3};font-size:11px;")
        self._info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(self._info)

        self._elapsed_lbl = QLabel("0s elapsed")
        self._elapsed_lbl.setStyleSheet(f"color:{txt3};font-size:10px;")
        self._elapsed_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(self._elapsed_lbl)

        cancel = QPushButton(lang.get("cancel","Cancel"))
        cancel.setStyleSheet(f"QPushButton{{background:{bg3};border:1px solid {brd};border-radius:7px;padding:7px 20px;color:{txt2};}}QPushButton:hover{{border-color:rgba({ar},{ag},{ab},100);}}")
        cancel.clicked.connect(self._cancel)
        hl = QHBoxLayout(); hl.addStretch(); hl.addWidget(cancel); hl.addStretch()
        vl.addLayout(hl)

        # Timers
        self._dot      = 0
        self._elapsed  = 0
        self._timer    = QTimer(self); self._timer.timeout.connect(self._animate); self._timer.start(500)
        self._etimer   = QTimer(self); self._etimer.timeout.connect(self._tick_elapsed); self._etimer.start(1000)

        # Start background worker
        self._worker = MetadataFetchWorker(magnet_url, save_dir)
        self._worker.metadata_ready.connect(self._on_ready)
        self._worker.fetch_error.connect(self._on_error)
        self._worker.start()

    def _animate(self):
        self._dot = (self._dot + 1) % 4
        # Try to get live peer count from worker's session handle
        peer_hint = ""
        try:
            w = self._worker
            if hasattr(w, "_session") and w._session:
                handles = w._session.get_torrents()
                if handles:
                    p = handles[0].status().num_peers
                    peer_hint = f"  •  peers: {p}"
        except Exception:
            pass
        self._info.setText("Connecting to peers" + "." * self._dot + peer_hint)

    def _tick_elapsed(self):
        self._elapsed += 1
        self._elapsed_lbl.setText(f"{self._elapsed}s elapsed  (timeout: 180s)")

    def _on_ready(self, files):
        self._timer.stop(); self._etimer.stop()
        self._files      = files
        self._worker_ref = self._worker
        self.accept()
        QTimer.singleShot(0, self._emit_ready)

    def _emit_ready(self):
        self.files_ready.emit(self._files, self._worker_ref)

    def _on_error(self, msg):
        self._timer.stop(); self._etimer.stop()
        self._bar.setRange(0, 1); self._bar.setValue(0)
        self._info.setText(f"❌ Error")
        self._elapsed_lbl.setText(msg)
        self._elapsed_lbl.setWordWrap(True)

    def _cancel(self):
        self._worker.stop()
        self.reject()


# ══════════════════════════════════════════════════════════════════
#  TORRENT FILE SELECTOR DIALOG
# ══════════════════════════════════════════════════════════════════
class TorrentFileDlg(QDialog):
    """
    File selector with nested folder tree (qBittorrent-style).
    Columns: Name | Total Size | Priority
    Pass files=[(full_path, size), ...] for magnet (post-fetch),
    or torrent_path=str for .torrent files (parsed inline).
    self.selected → set of int indices of checked leaf files.
    """
    COL_NAME = 0
    COL_SIZE = 1
    COL_PRIO = 2

    def __init__(self, lang, theme, mode, parent=None,
                 torrent_path=None, files=None, torrent_info=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.lang = lang
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = build_style(theme, mode)
        self.setStyleSheet(global_style(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win))
        self.setWindowTitle("RapidGet — " + lang["torrent_files"])
        self.setMinimumSize(620, 480)
        self.resize(720, 540)
        self.selected = None   # set of indices after accept

        # ── Style vars stored for reuse ───────────────────────────
        self._ar, self._ag, self._ab = ar, ag, ab
        self._bg, self._bg2, self._bg3 = bg, bg2, bg3
        self._txt, self._txt2, self._txt3, self._brd = txt, txt2, txt3, brd
        self._accent = f"rgba({ar},{ag},{ab},220)"

        vl = QVBoxLayout(self); vl.setContentsMargins(18,18,18,14); vl.setSpacing(10)

        # Header
        hdr = QLabel("🧲  " + lang["torrent_files"])
        hdr.setStyleSheet(f"color:{self._accent};font-size:15px;font-weight:bold;")
        vl.addWidget(hdr)

        # Tree
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Total Size", "Priority"])
        hh = self.tree.header()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setStyleSheet(f"""
            QTreeWidget {{
                background:{bg2}; border:1px solid {brd};
                border-radius:8px; color:{txt2};
                alternate-background-color:{bg3};
            }}
            QTreeWidget::item {{ padding:4px 6px; }}
            QTreeWidget::item:selected {{ background:rgba({ar},{ag},{ab},30); }}
            QTreeWidget::branch:has-children:!has-siblings:closed,
            QTreeWidget::branch:closed:has-children:has-siblings {{
                border-image:none; image:none;
            }}
        """)
        self.tree.setAlternatingRowColors(True)
        self.tree.setAnimated(True)
        self.tree.itemChanged.connect(self._on_item_changed)
        vl.addWidget(self.tree)

        # ── Build raw file list ───────────────────────────────────
        # Priority: files > torrent_info > torrent_path
        raw_files   = []
        parse_error = ""

        if files is not None:
            # Magnet: file list already built by MetadataFetchWorker
            raw_files = list(files)

        elif torrent_info is not None:
            # Pre-parsed torrent_info object — fastest, most reliable
            try:
                fs = torrent_info.files()
                for i in range(fs.num_files()):
                    raw_files.append((fs.file_path(i), fs.file_size(i)))
            except Exception:
                try:
                    # lt 1.x: num_files / file_at API
                    for i in range(torrent_info.num_files()):
                        f2 = torrent_info.file_at(i)
                        raw_files.append((f2.path, f2.size))
                except Exception as e2:
                    parse_error = str(e2)

        elif torrent_path:
            if HAS_LT:
                try:
                    ti = lt.torrent_info(str(torrent_path))
                    try:
                        fs = ti.files()
                        for i in range(fs.num_files()):
                            raw_files.append((fs.file_path(i), fs.file_size(i)))
                    except Exception:
                        for i in range(ti.num_files()):
                            f2 = ti.file_at(i)
                            raw_files.append((f2.path, f2.size))
                except Exception as e:
                    parse_error = str(e)
            if not raw_files:
                try:
                    _, raw_files = parse_torrent_metadata(torrent_path)
                    parse_error = ""
                except Exception as e:
                    parse_error = str(e)

        if not raw_files and torrent_path:
            try:
                _, raw_files = parse_torrent_metadata(torrent_path)
                parse_error = ""
            except Exception:
                pass

        if parse_error:
            err_lbl = QLabel(f"⚠  {parse_error}\n(All files will be downloaded)")
            err_lbl.setStyleSheet("color:#e17055;font-size:11px;padding:4px 0;")
            err_lbl.setWordWrap(True)
            vl.addWidget(err_lbl)

        # ── Build nested tree ─────────────────────────────────────
        self._leaf_items = []   # list of QTreeWidgetItem (leaf files only)
        self._leaf_indices = [] # parallel index list (original file index)
        self._building = True   # suppress itemChanged during build
        self._build_tree(raw_files, a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd)
        self._building = False
        self.tree.expandAll()

        # ── Bottom buttons ────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        sel_all  = QPushButton(lang["select_all"])
        sel_none = QPushButton(lang["select_none"])
        _bs = (f"QPushButton{{background:{bg3};border:1px solid {brd};"
               f"border-radius:6px;padding:6px 16px;color:{txt2};font-size:12px;}}"
               f"QPushButton:hover{{border-color:rgba({ar},{ag},{ab},120);"
               f"color:rgba({ar},{ag},{ab},220);}}")
        sel_all.setStyleSheet(_bs); sel_none.setStyleSheet(_bs)
        sel_all.clicked.connect(self._select_all)
        sel_none.clicked.connect(self._select_none)
        btn_row.addWidget(sel_all); btn_row.addWidget(sel_none); btn_row.addStretch()

        # File count label
        self._count_lbl = QLabel()
        self._count_lbl.setStyleSheet(f"color:{txt3};font-size:11px;")
        btn_row.addWidget(self._count_lbl)
        vl.addLayout(btn_row)

        # Footer
        footer = QHBoxLayout()
        ok = QPushButton(lang["download"]); no = QPushButton(lang["cancel"])
        accent_fg = readable_on_rgb(ar, ag, ab)
        ok.setStyleSheet(
            f"QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 rgba({ar},{ag},{ab},220),stop:1 rgba({ar},{ag},{ab},160));"
            f"color:{accent_fg};border:none;border-radius:8px;padding:10px 24px;font-weight:bold;}}"
            f"QPushButton:hover{{background:rgba({ar},{ag},{ab},255);}}")
        no.setStyleSheet(f"QPushButton{{background:{bg2};border:1px solid {brd};"
                         f"border-radius:8px;padding:10px 16px;color:{txt3};}}")
        ok.clicked.connect(self._accept); no.clicked.connect(self.reject)
        footer.addStretch(); footer.addWidget(ok); footer.addWidget(no)
        vl.addLayout(footer)
        self._update_count()

    # ── Build nested folder tree ──────────────────────────────────
    def _build_tree(self, raw_files, *style):
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,*_ = style
        folder_nodes = {}   # path_str → QTreeWidgetItem

        def get_or_create_folder(parts):
            """Recursively get/create folder nodes, return leaf parent."""
            key = "/".join(parts)
            if key in folder_nodes:
                return folder_nodes[key]
            if len(parts) == 1:
                node = QTreeWidgetItem(self.tree)
            else:
                parent = get_or_create_folder(parts[:-1])
                node   = QTreeWidgetItem(parent)
            node.setText(self.COL_NAME, parts[-1])
            node.setText(self.COL_SIZE, "")
            node.setText(self.COL_PRIO, "")
            node.setCheckState(self.COL_NAME, Qt.CheckState.Checked)
            node.setForeground(self.COL_NAME, QColor(f"rgba({ar},{ag},{ab},200)"))
            node.setExpanded(True)
            # Bold folder names
            f = node.font(self.COL_NAME); f.setBold(True); node.setFont(self.COL_NAME, f)
            folder_nodes[key] = node
            return node

        # Sort by path for clean grouping
        indexed = sorted(enumerate(raw_files), key=lambda x: x[1][0].replace("\\","/"))

        folder_sizes = {}   # folder key → total bytes

        for orig_idx, (fpath, fsize) in indexed:
            # Normalise separators
            fpath_norm = fpath.replace("\\", "/").strip("/")
            parts      = fpath_norm.split("/")

            if len(parts) == 1:
                # Top-level file — add directly
                leaf = QTreeWidgetItem(self.tree)
            else:
                parent = get_or_create_folder(parts[:-1])
                leaf   = QTreeWidgetItem(parent)

            fname = parts[-1]
            leaf.setText(self.COL_NAME, fname)
            leaf.setText(self.COL_SIZE, fmt_bytes(fsize))
            leaf.setText(self.COL_PRIO, "Normal")
            leaf.setCheckState(self.COL_NAME, Qt.CheckState.Checked)
            leaf.setData(self.COL_NAME, Qt.ItemDataRole.UserRole, orig_idx)
            leaf.setForeground(self.COL_SIZE, QColor(txt3))
            leaf.setForeground(self.COL_PRIO, QColor(f"rgba({ar},{ag},{ab},160)"))

            self._leaf_items.append(leaf)
            self._leaf_indices.append(orig_idx)

            # Accumulate folder sizes
            for depth in range(1, len(parts)):
                fkey = "/".join(parts[:depth])
                folder_sizes[fkey] = folder_sizes.get(fkey, 0) + fsize

        # Fill folder size column
        for key, node in folder_nodes.items():
            if key in folder_sizes:
                node.setText(self.COL_SIZE, fmt_bytes(folder_sizes[key]))
                node.setForeground(self.COL_SIZE, QColor(txt3))

        if not raw_files:
            it = QTreeWidgetItem(self.tree)
            it.setText(0, "(No file info — all files will download)")

    # ── Checkbox cascade logic ────────────────────────────────────
    def _on_item_changed(self, item, col):
        if col != self.COL_NAME or self._building: return
        self._building = True
        state = item.checkState(self.COL_NAME)
        # Propagate DOWN to children
        self._set_children(item, state)
        # Propagate UP to parents
        parent = item.parent()
        while parent:
            self._update_parent_state(parent)
            parent = parent.parent()
        self._building = False
        self._update_count()

    def _set_children(self, item, state):
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(self.COL_NAME, state)
            child.setText(self.COL_PRIO,
                          "Normal" if state == Qt.CheckState.Checked else "Not downloaded")
            self._set_children(child, state)

    def _update_parent_state(self, parent):
        checked = sum(
            1 for i in range(parent.childCount())
            if parent.child(i).checkState(self.COL_NAME) == Qt.CheckState.Checked
        )
        total = parent.childCount()
        if checked == 0:
            parent.setCheckState(self.COL_NAME, Qt.CheckState.Unchecked)
        elif checked == total:
            parent.setCheckState(self.COL_NAME, Qt.CheckState.Checked)
        else:
            parent.setCheckState(self.COL_NAME, Qt.CheckState.PartiallyChecked)

    def _select_all(self):
        self._building = True
        for it in self._leaf_items:
            it.setCheckState(self.COL_NAME, Qt.CheckState.Checked)
            it.setText(self.COL_PRIO, "Normal")
        # Update all folder nodes
        self._building = False
        self._refresh_all_parents()
        self._update_count()

    def _select_none(self):
        self._building = True
        for it in self._leaf_items:
            it.setCheckState(self.COL_NAME, Qt.CheckState.Unchecked)
            it.setText(self.COL_PRIO, "Not downloaded")
        self._building = False
        self._refresh_all_parents()
        self._update_count()

    def _refresh_all_parents(self):
        """Re-compute all folder check states from leaves up."""
        visited = set()
        for it in self._leaf_items:
            p = it.parent()
            while p and id(p) not in visited:
                visited.add(id(p))
                self._update_parent_state(p)
                p = p.parent()

    def _update_count(self):
        checked = sum(
            1 for it in self._leaf_items
            if it.checkState(self.COL_NAME) == Qt.CheckState.Checked
        )
        total = len(self._leaf_items)
        self._count_lbl.setText(f"{checked} / {total} files selected")

    def _accept(self):
        self.selected = {
            it.data(self.COL_NAME, Qt.ItemDataRole.UserRole)
            for it in self._leaf_items
            if it.checkState(self.COL_NAME) == Qt.CheckState.Checked
        }
        self.accept()

# ══════════════════════════════════════════════════════════════════
#  QUALITY DIALOG (with live-fetch support)
# ══════════════════════════════════════════════════════════════════
class QualityDlg(QDialog):
    def __init__(self, url, lang, theme, mode, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.url  = url
        self.lang = lang
        self.setWindowTitle(lang["quality_title"])
        self.setMinimumSize(430, 600)
        self.resize(430, 640)
        self.setModal(True)
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = build_style(theme, mode)
        self._style = (a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd)
        self.setStyleSheet(global_style(*self._style))

        self._sel  = "best"
        self._btns = []
        self._fetching_formats = False

        vl = QVBoxLayout(self)
        vl.setContentsMargins(20,20,20,16); vl.setSpacing(8)

        t = QLabel(f"  {lang['quality_title']}")
        t.setStyleSheet(f"color:rgba({ar},{ag},{ab},220);font-size:16px;font-weight:bold;")
        vl.addWidget(t)

        sub = QLabel(lang["quality_sub"])
        sub.setStyleSheet(f"color:{txt3};font-size:12px;")
        vl.addWidget(sub)

        self.btn_scroll = QScrollArea()
        self.btn_scroll.setWidgetResizable(True)
        self.btn_scroll.setMinimumHeight(210)
        self.btn_scroll.setMaximumHeight(260)
        self.btn_area = QWidget(); self.btn_layout = QVBoxLayout(self.btn_area)
        self.btn_layout.setSpacing(4); self.btn_layout.setContentsMargins(0,0,0,0)
        self.btn_layout.addStretch()
        self.btn_scroll.setWidget(self.btn_area)
        vl.addWidget(self.btn_scroll, 1)

        self._populate_default(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd)

        self.fetch_btn = QPushButton(f"⟳  {lang['fetch']}")
        self.fetch_btn.clicked.connect(self._fetch)
        self.fetch_btn.setStyleSheet(f"QPushButton{{background:{bg3};border:1px solid rgba({ar},{ag},{ab},60);border-radius:8px;padding:7px;color:rgba({ar},{ag},{ab},200);font-size:12px;}}QPushButton:hover{{border-color:rgba({ar},{ag},{ab},160);}}")
        vl.addWidget(self.fetch_btn)
        QTimer.singleShot(250, self._fetch)

        edit_box = QGroupBox("Clip / Crop")
        edit_box.setStyleSheet(
            f"QGroupBox{{border:1px solid {brd};border-radius:8px;margin-top:8px;padding:10px;color:{txt2};}}"
            f"QGroupBox::title{{subcontrol-origin:margin;left:10px;padding:0 4px;color:rgba({ar},{ag},{ab},210);}}"
        )
        eg = QGridLayout(edit_box); eg.setContentsMargins(10,12,10,8); eg.setHorizontalSpacing(8); eg.setVerticalSpacing(8)
        self.clip_cb = QCheckBox("Time clip")
        self.clip_start = QLineEdit(); self.clip_start.setPlaceholderText("Start 00:01:20")
        self.clip_end = QLineEdit(); self.clip_end.setPlaceholderText("End 00:03:45")
        self.crop_cb = QCheckBox("Visual crop")
        self.crop_combo = QComboBox(); self.crop_combo.addItems(["Original", "16:9", "9:16", "1:1"])
        eg.addWidget(self.clip_cb, 0, 0)
        eg.addWidget(self.clip_start, 0, 1)
        eg.addWidget(self.clip_end, 0, 2)
        eg.addWidget(self.crop_cb, 1, 0)
        eg.addWidget(self.crop_combo, 1, 1, 1, 2)
        vl.addWidget(edit_box)

        hl = QHBoxLayout()
        ok = QPushButton(lang["download"]); no = QPushButton(lang["cancel"])
        accent_fg = readable_on_rgb(ar, ag, ab)
        ok.setStyleSheet(f"QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},220),stop:1 rgba({ar},{ag},{ab},160));color:{accent_fg};border:none;border-radius:8px;padding:10px;font-weight:bold;}}QPushButton:hover{{background:rgba({ar},{ag},{ab},255);}}")
        no.setStyleSheet(f"QPushButton{{background:{bg2};border:1px solid {brd};border-radius:8px;padding:10px;color:{txt3};}}")
        ok.clicked.connect(self.accept); no.clicked.connect(self.reject)
        hl.addWidget(ok); hl.addWidget(no); vl.addLayout(hl)

    def _populate_default(self, a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd):
        unknown = "  | site size: Unknown"
        defaults = [
            (f"{self.lang['best']}{unknown}",   "best"),
            (f"{self.lang['p2160']}{unknown}",  "bestvideo[height<=2160]+bestaudio/best"),
            (f"{self.lang['p1080']}{unknown}",  "bestvideo[height<=1080]+bestaudio/best"),
            (f"{self.lang['p720']}{unknown}",   "bestvideo[height<=720]+bestaudio/best"),
            (f"{self.lang['p480']}{unknown}",   "bestvideo[height<=480]+bestaudio/best"),
            (f"{self.lang['p360']}{unknown}",   "bestvideo[height<=360]+bestaudio/best"),
            (f"{self.lang['mp3']}{unknown}",    "mp3"),
        ]
        self._rebuild_buttons(defaults, a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd)

    def _rebuild_buttons(self, fmt_list, a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd):
        for b in self._btns:
            self.btn_layout.removeWidget(b); b.deleteLater()
        self._btns = []
        stretch = self.btn_layout.takeAt(self.btn_layout.count() - 1)
        for lbl, q in fmt_list:
            b = QPushButton(lbl); b.setCheckable(True); b.setProperty("q", q)
            b.setMinimumHeight(34)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setStyleSheet(
                f"QPushButton{{background:{bg2};border:1px solid {brd};border-radius:8px;"
                f"padding:8px 12px;color:{txt};font-size:12px;text-align:left;}}"
                f"QPushButton:hover{{border-color:rgba({ar},{ag},{ab},120);color:rgba({ar},{ag},{ab},220);}}"
                f"QPushButton:checked{{background:rgba({ar},{ag},{ab},30);border-color:rgba({ar},{ag},{ab},180);"
                f"color:rgba({ar},{ag},{ab},220);font-weight:bold;}}")
            b.clicked.connect(lambda _, btn=b: self._click(btn))
            self.btn_layout.addWidget(b); self._btns.append(b)
        self.btn_layout.addStretch()
        if self._btns: self._btns[0].setChecked(True); self._sel = self._btns[0].property("q")

    def _click(self, btn):
        for b in self._btns: b.setChecked(False)
        btn.setChecked(True); self._sel = btn.property("q")

    def _fetch(self):
        if self._fetching_formats:
            return
        self._fetching_formats = True
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("⟳ Fetching…")
        tmp = DownloadItem(self.url, "", "", dl_type="yt")
        w = YTWorker(0, tmp, "", fetch_only=True)
        w.formats_ready.connect(self._on_formats)
        w.start()
        self._fw = w

    def _on_formats(self, _, fmts):
        self._fetching_formats = False
        self.fetch_btn.setText(f"⟳  {self.lang['fetch']}")
        self.fetch_btn.setEnabled(True)
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,*_ = self._style
        self._rebuild_buttons(fmts, a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd)

    def quality(self): return self._sel

    def selected_size(self):
        for b in self._btns:
            if b.isChecked():
                return parse_size_text(b.text())
        return 0

    def edit_options(self):
        return {
            "clip_start": self.clip_start.text().strip() if self.clip_cb.isChecked() else "",
            "clip_end": self.clip_end.text().strip() if self.clip_cb.isChecked() else "",
            "crop_preset": self.crop_combo.currentText() if self.crop_cb.isChecked() else "Original",
        }

# ══════════════════════════════════════════════════════════════════
#  SPEED METER WIDGET  (standalone floating overlay)
# ══════════════════════════════════════════════════════════════════
# Stonepit local assistant: command -> proposal -> user approval -> RapidGet flow.
class StonepitWorker(QThread):
    message  = pyqtSignal(str)
    proposal = pyqtSignal(dict)

    def __init__(self, command, texts=None):
        super().__init__()
        self.command = (command or "").strip()
        self.texts = texts or LANG["English"]

    def _t(self, key):
        return self.texts.get(key, LANG["English"].get(key, key))

    def run(self):
        cmd = self.command
        if not cmd:
            self.message.emit(self._t("stonepit_empty"))
            return

        url = self._extract_url(cmd)
        if url:
            self._emit_link(url, self._t("stonepit_link_found"))
            return

        low = cmd.lower()
        chrome_words = (
            "chrome", "google browser", "google chrome", "क्रोम", "গুগল ক্রোম",
            "ক্রোম", "குரோம்", "கூகுள் குரோம்", "క్రోమ్", "గూగుల్ క్రోమ్",
            "ક્રોમ", "ಕ್ರೋಮ್", "क्रोम", "کروم", "كروم", "cromo", "谷歌瀏覽器",
            "chrome 瀏覽器", "chrome 浏览器",
        )
        firefox_words = (
            "firefox", "mozilla", "फ़ायरफ़ॉक्स", "फायरफॉक्स", "ফায়ারফক্স",
            "ஃபயர்பாக்ஸ்", "ఫైర్‌ఫాక్స్", "ફાયરફોક્સ", "ಫೈರ್‌ಫಾಕ್ಸ್",
            "فائر فاکس", "firefoxe", "火狐", "firefox 瀏覽器",
        )
        zip_words = (
            "7zip", "7-zip", "7 zip", "৭জিপ", "७-zip", "৭-zip", "7 ஜிப்",
            "7జిప్", "7 ઝિપ", "7 ಜಿಪ್", "۷زیپ", "7 zip baixar",
        )
        vlc_words = (
            "vlc", "media player", "vlc media player", "वीएलसी", "ভিএলসি",
            "விஎல்சி", "వీఎల్సీ", "વીએલસી", "ವಿಎಲ್ಸಿ", "وی ایل سی",
            "vlc player", "reprodutor vlc", "vlc 播放器",
        )

        if any(w in low for w in chrome_words):
            self._emit_link(
                "https://dl.google.com/chrome/install/latest/chrome_installer.exe",
                self._t("stonepit_chrome_note"))
            return
        if any(w in low for w in firefox_words):
            self._emit_link(
                "https://download.mozilla.org/?product=firefox-latest-ssl&os=win64&lang=en-US",
                self._t("stonepit_firefox_note"))
            return
        if any(w in low for w in zip_words):
            link = self._find_7zip_link()
            if link:
                self._emit_link(link, self._t("stonepit_7zip_note"))
            else:
                self.message.emit(self._t("stonepit_7zip_error"))
            return
        if any(w in low for w in vlc_words):
            link = self._find_vlc_link()
            if link:
                self._emit_link(link, self._t("stonepit_vlc_note"))
            else:
                self.message.emit(self._t("stonepit_vlc_error"))
            return

        torrent_words = (
            "torrent", ".torrent", "magnet", "टोरेंट", "টরেন্ট", "டோரண்ட்",
            "టొరెంట్", "ટોરેન્ટ", "ಟೊರೆಂಟ್", "ٹورنٹ", "تورنت", "種子", "磁力",
        )
        if any(w in low for w in torrent_words):
            self.message.emit(self._t("stonepit_torrent_safe"))
            return

        if self._looks_like_video_search(low):
            self._search_youtube(cmd)
            return

        self.message.emit(self._t("stonepit_unknown"))

    def _extract_url(self, text):
        m = re.search(r"(magnet:\?[^\s]+|https?://[^\s]+)", text or "", re.I)
        return m.group(1).strip(" \t\r\n\"'<>") if m else ""

    def _emit_link(self, url, note):
        size = 0
        if not url.startswith("magnet:"):
            size = probe_url_size(url)
        name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "download"
        self.proposal.emit({
            "title": name,
            "url": url,
            "source": urlparse(url).netloc or "magnet",
            "size": size,
            "note": note,
        })

    def _looks_like_video_search(self, low):
        return any(w in low for w in (
            "youtube", "yt ", "video", "song", "music", "gaan", "গান", "ভিডিও",
            "ইউটিউব", "यूट्यूब", "वीडियो", "गाना", "संगीत", "यूट्युब",
            "வீடியோ", "பாட்டு", "இசை", "யூடியூப்", "వీడియో", "పాట", "సంగీతం",
            "యూట్యూబ్", "વિડિયો", "ગીત", "સંગીત", "ಯೂಟ್ಯೂಬ್", "ವೀಡಿಯೊ",
            "ಹಾಡು", "موسیقی", "گانا", "وڈیو", "يوتيوب", "vídeo", "musica",
            "música", "canção", "影片", "視頻", "歌曲", "音樂", "音乐",
            "gan", "gaan", "ganta", "gantai", "gait", "ganta namaw", "gan namaw",
            "namaw", "namao", "namai", "namate", "namabo", "namay dao",
            "khujo", "khuje", "khuj", "khojo", "khuje dao", "download koro",
        ))

    def _clean_video_query(self, text):
        q = re.sub(r"https?://\S+", "", text, flags=re.I)
        for word in ("youtube", "yt", "video", "song", "music", "gaan",
                     "থেকে", "নামাও", "নামাই", "download", "khuje", "খুঁজে", "খুজে",
                     "से", "डाउनलोड", "लाओ", "खोजो", "கண்டுபிடி", "பதிவிறக்கு",
                     "నుండి", "డౌన్‌లోడ్", "తెచ్చి", "ડાઉનલોડ", "શોધો",
                     "ಡೌನ್‌ಲೋಡ್", "ಹುಡುಕಿ", "ڈاؤن لوڈ", "تلاش", "baixar",
                     "procure", "pesquise", "下載", "下载", "搵", "找",
                     "gan", "gaan", "ganta", "gantai", "gait", "namaw", "namao",
                     "namai", "namate", "namabo", "namay dao", "khujo", "khuje",
                     "khuj", "khojo", "khuje dao", "download koro"):
            q = re.sub(rf"\b{re.escape(word)}\b", " ", q, flags=re.I)
        q = re.sub(r"\s+", " ", q).strip()
        return q or text.strip()

    def _search_youtube(self, text):
        query = self._clean_video_query(text)
        self.message.emit(f"{self._t('stonepit_searching_youtube')}: {query}")
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
                info = ydl.extract_info(f"ytsearch5:{query}", download=False)
            entries = info.get("entries") or []
            first = next((e for e in entries if e), None)
            if not first:
                self.message.emit(self._t("stonepit_no_youtube"))
                return
            url = first.get("url") or first.get("webpage_url")
            if url and not str(url).startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"
            self.proposal.emit({
                "title": first.get("title") or query,
                "url": url,
                "source": "youtube.com",
                "size": 0,
                "note": self._t("stonepit_youtube_note"),
            })
        except Exception as e:
            self.message.emit(f"{self._t('stonepit_youtube_error')}: {e}")

    def _find_7zip_link(self):
        try:
            html = requests.get("https://www.7-zip.org/download.html", timeout=12).text
            matches = re.findall(r'href="([^"]*a/7z(\d+)[^"]*-x64\.exe)"', html, re.I)
            if not matches:
                return ""
            href = max(matches, key=lambda m: int(m[1]))[0]
            return href if href.startswith("http") else "https://www.7-zip.org/" + href.lstrip("/")
        except Exception:
            return ""

    def _find_vlc_link(self):
        try:
            html = requests.get("https://www.videolan.org/vlc/download-windows.html", timeout=12).text
            m = re.search(r'https://get\.videolan\.org/vlc/[^"\']+win64\.exe', html, re.I)
            if m:
                return m.group(0)
            m = re.search(r'href="([^"]*win64\.exe)"', html, re.I)
            if not m:
                return ""
            href = m.group(1)
            if href.startswith("http"):
                return href
            if href.startswith("//"):
                return "https:" + href
            if href.startswith("get.videolan.org"):
                return "https://" + href
            return "https://www.videolan.org/" + href.lstrip("/")
        except Exception:
            return ""


class StonepitVoiceWorker(QThread):
    status = pyqtSignal(str)
    wake = pyqtSignal()
    command = pyqtSignal(str)

    def __init__(self, texts=None):
        super().__init__()
        self.texts = texts or LANG["English"]
        self._stop = False
        self._waiting_command = False

    def stop(self):
        self._stop = True

    def _t(self, key):
        return self.texts.get(key, LANG["English"].get(key, key))

    def run(self):
        try:
            import speech_recognition as sr
        except Exception:
            self.status.emit(self._t("stonepit_mic_missing"))
            return
        try:
            names = sr.Microphone.list_microphone_names()
            if not names:
                self.status.emit(self._t("stonepit_mic_missing"))
                return
        except Exception:
            self.status.emit(self._t("stonepit_mic_missing"))
            return

        rec = sr.Recognizer()
        try:
            mic = sr.Microphone()
        except Exception:
            self.status.emit(self._t("stonepit_mic_missing"))
            return

        try:
            with mic as source:
                self.status.emit(self._t("stonepit_mic_ready"))
                try:
                    rec.adjust_for_ambient_noise(source, duration=0.5)
                except Exception:
                    pass
                while not self._stop:
                    try:
                        audio = rec.listen(source, timeout=1, phrase_time_limit=5)
                    except sr.WaitTimeoutError:
                        continue
                    except Exception:
                        self.status.emit(self._t("stonepit_mic_missing"))
                        return

                    try:
                        text = rec.recognize_google(audio)
                    except sr.UnknownValueError:
                        continue
                    except Exception as e:
                        self.status.emit(f"{self._t('stonepit_mic_missing')} ({e})")
                        return

                    low = text.lower().strip()
                    if self._waiting_command:
                        self._waiting_command = False
                        if low:
                            self.command.emit(text)
                        self.status.emit(self._t("stonepit_mic_ready"))
                    elif "stonepit" in low or "stone pit" in low:
                        self._waiting_command = True
                        self.wake.emit()
                        self.status.emit(self._t("stonepit_mic_listening"))
        except Exception:
            self.status.emit(self._t("stonepit_mic_missing"))


class StonepitVoiceCaptureWorker(QThread):
    status = pyqtSignal(str)
    command = pyqtSignal(str)
    finished_listening = pyqtSignal()

    def __init__(self, texts=None):
        super().__init__()
        self.texts = texts or LANG["English"]

    def _t(self, key):
        return self.texts.get(key, LANG["English"].get(key, key))

    def run(self):
        try:
            import speech_recognition as sr
            if not sr.Microphone.list_microphone_names():
                self.status.emit(self._t("stonepit_mic_missing"))
                return
            rec = sr.Recognizer()
            with sr.Microphone() as source:
                self.status.emit(self._t("stonepit_mic_listening"))
                try:
                    rec.adjust_for_ambient_noise(source, duration=0.3)
                except Exception:
                    pass
                audio = rec.listen(source, timeout=5, phrase_time_limit=7)
            text = rec.recognize_google(audio)
            if text.strip():
                self.command.emit(text.strip())
        except Exception as e:
            self.status.emit(f"{self._t('stonepit_mic_missing')} ({e})")
        finally:
            self.finished_listening.emit()


class VoiceWave(QWidget):
    def __init__(self, accent="#00d4ff", parent=None):
        super().__init__(parent)
        self.setFixedSize(64, 34)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._accent = QColor(accent)
        self._phase = 0
        self._active = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set_active(self, active):
        self._active = bool(active)
        self.setVisible(self._active)
        if self._active and not self._timer.isActive():
            self._timer.start(80)
        elif not self._active:
            self._timer.stop()
            self.update()

    def _tick(self):
        self._phase = (self._phase + 1) % 24
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        color = QColor(self._accent)
        color.setAlpha(220 if self._active else 70)
        p.setBrush(color)
        mid = self.height() / 2
        bars = 7
        for i in range(bars):
            x = 5 + i * 8
            if self._active:
                amp = 5 + abs(math.sin((self._phase + i * 2) / 3.0)) * 18
            else:
                amp = 4
            p.drawRoundedRect(QRectF(x, mid - amp / 2, 4, amp), 2, 2)
        p.end()


class StonepitInput(QLineEdit):
    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        parent = self.parent()
        if parent and hasattr(parent, "_input_focus_out"):
            parent._input_focus_out(event.reason())


class StonepitDlg(QDialog):
    def __init__(self, lang, theme, mode, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.lang = lang
        self.setWindowTitle("Stonepit")
        self.setMinimumSize(520, 520)
        self.setModal(False)
        self._pending = None
        self._typing_locked = False
        self._input_cursor_pos = 0
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = build_style(theme, mode)
        self.setStyleSheet(global_style(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win))

        vl = QVBoxLayout(self); vl.setContentsMargins(18,18,18,14); vl.setSpacing(10)
        title = QLabel("Stonepit")
        title.setStyleSheet(f"color:rgba({ar},{ag},{ab},230);font-size:20px;font-weight:bold;letter-spacing:1px;")
        sub = QLabel(self._t("stonepit_sub"))
        sub.setStyleSheet(f"color:{txt3};font-size:12px;")
        vl.addWidget(title); vl.addWidget(sub)

        self.chat = QTextEdit()
        self.chat.setReadOnly(True)
        self.chat.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.chat.setMinimumHeight(260)
        self.chat.setStyleSheet(f"QTextEdit{{background:{bg2};border:1px solid {brd};border-radius:8px;padding:10px;color:{txt2};}}")
        vl.addWidget(self.chat, 1)

        row = QHBoxLayout()
        self.input = StonepitInput()
        self.input.setParent(self)
        self.input.setPlaceholderText(self._t("stonepit_placeholder"))
        self.input.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.input.returnPressed.connect(self._ask)
        self.input.textEdited.connect(self._lock_input_focus)
        self.input.cursorPositionChanged.connect(lambda _old, new: setattr(self, "_input_cursor_pos", new))
        self.ask_btn = QPushButton(self._t("stonepit_enter"))
        self.ask_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ask_btn.setAutoDefault(False)
        self.ask_btn.setDefault(False)
        self.ask_btn.clicked.connect(lambda _=False: self._ask())
        row.addWidget(self.input); row.addWidget(self.ask_btn)
        vl.addLayout(row)

        self.approve_btn = QPushButton(self._t("stonepit_approve"))
        self.approve_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.approve_btn.setAutoDefault(False)
        self.approve_btn.setDefault(False)
        self.approve_btn.setEnabled(False)
        self.approve_btn.clicked.connect(lambda _=False: self._approve())
        accent_fg = readable_on_rgb(ar, ag, ab)
        self.approve_btn.setStyleSheet(
            f"QPushButton{{background:rgba({ar},{ag},{ab},210);border:none;border-radius:8px;padding:10px;color:{accent_fg};font-weight:bold;}}"
            f"QPushButton:disabled{{background:{bg3};color:{txt3};}}")
        vl.addWidget(self.approve_btn)

        self._bot(self._t("stonepit_ready"))
        self._focus_guard = QTimer(self)
        self._focus_guard.timeout.connect(self._keep_input_focus)
        self._focus_guard.start(80)
        QTimer.singleShot(0, self._focus_input)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._focus_input)

    def _t(self, key):
        return self.lang.get(key, LANG["English"].get(key, key))

    def _append(self, who, text):
        self.chat.append(f"<b>{who}:</b> {text}")

    def _bot(self, text, speak=False):
        self._append("Stonepit", text)

    def _input_focus_out(self, reason):
        if reason == Qt.FocusReason.PopupFocusReason:
            return
        if self.isVisible() and self.isActiveWindow():
            QTimer.singleShot(0, self._focus_input)

    def _lock_input_focus(self):
        self._typing_locked = True
        self._keep_input_focus()

    def _keep_input_focus(self):
        if not self.isVisible() or not self.isActiveWindow():
            return
        if QApplication.activePopupWidget():
            return
        fw = QApplication.focusWidget()
        if fw is not self.input:
            self._focus_input()

    def _focus_input(self):
        if not self.isVisible():
            return
        pos = max(0, min(self._input_cursor_pos, len(self.input.text())))
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)
        self.input.setCursorPosition(pos)

    def _ask(self):
        cmd = self.input.text().strip()
        if not cmd:
            self._bot(self._t("stonepit_empty"))
            QTimer.singleShot(0, self.input.setFocus)
            return
        self._typing_locked = False
        self.input.clear()
        self._pending = None
        self.approve_btn.setEnabled(False)
        self._append("You", cmd)
        if self._handle_settings_command(cmd):
            QTimer.singleShot(0, self._focus_input)
            return
        self._worker = StonepitWorker(cmd, self.lang)
        self._worker.message.connect(self._bot)
        self._worker.proposal.connect(self._proposal)
        self._worker.start()
        QTimer.singleShot(0, self.input.setFocus)

    def _handle_settings_command(self, cmd):
        parent = self.parent()
        if not parent or not hasattr(parent, "S"):
            return False
        low = (cmd or "").casefold()
        wants_settings = any(w in low for w in (
            "setting", "settings", "set ", "change", "make ", "koro", "kore dao",
            "dao", "on", "off", "enable", "disable", "language", "lang", "bhasha",
            "theme", "mode", "dark", "light", "opacity", "folder", "threads",
            "notification", "startup", "monitor", "speed", "port", "concurrent",
            "\u09b8\u09c7\u099f\u09bf\u0982", "\u09ad\u09be\u09b7\u09be",
            "\u09a5\u09bf\u09ae", "\u09ae\u09cb\u09a1", "\u09a1\u09be\u09b0\u09cd\u0995",
            "\u09b2\u09be\u0987\u099f", "\u09ab\u09cb\u09b2\u09cd\u09a1\u09be\u09b0",
        ))
        if not wants_settings:
            return False

        s = dict(parent.S)
        changed = []

        def set_value(key, value, label):
            if s.get(key) != value:
                s[key] = value
                changed.append(f"{label}: {value}")

        def bool_value():
            tokens = set(re.findall(r"[a-z0-9]+", low))
            if any(w in tokens for w in ("off", "disable", "false", "no", "bondho", "stop")) or "\u09ac\u09a8\u09cd\u09a7" in low:
                return False
            if any(w in tokens for w in ("on", "enable", "true", "yes", "chalu", "start")) or "\u099a\u09be\u09b2\u09c1" in low:
                return True
            return None

        lang_aliases = {
            "english": "English", "en": "English",
            "bangla": "\u09ac\u09be\u0982\u09b2\u09be", "bengali": "\u09ac\u09be\u0982\u09b2\u09be", "bn": "\u09ac\u09be\u0982\u09b2\u09be",
            "hindi": "Hindi", "hi": "Hindi",
            "portuguese": "Portuguese", "portugues": "Portuguese", "pt": "Portuguese",
            "cantonese": "Cantonese", "chinese": "Cantonese", "zh": "Cantonese",
        }
        if any(w in low for w in ("language", "lang", "bhasha", "\u09ad\u09be\u09b7\u09be")):
            for alias, name in lang_aliases.items():
                if alias in low and name in LANG:
                    set_value("language", name, "Language")
                    break
            else:
                for name in LANG:
                    if str(name).casefold() in low:
                        set_value("language", name, "Language")
                        break

        if "theme" in low or "\u09a5\u09bf\u09ae" in low:
            for name in THEMES:
                if str(name).casefold() in low:
                    set_value("theme", name, "Theme")
                    break

        if "dark" in low or "\u09a1\u09be\u09b0\u09cd\u0995" in low:
            set_value("mode", "Dark", "Mode")
        elif "light" in low or "\u09b2\u09be\u0987\u099f" in low:
            set_value("mode", "Light", "Mode")
        elif "os default" in low or "system" in low:
            set_value("mode", "OS Default", "Mode")

        m = re.search(r"(?:opacity|transparent|transparency|alpha)\D*(\d{1,3})", low)
        if m:
            set_value("opacity", max(30, min(100, int(m.group(1)))), "Opacity")

        m = re.search(r"(?:net opacity|monitor opacity|speed opacity)\D*(\d{1,3})", low)
        if m:
            set_value("net_opacity", max(10, min(100, int(m.group(1)))), "Speed monitor opacity")

        m = re.search(r"(?:concurrent|max concurrent|max download)\D*(\d{1,2})", low)
        if m:
            set_value("max_concurrent", max(1, min(10, int(m.group(1)))), "Max concurrent")

        m = re.search(r"(?:port|extension port|ext port)\D*(\d{2,5})", low)
        if m:
            set_value("ext_port", max(1, min(65535, int(m.group(1)))), "Extension port")

        m = re.search(r"(?:threads?|thread count)\D*(auto|\d{1,2})", low)
        if m:
            value = m.group(1)
            set_value("threads", "auto" if value == "auto" else max(1, min(32, int(value))), "Threads")

        folder_match = re.search(r"(?:download folder|folder|save folder|path)\s+(.+)$", cmd, re.I)
        if folder_match:
            folder = folder_match.group(1).strip().strip("\"'")
            if folder:
                try:
                    Path(folder).mkdir(parents=True, exist_ok=True)
                    set_value("download_folder", folder, "Download folder")
                except Exception as e:
                    self._bot(f"Could not set folder: {e}")
                    return True

        bv = bool_value()
        if bv is not None:
            if "startup" in low or "start with windows" in low:
                set_value("startup", bv, "Startup")
                try:
                    set_startup(bv)
                except Exception:
                    pass
            if "notification" in low or "notify" in low:
                set_value("notification", bv, "Notification")
            if "speed" in low or "net monitor" in low or "monitor" in low:
                set_value("net_monitor", bv, "Speed monitor")
            if "pin" in low or "top" in low:
                set_value("net_pin_top", bv, "Always on top")
            if "dock" in low or "taskbar" in low:
                set_value("net_dock_taskbar", bv, "Dock to taskbar")
            if "lock" in low:
                set_value("net_locked", bv, "Monitor lock")

        if not changed:
            if "settings" in low or "setting" in low:
                self._bot("Tell me the setting and value. Example: language Bangla, theme Cyan, dark mode, opacity 90, notification off.")
                return True
            return False

        save_settings(s)
        if hasattr(parent, "_apply_theme_live"):
            parent._apply_theme_live(dict(s))
            self.lang = getattr(parent, "L", self.lang)
        self._bot("Settings updated: " + ", ".join(changed))
        return True

    def _proposal(self, data):
        self._pending = data
        size = fmt_bytes(data.get("size", 0)) if data.get("size", 0) else "Unknown"
        self._bot(
            f"{self._t('stonepit_found')}: <b>{data.get('title','download')}</b><br>"
            f"{self._t('stonepit_source')}: {data.get('source','')}<br>"
            f"{self._t('stonepit_site_size')}: {size}<br>"
            f"{data.get('note','')}<br>"
            f"{self._t('stonepit_approve_hint')}")
        self.approve_btn.setEnabled(True)

    def _approve(self):
        if not self._pending:
            return
        url = self._pending.get("url", "")
        self._bot(self._t("stonepit_approved"))
        parent = self.parent()
        if parent and hasattr(parent, "_create"):
            parent._create(url)

    def closeEvent(self, event):
        super().closeEvent(event)


class SpeedMeter(QWidget):
    """Floating translucent speed meter with mini graph."""
    HISTORY = 30

    def __init__(self, settings, accent):
        super().__init__()
        self.settings = settings
        self.accent   = accent
        self.setFixedSize(170, 44)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.BypassWindowManagerHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._drag = None
        if self.settings.get("net_dock_taskbar", False):
            self._dock_to_taskbar()
        else:
            self.move(settings.get("net_pos_x",100), settings.get("net_pos_y",100))

        net = psutil.net_io_counters()
        self._pr = net.bytes_recv; self._ps = net.bytes_sent
        self._hist_dl = deque([0]*self.HISTORY, maxlen=self.HISTORY)
        self._hist_ul = deque([0]*self.HISTORY, maxlen=self.HISTORY)
        self._dl_val  = 0
        self._ul_val  = 0
        self._peak    = 1

        self.setWindowOpacity(settings.get("net_opacity",90)/100)
        t = QTimer(self); t.timeout.connect(self._tick); t.start(1000)
        top_t = QTimer(self); top_t.timeout.connect(self._force_top); top_t.start(3000)
        dock_t = QTimer(self); dock_t.timeout.connect(self._dock_to_taskbar); dock_t.start(2500)

    def _force_top(self):
        if self.isVisible():
            self.raise_(); self.activateWindow()

    def _dock_to_taskbar(self):
        if not self.settings.get("net_dock_taskbar", False):
            return
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if not screen:
            return
        avail = screen.availableGeometry()
        full = screen.geometry()
        margin = 8

        # availableGeometry excludes the taskbar. Snap to the edge nearest to it.
        if avail.bottom() < full.bottom():       # bottom taskbar
            x = avail.right() - self.width() - margin
            y = avail.bottom() - self.height() - margin
        elif avail.top() > full.top():           # top taskbar
            x = avail.right() - self.width() - margin
            y = avail.top() + margin
        elif avail.left() > full.left():         # left taskbar
            x = avail.left() + margin
            y = avail.bottom() - self.height() - margin
        elif avail.right() < full.right():       # right taskbar
            x = avail.right() - self.width() - margin
            y = avail.bottom() - self.height() - margin
        else:
            x = avail.right() - self.width() - margin
            y = avail.bottom() - self.height() - margin
        self.move(x, y)

    def _tick(self):
        net = psutil.net_io_counters()
        dl  = net.bytes_recv - self._pr
        ul  = net.bytes_sent - self._ps
        self._pr = net.bytes_recv; self._ps = net.bytes_sent
        self._dl_val = dl; self._ul_val = ul
        self._hist_dl.append(dl); self._hist_ul.append(ul)
        self._peak = max(1, max(max(self._hist_dl), max(self._hist_ul)))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ar, ag, ab = hrx(self.accent)
        W, H = self.width(), self.height()   # 170 x 44

        # Background pill
        p.setBrush(QBrush(QColor(10, 14, 26, 215)))
        p.setPen(QPen(QColor(ar, ag, ab, 90), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, W-1, H-1), 8, 8)

        # ── Labels  (top strip: y 2..20) ─────────────────────────
        p.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        half = W // 2
        p.setPen(QColor(ar, ag, ab, 235))
        p.drawText(QRectF(6, 2, half - 6, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"\u2b07 {self._fmtspeed(self._dl_val)}")
        p.setPen(QColor(0, 200, 140, 220))
        p.drawText(QRectF(half, 2, half - 6, 18),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"\u2b06 {self._fmtspeed(self._ul_val)}")

        # ── Mini graph (bottom strip: y 21..H-3) ─────────────────
        gx = 6; gy = 21; gw = W - 12; gh = H - 25
        history = self.HISTORY

        def draw_graph(hist, color_rgba):
            pts = list(hist)
            if len(pts) < 2: return
            path = QPainterPath()
            for i, v in enumerate(pts):
                x = gx + i * gw / (history - 1)
                y = gy + gh - max(0, min(gh, v / self._peak * gh))
                if i == 0: path.moveTo(x, y)
                else:       path.lineTo(x, y)
            p.setPen(QPen(QColor(*color_rgba), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        draw_graph(self._hist_dl, (ar, ag, ab, 180))
        draw_graph(self._hist_ul, (0, 200, 140, 130))
        p.end()

    def mousePressEvent(self, e):
        if self.settings.get("net_dock_taskbar", False): return
        if self.settings.get("net_locked", False): return
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self.settings.get("net_dock_taskbar", False): return
        if self.settings.get("net_locked", False): return
        if self._drag and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        if self.settings.get("net_dock_taskbar", False): return
        if self.settings.get("net_locked", False): return
        self._drag = None
        self.settings["net_pos_x"] = self.x()
        self.settings["net_pos_y"] = self.y()
        save_settings(self.settings)

    @staticmethod
    def _fmtspeed(b):
        if b < 1024:     return f"{b}B/s"
        if b < 1<<20:    return f"{b/1024:.0f}KB/s"
        return f"{b/(1<<20):.1f}MB/s"

# ══════════════════════════════════════════════════════════════════
#  SETTINGS DIALOG
# ══════════════════════════════════════════════════════════════════
class SettingsDlg(QDialog):
    apply_signal = pyqtSignal(dict)

    def __init__(self, settings, lang, theme, mode, net_monitor, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.settings    = dict(settings)
        self.lang        = lang
        self.net_monitor = net_monitor
        self._rebuilding = False
        self.setWindowTitle(lang["settings_title"])
        self.setMinimumSize(520, 580)
        self.setModal(True)
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = build_style(theme, mode)
        self.setStyleSheet(global_style(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win))
        self._build(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd)

    def _clear_layout(self, layout):
        while layout and layout.count():
            item = layout.takeAt(0)
            child = item.widget()
            if child:
                child.deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def _rebuild_with_current_style(self):
        self._rebuilding = True
        old = self.layout()
        if old:
            self._clear_layout(old)
        self.lang = LANG.get(self.settings.get("language", "English"), LANG["English"])
        _, theme = resolve_theme(self.settings.get("theme", "Cyan"))
        _, mode = resolve_mode(self.settings.get("mode", "Dark"))
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = build_style(theme, mode)
        self.setStyleSheet(global_style(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win))
        self._build(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd)
        self._rebuilding = False

    def _preview_live_theme(self):
        if self._rebuilding:
            return
        self._collect()
        _tn, _theme = resolve_theme(self.settings.get("theme", "Cyan"))
        _mn, _mode = resolve_mode(self.settings.get("mode", "Dark"))
        self.settings["theme"], self.settings["mode"] = _tn, _mn
        save_settings(self.settings)
        self.apply_signal.emit(dict(self.settings))
        self._rebuild_with_current_style()

    def _build(self, a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd):
        vl = self.layout()
        if vl is None:
            vl = QVBoxLayout(self)
        vl.setContentsMargins(0,0,0,0); vl.setSpacing(0)

        hdr = QWidget(); hdr.setFixedHeight(50)
        hdr.setStyleSheet(f"background:rgba({ar},{ag},{ab},20);border-bottom:1px solid rgba({ar},{ag},{ab},60);")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(20,0,20,0)
        hl.addWidget(QLabel(f"  {self.lang['settings_title']}",
                            styleSheet=f"color:rgba({ar},{ag},{ab},220);font-size:16px;font-weight:bold;"))
        hl.addStretch(); vl.addWidget(hdr)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner  = QWidget(); il = QVBoxLayout(inner)
        il.setContentsMargins(24,16,24,16); il.setSpacing(10)

        def sec(title):
            w = QWidget(); w.setFixedHeight(28)
            w.setStyleSheet(f"background:rgba({ar},{ag},{ab},12);border-radius:4px;")
            wl = QHBoxLayout(w); wl.setContentsMargins(10,0,10,0)
            wl.addWidget(QLabel(title,styleSheet=f"color:rgba({ar},{ag},{ab},200);font-size:11px;font-weight:bold;letter-spacing:2px;"))
            il.addWidget(w)

        def row(lbl, widget):
            h = QHBoxLayout()
            lb = QLabel(lbl); lb.setMinimumWidth(210); lb.setStyleSheet(f"color:{txt2};")
            h.addWidget(lb); h.addWidget(widget); h.addStretch(); il.addLayout(h)

        def sl_row(lbl, slider, lbl_val):
            h = QHBoxLayout()
            lb = QLabel(lbl); lb.setMinimumWidth(210); lb.setStyleSheet(f"color:{txt2};")
            h.addWidget(lb); h.addWidget(slider); h.addWidget(lbl_val); il.addLayout(h)

        sec("GENERAL")
        self.lang_cb  = QComboBox(); self.lang_cb.addItems(list(LANG.keys())); self.lang_cb.setCurrentText(self.settings.get("language","English"))
        self.theme_cb = QComboBox(); self.theme_cb.addItems(list(THEMES.keys())); self.theme_cb.setCurrentText(self.settings.get("theme","Cyan"))
        USER_MODES = ["Dark", "Light", "OS Default"]
        self.mode_cb  = QComboBox(); self.mode_cb.addItems(USER_MODES)
        _saved_mode, _ = resolve_mode(self.settings.get("mode","Dark"))
        self.settings["mode"] = _saved_mode
        self.mode_cb.setCurrentText(_saved_mode if _saved_mode in USER_MODES else "Dark")
        row(self.lang["language"], self.lang_cb)
        row(self.lang["theme"],    self.theme_cb)
        row(self.lang["mode"],     self.mode_cb)

        self.theme_cb.currentTextChanged.connect(lambda _: QTimer.singleShot(0, self._preview_live_theme))
        self.mode_cb.currentTextChanged.connect(lambda _: QTimer.singleShot(0, self._preview_live_theme))

        self.app_op     = QSlider(Qt.Orientation.Horizontal); self.app_op.setRange(30,100); self.app_op.setValue(self.settings.get("opacity",100))
        self.app_op_lbl = QLabel(f"{self.app_op.value()}%"); self.app_op_lbl.setFixedWidth(40)
        self.app_op.valueChanged.connect(lambda v: self.app_op_lbl.setText(f"{v}%"))
        sl_row(self.lang["transparency"], self.app_op, self.app_op_lbl)

        self.startup_cb = QCheckBox(); self.startup_cb.setChecked(self.settings.get("startup",False))
        self.notif_cb   = QCheckBox(); self.notif_cb.setChecked(self.settings.get("notification",True))
        row(self.lang["startup"],      self.startup_cb)
        row(self.lang["notification"], self.notif_cb)

        sec("DOWNLOAD")
        fl = QHBoxLayout()
        self.folder_in = QLineEdit(self.settings.get("download_folder", get_default_dir()))
        br = QPushButton(self.lang["browse"]); br.setFixedWidth(80); br.clicked.connect(self._browse)
        fl.addWidget(self.folder_in); fl.addWidget(br)
        lb2 = QLabel(self.lang["dl_folder"]); lb2.setMinimumWidth(210); lb2.setStyleSheet(f"color:{txt2};")
        hf  = QHBoxLayout(); hf.addWidget(lb2); hf.addLayout(fl); il.addLayout(hf)

        self.thread_cb = QComboBox(); self.thread_cb.addItems([self.lang["auto"],"4","8","16","32"])
        tc = self.settings.get("threads","auto")
        self.thread_cb.setCurrentText(self.lang["auto"] if tc=="auto" else str(tc))
        row(self.lang["threads"], self.thread_cb)

        self.conc_sb = QSpinBox(); self.conc_sb.setRange(1,10); self.conc_sb.setValue(self.settings.get("max_concurrent",3))
        row(self.lang["max_concurrent"], self.conc_sb)

        self.port_in = QLineEdit(str(self.settings.get("ext_port",49152))); self.port_in.setFixedWidth(100)
        row(self.lang["port"], self.port_in)

        sec("SPEED MONITOR")
        self.net_cb  = QCheckBox(); self.net_cb.setChecked(self.settings.get("net_monitor",False))
        self.net_cb.toggled.connect(lambda c: self.net_monitor.show() if c else self.net_monitor.hide())
        row(self.lang["net_monitor"], self.net_cb)

        self.net_op     = QSlider(Qt.Orientation.Horizontal); self.net_op.setRange(10,100); self.net_op.setValue(self.settings.get("net_opacity",90))
        self.net_op_lbl = QLabel(f"{self.net_op.value()}%"); self.net_op_lbl.setFixedWidth(40)
        self.net_op.valueChanged.connect(lambda v: (self.net_op_lbl.setText(f"{v}%"), self.net_monitor.setWindowOpacity(v/100)))
        sl_row(self.lang["transparency"], self.net_op, self.net_op_lbl)

        self.pin_cb = QCheckBox(); self.pin_cb.setChecked(self.settings.get("net_pin_top",True))
        row(self.lang["pin_top"], self.pin_cb)

        self.dock_cb = QCheckBox()
        self.dock_cb.setChecked(self.settings.get("net_dock_taskbar", False))
        self.dock_cb.toggled.connect(self._dock_toggled)
        row("Attach to taskbar", self.dock_cb)

        self.lock_cb = QCheckBox(); self.lock_cb.setChecked(self.settings.get("net_locked",False))
        row("Lock Position (Disable Drag)", self.lock_cb)

        il.addStretch()
        scroll.setWidget(inner); vl.addWidget(scroll)

        foot = QWidget(); foot.setFixedHeight(58)
        foot.setStyleSheet(f"background:{bg2};border-top:1px solid {brd};")
        fl2  = QHBoxLayout(foot); fl2.setContentsMargins(20,0,20,0); fl2.setSpacing(8)
        apply_btn = QPushButton(self.lang["apply"])
        accent_fg = readable_on_rgb(ar, ag, ab)
        apply_btn.setStyleSheet(f"QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},220),stop:1 rgba({ar},{ag},{ab},160));color:{accent_fg};border:none;border-radius:8px;padding:10px 28px;font-weight:bold;font-size:13px;}}QPushButton:hover{{background:rgba({ar},{ag},{ab},255);}}")
        close_btn = QPushButton(self.lang["close"])
        close_btn.setStyleSheet(f"QPushButton{{background:{bg3};border:1px solid {brd};border-radius:8px;padding:10px 20px;color:{txt2};font-size:12px;}}")
        apply_btn.clicked.connect(self._apply); close_btn.clicked.connect(self.accept)
        fl2.addStretch(); fl2.addWidget(apply_btn); fl2.addWidget(close_btn); vl.addWidget(foot)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self,"Select Folder",self.folder_in.text())
        if d: self.folder_in.setText(d)

    def _collect(self):
        tc = self.thread_cb.currentText()
        self.settings["language"]        = self.lang_cb.currentText()
        self.settings["theme"]           = self.theme_cb.currentText()
        self.settings["mode"]            = self.mode_cb.currentText()
        self.settings["opacity"]         = self.app_op.value()
        self.settings["startup"]         = self.startup_cb.isChecked()
        self.settings["notification"]    = self.notif_cb.isChecked()
        self.settings["download_folder"] = self.folder_in.text()
        self.settings["threads"]         = "auto" if tc == self.lang["auto"] else tc
        self.settings["max_concurrent"]  = self.conc_sb.value()
        self.settings["net_monitor"]     = self.net_cb.isChecked()
        self.settings["net_opacity"]     = self.net_op.value()
        self.settings["net_pin_top"]     = self.pin_cb.isChecked()
        self.settings["net_dock_taskbar"] = self.dock_cb.isChecked()
        self.settings["net_locked"]      = self.lock_cb.isChecked()
        try:    self.settings["ext_port"] = int(self.port_in.text())
        except: pass

    def _dock_toggled(self, checked):
        self.settings["net_dock_taskbar"] = checked
        self.net_monitor.settings["net_dock_taskbar"] = checked
        if checked:
            self.net_monitor._dock_to_taskbar()

    def _apply(self):
        self._collect()
        set_startup(self.settings["startup"])
        save_settings(self.settings)
        self.apply_signal.emit(self.settings)

# ══════════════════════════════════════════════════════════════════
#  ABOUT DIALOG
# ══════════════════════════════════════════════════════════════════
class AboutDlg(QDialog):
    def __init__(self, lang, theme, mode, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setWindowTitle(lang["about_title"])
        self.setFixedSize(400, 400)
        self.setModal(True)
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = build_style(theme, mode)
        self.setStyleSheet(global_style(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win))
        vl = QVBoxLayout(self); vl.setContentsMargins(28,22,28,18); vl.setSpacing(8)

        logo = QLabel("RapidGet")
        logo.setStyleSheet(f"color:rgba({ar},{ag},{ab},220);font-size:26px;font-weight:bold;letter-spacing:3px;")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter); vl.addWidget(logo)
        vl.addWidget(QLabel(f"Version {APP_VERSION}  ·  Open Source",
            styleSheet=f"color:{txt3};font-size:12px;",
            alignment=Qt.AlignmentFlag.AlignCenter))
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"border:none;border-top:1px solid rgba({ar},{ag},{ab},50);")
        vl.addWidget(line)
        vl.addWidget(QLabel("Stonepit Labs",
            styleSheet=f"color:rgba({ar},{ag},{ab},200);font-size:15px;font-weight:bold;",
            alignment=Qt.AlignmentFlag.AlignCenter))
        vl.addWidget(QLabel("Developer: Tanjir Ahmed",
            styleSheet=f"color:{txt2};font-size:13px;",
            alignment=Qt.AlignmentFlag.AlignCenter))
        vl.addSpacing(6)
        for lbl, url in [
            ("GitHub",    "https://github.com/tanjir49"),
            ("Facebook",  "https://web.facebook.com/tanjir49"),
            ("Spotify",   "https://open.spotify.com/artist/5fmvVyTddXsJR6Rouf79vR"),
            ("Instagram", "https://www.instagram.com/tanjir49/"),
        ]:
            b = QPushButton(lbl)
            b.setStyleSheet(f"QPushButton{{background:{bg2};border:1px solid {brd};border-radius:8px;padding:10px;color:{txt2};font-size:13px;}}QPushButton:hover{{border-color:rgba({ar},{ag},{ab},120);color:rgba({ar},{ag},{ab},220);}}")
            b.clicked.connect(lambda _, u=url: webbrowser.open(u))
            vl.addWidget(b)
        vl.addWidget(QLabel("© 2026 Stonepit Labs  ·  Open Source Software",
            styleSheet=f"color:{txt3};font-size:10px;",
            alignment=Qt.AlignmentFlag.AlignCenter))
        cb = QPushButton(lang["close"])
        accent_fg = readable_on_rgb(ar, ag, ab)
        cb.setStyleSheet(f"QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},220),stop:1 rgba({ar},{ag},{ab},160));color:{accent_fg};border:none;border-radius:8px;padding:10px;font-weight:bold;}}QPushButton:hover{{background:rgba({ar},{ag},{ab},255);}}")
        cb.clicked.connect(self.accept); vl.addWidget(cb)

# ══════════════════════════════════════════════════════════════════
#  BROWSER EXTENSION SERVER  (WebSocket + legacy HTTP)
# ══════════════════════════════════════════════════════════════════
class ExtServer(QThread):
    url_received = pyqtSignal(str)

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.setDaemon = True

    def run(self):
        # Auto-find a free port if the configured one is busy
        port = self._find_free_port(self.port)
        self.port = port
        # Try WebSocket first; fall back to plain HTTP
        if HAS_WS:
            try:
                asyncio.run(self._ws_server())
            except Exception:
                self._http_server()
        else:
            self._http_server()

    def _find_free_port(self, start_port):
        """Try start_port first, then scan upward for a free one."""
        for port in range(start_port, start_port + 20):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", port))
                s.close()
                return port
            except OSError:
                continue
        return start_port  # fallback, will fail gracefully

    async def _ws_server(self):
        async def handler(ws):
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    url  = data.get("url","")
                    if url:
                        self.url_received.emit(url)
                    await ws.send(json.dumps({"status":"ok"}))
                except Exception:
                    pass

        try:
            async with websockets.serve(handler, "127.0.0.1", self.port,
                                        origins=None,
                                        ping_interval=None):
                await asyncio.Future()   # run forever
        except OSError:
            # Port still busy — silently skip (extension won't work but app runs)
            pass

    def _http_server(self):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))))
                url  = data.get("url","")
                if url: outer.url_received.emit(url)
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            def do_OPTIONS(self):
                self.send_response(200)
                for h,v in [("Access-Control-Allow-Origin","*"),
                            ("Access-Control-Allow-Methods","POST,OPTIONS"),
                            ("Access-Control-Allow-Headers","Content-Type")]:
                    self.send_header(h,v)
                self.end_headers()
            def log_message(self, *a): pass

        try:
            srv = HTTPServer(("127.0.0.1", self.port), H)
            srv.serve_forever()
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════
#  MAIN WINDOW
# ══════════════════════════════════════════════════════════════════
COL_FILE, COL_TYPE, COL_SIZE, COL_PROG, COL_SPEED, COL_ETA, COL_STATUS, COL_SEEDS = range(8)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.S = load_settings()
        self.L = LANG.get(self.S.get("language","English"), LANG["English"])
        _tn, self.T = resolve_theme(self.S.get("theme","Cyan"))
        _mn, self.M = resolve_mode(self.S.get("mode","Dark"))
        self.S["theme"], self.S["mode"] = _tn, _mn

        self.downloads : list[DownloadItem] = []
        self.workers   : dict[int, QThread] = {}
        self.last_bytes: dict[int, int]     = {}
        self.speed_hist: dict[int, deque]   = {}
        self._drag      = None
        self._maximized = False

        # Speed meter
        self.net_mon = SpeedMeter(self.S, self.T["accent"])
        if self.S.get("net_monitor", False): self.net_mon.show()

        self.setWindowTitle("RapidGet")
        self.setMinimumSize(980, 580)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)

        # Icon — safe on macOS too
        if os.path.exists(ICO_PATH):
            self.setWindowIcon(QIcon(ICO_PATH))

        self._load_state()
        self._build()
        self._tray()

        # Extension server
        self._ext = ExtServer(self.S.get("ext_port", 49152))
        self._ext.url_received.connect(self.add_from_ext)
        self._ext.start()

        self.setWindowOpacity(self.S.get("opacity",100)/100)

        # Queue management: process queue every 2 s
        self._queue_timer = QTimer()
        self._queue_timer.timeout.connect(self._process_queue)
        self._queue_timer.start(2000)

        # Check for .torrent / magnet in argv (file association)
        for arg in sys.argv[1:]:
            if arg.startswith("magnet:") or arg.endswith(".torrent"):
                QTimer.singleShot(500, lambda a=arg: self._create(a))

    # ── helpers ──────────────────────────────────────────────────
    def _vals(self):
        return build_style(self.T, self.M)

    def _L(self, key):
        return self.L.get(key, key)

    def paintEvent(self, event):
        """Draw shattered glass crack lines over the window when cracked theme active."""
        super().paintEvent(event)

        # ── Frosted Glass — Windows 11 Acrylic overlay ────────────
        if getattr(self, '_frosted_theme', False):
            a,a2,ar,ag,ab,*_ = self._vals()
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            W, H = self.width(), self.height()

            # 1. Acrylic luminosity layer — soft diagonal sky-blue tint
            g = QLinearGradient(0, 0, W, H)
            g.setColorAt(0.0, QColor(ar, ag, ab, 28))
            g.setColorAt(0.45, QColor(255, 255, 255, 16))
            g.setColorAt(1.0, QColor(ar, ag, ab, 22))
            p.fillRect(0, 0, W, H, g)

            # 2. Top sheen — the lit upper edge of an acrylic pane
            top = QLinearGradient(0, 0, 0, 120)
            top.setColorAt(0.0, QColor(255, 255, 255, 40))
            top.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillRect(0, 0, W, 120, top)

            # 3. Faint acrylic noise streaks (very subtle, evokes blur grain)
            streak = QPen(QColor(255, 255, 255, 9), 1.0)
            p.setPen(streak)
            step = 7
            for y in range(0, H, step):
                p.drawLine(0, y, W, y)

            # 4. Soft inner vignette toward the bottom for depth
            vg = QLinearGradient(0, H - 160, 0, H)
            vg.setColorAt(0.0, QColor(0, 0, 0, 0))
            vg.setColorAt(1.0, QColor(0, 0, 0, 28))
            p.fillRect(0, H - 160, W, 160, vg)

            p.end()
            return

        if not getattr(self, '_cracked_theme', False):
            return
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)  # crisp, sharp cracks
        W, H = self.width(), self.height()

        # Crack line segments - a radiating fracture pattern from top-right area
        # Each tuple: list of (x_frac, y_frac) points as fractions of W, H
        crack_patterns = [
            # Main crack from upper-right, branching downward
            [(0.72, 0.0), (0.68, 0.12), (0.61, 0.23), (0.55, 0.38), (0.48, 0.55), (0.42, 0.72)],
            # Branch 1 from main crack
            [(0.61, 0.23), (0.70, 0.31), (0.80, 0.34)],
            # Branch 2
            [(0.55, 0.38), (0.63, 0.44), (0.74, 0.48), (0.88, 0.45)],
            # Branch 3 going lower
            [(0.48, 0.55), (0.58, 0.62), (0.67, 0.75), (0.72, 0.90)],
            # Small crack top-left corner
            [(0.0, 0.06), (0.08, 0.14), (0.14, 0.24), (0.10, 0.36)],
            [(0.08, 0.14), (0.18, 0.18)],
            # Hairline crack bottom
            [(0.25, 1.0), (0.32, 0.88), (0.38, 0.80)],
        ]

        # Glowing bright crack line (white-ice shimmer)
        glow_pen = QPen(QColor(ar, ag, ab, 60), 2.5)
        glow_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        bright_pen = QPen(QColor(min(ar+60,255), min(ag+60,255), min(ab+40,255), 120), 1.0)
        bright_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        hairline_pen = QPen(QColor(ar, ag, ab, 45), 0.8)

        for pattern in crack_patterns:
            pts = [(int(x*W), int(y*H)) for x,y in pattern]
            # Glow pass (thicker, dimmer)
            p.setPen(glow_pen)
            for i in range(len(pts)-1):
                p.drawLine(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])
            # Bright core
            p.setPen(bright_pen)
            for i in range(len(pts)-1):
                p.drawLine(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])
            # Hairline shadow offset (+1,+1)
            p.setPen(hairline_pen)
            for i in range(len(pts)-1):
                p.drawLine(pts[i][0]+1, pts[i][1]+1, pts[i+1][0]+1, pts[i+1][1]+1)

        p.end()

    # ── UI BUILD ─────────────────────────────────────────────────
    def _build(self):
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
        self.setStyleSheet(global_style(a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win))
        self._cracked_theme = cracked  # stored for paintEvent
        self._frosted_theme = frosted  # stored for paintEvent

        root = QWidget(); root.setObjectName("root"); self.setCentralWidget(root)
        if mac:
            root.setStyleSheet(f"QWidget#root{{background:{bg};border-radius:12px;border:1px solid rgba(0,0,0,30);}}")
        elif win:
            root.setStyleSheet(f"QWidget#root{{background:{bg};border-radius:8px;border:1px solid {brd};}}")
        elif frosted:
            root.setStyleSheet(
                f"QWidget#root{{background:{bg};"
                f"border-radius:14px;"
                f"border:1px solid rgba(255,255,255,40);"
                f"border-top:1px solid rgba(255,255,255,70);}}"
            )
        elif cracked:
            root.setStyleSheet(
                f"QWidget#root{{background:{bg};"
                f"border-radius:2px;"
                f"border:2px solid rgba({ar},{ag},{ab},200);"
                f"border-top:3px solid rgba({ar},{ag},{ab},255);"
                f"border-left:1px solid rgba({ar},{ag},{ab},90);}}"
            )
        else:
            root.setStyleSheet(f"QWidget#root{{background:{bg};border-radius:12px;border:1px solid rgba({ar},{ag},{ab},50);}}")
        main = QVBoxLayout(root); main.setContentsMargins(0,0,0,0); main.setSpacing(0)

        # ── Title bar ────────────────────────────────────────────
        tb = QWidget(); tb.setObjectName("tb"); tb.setFixedHeight(48)
        if mac:
            tb.setStyleSheet(
                f"QWidget#tb{{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                f"stop:0 {bg3},stop:1 {bg2});"
                f"border-bottom:1px solid {brd};"
                f"border-radius:12px 12px 0 0;}}"
            )
        elif win:
            tb.setStyleSheet(
                f"QWidget#tb{{background:{bg2};"
                f"border-bottom:1px solid {brd};"
                f"border-radius:8px 8px 0 0;}}"
            )
        elif cracked:
            tb.setStyleSheet(
                f"QWidget#tb{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
                f"stop:0 rgba({ar},{ag},{ab},30),stop:0.4 rgba({ar},{ag},{ab},12),stop:1 rgba({ar},{ag},{ab},20));"
                f"border-bottom:2px solid rgba({ar},{ag},{ab},180);"
                f"border-top:1px solid rgba(255,255,255,40);"
                f"border-radius:2px 2px 0 0;}}"
            )
        elif frosted:
            tb.setStyleSheet(
                f"QWidget#tb{{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                f"stop:0 rgba(255,255,255,30),stop:1 rgba(255,255,255,12));"
                f"border-bottom:1px solid rgba(255,255,255,35);"
                f"border-top:1px solid rgba(255,255,255,60);"
                f"border-radius:14px 14px 0 0;}}"
            )
        else:
            tb.setStyleSheet(f"QWidget#tb{{background:rgba({ar},{ag},{ab},18);border-bottom:1px solid rgba({ar},{ag},{ab},40);border-radius:12px 12px 0 0;}}")
        tl = QHBoxLayout(tb); tl.setContentsMargins(14,0,8,0); tl.setSpacing(4)

        logo_lbl = QLabel()
        if os.path.exists(ICO_PATH):
            px = QPixmap(ICO_PATH).scaled(24, 24,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            logo_lbl.setPixmap(px)
        else:
            logo_lbl.setText("⚡")
            logo_lbl.setStyleSheet(f"color:rgba({ar},{ag},{ab},220);font-size:18px;background:transparent;")
        logo_lbl.setFixedSize(28, 28)
        logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_lbl.setStyleSheet("background:transparent;")

        logo = QLabel("RapidGet")
        logo.setStyleSheet(f"color:rgba({ar},{ag},{ab},220);font-size:16px;font-weight:bold;letter-spacing:2px;background:transparent;")
        sub  = QLabel("  Direct · Video · Torrent · Queue")
        sub.setStyleSheet(f"color:rgba({ar},{ag},{ab},50);font-size:11px;background:transparent;")
        tl.addWidget(logo_lbl); tl.addWidget(logo); tl.addWidget(sub); tl.addStretch()

        ctrl_s = """
            QPushButton{{background:{bg};border:none;border-radius:14px;color:{fg};
                font-size:16px;font-family:'Segoe MDL2 Assets','Segoe UI Symbol',Arial;
                min-width:28px;max-width:28px;min-height:28px;max-height:28px;padding:0;margin:2px;}}
            QPushButton:hover{{background:{hbg};}}"""
        for icon, ibg, ihbg, ifg, fn in [
            ("AI","transparent",f"rgba({ar},{ag},{ab},35)",f"rgba({ar},{ag},{ab},240)", self._stonepit),
            ("\uE713","transparent","rgba(120,120,120,60)",txt2,           self._settings),
            ("\uE946","transparent","rgba(120,120,120,60)",txt2,           self._about),
            ("\uE921","transparent","rgba(255,215,0,40)","#ffd93d",        self.showMinimized),
            ("\uE922","transparent",f"rgba({ar},{ag},{ab},40)",f"rgba({ar},{ag},{ab},255)", self._toggle_max),
            ("\uE8BB","transparent","rgba(200,50,50,80)","#ff6b6b",        self._quit),
        ]:
            b = QPushButton(icon)
            b.setStyleSheet(ctrl_s.format(bg=ibg,hbg=ihbg,fg=ifg))
            b.clicked.connect(fn); tl.addWidget(b)

        tb.mousePressEvent   = lambda e: setattr(self,"_drag",e.globalPosition().toPoint()-self.frameGeometry().topLeft()) if e.button()==Qt.MouseButton.LeftButton else None
        tb.mouseMoveEvent    = lambda e: self.move(e.globalPosition().toPoint()-self._drag) if self._drag and e.buttons()==Qt.MouseButton.LeftButton and not self._maximized else None
        tb.mouseReleaseEvent = lambda e: setattr(self,"_drag",None)
        tb.mouseDoubleClickEvent = lambda e: self._toggle_max()
        main.addWidget(tb)

        cw = QWidget(); cl = QVBoxLayout(cw); cl.setContentsMargins(12,10,12,8); cl.setSpacing(8)

        # ── URL input row ────────────────────────────────────────
        ul = QHBoxLayout(); ul.setSpacing(8)
        self.url_in = QLineEdit(); self.url_in.setPlaceholderText(self._L("url_placeholder"))
        self.url_in.returnPressed.connect(self._add)
        add_b = QPushButton(self._L("add")); add_b.setFixedSize(110,38)
        accent_fg = readable_on_rgb(ar, ag, ab)
        add_b.setStyleSheet(f"QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},220),stop:1 rgba({ar},{ag},{ab},160));border:none;border-radius:8px;color:{accent_fg};font-weight:bold;font-size:13px;}}QPushButton:hover{{background:rgba({ar},{ag},{ab},255);}}")
        add_b.clicked.connect(self._add)
        torrent_b = QPushButton("🧲 .torrent"); torrent_b.setFixedSize(100,38)
        torrent_b.setStyleSheet(f"QPushButton{{background:rgba({ar},{ag},{ab},18);border:1px solid rgba({ar},{ag},{ab},60);border-radius:8px;color:rgba({ar},{ag},{ab},200);font-size:12px;}}QPushButton:hover{{background:rgba({ar},{ag},{ab},35);border-color:rgba({ar},{ag},{ab},140);}}")
        torrent_b.clicked.connect(self._browse_torrent)
        # Drop .torrent files on URL bar
        self.url_in.setAcceptDrops(True)
        self.url_in.dragEnterEvent = lambda e: e.acceptProposedAction() if e.mimeData().hasUrls() or e.mimeData().hasText() else None
        self.url_in.dropEvent      = lambda e: self._handle_drop(e)
        ul.addWidget(self.url_in); ul.addWidget(add_b); ul.addWidget(torrent_b); cl.addLayout(ul)

        # ── Download table ───────────────────────────────────────
        self.table = QTableWidget(0, 7)
        self.table.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            self._L("file"), self._L("type"), self._L("size"),
            self._L("progress"), self._L("speed"), self._L("eta"),
            self._L("status"), "Seeds/Peers"])
        hh = self.table.horizontalHeader()
        hh.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        hh.setSectionsMovable(False)
        for col in range(8):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 270)
        self.table.setColumnWidth(1, 70); self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3,160); self.table.setColumnWidth(4,110)
        self.table.setColumnWidth(5, 70); self.table.setColumnWidth(6,115)
        self.table.setColumnWidth(7, 90)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setShowGrid(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._ctx)
        cl.addWidget(self.table)

        ai_row = QHBoxLayout(); ai_row.setSpacing(6)
        self.ai_pending = None
        self.ai_in = QLineEdit()
        self.ai_in.setPlaceholderText(self._L("stonepit_placeholder"))
        self.ai_in.returnPressed.connect(self._ai_ask)
        self.ai_send = QPushButton("AI")
        self.ai_send.setFixedSize(52, 34)
        self.ai_send.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ai_send.setAutoDefault(False)
        self.ai_send.setDefault(False)
        self.ai_send.clicked.connect(self._ai_ask)
        self.ai_approve = QPushButton(self._L("stonepit_approve"))
        self.ai_approve.setFixedSize(150, 34)
        self.ai_approve.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ai_approve.setAutoDefault(False)
        self.ai_approve.setDefault(False)
        self.ai_approve.setEnabled(False)
        self.ai_approve.clicked.connect(self._ai_approve)
        self.ai_send.setStyleSheet(f"QPushButton{{background:rgba({ar},{ag},{ab},22);border:1px solid rgba({ar},{ag},{ab},85);border-radius:7px;color:rgba({ar},{ag},{ab},230);font-weight:bold;}}QPushButton:hover{{background:rgba({ar},{ag},{ab},38);}}")
        self.ai_approve.setStyleSheet(f"QPushButton{{background:rgba({ar},{ag},{ab},26);border:1px solid rgba({ar},{ag},{ab},80);border-radius:7px;color:{txt2};font-size:12px;}}QPushButton:disabled{{background:{bg3};border:1px solid {brd};color:{txt3};}}QPushButton:hover{{background:rgba({ar},{ag},{ab},40);}}")
        ai_row.addWidget(self.ai_in)
        ai_row.addWidget(self.ai_send)
        ai_row.addWidget(self.ai_approve)
        cl.addLayout(ai_row)

        # ── Bottom button bar ────────────────────────────────────
        bl = QHBoxLayout(); bl.setSpacing(6)
        for lbl_key, fn, bc, bgc in [
            ("resume",  self._resume,    "rgba(0,184,148,200)","rgba(0,184,148,25)"),
            ("pause",   self._pause_sel, "rgba(253,203,110,200)","rgba(253,203,110,25)"),
            ("remove",  self._remove,    "rgba(225,112,85,200)","rgba(225,112,85,25)"),
            ("move_up", self._move_up,   f"rgba({ar},{ag},{ab},100)",f"rgba({ar},{ag},{ab},12)"),
            ("move_down",self._move_down,f"rgba({ar},{ag},{ab},100)",f"rgba({ar},{ag},{ab},12)"),
            ("folder",  self._folder,    f"rgba({ar},{ag},{ab},100)",f"rgba({ar},{ag},{ab},12)"),
            ("open",    self._open_f,    f"rgba({ar},{ag},{ab},100)",f"rgba({ar},{ag},{ab},12)"),
        ]:
            b = QPushButton(self._L(lbl_key)); b.clicked.connect(fn)
            b.setStyleSheet(f"QPushButton{{background:{bgc};border:1px solid {bc};border-radius:7px;padding:7px 11px;color:{txt2};font-size:12px;}}QPushButton:hover{{background:rgba({ar},{ag},{ab},25);border-color:rgba({ar},{ag},{ab},80);color:rgba({ar},{ag},{ab},220);}}")
            bl.addWidget(b)
        bl.addStretch(); cl.addLayout(bl)

        # ── Status bar ───────────────────────────────────────────
        self.stat = QLabel(f"⚡  {self._L('ready')}")
        self.stat.setStyleSheet(f"color:rgba({ar},{ag},{ab},65);font-size:11px;padding:1px 3px;background:transparent;")
        cl.addWidget(self.stat)

        main.addWidget(cw)
        self.setStatusBar(QStatusBar())

        # Re-populate table rows
        for item in self.downloads:
            self._row(item)

        # 1-second tick timer
        t = QTimer(); t.timeout.connect(self._tick); t.start(1000); self._timer = t

    # ── TRAY ─────────────────────────────────────────────────────
    def _tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self._load_tray_icon())
        self.tray.setToolTip("⚡ RapidGet")
        m = QMenu()
        m.addAction(self._L("show"),     self._show)
        m.addAction(self._L("settings"), self._settings)
        m.addAction(self._L("about"),    self._about)
        m.addSeparator()
        m.addAction(self._L("exit"),     self._quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self._show()
            if r == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    @staticmethod
    def _load_tray_icon():
        """Load rapidget.ico preserving alpha on Windows & macOS.
        Tries multiple Qt strategies to ensure transparency is kept."""
        icon = QIcon()
        for fname, size in [("icon16.png", 16), ("icon32.png", 32), ("icon48.png", 48), ("icon128.png", 128)]:
            p = os.path.join(APP_DIR, fname)
            if os.path.exists(p):
                icon.addFile(p, QSize(size, size))
        if not icon.isNull():
            return icon
        if os.path.exists(ICO_PATH):
            # Strategy 1: QIcon direct (works on most platforms)
            icon = QIcon(ICO_PATH)
            if not icon.isNull():
                px = icon.pixmap(32, 32)
                if not px.isNull():
                    return icon
            # Strategy 2: Force through QPixmap (fixes Windows alpha decode)
            px = QPixmap(ICO_PATH)
            if not px.isNull():
                return QIcon(px)
        # ICO missing — transparent fallback (no solid box)
        px = QPixmap(32, 32)
        px.fill(Qt.GlobalColor.transparent)
        return QIcon(px)

    def _show(self): self.show(); self.raise_(); self.activateWindow()

    def _toggle_max(self):
        if self._maximized: self.showNormal(); self._maximized = False
        else:               self.showMaximized(); self._maximized = True

    def _stonepit(self):
        self._show()
        if hasattr(self, "ai_in"):
            self.ai_in.setFocus(Qt.FocusReason.OtherFocusReason)
            self.ai_in.setCursorPosition(len(self.ai_in.text()))

    def _ai_note(self, text):
        clean = re.sub(r"<[^>]+>", " ", str(text))
        clean = re.sub(r"\s+", " ", clean).strip()
        self.stat.setText(f"AI: {clean}")

    def _ai_ask(self):
        cmd = self.ai_in.text().strip()
        if not cmd:
            self._ai_note(self._L("stonepit_empty"))
            self.ai_in.setFocus()
            return
        self.ai_in.clear()
        self.ai_pending = None
        self.ai_approve.setEnabled(False)
        self._ai_note(f"You: {cmd}")
        if self._ai_handle_settings(cmd):
            self.ai_in.setFocus()
            return
        self.ai_worker = StonepitWorker(cmd, self.L)
        self.ai_worker.message.connect(self._ai_note)
        self.ai_worker.proposal.connect(self._ai_proposal)
        self.ai_worker.start()
        self.ai_in.setFocus()

    def _ai_proposal(self, data):
        self.ai_pending = data
        size = fmt_bytes(data.get("size", 0)) if data.get("size", 0) else "Unknown"
        self._ai_note(f"{self._L('stonepit_found')}: {data.get('title','download')} | {self._L('stonepit_site_size')}: {size}. {self._L('stonepit_approve_hint')}")
        self.ai_approve.setEnabled(True)
        self.ai_in.setFocus()

    def _ai_approve(self):
        if not self.ai_pending:
            return
        url = self.ai_pending.get("url", "")
        self._ai_note(self._L("stonepit_approved"))
        self.ai_pending = None
        self.ai_approve.setEnabled(False)
        self._create(url)
        self.ai_in.setFocus()

    def _ai_handle_settings(self, cmd):
        low = (cmd or "").casefold()
        wants_settings = any(w in low for w in (
            "setting", "settings", "set ", "change", "make ", "koro", "kore dao",
            "language", "lang", "bhasha", "theme", "mode", "dark", "light",
            "opacity", "folder", "threads", "notification", "startup", "monitor",
            "speed", "port", "concurrent", "সেটিং", "ভাষা", "থিম", "মোড", "ডার্ক", "লাইট",
        ))
        if not wants_settings:
            return False
        s = dict(self.S)
        changed = []

        def set_value(key, value, label):
            if s.get(key) != value:
                s[key] = value
                changed.append(f"{label}: {value}")

        tokens = set(re.findall(r"[a-z0-9]+", low))
        bool_val = None
        if any(w in tokens for w in ("off", "disable", "false", "no", "bondho", "stop")) or "বন্ধ" in low:
            bool_val = False
        elif any(w in tokens for w in ("on", "enable", "true", "yes", "chalu", "start")) or "চালু" in low:
            bool_val = True

        lang_aliases = {
            "english": "English", "en": "English",
            "bangla": "বাংলা", "bengali": "বাংলা", "bn": "বাংলা",
            "hindi": "Hindi", "hi": "Hindi",
            "portuguese": "Portuguese", "portugues": "Portuguese", "pt": "Portuguese",
            "cantonese": "Cantonese", "chinese": "Cantonese", "zh": "Cantonese",
        }
        if any(w in low for w in ("language", "lang", "bhasha", "ভাষা")):
            for alias, name in lang_aliases.items():
                if alias in low and name in LANG:
                    set_value("language", name, "Language")
                    break
            else:
                for name in LANG:
                    if str(name).casefold() in low:
                        set_value("language", name, "Language")
                        break

        if "theme" in low or "থিম" in low:
            for name in THEMES:
                if str(name).casefold() in low:
                    set_value("theme", name, "Theme")
                    break
        if "dark" in low or "ডার্ক" in low:
            set_value("mode", "Dark", "Mode")
        elif "light" in low or "লাইট" in low:
            set_value("mode", "Light", "Mode")
        elif "os default" in low or "system" in low:
            set_value("mode", "OS Default", "Mode")

        m = re.search(r"(?:opacity|transparent|transparency|alpha)\D*(\d{1,3})", low)
        if m:
            set_value("opacity", max(30, min(100, int(m.group(1)))), "Opacity")
        m = re.search(r"(?:concurrent|max concurrent|max download)\D*(\d{1,2})", low)
        if m:
            set_value("max_concurrent", max(1, min(10, int(m.group(1)))), "Max concurrent")
        m = re.search(r"(?:port|extension port|ext port)\D*(\d{2,5})", low)
        if m:
            set_value("ext_port", max(1, min(65535, int(m.group(1)))), "Extension port")
        m = re.search(r"(?:threads?|thread count)\D*(auto|\d{1,2})", low)
        if m:
            value = m.group(1)
            set_value("threads", "auto" if value == "auto" else max(1, min(32, int(value))), "Threads")

        folder_match = re.search(r"(?:download folder|folder|save folder|path)\s+(.+)$", cmd, re.I)
        if folder_match:
            folder = folder_match.group(1).strip().strip("\"'")
            if folder:
                try:
                    Path(folder).mkdir(parents=True, exist_ok=True)
                    set_value("download_folder", folder, "Download folder")
                except Exception as e:
                    self._ai_note(f"Could not set folder: {e}")
                    return True

        if bool_val is not None:
            if "startup" in low or "start with windows" in low:
                set_value("startup", bool_val, "Startup")
                try:
                    set_startup(bool_val)
                except Exception:
                    pass
            if "notification" in low or "notify" in low:
                set_value("notification", bool_val, "Notification")
            if "speed" in low or "net monitor" in low or "monitor" in low:
                set_value("net_monitor", bool_val, "Speed monitor")
            if "pin" in low or "top" in low:
                set_value("net_pin_top", bool_val, "Always on top")
            if "dock" in low or "taskbar" in low:
                set_value("net_dock_taskbar", bool_val, "Dock to taskbar")
            if "lock" in low:
                set_value("net_locked", bool_val, "Monitor lock")

        if not changed:
            if "setting" in low or "settings" in low:
                self._ai_note("Example: language Bangla, theme Cyan, dark mode, opacity 90, notification off.")
                return True
            return False

        save_settings(s)
        self._apply_theme_live(dict(s))
        self._ai_note("Settings updated: " + ", ".join(changed))
        return True

    def _settings(self):
        dlg = SettingsDlg(self.S, self.L, self.T, self.M, self.net_mon, self)
        dlg.apply_signal.connect(self._apply_theme_live)
        dlg.exec()

    def _about(self): AboutDlg(self.L, self.T, self.M, self).exec()

    def _quit(self):
        self._save_state()
        for w in self.workers.values(): w.stop()
        QApplication.quit()

    # ── THEME LIVE APPLY ─────────────────────────────────────────
    def _apply_theme_live(self, new_settings):
        self.S = new_settings
        self.L = LANG.get(self.S.get("language","English"), LANG["English"])
        _tn, self.T = resolve_theme(self.S.get("theme","Cyan"))
        _mn, self.M = resolve_mode(self.S.get("mode","Dark"))
        self.S["theme"], self.S["mode"] = _tn, _mn
        self._build()
        self.setWindowOpacity(self.S.get("opacity",100)/100)
        if self.S.get("net_monitor",False): self.net_mon.show()
        else: self.net_mon.hide()
        self.net_mon.settings = self.S
        self.net_mon.setWindowOpacity(self.S.get("net_opacity",90)/100)
        self.net_mon._dock_to_taskbar()
        # Always restore the real icon — never overwrite with a colour box
        self.tray.setIcon(self._load_tray_icon())

    # ── URL DETECTION ─────────────────────────────────────────────
    def _detect(self, url):
        if url.startswith("magnet:"): return "torrent"
        if url.lower().endswith(".torrent"): return "torrent"
        if any(s in url for s in YT_SITES): return "yt"
        return "direct"

    def _badge(self, t):
        return {
            "direct":  self._L("direct"),
            "yt":      self._L("video"),
            "torrent": self._L("torrent"),
        }.get(t, t)

    # ── TABLE ROW ────────────────────────────────────────────────
    def _make_progress_bar(self, item):
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(True)
        bar.setFixedHeight(28)
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar.setFormat("%p%")
        if item.size > 0:
            pct = max(0, min(100, int(item.downloaded / item.size * 100)))
            item._last_progress_pct = pct
            bar.setValue(pct)
        else:
            item._last_progress_pct = getattr(item, "_last_progress_pct", 0)
            bar.setValue(item._last_progress_pct)
        return bar

    def _set_progress_bar(self, row, dl, total):
        if row >= len(self.downloads) or total <= 0:
            return
        item = self.downloads[row]
        pct = max(0, min(100, int(dl / total * 100)))
        if item.status in ("downloading", "queued", "waiting"):
            pct = max(pct, getattr(item, "_last_progress_pct", 0))
        item._last_progress_pct = pct
        bar = self.table.cellWidget(row, COL_PROG)
        if bar:
            bar.setValue(pct)

    def _row(self, item):
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
        r = self.table.rowCount(); self.table.insertRow(r)
        self.table.setItem(r, COL_FILE,   QTableWidgetItem(item.filename))
        badge = QTableWidgetItem(self._badge(item.dl_type))
        badge.setForeground(QColor(self.T["accent"]))
        self.table.setItem(r, COL_TYPE, badge)
        self.table.setItem(r, COL_SIZE,   QTableWidgetItem(fmt_bytes(item.size)))
        self.table.setCellWidget(r, COL_PROG, self._make_progress_bar(item))
        self.table.setItem(r, COL_SPEED,  QTableWidgetItem("—"))
        self.table.setItem(r, COL_ETA,    QTableWidgetItem("—"))
        self.table.setItem(r, COL_SEEDS,  QTableWidgetItem("—"))
        st = QTableWidgetItem(item.status); st.setForeground(QColor(txt3))
        self.table.setItem(r, COL_STATUS, st)
        self.table.setRowHeight(r, 38)
        self.speed_hist[r] = deque([0]*10, maxlen=10)

    # ── ADD ───────────────────────────────────────────────────────
    def _add(self):
        url = self.url_in.text().strip()
        if not url: return
        self._create(url); self.url_in.clear()

    def _handle_drop(self, e):
        if e.mimeData().hasUrls():
            for u in e.mimeData().urls():
                path = u.toLocalFile()
                if path.endswith(".torrent"):
                    self._create(path); return
                self.url_in.setText(u.toString())
        elif e.mimeData().hasText():
            self.url_in.setText(e.mimeData().text())

    def _browse_torrent(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Torrent File", "",
            "Torrent Files (*.torrent);;All Files (*)")
        if path:
            self._create(path)

    @pyqtSlot(str)
    def add_from_ext(self, url):
        self._show(); self._create(url)
        if self.S.get("notification", True):
            self.tray.showMessage("⚡ RapidGet", "New download added!",
                QSystemTrayIcon.MessageIcon.Information, 3000)

    def _create(self, url):
        url, quality_hint = split_rapidget_quality(url)
        dl_type = self._detect(url)
        folder  = self._get_folder()

        if dl_type == "yt":
            edit_opts = {"clip_start": "", "clip_end": "", "crop_preset": "Original"}
            size_hint = 0
            if quality_hint:
                q = quality_hint
            else:
                dlg = QualityDlg(url, self.L, self.T, self.M, self)
                if dlg.exec() != QDialog.DialogCode.Accepted: return
                q = dlg.quality()
                size_hint = dlg.selected_size()
                edit_opts = dlg.edit_options()
            name = url.split("?")[0].split("/")[-1] or "video"
            item = DownloadItem(url, name, os.path.join(folder, name),
                                size=size_hint, dl_type="yt", quality=q, status="queued",
                                **edit_opts)
            self.downloads.append(item); self._row(item); self._save_state()

        elif dl_type == "torrent":
            if url.startswith("magnet:"):
                # ── Magnet: show spinner, fetch metadata async ───
                # Connect files_ready BEFORE exec() so it fires after exec returns
                spinner = MagnetLoadingDlg(url, folder, self.L, self.T, self.M, self)
                spinner.files_ready.connect(self._on_magnet_ready)
                spinner.exec()
                # _on_magnet_ready handles the rest after metadata arrives
            else:
                # ── .torrent file: parse ONCE, pass info to dialog ─
                _ti = None
                if HAS_LT:
                    try:
                        _ti = lt.torrent_info(str(url))
                    except Exception as e:
                        QMessageBox.critical(self, "RapidGet — Torrent Error",
                            f"Cannot read .torrent file:\n{e}")
                        return
                dlg = TorrentFileDlg(self.L, self.T, self.M, self,
                                     torrent_path=url, torrent_info=_ti)
                if dlg.exec() != QDialog.DialogCode.Accepted: return
                # Get torrent name from already-parsed info
                name = "torrent"
                if _ti:
                    try: name = _ti.name() or name
                    except Exception: pass
                elif url.lower().endswith(".torrent"):
                    try: name = parse_torrent_metadata(url)[0] or name
                    except Exception: pass
                if not name: name = os.path.basename(url) or "torrent"
                item = DownloadItem(url, name, os.path.join(folder, name),
                                    dl_type="torrent", status="queued")
                item._selected_files = dlg.selected
                self.downloads.append(item); self._row(item); self._save_state()

        else:
            name = url.split("/")[-1].split("?")[0] or "download"
            path, _ = QFileDialog.getSaveFileName(self, "Save As",
                                                   os.path.join(folder, name))
            if not path: return
            size = probe_url_size(url)
            item = DownloadItem(url, os.path.basename(path), path,
                                size=size, dl_type="direct", status="queued")
            self.downloads.append(item); self._row(item); self._save_state()

    def _on_magnet_ready(self, files, worker):
        """Called via QTimer.singleShot after spinner closes — show file selector."""
        url    = worker.magnet_url
        folder = self._get_folder()

        dlg = TorrentFileDlg(self.L, self.T, self.M, self,
                             files=files,
                             torrent_info=getattr(worker, "torrent_info", None))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            worker.stop(); return

        # Derive display name from torrent_info if available
        name = "torrent"
        if worker.torrent_info:
            try: name = worker.torrent_info.name()
            except Exception: pass
        if not name: name = "torrent"

        item = DownloadItem(url, name, os.path.join(folder, name),
                            dl_type="torrent", status="queued")
        item._selected_files  = dlg.selected
        item._metadata_worker = worker   # keeps session alive
        self.downloads.append(item); self._row(item); self._save_state()

    # ── QUEUE PROCESSOR ──────────────────────────────────────────
    def _process_queue(self):
        max_conc = self.S.get("max_concurrent", 3)
        active   = sum(1 for w in self.workers.values() if w.isRunning())
        if active >= max_conc: return
        for idx, item in enumerate(self.downloads):
            if item.status == "queued":
                self._start(idx)
                active += 1
                if active >= max_conc: break

    # ── START WORKER ─────────────────────────────────────────────
    def _start(self, row):
        item = self.downloads[row]
        item.status = "downloading"
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
        st = QTableWidgetItem(self._L("downloading"))
        st.setForeground(QColor(self.T["accent"]))
        self.table.setItem(row, COL_STATUS, st)

        if item.dl_type == "yt":
            w = YTWorker(row, item, self._get_folder())
            w.name_update.connect(self._upd_name)
        elif item.dl_type == "torrent":
            sel = getattr(item, "_selected_files", None)
            w   = TorrentWorker(row, item, self._get_folder(), selected_files=sel)
            w.name_update.connect(self._upd_name)
            w.status_text.connect(self._torrent_status)
            w.torrent_stat.connect(self._torrent_stat_update)
        else:
            w = DirectWorker(row, item, self.S.get("threads","auto"))

        w.progress.connect(self._prog)
        w.finished.connect(self._done)
        w.error.connect(self._err)
        self.workers[row]    = w
        self.last_bytes[row] = item.downloaded
        self.speed_hist[row] = self.speed_hist.get(row, deque([0]*10, maxlen=10))
        w.start()

    # ── PAUSE / RESUME / REMOVE ───────────────────────────────────
    def _pause_sel(self):
        for r in self._sel():
            item = self.downloads[r]
            if r in self.workers and hasattr(self.workers[r], "pause"):
                self.workers[r].pause()
            item.status = "paused"
            st = QTableWidgetItem(self._L("paused")); st.setForeground(QColor("#fdcb6e"))
            self.table.setItem(r, COL_STATUS, st)
        self._save_state()

    def _resume(self):
        for r in self._sel():
            item = self.downloads[r]
            if item.status in ("paused","error","waiting","queued"):
                if r in self.workers and self.workers[r].isRunning():
                    if hasattr(self.workers[r],"resume"):
                        self.workers[r].resume()
                    item.status = "downloading"
                    st = QTableWidgetItem(self._L("downloading"))
                    st.setForeground(QColor(self.T["accent"]))
                    self.table.setItem(r, COL_STATUS, st)
                else:
                    item.status = "queued"   # re-queue it
                    st = QTableWidgetItem(self._L("queued"))
                    self.table.setItem(r, COL_STATUS, st)

    def _remove(self):
        for r in sorted(self._sel(), reverse=True):
            if r in self.workers: self.workers[r].stop(); del self.workers[r]
            if r in self.speed_hist: del self.speed_hist[r]
            if r in self.last_bytes:  del self.last_bytes[r]
            self.downloads.pop(r); self.table.removeRow(r)
        self._save_state()

    # ── QUEUE ORDER ──────────────────────────────────────────────
    def _move_up(self):
        rows = sorted(self._sel())
        for r in rows:
            if r == 0: continue
            self.downloads[r], self.downloads[r-1] = self.downloads[r-1], self.downloads[r]
            self._swap_rows(r, r-1)

    def _move_down(self):
        rows = sorted(self._sel(), reverse=True)
        for r in rows:
            if r >= len(self.downloads)-1: continue
            self.downloads[r], self.downloads[r+1] = self.downloads[r+1], self.downloads[r]
            self._swap_rows(r, r+1)

    def _swap_rows(self, r1, r2):
        for col in range(self.table.columnCount()):
            i1 = self.table.takeItem(r1, col)
            i2 = self.table.takeItem(r2, col)
            if i1: self.table.setItem(r2, col, i1)
            if i2: self.table.setItem(r1, col, i2)
        w1 = self.table.cellWidget(r1, COL_PROG)
        w2 = self.table.cellWidget(r2, COL_PROG)
        if w1 or w2:
            d1 = self.downloads[r1]; d2 = self.downloads[r2]
            self.table.setCellWidget(r1, COL_PROG, self._make_progress_bar(d1))
            self.table.setCellWidget(r2, COL_PROG, self._make_progress_bar(d2))

    # ── FOLDER / FILE ─────────────────────────────────────────────
    def _get_folder(self):
        f = self.S.get("download_folder", get_default_dir())
        os.makedirs(f, exist_ok=True); return f

    def _folder(self): open_path(self._get_folder())
    def _open_f(self):
        for r in self._sel():
            open_path(self.downloads[r].save_path)

    # ── CONTEXT MENU ─────────────────────────────────────────────
    def _ctx(self, pos):
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
        m = QMenu()
        m.setStyleSheet(f"QMenu{{background:{bg};border:1px solid rgba({ar},{ag},{ab},60);border-radius:8px;color:{txt2};padding:4px;}}QMenu::item{{padding:8px 16px;border-radius:4px;}}QMenu::item:selected{{background:rgba({ar},{ag},{ab},30);}}")
        m.addAction(self._L("resume"),      self._resume)
        m.addAction(self._L("pause"),       self._pause_sel)
        m.addSeparator()
        m.addAction(self._L("move_up"),     self._move_up)
        m.addAction(self._L("move_down"),   self._move_down)
        m.addSeparator()
        m.addAction(self._L("open_folder"), self._folder)
        m.addAction(self._L("open_file"),   self._open_f)
        m.addAction(self._L("copy_url"),    self._copy_url)

        # Torrent-specific actions
        rows = self._sel()
        if rows and self.downloads[rows[0]].dl_type == "torrent":
            m.addSeparator()
            m.addAction("📋 Copy Magnet / Hash", self._copy_magnet)
            m.addAction("🔄 Re-check Files",     self._torrent_recheck)
            m.addAction("⬆ Force Resume (Seed)", self._torrent_force_resume)

        m.addSeparator()
        m.addAction(self._L("remove"),      self._remove)
        m.exec(self.table.mapToGlobal(pos))

    def _copy_magnet(self):
        for r in self._sel():
            item = self.downloads[r]
            if item.dl_type == "torrent":
                QApplication.clipboard().setText(item.url)
                break

    def _torrent_recheck(self):
        """Force hash re-check for selected torrent rows."""
        for r in self._sel():
            w = self.workers.get(r)
            if w and hasattr(w, "_handle") and w._handle:
                try: w._handle.force_recheck()
                except Exception: pass

    def _torrent_force_resume(self):
        """Force resume even if finished (continue seeding)."""
        for r in self._sel():
            item = self.downloads[r]
            if item.dl_type == "torrent":
                item.status = "queued"
                a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
                st = QTableWidgetItem("⬆ Resuming")
                st.setForeground(QColor(f"rgba({ar},{ag},{ab},200)"))
                self.table.setItem(r, COL_STATUS, st)
        self._process_queue()

    def _copy_url(self):
        for r in self._sel():
            QApplication.clipboard().setText(self.downloads[r].url)

    # ── SIGNALS ───────────────────────────────────────────────────
    def _prog(self, row, dl, total):
        if row >= len(self.downloads): return
        item = self.downloads[row]
        item.downloaded = dl; item.size = total
        self._set_progress_bar(row, dl, total)
        self.table.setItem(row, COL_SIZE, QTableWidgetItem(fmt_bytes(total)))

    def _done(self, row):
        if row >= len(self.downloads): return
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
        item = self.downloads[row]
        if item.dl_type == "torrent" and item.status == "seeding":
            st = QTableWidgetItem("🌱 Seeding")
            st.setForeground(QColor(f"rgba({ar},{ag},{ab},220)"))
        else:
            item.status = "done"
            st = QTableWidgetItem(self._L("done")); st.setForeground(QColor("#00b894"))
        self.table.setItem(row, COL_STATUS, st)
        bar = self.table.cellWidget(row, COL_PROG)
        if bar: bar.setValue(100)
        if row in self.workers and item.status != "seeding":
            QTimer.singleShot(0, lambda r=row: self.workers.pop(r, None))
        self._save_state()
        name = item.filename
        if self.S.get("notification", True):
            self.tray.showMessage("✅ RapidGet", f"{name} — done!",
                QSystemTrayIcon.MessageIcon.Information, 4000)

    def _err(self, row, msg):
        if row >= len(self.downloads): return
        st = QTableWidgetItem(self._L("error")); st.setForeground(QColor("#e17055"))
        self.table.setItem(row, COL_STATUS, st)
        self.downloads[row].status = "error"
        if row in self.workers:
            QTimer.singleShot(0, lambda r=row: self.workers.pop(r, None))

    def _upd_name(self, row, name):
        if row >= len(self.downloads): return
        self.downloads[row].filename = name
        self.table.setItem(row, COL_FILE, QTableWidgetItem(name))

    def _torrent_status(self, row, extra):
        if row >= len(self.downloads): return
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
        item = self.downloads[row]
        if item.status == "seeding":
            st = QTableWidgetItem("🌱 Seeding")
            st.setForeground(QColor(f"rgba({ar},{ag},{ab},220)"))
        else:
            st = QTableWidgetItem(extra)
            st.setForeground(QColor(txt2))
        self.table.setItem(row, COL_STATUS, st)

    def _torrent_stat_update(self, row, stat):
        """Update Speed, ETA, Seeds/Peers columns with torrent stats."""
        if row >= len(self.downloads): return
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
        dl  = stat.get("dl_rate", 0)
        ul  = stat.get("ul_rate", 0)
        s   = stat.get("seeds", 0)
        p   = stat.get("peers", 0)
        rat = stat.get("ratio", 0.0)
        item = self.downloads[row]
        acc = QColor(f"rgba({ar},{ag},{ab},200)")

        # Speed column: ↓ dl / ↑ ul
        spd_text = f"↓{fmt_bytes(dl)}/s  ↑{fmt_bytes(ul)}/s"
        spd_item = QTableWidgetItem(spd_text)
        spd_item.setForeground(acc)
        self.table.setItem(row, COL_SPEED, spd_item)

        # ETA column
        if item.status == "seeding":
            eta_item = QTableWidgetItem(f"R:{rat:.3f}")
        else:
            sz   = item.size
            done = item.downloaded
            if dl > 0 and sz > done:
                eta_item = QTableWidgetItem(fmt_eta((sz - done) / dl))
            else:
                eta_item = QTableWidgetItem("—")
        self.table.setItem(row, COL_ETA, eta_item)

        # Seeds / Peers column (qBittorrent-style: S(total) P(total))
        seeds_item = QTableWidgetItem(f"S:{s}  P:{p}")
        seeds_item.setForeground(QColor(txt3))
        self.table.setItem(row, COL_SEEDS, seeds_item)

    # ── 1-SECOND TICK ────────────────────────────────────────────
    def _tick(self):
        a,a2,ar,ag,ab,bg,bg2,bg3,txt,txt2,txt3,brd,cracked,frosted,mac,win = self._vals()
        active = 0; total_spd = 0

        for row, w in list(self.workers.items()):
            if not w.isRunning(): continue
            active += 1
            if row >= len(self.downloads): continue
            item = self.downloads[row]
            # Torrent rows manage their own speed/ETA via torrent_stat signal
            if item.dl_type == "torrent":
                total_spd += item.downloaded - self.last_bytes.get(row, item.downloaded)
                self.last_bytes[row] = item.downloaded
                continue
            cur  = item.downloaded
            spd  = max(0, cur - self.last_bytes.get(row, cur))
            self.last_bytes[row] = cur
            if row in self.speed_hist: self.speed_hist[row].append(spd)
            total_spd += spd
            self.table.setItem(row, COL_SPEED, QTableWidgetItem(fmt_bytes(spd)+"/s"))
            # ETA
            sz = item.size
            if spd > 0 and sz > cur:
                eta_s = (sz - cur) / spd
                self.table.setItem(row, COL_ETA, QTableWidgetItem(fmt_eta(eta_s)))
            else:
                self.table.setItem(row, COL_ETA, QTableWidgetItem("—"))

        if active:
            self.stat.setText(f"⬇ {active} {self._L('active')}  ·  {fmt_bytes(total_spd)}/s {self._L('total')}")
        else:
            self.stat.setText(f"⚡  {self._L('ready')}")

    # ── STATE PERSIST ────────────────────────────────────────────
    def _save_state(self):
        try:
            with open(SAVE_FILE,"w") as f:
                json.dump([d.to_dict() for d in self.downloads], f, indent=2)
        except Exception:
            pass

    def _load_state(self):
        if not os.path.exists(SAVE_FILE): return
        try:
            with open(SAVE_FILE) as f:
                data = json.load(f)
            for d in data:
                if d.get("status") == "downloading":
                    d["status"] = "paused"
                item = DownloadItem.from_dict(d)
                self.downloads.append(item)
        except Exception:
            pass

    def _sel(self):
        return list({i.row() for i in self.table.selectedItems()})

    def closeEvent(self, e):
        e.ignore(); self.hide()
        self.tray.showMessage("⚡ RapidGet", self._L("bg_msg"),
            QSystemTrayIcon.MessageIcon.Information, 3000)

# ══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)

    if os.path.exists(ICO_PATH):
        app.setWindowIcon(QIcon(ICO_PATH))

    win = MainWindow(); win.show()
    sys.exit(app.exec())
