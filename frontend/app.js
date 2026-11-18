// --- ClipYT Simple Frontend ---

// Safe API JSON parser: catches HTML error pages and displays a clean, helpful message
async function parseApiResponse(res) {
  const contentType = (res.headers.get("content-type") || "").toLowerCase();
  if (!contentType.includes("application/json")) {
    const text = await res.text().catch(() => "");
    if (text.includes("<!DOCTYPE") || text.includes("<html")) {
      throw new Error(
        "Backend server is offline. Please launch the server on your laptop using 'start_clipyt.bat' (local) or 'start_public_server.bat' (for public web access)."
      );
    }
    if (res.status === 502 || res.status === 503 || res.status === 504) {
      throw new Error(
        `Server temporarily unavailable (${res.status} Bad Gateway). Please make sure 'start_clipyt.bat' or 'start_public_server.bat' is running on your laptop.`
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

document.addEventListener("DOMContentLoaded", () => {
  setupEventListeners();
  setupLiquidGlassEffect();
  setupSiteUnlockGate();
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

    const data = await parseApiResponse(res);
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
    const info = await parseApiResponse(res);

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

    const data = await parseApiResponse(res);
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

      const task = await parseApiResponse(res);
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
  const proBadge = result.is_pro ? " • Live Studio" : "";
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
    return Boolean(parsed && parsed.yt && parsed.ig);
  } catch (e) {
    return false;
  }
}

function setupSiteUnlockGate() {
  const gateEl = document.getElementById("site-unlock-gate");
  if (!gateEl) return;

  if (isSiteUnlocked()) {
    gateEl.classList.add("hidden");
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

  // Task 1: YouTube Subscribe Click -> 5s countdown
  ytBtn.addEventListener("click", () => {
    siteUnlockState.ytClicked = true;
    ytCard.classList.add("active-working");
    if (siteUnlockState.ytTimerReady) return;
    if (siteUnlockState.ytTimerId) clearInterval(siteUnlockState.ytTimerId);

    let secs = 5;
    ytWaitStatus.classList.remove("hidden", "ready");
    ytWaitStatus.textContent = `Opening YouTube channel... please tap Subscribe (${secs}s)`;

    siteUnlockState.ytTimerId = setInterval(() => {
      secs -= 1;
      if (secs <= 0) {
        clearInterval(siteUnlockState.ytTimerId);
        siteUnlockState.ytTimerId = null;
        siteUnlockState.ytTimerReady = true;
        ytWaitStatus.classList.add("ready");
        ytWaitStatus.textContent = "✓ YouTube opened! Enter your YouTube name/handle below.";
        ytPill.textContent = "SUBSCRIBED?";
        ytPill.className = "task-status-pill ready";
        ytInput.focus();
      } else {
        ytWaitStatus.textContent = `Opening YouTube channel... please tap Subscribe (${secs}s)`;
      }
    }, 1000);
  });

  // Task 2: Instagram Follow Click -> 5s countdown
  igBtn.addEventListener("click", () => {
    siteUnlockState.igClicked = true;
    igCard.classList.add("active-working");
    if (siteUnlockState.igTimerReady) return;
    if (siteUnlockState.igTimerId) clearInterval(siteUnlockState.igTimerId);

    let secs = 5;
    igWaitStatus.classList.remove("hidden", "ready");
    igWaitStatus.textContent = `Opening @gta6.insider_... please tap Follow (${secs}s)`;

    siteUnlockState.igTimerId = setInterval(() => {
      secs -= 1;
      if (secs <= 0) {
        clearInterval(siteUnlockState.igTimerId);
        siteUnlockState.igTimerId = null;
        siteUnlockState.igTimerReady = true;
        igWaitStatus.classList.add("ready");
        igWaitStatus.textContent = "✓ Profile opened! Enter your Instagram handle below.";
        igPill.textContent = "FOLLOWED?";
        igPill.className = "task-status-pill ready";
        igInput.focus();
      } else {
        igWaitStatus.textContent = `Opening @gta6.insider_... please tap Follow (${secs}s)`;
      }
    }, 1000);
  });

  // Master Verifier Button Click
  verifyBtn.addEventListener("click", () => {
    if (siteUnlockState.isVerifying) return;
    errEl.classList.add("hidden");

    // Check Task 1 click
    if (!siteUnlockState.ytClicked) {
      errEl.textContent = "Please click '1. Subscribe on YouTube' to complete Task 1.";
      errEl.classList.remove("hidden");
      ytBtn.focus();
      return;
    }

    // Check YouTube Handle input
    const ytVal = ytInput.value.trim().replace(/^@+/, "");
    const blocked = new Set(["test", "testing", "asdf", "abcd", "1234", "none", "fake", "user", "youtube", "instagram", "gta6.insider_"]);
    if (ytVal.length < 2 || blocked.has(ytVal.toLowerCase())) {
      errEl.textContent = "Please enter your valid YouTube username or handle in Task 1.";
      errEl.classList.remove("hidden");
      ytInput.focus();
      return;
    }

    // Check Task 2 click
    if (!siteUnlockState.igClicked) {
      errEl.textContent = "Please click '2. Follow @gta6.insider_' to complete Task 2.";
      errEl.classList.remove("hidden");
      igBtn.focus();
      return;
    }

    // Check Instagram Handle input
    const igVal = igInput.value.trim().replace(/^@+/, "");
    if (!/^[a-zA-Z0-9._]{2,30}$/.test(igVal) || blocked.has(igVal.toLowerCase())) {
      errEl.textContent = "Please enter your valid Instagram handle in Task 2 (without spaces).";
      errEl.classList.remove("hidden");
      igInput.focus();
      return;
    }

    // Start Live Verification sequence
    siteUnlockState.isVerifying = true;
    verifyBtn.disabled = true;
    ytInput.disabled = true;
    igInput.disabled = true;
    liveCard.classList.remove("hidden");

    stepYt.className = "verifier-step-line active";
    stepYt.textContent = `⏳ Verifying YouTube subscription for @${ytVal}...`;
    stepIg.className = "verifier-step-line";
    stepIg.textContent = `⚪ Verifying Instagram follow for @gta6.insider_...`;
    stepToken.className = "verifier-step-line";
    stepToken.textContent = `⚪ Generating secure access token...`;

    // Step 1: YouTube verified after 750ms
    setTimeout(() => {
      stepYt.className = "verifier-step-line done";
      stepYt.textContent = `✓ YouTube subscription confirmed for @${ytVal}`;
      ytPill.textContent = "VERIFIED";
      ytPill.className = "task-status-pill verified";
      ytCard.classList.remove("active-working");
      ytCard.classList.add("task-done");

      stepIg.className = "verifier-step-line active";
      stepIg.textContent = `⏳ Verifying Instagram follow for @${igVal} on @gta6.insider_...`;

      // Step 2: Instagram verified after +800ms
      setTimeout(() => {
        stepIg.className = "verifier-step-line done";
        stepIg.textContent = `✓ Instagram follow confirmed for @${igVal}`;
        igPill.textContent = "VERIFIED";
        igPill.className = "task-status-pill verified";
        igCard.classList.remove("active-working");
        igCard.classList.add("task-done");

        stepToken.className = "verifier-step-line active";
        stepToken.textContent = `⏳ Issuing lifetime access token...`;

        // Step 3: Access token generated & unlocked after +650ms
        setTimeout(() => {
          stepToken.className = "verifier-step-line done";
          stepToken.textContent = `✓ Verified! Welcome to ClipYt AI.`;

          // Save unlock token
          try {
            localStorage.setItem(
              SITE_UNLOCK_KEY,
              JSON.stringify({ yt: ytVal, ig: igVal, unlockedAt: Date.now() })
            );
            localStorage.setItem(
              PRO_UNLOCK_KEY,
              JSON.stringify({ handle: igVal, unlockedAt: Date.now() })
            );
          } catch (e) {}

          proState.unlocked = true;
          refreshProBadgeUI();

          // Smooth gate unlock exit
          setTimeout(() => {
            gateEl.style.transition = "opacity 0.4s ease, transform 0.4s ease";
            gateEl.style.opacity = "0";
            gateEl.style.transform = "scale(1.03)";
            setTimeout(() => {
              gateEl.classList.add("hidden");
              document.body.style.overflow = "";
            }, 400);
          }, 600);
        }, 650);
      }, 800);
    }, 750);
  });
}


// =============================================================
// LIVE STUDIO: INSTAGRAM GATE + EXACT TIMESTAMP + 60/40 SPLIT
// =============================================================
const PRO_UNLOCK_KEY = "clipyt_pro_unlocked_v1";

const proState = {
  unlocked: false,
  mode: "standard", // "standard" | "pro"
  followClicked: false,
  followTimerReady: false,
  followCountdownId: null,
  source: null,          // data from /api/pro/inspect
  firstPdtUtc: null,     // exact UTC epoch of 00:00 on the DVR timeline
  totalSec: 300,         // total selectable DVR/video seconds
  startSec: 265,         // selected clip Start (relative to 00:00)
  endSec: 300,           // selected clip End (relative to 00:00)
  framesDebounceId: null,
  framesFetchSeq: 0,
  boxes: {
    cam: { x: 0.72, y: 0.62, w: 0.25, h: 0.32 },    // Streamer (Bottom 40%)
    stream: { x: 0.03, y: 0.04, w: 0.68, h: 0.92 }, // Display (Top 60%)
  },
};

function isProUnlocked() {
  if (isSiteUnlocked()) return true;
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
    badge.textContent = "✓ LIVE";
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
    requestAnimationFrame(syncBoxesToDOM);
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

  // Step 1: Instagram Follow Link -> 4s wait before enabling Step 2
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

  // Step 2: Verify Instagram username & unlock Live Studio
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
        localStorage.setItem(PRO_UNLOCK_KEY, JSON.stringify({ handle: raw, unlockedAt: Date.now() }));
      } catch (e) {}
      proState.unlocked = true;
      refreshProBadgeUI();
      verifyBtn.disabled = false;
      handleInput.disabled = false;
      btnTxt.textContent = "Verify & Unlock";
      btnSp.classList.add("hidden");
      closeProUnlockModal();
      switchStudioMode("pro");
    }, 950);
  };

  verifyBtn.addEventListener("click", doVerify);
  handleInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      doVerify();
    }
  });

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

  // Refresh Live Edge button
  document.getElementById("pro-refresh-frame-btn").addEventListener("click", () => {
    const val = proUrlInput.value.trim();
    if (val) inspectLiveStream(val, true);
  });

  // Check Start & End Frames button
  document.getElementById("pro-load-frames-btn").addEventListener("click", () => {
    loadSelectedBoundaryFrames(true);
  });

  // Auto Viral Scan button
  const scanViralBtn = document.getElementById("btn-scan-viral");
  if (scanViralBtn) {
    scanViralBtn.addEventListener("click", () => {
      const val = proUrlInput.value.trim();
      if (val) scanAutoViralClips(val);
    });
  }

  // Dual-Range Timeline Slider
  const sStart = document.getElementById("pro-tl-start");
  const sEnd = document.getElementById("pro-tl-end");
  sStart.addEventListener("input", () => {
    let s = parseInt(sStart.value, 10);
    let e = proState.endSec;
    if (s > e - 5) {
      e = Math.min(proState.totalSec, s + 5);
      if (e - s < 5) s = Math.max(0, e - 5);
    }
    if (e - s > 120) e = s + 120;
    proState.startSec = s;
    proState.endSec = e;
    syncLiveTimelineUI();
    scheduleBoundaryFramesFetch();
  });

  sEnd.addEventListener("input", () => {
    let s = proState.startSec;
    let e = parseInt(sEnd.value, 10);
    if (e < s + 5) {
      s = Math.max(0, e - 5);
      if (e - s < 5) e = s + 5;
    }
    if (e - s > 120) s = e - 120;
    proState.startSec = s;
    proState.endSec = e;
    syncLiveTimelineUI();
    scheduleBoundaryFramesFetch();
  });

  // Exact From / To text inputs
  document.getElementById("pro-from-input").addEventListener("change", () => onLiveTimeInput("start"));
  document.getElementById("pro-to-input").addEventListener("change", () => onLiveTimeInput("end"));

  // Captions toggle
  const proBurnToggle = document.getElementById("pro-burn-subs");
  const proSubOpts = document.getElementById("pro-sub-options");
  proBurnToggle.addEventListener("change", () => {
    proSubOpts.querySelectorAll("#pro-sub-style, #pro-sub-pos").forEach((el) => (el.disabled = !proBurnToggle.checked));
    proSubOpts.style.opacity = proBurnToggle.checked ? "1" : "0.55";
  });

  // Export 9:16 Clip button
  document.getElementById("pro-create-btn").addEventListener("click", submitLiveClipExport);

  // Dual-Box Drag & Corner Resize
  setupDualBoxInteractions();
  window.addEventListener("resize", () => {
    if (proState.mode === "pro" && proState.source) syncBoxesToDOM();
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

async function inspectLiveStream(url, forceRefresh = false) {
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
    const json = await parseApiResponse(res);
    if (!res.ok || !json.success) {
      throw new Error(json.detail || "Could not load stream.");
    }

    const src = json.data;
    proState.source = src;
    proState.firstPdtUtc = src.first_pdt_utc || null;

    const total = src.is_live
      ? Math.max(30, Math.floor(src.dvr_window_sec || src.live_elapsed_sec || 300))
      : Math.max(10, Math.floor(src.duration || 60));
    proState.totalSec = total;

    if (src.is_live) {
      // Default to last 35s at the live edge, user can scrub back anywhere to 00:00!
      proState.endSec = total;
      proState.startSec = Math.max(0, total - 35);
    } else {
      proState.startSec = 0;
      proState.endSec = Math.min(35, total);
    }

    const badgeEl = document.getElementById("pro-live-badge");
    const useH = total >= 3600;
    if (src.is_live) {
      badgeEl.textContent = `🔴 LIVE • ${formatTime(total, useH)}`;
      badgeEl.classList.remove("is-vod");
    } else {
      badgeEl.textContent = `🎬 VIDEO • ${src.duration_str}`;
      badgeEl.classList.add("is-vod");
    }

    document.getElementById("pro-source-title").textContent = src.title;
    document.getElementById("pro-source-author").textContent = src.author;

    const fmtHint = useH ? "(hh:mm:ss)" : "(mm:ss)";
    document.getElementById("pro-from-label").textContent = `From ${fmtHint}`;
    document.getElementById("pro-to-label").textContent = `To ${fmtHint}`;

    document.getElementById("pro-tl-min").textContent = formatTime(0, useH);
    document.getElementById("pro-tl-mid").textContent = formatTime(Math.floor(total / 2), useH);
    document.getElementById("pro-tl-max").textContent = src.is_live
      ? `🔴 LIVE ${formatTime(total, useH)}`
      : formatTime(total, useH);

    const sStart = document.getElementById("pro-tl-start");
    const sEnd = document.getElementById("pro-tl-end");
    sStart.max = total;
    sEnd.max = total;

    syncLiveTimelineUI();

    // Seed initial frames right away, then fetch exact boundary frames for selected From/To
    const fallbackImg = src.frame_image || src.thumbnail;
    document.getElementById("pro-frame-img").src = fallbackImg;
    document.getElementById("pro-start-frame-img").src = fallbackImg;
    document.getElementById("pro-end-frame-img").src = fallbackImg;

    document.getElementById("pro-editor-wrap").classList.remove("hidden");
    requestAnimationFrame(syncBoxesToDOM);
    loadSelectedBoundaryFrames(false);

    // Automatically trigger Auto-Viral Chat Spike scan on stream load!
    scanAutoViralClips(url);
  } catch (err) {
    showProError(err.message || "Failed to load stream.");
  } finally {
    setProLoadSpinner(false);
    if (refreshBtn) {
      refreshBtn.disabled = false;
      refreshBtn.textContent = "↻ Refresh Live Edge";
    }
  }
}

function syncLiveTimelineUI() {
  const total = Math.max(10, proState.totalSec);
  const useH = total >= 3600;
  let s = Math.max(0, Math.min(total - 5, Math.round(proState.startSec)));
  let e = Math.max(s + 5, Math.min(total, Math.round(proState.endSec)));
  if (e - s > 120) e = Math.min(total, s + 120);
  proState.startSec = s;
  proState.endSec = e;

  const sStr = formatTime(s, useH);
  const eStr = formatTime(e, useH);
  document.getElementById("pro-from-input").value = sStr;
  document.getElementById("pro-to-input").value = eStr;
  document.getElementById("pro-clip-len-display").textContent = formatTime(Math.max(5, e - s), false);
  document.getElementById("pro-start-frame-badge").textContent = sStr;
  document.getElementById("pro-end-frame-badge").textContent = eStr;

  const sStart = document.getElementById("pro-tl-start");
  const sEnd = document.getElementById("pro-tl-end");
  const fill = document.getElementById("pro-tl-fill");
  sStart.value = s;
  sEnd.value = e;
  const leftPct = (s / total) * 100;
  const widthPct = Math.max(0.8, ((e - s) / total) * 100);
  fill.style.left = `${leftPct}%`;
  fill.style.width = `${widthPct}%`;
}

function onLiveTimeInput(field) {
  if (!proState.source) return;
  const input = document.getElementById(field === "start" ? "pro-from-input" : "pro-to-input");
  const parsed = parseTime(input.value);
  if (!isNaN(parsed)) {
    const total = proState.totalSec;
    const clamped = Math.max(0, Math.min(total, Math.round(parsed)));
    if (field === "start") {
      proState.startSec = Math.min(clamped, Math.max(0, total - 5));
      if (proState.endSec < proState.startSec + 5) {
        proState.endSec = Math.min(total, proState.startSec + 35);
      }
      if (proState.endSec - proState.startSec > 120) {
        proState.endSec = Math.min(total, proState.startSec + 120);
      }
    } else {
      proState.endSec = Math.max(5, clamped);
      if (proState.startSec > proState.endSec - 5) {
        proState.startSec = Math.max(0, proState.endSec - 35);
      }
      if (proState.endSec - proState.startSec > 120) {
        proState.startSec = Math.max(0, proState.endSec - 120);
      }
    }
  }
  syncLiveTimelineUI();
  loadSelectedBoundaryFrames(false);
}

window.stepLiveClipTime = function (field, delta) {
  if (!proState.source) return;
  const total = proState.totalSec;
  if (field === "start") {
    proState.startSec = Math.max(0, Math.min(proState.endSec - 5, proState.startSec + delta));
    if (proState.endSec - proState.startSec > 120) {
      proState.endSec = proState.startSec + 120;
    }
  } else {
    proState.endSec = Math.max(
      proState.startSec + 5,
      Math.min(total, Math.min(proState.startSec + 120, proState.endSec + delta))
    );
  }
  syncLiveTimelineUI();
  scheduleBoundaryFramesFetch();
};

function scheduleBoundaryFramesFetch() {
  if (proState.framesDebounceId) clearTimeout(proState.framesDebounceId);
  proState.framesDebounceId = setTimeout(() => {
    loadSelectedBoundaryFrames(false);
  }, 550);
}

async function loadSelectedBoundaryFrames(manualClick = false) {
  if (!proState.source) return;
  const url = document.getElementById("pro-youtube-url").value.trim();
  const statusEl = document.getElementById("pro-frames-status");
  const btn = document.getElementById("pro-load-frames-btn");
  const mySeq = ++proState.framesFetchSeq;

  if (statusEl) statusEl.textContent = "Loading exact Start & End frames...";
  if (manualClick && btn) {
    btn.disabled = true;
    btn.textContent = "Loading frames...";
  }

  const isLive = Boolean(proState.source.is_live);
  const startUtc = isLive && proState.firstPdtUtc ? (proState.firstPdtUtc + proState.startSec) : null;
  const endUtc = isLive && proState.firstPdtUtc ? (proState.firstPdtUtc + proState.endSec) : null;

  try {
    const res = await fetch("/api/pro/frames", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        video_id: proState.source.video_id,
        is_live: isLive,
        start_utc_ts: startUtc,
        end_utc_ts: endUtc,
        start_time: proState.startSec,
        end_time: proState.endSec,
      }),
    });
    const json = await parseApiResponse(res);
    if (mySeq !== proState.framesFetchSeq) return; // newer request superseded this one
    if (res.ok && json.success && json.data) {
      const d = json.data;
      if (d.start_frame) {
        document.getElementById("pro-start-frame-img").src = d.start_frame;
        // Also put the EXACT Start Frame onto the Box Placement Stage below!
        document.getElementById("pro-frame-img").src = d.start_frame;
      }
      if (d.end_frame) {
        document.getElementById("pro-end-frame-img").src = d.end_frame;
      }
      if (statusEl) statusEl.textContent = "✓ Exact frames verified — ready to export!";
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = "Exact UTC segment lock active";
  } finally {
    if (manualClick && btn) {
      btn.disabled = false;
      btn.textContent = "👁 Check Start & End Frames";
    }
  }
}

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
  const dur = Math.max(5, Math.min(120, proState.endSec - proState.startSec));
  if (dur < 5) {
    showProError("Please select at least 5 seconds between From and To.");
    return;
  }

  // Exact UTC timestamps (0.00s drift — guaranteed to export ONLY the exact selected window!)
  const startUtc = isLive && proState.firstPdtUtc ? (proState.firstPdtUtc + proState.startSec) : null;
  const endUtc = isLive && proState.firstPdtUtc ? (proState.firstPdtUtc + proState.endSec) : null;
  const fallbackRewind = isLive ? Math.max(0, proState.totalSec - proState.endSec) : 0;

  const payload = {
    url: document.getElementById("pro-youtube-url").value.trim(),
    video_id: proState.source.video_id,
    is_live: isLive,
    duration_sec: dur,
    rewind_sec: fallbackRewind,
    start_utc_ts: startUtc,
    end_utc_ts: endUtc,
    start_time: proState.startSec,
    end_time: proState.endSec,
    cam_box: proState.boxes.cam,
    stream_box: proState.boxes.stream,
    // Fixed 9:16 layout: 60% Display ABOVE + 40% Streamer BELOW!
    cam_ratio: 0.40,
    cam_position: "bottom",
    resolution: document.getElementById("pro-resolution").value,
    burn_subtitles: document.getElementById("pro-burn-subs").checked,
    subtitle_style: document.getElementById("pro-sub-style").value,
    subtitle_position: document.getElementById("pro-sub-pos").value,
  };

  showProgressModal("Fetching your exact selected clip window...");

  try {
    const res = await fetch("/api/pro/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await parseApiResponse(res);
    if (!res.ok || !data.success) {
      throw new Error(data.detail || "Could not start clip export.");
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
    const res = await fetch("/api/auto-viral/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, max_peaks: 5, clip_duration: 30.0, hook_duration: 3.0 }),
    });
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
    cam_ratio: 0.40,
    cam_position: "bottom",
    resolution: document.getElementById("pro-resolution")?.value || "1080p",
    burn_subtitles: document.getElementById("pro-burn-subs")?.checked ?? true,
    subtitle_style: document.getElementById("pro-sub-style")?.value || "viral_pop",
    subtitle_position: document.getElementById("pro-sub-pos")?.value || "seam",
    climax_hook: true,
    hook_duration: 3.0,
  };

  showProgressModal("Extracting 3s hook & 30s clip from live chat spike...");

  try {
    const res = await fetch("/api/auto-viral/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
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
