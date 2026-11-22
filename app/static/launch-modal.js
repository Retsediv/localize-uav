/* global L */

/**
 * "+ New Flight" Engineering Form modal + Live 5-Step Pipeline Stepper (SSE).
 */

export class LaunchController {
  constructor({ onFlightReady, getSessionSettings }) {
    this.onFlightReady = onFlightReady;
    this.getSessionSettings = getSessionSettings;

    this.resources = { videos: [], maps: [] };
    this.uploadedVideoPath = null;
    this.uploadedIntrinsicsPath = null;
    this.uploadedGtPath = null;

    this.locMode = "vpr";
    this.calibMode = "auto";
    this.pinLat = 47.37;
    this.pinLon = 8.5425;
    this.lastPayload = null;

    this.leafletMap = null;
    this.pinMarker = null;
    this.pinCircle = null;

    this.activeJobId = null;
    this.activeFlightId = null;
    this.eventSource = null;

    this._bindForm();
    this._bindStepper();
  }

  async open() {
    this._showError("");
    document.getElementById("modal-new-flight").hidden = false;
    await this._loadResources();
    this._updateVlmHint();
  }

  close() {
    document.getElementById("modal-new-flight").hidden = true;
  }

  async _loadResources() {
    try {
      const res = await fetch("/api/resources");
      this.resources = await res.json();
    } catch (err) {
      console.error("Failed to fetch resources:", err);
    }

    const vidSel = document.getElementById("select-local-video");
    vidSel.innerHTML = `<option value="">— Select existing video —</option>`;
    for (const v of this.resources.videos || []) {
      const opt = document.createElement("option");
      opt.value = v.path;
      opt.textContent = v.label;
      opt.dataset.name = v.id || "";
      opt.dataset.suggestedMap = v.suggested_map || "";
      vidSel.appendChild(opt);
    }

    const mapSel = document.getElementById("select-local-map");
    mapSel.innerHTML = "";
    for (const m of this.resources.maps || []) {
      const opt = document.createElement("option");
      opt.value = m.path;
      opt.textContent = m.label;
      mapSel.appendChild(opt);
    }
    if (!mapSel.options.length) {
      mapSel.innerHTML = `<option value="">No local maps cached yet</option>`;
    }
  }

  _updateVlmHint() {
    const cfg = this.getSessionSettings();
    const vprHint = document.getElementById("vpr-mode-hint");
    if (vprHint) {
      if (cfg.vpr_ready) {
        const tiles = Number(cfg.vpr_num_tiles || 0).toLocaleString();
        vprHint.innerHTML = `Offline Cross-View VPR (active winner: <code>${cfg.vpr_active_model}</code> · <strong>${tiles} tiles</strong> across 10 Swiss regions) + RoMaV2 &amp; PnP-RANSAC verification. Zero cloud APIs.`;
      } else {
        vprHint.innerHTML = `<span style="color: var(--amber)">Offline VPR index not built yet.</span> Run <code>make build-index</code> or use <strong>Global VLM</strong> / <strong>Pin on Map</strong>.`;
      }
    }
    const hint = document.getElementById("vlm-mode-hint");
    if (!cfg.openrouter_configured && !cfg.openrouter_api_key) {
      hint.innerHTML = `<span style="color: var(--amber)">OPENROUTER_API_KEY not set yet.</span> Add it in <code>.env</code> or ⚙ Settings for Global VLM, or use <strong>Offline VPR</strong> / <strong>Pin on Map</strong>.`;
    } else {
      const model = cfg.openrouter_model || "google/gemini-2.5-flash";
      hint.innerHTML = `OpenRouter Vision (<code>${model}</code>) infers approximate (lat, lon) anywhere in Switzerland → matches cached local maps or auto-fetches swisstopo 3D tiles.`;
    }
  }

  _initLeafletMap() {
    if (this.leafletMap) {
      setTimeout(() => this.leafletMap.invalidateSize(), 60);
      return;
    }
    const mapEl = document.getElementById("pin-map");
    this.leafletMap = L.map(mapEl, { zoomControl: true }).setView([this.pinLat, this.pinLon], 13);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap",
    }).addTo(this.leafletMap);

    this.pinMarker = L.marker([this.pinLat, this.pinLon]).addTo(this.leafletMap);
    // Draw Switzerland swisstopo coverage bounding box
    L.rectangle([[45.78, 5.90], [47.85, 10.55]], {
      color: "#80f2be",
      weight: 1.2,
      dashArray: "5 5",
      fill: false,
      opacity: 0.55,
      interactive: false,
    }).addTo(this.leafletMap);

    const radius = Number(document.getElementById("input-pin-radius").value) || 600;
    this.pinCircle = L.circle([this.pinLat, this.pinLon], {
      radius,
      color: "#80f2be",
      fillColor: "#80f2be",
      fillOpacity: 0.14,
      weight: 1.5,
    }).addTo(this.leafletMap);

    this.leafletMap.on("click", (ev) => {
      this._setPin(ev.latlng.lat, ev.latlng.lng, false);
    });
    this._updatePinChStatus();
    setTimeout(() => this.leafletMap.invalidateSize(), 100);
  }

  _updatePinChStatus() {
    const el = document.getElementById("pin-ch-status");
    if (!el) return;
    const inCh = this.pinLat >= 45.78 && this.pinLat <= 47.85 && this.pinLon >= 5.90 && this.pinLon <= 10.55;
    el.textContent = inCh ? "Inside CH (swisstopo)" : "Outside CH (unsupported)";
    el.classList.toggle("outside", !inCh);
  }

  _setPin(lat, lon, pan = true) {
    this.pinLat = Number(lat);
    this.pinLon = Number(lon);
    document.getElementById("pin-coords-readout").textContent = `${this.pinLat.toFixed(4)}, ${this.pinLon.toFixed(4)}`;
    this._updatePinChStatus();
    if (this.pinMarker) this.pinMarker.setLatLng([this.pinLat, this.pinLon]);
    if (this.pinCircle) this.pinCircle.setLatLng([this.pinLat, this.pinLon]);
    if (pan && this.leafletMap) this.leafletMap.setView([this.pinLat, this.pinLon], 14);
  }

  _selectLocMode(mode) {
    this.locMode = mode;
    const locGroup = document.getElementById("loc-mode-group");
    for (const b of locGroup.querySelectorAll("button")) b.classList.toggle("active", b.dataset.loc === mode);
    document.getElementById("loc-panel-vpr").hidden = mode !== "vpr";
    document.getElementById("loc-panel-auto").hidden = mode !== "auto";
    document.getElementById("loc-panel-pin").hidden = mode !== "pin";
    document.getElementById("loc-panel-local").hidden = mode !== "local_map";
    if (mode === "pin") this._initLeafletMap();
  }

  _updateAdvancedSummary() {
    const el = document.getElementById("advanced-summary");
    if (!el) return;
    const calibLabel = this.calibMode === "auto" ? "Auto FoV" : this.calibMode === "file" ? "intrinsics.json" : `FoV ${document.getElementById("input-manual-fov").value}°`;
    const matcher = document.getElementById("select-fine-matcher").value;
    const skip = document.getElementById("input-skip-frames").value || "1";
    const accum = document.getElementById("input-accumulate-points")?.checked ? "accum=on" : "accum=off";
    const vis = document.getElementById("input-save-kf-vis").checked ? "vis=on" : "vis=off";
    el.textContent = `${calibLabel} · ${matcher} · skip=${skip} · ${accum} · ${vis}`;
  }

  _bindForm() {
    const dropzone = document.getElementById("video-dropzone");
    const fileInput = document.getElementById("input-video-file");
    const vidSel = document.getElementById("select-local-video");
    const nameInput = document.getElementById("input-flight-name");

    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
    dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
      const file = e.dataTransfer?.files?.[0];
      if (file) this._uploadVideo(file);
    });
    fileInput.addEventListener("change", () => {
      const file = fileInput.files?.[0];
      if (file) this._uploadVideo(file);
    });

    vidSel.addEventListener("change", () => {
      if (vidSel.value) {
        this.uploadedVideoPath = null;
        document.getElementById("video-dropzone-text").innerHTML =
          `<strong>Drag &amp; drop .mp4 / .mov</strong> or click to upload<span class="muted-sm">Using local video selected on right</span>`;
        const selOpt = vidSel.selectedOptions[0];
        if (selOpt?.dataset.name && !nameInput.value.trim()) {
          nameInput.value = selOpt.dataset.name.replace(/_/g, " ");
        }
        if (selOpt?.dataset.suggestedMap) {
          const mapSel = document.getElementById("select-local-map");
          for (const opt of mapSel.options) {
            if (opt.value === selOpt.dataset.suggestedMap) {
              mapSel.value = opt.value;
              break;
            }
          }
        }
      }
    });

    // Location mode tabs
    const locGroup = document.getElementById("loc-mode-group");
    locGroup.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-loc]");
      if (!btn) return;
      this._selectLocMode(btn.dataset.loc);
    });

    // Clickable cities on the Switzerland coverage mini-map
    for (const g of document.querySelectorAll("[data-ch-city]")) {
      g.addEventListener("click", () => {
        const [lat, lon] = g.dataset.chCity.split(",").map(Number);
        this._selectLocMode("pin");
        this._setPin(lat, lon, true);
      });
    }

    // Radius sliders
    const vprRad = document.getElementById("input-vpr-radius");
    vprRad?.addEventListener("input", () => {
      document.getElementById("vpr-radius-val").textContent = `${vprRad.value} m`;
    });
    const autoRad = document.getElementById("input-auto-radius");
    autoRad.addEventListener("input", () => {
      document.getElementById("auto-radius-val").textContent = `${autoRad.value} m`;
    });
    const pinRad = document.getElementById("input-pin-radius");
    pinRad.addEventListener("input", () => {
      document.getElementById("pin-radius-val").textContent = `${pinRad.value} m`;
      if (this.pinCircle) this.pinCircle.setRadius(Number(pinRad.value));
    });

    // Quick pin presets
    for (const btn of document.querySelectorAll("[data-pin]")) {
      btn.addEventListener("click", () => {
        const [lat, lon] = btn.dataset.pin.split(",").map(Number);
        this._setPin(lat, lon, true);
      });
    }

    // Calibration mode tabs
    const calibGroup = document.getElementById("calib-mode-group");
    calibGroup.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-calib]");
      if (!btn) return;
      this.calibMode = btn.dataset.calib;
      for (const b of calibGroup.querySelectorAll("button")) b.classList.toggle("active", b === btn);
      document.getElementById("calib-panel-file").hidden = this.calibMode !== "file";
      document.getElementById("calib-panel-manual").hidden = this.calibMode !== "manual_fov";
      this._updateAdvancedSummary();
    });

    for (const id of ["select-fine-matcher", "input-skip-frames", "input-accumulate-points", "input-save-kf-vis", "input-manual-fov"]) {
      document.getElementById(id)?.addEventListener("change", () => this._updateAdvancedSummary());
    }
    this._updateAdvancedSummary();

    // Aux file uploads (intrinsics & GT)
    document.getElementById("input-intrinsics-file").addEventListener("change", async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const statusEl = document.getElementById("intrinsics-status");
      statusEl.textContent = "Uploading...";
      const res = await this._uploadAux(file, "intrinsics");
      if (res) {
        this.uploadedIntrinsicsPath = res.path;
        statusEl.textContent = `Uploaded: ${res.filename}`;
      }
    });

    document.getElementById("input-gt-file").addEventListener("change", async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const statusEl = document.getElementById("gt-file-status");
      statusEl.textContent = "Uploading...";
      const res = await this._uploadAux(file, "gt");
      if (res) {
        this.uploadedGtPath = res.path;
        statusEl.textContent = `Attached GT: ${res.filename}`;
      }
    });

    // Submit form
    document.getElementById("form-new-flight").addEventListener("submit", (e) => {
      e.preventDefault();
      this._submitJob();
    });
  }

  async _uploadVideo(file) {
    const label = document.getElementById("video-dropzone-text");
    label.innerHTML = `<strong>Uploading ${file.name}...</strong><span class="muted-sm">Probing codec &amp; preparing browser-playable MP4</span>`;
    this._showError("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch("/api/upload/video", { method: "POST", body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Upload failed (${res.status})`);
      }
      const data = await res.json();
      this.uploadedVideoPath = data.path;
      document.getElementById("select-local-video").value = "";
      const nameInput = document.getElementById("input-flight-name");
      if (!nameInput.value.trim()) {
        nameInput.value = file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ");
      }
      const tcNote = data.transcoded ? " (transcoded to H.264)" : "";
      const infoStr = data.info?.width ? `${data.info.width}×${data.info.height} · ${data.info.frames}f` : "";
      label.innerHTML = `<strong style="color:var(--mint)">Ready: ${file.name}${tcNote}</strong><span class="muted-sm mono">${infoStr}</span>`;
    } catch (err) {
      label.innerHTML = `<strong>Upload failed</strong><span class="muted-sm">${err.message}</span>`;
      this._showError(err.message);
    }
  }

  async _uploadAux(file, kind) {
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`/api/upload/file?kind=${encodeURIComponent(kind)}`, { method: "POST", body: fd });
      if (!res.ok) throw new Error(`Upload failed (${res.status})`);
      return await res.json();
    } catch (err) {
      this._showError(err.message);
      return null;
    }
  }

  _showError(msg) {
    const el = document.getElementById("form-new-flight-error");
    el.textContent = msg;
    el.hidden = !msg;
  }

  async _submitJob() {
    this._showError("");
    const videoPath = this.uploadedVideoPath || document.getElementById("select-local-video").value;
    if (!videoPath) {
      this._showError("Please upload a video or select an existing video from data/videos/.");
      return;
    }

    const session = this.getSessionSettings();
    const payload = {
      name: document.getElementById("input-flight-name").value.trim() || null,
      video_path: videoPath,
      location_mode: this.locMode,
      vpr_model: this.locMode === "vpr" ? (document.getElementById("select-vpr-model").value || null) : null,
      pin_lat: this.locMode === "pin" ? this.pinLat : null,
      pin_lon: this.locMode === "pin" ? this.pinLon : null,
      radius_m: Number(
        this.locMode === "pin"
          ? document.getElementById("input-pin-radius").value
          : this.locMode === "vpr"
            ? document.getElementById("input-vpr-radius").value
            : document.getElementById("input-auto-radius").value
      ),
      local_map_path: this.locMode === "local_map" ? document.getElementById("select-local-map").value : null,
      calibration_mode: this.calibMode,
      intrinsics_path: this.calibMode === "file" ? this.uploadedIntrinsicsPath : null,
      manual_fov: this.calibMode === "manual_fov" ? Number(document.getElementById("input-manual-fov").value) : null,
      fine_matcher: document.getElementById("select-fine-matcher").value,
      skip_frames: Number(document.getElementById("input-skip-frames").value) || 1,
      start_frame: document.getElementById("input-start-frame").value ? Number(document.getElementById("input-start-frame").value) : null,
      end_frame: document.getElementById("input-end-frame").value ? Number(document.getElementById("input-end-frame").value) : null,
      accumulate_points: document.getElementById("input-accumulate-points")?.checked ?? true,
      save_keyframe_vis: document.getElementById("input-save-kf-vis").checked,
      gt_path: this.uploadedGtPath || null,
      openrouter_api_key: session.openrouter_api_key || null,
      openrouter_model: session.openrouter_model || null,
    };
    this.lastPayload = payload;
    await this._launchPayload(payload);
  }

  async _launchPayload(payload) {
    try {
      const btn = document.getElementById("btn-run-flight");
      btn.disabled = true;
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      btn.disabled = false;
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Failed to launch job (${res.status})`);
      }
      const job = await res.json();
      this.close();
      this._openStepper(job);
    } catch (err) {
      document.getElementById("btn-run-flight").disabled = false;
      this._showError(err.message);
    }
  }

  _bindStepper() {
    document.getElementById("btn-cancel-job").addEventListener("click", async () => {
      if (!this.activeJobId) return;
      await fetch(`/api/jobs/${this.activeJobId}/cancel`, { method: "POST" });
    });
    document.getElementById("btn-retry-vlm").addEventListener("click", async () => {
      if (!this.lastPayload) return;
      const retryPayload = { ...this.lastPayload, location_mode: "auto" };
      this._closeStepper();
      await this._launchPayload(retryPayload);
    });
    document.getElementById("btn-stepper-close").addEventListener("click", () => {
      this._closeStepper();
    });
    document.getElementById("btn-stepper-open").addEventListener("click", () => {
      const fid = this.activeFlightId;
      this._closeStepper();
      if (fid && this.onFlightReady) this.onFlightReady(fid);
    });
  }

  _openStepper(job) {
    this.activeJobId = job.job_id;
    this.activeFlightId = job.flight_id;
    document.getElementById("stepper-flight-id").textContent = `${job.name} (${job.flight_id})`;
    document.getElementById("stepper-log").textContent = "";
    document.getElementById("stepper-progress-fill").style.width = "0%";
    document.getElementById("stepper-progress-meta").textContent = "Starting job...";
    document.getElementById("btn-cancel-job").hidden = false;
    document.getElementById("btn-retry-vlm").hidden = true;
    document.getElementById("btn-stepper-open").hidden = true;
    document.getElementById("btn-stepper-close").hidden = true;
    const candWrap = document.getElementById("vpr-cand-table-wrap");
    if (candWrap) { candWrap.innerHTML = ""; candWrap.hidden = true; }
    document.getElementById("modal-stepper").hidden = false;

    this._renderStepperState(job);

    if (this.eventSource) this.eventSource.close();
    this.eventSource = new EventSource(`/api/jobs/${job.job_id}/events`);

    this.eventSource.addEventListener("state", (e) => {
      const data = JSON.parse(e.data);
      this._renderStepperState(data);
    });

    this.eventSource.addEventListener("log", (e) => {
      const { line } = JSON.parse(e.data);
      const logEl = document.getElementById("stepper-log");
      logEl.textContent += line + "\n";
      logEl.scrollTop = logEl.scrollHeight;
    });

    this.eventSource.addEventListener("done", (e) => {
      const data = JSON.parse(e.data);
      this._renderStepperState(data);
      if (this.eventSource) {
        this.eventSource.close();
        this.eventSource = null;
      }
      document.getElementById("btn-cancel-job").hidden = true;
      document.getElementById("btn-stepper-close").hidden = false;

      if (data.status === "completed") {
        document.getElementById("btn-stepper-open").hidden = false;
        setTimeout(() => {
          if (!document.getElementById("modal-stepper").hidden && this.activeFlightId === data.flight_id) {
            this._closeStepper();
            if (this.onFlightReady) this.onFlightReady(data.flight_id);
          }
        }, 900);
      } else if (data.status === "rejected") {
        document.getElementById("btn-stepper-open").hidden = false;
        document.getElementById("btn-stepper-open").textContent = "Inspect Rejected Run";
        if (data.location_mode === "vpr" && this.lastPayload) {
          document.getElementById("btn-retry-vlm").hidden = false;
        }
      }
    });
  }

  _closeStepper() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
    document.getElementById("modal-stepper").hidden = true;
    document.getElementById("btn-stepper-open").textContent = "Open in Studio";
    document.getElementById("btn-retry-vlm").hidden = true;
  }

  _renderStepperState(job) {
    const step1Label = document.getElementById("step1-label");
    if (step1Label) {
      step1Label.textContent =
        job.location_mode === "vpr"
          ? "Offline VPR + PnP Verification"
          : job.location_mode === "auto"
            ? "Global VLM Region Detect"
            : "Region Prior";
    }

    const candWrap = document.getElementById("vpr-cand-table-wrap");
    if (candWrap && job.vpr_result?.candidates?.length) {
      const rows = job.vpr_result.candidates
        .map((c, i) => {
          const badge = c.accepted
            ? `<span class="vpr-badge-ok">✓ ACCEPTED</span>`
            : `<span class="vpr-badge-no">✗ rejected</span>`;
          return `<tr class="${c.accepted ? "accepted" : ""}">
            <td>#${i + 1}</td>
            <td>${c.region} (${c.size_m}m · ${c.rot_deg}°)</td>
            <td>${Number(c.score).toFixed(3)}</td>
            <td>${c.inliers}</td>
            <td>${badge}</td>
          </tr>`;
        })
        .join("");
      candWrap.innerHTML = `<table class="vpr-cand-table">
        <thead><tr><th>#</th><th>Candidate Tile</th><th>Sim</th><th>PnP Inliers</th><th>Verdict</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
      candWrap.hidden = false;
    }
    const pill = document.getElementById("stepper-status-pill");
    const st = (job.status || "running").toUpperCase();
    pill.textContent = st;
    pill.className = "status-pill " + (
      st === "COMPLETED" ? "status-locked" :
      st === "REJECTED" || st === "FAILED" || st === "CANCELLED" ? "status-lost" :
      "status-tracking"
    );

    const list = document.getElementById("stepper-list");
    for (const s of job.steps || []) {
      const li = list.querySelector(`li[data-step="${s.id}"]`);
      if (!li) continue;
      li.className = s.status || "pending";
      const det = li.querySelector(".step-detail");
      if (det) det.textContent = s.detail || (s.status === "pending" ? "Waiting..." : s.status);
    }

    const fill = document.getElementById("stepper-progress-fill");
    const meta = document.getElementById("stepper-progress-meta");
    if (job.frame_total > 0) {
      const pct = Math.min(100, Math.round((job.frame_current / job.frame_total) * 100));
      fill.style.width = `${pct}%`;
      meta.textContent = `Frame ${job.frame_current} / ${job.frame_total} (${pct}%) · ${job.fps ? job.fps.toFixed(1) + " FPS" : "—"} ${job.tracking_badge ? "· " + job.tracking_badge : ""}`;
    } else if (job.status === "completed") {
      fill.style.width = "100%";
      meta.textContent = "Completed — opening Output Studio...";
    } else if (job.error) {
      meta.textContent = job.error;
    }
  }
}
