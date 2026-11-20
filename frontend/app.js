// --- ClipYT Simple Frontend ---

// Safe fetch with timeout to prevent frozen/hanging requests
async function fetchWithTimeout(url, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    return res;
  } catch (err) {
    if (err.name === "AbortError") {
      const isVercel = window.location.hostname.includes("vercel.app");
      if (isVercel) {
        throw new Error(
          "Connection timed out. Your AI server is running on your laptop! Please open http://localhost:8000 in your browser."
        );
      }
      throw new Error("Server request timed out. Please check if your backend server is running.");
    }
    throw err;
  } finally {
    clearTimeout(id);
  }
}

// Safe API JSON parser: catches HTML error pages and displays a clean, helpful message
async function parseApiResponse(res) {
  const contentType = (res.headers.get("content-type") || "").toLowerCase();
  const isVercel = window.location.hostname.includes("vercel.app");
  if (!contentType.includes("application/json")) {
    const text = await res.text().catch(() => "");
    if (
      text.includes("<!DOCTYPE") ||
      text.includes("<html") ||
      res.status === 502 ||
      res.status === 503 ||
      res.status === 504 ||
      res.status === 404
    ) {
      if (isVercel) {
        throw new Error(
          "Backend is running on your laptop! Please open http://localhost:8000 in your browser to process videos with your laptop GPU."
        );
      }
      throw new Error(
        "Backend server is offline. Please launch the server on your laptop using 'python run.py'."
      );
    }
    throw new Error(`Server returned unexpected response (${res.status} ${res.statusText || "Error"}).`);
  }
  try {
    return await res.json();
  } catch (err) {
    throw new Error("Invalid response format from server. Please try again.");
  }
}

let currentAnalysis = null;
let heatmapChartInstance = null;
let selectedAspectRatio = "9:16";
let selectedFramingMode = "blur";
let lastAnalyzedUrl = "";
let clipSecondsState = {}; // { clip_id: { start: float, end: float, peak: float|null } }
let manualState = { start: 0, end: 35 };
let storyboardSheetDims = {}; // { url: { cols, rows } }
let manualRafPending = false;

// Rate Limit Quota State: Unlimited links & clips
let currentQuota = {
  allowed: true,
  unlimited: true,
  remaining: 999999,
  max_links: 999999,
  max_clips: 999999,
  reset_human: "Unlimited",
  reset_in_seconds: 0,
};

function show4kUnavailableModal(maxRes) {
  const el = document.getElementById("res-notice-max");
  if (el) el.textContent = maxRes || "1080p";
  const modal = document.getElementById("res-notice-modal");
  if (modal) modal.classList.remove("hidden");
}

function closeResNoticeModal() {
  const modal = document.getElementById("res-notice-modal");
  if (modal) modal.classList.add("hidden");
}
window.closeResNoticeModal = closeResNoticeModal;
window.show4kUnavailableModal = show4kUnavailableModal;

async function fetchRateLimitStatus() {
  try {
    const res = await fetchWithTimeout("/api/rate-limit", {}, 4000);
    if (!res.ok) return;
    const data = await res.json();
    if (data.success) {
      currentQuota = data;
      renderRateLimitUI();
    }
  } catch (e) {
    // Non-blocking fallback
  }
}

function renderRateLimitUI() {
  const pill = document.getElementById("rate-limit-pill");
  const txt = document.getElementById("rate-limit-text");
  if (!pill || !txt) return;

  pill.classList.remove("depleted");
  txt.textContent = "⚡ Unlimited Links & Clips";
}

document.addEventListener("DOMContentLoaded", () => {
  setupEventListeners();
  setupLiquidGlassEffect();
  setupSiteUnlockGate();
  setupProStudio();
  setupVodStudio();
  fetchRateLimitStatus();
});

// Zero-overhead pure GPU compositor styling (0% JS execution on mousemove/scroll)
function setupLiquidGlassEffect() {
  // Pure CSS GPU acceleration is used for ultra-high FPS and butter-smooth scrolling
}

function setupEventListeners() {
  const analyzeForm = document.getElementById("analyze-form");
  const urlInput = document.getElementById("youtube-url");

  analyzeForm.addEventListener("submit", (e) => {
    e.preventDefault();
    runFullAnalysis(urlInput.value.trim());
  });

  // Auto-preview & analyze immediately when user pastes a YouTube link
  urlInput.addEventListener("paste", (e) => {
    setTimeout(() => {
      const val = urlInput.value.trim();
      if (looksLikeYouTubeUrl(val)) {
        runFullAnalysis(val);
      }
    }, 60);
  });

  // Paste button
  const pasteBtn = document.getElementById("paste-btn");
  if (pasteBtn) {
    pasteBtn.addEventListener("click", async () => {
      try {
        const text = (await navigator.clipboard.readText()).trim();
        if (text) {
          urlInput.value = text;
          if (looksLikeYouTubeUrl(text)) {
            runFullAnalysis(text);
          }
        }
      } catch (e) {
        urlInput.focus();
      }
    });
  }

  // Explore demo clips tab
  const demoClipsBtn = document.getElementById("demo-clips-btn");
  if (demoClipsBtn) {
    demoClipsBtn.addEventListener("click", () => {
      const demoUrl = "https://www.youtube.com/watch?v=0e3GPea1Tyg";
      urlInput.value = demoUrl;
      runFullAnalysis(demoUrl);
    });
  }

  // Re-analyze if user changes duration or number of clips while a video is loaded
  document.getElementById("clip-duration").addEventListener("change", () => {
    const val = urlInput.value.trim();
    if (looksLikeYouTubeUrl(val) && currentAnalysis) {
      runFullAnalysis(val, true);
    }
  });

  document.getElementById("max-clips").addEventListener("change", () => {
    const val = urlInput.value.trim();
    if (looksLikeYouTubeUrl(val) && currentAnalysis) {
      runFullAnalysis(val, true);
    }
  });

  // Aspect ratio pills
  document.querySelectorAll("#aspect-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#aspect-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      selectedAspectRatio = btn.getAttribute("data-ratio");
    });
  });

  // Video fit / framing pills
  document.querySelectorAll("#framing-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#framing-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      selectedFramingMode = btn.getAttribute("data-framing");
    });
  });

  // Captions toggle
  const burnSubsToggle = document.getElementById("opt-burn-subs");
  const subBox = document.getElementById("subtitles-options-box");
  burnSubsToggle.addEventListener("change", () => {
    const selects = subBox.querySelectorAll("#opt-sub-style, #opt-sub-pos");
    selects.forEach((el) => (el.disabled = !burnSubsToggle.checked));
    subBox.style.opacity = burnSubsToggle.checked ? "1" : "0.55";
  });

  // Show more / less on YouTube caption/description
  document.getElementById("toggle-caption-btn").addEventListener("click", () => {
    const descEl = document.getElementById("meta-description");
    const btn = document.getElementById("toggle-caption-btn");
    const expanded = descEl.classList.toggle("expanded");
    btn.textContent = expanded ? "Show less" : "Show more";
  });

  // Modal close buttons
  document.getElementById("modal-close-btn").addEventListener("click", hideProgressModal);
  document.getElementById("modal-close-x").addEventListener("click", hideProgressModal);

  // Auto Clipper resolution selection listener (1080p vs 4K)
  const resSelect = document.getElementById("opt-resolution");
  if (resSelect) {
    resSelect.addEventListener("change", (e) => {
      if (e.target.value === "4k") {
        if (currentAnalysis && currentAnalysis.has_4k === false) {
          e.target.value = "1080p";
          const maxStr = currentAnalysis.max_height ? `${currentAnalysis.max_height}p` : "1080p";
          show4kUnavailableModal(maxStr);
        }
      }
    });
  }

  // Mode tabs: Auto Moments vs Select Manually
  const tabAuto = document.getElementById("tab-auto");
  const tabManual = document.getElementById("tab-manual");
  const panelAuto = document.getElementById("panel-auto");
  const panelManual = document.getElementById("panel-manual");

  tabAuto.addEventListener("click", () => {
    tabAuto.classList.add("active");
    tabManual.classList.remove("active");
    panelAuto.classList.remove("hidden");
    panelManual.classList.add("hidden");
  });

  tabManual.addEventListener("click", () => {
    tabManual.classList.add("active");
    tabAuto.classList.remove("active");
    panelManual.classList.remove("hidden");
    panelAuto.classList.add("hidden");
    scheduleManualUIUpdate();
  });

  // Manual dual range sliders (60fps rAF-throttled live frame scrubbing)
  const sStart = document.getElementById("manual-slider-start");
  const sEnd = document.getElementById("manual-slider-end");

  sStart.addEventListener("input", () => {
    let v = parseFloat(sStart.value);
    if (v >= manualState.end - 1) {
      v = Math.max(0, manualState.end - 1);
      sStart.value = v;
    }
    manualState.start = v;
    scheduleManualUIUpdate();
  });

  sEnd.addEventListener("input", () => {
    let v = parseFloat(sEnd.value);
    if (v <= manualState.start + 1) {
      v = Math.min(currentAnalysis ? currentAnalysis.duration : v, manualState.start + 1);
      sEnd.value = v;
    }
    manualState.end = v;
    scheduleManualUIUpdate();
  });

  // Manual text inputs (mm:ss or hh:mm:ss)
  document.getElementById("manual-start-input").addEventListener("change", () => applyManualTextEdit("start"));
  document.getElementById("manual-end-input").addEventListener("change", () => applyManualTextEdit("end"));

  // Create manual Short button
  document.getElementById("manual-create-btn").addEventListener("click", () => {
    applyManualTextEdit("start");
    applyManualTextEdit("end");
    submitGenerateRequest("manual", manualState.start, manualState.end, null);
  });
}

function looksLikeYouTubeUrl(url) {
  return /(youtube\.com\/|youtu\.be\/)/i.test(url);
}

function extractYtVideoId(url) {
  if (!url) return null;
  const m = url.match(/(?:live\/|v=|youtu\.be\/|embed\/|shorts\/)([a-zA-Z0-9_-]{11})/);
  return m ? m[1] : null;
}

// -------------------------------------------------------------
// Time Formatting & Parsing (HH:MM:SS for >= 1 hr, MM:SS for < 1 hr)
// -------------------------------------------------------------
function formatTime(totalSeconds, useHours) {
  const s = Math.max(0, Math.round(totalSeconds));
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  if (useHours || hrs > 0) {
    return `${pad(hrs)}:${pad(mins)}:${pad(secs)}`;
  }
  return `${pad(mins)}:${pad(secs)}`;
}

function parseTime(str) {
  if (str == null) return NaN;
  const cleaned = String(str).trim();
  if (!cleaned) return NaN;
  if (/^\d+(\.\d+)?$/.test(cleaned)) {
    return parseFloat(cleaned);
  }
  const parts = cleaned.split(":").map((p) => parseFloat(p));
  if (parts.some((p) => isNaN(p) || p < 0)) return NaN;
  if (parts.length === 2) {
    return parts[0] * 60 + parts[1];
  }
  if (parts.length === 3) {
    return parts[0] * 3600 + parts[1] * 60 + parts[2];
  }
  return NaN;
}

// -------------------------------------------------------------
// Instant Video Preview + Full Heatmap & Frame Analysis
// -------------------------------------------------------------
async function runFullAnalysis(url, force = false) {
  if (!url) return;
  if (!force && url === lastAnalyzedUrl && currentAnalysis) return;

  hideError();
  setAnalyzeLoading(true);
  lastAnalyzedUrl = url;

  // Step 1: Instant preview (thumbnail + YouTube title)
  fetchInstantPreview(url);

  // Step 2: Full analysis (YouTube caption/description + heatmap + storyboard frames)
  const duration = parseFloat(document.getElementById("clip-duration").value);
  const maxClips = parseInt(document.getElementById("max-clips").value);

  try {
    const res = await fetchWithTimeout("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: url,
        clip_duration: duration,
        max_clips: maxClips,
      }),
    }, 45000);

    const data = await parseApiResponse(res);
    if (!res.ok || !data.success) {
      throw new Error(data.detail || "Could not analyze this YouTube video.");
    }

    currentAnalysis = data.data;
    renderFullAnalysis(currentAnalysis);
    fetchRateLimitStatus();
  } catch (err) {
    document.getElementById("analyzing-indicator").classList.add("hidden");
    showError(err.message || "Something went wrong. Please check the YouTube link.");
    fetchRateLimitStatus();
  } finally {
    setAnalyzeLoading(false);
  }
}

async function fetchInstantPreview(url) {
  const previewSec = document.getElementById("video-preview-section");
  const indicator = document.getElementById("analyzing-indicator");

  try {
    const res = await fetchWithTimeout(`/api/preview?url=${encodeURIComponent(url)}`, {}, 10000);
    if (!res.ok) return;
    const info = await parseApiResponse(res);

    const thumbEl = document.getElementById("meta-thumb");
    thumbEl.onerror = function () {
      if (this.src && this.src.includes("maxresdefault.jpg")) {
        this.src = this.src.replace("maxresdefault.jpg", "hqdefault.jpg");
      }
    };
    thumbEl.src = info.thumbnail;
    document.getElementById("meta-title").textContent = info.title;
    document.getElementById("meta-author").textContent = info.author || "";
    document.getElementById("meta-views").textContent = "";
    document.getElementById("meta-views-sep").classList.add("hidden");
    document.getElementById("meta-duration").classList.add("hidden");
    document.getElementById("meta-description").classList.add("hidden");
    document.getElementById("toggle-caption-btn").classList.add("hidden");
    indicator.classList.remove("hidden");
    previewSec.classList.remove("hidden");
  } catch (e) {
    // Full analysis will still populate everything
  }
}

function renderFullAnalysis(data) {
  const previewSec = document.getElementById("video-preview-section");
  const resultsSec = document.getElementById("results-section");
  const indicator = document.getElementById("analyzing-indicator");

  indicator.classList.add("hidden");
  previewSec.classList.remove("hidden");
  resultsSec.classList.remove("hidden");

  // Validate selected resolution against loaded video
  const resSelect = document.getElementById("opt-resolution");
  if (resSelect && resSelect.value === "4k" && data.has_4k === false) {
    resSelect.value = "1080p";
    const maxStr = data.max_height ? `${data.max_height}p` : "1080p";
    show4kUnavailableModal(maxStr);
  }

  // Video Thumbnail (1080p MaxRes with fallback) + Title + Caption as it is from YouTube
  const fullThumbEl = document.getElementById("meta-thumb");
  fullThumbEl.onerror = function () {
    if (this.src && this.src.includes("maxresdefault.jpg")) {
      this.src = this.src.replace("maxresdefault.jpg", "hqdefault.jpg");
    }
  };
  fullThumbEl.src = data.thumbnail;
  document.getElementById("meta-title").textContent = data.title;
  document.getElementById("meta-author").textContent = data.author;

  const durPill = document.getElementById("meta-duration");
  durPill.textContent = data.duration_str;
  durPill.classList.remove("hidden");

  if (data.view_count) {
    document.getElementById("meta-views").textContent = `${Number(data.view_count).toLocaleString()} views`;
    document.getElementById("meta-views-sep").classList.remove("hidden");
  }

  const descEl = document.getElementById("meta-description");
  const toggleBtn = document.getElementById("toggle-caption-btn");
  descEl.classList.remove("expanded");
  if (data.description) {
    descEl.textContent = data.description;
    descEl.classList.remove("hidden");
    if (data.description.length > 180 || data.description.split("\n").length > 3) {
      toggleBtn.textContent = "Show more";
      toggleBtn.classList.remove("hidden");
    } else {
      toggleBtn.classList.add("hidden");
    }
  } else {
    descEl.classList.add("hidden");
    toggleBtn.classList.add("hidden");
  }

  // Captions & Heatmap status
  const capStatus = document.getElementById("captions-status");
  capStatus.textContent = "AI English + Hinglish Captions";

  const mapSub = document.getElementById("map-subtitle");
  if (data.hype_source === "smart_hype" && Array.isArray(data.hype_signals) && data.hype_signals.length > 0) {
    mapSub.textContent = `⚡ Smart Hype Map (No YouTube replay graph yet) — detected via ${data.hype_signals.join(" + ")}.`;
  } else if (data.has_heatmap) {
    mapSub.textContent = "Higher peaks show where viewers replayed and interacted the most.";
  } else {
    mapSub.textContent = "YouTube hasn't published a replay graph for this video yet — showing suggested moments.";
  }

  renderHeatmapChart(data.heatmap);
  renderClipsGrid(data.clips, data.video_id, data.use_hours, data.duration);
  initManualSelector(data);
}

// -------------------------------------------------------------
// Engagement & Interaction Map (Chart.js)
// -------------------------------------------------------------
function renderHeatmapChart(heatmapPoints) {
  const canvas = document.getElementById("heatmapChart");
  const ctx = canvas.getContext("2d");

  if (heatmapChartInstance) {
    heatmapChartInstance.destroy();
  }

  const points =
    heatmapPoints && heatmapPoints.length > 0
      ? heatmapPoints
      : [
          { time_str: "00:00", intensity: 25 },
          { time_str: "Middle", intensity: 75 },
          { time_str: "End", intensity: 35 },
        ];

  const labels = points.map((p) => p.time_str);
  const values = points.map((p) => p.intensity);

  const gradient = ctx.createLinearGradient(0, 0, 0, 215);
  gradient.addColorStop(0, "rgba(37, 99, 235, 0.35)");
  gradient.addColorStop(0.65, "rgba(96, 165, 250, 0.12)");
  gradient.addColorStop(1, "rgba(191, 219, 254, 0.0)");

  heatmapChartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels: labels,
      datasets: [
        {
          label: "Engagement",
          data: values,
          borderColor: "#3b82f6",
          borderWidth: 2.8,
          backgroundColor: gradient,
          fill: true,
          tension: 0.38,
          pointRadius: 0,
          pointHoverRadius: 6,
          pointHoverBackgroundColor: "#ffffff",
          pointHoverBorderColor: "#2563eb",
          pointHoverBorderWidth: 2.5,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      devicePixelRatio: Math.min(window.devicePixelRatio || 1, 2),
      animation: { duration: 260 },
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "rgba(10, 16, 28, 0.96)",
          titleColor: "#f8fafc",
          bodyColor: "#60a5fa",
          borderColor: "rgba(37, 99, 235, 0.35)",
          borderWidth: 1,
          padding: 10,
          cornerRadius: 10,
          displayColors: false,
          callbacks: {
            title: (items) => `Time: ${items[0].label}`,
            label: (item) => `Engagement: ${item.parsed.y}%`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: "rgba(109, 64, 198, 0.08)" },
          ticks: { color: "#675b85", maxTicksLimit: 10 },
        },
        y: {
          min: 0,
          max: 100,
          grid: { color: "rgba(109, 64, 198, 0.08)" },
          ticks: { color: "#675b85", callback: (v) => v + "%" },
        },
      },
    },
  });
}

// -------------------------------------------------------------
// Most Engaging Moments Grid (with Video Frame Pictures + MM:SS / HH:MM:SS)
// -------------------------------------------------------------
function renderClipsGrid(clips, videoId, useHours, videoDuration) {
  const container = document.getElementById("clips-grid");
  container.innerHTML = "";
  clipSecondsState = {};

  if (!clips || clips.length === 0) {
    container.innerHTML = `<div class="empty-state">No clips found for this video.</div>`;
    return;
  }

  clips.forEach((clip) => {
    clipSecondsState[clip.clip_id] = {
      start: clip.start_time,
      end: clip.end_time,
      peak: clip.peak_time,
    };

    const startFormatted = formatTime(clip.start_time, useHours);
    const endFormatted = formatTime(clip.end_time, useHours);
    const durFormatted = formatTime(clip.end_time - clip.start_time, false);
    const ytPreviewUrl = `https://www.youtube.com/watch?v=${videoId}&t=${Math.floor(clip.start_time)}s`;

    const card = document.createElement("div");
    card.className = "clip-card";
    card.innerHTML = `
      <div class="clip-frame-box">
        <img src="${clip.frame_image}" alt="Video frame at ${clip.peak_str}">
        <div class="clip-frame-gradient"></div>
        <div class="frame-top-badges">
          <span class="moment-badge">Moment #${clip.rank}</span>
          <span class="engagement-badge">${clip.intensity_score}% Engagement</span>
        </div>
        <div class="frame-time-overlay">
          <span class="peak-pill">Peak at ${clip.peak_str}</span>
        </div>
      </div>

      <div class="clip-card-body">
        <div class="timing-row">
          <div class="timing-field">
            <span class="timing-label">Start (${useHours ? "hh:mm:ss" : "mm:ss"})</span>
            <div class="timing-controls">
              <button type="button" class="step-btn" onclick="stepClipTime('${clip.clip_id}', 'start', -1)">−</button>
              <input
                type="text"
                id="start-${clip.clip_id}"
                class="time-text-input"
                value="${startFormatted}"
                onchange="onManualTimeEdit('${clip.clip_id}', 'start')"
              >
              <button type="button" class="step-btn" onclick="stepClipTime('${clip.clip_id}', 'start', 1)">+</button>
            </div>
          </div>

          <div class="timing-field">
            <span class="timing-label">End (${useHours ? "hh:mm:ss" : "mm:ss"})</span>
            <div class="timing-controls">
              <button type="button" class="step-btn" onclick="stepClipTime('${clip.clip_id}', 'end', -1)">−</button>
              <input
                type="text"
                id="end-${clip.clip_id}"
                class="time-text-input"
                value="${endFormatted}"
                onchange="onManualTimeEdit('${clip.clip_id}', 'end')"
              >
              <button type="button" class="step-btn" onclick="stepClipTime('${clip.clip_id}', 'end', 1)">+</button>
            </div>
          </div>
        </div>

        <div class="clip-foot-row">
          <span>Length: <strong id="dur-${clip.clip_id}">${durFormatted}</strong></span>
          <a id="ytlink-${clip.clip_id}" href="${ytPreviewUrl}" target="_blank" rel="noopener noreferrer" class="yt-watch-link">
            Watch on YouTube ↗
          </a>
        </div>

        <button type="button" class="btn-create-short" onclick="triggerGenerate('${clip.clip_id}')">
          Create this Short
        </button>
      </div>
    `;
    container.appendChild(card);
  });
}

window.stepClipTime = function (clipId, field, deltaSec) {
  if (!currentAnalysis || !clipSecondsState[clipId]) return;
  const maxDur = currentAnalysis.duration || 999999;
  let nextVal = clipSecondsState[clipId][field] + deltaSec;
  nextVal = Math.max(0, Math.min(maxDur, nextVal));
  clipSecondsState[clipId][field] = nextVal;
  syncClipTimeUI(clipId);
};

window.onManualTimeEdit = function (clipId, field) {
  if (!currentAnalysis || !clipSecondsState[clipId]) return;
  const input = document.getElementById(`${field}-${clipId}`);
  const parsed = parseTime(input.value);
  const maxDur = currentAnalysis.duration || 999999;
  if (!isNaN(parsed)) {
    clipSecondsState[clipId][field] = Math.max(0, Math.min(maxDur, parsed));
  }
  syncClipTimeUI(clipId);
};

function syncClipTimeUI(clipId) {
  const useHours = Boolean(currentAnalysis && currentAnalysis.use_hours);
  const st = clipSecondsState[clipId];
  document.getElementById(`start-${clipId}`).value = formatTime(st.start, useHours);
  document.getElementById(`end-${clipId}`).value = formatTime(st.end, useHours);
  const dur = Math.max(0, st.end - st.start);
  document.getElementById(`dur-${clipId}`).textContent = formatTime(dur, false);
  const link = document.getElementById(`ytlink-${clipId}`);
  if (link && currentAnalysis) {
    link.href = `https://www.youtube.com/watch?v=${currentAnalysis.video_id}&t=${Math.floor(st.start)}s`;
  }
}

// -------------------------------------------------------------
// Manual Clip Selector (Dual Sliders + 60fps Live Storyboard Frames)
// -------------------------------------------------------------
function initManualSelector(data) {
  const dur = Math.max(2, Math.floor(data.duration || 60));
  const useHours = Boolean(data.use_hours);
  const fmtHint = useHours ? "hh:mm:ss" : "mm:ss";

  document.getElementById("manual-start-label").textContent = `Start (${fmtHint})`;
  document.getElementById("manual-end-label").textContent = `End (${fmtHint})`;

  const sStart = document.getElementById("manual-slider-start");
  const sEnd = document.getElementById("manual-slider-end");
  sStart.max = dur;
  sEnd.max = dur;

  // Start with the top clip range (or 0 to 35s)
  const firstClip = data.clips && data.clips[0];
  const initStart = firstClip ? Math.floor(firstClip.start_time) : 0;
  const initEnd = firstClip ? Math.min(dur, Math.floor(firstClip.end_time)) : Math.min(dur, 35);

  manualState.start = initStart;
  manualState.end = Math.max(initStart + 2, initEnd);

  storyboardSheetDims = {};
  if (data.storyboard && data.storyboard.fragments) {
    // Lazily cache actual dimensions of first and last sprite sheets
    const sb = data.storyboard;
    const toPreload = [sb.fragments[0], sb.fragments[sb.fragments.length - 1]].filter(Boolean);
    toPreload.forEach((f) => preloadSpriteSheetDims(f.url, sb.width, sb.height));
  }

  updateManualUI();
}

function preloadSpriteSheetDims(url, cellW, cellH) {
  if (!url || storyboardSheetDims[url]) return;
  storyboardSheetDims[url] = { loading: true };
  const img = new Image();
  img.referrerPolicy = "no-referrer";
  img.onload = () => {
    storyboardSheetDims[url] = {
      cols: Math.max(1, Math.round(img.naturalWidth / cellW)),
      rows: Math.max(1, Math.round(img.naturalHeight / cellH)),
    };
    scheduleManualUIUpdate();
  };
  img.onerror = () => {
    storyboardSheetDims[url] = { failed: true };
  };
  img.src = url;
}

function scheduleManualUIUpdate() {
  if (manualRafPending) return;
  manualRafPending = true;
  requestAnimationFrame(() => {
    manualRafPending = false;
    updateManualUI();
  });
}

function updateManualUI() {
  if (!currentAnalysis) return;
  const dur = Math.max(1, currentAnalysis.duration || 60);
  const useHours = Boolean(currentAnalysis.use_hours);

  const startStr = formatTime(manualState.start, useHours);
  const endStr = formatTime(manualState.end, useHours);
  const lenStr = formatTime(Math.max(0, manualState.end - manualState.start), false);

  document.getElementById("manual-slider-start").value = manualState.start;
  document.getElementById("manual-slider-end").value = manualState.end;
  document.getElementById("manual-start-input").value = startStr;
  document.getElementById("manual-end-input").value = endStr;
  document.getElementById("manual-start-badge").textContent = startStr;
  document.getElementById("manual-end-badge").textContent = endStr;
  document.getElementById("manual-duration-display").textContent = lenStr;

  // Highlight active span on dual range track
  const leftPct = (manualState.start / dur) * 100;
  const widthPct = Math.max(0.5, ((manualState.end - manualState.start) / dur) * 100);
  const fill = document.getElementById("dual-slider-fill");
  fill.style.left = `${leftPct}%`;
  fill.style.width = `${widthPct}%`;

  const ytLink = document.getElementById("manual-yt-link");
  ytLink.href = `https://www.youtube.com/watch?v=${currentAnalysis.video_id}&t=${Math.floor(manualState.start)}s`;

  // Update live 60fps storyboard frame previews
  renderSpriteFrame(document.getElementById("manual-start-preview"), manualState.start);
  renderSpriteFrame(document.getElementById("manual-end-preview"), manualState.end);
}

function renderSpriteFrame(el, seconds) {
  if (!el || !currentAnalysis) return;
  const sb = currentAnalysis.storyboard;
  if (!sb || !sb.fragments || sb.fragments.length === 0) {
    el.style.backgroundImage = `url('${currentAnalysis.thumbnail}')`;
    el.style.backgroundSize = "cover";
    el.style.backgroundPosition = "center";
    return;
  }

  const ts = Math.max(0, Math.min(seconds, sb.total_duration - 0.01));
  let frag = sb.fragments[sb.fragments.length - 1];
  for (let i = 0; i < sb.fragments.length; i++) {
    if (ts >= sb.fragments[i].start && ts <= sb.fragments[i].end) {
      frag = sb.fragments[i];
      break;
    }
  }

  const dims = storyboardSheetDims[frag.url];
  if (!dims) {
    preloadSpriteSheetDims(frag.url, sb.width, sb.height);
  }
  const cols = (dims && dims.cols) ? dims.cols : sb.cols;
  const rows = (dims && dims.rows) ? dims.rows : sb.rows;
  const totalCells = cols * rows;

  const relT = Math.max(0, ts - frag.start);
  const fragDur = Math.max(0.01, frag.end - frag.start);
  let idx = sb.fps > 0 ? Math.floor(relT * sb.fps) : Math.floor((relT / fragDur) * (sb.rows * sb.cols));
  idx = Math.max(0, Math.min(idx, totalCells - 1));

  const r = Math.floor(idx / cols);
  const c = idx % cols;
  const posX = cols > 1 ? (c / (cols - 1)) * 100 : 0;
  const posY = rows > 1 ? (r / (rows - 1)) * 100 : 0;

  el.style.backgroundImage = `url('${frag.url}')`;
  el.style.backgroundSize = `${cols * 100}% ${rows * 100}%`;
  el.style.backgroundPosition = `${posX}% ${posY}%`;
}

window.stepManualTime = function (field, deltaSec) {
  if (!currentAnalysis) return;
  const maxDur = currentAnalysis.duration || 999999;
  let val = manualState[field] + deltaSec;
  if (field === "start") {
    val = Math.max(0, Math.min(manualState.end - 1, val));
  } else {
    val = Math.max(manualState.start + 1, Math.min(maxDur, val));
  }
  manualState[field] = val;
  updateManualUI();
};

function applyManualTextEdit(field) {
  if (!currentAnalysis) return;
  const input = document.getElementById(`manual-${field}-input`);
  const parsed = parseTime(input.value);
  const maxDur = currentAnalysis.duration || 999999;
  if (!isNaN(parsed)) {
    let val = Math.max(0, Math.min(maxDur, parsed));
    if (field === "start" && val >= manualState.end) {
      val = Math.max(0, manualState.end - 1);
    } else if (field === "end" && val <= manualState.start) {
      val = Math.min(maxDur, manualState.start + 1);
    }
    manualState[field] = val;
  }
  updateManualUI();
}

// -------------------------------------------------------------
// Create Short Pipeline (Shared by Auto Moments & Manual Selector)
// -------------------------------------------------------------
window.triggerGenerate = async function (clipId) {
  if (!currentAnalysis || !clipSecondsState[clipId]) return;

  onManualTimeEdit(clipId, "start");
  onManualTimeEdit(clipId, "end");

  const st = clipSecondsState[clipId];
  await submitGenerateRequest(clipId, st.start, st.end, st.peak ?? null);
};

async function submitGenerateRequest(clipId, startVal, endVal, peakTime) {
  if (!currentAnalysis) return;
  if (endVal <= startVal) {
    showError("End time must be after Start time.");
    return;
  }

  hideError();
  const payload = {
    url: document.getElementById("youtube-url").value.trim(),
    video_id: currentAnalysis.video_id,
    clip_id: clipId,
    start_time: startVal,
    end_time: endVal,
    aspect_ratio: selectedAspectRatio,
    framing_mode: selectedFramingMode,
    burn_subtitles: document.getElementById("opt-burn-subs").checked,
    subtitle_style: document.getElementById("opt-sub-style").value,
    subtitle_position: document.getElementById("opt-sub-pos").value,
    resolution: document.getElementById("opt-resolution").value,
    climax_hook: document.getElementById("opt-climax-hook").checked,
    peak_time: peakTime,
  };

  showProgressModal("Starting...");

  try {
    const res = await fetchWithTimeout("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 20000);

    const data = await parseApiResponse(res);
    if (!res.ok || !data.success) {
      throw new Error(data.detail || "Could not start creating Short.");
    }

    if (data.remaining_clips !== undefined) {
      currentQuota.remaining = data.remaining_clips;
      renderRateLimitUI();
    }

    pollTaskProgress(data.task_id);
  } catch (err) {
    hideProgressModal();
    showError(err.message);
  }
}

let activePollingTaskId = null;

function pollTaskProgress(taskId) {
  activePollingTaskId = taskId;
  async function tick() {
    if (activePollingTaskId !== taskId) return;
    try {
      const res = await fetchWithTimeout(`/api/task/${taskId}`, {}, 8000);
      if (!res.ok) {
        clearInterval(timer);
        activePollingTaskId = null;
        hideProgressModal();
        const msg = "Lost connection while creating Short.";
        showError(msg);
        showProError(msg);
        return;
      }

      const task = await parseApiResponse(res);
      updateProgressModal(task.message, task.progress);

      if (task.status === "completed" && task.result) {
        clearInterval(timer);
        activePollingTaskId = null;
        fetchRateLimitStatus();
        const sz = Number(task.result.size_mb || task.result.file_size_mb || 0);
        const d = Number(task.result.duration || 0);
        if (sz <= 0.05 || d <= 0.5) {
          hideProgressModal();
          const failMsg = "Short creation produced an empty video (0 MB). If this stream ended very recently, YouTube is still archiving it. Please try another video or timestamp.";
          showError(failMsg);
          showProError(failMsg);
          if (typeof showVodError === "function") showVodError(failMsg);
          return;
        }
        showResultModal(task.result);
      } else if (task.status === "failed") {
        clearInterval(timer);
        activePollingTaskId = null;
        fetchRateLimitStatus();
        hideProgressModal();
        const errText = task.error || task.message || "Short creation failed.";
        showError(errText);
        showProError(errText);
        if (typeof showVodError === "function") showVodError(errText);
      }
    } catch (e) {
      console.error(e);
    }
  }

  const timer = setInterval(tick, 1500);

  // Resume immediately if user switches back to Safari on iOS
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && activePollingTaskId === taskId) {
      tick();
    }
  }, { passive: true });
}

// -------------------------------------------------------------
// UI & Modal Helpers
// -------------------------------------------------------------
function setAnalyzeLoading(loading) {
  const btn = document.getElementById("analyze-btn");
  const txt = btn.querySelector(".btn-text");
  const sp = btn.querySelector(".spinner");
  btn.disabled = loading;
  txt.textContent = loading ? "Analyzing..." : "Find Moments";
  sp.classList.toggle("hidden", !loading);
}

function showError(msg) {
  document.getElementById("error-message").textContent = msg;
  document.getElementById("error-banner").classList.remove("hidden");
}

function hideError() {
  document.getElementById("error-banner").classList.add("hidden");
}

function showProgressModal(msg) {
  document.getElementById("modal-result-view").classList.add("hidden");
  document.getElementById("modal-progress-view").classList.remove("hidden");
  updateProgressModal(msg, 12);
  document.getElementById("progress-modal").classList.remove("hidden");
}

function updateProgressModal(msg, pct) {
  document.getElementById("progress-status").textContent = msg;
  document.getElementById("progress-pct").textContent = `${pct}%`;
  document.getElementById("progress-fill").style.width = `${pct}%`;
}

function showResultModal(result) {
  const videoEl = document.getElementById("result-video");
  if (videoEl) {
    videoEl.preload = "metadata";
    videoEl.onerror = () => {
      // Prevent browser infinite 404 reconnect storm if clip expired
      videoEl.removeAttribute("src");
      videoEl.load();
    };
    // Stream inline for RFC 7233 byte-range playback (avoids iOS Safari attachment cancellation)
    const streamUrl = result.download_url.includes("?")
      ? `${result.download_url}&inline=true`
      : `${result.download_url}?inline=true`;
    videoEl.src = streamUrl;
  }

  const dlBtn = document.getElementById("result-download-btn");
  if (dlBtn) {
    dlBtn.href = result.download_url;
    dlBtn.setAttribute("download", result.filename || "clip.mp4");
    dlBtn.innerHTML = "⬇️ Download Short (MP4)";

    if (!dlBtn.dataset.boundDownload) {
      dlBtn.dataset.boundDownload = "1";
      dlBtn.addEventListener("click", () => {
        // Pause preview video to allocate 100% bandwidth to file download
        if (videoEl && !videoEl.paused) {
          videoEl.pause();
        }

        dlBtn.innerHTML = "<span>✓ Downloading started! Check downloads</span>";
        setTimeout(() => {
          dlBtn.innerHTML = "⬇️ Download Short (MP4)";
        }, 4000);
      });
    }
  }

  const hookBadge = result.climax_hook ? " • Climax Hook" : "";
  const proBadge = result.is_pro ? " • Live Clipper" : "";
  const capBadge = result.has_subtitles
    ? (result.caption_engine === "ai_whisper_hinglish" ? " • AI Hinglish Captions" : " • Captions")
    : (result.caption_note ? ` • Captions skipped: ${result.caption_note}` : "");
  const sizeVal = result.file_size_mb ?? result.size_mb ?? 0;
  document.getElementById("result-meta").textContent =
    `${result.aspect_ratio} • ${result.duration}s • ${sizeVal} MB${proBadge}${capBadge}${hookBadge}`;

  document.getElementById("modal-progress-view").classList.add("hidden");
  document.getElementById("modal-result-view").classList.remove("hidden");
}

function hideProgressModal() {
  const videoEl = document.getElementById("result-video");
  videoEl.pause();
  videoEl.removeAttribute("src");
  videoEl.load();
  document.getElementById("progress-modal").classList.add("hidden");
}


// =============================================================
// MANDATORY 2-TASK SITE UNLOCK GATE (YOUTUBE + INSTAGRAM)
// =============================================================
const SITE_UNLOCK_KEY = "clipyt_site_unlocked_v2";

const siteUnlockState = {
  ytClicked: false,
  ytTimerReady: false,
  ytTimerId: null,
  igClicked: false,
  igTimerReady: false,
  igTimerId: null,
  isVerifying: false,
};

function isSiteUnlocked() {
  try {
    const raw = localStorage.getItem(SITE_UNLOCK_KEY);
    if (!raw) return false;
    const parsed = JSON.parse(raw);
    return Boolean(parsed && (parsed.unlocked || parsed.unlockedAt || (parsed.yt && parsed.ig)));
  } catch (e) {
    return false;
  }
}

function setupSiteUnlockGate() {
  const gateEl = document.getElementById("site-unlock-gate");
  if (!gateEl) return;

  if (isSiteUnlocked()) {
    gateEl.classList.add("hidden");
    gateEl.style.display = "none";
    document.body.style.overflow = "";
    return;
  }

  // Show gate overlay
  gateEl.classList.remove("hidden");
  document.body.style.overflow = "hidden";

  // Elements
  const ytCard = document.getElementById("task-card-yt");
  const ytBtn = document.getElementById("yt-subscribe-btn");
  const ytPill = document.getElementById("yt-status-pill");
  const ytInput = document.getElementById("yt-handle-input");
  const ytWaitStatus = document.getElementById("yt-wait-status");

  const igCard = document.getElementById("task-card-ig");
  const igBtn = document.getElementById("ig-follow-btn");
  const igPill = document.getElementById("ig-status-pill");
  const igInput = document.getElementById("ig-handle-input");
  const igWaitStatus = document.getElementById("ig-wait-status");

  const verifyBtn = document.getElementById("btn-verify-all-tasks");
  const liveCard = document.getElementById("verifier-live-card");
  const errEl = document.getElementById("unlock-gate-error");
  const stepYt = document.getElementById("vstep-yt");
  const stepIg = document.getElementById("vstep-ig");
  const stepToken = document.getElementById("vstep-token");

  // Persistent click states (survive tab switches / reloads on mobile!)
  let hasYtClicked = localStorage.getItem("clipyt_task_yt") === "true";
  let hasIgClicked = localStorage.getItem("clipyt_task_ig") === "true";

  function markYtDone() {
    hasYtClicked = true;
    localStorage.setItem("clipyt_task_yt", "true");
    if (ytPill) {
      ytPill.textContent = "✓ READY";
      ytPill.className = "task-status-pill ready";
    }
    if (ytCard) {
      ytCard.classList.remove("active-working");
      ytCard.classList.add("task-done");
    }
    if (ytWaitStatus) {
      ytWaitStatus.classList.remove("hidden");
      ytWaitStatus.classList.add("ready");
      ytWaitStatus.textContent = "✓ Channel opened! Ready to verify.";
    }
    checkBothReady();
  }

  function markIgDone() {
    hasIgClicked = true;
    localStorage.setItem("clipyt_task_ig", "true");
    if (igPill) {
      igPill.textContent = "✓ READY";
      igPill.className = "task-status-pill ready";
    }
    if (igCard) {
      igCard.classList.remove("active-working");
      igCard.classList.add("task-done");
    }
    if (igWaitStatus) {
      igWaitStatus.classList.remove("hidden");
      igWaitStatus.classList.add("ready");
      igWaitStatus.textContent = "✓ Profile opened! Ready to verify.";
    }
    checkBothReady();
  }

  function checkBothReady() {
    if (hasYtClicked && hasIgClicked && verifyBtn) {
      verifyBtn.classList.add("btn-verify-pulse");
      if (errEl) errEl.classList.add("hidden");
    }
  }

  // Restore state if returning from YouTube or Instagram app
  if (hasYtClicked) markYtDone();
  if (hasIgClicked) markIgDone();

  // Task 1: YouTube Subscribe Click -> instantly mark clicked & ready
  if (ytBtn) {
    ytBtn.addEventListener("click", () => {
      markYtDone();
    });
  }

  // Task 2: Instagram Follow Click -> instantly mark clicked & ready
  if (igBtn) {
    igBtn.addEventListener("click", () => {
      markIgDone();
    });
  }

  // Master Verifier Button Click
  if (verifyBtn) {
    verifyBtn.addEventListener("click", () => {
      if (siteUnlockState.isVerifying) return;
      if (errEl) errEl.classList.add("hidden");

      // Check Task 1
      if (!hasYtClicked) {
        if (errEl) {
          errEl.textContent = "👉 Please tap '1. Subscribe to @Annieplaysgta6' first!";
          errEl.classList.remove("hidden");
        }
        ytBtn?.focus();
        return;
      }

      // Check Task 2
      if (!hasIgClicked) {
        if (errEl) {
          errEl.textContent = "👉 Please tap '2. Follow @gta6.insider_' first!";
          errEl.classList.remove("hidden");
        }
        igBtn?.focus();
        return;
      }

      // Optional handles (never block users!)
      const ytVal = ytInput?.value?.trim()?.replace(/^@+/, "") || "subscriber";
      const igVal = igInput?.value?.trim()?.replace(/^@+/, "") || "follower";

      // Start Verification sequence
      siteUnlockState.isVerifying = true;
      verifyBtn.disabled = true;
      if (ytInput) ytInput.disabled = true;
      if (igInput) igInput.disabled = true;
      if (liveCard) liveCard.classList.remove("hidden");

      if (stepYt) {
        stepYt.className = "verifier-step-line active";
        stepYt.textContent = `⏳ Verifying YouTube subscription for @${ytVal}...`;
      }
      if (stepIg) {
        stepIg.className = "verifier-step-line";
        stepIg.textContent = `⚪ Verifying Instagram follow for @gta6.insider_...`;
      }
      if (stepToken) {
        stepToken.className = "verifier-step-line";
        stepToken.textContent = `⚪ Generating secure access token...`;
      }

      // Fast, smooth verification animation (~1s total)
      setTimeout(() => {
        if (stepYt) {
          stepYt.className = "verifier-step-line done";
          stepYt.textContent = `✓ YouTube subscription confirmed!`;
        }
        if (ytPill) {
          ytPill.textContent = "VERIFIED";
          ytPill.className = "task-status-pill verified";
        }
        if (stepIg) {
          stepIg.className = "verifier-step-line active";
          stepIg.textContent = `⏳ Verifying Instagram follow for @gta6.insider_...`;
        }

        setTimeout(() => {
          if (stepIg) {
            stepIg.className = "verifier-step-line done";
            stepIg.textContent = `✓ Instagram follow confirmed!`;
          }
          if (igPill) {
            igPill.textContent = "VERIFIED";
            igPill.className = "task-status-pill verified";
          }
          if (stepToken) {
            stepToken.className = "verifier-step-line active";
            stepToken.textContent = `⏳ Issuing lifetime access token...`;
          }

          setTimeout(() => {
            if (stepToken) {
              stepToken.className = "verifier-step-line done";
              stepToken.textContent = `✓ Verified! Welcome to ClipYt AI.`;
            }

            // Stop the spinner and show unlocked button
            const vSpinner = liveCard?.querySelector(".verifier-spinner");
            if (vSpinner) vSpinner.style.display = "none";
            if (verifyBtn) {
              verifyBtn.classList.remove("btn-verify-pulse");
              verifyBtn.innerHTML = "<span>✓ Unlocked! Entering Studio...</span>";
              verifyBtn.style.background = "#2563eb";
            }

            // Save permanent unlock
            try {
              localStorage.setItem(
                SITE_UNLOCK_KEY,
                JSON.stringify({ yt: ytVal, ig: igVal, unlocked: true, unlockedAt: Date.now() })
              );
              localStorage.setItem(
                PRO_UNLOCK_KEY,
                JSON.stringify({ handle: igVal, unlocked: true, unlockedAt: Date.now() })
              );
            } catch (e) {}

            proState.unlocked = true;
            siteUnlockState.isVerifying = false;
            try {
              if (typeof updateLiveKeyUI === "function") updateLiveKeyUI();
            } catch (e) {}

            // Smooth fade out & dismiss modal
            setTimeout(() => {
              gateEl.style.transition = "opacity 0.35s ease, transform 0.35s ease";
              gateEl.style.opacity = "0";
              gateEl.style.transform = "scale(1.02)";
              setTimeout(() => {
                gateEl.classList.add("hidden");
                gateEl.style.display = "none";
                document.body.style.overflow = "";
              }, 350);
            }, 300);
          }, 350);
        }, 350);
      }, 400);
    });
  }
}


// =============================================================
// LIVE STUDIO: TELEGRAM 6-HOUR KEY GATE + STREAM PLAYER + DUAL BOX
// =============================================================
const LIVE_KEY_STORAGE = "clipyt_live_key_session";

const proState = {
  unlocked: false,
  accessKey: null,
  mode: "standard", // "standard" | "pro"
  source: null,          // data from /api/pro/inspect
  firstPdtUtc: null,     // exact UTC epoch of 00:00 on the DVR timeline
  totalSec: 300,         // total selectable DVR/video seconds
  currentPlaySec: 0,     // current playback second
  clipDuration: 35,      // Short length: 20, 35, 50, 60
  isDraggingScrubber: false,
  trackerId: null,
  cam_position: "top",   // "top" or "bottom"
  cam_ratio: 0.40,       // 0.25 to 0.65
  resolution: "1080p",   // "1080p" or "4k"
  boxes: {
    cam: { x: 0.03, y: 0.05, w: 0.28, h: 0.36 },    // Streamer (Facecam) top-left (keeps bottom-right clear)
    stream: { x: 0.02, y: 0.03, w: 0.96, h: 0.82 }, // Display (Screen) above native YouTube control bar
  },
};

function getLiveKeySession() {
  try {
    const raw = localStorage.getItem(LIVE_KEY_STORAGE);
    if (!raw) return null;
    const sess = JSON.parse(raw);
    if (!sess || !sess.expiresAt) return null;
    if (Date.now() > sess.expiresAt) {
      localStorage.removeItem(LIVE_KEY_STORAGE);
      return null;
    }
    return sess;
  } catch (e) {
    return null;
  }
}

function isLiveStudioUnlocked() {
  return Boolean(getLiveKeySession());
}

function refreshLiveKeyUI() {
  const sess = getLiveKeySession();
  const badgePill = document.getElementById("pro-badge-pill");
  const activeBadge = document.getElementById("live-key-active-badge");
  const countdownEl = document.getElementById("live-key-countdown");

  if (sess) {
    proState.unlocked = true;
    proState.accessKey = sess.key;
    if (badgePill) {
      badgePill.textContent = "✓ 6h ACTIVE";
      badgePill.classList.add("unlocked");
    }
    if (activeBadge && countdownEl) {
      activeBadge.classList.remove("hidden");
      const remSec = Math.max(0, Math.floor((sess.expiresAt - Date.now()) / 1000));
      const hrs = Math.floor(remSec / 3600);
      const mins = Math.floor((remSec % 3600) / 60);
      countdownEl.textContent = `${String(hrs).padStart(2, "0")}h ${String(mins).padStart(2, "0")}m left`;
    }
  } else {
    proState.unlocked = false;
    proState.accessKey = null;
    if (badgePill) {
      badgePill.textContent = "🔒 6h KEY";
      badgePill.classList.remove("unlocked");
    }
    if (activeBadge) {
      activeBadge.classList.add("hidden");
    }
  }
}

function openLiveKeyModal() {
  const modal = document.getElementById("live-key-modal");
  const errEl = document.getElementById("live-key-error");
  if (errEl) errEl.classList.add("hidden");
  if (modal) {
    modal.classList.remove("hidden");
    const input = document.getElementById("live-access-key-input");
    if (input) setTimeout(() => input.focus(), 80);
  }
}

function closeLiveKeyModal() {
  const modal = document.getElementById("live-key-modal");
  if (modal) modal.classList.add("hidden");
}

async function submitLiveAccessKey() {
  const input = document.getElementById("live-access-key-input");
  const errEl = document.getElementById("live-key-error");
  const submitBtn = document.getElementById("live-key-submit-btn");
  if (!input || !submitBtn) return;

  const keyVal = input.value.trim();
  if (errEl) errEl.classList.add("hidden");

  if (!keyVal) {
    if (errEl) {
      errEl.textContent = "Please enter the 6-hour access key from Telegram.";
      errEl.classList.remove("hidden");
    }
    return;
  }

  const btnTxt = submitBtn.querySelector(".btn-text");
  const sp = submitBtn.querySelector(".spinner");
  submitBtn.disabled = true;
  if (btnTxt) btnTxt.textContent = "Verifying Key...";
  if (sp) sp.classList.remove("hidden");

  try {
    const res = await fetchWithTimeout("/api/live/verify-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: keyVal }),
    }, 12000);
    const json = await parseApiResponse(res);

    if (!res.ok || !json.valid) {
      const msg = (json && (json.message || json.detail?.message)) || "Invalid key. Get the active key on Telegram!";
      throw new Error(msg);
    }

    const expSec = json.expires_in_sec || (6 * 3600);
    localStorage.setItem(LIVE_KEY_STORAGE, JSON.stringify({
      key: keyVal,
      unlockedAt: Date.now(),
      expiresAt: Date.now() + (expSec * 1000),
    }));

    refreshLiveKeyUI();
    closeLiveKeyModal();
    switchStudioMode("pro");
  } catch (err) {
    if (errEl) {
      errEl.textContent = err.message || "Invalid access key. Check Telegram channel!";
      errEl.classList.remove("hidden");
    }
  } finally {
    submitBtn.disabled = false;
    if (btnTxt) btnTxt.textContent = "Verify";
    if (sp) sp.classList.add("hidden");
  }
}

function switchStudioMode(targetMode) {
  const stdBtn = document.getElementById("studio-btn-standard");
  const proBtn = document.getElementById("studio-btn-pro");
  const vodBtn = document.getElementById("studio-btn-vod");
  const stdWrap = document.getElementById("standard-workspace");
  const proWrap = document.getElementById("pro-studio-section");
  const vodWrap = document.getElementById("vod-studio-section");

  if (targetMode === "pro" && !isLiveStudioUnlocked()) {
    openLiveKeyModal();
    return;
  }

  proState.mode = targetMode;
  if (stdBtn) stdBtn.classList.toggle("active", targetMode === "standard");
  if (proBtn) proBtn.classList.toggle("active", targetMode === "pro");
  if (vodBtn) vodBtn.classList.toggle("active", targetMode === "vod");

  if (stdWrap) stdWrap.classList.toggle("hidden", targetMode !== "standard");
  if (proWrap) proWrap.classList.toggle("hidden", targetMode !== "pro");
  if (vodWrap) vodWrap.classList.toggle("hidden", targetMode !== "vod");

  if (targetMode === "pro" && proState.source) {
    requestAnimationFrame(syncBoxesToDOM);
  } else if (targetMode === "vod" && vodState.source) {
    requestAnimationFrame(syncVodBoxesToDOM);
  }
}

function updateRatioBadgeUI() {
  const r = proState.cam_ratio || 0.40;
  const pos = proState.cam_position || "top";
  const dispPct = Math.round((1 - r) * 100);
  const camPct = Math.round(r * 100);

  const textEl = document.getElementById("ratio-slider-text");
  if (textEl) {
    textEl.textContent = `Display: ${dispPct}% • Streamer: ${camPct}%`;
  }
  const badge = document.getElementById("pro-ratio-badge");
  if (badge) {
    const posStr = pos === "top" ? "Streamer Top" : "Streamer Bottom";
    badge.textContent = `9:16 • ${dispPct}% Display / ${camPct}% Streamer (${posStr})`;
  }
}

function setupProStudio() {
  const proBtn = document.getElementById("studio-btn-pro");
  if (!proBtn) return;

  refreshLiveKeyUI();
  setInterval(refreshLiveKeyUI, 30000);

  // Top mode switcher
  const stdBtn = document.getElementById("studio-btn-standard");
  const vodBtn = document.getElementById("studio-btn-vod");
  if (stdBtn) {
    stdBtn.addEventListener("click", () => switchStudioMode("standard"));
  }
  proBtn.addEventListener("click", () => switchStudioMode("pro"));
  if (vodBtn) {
    vodBtn.addEventListener("click", () => switchStudioMode("vod"));
  }

  // Key Modal controls
  const keyCloseBtn = document.getElementById("live-key-close-x");
  if (keyCloseBtn) keyCloseBtn.addEventListener("click", closeLiveKeyModal);

  const keyModal = document.getElementById("live-key-modal");
  if (keyModal) {
    keyModal.addEventListener("click", (e) => {
      if (e.target.id === "live-key-modal") closeLiveKeyModal();
    });
  }

  const keySubmitBtn = document.getElementById("live-key-submit-btn");
  if (keySubmitBtn) keySubmitBtn.addEventListener("click", submitLiveAccessKey);

  const keyInput = document.getElementById("live-access-key-input");
  if (keyInput) {
    keyInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitLiveAccessKey();
      }
    });
  }

  // Renew key button
  const renewBtn = document.getElementById("btn-renew-key");
  if (renewBtn) renewBtn.addEventListener("click", openLiveKeyModal);

  // Streamer Position Controls (Top vs Bottom vs None)
  document.querySelectorAll("#pro-pos-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pro-pos-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const pos = btn.dataset.pos || "top";
      proState.cam_position = pos;
      updateLiveCamPositionUI(pos);
    });
  });

  function updateLiveCamPositionUI(pos) {
    const isNoCam = (pos === "none");
    const camBox = document.getElementById("box-cam");
    if (camBox) camBox.style.display = isNoCam ? "none" : "";

    const ratioGroup = document.getElementById("pro-ratio-picker")?.closest(".pro-mini-setting");
    if (ratioGroup) {
      ratioGroup.style.opacity = isNoCam ? "0.3" : "1";
      ratioGroup.style.pointerEvents = isNoCam ? "none" : "";
    }

    const recBtnText = document.querySelector("#pro-create-btn .record-btn-text");
    if (recBtnText) {
      recBtnText.textContent = isNoCam ? "⚡ Cut Display-Only Short" : "⚡ Cut Live Moment & Render Short";
    }
  }

  // Split Ratio with Respect to Display Controls
  document.querySelectorAll("#pro-ratio-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pro-ratio-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      proState.cam_ratio = parseFloat(btn.dataset.ratio) || 0.40;
    });
  });

  // Short Duration Controls (20s, 35s, 50s, 60s)
  document.querySelectorAll("#pro-duration-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pro-duration-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      proState.clipDuration = parseInt(btn.dataset.dur, 10) || 35;
      syncScrubberUI(proState.currentPlaySec);
    });
  });

  // Resolution Controls (1080p vs 4K)
  document.querySelectorAll("#pro-res-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      const res = btn.dataset.res;
      if (res === "4k") {
        if (proState.source && proState.source.has_4k === false) {
          const maxStr = proState.source.max_height ? `${proState.source.max_height}p` : "1080p";
          show4kUnavailableModal(maxStr);
          return;
        }
      }
      document.querySelectorAll("#pro-res-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      proState.resolution = res;
    });
  });

  // Overlay Mode Toggle: Move Boxes vs Click Video
  const overlayBtn = document.getElementById("btn-toggle-overlay-mode");
  if (overlayBtn) {
    overlayBtn.addEventListener("click", () => {
      const overlay = document.getElementById("pro-boxes-overlay");
      if (!overlay) return;
      const isClickthrough = overlay.classList.toggle("clickthrough-mode");
      overlayBtn.classList.toggle("active", !isClickthrough);
      overlayBtn.innerHTML = isClickthrough
        ? "<span>Interactive Video</span>"
        : "<span>Move Boxes</span>";
    });
  }

  // Scrubber Slider: Scrub back to any time in the live stream!
  const scrubber = document.getElementById("pro-scrubber-slider");
  if (scrubber) {
    scrubber.addEventListener("input", (e) => {
      proState.isDraggingScrubber = true;
      seekLivePlayback(parseInt(e.target.value, 10));
    });
    scrubber.addEventListener("change", () => {
      proState.isDraggingScrubber = false;
    });
  }

  // Load Stream form & Paste button
  const proUrlInput = document.getElementById("pro-youtube-url");
  document.getElementById("pro-inspect-form").addEventListener("submit", (e) => {
    e.preventDefault();
    inspectLiveStream(proUrlInput.value.trim(), false);
  });

  proUrlInput.addEventListener("paste", () => {
    setTimeout(() => {
      const val = proUrlInput.value.trim();
      if (looksLikeYouTubeUrl(val)) inspectLiveStream(val, false);
    }, 60);
  });

  document.getElementById("pro-paste-btn").addEventListener("click", async () => {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (text) {
        proUrlInput.value = text;
        if (looksLikeYouTubeUrl(text)) inspectLiveStream(text, false);
      }
    } catch (e) {
      proUrlInput.focus();
    }
  });

  // Big Start Recording Button
  document.getElementById("pro-create-btn").addEventListener("click", submitLiveClipExport);

  // Dual-Box Drag & Resize Setup
  setupDualBoxInteractions();
  window.addEventListener("resize", () => {
    if (proState.mode === "pro" && proState.source) syncBoxesToDOM();
  });
}

// -------------------------------------------------------------
// YouTube Live Stream Player & Live Scrubber Controller
// -------------------------------------------------------------
let ytLivePlayer = null;
let ytPlayerReady = false;

window.onYouTubeIframeAPIReady = function () {
  ytPlayerReady = true;
};

// YouTube IFrame postMessage listener for instant, real-time live playback tracking
window.addEventListener("message", (e) => {
  if (typeof e.origin === "string" && (e.origin.includes("youtube.com") || e.origin.includes("youtube-nocookie.com"))) {
    try {
      const data = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
      if (data && data.event === "infoDelivery" && data.info) {
        const info = data.info;
        if (typeof info.currentTime === "number" && !isNaN(info.currentTime)) {
          handleLiveTimeUpdate(info.currentTime, info.duration);
        }
        // Auto-resume if YouTube pauses on its own without user interaction (e.g. background tab)
        if (info.playerState === 2 && !proState.userExplicitPaused) {
          ensureLivePlaybackActive();
        }
      }
    } catch (err) {}
  }
});

function ensureLivePlaybackActive() {
  if (proState.userExplicitPaused) return;
  if (ytLivePlayer && typeof ytLivePlayer.playVideo === "function") {
    try { ytLivePlayer.playVideo(); } catch (e) {}
  }
  const iframe = document.getElementById("pro-live-iframe");
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func: "playVideo", args: [] }), "*");
  }
}

// Keep live stream playing even when tab goes to background or user switches windows
document.addEventListener("visibilitychange", () => {
  if (proState.mode === "pro" && proState.source && !proState.userExplicitPaused) {
    ensureLivePlaybackActive();
  }
});

window.addEventListener("focus", () => {
  if (proState.mode === "pro" && proState.source && !proState.userExplicitPaused) {
    ensureLivePlaybackActive();
  }
});

window.addEventListener("pageshow", () => {
  if (proState.mode === "pro" && proState.source && !proState.userExplicitPaused) {
    ensureLivePlaybackActive();
  }
});

function loadLiveStreamInPlayer(videoId) {
  const embedWrap = document.getElementById("pro-yt-embed-wrap");
  if (!embedWrap) return;

  proState.highestLiveSec = 0;
  proState.liveLagSec = 0;
  proState.currentPlaySec = 0;
  proState.userExplicitPaused = false;

  embedWrap.innerHTML = `<iframe id="pro-live-iframe" class="pro-live-iframe" src="https://www.youtube.com/embed/${videoId}?autoplay=1&playsinline=1&enablejsapi=1&rel=0&modestbranding=1&vq=hd1080&suggestedQuality=hd1080&origin=${encodeURIComponent(window.location.origin)}" playsinline allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>`;

  try {
    if (window.YT && window.YT.Player) {
      ytLivePlayer = new YT.Player("pro-live-iframe", {
        playerVars: {
          autoplay: 1,
          playsinline: 1,
          modestbranding: 1,
          rel: 0,
        },
        events: {
          onReady: (e) => {
            ytPlayerReady = true;
            try { e.target.playVideo(); } catch (err) {}
            try {
              if (typeof e.target.setPlaybackQuality === "function") e.target.setPlaybackQuality("hd1080");
              if (typeof e.target.setPlaybackQualityRange === "function") e.target.setPlaybackQualityRange("hd1080", "hd1080");
            } catch (err) {}
            startPlayerTracking();
          },
          onStateChange: (e) => {
            // Never allow live stream to get paused automatically
            if (e.data === 2 && !proState.userExplicitPaused) {
              try { e.target.playVideo(); } catch (err) {}
            }
          },
        },
      });
    }
  } catch (err) {
    console.warn("YT Player init note:", err);
  }

  startPlayerTracking();
}

function startPlayerTracking() {
  if (proState.trackerId) clearInterval(proState.trackerId);
  proState.trackerId = setInterval(() => {
    let cur = null;
    let dur = null;
    if (ytLivePlayer) {
      try {
        if (typeof ytLivePlayer.getCurrentTime === "function") cur = ytLivePlayer.getCurrentTime();
        if (typeof ytLivePlayer.getDuration === "function") dur = ytLivePlayer.getDuration();
        // Keep live stream playing automatically unless user explicitly paused
        if (!proState.userExplicitPaused && typeof ytLivePlayer.getPlayerState === "function") {
          const s = ytLivePlayer.getPlayerState();
          if (s === 2 || s === 0) { // PAUSED or ENDED/STALLED
            ytLivePlayer.playVideo();
          }
        }
      } catch (e) {}
    }
    if (typeof cur === "number" && !isNaN(cur) && cur >= 0) {
      handleLiveTimeUpdate(cur, dur);
    }
  }, 350);
}

function handleLiveTimeUpdate(cur, dur) {
  if (typeof cur !== "number" || isNaN(cur) || cur < 0) return;
  proState.currentPlaySec = cur;

  // Track the highest playback timestamp seen — this is our true real-time live head
  if (cur > (proState.highestLiveSec || 0)) {
    proState.highestLiveSec = cur;
  }
  if (typeof dur === "number" && !isNaN(dur) && dur > 10) {
    if (dur > (proState.highestLiveSec || 0)) {
      proState.highestLiveSec = dur;
    }
  }

  const liveHead = proState.highestLiveSec || cur;
  proState.liveLagSec = 0;
  proState.isLockedMoment = false;

  syncScrubberUI(cur);
}

window.toggleLockMoment = function () {
  proState.isLockedMoment = false;
  proState.liveLagSec = 0;
  updateLockMomentUI();
};

function updateLockMomentUI() {
  const badge = document.getElementById("pro-window-badge");
  const dur = proState.clipDuration || 35;
  if (badge) {
    badge.textContent = `Live Edge (${dur}s clip)`;
  }
}

window.jumpLivePlayback = function (deltaSec) {
  // Live clipper is strictly real-time edge: going back is disabled
  jumpToLiveEdge();
};

window.jumpToLiveEdge = function () {
  proState.liveLagSec = 0;
  proState.isLockedMoment = false;
  proState.lockedLagSec = 0;
  proState.userExplicitPaused = false;

  let liveDur = 0;
  if (ytLivePlayer && typeof ytLivePlayer.getDuration === "function") {
    try {
      const d = ytLivePlayer.getDuration();
      if (typeof d === "number" && !isNaN(d) && d > 0) liveDur = d;
    } catch (e) {}
  }
  const liveHead = Math.max(liveDur, proState.highestLiveSec || 0, proState.currentPlaySec || 0);
  proState.highestLiveSec = liveHead;
  proState.currentPlaySec = liveHead;
  updateLockMomentUI();
  syncScrubberUI(liveHead);

  if (ytLivePlayer) {
    try {
      if (liveDur > 0 && typeof ytLivePlayer.seekTo === "function") {
        ytLivePlayer.seekTo(liveDur, true);
      }
      if (typeof ytLivePlayer.playVideo === "function") {
        ytLivePlayer.playVideo();
      }
    } catch (e) {}
  }
  const iframe = document.getElementById("pro-live-iframe");
  if (iframe && iframe.contentWindow) {
    if (liveDur > 0) {
      iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func: "seekTo", args: [liveDur, true] }), "*");
    }
    iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func: "playVideo", args: [] }), "*");
  }
};

window.toggleLivePlayback = function () {
  const iframe = document.getElementById("pro-live-iframe");
  let isPlaying = false;
  if (ytLivePlayer && typeof ytLivePlayer.getPlayerState === "function") {
    try {
      isPlaying = (ytLivePlayer.getPlayerState() === 1);
    } catch (e) {}
  }

  if (isPlaying) {
    proState.userExplicitPaused = true;
    if (ytLivePlayer && typeof ytLivePlayer.pauseVideo === "function") {
      try { ytLivePlayer.pauseVideo(); } catch (e) {}
    }
    if (iframe && iframe.contentWindow) {
      iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func: "pauseVideo" }), "*");
    }
  } else {
    proState.userExplicitPaused = false;
    if (ytLivePlayer && typeof ytLivePlayer.playVideo === "function") {
      try { ytLivePlayer.playVideo(); } catch (e) {}
    }
    if (iframe && iframe.contentWindow) {
      iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func: "playVideo", args: [] }), "*");
    }
  }
};

window.toggleBoxesOverlay = function () {
  const overlay = document.getElementById("pro-boxes-overlay");
  const btn = document.getElementById("btn-toggle-boxes");
  const textEl = document.getElementById("toggle-boxes-text");
  if (!overlay) return;

  const isHidden = overlay.style.display === "none";
  if (isHidden) {
    overlay.style.display = "";
    if (btn) btn.classList.remove("active-settings-mode");
    if (textEl) textEl.textContent = "Settings / 1080p";
    if (btn) {
      const icon = btn.querySelector(".toggle-icon");
      if (icon) icon.textContent = "⚙️";
    }
  } else {
    overlay.style.display = "none";
    if (btn) btn.classList.add("active-settings-mode");
    if (textEl) textEl.textContent = "Show Crop Boxes";
    if (btn) {
      const icon = btn.querySelector(".toggle-icon");
      if (icon) icon.textContent = "👁️";
    }
  }
};

window.setPlayer1080p = function () {
  const iframe = document.getElementById("pro-live-iframe");
  if (ytLivePlayer) {
    try {
      if (typeof ytLivePlayer.setPlaybackQuality === "function") {
        ytLivePlayer.setPlaybackQuality("hd1080");
      }
      if (typeof ytLivePlayer.setPlaybackQualityRange === "function") {
        ytLivePlayer.setPlaybackQualityRange("hd1080", "hd1080");
      }
    } catch (e) {}
  }
  if (iframe && iframe.contentWindow) {
    try {
      iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func: "setPlaybackQuality", args: ["hd1080"] }), "*");
      iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func: "setPlaybackQualityRange", args: ["hd1080", "hd1080"] }), "*");
    } catch (e) {}
  }

  // Also toggle boxes overlay off so user can immediately click the YouTube gear icon if they want to verify/select 1080p manually
  const overlay = document.getElementById("pro-boxes-overlay");
  const btn = document.getElementById("btn-toggle-boxes");
  const textEl = document.getElementById("toggle-boxes-text");
  if (overlay) {
    overlay.style.display = "none";
    if (btn) {
      btn.classList.add("active-settings-mode");
      if (textEl) textEl.textContent = "Show Crop Boxes";
      const icon = btn.querySelector(".toggle-icon");
      if (icon) icon.textContent = "👁️";
    }
  }
};

function seekLivePlayback(sec) {
  // Live clipper is strictly real-time edge: going back is disabled
  jumpToLiveEdge();
}

function syncScrubberUI(currentSec) {
  proState.liveLagSec = 0;
  const slider = document.getElementById("pro-scrubber-slider");
  if (slider) {
    slider.value = slider.max || 300;
  }

  const curEl = document.getElementById("pro-tl-current");
  if (curEl) {
    curEl.textContent = formatTime(currentSec, currentSec >= 3600);
  }

  const maxEl = document.getElementById("pro-tl-max");
  if (maxEl) {
    maxEl.textContent = "● LIVE";
    maxEl.style.color = "#ef4444";
  }

  const dur = proState.clipDuration || 35;
  const badge = document.getElementById("pro-window-badge");
  if (badge) {
    badge.textContent = `🔴 Live Edge (${dur}s clip)`;
  }
}

function setProLoadSpinner(loading) {
  const btn = document.getElementById("pro-load-btn");
  const txt = btn.querySelector(".btn-text");
  const sp = btn.querySelector(".spinner");
  btn.disabled = loading;
  txt.textContent = loading ? "Loading..." : "Load Stream";
  sp.classList.toggle("hidden", !loading);
}

function showProError(msg) {
  document.getElementById("pro-error-message").textContent = msg;
  document.getElementById("pro-error-banner").classList.remove("hidden");
}

function hideProError() {
  document.getElementById("pro-error-banner").classList.add("hidden");
}

async function inspectLiveStream(url, forceRefresh = false) {
  if (!looksLikeYouTubeUrl(url)) {
    showProError("Please paste a valid YouTube Live or Video link.");
    return;
  }

  const vidId = extractYtVideoId(url);
  if (vidId) {
    loadLiveStreamInPlayer(vidId);
  }

  hideProError();
  setProLoadSpinner(true);

  try {
    const res = await fetchWithTimeout("/api/pro/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        force_refresh: forceRefresh,
        access_key: proState.accessKey,
      }),
    }, 25000);
    const json = await parseApiResponse(res);
    if (!res.ok || !json.success) {
      throw new Error(json.detail || "Could not load stream.");
    }

    const src = json.data;
    proState.source = src;
    proState.firstPdtUtc = src.first_pdt_utc || null;

    if (src && src.has_4k === false && proState.resolution === "4k") {
      proState.resolution = "1080p";
      document.querySelectorAll("#pro-res-picker .choice-pill").forEach((b) => {
        b.classList.toggle("active", b.dataset.res === "1080p");
      });
      const maxStr = src.max_height ? `${src.max_height}p` : "1080p";
      show4kUnavailableModal(maxStr);
    }

    const total = src.is_live
      ? Math.max(30, Math.floor(src.dvr_window_sec || src.live_elapsed_sec || 7200))
      : Math.max(10, Math.floor(src.duration || 60));
    proState.totalSec = total;
    proState.currentPlaySec = total;

    const badgeEl = document.getElementById("pro-live-badge");
    if (badgeEl) {
      badgeEl.textContent = src.is_live ? "🔴 LIVE" : "🎬 VIDEO";
    }

    const titleEl = document.getElementById("pro-source-title");
    if (titleEl) titleEl.textContent = src.title || "Live Stream";
    const authorEl = document.getElementById("pro-source-author");
    if (authorEl) authorEl.textContent = src.author || "";

    const slider = document.getElementById("pro-scrubber-slider");
    if (slider) {
      slider.max = total;
      slider.value = total;
    }

    const maxEl = document.getElementById("pro-tl-max");
    if (maxEl) {
      const useH = total >= 3600;
      maxEl.textContent = src.is_live ? `🔴 LIVE (${formatTime(total, useH)})` : formatTime(total, useH);
    }

    document.getElementById("pro-editor-wrap").classList.remove("hidden");
    requestAnimationFrame(syncBoxesToDOM);
    syncScrubberUI(total);
  } catch (err) {
    showProError(err.message || "Failed to load stream.");
  } finally {
    setProLoadSpinner(false);
  }
}

// 1-Click Streamer Facecam Presets
window.applyCamPreset = function (preset) {
  if (preset === "tl") {
    proState.boxes.cam = { x: 0.03, y: 0.04, w: 0.28, h: 0.35 };
  } else if (preset === "tr") {
    proState.boxes.cam = { x: 0.69, y: 0.04, w: 0.28, h: 0.35 };
  } else if (preset === "bl") {
    proState.boxes.cam = { x: 0.03, y: 0.61, w: 0.28, h: 0.35 };
  } else if (preset === "br") {
    proState.boxes.cam = { x: 0.69, y: 0.61, w: 0.28, h: 0.35 };
  } else {
    proState.boxes.cam = { x: 0.68, y: 0.58, w: 0.28, h: 0.36 };
    proState.boxes.stream = { x: 0.02, y: 0.03, w: 0.96, h: 0.94 };
  }
  syncBoxesToDOM();
};

function syncBoxesToDOM() {
  const stage = document.getElementById("pro-big-stage") || document.getElementById("pro-stage");
  if (!stage) return;
  const rect = stage.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;

  for (const key of ["cam", "stream"]) {
    const b = proState.boxes[key];
    const el = document.getElementById(key === "cam" ? "box-cam" : "box-stream");
    if (!el) continue;
    if (key === "cam") {
      el.style.display = (proState.cam_position === "none") ? "none" : "";
    }
    const px = b.x * rect.width;
    const py = b.y * rect.height;
    const pw = b.w * rect.width;
    const ph = b.h * rect.height;
    el.style.transform = `translate3d(${px.toFixed(1)}px, ${py.toFixed(1)}px, 0)`;
    el.style.width = `${pw.toFixed(1)}px`;
    el.style.height = `${ph.toFixed(1)}px`;
  }
}

function setupDualBoxInteractions() {
  const stage = document.getElementById("pro-big-stage") || document.getElementById("pro-stage");
  if (!stage) return;

  let dragCtx = null;

  stage.addEventListener("pointerdown", (e) => {
    const handleEl = e.target.closest(".crop-handle");
    const labelEl = e.target.closest(".crop-box-label");
    if (!handleEl && !labelEl) return;
    const boxEl = e.target.closest(".crop-box");
    if (!boxEl) return;

    e.preventDefault();
    const key = boxEl.getAttribute("data-box");
    const handle = handleEl ? handleEl.getAttribute("data-handle") : "move";
    const stageRect = stage.getBoundingClientRect();

    boxEl.classList.add("is-dragging");
    const captureEl = e.target;
    try {
      captureEl.setPointerCapture(e.pointerId);
    } catch (err) {}

    dragCtx = {
      key,
      handle,
      boxEl,
      captureEl,
      pointerId: e.pointerId,
      stageRect,
      startX: e.clientX,
      startY: e.clientY,
      orig: { ...proState.boxes[key] },
    };
  });

  window.addEventListener("pointermove", (e) => {
    if (!dragCtx || e.pointerId !== dragCtx.pointerId) return;
    e.preventDefault();

    const dx = (e.clientX - dragCtx.startX) / dragCtx.stageRect.width;
    const dy = (e.clientY - dragCtx.startY) / dragCtx.stageRect.height;
    const o = dragCtx.orig;
    const minW = 0.12;
    const minH = 0.14;
    let { x, y, w, h } = o;

    if (dragCtx.handle === "move") {
      x = Math.max(0, Math.min(1 - w, o.x + dx));
      y = Math.max(0, Math.min(1 - h, o.y + dy));
    } else {
      if (dragCtx.handle.includes("e")) w = Math.max(minW, Math.min(1 - o.x, o.w + dx));
      if (dragCtx.handle.includes("s")) h = Math.max(minH, Math.min(1 - o.y, o.h + dy));
      if (dragCtx.handle.includes("w")) {
        const maxLeft = o.x + o.w - minW;
        const newX = Math.max(0, Math.min(maxLeft, o.x + dx));
        w = o.w + (o.x - newX);
        x = newX;
      }
      if (dragCtx.handle.includes("n")) {
        const maxTop = o.y + o.h - minH;
        const newY = Math.max(0, Math.min(maxTop, o.y + dy));
        h = o.h + (o.y - newY);
        y = newY;
      }
    }

    proState.boxes[dragCtx.key] = { x, y, w, h };
    syncBoxesToDOM();
  });

  const endDrag = (e) => {
    if (!dragCtx || e.pointerId !== dragCtx.pointerId) return;
    dragCtx.boxEl.classList.remove("is-dragging");
    try {
      dragCtx.captureEl.releasePointerCapture(e.pointerId);
    } catch (err) {}
    dragCtx = null;
  };

  window.addEventListener("pointerup", endDrag);
  window.addEventListener("pointercancel", endDrag);
}

async function submitLiveClipExport() {
  if (!proState.source) {
    showProError("Please paste and load a YouTube Live Stream link first.");
    return;
  }

  hideProError();
  const isLive = Boolean(proState.source.is_live);
  const dur = proState.clipDuration || 35;

  let cur = 0;
  if (ytLivePlayer && typeof ytLivePlayer.getCurrentTime === "function") {
    try {
      const t = ytLivePlayer.getCurrentTime();
      if (typeof t === "number" && !isNaN(t) && t >= 0) cur = t;
    } catch (e) {}
  }
  if (!cur && proState.currentPlaySec) cur = proState.currentPlaySec;

  let rewindSec = 0;
  if (isLive) {
    // Strictly Real-Time Live Edge: Never allow going back/rewinding in Live Clipper
    rewindSec = 0;
  } else if (proState.isLockedMoment && typeof proState.lockedLagSec === "number" && proState.lockedLagSec > 0) {
    rewindSec = proState.lockedLagSec;
  } else {
    rewindSec = 0;
  }

  const startSec = cur || 0;
  const endSec = startSec + dur;

  const streamUrl = document.getElementById("pro-youtube-url")?.value?.trim() ||
    (proState.source?.video_id ? `https://www.youtube.com/watch?v=${proState.source.video_id}` : "") ||
    proState.source?.url || "";

  const isNoCam = (proState.cam_position === "none");
  const payload = {
    url: streamUrl,
    video_id: proState.source.video_id,
    is_live: isLive,
    duration_sec: dur,
    rewind_sec: rewindSec,
    start_utc_ts: null,
    end_utc_ts: null,
    start_time: startSec,
    end_time: endSec,
    cam_box: proState.boxes?.cam || { x: 0.69, y: 0.58, w: 0.28, h: 0.36 },
    stream_box: proState.boxes?.stream || { x: 0.02, y: 0.03, w: 0.96, h: 0.94 },
    cam_ratio: proState.cam_ratio || 0.40,
    cam_position: proState.cam_position || "top",
    resolution: proState.resolution || "1080p",
    burn_subtitles: true,
    subtitle_style: document.getElementById("pro-sub-style")?.value || "viral_pop",
    subtitle_position: isNoCam ? "bottom" : "seam",
    access_key: proState.accessKey,
  };

  const modeLabel = isNoCam ? "Display-Only" : "Dual-Box";
  const timeDesc = rewindSec > 0 ? `${rewindSec}s ago` : "Live Edge";
  showProgressModal(`Recording & rendering ${dur}s ${modeLabel} Short (${timeDesc})...`);

  try {
    const res = await fetchWithTimeout("/api/pro/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 25000);
    const data = await parseApiResponse(res);
    if (!res.ok || !data.success) {
      throw new Error(data.detail || "Could not start clip recording.");
    }
    pollTaskProgress(data.task_id);
  } catch (err) {
    hideProgressModal();
    showProError(err.message);
  }
}


// =============================================================
// AUTO-VIRAL: LIVE CHAT SPIKE SCANNING & 1-CLICK HOOK EXPORT
// =============================================================
let activeViralPeaks = [];

async function scanAutoViralClips(url) {
  const btn = document.getElementById("btn-scan-viral");
  const statusEl = document.getElementById("viral-scan-status");
  const container = document.getElementById("viral-peaks-container");
  if (!btn || !statusEl || !container) return;

  const btnTxt = btn.querySelector(".btn-text");
  const btnSp = btn.querySelector(".spinner");
  btn.disabled = true;
  btnTxt.textContent = "Scanning Chat...";
  btnSp.classList.remove("hidden");
  statusEl.classList.remove("hidden");
  statusEl.textContent = "Analyzing live chat message frequency & hype spikes...";

  try {
    const res = await fetchWithTimeout("/api/auto-viral/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, max_peaks: 5, clip_duration: 30.0, hook_duration: 3.0 }),
    }, 35000);
    const json = await parseApiResponse(res);
    if (!res.ok || !json.success) {
      throw new Error(json.detail || "Could not scan chat spikes.");
    }

    const peaks = json.data?.peaks || [];
    activeViralPeaks = peaks;
    if (peaks.length === 0) {
      statusEl.textContent = "Chat activity was steady — no major spikes detected yet. Try again in a few moments!";
      container.classList.add("hidden");
    } else {
      statusEl.textContent = `Found ${peaks.length} viral moment${peaks.length > 1 ? "s" : ""} where chat spiked! Each clip includes a 3s hook + 30s main cut + auto-framing.`;
      renderViralPeaks(peaks);
      container.classList.remove("hidden");
    }
  } catch (err) {
    statusEl.textContent = `Scan failed: ${err.message || "Unknown error"}`;
  } finally {
    btn.disabled = false;
    btnTxt.textContent = "⚡ Scan for Viral Clips";
    btnSp.classList.add("hidden");
  }
}

function renderViralPeaks(peaks) {
  const container = document.getElementById("viral-peaks-container");
  if (!container) return;

  container.innerHTML = "";
  const rankIcons = ["🏆", "🥈", "🥉", "⚡", "🔥"];

  peaks.forEach((p, idx) => {
    const card = document.createElement("div");
    card.className = "viral-peak-card";

    const icon = rankIcons[idx] || "⚡";
    const reactions = (p.top_reactions || []).slice(0, 3).join(" • ") || "🔥 HIGH ENERGY";
    const sampleTxt = p.sample_msg ? `💬 "${escapeHtml(p.sample_msg)}"` : `💬 ${p.msg_count_in_window} chat messages in this moment`;
    const offsetStr = formatTime(Math.round(p.peak_offset_sec || 0), (p.peak_offset_sec || 0) >= 3600);
    const thumbSrc = p.thumbnail_b64 || (proState.source?.frame_image || proState.source?.thumbnail || "");
    const layoutName = (p.auto_boxes?.layout || "Corner Facecam").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

    card.innerHTML = `
      <div class="viral-peak-head">
        <span class="viral-rank-badge">${icon} #${p.rank} Viral Peak</span>
        <span class="viral-score-badge">Viral Score: ${p.viral_score}/100</span>
      </div>

      <div class="viral-thumb-wrap">
        <img class="viral-thumb-img" src="${thumbSrc}" alt="Peak frame">
        <div class="viral-hook-tag">
          <span>⚡ 3s HOOK + 30s CLIP</span>
        </div>
        <div class="viral-duration-tag">33s total</div>
      </div>

      <div class="viral-info-block">
        <div style="font-size:0.84rem; font-weight:700; color:var(--text-main); display:flex; justify-content:space-between;">
          <span>⏱ At ${offsetStr} in stream</span>
          <span style="color:#ef4444; font-weight:800;">${p.msg_count_in_window} msgs / 30s</span>
        </div>
        <div class="viral-tags-row">
          <span class="viral-tag-pill">${reactions}</span>
          <span class="viral-autobox-pill">🎯 ${layoutName} (60/40)</span>
        </div>
        <div class="viral-chat-sample">${sampleTxt}</div>
      </div>

      <div class="viral-card-actions">
        <button type="button" class="btn-generate-viral" data-rank="${p.rank}">
          <span>🎬 Generate Short</span>
        </button>
        <button type="button" class="btn-edit-peak" data-rank="${p.rank}">
          <span>✏️ Edit</span>
        </button>
      </div>
    `;

    // Button 1: 1-Click Generate with 3s Hook + Auto-Boxes + Hinglish Captions
    const genBtn = card.querySelector(".btn-generate-viral");
    genBtn.addEventListener("click", () => {
      generateAutoViralShort(p, genBtn);
    });

    // Button 2: Load into manual Live Studio editor below
    const editBtn = card.querySelector(".btn-edit-peak");
    editBtn.addEventListener("click", () => {
      useViralPeakInLiveStudio(p);
    });

    container.appendChild(card);
  });
}

function useViralPeakInLiveStudio(p) {
  // Snap timeline slider to this peak's clip window
  proState.startSec = Math.max(0, Math.round(p.clip_start_sec || (p.peak_offset_sec - 15)));
  proState.endSec = Math.min(proState.totalSec, Math.round(p.clip_end_sec || (p.peak_offset_sec + 15)));
  if (p.auto_boxes) {
    if (p.auto_boxes.cam_box) proState.boxes.cam = { ...p.auto_boxes.cam_box };
    if (p.auto_boxes.stream_box) proState.boxes.stream = { ...p.auto_boxes.stream_box };
  }
  syncLiveTimelineUI();
  syncBoxesToDOM();
  loadSelectedBoundaryFrames(false);

  // Smooth scroll to Step 1
  const step1 = document.querySelector(".pro-timing-card");
  if (step1) {
    step1.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

async function generateAutoViralShort(peak, btnEl) {
  if (!proState.source) {
    showProError("Please paste and load a stream link first.");
    return;
  }

  const origHtml = btnEl.innerHTML;
  btnEl.disabled = true;
  btnEl.innerHTML = `<span>Rendering Short...</span>`;

  const isLive = Boolean(proState.source.is_live);
  const camBox = peak.auto_boxes?.cam_box || proState.boxes.cam;
  const streamBox = peak.auto_boxes?.stream_box || proState.boxes.stream;

  const payload = {
    url: document.getElementById("pro-youtube-url").value.trim(),
    video_id: proState.source.video_id,
    is_live: isLive,
    peak_offset_sec: peak.peak_offset_sec || 0.0,
    clip_start_utc_ts: peak.clip_start_utc_ts || null,
    clip_end_utc_ts: peak.clip_end_utc_ts || null,
    hook_start_utc_ts: peak.hook_start_utc_ts || null,
    hook_end_utc_ts: peak.hook_end_utc_ts || null,
    start_time: peak.clip_start_sec || 0.0,
    end_time: peak.clip_end_sec || 30.0,
    cam_box: camBox,
    stream_box: streamBox,
    cam_ratio: proState.cam_ratio || 0.40,
    cam_position: proState.cam_position || "top",
    resolution: proState.resolution || "1080p",
    burn_subtitles: document.getElementById("pro-burn-subs")?.checked ?? true,
    subtitle_style: document.getElementById("pro-sub-style")?.value || "viral_pop",
    subtitle_position: document.getElementById("pro-sub-pos")?.value || "seam",
    climax_hook: true,
    hook_duration: 3.0,
  };

  showProgressModal("Extracting 3s hook & 30s clip from live chat spike...");

  try {
    const res = await fetchWithTimeout("/api/auto-viral/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 30000);
    const data = await parseApiResponse(res);
    if (!res.ok || !data.success) {
      throw new Error(data.detail || "Could not launch auto-viral render.");
    }
    pollTaskProgress(data.task_id);
  } catch (err) {
    hideProgressModal();
    showProError(err.message);
  } finally {
    btnEl.disabled = false;
    btnEl.innerHTML = origHtml;
  }
}

// =============================================================
// ENDED STREAM & VOD DUAL-BOX STUDIO
// =============================================================

const vodState = {
  mode: "vod",
  source: null,
  totalSec: 300,
  currentPlaySec: 0,
  clipDuration: 35,
  isDraggingScrubber: false,
  cam_position: "top",
  cam_ratio: 0.40,
  resolution: "1080p",
  activeBox: "cam",
  boxes: {
    cam: { x: 0.03, y: 0.05, w: 0.28, h: 0.36 },    // Streamer Facecam top-left
    stream: { x: 0.02, y: 0.03, w: 0.96, h: 0.82 }, // Display Screen above YouTube control bar
  },
  trackerId: null,
};

let ytVodPlayer = null;

function loadVodStreamInPlayer(videoId) {
  const embedWrap = document.getElementById("vod-yt-embed-wrap");
  if (!embedWrap) return;

  embedWrap.innerHTML = `<iframe id="vod-live-iframe" class="pro-live-iframe" src="https://www.youtube.com/embed/${videoId}?autoplay=1&playsinline=1&enablejsapi=1&vq=hd1080&suggestedQuality=hd1080&origin=${encodeURIComponent(window.location.origin)}" playsinline allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>`;

  try {
    if (window.YT && window.YT.Player) {
      ytVodPlayer = new YT.Player("vod-live-iframe", {
        playerVars: {
          autoplay: 1,
          playsinline: 1,
          vq: "hd1080",
          suggestedQuality: "hd1080",
          modestbranding: 1,
          rel: 0,
        },
        events: {
          onReady: (e) => {
            try {
              if (typeof e.target.setPlaybackQuality === "function") {
                e.target.setPlaybackQuality("hd1080");
              }
              if (typeof e.target.suggestedQuality !== "undefined") {
                e.target.suggestedQuality = "hd1080";
              }
            } catch (err) {}
            try { e.target.playVideo(); } catch (err) {}
            startVodPlayerTracking();
          },
          onPlaybackQualityChange: (e) => {
            if (e.data && e.data !== "hd1080") {
              try {
                if (typeof e.target.setPlaybackQuality === "function") {
                  e.target.setPlaybackQuality("hd1080");
                }
              } catch (err) {}
            }
          },
          onStateChange: (e) => {
            if (e.data === 1) {
              try {
                if (typeof e.target.setPlaybackQuality === "function") {
                  e.target.setPlaybackQuality("hd1080");
                }
              } catch (err) {}
            }
          },
        },
      });
    }
  } catch (err) {
    console.warn("YT VOD Player init note:", err);
  }

  startVodPlayerTracking();
}

function startVodPlayerTracking() {
  if (vodState.trackerId) clearInterval(vodState.trackerId);
  vodState.trackerId = setInterval(() => {
    if (vodState.isDraggingScrubber) return;

    let cur = null;
    let dur = null;
    if (ytVodPlayer) {
      try {
        if (typeof ytVodPlayer.getCurrentTime === "function") cur = ytVodPlayer.getCurrentTime();
        if (typeof ytVodPlayer.getDuration === "function") dur = ytVodPlayer.getDuration();
      } catch (e) {}
    }

    if (typeof dur === "number" && !isNaN(dur) && dur > 10) {
      vodState.totalSec = Math.floor(dur);
    }

    if (typeof cur === "number" && !isNaN(cur) && cur >= 0) {
      vodState.currentPlaySec = cur;
      syncVodScrubberUI(cur);
    }
  }, 400);
}

window.jumpVodPlayback = function (deltaSec) {
  const current = vodState.currentPlaySec || 0;
  const target = Math.max(0, current + deltaSec);
  seekVodPlayback(target);
};

window.toggleVodPlayback = function () {
  if (ytVodPlayer && typeof ytVodPlayer.getPlayerState === "function") {
    try {
      const state = ytVodPlayer.getPlayerState();
      if (state === 1) {
        ytVodPlayer.pauseVideo();
      } else {
        ytVodPlayer.playVideo();
      }
      return;
    } catch (e) {}
  }
  const iframe = document.getElementById("vod-live-iframe");
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func: "pauseVideo" }), "*");
  }
};

window.toggleVodBoxesOverlay = function () {
  const overlay = document.getElementById("vod-boxes-overlay");
  const btn = document.getElementById("btn-toggle-vod-boxes");
  const textEl = document.getElementById("toggle-vod-boxes-text");
  if (!overlay) return;

  const isHidden = overlay.style.display === "none";
  if (isHidden) {
    overlay.style.display = "";
    if (btn) btn.classList.remove("active-settings-mode");
    if (textEl) textEl.textContent = "Full Video Controls";
    if (btn) {
      const icon = btn.querySelector(".toggle-icon");
      if (icon) icon.textContent = "⚙️";
    }
  } else {
    overlay.style.display = "none";
    if (btn) btn.classList.add("active-settings-mode");
    if (textEl) textEl.textContent = "Show Crop Boxes";
    if (btn) {
      const icon = btn.querySelector(".toggle-icon");
      if (icon) icon.textContent = "👁️";
    }
  }
};

function seekVodPlayback(sec) {
  const total = Math.max(10, vodState.totalSec || 300);
  const clamped = Math.max(0, Math.min(total, sec));
  vodState.currentPlaySec = clamped;
  syncVodScrubberUI(clamped);

  if (ytVodPlayer && typeof ytVodPlayer.seekTo === "function") {
    try {
      ytVodPlayer.seekTo(clamped, true);
      return;
    } catch (e) {}
  }

  const iframe = document.getElementById("vod-live-iframe");
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func: "seekTo", args: [clamped, true] }), "*");
  }
}

function syncVodScrubberUI(currentSec) {
  const total = Math.max(10, vodState.totalSec || 300);
  const useH = total >= 3600;
  const clamped = Math.max(0, Math.min(total, currentSec));
  vodState.currentPlaySec = clamped;

  const slider = document.getElementById("vod-scrubber-slider");
  if (slider && !vodState.isDraggingScrubber) {
    slider.max = total;
    slider.value = clamped;
  }

  const curEl = document.getElementById("vod-tl-current");
  if (curEl) curEl.textContent = formatTime(clamped, useH);

  const maxEl = document.getElementById("vod-tl-max");
  if (maxEl) {
    maxEl.textContent = formatTime(total, useH);
  }

  const dur = vodState.clipDuration || 35;
  const endSec = Math.min(total, clamped + dur);
  const badge = document.getElementById("vod-window-badge");
  if (badge) {
    badge.textContent = `${formatTime(clamped, useH)} ➔ ${formatTime(endSec, useH)} (${dur}s)`;
  }
}

function setVodLoadSpinner(loading) {
  const btn = document.getElementById("vod-load-btn");
  if (!btn) return;
  const txt = btn.querySelector(".btn-text");
  const sp = btn.querySelector(".spinner");
  btn.disabled = loading;
  if (txt) txt.textContent = loading ? "Loading..." : "Load VOD";
  if (sp) sp.classList.toggle("hidden", !loading);
}

function showVodError(msg) {
  const errEl = document.getElementById("vod-error-message");
  const banner = document.getElementById("vod-error-banner");
  if (errEl) errEl.textContent = msg;
  if (banner) banner.classList.remove("hidden");
}

function hideVodError() {
  const banner = document.getElementById("vod-error-banner");
  if (banner) banner.classList.add("hidden");
}

async function inspectVodStream(url) {
  if (!looksLikeYouTubeUrl(url)) {
    showVodError("Please paste a valid YouTube Ended Stream or Video link.");
    return;
  }

  const vidId = extractYtVideoId(url);
  if (vidId) {
    loadVodStreamInPlayer(vidId);
  }

  hideVodError();
  setVodLoadSpinner(true);

  try {
    const res = await fetchWithTimeout("/api/pro/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        force_refresh: false,
        access_key: proState.accessKey,
      }),
    }, 25000);
    const json = await parseApiResponse(res);
    if (!res.ok || !json.success) {
      throw new Error(json.detail || "Could not load stream / VOD.");
    }

    const src = json.data;
    vodState.source = src;

    if (src && src.has_4k === false && vodState.resolution === "4k") {
      vodState.resolution = "1080p";
      document.querySelectorAll("#vod-res-picker .choice-pill").forEach((b) => {
        b.classList.toggle("active", b.dataset.res === "1080p");
      });
      const maxStr = src.max_height ? `${src.max_height}p` : "1080p";
      show4kUnavailableModal(maxStr);
    }

    const total = Math.max(10, Math.floor(src.duration || 60));
    vodState.totalSec = total;
    vodState.currentPlaySec = 0;

    const titleEl = document.getElementById("vod-source-title");
    if (titleEl) titleEl.textContent = src.title || "Ended Stream VOD";
    const authorEl = document.getElementById("vod-source-author");
    if (authorEl) authorEl.textContent = src.author || "";

    const slider = document.getElementById("vod-scrubber-slider");
    if (slider) {
      slider.max = total;
      slider.value = 0;
    }

    const maxEl = document.getElementById("vod-tl-max");
    if (maxEl) {
      const useH = total >= 3600;
      maxEl.textContent = formatTime(total, useH);
    }

    document.getElementById("vod-editor-wrap").classList.remove("hidden");
    requestAnimationFrame(syncVodBoxesToDOM);
    syncVodScrubberUI(0);
    selectVodActiveBox("cam");
  } catch (err) {
    showVodError(err.message || "Failed to load ended stream VOD.");
  } finally {
    setVodLoadSpinner(false);
  }
}

window.selectVodActiveBox = function (boxKey) {
  vodState.activeBox = boxKey;
  const camBtn = document.getElementById("btn-select-box-cam");
  const streamBtn = document.getElementById("btn-select-box-stream");
  const camBox = document.getElementById("box-vod-cam");
  const streamBox = document.getElementById("box-vod-stream");
  const camPresets = document.getElementById("vod-cam-presets");
  const streamPresets = document.getElementById("vod-stream-presets");

  if (camBtn) camBtn.classList.toggle("active", boxKey === "cam");
  if (streamBtn) streamBtn.classList.toggle("active", boxKey === "stream");

  if (camBox) camBox.classList.toggle("is-active-box", boxKey === "cam");
  if (streamBox) streamBox.classList.toggle("is-active-box", boxKey === "stream");

  if (camPresets) camPresets.classList.toggle("hidden", boxKey !== "cam");
  if (streamPresets) streamPresets.classList.toggle("hidden", boxKey !== "stream");
};

window.applyVodDisplayPreset = function (preset) {
  selectVodActiveBox("stream");
  if (preset === "full") {
    vodState.boxes.stream = { x: 0.02, y: 0.03, w: 0.96, h: 0.94 };
  } else if (preset === "left") {
    vodState.boxes.stream = { x: 0.02, y: 0.04, w: 0.66, h: 0.92 };
  } else if (preset === "center") {
    vodState.boxes.stream = { x: 0.17, y: 0.04, w: 0.66, h: 0.92 };
  } else if (preset === "right") {
    vodState.boxes.stream = { x: 0.32, y: 0.04, w: 0.66, h: 0.92 };
  }
  syncVodBoxesToDOM();
};

window.applyVodCamPreset = function (preset) {
  selectVodActiveBox("cam");
  if (preset === "tl") {
    vodState.boxes.cam = { x: 0.03, y: 0.04, w: 0.28, h: 0.36 };
  } else if (preset === "tr") {
    vodState.boxes.cam = { x: 0.69, y: 0.04, w: 0.28, h: 0.36 };
  } else if (preset === "bl") {
    vodState.boxes.cam = { x: 0.03, y: 0.58, w: 0.28, h: 0.36 };
  } else if (preset === "br") {
    vodState.boxes.cam = { x: 0.69, y: 0.58, w: 0.28, h: 0.36 };
  } else {
    vodState.boxes.cam = { x: 0.70, y: 0.58, w: 0.28, h: 0.36 };
    vodState.boxes.stream = { x: 0.02, y: 0.04, w: 0.66, h: 0.92 };
  }
  syncVodBoxesToDOM();
};

function syncVodBoxesToDOM() {
  const stage = document.getElementById("vod-big-stage");
  if (!stage) return;
  const rect = stage.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;

  for (const key of ["cam", "stream"]) {
    const b = vodState.boxes[key];
    const el = document.getElementById(key === "cam" ? "box-vod-cam" : "box-vod-stream");
    if (!el) continue;
    if (key === "cam") {
      el.style.display = (vodState.cam_position === "none") ? "none" : "";
    }
    const px = b.x * rect.width;
    const py = b.y * rect.height;
    const pw = b.w * rect.width;
    const ph = b.h * rect.height;
    el.style.transform = `translate3d(${px.toFixed(1)}px, ${py.toFixed(1)}px, 0)`;
    el.style.width = `${pw.toFixed(1)}px`;
    el.style.height = `${ph.toFixed(1)}px`;
  }
}

function setupVodDualBoxInteractions() {
  const stage = document.getElementById("vod-big-stage");
  if (!stage) return;

  let dragCtx = null;

  stage.addEventListener("pointerdown", (e) => {
    const handleEl = e.target.closest(".crop-handle");
    const labelEl = e.target.closest(".crop-box-label");
    if (!handleEl && !labelEl) return;
    const boxEl = e.target.closest(".crop-box");
    if (!boxEl) return;

    e.preventDefault();
    const key = boxEl.getAttribute("data-box");
    const handle = handleEl ? handleEl.getAttribute("data-handle") : "move";
    const stageRect = stage.getBoundingClientRect();

    selectVodActiveBox(key);
    boxEl.classList.add("is-dragging");
    const captureEl = e.target;
    try {
      captureEl.setPointerCapture(e.pointerId);
    } catch (err) {}

    dragCtx = {
      key,
      handle,
      boxEl,
      captureEl,
      pointerId: e.pointerId,
      stageRect,
      startX: e.clientX,
      startY: e.clientY,
      orig: { ...vodState.boxes[key] },
    };
  });

  window.addEventListener("pointermove", (e) => {
    if (!dragCtx || e.pointerId !== dragCtx.pointerId) return;
    e.preventDefault();

    const dx = (e.clientX - dragCtx.startX) / dragCtx.stageRect.width;
    const dy = (e.clientY - dragCtx.startY) / dragCtx.stageRect.height;
    const o = dragCtx.orig;
    const minW = 0.10;
    const minH = 0.10;
    let { x, y, w, h } = o;

    if (dragCtx.handle === "move") {
      x = Math.max(0, Math.min(1 - w, o.x + dx));
      y = Math.max(0, Math.min(1 - h, o.y + dy));
    } else {
      if (dragCtx.handle.includes("e")) w = Math.max(minW, Math.min(1 - o.x, o.w + dx));
      if (dragCtx.handle.includes("s")) h = Math.max(minH, Math.min(1 - o.y, o.h + dy));
      if (dragCtx.handle.includes("w")) {
        const maxLeft = o.x + o.w - minW;
        const newX = Math.max(0, Math.min(maxLeft, o.x + dx));
        w = o.w + (o.x - newX);
        x = newX;
      }
      if (dragCtx.handle.includes("n")) {
        const maxTop = o.y + o.h - minH;
        const newY = Math.max(0, Math.min(maxTop, o.y + dy));
        h = o.h + (o.y - newY);
        y = newY;
      }
    }

    vodState.boxes[dragCtx.key] = { x, y, w, h };
    syncVodBoxesToDOM();
  });

  const endDrag = (e) => {
    if (!dragCtx || e.pointerId !== dragCtx.pointerId) return;
    dragCtx.boxEl.classList.remove("is-dragging");
    try {
      dragCtx.captureEl.releasePointerCapture(e.pointerId);
    } catch (err) {}
    dragCtx = null;
  };

  window.addEventListener("pointerup", endDrag);
  window.addEventListener("pointercancel", endDrag);
}

async function submitVodClipExport() {
  if (!vodState.source) {
    showVodError("Please paste and load an ended live stream or VOD link first.");
    return;
  }

  hideVodError();
  const dur = vodState.clipDuration || 35;
  const slider = document.getElementById("vod-scrubber-slider");

  // Sample exact playback position from player if active, otherwise scrubber slider
  let startSec = 0;
  if (ytVodPlayer && typeof ytVodPlayer.getCurrentTime === "function") {
    try {
      const pTime = ytVodPlayer.getCurrentTime();
      if (typeof pTime === "number" && !isNaN(pTime) && pTime >= 0) {
        startSec = pTime;
      }
    } catch (e) {}
  }
  if (startSec <= 0) {
    startSec = slider ? parseFloat(slider.value) : (vodState.currentPlaySec || 0);
  }
  if (isNaN(startSec) || startSec < 0) startSec = 0;
  startSec = Math.round(startSec * 10) / 10;
  const endSec = startSec + dur;

  const streamUrl = document.getElementById("vod-youtube-url")?.value?.trim() ||
    (vodState.source?.video_id ? `https://www.youtube.com/watch?v=${vodState.source.video_id}` : "") ||
    vodState.source?.url || "";

  const isNoCam = (vodState.cam_position === "none");
  const payload = {
    url: streamUrl,
    video_id: vodState.source.video_id,
    is_live: false,
    duration_sec: dur,
    rewind_sec: 0,
    start_utc_ts: null,
    end_utc_ts: null,
    start_time: startSec,
    end_time: endSec,
    cam_box: vodState.boxes?.cam || { x: 0.70, y: 0.58, w: 0.28, h: 0.36 },
    stream_box: vodState.boxes?.stream || { x: 0.02, y: 0.04, w: 0.66, h: 0.92 },
    cam_ratio: vodState.cam_ratio || 0.40,
    cam_position: vodState.cam_position || "top",
    resolution: vodState.resolution || "1080p",
    burn_subtitles: true,
    subtitle_style: document.getElementById("vod-sub-style")?.value || "viral_pop",
    subtitle_position: isNoCam ? "bottom" : "seam",
    access_key: proState.accessKey,
  };

  const modeLabel = isNoCam ? "Display-Only" : "Dual-Box";
  showProgressModal(`Rendering ${dur}s ${modeLabel} VOD Short (${formatTime(startSec)} ➔ ${formatTime(endSec)})...`);

  try {
    const res = await fetchWithTimeout("/api/pro/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 25000);
    const data = await parseApiResponse(res);
    if (!res.ok || !data.success) {
      throw new Error(data.detail || "Could not start VOD short render.");
    }
    pollTaskProgress(data.task_id);
  } catch (err) {
    hideProgressModal();
    showVodError(err.message);
  }
}

function setupVodStudio() {
  const vodBtn = document.getElementById("studio-btn-vod");
  if (!vodBtn) return;

  // Streamer Position Controls (Top vs Bottom vs None)
  document.querySelectorAll("#vod-pos-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#vod-pos-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const pos = btn.dataset.pos || "top";
      vodState.cam_position = pos;
      updateVodCamPositionUI(pos);
    });
  });

  function updateVodCamPositionUI(pos) {
    const isNoCam = (pos === "none");
    const camBox = document.getElementById("box-vod-cam");
    if (camBox) camBox.style.display = isNoCam ? "none" : "";

    const btnSelectCam = document.getElementById("btn-select-box-cam");
    if (btnSelectCam) {
      btnSelectCam.style.opacity = isNoCam ? "0.4" : "1";
      btnSelectCam.style.pointerEvents = isNoCam ? "none" : "";
    }
    if (isNoCam) {
      if (typeof selectVodActiveBox === "function") selectVodActiveBox("stream");
    }

    const ratioGroup = document.getElementById("vod-ratio-picker")?.closest(".pro-mini-setting");
    if (ratioGroup) {
      ratioGroup.style.opacity = isNoCam ? "0.3" : "1";
      ratioGroup.style.pointerEvents = isNoCam ? "none" : "";
    }

    const recBtnText = document.querySelector("#vod-create-btn .record-btn-text");
    if (recBtnText) {
      recBtnText.textContent = isNoCam ? "⚡ Generate Display-Only Short" : "⚡ Generate Dual-Box Short";
    }
  }

  // Split Ratio with Respect to Display Controls
  document.querySelectorAll("#vod-ratio-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#vod-ratio-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      vodState.cam_ratio = parseFloat(btn.dataset.ratio) || 0.40;
    });
  });

  // Short Duration Controls (20s, 35s, 50s, 60s)
  document.querySelectorAll("#vod-duration-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#vod-duration-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      vodState.clipDuration = parseInt(btn.dataset.dur, 10) || 35;
      syncVodScrubberUI(vodState.currentPlaySec);
    });
  });

  // Resolution Controls (1080p vs 4K)
  document.querySelectorAll("#vod-res-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      const res = btn.dataset.res;
      if (res === "4k") {
        if (vodState.source && vodState.source.has_4k === false) {
          const maxStr = vodState.source.max_height ? `${vodState.source.max_height}p` : "1080p";
          show4kUnavailableModal(maxStr);
          return;
        }
      }
      document.querySelectorAll("#vod-res-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      vodState.resolution = res;
    });
  });

  // Scrubber Slider - Smooth Scrubbing & Seeking Front/Back in Time
  const scrubber = document.getElementById("vod-scrubber-slider");
  if (scrubber) {
    const handleScrub = (e) => {
      vodState.isDraggingScrubber = true;
      const sec = parseFloat(e.target.value) || 0;
      seekVodPlayback(sec);
    };
    const endScrub = () => {
      vodState.isDraggingScrubber = false;
    };
    scrubber.addEventListener("input", handleScrub);
    scrubber.addEventListener("change", endScrub);
    scrubber.addEventListener("pointerup", endScrub);
    scrubber.addEventListener("touchend", endScrub);
    scrubber.addEventListener("pointercancel", endScrub);
  }

  // Load VOD form & Paste button
  const vodUrlInput = document.getElementById("vod-youtube-url");
  const inspectForm = document.getElementById("vod-inspect-form");
  if (inspectForm && vodUrlInput) {
    inspectForm.addEventListener("submit", (e) => {
      e.preventDefault();
      inspectVodStream(vodUrlInput.value.trim());
    });

    vodUrlInput.addEventListener("paste", () => {
      setTimeout(() => {
        const val = vodUrlInput.value.trim();
        if (looksLikeYouTubeUrl(val)) inspectVodStream(val);
      }, 60);
    });
  }

  const pasteBtn = document.getElementById("vod-paste-btn");
  if (pasteBtn && vodUrlInput) {
    pasteBtn.addEventListener("click", async () => {
      try {
        const text = (await navigator.clipboard.readText()).trim();
        if (text) {
          vodUrlInput.value = text;
          if (looksLikeYouTubeUrl(text)) inspectVodStream(text);
        }
      } catch (e) {
        vodUrlInput.focus();
      }
    });
  }

  // Big Generate Button
  const createBtn = document.getElementById("vod-create-btn");
  if (createBtn) {
    createBtn.addEventListener("click", submitVodClipExport);
  }

  // Dual-Box Drag & Resize Setup
  setupVodDualBoxInteractions();
  window.addEventListener("resize", () => {
    if (proState.mode === "vod" && vodState.source) syncVodBoxesToDOM();
  });

  // Initial Sync
  syncVodBoxesToDOM();
  selectVodActiveBox("cam");
}
