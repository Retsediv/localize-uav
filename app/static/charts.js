/**
 * Synchronized Canvas 2D telemetry & error charts with interactive playhead scrubbing.
 */

export class SyncCharts {
  constructor({ altCanvas, inliersCanvas, errorCanvas, errorRow, onSeekIndex }) {
    this.altCanvas = altCanvas;
    this.inliersCanvas = inliersCanvas;
    this.errorCanvas = errorCanvas;
    this.errorRow = errorRow;
    this.onSeekIndex = onSeekIndex;

    this.frames = [];
    this.currentIndex = 0;
    this.hasGt = false;
    this.useSmooth = true;

    for (const c of [this.altCanvas, this.inliersCanvas, this.errorCanvas]) {
      if (!c) continue;
      c.addEventListener("pointerdown", (e) => this._handleSeek(e, c));
    }
    window.addEventListener("resize", () => this.render());
  }

  setTrajectory(trajectory, useSmooth = true) {
    this.frames = trajectory?.frames || [];
    this.useSmooth = Boolean(useSmooth);
    this.hasGt = this.frames.some(
      (f) => (f.error && f.error.horiz_m != null) || (f.error_smooth && f.error_smooth.horiz_m != null)
    );
    if (this.errorRow) {
      this.errorRow.hidden = !this.hasGt;
    }
    this.currentIndex = 0;
    this.render();
  }

  setUseSmooth(useSmooth) {
    this.useSmooth = Boolean(useSmooth);
    this.render();
  }

  setIndex(idx) {
    this.currentIndex = idx;
    this.render();
  }

  _handleSeek(e, canvas) {
    if (!this.frames.length || !this.onSeekIndex) return;
    const rect = canvas.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / Math.max(1, rect.width)));
    const idx = Math.round(frac * (this.frames.length - 1));
    this.onSeekIndex(idx);
  }

  _prep(canvas) {
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return null;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.round(rect.width * dpr);
    const h = Math.round(rect.height * dpr);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    return { ctx, w: rect.width, h: rect.height };
  }

  _computeBounds(finite, minFloor = null, minSpan = 0) {
    let minV = minFloor != null ? minFloor : Math.min(...finite);
    let maxV = Math.max(...finite);
    if (minFloor != null) {
      minV = Math.min(minV, Math.min(...finite));
    }
    const rawSpan = maxV - minV;
    if (minSpan > 0 && rawSpan < minSpan) {
      if (minFloor != null) {
        minV = minFloor;
        maxV = Math.max(maxV, minFloor + minSpan);
      } else {
        const mid = (minV + maxV) / 2;
        minV = mid - minSpan / 2;
        maxV = mid + minSpan / 2;
      }
    } else if (rawSpan < 1e-3) {
      minV -= 1;
      maxV += 1;
    }
    return { minV, maxV };
  }

  _drawSeries(canvas, seriesList, opts = {}) {
    const p = this._prep(canvas);
    if (!p || !this.frames.length) return;
    const { ctx, w, h } = p;
    const padL = 8, padR = 44, padT = 18, padB = 8;
    const plotW = Math.max(10, w - padL - padR);
    const plotH = Math.max(10, h - padT - padB);
    const n = this.frames.length;
    const xAt = (i) => padL + (n > 1 ? (i / (n - 1)) * plotW : 0);

    // Subtle horizontal grid
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (const frac of [0, 0.5, 1]) {
      const y = padT + frac * plotH;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
    }

    let sharedBounds = null;
    if (opts.sharedScale) {
      const allFinite = [];
      for (const s of seriesList) {
        for (const v of s.values) {
          if (v != null && Number.isFinite(v)) allFinite.push(v);
        }
      }
      if (allFinite.length) {
        sharedBounds = this._computeBounds(allFinite, opts.min ?? null, opts.minSpan ?? 0);
      }
    }

    for (const s of seriesList) {
      const vals = s.values;
      const finite = vals.filter((v) => v != null && Number.isFinite(v));
      if (!finite.length) continue;
      const { minV, maxV } = sharedBounds || this._computeBounds(finite, s.min ?? null, s.minSpan ?? 0);
      const yAt = (v) => padT + (1 - (v - minV) / (maxV - minV)) * plotH;

      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.width || 1.6;
      if (s.dashed) ctx.setLineDash(s.dash || [4, 3]);
      else ctx.setLineDash([]);

      ctx.beginPath();
      let started = false;
      for (let i = 0; i < n; i++) {
        const v = vals[i];
        if (v == null || !Number.isFinite(v)) {
          started = false;
          continue;
        }
        const x = xAt(i);
        const y = yAt(v);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
      ctx.setLineDash([]);

      if (s.labelSide !== false) {
        ctx.fillStyle = s.color;
        ctx.font = '10px "IBM Plex Mono", monospace';
        ctx.textAlign = "right";
        ctx.fillText(`${maxV.toFixed(s.decimals ?? 0)}${s.unit || ""}`, w - 4, padT + 7);
        ctx.fillText(`${minV.toFixed(s.decimals ?? 0)}${s.unit || ""}`, w - 4, padT + plotH);
      }
    }

    // Playhead
    const px = xAt(Math.max(0, Math.min(n - 1, this.currentIndex)));
    ctx.strokeStyle = "rgba(255,255,255,0.85)";
    ctx.lineWidth = 1.25;
    ctx.beginPath();
    ctx.moveTo(px, 0);
    ctx.lineTo(px, h);
    ctx.stroke();
  }

  render() {
    if (!this.frames.length) return;

    const hasSmooth = this.frames.some((f) => f.est_smooth != null);
    const rawAlt = this.frames.map((f) => (f.est ? f.est.alt_map_m : null));
    const smAlt = this.frames.map((f) => (f.est_smooth ? f.est_smooth.alt_map_m : null));
    const gtAlt = this.frames.map((f) => (f.gt ? f.gt.alt_map_m : null));
    const hasGtAlt = gtAlt.some((v) => v != null);

    const altSeries = [];
    if (this.useSmooth && hasSmooth) {
      altSeries.push({
        values: rawAlt,
        color: "rgba(128, 242, 190, 0.34)",
        width: 1.1,
        dashed: true,
        dash: [3, 3],
        labelSide: false,
      });
      if (hasGtAlt) {
        altSeries.push({ values: gtAlt, color: "#ffc477", width: 1.4, dashed: true, labelSide: false });
      }
      altSeries.push({ values: smAlt, color: "#80f2be", width: 2.0, unit: "m", decimals: 0 });
    } else {
      if (hasGtAlt) {
        altSeries.push({ values: gtAlt, color: "#ffc477", width: 1.4, dashed: true, labelSide: false });
      }
      altSeries.push({ values: rawAlt, color: "#80f2be", width: 1.8, unit: "m", decimals: 0 });
    }
    this._drawSeries(this.altCanvas, altSeries, { sharedScale: true, minSpan: 12 });

    const inliers = this.frames.map((f) => f.num_inliers ?? 0);
    const reproj = this.frames.map((f) => f.reproj_error_px);
    this._drawSeries(this.inliersCanvas, [
      { values: inliers, color: "#7ee0ff", min: 0, decimals: 0 },
      { values: reproj, color: "#ffc477", min: 0, labelSide: false, width: 1.2 },
    ]);

    if (this.hasGt) {
      const rawErr = this.frames.map((f) => (f.error ? f.error.horiz_m : null));
      const smErr = this.frames.map((f) => (f.error_smooth ? f.error_smooth.horiz_m : null));
      const hasSmErr = smErr.some((v) => v != null);
      if (this.useSmooth && hasSmErr) {
        this._drawSeries(
          this.errorCanvas,
          [
            { values: rawErr, color: "rgba(255, 148, 127, 0.34)", width: 1.1, dashed: true, dash: [3, 3], labelSide: false },
            { values: smErr, color: "#ff947f", width: 1.8, unit: "m", decimals: 1 },
          ],
          { sharedScale: true, min: 0 }
        );
      } else {
        this._drawSeries(this.errorCanvas, [
          { values: rawErr, color: "#ff947f", min: 0, unit: "m", decimals: 1 },
        ]);
      }
    }
  }
}
