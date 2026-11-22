import { CesiumWorld } from "./cesium-view.js";
import { SyncCharts } from "./charts.js";
import { LaunchController } from "./launch-modal.js";

const SETTINGS_STORAGE_KEY = "groundframe_settings_v1";

const state = {
  config: {},
  sessionOverrides: {},
  flights: [],
  activeFlightId: null,
  flightData: null,
  frames: [],
  fps: 24,
  frameIndex: 0,
  playing: false,
  cameraMode: "orbit",
  wipeRatio: 0.5,
  activeKfIndex: 0,
  keyframesByFrame: new Map(),
};

const els = {
  gpuBadge: document.getElementById("gpu-badge"),
  flightSelect: document.getElementById("flight-select"),
  btnDeleteFlight: document.getElementById("btn-delete-flight"),
  btnNewFlight: document.getElementById("btn-new-flight"),
  btnExport: document.getElementById("btn-export"),
  exportMenu: document.getElementById("export-menu"),
  btnSettings: document.getElementById("btn-settings"),

  viewportGrid: document.getElementById("viewport-grid"),
  video: document.getElementById("drone-video"),
  videoEmpty: document.getElementById("video-empty"),
  videoFrameMeta: document.getElementById("video-frame-meta"),
  hudStatusPill: document.getElementById("hud-status-pill"),
  rejectionBanner: document.getElementById("rejection-banner"),
  rejectionDetail: document.getElementById("rejection-detail"),

  cesiumContainer: document.getElementById("cesium-container"),
  cesiumStage: document.getElementById("cesium-stage"),
  tilesBadge: document.getElementById("tiles-badge"),
  cameraModeGroup: document.getElementById("camera-mode-group"),
  toggleGt: document.getElementById("toggle-gt"),
  gtToggleWrap: document.getElementById("gt-toggle-wrap"),
  btnAlignTune: document.getElementById("btn-align-tune"),
  alignBar: document.getElementById("align-bar"),
  alignAlt: document.getElementById("align-alt"),
  alignAltVal: document.getElementById("align-alt-val"),
  alignFov: document.getElementById("align-fov"),
  alignFovVal: document.getElementById("align-fov-val"),
  btnAlignReset: document.getElementById("btn-align-reset"),

  wipeClip: document.getElementById("wipe-video-clip"),
  wipeCanvas: document.getElementById("wipe-video-canvas"),
  wipeTagRight: document.getElementById("wipe-tag-right"),
  wipeDivider: document.getElementById("wipe-divider"),

  btnPlay: document.getElementById("btn-play"),
  iconPlay: document.getElementById("icon-play"),
  iconPause: document.getElementById("icon-pause"),
  btnPrev: document.getElementById("btn-prev-frame"),
  btnNext: document.getElementById("btn-next-frame"),
  timelineClock: document.getElementById("timeline-clock"),
  timelineSlider: document.getElementById("timeline-slider"),
  timelineSegments: document.getElementById("timeline-segments"),
  timelineKfTicks: document.getElementById("timeline-kf-ticks"),
  timelinePlayhead: document.getElementById("timeline-playhead"),
  kfCountBadge: document.getElementById("kf-count-badge"),
  speedSelect: document.getElementById("speed-select"),

  btnCopyCoords: document.getElementById("btn-copy-coords"),
  telLatLon: document.getElementById("tel-latlon"),
  telAlt: document.getElementById("tel-alt"),
  telHeading: document.getElementById("tel-heading"),
  telPitchRoll: document.getElementById("tel-pitchroll"),
  telInliers: document.getElementById("tel-inliers"),
  telReproj: document.getElementById("tel-reproj"),

  scCoverage: document.getElementById("sc-coverage"),
  scKeyframes: document.getElementById("sc-keyframes"),
  scHoriz: document.getElementById("sc-horiz"),
  scP95: document.getElementById("sc-p95"),
  scVertRot: document.getElementById("sc-vertrot"),

  evidenceTabs: document.getElementById("evidence-tabs"),
  tabMatches: document.getElementById("tab-matches"),
  tabCharts: document.getElementById("tab-charts"),
  kfKindPills: document.getElementById("kf-kind-pills"),
  kfImage: document.getElementById("kf-image"),
  kfEmpty: document.getElementById("kf-empty"),
};

const world = new CesiumWorld(els.cesiumContainer, els.tilesBadge);
const charts = new SyncCharts({
  altCanvas: document.getElementById("chart-alt"),
  inliersCanvas: document.getElementById("chart-inliers"),
  errorCanvas: document.getElementById("chart-error"),
  errorRow: document.getElementById("chart-error-row"),
  onSeekIndex: (idx) => seekToFrameIndex(idx, true),
});

const launcher = new LaunchController({
  onFlightReady: async (flightId) => {
    await refreshFlights(flightId);
  },
  getSessionSettings: () => ({ ...state.config, ...state.sessionOverrides }),
});

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

function loadLocalOverrides() {
  try {
    const raw = localStorage.getItem(SETTINGS_STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

async function init() {
  state.sessionOverrides = loadLocalOverrides();
  const cfgRes = await fetch("/api/config");
  state.config = await cfgRes.json();

  if (state.config.gpu_name) {
    els.gpuBadge.textContent = state.config.gpu_name.replace("NVIDIA GeForce ", "");
  }

  // Apply any stored localStorage overrides to backend session + Cesium config
  const effective = { ...state.config };
  if (state.sessionOverrides.cesium_ion_token) effective.cesium_ion_token = state.sessionOverrides.cesium_ion_token;
  if (state.sessionOverrides.google_maps_api_key) effective.google_maps_api_key = state.sessionOverrides.google_maps_api_key;
  if (state.sessionOverrides.openrouter_model) effective.openrouter_model = state.sessionOverrides.openrouter_model;

  if (Object.keys(state.sessionOverrides).length) {
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.sessionOverrides),
    }).catch(() => {});
  }

  bindUIEvents();
  await world.init(effective);
  await refreshFlights();
  startSyncLoop();
}

// ---------------------------------------------------------------------------
// Flight Library & Loading
// ---------------------------------------------------------------------------

async function refreshFlights(selectFlightId = null) {
  const res = await fetch("/api/flights");
  const data = await res.json();
  state.flights = data.flights || [];

  els.flightSelect.innerHTML = "";
  if (!state.flights.length) {
    els.flightSelect.innerHTML = `<option value="">No flights yet — click + New Flight</option>`;
    els.btnDeleteFlight.disabled = true;
    return;
  }

  els.btnDeleteFlight.disabled = false;
  for (const f of state.flights) {
    const opt = document.createElement("option");
    opt.value = f.flight_id;
    const covStr = `${Number(f.coverage_pct ?? 0).toFixed(0)}% cov`;
    const errStr = f.median_horiz_m != null ? ` · ${f.median_horiz_m}m med` : "";
    const tag = f.status === "REJECTED" ? " [REJECTED]" : "";
    opt.textContent = `${f.name} — ${f.location} (${covStr}${errStr})${tag}`;
    els.flightSelect.appendChild(opt);
  }

  const target = selectFlightId && state.flights.some((f) => f.flight_id === selectFlightId)
    ? selectFlightId
    : state.flights[0].flight_id;
  els.flightSelect.value = target;
  await loadFlight(target);
}

async function loadFlight(flightId) {
  setPlaying(false);
  state.activeFlightId = flightId;
  const res = await fetch(`/api/flights/${encodeURIComponent(flightId)}`);
  if (!res.ok) return;
  const data = await res.json();
  state.flightData = data;

  const traj = data.trajectory || {};
  state.frames = traj.frames || [];
  state.fps = Number(traj.fps) || 24;
  state.frameIndex = 0;

  // Video source
  if (data.video_url) {
    els.video.hidden = false;
    els.videoEmpty.hidden = true;
    els.video.src = data.video_url;
    els.video.load();
  } else {
    els.video.removeAttribute("src");
    els.video.hidden = true;
    els.videoEmpty.hidden = false;
  }

  // Rejection banner
  const rejected = traj.summary?.rejected || traj.summary?.coverage_pct === 0;
  els.rejectionBanner.hidden = !rejected;
  if (rejected) {
    els.rejectionDetail.textContent = data.meta?.rejection_reason || traj.summary?.rejection_reason || "No frames could be anchored to the 3D map.";
  }

  // GT toggle availability
  const hasGt = state.frames.some((f) => f.gt != null);
  els.toggleGt.disabled = !hasGt;
  els.gtToggleWrap.style.opacity = hasGt ? "1" : "0.45";

  // Scorecard
  const sum = traj.summary || {};
  const m = sum.metrics || null;
  els.scCoverage.textContent = `${sum.coverage_pct ?? 0}% (${sum.localized_frames ?? 0}/${sum.total_frames ?? 0})`;
  els.scKeyframes.textContent = `${sum.keyframes ?? 0}`;
  els.scHoriz.textContent = m?.median_horiz_m != null ? `${m.median_horiz_m} m` : "—";
  els.scP95.textContent = m?.p95_horiz_m != null ? `${m.p95_horiz_m} m` : "—";
  els.scVertRot.textContent = m?.median_vert_m != null
    ? `${m.median_vert_m}m / ${m.median_rot_deg != null ? m.median_rot_deg + "°" : "—"}`
    : "—";

  // Keyframes map
  state.keyframesByFrame.clear();
  for (const kf of data.keyframes || []) {
    const key = kf.frame_id ?? 0;
    if (!state.keyframesByFrame.has(key)) state.keyframesByFrame.set(key, []);
    state.keyframesByFrame.get(key).push(kf);
  }
  renderKeyframeEvidence(0);

  // Timeline
  els.timelineSlider.max = Math.max(0, state.frames.length - 1);
  els.timelineSlider.value = 0;
  buildTimelineOverlay();

  // 3D World & Charts
  world.loadTrajectory(traj);
  charts.setTrajectory(traj);
  applyFrame(0, false);
}

// ---------------------------------------------------------------------------
// Timeline & Playback Sync
// ---------------------------------------------------------------------------

function fmtTime(sec) {
  const s = Math.max(0, Number(sec) || 0);
  const mins = Math.floor(s / 60);
  const rem = s - mins * 60;
  return `${String(mins).padStart(2, "0")}:${rem.toFixed(2).padStart(5, "0")}`;
}

function buildTimelineOverlay() {
  els.timelineSegments.innerHTML = "";
  els.timelineKfTicks.innerHTML = "";
  const n = state.frames.length;
  if (!n) return;

  // Group contiguous statuses into colored timeline segments
  const segs = [];
  const classify = (f) => (f.status === "LOCKED" || f.status === "TRACKING" ? "ok" : f.status === "PREDICTED" ? "pred" : "lost");
  let curKind = classify(state.frames[0]);
  let curLen = 1;
  for (let i = 1; i < n; i++) {
    const k = classify(state.frames[i]);
    if (k === curKind) curLen++;
    else {
      segs.push({ kind: curKind, len: curLen });
      curKind = k;
      curLen = 1;
    }
  }
  segs.push({ kind: curKind, len: curLen });

  for (const s of segs) {
    const div = document.createElement("div");
    div.className = `timeline-seg ${s.kind}`;
    div.style.flex = String(s.len);
    els.timelineSegments.appendChild(div);
  }

  let kfCount = 0;
  state.frames.forEach((f, idx) => {
    if (!f.is_keyframe) return;
    kfCount++;
    const tick = document.createElement("button");
    tick.className = "kf-tick";
    tick.style.left = `${n > 1 ? (idx / (n - 1)) * 100 : 0}%`;
    tick.title = `Keyframe #${f.frame_id} (${fmtTime(f.timestamp_s)}) · ${f.num_inliers} inliers`;
    tick.addEventListener("click", (e) => {
      e.stopPropagation();
      seekToFrameIndex(idx, true);
      switchEvidenceTab("matches");
      renderKeyframeEvidence(f.frame_id);
    });
    els.timelineKfTicks.appendChild(tick);
  });
  els.kfCountBadge.textContent = `${kfCount} KF`;
}

function seekToFrameIndex(idx, syncVideo = true) {
  if (!state.frames.length) return;
  const clamped = Math.max(0, Math.min(state.frames.length - 1, idx));
  state.frameIndex = clamped;
  const frame = state.frames[clamped];

  if (syncVideo && els.video.src && !els.video.hidden) {
    const targetSec = frame.timestamp_s ?? (frame.frame_id / state.fps);
    if (Math.abs(els.video.currentTime - targetSec) > 0.015) {
      els.video.currentTime = targetSec;
    }
  }
  applyFrame(clamped, true);
}

function applyFrame(idx, updateCharts = true) {
  const frame = state.frames[idx];
  if (!frame) return;
  const n = state.frames.length;
  const totalSec = state.frames[n - 1]?.timestamp_s ?? ((n - 1) / state.fps);

  els.timelineSlider.value = idx;
  els.timelinePlayhead.style.left = `${n > 1 ? (idx / (n - 1)) * 100 : 0}%`;
  els.timelineClock.textContent = `${fmtTime(frame.timestamp_s)} / ${fmtTime(totalSec)}`;
  els.videoFrameMeta.textContent = `${fmtTime(frame.timestamp_s)} · Frame ${frame.frame_id}`;

  // Status pill
  const st = frame.status || "LOST";
  els.hudStatusPill.textContent = st;
  els.hudStatusPill.className = "status-pill " + (
    st === "LOCKED" ? "status-locked" :
    st === "TRACKING" ? "status-tracking" :
    st === "PREDICTED" ? "status-predicted" : "status-lost"
  );

  // Telemetry HUD
  const est = frame.est;
  if (est) {
    els.telLatLon.textContent = `${est.lat.toFixed(6)}, ${est.lon.toFixed(6)}`;
    els.telAlt.textContent = `${est.alt_map_m?.toFixed(1) ?? "—"} m / ${est.alt_wgs84_m?.toFixed(1) ?? "—"} m`;
    els.telHeading.textContent = `${est.heading_deg != null ? est.heading_deg.toFixed(1) + "°" : "—"} / ${est.course_deg != null ? est.course_deg.toFixed(1) + "°" : "—"}`;
    els.telPitchRoll.textContent = `${est.pitch_deg != null ? est.pitch_deg.toFixed(1) + "°" : "—"} / ${est.roll_deg != null ? est.roll_deg.toFixed(1) + "°" : "—"}`;
  } else {
    els.telLatLon.textContent = "—";
    els.telAlt.textContent = "—";
    els.telHeading.textContent = "—";
    els.telPitchRoll.textContent = "—";
  }

  els.telInliers.textContent = `${frame.num_inliers ?? 0} / ${frame.num_tracked_points ?? 0}`;
  const rep = frame.reproj_error_px != null ? `${frame.reproj_error_px.toFixed(2)} px` : "—";
  const ms = frame.processing_time_s != null ? `${Math.round(frame.processing_time_s * 1000)} ms` : "—";
  els.telReproj.textContent = `${rep} · ${ms}`;

  // 3D World & Charts
  world.updateFrame(frame, false);
  if (updateCharts) charts.setIndex(idx);
  if (state.cameraMode === "wipe") drawWipeVideoFrame();
}

function setPlaying(play) {
  state.playing = play;
  els.iconPlay.hidden = play;
  els.iconPause.hidden = !play;

  if (!els.video.src || els.video.hidden) return;
  if (play) {
    // If at end of trajectory, restart
    if (state.frameIndex >= state.frames.length - 1) {
      seekToFrameIndex(0, true);
    }
    els.video.playbackRate = Number(els.speedSelect.value) || 1;
    els.video.play().catch(() => setPlaying(false));
  } else {
    els.video.pause();
  }
}

function findNearestFrameIndexForTime(tSec) {
  const frames = state.frames;
  if (!frames.length) return 0;
  let lo = 0, hi = frames.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((frames[mid].timestamp_s ?? 0) < tSec) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs((frames[lo - 1].timestamp_s ?? 0) - tSec) < Math.abs((frames[lo].timestamp_s ?? 0) - tSec)) {
    return lo - 1;
  }
  return lo;
}

function startSyncLoop() {
  const tick = () => {
    if (state.playing && els.video.src && !els.video.paused && state.frames.length) {
      const idx = findNearestFrameIndexForTime(els.video.currentTime);
      const maxTs = state.frames[state.frames.length - 1]?.timestamp_s ?? 0;
      if (idx !== state.frameIndex) {
        state.frameIndex = idx;
        applyFrame(idx, true);
      } else if (state.cameraMode === "wipe") {
        drawWipeVideoFrame();
      }
      if (els.video.currentTime >= maxTs + 0.05) {
        setPlaying(false);
      }
    } else if (state.cameraMode === "wipe") {
      drawWipeVideoFrame();
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// ---------------------------------------------------------------------------
// Wipe Compare Rendering
// ---------------------------------------------------------------------------

function drawWipeVideoFrame() {
  if (!els.video.src || els.video.readyState < 2) return;
  const stageRect = els.cesiumStage.getBoundingClientRect();
  if (stageRect.width < 10 || stageRect.height < 10) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const cw = Math.round(stageRect.width * dpr);
  const ch = Math.round(stageRect.height * dpr);
  if (els.wipeCanvas.width !== cw || els.wipeCanvas.height !== ch) {
    els.wipeCanvas.width = cw;
    els.wipeCanvas.height = ch;
  }
  const ctx = els.wipeCanvas.getContext("2d");
  ctx.clearRect(0, 0, cw, ch);

  // Match Cesium's vertical-FoV cover projection so horizon & roofs match 1:1
  const vw = els.video.videoWidth || 1920;
  const vh = els.video.videoHeight || 1080;
  const scale = ch / vh;
  const drawW = vw * scale;
  const drawX = (cw - drawW) / 2;
  ctx.drawImage(els.video, drawX, 0, drawW, ch);
}

function setWipeRatio(ratio) {
  state.wipeRatio = Math.max(0.05, Math.min(0.95, ratio));
  const pct = state.wipeRatio * 100;
  els.wipeClip.style.clipPath = `inset(0 ${100 - pct}% 0 0)`;
  els.wipeDivider.style.left = `${pct}%`;
}

// ---------------------------------------------------------------------------
// Keyframe Evidence & Tabs
// ---------------------------------------------------------------------------

function switchEvidenceTab(tab) {
  for (const b of els.evidenceTabs.querySelectorAll("button")) {
    b.classList.toggle("active", b.dataset.tab === tab);
  }
  els.tabMatches.hidden = tab !== "matches";
  els.tabCharts.hidden = tab !== "charts";
  els.kfKindPills.hidden = tab !== "matches";
  if (tab === "charts") charts.render();
}

function renderKeyframeEvidence(preferredFrameId = null) {
  els.kfKindPills.innerHTML = "";
  const allKfs = state.flightData?.keyframes || [];
  if (!allKfs.length) {
    els.kfImage.hidden = true;
    els.kfEmpty.hidden = false;
    return;
  }

  // Pick closest keyframe group
  let list = allKfs;
  if (preferredFrameId != null && state.keyframesByFrame.has(preferredFrameId)) {
    list = state.keyframesByFrame.get(preferredFrameId);
  }

  // Prefer 'keyframe' vis image first
  const sorted = [...list].sort((a, b) => {
    const rank = { keyframe: 0, fine: 1, coarse: 2, crops: 3 };
    return (rank[a.kind] ?? 9) - (rank[b.kind] ?? 9);
  });

  const showImg = (kf, activeBtn) => {
    els.kfImage.src = kf.url;
    els.kfImage.hidden = false;
    els.kfEmpty.hidden = true;
    for (const b of els.kfKindPills.querySelectorAll("button")) b.classList.toggle("active", b === activeBtn);
  };

  sorted.forEach((kf, i) => {
    const btn = document.createElement("button");
    btn.className = "btn-chip mono";
    const fidLabel = kf.frame_id != null ? `F${kf.frame_id} ` : "";
    btn.textContent = `${fidLabel}${kf.kind}`;
    btn.addEventListener("click", () => showImg(kf, btn));
    els.kfKindPills.appendChild(btn);
    if (i === 0) showImg(kf, btn);
  });
}

// ---------------------------------------------------------------------------
// UI Event Bindings
// ---------------------------------------------------------------------------

function bindUIEvents() {
  // Flight selector
  els.flightSelect.addEventListener("change", () => {
    if (els.flightSelect.value) loadFlight(els.flightSelect.value);
  });

  els.btnDeleteFlight.addEventListener("click", async () => {
    if (!state.activeFlightId) return;
    if (!confirm(`Delete flight "${state.activeFlightId}" from outputs/?`)) return;
    await fetch(`/api/flights/${encodeURIComponent(state.activeFlightId)}`, { method: "DELETE" });
    await refreshFlights();
  });

  // New Flight
  els.btnNewFlight.addEventListener("click", () => launcher.open());

  // Export dropdown
  els.btnExport.addEventListener("click", (e) => {
    e.stopPropagation();
    els.exportMenu.hidden = !els.exportMenu.hidden;
  });
  document.addEventListener("click", () => { els.exportMenu.hidden = true; });
  els.exportMenu.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-export]");
    if (!btn || !state.activeFlightId) return;
    window.location.href = `/api/flights/${encodeURIComponent(state.activeFlightId)}/export/${btn.dataset.export}`;
  });

  // Camera modes
  els.cameraModeGroup.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-mode]");
    if (!btn) return;
    setCameraMode(btn.dataset.mode);
  });

  // GT toggle
  els.toggleGt.addEventListener("change", () => {
    world.setShowGt(els.toggleGt.checked);
  });

  // Align fine-tune controls
  els.btnAlignTune.addEventListener("click", () => {
    els.alignBar.hidden = !els.alignBar.hidden;
    els.btnAlignTune.classList.toggle("active", !els.alignBar.hidden);
    setTimeout(() => world.resize(), 40);
  });
  const onAlignChange = () => {
    const a = Number(els.alignAlt.value);
    const f = Number(els.alignFov.value);
    els.alignAltVal.textContent = `${a >= 0 ? "+" : ""}${a.toFixed(1)} m`;
    els.alignFovVal.textContent = `${f >= 0 ? "+" : ""}${f.toFixed(1)}°`;
    world.setAlignment(a, f);
  };
  els.alignAlt.addEventListener("input", onAlignChange);
  els.alignFov.addEventListener("input", onAlignChange);
  els.btnAlignReset.addEventListener("click", () => {
    els.alignAlt.value = 0;
    els.alignFov.value = 0;
    onAlignChange();
  });

  // Wipe divider drag
  let draggingWipe = false;
  els.wipeDivider.addEventListener("pointerdown", (e) => {
    draggingWipe = true;
    els.wipeDivider.setPointerCapture(e.pointerId);
  });
  els.wipeDivider.addEventListener("pointermove", (e) => {
    if (!draggingWipe) return;
    const rect = els.cesiumStage.getBoundingClientRect();
    setWipeRatio((e.clientX - rect.left) / Math.max(1, rect.width));
  });
  els.wipeDivider.addEventListener("pointerup", () => { draggingWipe = false; });
  els.wipeDivider.addEventListener("pointercancel", () => { draggingWipe = false; });

  // Transport controls
  els.btnPlay.addEventListener("click", () => setPlaying(!state.playing));
  els.btnPrev.addEventListener("click", () => { setPlaying(false); seekToFrameIndex(state.frameIndex - 1, true); });
  els.btnNext.addEventListener("click", () => { setPlaying(false); seekToFrameIndex(state.frameIndex + 1, true); });
  els.timelineSlider.addEventListener("input", () => {
    setPlaying(false);
    seekToFrameIndex(Number(els.timelineSlider.value), true);
  });
  els.speedSelect.addEventListener("change", () => {
    els.video.playbackRate = Number(els.speedSelect.value) || 1;
  });
  els.video.addEventListener("seeked", () => {
    if (state.cameraMode === "wipe") drawWipeVideoFrame();
  });

  // Copy coords
  els.btnCopyCoords.addEventListener("click", async () => {
    const f = state.frames[state.frameIndex];
    if (!f?.est) return;
    const txt = `${f.est.lat.toFixed(6)}, ${f.est.lon.toFixed(6)}`;
    await navigator.clipboard.writeText(txt).catch(() => {});
    els.btnCopyCoords.textContent = "Copied!";
    setTimeout(() => { els.btnCopyCoords.textContent = "Copy Lat/Lon"; }, 1100);
  });

  // Evidence tabs & Lightbox
  els.evidenceTabs.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (btn) switchEvidenceTab(btn.dataset.tab);
  });
  els.kfImage.addEventListener("click", () => {
    if (!els.kfImage.src) return;
    document.getElementById("lightbox-img").src = els.kfImage.src;
    document.getElementById("modal-lightbox").hidden = false;
  });

  // Settings modal
  els.btnSettings.addEventListener("click", openSettingsModal);
  document.getElementById("form-settings").addEventListener("submit", saveSettingsModal);

  // Modal close buttons
  for (const btn of document.querySelectorAll("[data-close]")) {
    btn.addEventListener("click", () => {
      document.getElementById(btn.dataset.close).hidden = true;
    });
  }

  // Keyboard shortcuts
  window.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (e.code === "Space") { e.preventDefault(); setPlaying(!state.playing); }
    else if (e.code === "ArrowLeft") { e.preventDefault(); setPlaying(false); seekToFrameIndex(state.frameIndex - 1, true); }
    else if (e.code === "ArrowRight") { e.preventDefault(); setPlaying(false); seekToFrameIndex(state.frameIndex + 1, true); }
    else if (e.key === "1") setCameraMode("orbit");
    else if (e.key === "2") setCameraMode("chase");
    else if (e.key === "3") setCameraMode("drone_eye");
    else if (e.key === "4") setCameraMode("wipe");
  });
}

function setCameraMode(mode) {
  state.cameraMode = mode;
  for (const b of els.cameraModeGroup.querySelectorAll("button")) {
    b.classList.toggle("active", b.dataset.mode === mode);
  }
  const isWipe = mode === "wipe";
  const isFp = mode === "drone_eye" || isWipe;

  els.viewportGrid.classList.toggle("wipe-active", isWipe);
  els.wipeClip.hidden = !isWipe;
  els.wipeTagRight.hidden = !isWipe;
  els.wipeDivider.hidden = !isWipe;
  els.btnAlignTune.hidden = !isFp;
  if (!isFp) {
    els.alignBar.hidden = true;
    els.btnAlignTune.classList.remove("active");
  }

  world.setMode(mode);
  setTimeout(() => {
    world.resize();
    if (isWipe) {
      setWipeRatio(state.wipeRatio);
      drawWipeVideoFrame();
    }
  }, 40);
}

async function openSettingsModal() {
  const res = await fetch("/api/settings");
  const s = await res.json();
  document.getElementById("state-cesium").textContent = s.cesium_ion_token_set ? `configured (${s.cesium_ion_token_masked})` : "not set";
  document.getElementById("state-gmaps").textContent = s.google_maps_api_key_set ? `configured (${s.google_maps_api_key_masked})` : "not set";
  document.getElementById("state-openrouter").textContent = s.openrouter_api_key_set ? `configured (${s.openrouter_api_key_masked})` : "not set";
  document.getElementById("set-cesium-token").value = "";
  document.getElementById("set-gmaps-key").value = "";
  document.getElementById("set-openrouter-key").value = "";
  document.getElementById("set-openrouter-model").value = s.openrouter_model || "google/gemini-2.5-flash";
  document.getElementById("modal-settings").hidden = false;
}

async function saveSettingsModal(e) {
  e.preventDefault();
  const payload = {
    cesium_ion_token: document.getElementById("set-cesium-token").value.trim() || null,
    google_maps_api_key: document.getElementById("set-gmaps-key").value.trim() || null,
    openrouter_api_key: document.getElementById("set-openrouter-key").value.trim() || null,
    openrouter_model: document.getElementById("set-openrouter-model").value.trim() || null,
  };

  const merged = { ...state.sessionOverrides };
  for (const [k, v] of Object.entries(payload)) {
    if (v) merged[k] = v;
  }
  state.sessionOverrides = merged;
  localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(merged));

  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const updated = await res.json();
  state.config = updated.config;
  document.getElementById("modal-settings").hidden = true;

  // Re-initialize CesiumJS if user changed Cesium or Google Maps token
  if (payload.cesium_ion_token || payload.google_maps_api_key) {
    await world.init(state.config);
    if (state.flightData?.trajectory) {
      world.loadTrajectory(state.flightData.trajectory);
      applyFrame(state.frameIndex, false);
    }
  }
}

init();
