/* global Cesium */

/**
 * CesiumJS 3D World controller for Groundframe.
 * Supports Google Photorealistic 3D Tiles (via Cesium ion or Google Maps key),
 * OpenStreetMap fallback, color-coded 3D trajectory, Ground Truth overlay,
 * live 3D camera frustum, and 4 viewing modes: orbit | chase | drone_eye | wipe.
 */

export class CesiumWorld {
  constructor(containerEl, badgeEl) {
    this.containerEl = containerEl;
    this.badgeEl = badgeEl;
    this.viewer = null;
    this.tileset = null;
    this.mode = "orbit"; // orbit | chase | drone_eye | wipe
    this.showGt = true;
    this.useSmooth = true;
    this.altDeltaM = 0.0;
    this.fovDeltaDeg = 0.0;

    this.trajectory = null;
    this.currentFrame = null;
    this.aspectRatio = 16 / 9;

    this._trajEntities = [];
    this._gtEntities = [];
    this._droneEntities = [];
    this._frustumPositions = [];
    this._dronePos = null;
    this._defaultFovRad = Cesium.Math.toRadians(60);
  }

  async init(config) {
    if (this.viewer) {
      this.viewer.destroy();
      this.viewer = null;
    }

    const ionToken = (config.cesium_ion_token || "").trim();
    const gmapsKey = (config.google_maps_api_key || "").trim();

    if (ionToken) {
      Cesium.Ion.defaultAccessToken = ionToken;
    }
    if (gmapsKey) {
      Cesium.GoogleMaps.defaultApiKey = gmapsKey;
    }

    const useGoogleTiles = Boolean(ionToken || gmapsKey);

    const viewerOpts = {
      animation: false,
      baseLayerPicker: false,
      fullscreenButton: false,
      vrButton: false,
      geocoder: false,
      homeButton: false,
      infoBox: false,
      sceneModePicker: false,
      selectionIndicator: false,
      timeline: false,
      navigationHelpButton: false,
      requestRenderMode: false,
      msaaSamples: 4,
    };

    if (useGoogleTiles) {
      // Hide default ellipsoid imagery once photorealistic 3D tiles load cleanly
      viewerOpts.globe = false;
    } else {
      viewerOpts.baseLayer = new Cesium.ImageryLayer(
        new Cesium.OpenStreetMapImageryProvider({ url: "https://tile.openstreetmap.org/" })
      );
    }

    this.viewer = new Cesium.Viewer(this.containerEl, viewerOpts);
    this.viewer.scene.backgroundColor = Cesium.Color.fromCssColorString("#070b0d");
    this.viewer.scene.skyAtmosphere.show = true;
    if (this.viewer.scene.globe) {
      this.viewer.scene.globe.depthTestAgainstTerrain = true;
    }
    this._defaultFovRad = this.viewer.camera.frustum.fov;

    if (useGoogleTiles) {
      this._setBadge("Loading Google 3D Tiles...");
      try {
        this.tileset = await Cesium.createGooglePhotorealistic3DTileset({
          onlyUsingWithGoogleGeocoder: true,
        });
        this.viewer.scene.primitives.add(this.tileset);
        this._setBadge(gmapsKey ? "Google 3D Tiles (API Key)" : "Google Photorealistic 3D Tiles");
      } catch (err) {
        console.error("Failed to load Google Photorealistic 3D Tiles, falling back to OSM:", err);
        this.viewer.scene.globe = new Cesium.Globe(Cesium.Ellipsoid.WGS84);
        this.viewer.imageryLayers.addImageryProvider(
          new Cesium.OpenStreetMapImageryProvider({ url: "https://tile.openstreetmap.org/" })
        );
        this._setBadge("OSM Fallback (check Cesium token)");
      }
    } else {
      this._setBadge("OSM 3D Globe (add CESIUM_ION_TOKEN for Google 3D)");
    }
  }

  _setBadge(text) {
    if (this.badgeEl) this.badgeEl.textContent = text;
  }

  setMode(mode) {
    this.mode = mode;
    if (!this.viewer) return;
    const isFirstPerson = mode === "drone_eye" || mode === "wipe";
    // Lock manual mouse navigation while in Drone-Eye / Wipe so pose stays exact
    const ssc = this.viewer.scene.screenSpaceCameraController;
    ssc.enableRotate = !isFirstPerson;
    ssc.enableTranslate = !isFirstPerson;
    ssc.enableZoom = !isFirstPerson;
    ssc.enableTilt = !isFirstPerson;
    ssc.enableLook = !isFirstPerson;

    this._setDroneMarkerVisibility(!isFirstPerson);
    this._setTrajectoryVisibility(!isFirstPerson);

    this.viewer.camera.cancelFlight();
    this.viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);

    if (!isFirstPerson) {
      this.viewer.camera.frustum.fov = this._defaultFovRad;
      if (mode === "orbit" && this.trajectory) {
        this.flyToTrajectory();
      }
    }
    if (this.currentFrame) {
      this.updateFrame(this.currentFrame, true);
    }
  }

  _getEst(frame) {
    if (!frame) return null;
    if (this.useSmooth && frame.est_smooth) return frame.est_smooth;
    return frame.est || null;
  }

  setShowGt(show) {
    this.showGt = show;
    const isFp = this.mode === "drone_eye" || this.mode === "wipe";
    for (const e of this._gtEntities) {
      e.show = show && !isFp;
    }
  }

  setUseSmooth(useSmooth) {
    this.useSmooth = Boolean(useSmooth);
    if (!this.viewer || !this.trajectory) return;
    this._rebuildEstimatedEntities();
    if (this.currentFrame) {
      this.updateFrame(this.currentFrame, false);
    }
  }

  setAlignment(altDeltaM, fovDeltaDeg) {
    this.altDeltaM = Number(altDeltaM) || 0;
    this.fovDeltaDeg = Number(fovDeltaDeg) || 0;
    if (this.currentFrame) {
      this.updateFrame(this.currentFrame, true);
    }
  }

  resize() {
    if (this.viewer) {
      this.viewer.resize();
      if (this.currentFrame && (this.mode === "drone_eye" || this.mode === "wipe" || this.mode === "chase")) {
        this.updateFrame(this.currentFrame, true);
      }
    }
  }

  _rebuildEstimatedEntities() {
    if (!this.viewer || !this.trajectory) return;
    for (const e of this._trajEntities) {
      this.viewer.entities.remove(e);
    }
    this._trajEntities = [];

    const frames = this.trajectory?.frames || [];
    const okSegments = [];
    const predSegments = [];
    let curOk = [];
    let curPred = [];
    const kfPositions = [];

    for (const f of frames) {
      const est = this._getEst(f);
      if (est) {
        const pos = Cesium.Cartesian3.fromDegrees(est.lon, est.lat, est.alt_wgs84_m ?? est.alt_map_m ?? 0);
        if (f.status === "PREDICTED") {
          if (curOk.length > 1) okSegments.push(curOk);
          curOk = [];
          curPred.push(pos);
        } else {
          if (curPred.length > 1) predSegments.push(curPred);
          curPred = [];
          curOk.push(pos);
        }
        if (f.is_keyframe) {
          kfPositions.push({ pos, frame_id: f.frame_id });
        }
      } else {
        if (curOk.length > 1) okSegments.push(curOk);
        if (curPred.length > 1) predSegments.push(curPred);
        curOk = [];
        curPred = [];
      }
    }
    if (curOk.length > 1) okSegments.push(curOk);
    if (curPred.length > 1) predSegments.push(curPred);

    const isFp = this.mode === "drone_eye" || this.mode === "wipe";

    // Draw estimated segments
    for (const seg of okSegments) {
      this._trajEntities.push(
        this.viewer.entities.add({
          show: !isFp,
          polyline: {
            positions: seg,
            width: 3.5,
            material: new Cesium.PolylineGlowMaterialProperty({
              glowPower: 0.18,
              color: Cesium.Color.fromCssColorString("#80f2be"),
            }),
          },
        })
      );
    }
    for (const seg of predSegments) {
      this._trajEntities.push(
        this.viewer.entities.add({
          show: !isFp,
          polyline: {
            positions: seg,
            width: 3.0,
            material: new Cesium.PolylineDashMaterialProperty({
              color: Cesium.Color.fromCssColorString("#ffc477"),
              dashLength: 12,
            }),
          },
        })
      );
    }

    // Draw keyframe anchors
    for (const kf of kfPositions) {
      this._trajEntities.push(
        this.viewer.entities.add({
          show: !isFp,
          position: kf.pos,
          point: {
            pixelSize: 8,
            color: Cesium.Color.fromCssColorString("#80f2be"),
            outlineColor: Cesium.Color.fromCssColorString("#0d1215"),
            outlineWidth: 2,
          },
        })
      );
    }
  }

  loadTrajectory(trajectory, useSmooth = true) {
    this.trajectory = trajectory;
    this.useSmooth = Boolean(useSmooth);
    this.currentFrame = null;
    if (!this.viewer) return;

    this.viewer.entities.removeAll();
    this._trajEntities = [];
    this._gtEntities = [];
    this._droneEntities = [];

    const intr = trajectory?.intrinsics;
    if (intr && intr.width && intr.height) {
      this.aspectRatio = intr.width / intr.height;
    } else {
      this.aspectRatio = 16 / 9;
    }

    this._rebuildEstimatedEntities();

    const frames = trajectory?.frames || [];
    const gtPositions = [];
    for (const f of frames) {
      if (f.gt) {
        gtPositions.push(
          Cesium.Cartesian3.fromDegrees(f.gt.lon, f.gt.lat, f.gt.alt_wgs84_m ?? f.gt.alt_map_m ?? 0)
        );
      }
    }

    // Draw Ground Truth polyline
    if (gtPositions.length > 1) {
      const gtEnt = this.viewer.entities.add({
        show: this.showGt,
        polyline: {
          positions: gtPositions,
          width: 2.5,
          material: new Cesium.PolylineDashMaterialProperty({
            color: Cesium.Color.fromCssColorString("#ffc477").withAlpha(0.9),
            dashLength: 10,
          }),
        },
      });
      this._gtEntities.push(gtEnt);
    }

    // Live drone marker + frustum wireframe (8 edges)
    this._dronePos = null;
    this._frustumEdges = Array.from({ length: 8 }, () => []);

    const dronePoint = this.viewer.entities.add({
      position: new Cesium.CallbackProperty(() => this._dronePos, false),
      point: {
        pixelSize: 11,
        color: Cesium.Color.WHITE,
        outlineColor: Cesium.Color.fromCssColorString("#80f2be"),
        outlineWidth: 3,
      },
    });
    this._droneEntities.push(dronePoint);

    for (let i = 0; i < 8; i++) {
      const idx = i;
      const edgeEnt = this.viewer.entities.add({
        polyline: {
          positions: new Cesium.CallbackProperty(() => this._frustumEdges[idx], false),
          width: idx < 4 ? 1.8 : 2.2,
          material: Cesium.Color.fromCssColorString("#80f2be").withAlpha(idx < 4 ? 0.85 : 0.95),
        },
      });
      this._droneEntities.push(edgeEnt);
    }

    const isFp = this.mode === "drone_eye" || this.mode === "wipe";
    this._setDroneMarkerVisibility(!isFp);
    this._setTrajectoryVisibility(!isFp);
    if (!isFp) {
      this.flyToTrajectory();
    }
  }

  _setDroneMarkerVisibility(visible) {
    for (const e of this._droneEntities) {
      e.show = visible;
    }
  }

  _setTrajectoryVisibility(visible) {
    for (const e of this._trajEntities) {
      e.show = visible;
    }
    for (const e of this._gtEntities) {
      e.show = visible && this.showGt;
    }
  }

  flyToTrajectory() {
    if (!this.viewer || !this.trajectory) return;
    const pts = [];
    for (const f of this.trajectory.frames || []) {
      const est = this._getEst(f);
      if (est) {
        pts.push(Cesium.Cartesian3.fromDegrees(est.lon, est.lat, est.alt_wgs84_m ?? est.alt_map_m ?? 0));
      }
    }
    if (!pts.length) {
      // Fallback: fly to Switzerland overview
      this.viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(8.5425, 47.37, 2500),
        duration: 0.8,
      });
      return;
    }
    const sphere = Cesium.BoundingSphere.fromPoints(pts);
    const range = Math.max(sphere.radius * 3.2, 420);
    this.viewer.camera.flyToBoundingSphere(sphere, {
      offset: new Cesium.HeadingPitchRange(Cesium.Math.toRadians(25), Cesium.Math.toRadians(-38), range),
      duration: 0.7,
    });
  }

  _enuToEcefBasis(lon, lat, alt) {
    const origin = Cesium.Cartesian3.fromDegrees(lon, lat, alt);
    const enuMatrix = Cesium.Transforms.eastNorthUpToFixedFrame(origin);
    const rot3 = Cesium.Matrix4.getMatrix3(enuMatrix, new Cesium.Matrix3());
    return { origin, rot3 };
  }

  _enuVecToEcef(rot3, vec) {
    const v = new Cesium.Cartesian3(vec[0], vec[1], vec[2]);
    const out = Cesium.Matrix3.multiplyByVector(rot3, v, new Cesium.Cartesian3());
    return Cesium.Cartesian3.normalize(out, out);
  }

  _updateFrustumGeometry(origin, rot3, fwdEnu, upEnu, fovVRad) {
    const f = this._enuVecToEcef(rot3, fwdEnu);
    const u = this._enuVecToEcef(rot3, upEnu);
    const r = Cesium.Cartesian3.normalize(Cesium.Cartesian3.cross(f, u, new Cesium.Cartesian3()), new Cesium.Cartesian3());

    const depth = 42.0; // meters visual pyramid depth
    const halfH = Math.tan(fovVRad / 2) * depth;
    const halfW = halfH * this.aspectRatio;

    const center = Cesium.Cartesian3.add(origin, Cesium.Cartesian3.multiplyByScalar(f, depth, new Cesium.Cartesian3()), new Cesium.Cartesian3());
    const dx = Cesium.Cartesian3.multiplyByScalar(r, halfW, new Cesium.Cartesian3());
    const dy = Cesium.Cartesian3.multiplyByScalar(u, halfH, new Cesium.Cartesian3());

    const tl = Cesium.Cartesian3.add(Cesium.Cartesian3.subtract(center, dx, new Cesium.Cartesian3()), dy, new Cesium.Cartesian3());
    const tr = Cesium.Cartesian3.add(Cesium.Cartesian3.add(center, dx, new Cesium.Cartesian3()), dy, new Cesium.Cartesian3());
    const br = Cesium.Cartesian3.subtract(Cesium.Cartesian3.add(center, dx, new Cesium.Cartesian3()), dy, new Cesium.Cartesian3());
    const bl = Cesium.Cartesian3.subtract(Cesium.Cartesian3.subtract(center, dx, new Cesium.Cartesian3()), dy, new Cesium.Cartesian3());

    this._frustumEdges[0] = [origin, tl];
    this._frustumEdges[1] = [origin, tr];
    this._frustumEdges[2] = [origin, br];
    this._frustumEdges[3] = [origin, bl];
    this._frustumEdges[4] = [tl, tr];
    this._frustumEdges[5] = [tr, br];
    this._frustumEdges[6] = [br, bl];
    this._frustumEdges[7] = [bl, tl];
  }

  updateFrame(frame, forceCamera = false) {
    this.currentFrame = frame;
    const est = this._getEst(frame);
    if (!this.viewer || !frame || !est) {
      this._dronePos = null;
      return;
    }

    const baseAlt = (est.alt_wgs84_m ?? est.alt_map_m ?? 0) + this.altDeltaM;
    const { origin, rot3 } = this._enuToEcefBasis(est.lon, est.lat, baseAlt);
    this._dronePos = origin;

    const baseFovV = Number(this.trajectory?.intrinsics?.fov_vertical) || 45.0;
    const fovVDeg = Math.max(12, Math.min(115, baseFovV + this.fovDeltaDeg));
    const fovVRad = Cesium.Math.toRadians(fovVDeg);

    const fwdEnu = est.forward_enu || [0, 1, -0.5];
    const upEnu = est.up_enu || [0, 0.5, 0.866];

    this._updateFrustumGeometry(origin, rot3, fwdEnu, upEnu, fovVRad);

    if (this.mode === "drone_eye" || this.mode === "wipe") {
      this.viewer.camera.cancelFlight();
      this.viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);

      const dirEcef = this._enuVecToEcef(rot3, fwdEnu);
      const upEcef = this._enuVecToEcef(rot3, upEnu);

      // Cesium PerspectiveFrustum.fov is the wider-axis FOV (horizontal when width > height)
      const canvasW = this.viewer.canvas.clientWidth || 16;
      const canvasH = this.viewer.canvas.clientHeight || 9;
      const canvasAspect = canvasW / Math.max(1, canvasH);
      const fovHRad = Math.min(Math.PI - 0.08, 2 * Math.atan(Math.tan(fovVRad / 2) * canvasAspect));
      this.viewer.camera.frustum.fov = canvasAspect >= 1 ? fovHRad : fovVRad;

      this.viewer.camera.setView({
        destination: origin,
        orientation: {
          direction: dirEcef,
          up: upEcef,
        },
      });
    } else if (this.mode === "chase") {
      this.viewer.camera.cancelFlight();
      this.viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
      const dirEcef = this._enuVecToEcef(rot3, fwdEnu);
      const worldUp = this._enuVecToEcef(rot3, [0, 0, 1]);
      // Position chase camera 90m behind and 42m above the drone
      const back = Cesium.Cartesian3.multiplyByScalar(dirEcef, -90, new Cesium.Cartesian3());
      const lift = Cesium.Cartesian3.multiplyByScalar(worldUp, 42, new Cesium.Cartesian3());
      const camPos = Cesium.Cartesian3.add(Cesium.Cartesian3.add(origin, back, new Cesium.Cartesian3()), lift, new Cesium.Cartesian3());
      const lookTarget = Cesium.Cartesian3.add(origin, Cesium.Cartesian3.multiplyByScalar(dirEcef, 60, new Cesium.Cartesian3()), new Cesium.Cartesian3());
      const lookDir = Cesium.Cartesian3.normalize(Cesium.Cartesian3.subtract(lookTarget, camPos, new Cesium.Cartesian3()), new Cesium.Cartesian3());

      this.viewer.camera.frustum.fov = this._defaultFovRad;
      this.viewer.camera.setView({
        destination: camPos,
        orientation: {
          direction: lookDir,
          up: worldUp,
        },
      });
    } else if (forceCamera && this.mode === "orbit") {
      this.viewer.camera.frustum.fov = this._defaultFovRad;
    }
  }
}
