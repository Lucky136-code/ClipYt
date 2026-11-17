// --- ClipYT Simple Frontend ---

let currentAnalysis = null;
let heatmapChartInstance = null;
let selectedAspectRatio = "9:16";
let selectedFramingMode = "blur";
let lastAnalyzedUrl = "";
let clipSecondsState = {}; // { clip_id: { start: float, end: float, peak: float|null } }
let manualState = { start: 0, end: 35 };
let storyboardSheetDims = {}; // { url: { cols, rows } }
let manualRafPending = false;

document.addEventListener("DOMContentLoaded", () => {
  setupEventListeners();
});

function setupEventListeners() {
  const analyzeForm = document.getElementById("analyze-form");
  const urlInput = document.getElementById("youtube-url");

  analyzeForm.addEventListener("submit", (e) => {
    e.preventDefault();
    runFullAnalysis(urlInput.value.trim());
  });

  // Auto-preview & analyze immediately when user pastes a YouTube link
  urlInput.addEventListener("paste", () => {
    setTimeout(() => {
      const val = urlInput.value.trim();
      if (looksLikeYouTubeUrl(val)) {
        runFullAnalysis(val);
      }
    }, 60);
  });

  // Paste button
  document.getElementById("paste-btn").addEventListener("click", async () => {
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
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: url,
        clip_duration: duration,
        max_clips: maxClips,
      }),
    });

    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.detail || "Could not analyze this YouTube video.");
    }

    currentAnalysis = data.data;
    renderFullAnalysis(currentAnalysis);
  } catch (err) {
    document.getElementById("analyzing-indicator").classList.add("hidden");
    showError(err.message || "Something went wrong. Please check the YouTube link.");
  } finally {
    setAnalyzeLoading(false);
  }
}

async function fetchInstantPreview(url) {
  const previewSec = document.getElementById("video-preview-section");
  const indicator = document.getElementById("analyzing-indicator");

  try {
    const res = await fetch(`/api/preview?url=${encodeURIComponent(url)}`);
    if (!res.ok) return;
    const info = await res.json();

    document.getElementById("meta-thumb").src = info.thumbnail;
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

  // Video Thumbnail + Title + Caption as it is from YouTube
  document.getElementById("meta-thumb").src = data.thumbnail;
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
  capStatus.textContent = data.has_transcript ? "Auto-Captions Ready" : "No YouTube Captions";

  const mapSub = document.getElementById("map-subtitle");
  mapSub.textContent = data.has_heatmap
    ? "Higher peaks show where viewers replayed and interacted the most."
    : "YouTube hasn't published a replay graph for this video yet — showing suggested moments.";

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

  const gradient = ctx.createLinearGradient(0, 0, 0, 210);
  gradient.addColorStop(0, "rgba(255, 26, 26, 0.55)");
  gradient.addColorStop(1, "rgba(255, 26, 26, 0.0)");

  heatmapChartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels: labels,
      datasets: [
        {
          label: "Engagement",
          data: values,
          borderColor: "#ff2626",
          borderWidth: 2.5,
          backgroundColor: gradient,
          fill: true,
          tension: 0.35,
          pointRadius: 0,
          pointHoverRadius: 6,
          pointHoverBackgroundColor: "#ffffff",
          pointHoverBorderColor: "#ff1a1a",
          pointHoverBorderWidth: 2,
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
          backgroundColor: "rgba(10, 10, 14, 0.94)",
          titleColor: "#ffffff",
          bodyColor: "#fca5a5",
          borderColor: "rgba(255, 26, 26, 0.5)",
          borderWidth: 1,
          padding: 10,
          displayColors: false,
          callbacks: {
            title: (items) => `Time: ${items[0].label}`,
            label: (item) => `Engagement: ${item.parsed.y}%`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: "rgba(255, 255, 255, 0.06)" },
          ticks: { color: "#9ca3af", maxTicksLimit: 10 },
        },
        y: {
          min: 0,
          max: 100,
          grid: { color: "rgba(255, 255, 255, 0.06)" },
          ticks: { color: "#9ca3af", callback: (v) => v + "%" },
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
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.detail || "Could not start creating Short.");
    }

    pollTaskProgress(data.task_id);
  } catch (err) {
    hideProgressModal();
    showError(err.message);
  }
}

function pollTaskProgress(taskId) {
  const timer = setInterval(async () => {
    try {
      const res = await fetch(`/api/task/${taskId}`);
      if (!res.ok) {
        clearInterval(timer);
        hideProgressModal();
        showError("Lost connection while creating Short.");
        return;
      }

      const task = await res.json();
      updateProgressModal(task.message, task.progress);

      if (task.status === "completed" && task.result) {
        clearInterval(timer);
        showResultModal(task.result);
      } else if (task.status === "failed") {
        clearInterval(timer);
        hideProgressModal();
        showError(task.error || task.message || "Short creation failed.");
      }
    } catch (e) {
      console.error(e);
    }
  }, 250);
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
  videoEl.src = result.download_url;
  videoEl.load();

  const dlBtn = document.getElementById("result-download-btn");
  dlBtn.href = result.download_url;
  dlBtn.setAttribute("download", result.filename);

  const hookBadge = result.climax_hook ? " • Climax Hook" : "";
  document.getElementById("result-meta").textContent =
    `${result.aspect_ratio} • ${result.duration}s • ${result.file_size_mb} MB${hookBadge}`;

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
