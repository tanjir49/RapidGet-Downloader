// content.js — RapidGet Extension v2.0
// Detects video players and injects a floating "⚡ RapidGet" download button

(function () {
  "use strict";

  const ACCENT = "#00d4ff";
  const BTN_ID = "rapidget-floating-btn";
  const OVERLAY_ID = "rapidget-format-overlay";

  // ── Inject button if not already present ──────────────────────
  function injectButton(target) {
    if (document.getElementById(BTN_ID)) return;

    const btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.innerHTML = "⚡ RapidGet";
    Object.assign(btn.style, {
      position:       "absolute",
      bottom:         "48px",
      right:          "12px",
      zIndex:         "2147483647",
      background:     "rgba(0,0,0,0.78)",
      color:          ACCENT,
      border:         `1px solid ${ACCENT}`,
      borderRadius:   "8px",
      padding:        "6px 14px",
      fontSize:       "13px",
      fontWeight:     "bold",
      fontFamily:     "'Segoe UI', Arial, sans-serif",
      cursor:         "pointer",
      backdropFilter: "blur(8px)",
      boxShadow:      `0 0 12px rgba(0,212,255,0.4)`,
      transition:     "all 0.2s",
      letterSpacing:  "0.5px",
    });

    btn.onmouseenter = () => { btn.style.background = `rgba(0,212,255,0.2)`; };
    btn.onmouseleave = () => { btn.style.background = "rgba(0,0,0,0.78)"; };

    btn.onclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      showFormatOverlay(btn);
    };

    // Make the video container relative for positioning
    const container = target.closest("[class*='player']") ||
                      target.closest("[class*='video']") ||
                      target.parentElement;
    if (container) {
      if (getComputedStyle(container).position === "static")
        container.style.position = "relative";
      container.appendChild(btn);
    } else {
      target.insertAdjacentElement("afterend", btn);
    }
  }

  // ── Format overlay ────────────────────────────────────────────
  function showFormatOverlay(anchor) {
    removeOverlay();

    const url = location.href;
    const overlay = document.createElement("div");
    overlay.id = OVERLAY_ID;
    Object.assign(overlay.style, {
      position:        "fixed",
      bottom:          "80px",
      right:           "20px",
      zIndex:          "2147483647",
      background:      "rgba(10,14,26,0.96)",
      border:          `1px solid ${ACCENT}40`,
      borderRadius:    "12px",
      padding:         "16px",
      minWidth:        "260px",
      backdropFilter:  "blur(16px)",
      boxShadow:       "0 8px 32px rgba(0,0,0,0.6)",
      fontFamily:      "'Segoe UI', Arial, sans-serif",
      color:           "#e6edf3",
    });

    overlay.innerHTML = `
      <div style="color:${ACCENT};font-weight:bold;font-size:14px;margin-bottom:10px;">
        ⚡ RapidGet — Download
      </div>
      <div style="font-size:12px;color:#8b949e;margin-bottom:12px;
                  max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
           title="${url}">${url}</div>
      <div id="rg-format-list" style="display:flex;flex-direction:column;gap:6px;margin-bottom:12px;">
        ${buildFormatButtons()}
      </div>
      <div style="display:flex;gap:8px;">
        <button id="rg-custom-btn" style="${btnStyle("#00d4ff")}">
          📥 Send to App
        </button>
        <button id="rg-close-btn" style="${btnStyle("#555")}">
          ✕ Close
        </button>
      </div>`;

    document.body.appendChild(overlay);

    // Format buttons
    overlay.querySelectorAll(".rg-fmt-btn").forEach(b => {
      b.onclick = () => {
        const fmt = b.dataset.fmt;
        sendToApp(url, fmt);
        removeOverlay();
      };
    });

    document.getElementById("rg-custom-btn").onclick = () => {
      sendToApp(url, "best");
      removeOverlay();
    };

    document.getElementById("rg-close-btn").onclick = removeOverlay;

    // Click outside to close
    setTimeout(() => {
      document.addEventListener("click", outsideClick, { once: true });
    }, 100);
  }

  function outsideClick(e) {
    const ov = document.getElementById(OVERLAY_ID);
    if (ov && !ov.contains(e.target)) removeOverlay();
  }

  function removeOverlay() {
    const ov = document.getElementById(OVERLAY_ID);
    if (ov) ov.remove();
  }

  function buildFormatButtons() {
    const formats = [
      { label: "🎬 Best Quality",  fmt: "best" },
      { label: "📺 1080p MP4",     fmt: "bestvideo[height<=1080]+bestaudio/best" },
      { label: "📺 720p MP4",      fmt: "bestvideo[height<=720]+bestaudio/best" },
      { label: "📺 480p MP4",      fmt: "bestvideo[height<=480]+bestaudio/best" },
      { label: "🎵 MP3 Audio",     fmt: "mp3" },
    ];
    return formats.map(f =>
      `<button class="rg-fmt-btn" data-fmt="${f.fmt}"
         style="${fmtBtnStyle()}"
         onmouseenter="this.style.borderColor='${ACCENT}80';this.style.color='${ACCENT}';"
         onmouseleave="this.style.borderColor='#30363d';this.style.color='#c9d1d9';">
         ${f.label}
       </button>`
    ).join("");
  }

  function fmtBtnStyle() {
    return `background:#161b22;border:1px solid #30363d;border-radius:6px;
            padding:7px 12px;color:#c9d1d9;font-size:12px;cursor:pointer;
            text-align:left;transition:all 0.15s;font-family:'Segoe UI',Arial,sans-serif;`;
  }

  function btnStyle(color) {
    return `background:transparent;border:1px solid ${color}60;border-radius:7px;
            padding:7px 14px;color:${color};font-size:12px;cursor:pointer;
            font-family:'Segoe UI',Arial,sans-serif;`;
  }

  // ── Send to RapidGet app ───────────────────────────────────────
  function sendToApp(url, quality) {
    // Append quality hint as a fragment so the app can pick it up
    const payload = quality && quality !== "best"
      ? `${url}#rapidget_quality=${encodeURIComponent(quality)}`
      : url;
    chrome.runtime.sendMessage({ type: "RAPIDGET_DOWNLOAD", url: payload });
  }

  // ── Watch for video elements ───────────────────────────────────
  const observed = new WeakSet();

  function scanVideos() {
    document.querySelectorAll("video").forEach(v => {
      if (!observed.has(v)) {
        observed.add(v);
        injectButton(v);
        v.addEventListener("enterpictureinpicture", () => injectButton(v));
      }
    });
  }

  // Initial scan
  scanVideos();

  // MutationObserver for dynamically loaded videos (SPAs)
  const observer = new MutationObserver(scanVideos);
  observer.observe(document.body, { childList: true, subtree: true });

})();
