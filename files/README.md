# ⚡ RapidGet Downloader v2.0.0
### by Stonepit Labs — Developer: Tanjir Ahmed

---

## 📦 Installation (একবারেই করুন)

### Python Dependencies
```bash
pip install PyQt6 requests yt-dlp psutil websockets
```

### Torrent support (optional but recommended)
```bash
# Windows / Linux:
pip install python-libtorrent

# macOS (Homebrew recommended):
brew install libtorrent-rasterbar
pip install python-libtorrent
```

### ffmpeg (for video merging, mp3 conversion)
- **Windows:** https://ffmpeg.org/download.html → add to PATH
- **macOS:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg`

---

## 🚀 চালু করুন

```bash
python rapidget.py
```

বা .torrent ফাইলে double-click করলে আপনার OS association থেকে সরাসরি খুলবে।

---

## 🌟 ৭টি ফিচারের বিস্তারিত

### 1. Multi-threaded Downloading
- সার্ভার `Accept-Ranges: bytes` সাপোর্ট করলে ফাইলটি N টি chunk-এ ভাগ হয়
- প্রতিটি chunk আলাদা থ্রেডে সমান্তরালভাবে ডাউনলোড হয়
- File size অনুযায়ী thread সংখ্যা auto-detect: ৫MB→4, ১০MB→8, ৫০MB→16, ১০০MB+→32
- Settings-এ manually 4/8/16/32 বা Auto সিলেক্ট করা যায়

### 2. Pause / Resume
- Pause করলে প্রতিটি chunk-এর `downloaded_so_far` JSON-এ সেভ হয়
- Resume করলে প্রতিটি chunk ঠিক যেখানে ছিল সেখান থেকে শুরু হয়
- অ্যাপ বন্ধ হলে পরের বার "paused" state হিসেবে লোড হয়
- Chunk state ফাইল: `~/.rapidget_state.json`

### 3. Download Queue
- যত ইচ্ছা URL অ্যাড করুন — সব "queued" স্ট্যাটাস পাবে
- Settings-এ "Max concurrent downloads" সেট করুন (default: 3)
- Queue Manager প্রতি ২ সেকেন্ডে চেক করে slot ফাঁকা হলে পরের download শুরু করে
- ▲ Move Up / ▼ Move Down বাটন বা right-click menu দিয়ে queue reorder করুন

### 4. Glass Theme GUI
- PyQt6 + `WA_TranslucentBackground` + `FramelessWindowHint`
- ৮টি accent color (Cyan, Purple, Green, Orange, Red, Pink, Gold, White)
- ৩টি mode: Dark, Light, OS Default
- Opacity slider (30%–100%)
- Windows ও macOS উভয়তে কোনো crash ছাড়াই চলে
- `.ico` file → Windows tray + taskbar; macOS-এ graceful fallback

### 5. Speed Meter Monitor
- ফ্লোটিং, সবসময় সামনে, translucent উইজেট
- Real-time ↓ download + ↑ upload গ্রাফ (30-point rolling history)
- Settings থেকে চালু/বন্ধ, opacity, pin-to-top কন্ট্রোল
- Drag করে যেকোনো জায়গায় সরানো যায়, position সেভ থাকে
- Main table-এ প্রতিটি download-এর ETA কলাম আলাদা দেখায়

### 6. Torrent & Magnet
- **libtorrent** ইন্টিগ্রেশন — আলাদা কোনো অ্যাপ লাগবে না
- Magnet link → metadata fetch → file list
- `.torrent` ফাইল → checkbox দিয়ে নির্দিষ্ট ফাইল বেছে নিন
- Download status: connecting → downloading → seeding
- **File Association (Windows):**
  ```
  assoc .torrent=TorrentFile
  ftype TorrentFile="C:\path\to\python.exe" "C:\path\to\rapidget.py" "%1"
  ```
- **File Association (macOS)** — `Info.plist` এ add করুন:
  ```xml
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeExtensions</key><array><string>torrent</string></array>
      <key>LSHandlerRank</key><string>Owner</string>
    </dict>
  </array>
  ```

### 7. Browser Extension

#### Installation
1. `browser_extension/` ফোল্ডারটি আপনার PC-তে রাখুন
2. Chrome/Edge: `chrome://extensions` → Developer mode ON → "Load unpacked" → ফোল্ডার সিলেক্ট
3. Firefox: `about:debugging` → "Load Temporary Add-on" → `manifest.json` সিলেক্ট

#### কীভাবে কাজ করে
- **Floating Button:** যেকোনো ওয়েবসাইটে `<video>` ট্যাগ detect হলে ভিডিওর corner-এ "⚡ RapidGet" বাটন দেখাবে
- **Format Overlay:** বাটনে click করলে Best/1080p/720p/480p/MP3 অপশন সহ overlay আসবে
- **WebSocket:** Extension → `ws://127.0.0.1:49152` → RapidGet app
- **Fallback HTTP:** WebSocket না থাকলে HTTP POST ব্যবহার করে
- **Port Change:** Extension popup → port number change → Save

#### Architecture
```
Browser Extension (content.js)
        ↓  chrome.runtime.sendMessage
Background Service Worker (background.js)
        ↓  WebSocket / HTTP POST
RapidGet App — ExtServer (Python Thread)
        ↓  PyQt signal (thread-safe)
MainWindow.add_from_ext(url)
        ↓
DownloadItem → Queue → Worker
```

---

## 📁 ফাইল স্ট্রাকচার

```
rapidget.py                   ← মূল অ্যাপ
rapidget.ico                  ← আইকন (একই ফোল্ডারে রাখুন)
browser_extension/
  manifest.json
  background.js
  content.js
  popup.html
  popup.js
  icon16.png / icon48.png / icon128.png  ← আপনার ইচ্ছামতো আইকন যোগ করুন
```

---

## ⚙️ Settings ফাইল
`~/.rapidget_settings.json` — সব সেটিংস এখানে সেভ হয়

## 💾 State ফাইল
`~/.rapidget_state.json` — সব download queue ও chunk progress এখানে সেভ হয়

---

## 🐛 সমস্যা ও সমাধান

| সমস্যা | সমাধান |
|--------|--------|
| Torrent কাজ করছে না | `pip install python-libtorrent` |
| Video download হচ্ছে না | `pip install yt-dlp --upgrade` |
| Extension connect হচ্ছে না | App চালু আছে কিনা দেখুন, port মিলছে কিনা দেখুন |
| macOS-এ আইকন নেই | `.icns` ফাইল বানিয়ে `CFBundleIconFile` set করুন |
| ffmpeg merge হচ্ছে না | ffmpeg PATH-এ আছে কিনা দেখুন: `ffmpeg -version` |

---

© 2026 Stonepit Labs — Open Source Software
