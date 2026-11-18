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
  setupLiquidGlassEffect();
  setupProStudio();
});

// 144Hz+ zero-reflow spring-interpolated GPU liquid glass sheen (0% idle CPU)
function setupLiquidGlassEffect() {
  let activeCard = null;
  let cachedRect = null;
  let targetX = 0;
  let targetY = 0;
  let currX = 0;
  let currY = 0;
  let rafId = 0;
  let lastTs = 0;

  const invalidateRect = () => {
    cachedRect = null;
  };
  window.addEventListener("scroll", invalidateRect, { passive: true });
  window.addEventListener("resize", invalidateRect, { passive: true });

  function tick(ts) {
    rafId = 0;
    if (!activeCard) return;

    const dt = lastTs ? Math.min(0.05, (ts - lastTs) / 1000) : 1 / 144;
    lastTs = ts;

    // Frame-rate independent smooth liquid glide (locked to monitor Hz: 60 / 120 / 144 / 240fps)
    const alpha = 1 - Math.exp(-24 * dt);
    currX += (targetX - currX) * alpha;
    currY += (targetY - currY) * alpha;

    activeCard.style.setProperty("--mx", `${currX.toFixed(1)}px`);
    activeCard.style.setProperty("--my", `${currY.toFixed(1)}px`);

    if (Math.abs(targetX - currX) > 0.25 || Math.abs(targetY - currY) > 0.25) {
      rafId = requestAnimationFrame(tick);
    } else {
      lastTs = 0;
    }
  }

  document.addEventListener(
    "pointermove",
    (e) => {
      if (!e.target || typeof e.target.closest !== "function") return;
      const card = e.target.closest(".glass-card, .clip-card");
      if (!card) {
        activeCard = null;
        cachedRect = null;
        return;
      }

      if (card !== activeCard || !cachedRect) {
        activeCard = card;
        cachedRect = card.getBoundingClientRect();
        currX = e.clientX - cachedRect.left;
        currY = e.clientY - cachedRect.top;
      }

      targetX = e.clientX - cachedRect.left;
      targetY = e.clientY - cachedRect.top;

      if (!rafId) {
        lastTs = 0;
        rafId = requestAnimationFrame(tick);
      }
    },
    { passive: true }
  );
}

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

  const gradient = ctx.createLinearGradient(0, 0, 0, 215);
  gradient.addColorStop(0, "rgba(124, 58, 237, 0.34)");
  gradient.addColorStop(0.65, "rgba(167, 139, 250, 0.12)");
  gradient.addColorStop(1, "rgba(196, 181, 253, 0.0)");

  heatmapChartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels: labels,
      datasets: [
        {
          label: "Engagement",
          data: values,
          borderColor: "#7c3aed",
          borderWidth: 2.8,
          backgroundColor: gradient,
          fill: true,
          tension: 0.38,
          pointRadius: 0,
          pointHoverRadius: 6,
          pointHoverBackgroundColor: "#ffffff",
          pointHoverBorderColor: "#7c3aed",
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
          backgroundColor: "rgba(255, 255, 255, 0.96)",
          titleColor: "#18102b",
          bodyColor: "#7c3aed",
          borderColor: "rgba(124, 58, 237, 0.28)",
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
  const proBadge = result.is_pro ? " • Pro Split-Screen" : "";
  const capBadge = result.caption_engine === "ai_whisper_hinglish" ? " • AI Hinglish" : "";
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
// PRO STUDIO: INSTAGRAM FOLLOW GATE + LIVE DUAL-BOX SPLIT-SCREEN
// =============================================================
const PRO_UNLOCK_KEY = "clipyt_pro_unlocked_v1";

const proState = {
  unlocked: false,
  mode: "standard", // "standard" | "pro"
  followClicked: false,
  followTimerReady: false,
  followCountdownId: null,
  source: null, // response.data from /api/pro/inspect
  liveDuration: 35,
  liveRewind: 0,
  vodStart: 0,
  vodEnd: 35,
  camRatio: 0.40,
  camPosition: "top", // "top" | "bottom"
  boxes: {
    cam: { x: 0.72, y: 0.62, w: 0.25, h: 0.32 },
    stream: { x: 0.03, y: 0.04, w: 0.68, h: 0.92 },
  },
  frameImageObj: null,
  previewRafPending: false,
};

function isProUnlocked() {
  try {
    const raw = localStorage.getItem(PRO_UNLOCK_KEY);
    if (!raw) return false;
    const parsed = JSON.parse(raw);
    return Boolean(parsed && parsed.handle);
  } catch (e) {
    return false;
  }
}

function refreshProBadgeUI() {
  const badge = document.getElementById("pro-badge-pill");
  if (!badge) return;
  if (proState.unlocked) {
    badge.textContent = "✓ PRO";
    badge.classList.add("unlocked");
  } else {
    badge.textContent = "🔒 LIVE";
    badge.classList.remove("unlocked");
  }
}

function switchStudioMode(targetMode) {
  const stdBtn = document.getElementById("studio-btn-standard");
  const proBtn = document.getElementById("studio-btn-pro");
  const stdWrap = document.getElementById("standard-workspace");
  const proWrap = document.getElementById("pro-studio-section");

  if (targetMode === "pro" && !proState.unlocked) {
    openProUnlockModal();
    return;
  }

  proState.mode = targetMode;
  const isPro = targetMode === "pro";
  stdBtn.classList.toggle("active", !isPro);
  proBtn.classList.toggle("active", isPro);
  stdWrap.classList.toggle("hidden", isPro);
  proWrap.classList.toggle("hidden", !isPro);

  if (isPro && proState.source) {
    requestAnimationFrame(() => {
      syncBoxesToDOM();
      scheduleProPreviewRender();
    });
  }
}

function openProUnlockModal() {
  const modal = document.getElementById("pro-unlock-modal");
  document.getElementById("insta-verify-error").classList.add("hidden");
  modal.classList.remove("hidden");
}

function closeProUnlockModal() {
  document.getElementById("pro-unlock-modal").classList.add("hidden");
}

function setupProStudio() {
  proState.unlocked = isProUnlocked();
  refreshProBadgeUI();

  // Top mode switcher
  document.getElementById("studio-btn-standard").addEventListener("click", () => {
    switchStudioMode("standard");
  });
  document.getElementById("studio-btn-pro").addEventListener("click", () => {
    switchStudioMode("pro");
  });

  // Unlock modal close
  document.getElementById("pro-unlock-close-x").addEventListener("click", closeProUnlockModal);
  document.getElementById("pro-unlock-modal").addEventListener("click", (e) => {
    if (e.target.id === "pro-unlock-modal") closeProUnlockModal();
  });

  // Step 1: Instagram Follow Button click -> 4s wait before enabling Step 2
  const followBtn = document.getElementById("insta-follow-btn");
  const waitStatus = document.getElementById("insta-wait-status");
  const step2El = document.getElementById("unlock-step-2");
  const handleInput = document.getElementById("insta-username-input");
  const verifyBtn = document.getElementById("insta-verify-btn");

  followBtn.addEventListener("click", () => {
    proState.followClicked = true;
    if (proState.followTimerReady) return;
    if (proState.followCountdownId) clearInterval(proState.followCountdownId);

    let secs = 4;
    waitStatus.classList.remove("hidden", "ready");
    waitStatus.textContent = `Opening @gta6.insider_... please tap Follow (${secs}s)`;

    proState.followCountdownId = setInterval(() => {
      secs -= 1;
      if (secs <= 0) {
        clearInterval(proState.followCountdownId);
        proState.followCountdownId = null;
        proState.followTimerReady = true;
        waitStatus.classList.add("ready");
        waitStatus.textContent = "✓ Profile opened! Now enter your Instagram username below.";
        step2El.classList.remove("unlock-step-locked");
        handleInput.disabled = false;
        verifyBtn.disabled = false;
        handleInput.focus();
      } else {
        waitStatus.textContent = `Opening @gta6.insider_... please tap Follow (${secs}s)`;
      }
    }, 1000);
  });

  // Step 2: Verify Instagram handle & unlock Pro Studio
  const doVerify = () => {
    const errEl = document.getElementById("insta-verify-error");
    errEl.classList.add("hidden");

    if (!proState.followClicked || !proState.followTimerReady) {
      errEl.textContent = "Please follow @gta6.insider_ in Step 1 first.";
      errEl.classList.remove("hidden");
      return;
    }

    const raw = handleInput.value.trim().replace(/^@+/, "");
    const blocked = new Set(["test", "testing", "asdf", "abcd", "1234", "12345", "none", "fake", "user", "instagram", "gta6.insider_"]);
    if (!/^[a-zA-Z0-9._]{2,30}$/.test(raw) || blocked.has(raw.toLowerCase())) {
      errEl.textContent = "Please enter your valid Instagram username (without spaces).";
      errEl.classList.remove("hidden");
      return;
    }

    const btnTxt = verifyBtn.querySelector(".btn-text");
    const btnSp = verifyBtn.querySelector(".spinner");
    verifyBtn.disabled = true;
    handleInput.disabled = true;
    btnTxt.textContent = "Verifying...";
    btnSp.classList.remove("hidden");

    setTimeout(() => {
      try {
        localStorage.setItem(
          PRO_UNLOCK_KEY,
          JSON.stringify({ handle: raw, unlockedAt: Date.now() })
        );
      } catch (e) {}
      proState.unlocked = true;
      refreshProBadgeUI();
      verifyBtn.disabled = false;
      handleInput.disabled = false;
      btnTxt.textContent = "Verify & Unlock";
      btnSp.classList.add("hidden");
      closeProUnlockModal();
      switchStudioMode("pro");
    }, 1050);
  };

  verifyBtn.addEventListener("click", doVerify);
  handleInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      doVerify();
    }
  });

  // Pro URL form & Paste button
  const proUrlInput = document.getElementById("pro-youtube-url");
  document.getElementById("pro-inspect-form").addEventListener("submit", (e) => {
    e.preventDefault();
    inspectProStream(proUrlInput.value.trim(), false);
  });

  proUrlInput.addEventListener("paste", () => {
    setTimeout(() => {
      const val = proUrlInput.value.trim();
      if (looksLikeYouTubeUrl(val)) {
        inspectProStream(val, false);
      }
    }, 60);
  });

  document.getElementById("pro-paste-btn").addEventListener("click", async () => {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (text) {
        proUrlInput.value = text;
        if (looksLikeYouTubeUrl(text)) {
          inspectProStream(text, false);
        }
      }
    } catch (e) {
      proUrlInput.focus();
    }
  });

  // Refresh live frame button
  document.getElementById("pro-refresh-frame-btn").addEventListener("click", () => {
    const val = proUrlInput.value.trim();
    if (val) inspectProStream(val, true);
  });

  // Live DVR duration pills
  document.querySelectorAll("#pro-live-dur-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pro-live-dur-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      proState.liveDuration = parseInt(btn.getAttribute("data-dur"), 10) || 35;
    });
  });

  // Live DVR rewind pills
  document.querySelectorAll("#pro-live-rewind-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pro-live-rewind-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      proState.liveRewind = parseInt(btn.getAttribute("data-rewind"), 10) || 0;
    });
  });

  // VOD manual timing inputs
  const vodStartIn = document.getElementById("pro-vod-start-input");
  const vodEndIn = document.getElementById("pro-vod-end-input");
  vodStartIn.addEventListener("change", () => onProVodInputChange("start"));
  vodEndIn.addEventListener("change", () => onProVodInputChange("end"));

  // Streamer split ratio pills
  document.querySelectorAll("#pro-ratio-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pro-ratio-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      proState.camRatio = parseFloat(btn.getAttribute("data-camratio")) || 0.40;
      scheduleProPreviewRender();
    });
  });

  // Streamer Top/Bottom pills
  document.querySelectorAll("#pro-campos-picker .choice-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pro-campos-picker .choice-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      proState.camPosition = btn.getAttribute("data-campos") || "top";
      scheduleProPreviewRender();
    });
  });

  // Pro Captions toggle & selects update live 9:16 preview
  const proBurnToggle = document.getElementById("pro-burn-subs");
  const proSubOpts = document.getElementById("pro-sub-options");
  proBurnToggle.addEventListener("change", () => {
    proSubOpts.querySelectorAll("#pro-sub-style, #pro-sub-pos").forEach((el) => (el.disabled = !proBurnToggle.checked));
    proSubOpts.style.opacity = proBurnToggle.checked ? "1" : "0.55";
    scheduleProPreviewRender();
  });
  document.getElementById("pro-sub-style").addEventListener("change", scheduleProPreviewRender);
  document.getElementById("pro-sub-pos").addEventListener("change", scheduleProPreviewRender);

  // Create Pro Short button
  document.getElementById("pro-create-btn").addEventListener("click", submitProGenerate);

  // Dual-Box pointer drag & corner resize engine
  setupDualBoxInteractions();
  window.addEventListener("resize", () => {
    if (proState.mode === "pro" && proState.source) {
      syncBoxesToDOM();
      scheduleProPreviewRender();
    }
  });
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

async function inspectProStream(url, forceRefresh = false) {
  if (!looksLikeYouTubeUrl(url)) {
    showProError("Please paste a valid YouTube Live or Video link.");
    return;
  }

  hideProError();
  setProLoadSpinner(true);
  const refreshBtn = document.getElementById("pro-refresh-frame-btn");
  if (forceRefresh && refreshBtn) {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "Refreshing...";
  }

  try {
    const res = await fetch("/api/pro/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, force_refresh: forceRefresh }),
    });
    const json = await res.json();
    if (!res.ok || !json.success) {
      throw new Error(json.detail || "Could not load stream.");
    }

    const src = json.data;
    proState.source = src;

    // Update source bar
    const badgeEl = document.getElementById("pro-live-badge");
    if (src.is_live) {
      badgeEl.textContent = "🔴 LIVE NOW";
      badgeEl.classList.remove("is-vod");
      document.getElementById("pro-live-controls").classList.remove("hidden");
      document.getElementById("pro-vod-controls").classList.add("hidden");
      refreshBtn.textContent = "↻ Refresh Live Frame";
    } else {
      badgeEl.textContent = `🎬 VIDEO • ${src.duration_str}`;
      badgeEl.classList.add("is-vod");
      document.getElementById("pro-live-controls").classList.add("hidden");
      document.getElementById("pro-vod-controls").classList.remove("hidden");
      refreshBtn.textContent = "↻ Refresh Frame";

      const maxDur = Math.max(10, Math.floor(src.duration || 60));
      proState.vodStart = 0;
      proState.vodEnd = Math.min(35, maxDur);
      const fmtHint = src.use_hours ? "(hh:mm:ss)" : "(mm:ss)";
      document.getElementById("pro-vod-start-label").textContent = `Start ${fmtHint}`;
      document.getElementById("pro-vod-end-label").textContent = `End ${fmtHint}`;
      syncProVodUI();
    }

    document.getElementById("pro-source-title").textContent = src.title;
    document.getElementById("pro-source-author").textContent = src.author;

    // Load grabbed 16:9 frame into editor & canvas preview
    const imgEl = document.getElementById("pro-frame-img");
    const frameSrc = src.frame_image || src.thumbnail;
    imgEl.src = frameSrc;

    const previewImg = new Image();
    previewImg.crossOrigin = "anonymous";
    previewImg.onload = () => {
      proState.frameImageObj = previewImg;
      scheduleProPreviewRender();
    };
    previewImg.src = frameSrc;

    document.getElementById("pro-editor-wrap").classList.remove("hidden");
    requestAnimationFrame(() => {
      syncBoxesToDOM();
      scheduleProPreviewRender();
    });
  } catch (err) {
    showProError(err.message || "Failed to load stream.");
  } finally {
    setProLoadSpinner(false);
    if (refreshBtn) {
      refreshBtn.disabled = false;
      if (proState.source) {
        refreshBtn.textContent = proState.source.is_live ? "↻ Refresh Live Frame" : "↻ Refresh Frame";
      }
    }
  }
}

function syncProVodUI() {
  if (!proState.source) return;
  const useH = Boolean(proState.source.use_hours);
  document.getElementById("pro-vod-start-input").value = formatTime(proState.vodStart, useH);
  document.getElementById("pro-vod-end-input").value = formatTime(proState.vodEnd, useH);
  document.getElementById("pro-vod-dur-display").textContent = formatTime(
    Math.max(1, proState.vodEnd - proState.vodStart),
    false
  );
}

function onProVodInputChange(field) {
  if (!proState.source) return;
  const maxDur = Math.max(10, Math.floor(proState.source.duration || 3600));
  const input = document.getElementById(field === "start" ? "pro-vod-start-input" : "pro-vod-end-input");
  const parsed = parseTime(input.value);
  if (!isNaN(parsed)) {
    const clamped = Math.max(0, Math.min(maxDur, parsed));
    if (field === "start") {
      proState.vodStart = Math.min(clamped, Math.max(0, proState.vodEnd - 5));
    } else {
      proState.vodEnd = Math.max(proState.vodStart + 5, Math.min(clamped, proState.vodStart + 120));
    }
  }
  syncProVodUI();
}

window.stepProVodTime = function (field, delta) {
  if (!proState.source) return;
  const maxDur = Math.max(10, Math.floor(proState.source.duration || 3600));
  if (field === "start") {
    proState.vodStart = Math.max(0, Math.min(proState.vodEnd - 5, proState.vodStart + delta));
  } else {
    proState.vodEnd = Math.max(proState.vodStart + 5, Math.min(maxDur, Math.min(proState.vodStart + 120, proState.vodEnd + delta)));
  }
  syncProVodUI();
};

// 1-Click Streamer Facecam Presets
window.applyCamPreset = function (preset) {
  if (preset === "tl") {
    proState.boxes.cam = { x: 0.02, y: 0.04, w: 0.25, h: 0.32 };
  } else if (preset === "tr") {
    proState.boxes.cam = { x: 0.73, y: 0.04, w: 0.25, h: 0.32 };
  } else if (preset === "bl") {
    proState.boxes.cam = { x: 0.02, y: 0.63, w: 0.25, h: 0.32 };
  } else if (preset === "br") {
    proState.boxes.cam = { x: 0.73, y: 0.63, w: 0.25, h: 0.32 };
  } else {
    proState.boxes.cam = { x: 0.72, y: 0.62, w: 0.25, h: 0.32 };
    proState.boxes.stream = { x: 0.03, y: 0.04, w: 0.68, h: 0.92 };
  }
  syncBoxesToDOM();
  scheduleProPreviewRender();
};

function syncBoxesToDOM() {
  const stage = document.getElementById("pro-stage");
  if (!stage) return;
  const rect = stage.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;

  for (const key of ["cam", "stream"]) {
    const b = proState.boxes[key];
    const el = document.getElementById(key === "cam" ? "box-cam" : "box-stream");
    if (!el) continue;
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
  const stage = document.getElementById("pro-stage");
  if (!stage) return;

  let dragCtx = null;

  stage.addEventListener("pointerdown", (e) => {
    const handleEl = e.target.closest(".crop-handle");
    const boxEl = e.target.closest(".crop-box");
    if (!boxEl) return;

    e.preventDefault();
    const key = boxEl.getAttribute("data-box");
    const handle = handleEl ? handleEl.getAttribute("data-handle") : "move";
    const stageRect = stage.getBoundingClientRect();

    boxEl.classList.add("is-dragging");
    boxEl.setPointerCapture(e.pointerId);

    dragCtx = {
      key,
      handle,
      boxEl,
      pointerId: e.pointerId,
      stageRect,
      startX: e.clientX,
      startY: e.clientY,
      orig: { ...proState.boxes[key] },
    };
  });

  stage.addEventListener("pointermove", (e) => {
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
      if (dragCtx.handle.includes("e")) {
        w = Math.max(minW, Math.min(1 - o.x, o.w + dx));
      }
      if (dragCtx.handle.includes("s")) {
        h = Math.max(minH, Math.min(1 - o.y, o.h + dy));
      }
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
    scheduleProPreviewRender();
  });

  const endDrag = (e) => {
    if (!dragCtx || e.pointerId !== dragCtx.pointerId) return;
    dragCtx.boxEl.classList.remove("is-dragging");
    try {
      dragCtx.boxEl.releasePointerCapture(e.pointerId);
    } catch (err) {}
    dragCtx = null;
  };

  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);
}

function scheduleProPreviewRender() {
  if (proState.previewRafPending) return;
  proState.previewRafPending = true;
  requestAnimationFrame(() => {
    proState.previewRafPending = false;
    drawProPreviewCanvas();
  });
}

function drawCoverCrop(ctx, img, box, destX, destY, destW, destH) {
  const iw = img.naturalWidth || img.width;
  const ih = img.naturalHeight || img.height;
  if (!iw || !ih) return;

  let sx = box.x * iw;
  let sy = box.y * ih;
  let sw = box.w * iw;
  let sh = box.h * ih;

  // Match FFmpeg's scale=increase + center crop (zero stretch, zero distortion!)
  const srcAspect = sw / sh;
  const dstAspect = destW / destH;
  if (srcAspect > dstAspect) {
    const newSw = sh * dstAspect;
    sx += (sw - newSw) / 2;
    sw = newSw;
  } else {
    const newSh = sw / dstAspect;
    sy += (sh - newSh) / 2;
    sh = newSh;
  }

  ctx.drawImage(img, sx, sy, sw, sh, destX, destY, destW, destH);
}

function drawProPreviewCanvas() {
  const canvas = document.getElementById("pro-preview-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width;
  const H = canvas.height;

  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#0f0a1d";
  ctx.fillRect(0, 0, W, H);

  const dividerH = 3;
  const camH = Math.round(H * proState.camRatio);
  const streamH = H - camH - dividerH;
  const camOnTop = proState.camPosition !== "bottom";

  const camY = camOnTop ? 0 : streamH + dividerH;
  const streamY = camOnTop ? camH + dividerH : 0;
  const seamY = camOnTop ? camH : streamH;

  if (proState.frameImageObj) {
    drawCoverCrop(ctx, proState.frameImageObj, proState.boxes.cam, 0, camY, W, camH);
    drawCoverCrop(ctx, proState.frameImageObj, proState.boxes.stream, 0, streamY, W, streamH);
  }

  // Subtle dark-glass divider line
  ctx.fillStyle = "#120d24";
  ctx.fillRect(0, seamY, W, dividerH);

  // Sample AI Hinglish Caption preview
  const burnSubs = document.getElementById("pro-burn-subs")?.checked;
  if (burnSubs) {
    const style = document.getElementById("pro-sub-style")?.value || "viral_pop";
    const pos = document.getElementById("pro-sub-pos")?.value || "seam";

    let capY = seamY + 6;
    if (pos === "top") capY = Math.round(H * 0.14);
    else if (pos === "bottom") capY = Math.round(H * 0.79);

    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const isModern = style === "clean_modern";
    ctx.font = isModern ? "700 18px Inter, sans-serif" : "900 19px Inter, sans-serif";

    const words = isModern ? ["Bhai", "kya", "clutch!"] : ["BHAI", "KYA", "CLUTCH!"];
    const fullText = words.join(" ");
    const totalW = ctx.measureText(fullText).width;

    if (isModern) {
      ctx.fillStyle = "rgba(0, 0, 0, 0.72)";
      const padX = 12;
      const padY = 8;
      ctx.beginPath();
      ctx.roundRect(W / 2 - totalW / 2 - padX, capY - 12 - padY / 2, totalW + padX * 2, 26 + padY, 8);
      ctx.fill();
    }

    let curX = W / 2 - totalW / 2;
    ctx.textAlign = "left";
    words.forEach((w, idx) => {
      const displayW = idx < words.length - 1 ? w + " " : w;
      const active = idx === 2;
      let fill = "#ffffff";
      if (style === "neon_yellow") fill = active ? "#33ff55" : "#ffe500";
      else if (style === "clean_modern") fill = active ? "#ffe500" : "#ffffff";
      else fill = active ? "#ffea00" : "#ffffff";

      if (!isModern) {
        ctx.lineWidth = 4.5;
        ctx.strokeStyle = "#000000";
        ctx.strokeText(displayW, curX, capY);
      }
      ctx.fillStyle = fill;
      ctx.fillText(displayW, curX, capY);
      curX += ctx.measureText(displayW).width;
    });
    ctx.restore();
  }
}

async function submitProGenerate() {
  if (!proState.source) {
    showProError("Please load a YouTube Live Stream or Video first.");
    return;
  }

  hideProError();
  const isLive = Boolean(proState.source.is_live);
  if (!isLive) {
    onProVodInputChange("start");
    onProVodInputChange("end");
    if (proState.vodEnd <= proState.vodStart) {
      showProError("End time must be after Start time.");
      return;
    }
  }

  const payload = {
    url: document.getElementById("pro-youtube-url").value.trim(),
    video_id: proState.source.video_id,
    is_live: isLive,
    duration_sec: proState.liveDuration,
    rewind_sec: proState.liveRewind,
    start_time: proState.vodStart,
    end_time: proState.vodEnd,
    cam_box: proState.boxes.cam,
    stream_box: proState.boxes.stream,
    cam_ratio: proState.camRatio,
    cam_position: proState.camPosition,
    resolution: document.getElementById("pro-resolution").value,
    burn_subtitles: document.getElementById("pro-burn-subs").checked,
    subtitle_style: document.getElementById("pro-sub-style").value,
    subtitle_position: document.getElementById("pro-sub-pos").value,
  };

  showProgressModal(isLive ? "Connecting to live stream DVR..." : "Starting Pro Studio...");

  try {
    const res = await fetch("/api/pro/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.detail || "Could not start Pro Short creation.");
    }
    pollTaskProgress(data.task_id);
  } catch (err) {
    hideProgressModal();
    showProError(err.message);
  }
}
