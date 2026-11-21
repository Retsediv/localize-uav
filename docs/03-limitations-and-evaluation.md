# Where localization fails and how to measure it

A smooth path can be wrong by a street or by a constant vertical offset. Judge the prototype on withheld positions, the time it spends lost, and the frequency with which it reports a plausible but wrong fix.

## Map coverage and appearance

Repeated roofs, crop rows and identical road junctions can yield a coherent pose at the wrong location. Water, snow, freshly paved roads or a lens aimed at the sky may offer too few stable points. A winter flight over a summer map tests appearance change as well as geometry. Include one visually similar wrong reference area, one poorly textured interval and one out-of-map clip. A rejected fix is the expected outcome for the last case.

A terrain grid gives one elevation per horizontal location. A bare-earth surface omits roofs and trees, and an overhead photograph contains little facade texture. For a near-nadir rural flight, independent imagery draped over terrain can work. For low oblique views, use a mesh or reference photographs with triangulated landmarks. A public Swiss example is SWISSIMAGE over swissALTI3D, whose source frame is LV95/LN02. Attribute the data when reusing it. [SWISSIMAGE](https://www.swisstopo.admin.ch/en/orthoimage-swissimage-10) · [swissALTI3D](https://www.swisstopo.admin.ch/en/height-model-swissalti3d) · [swisstopo usage terms](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices)

## Vertical accuracy and direction

A horizontal coordinate system such as UTM does not identify the vertical datum. A nearly constant height error suggests a missing geoid correction or an origin offset. Validate heights at independent controls in the chosen datum. A launch-relative altimeter reading needs an independently known launch elevation. Compute above-ground height only against bare ground in the same vertical datum. [GeographicLib height conversion](https://geographiclib.sourceforge.io/html/GeoidEval.1.html) · [swissALTI3D](https://www.swisstopo.admin.ch/en/height-model-swissalti3d)

Camera pose refers to the optical center. To report the airframe center, apply the measured camera lever arm and rotation. A tilting gimbal changes that transform. For an unknown mount, label the position as the camera center. Show course only while horizontal motion exceeds the uncertainty in the displacement. Body yaw, course, and camera bearing can all differ on the same frame.

## Calibration and timing

Digital stabilization, zoom, cropping and distortion change pixel geometry. A single fixed calibration can fit the middle of an image while leaving a ring of residuals near the edges. Plot residuals by image location and split the test at a zoom change. A rolling shutter can add row-dependent error during turns. Zurich documents a 30 ms readout. [Zurich data and sample](https://rpg.ifi.uzh.ch/zurichmavdataset.html)

At 12 m/s, a 0.2 s clock offset produces 2.4 m of apparent along-track error. Join truth by capture time or frame ID. Measure any offset on calibration data. Keep variable frame intervals and do not tune time alignment on the scored flight.

## Scoring a recorded flight

### Freeze the experiment

Publish the site boundary, reference image IDs, query intervals, camera profile, software and model versions. Keep test pixels and poses out of map fitting, tuning and splat training. Exclude exact-pose synthetic twins. A same-flight revisit is a useful replay test. A separately surveyed map and a different flight test generalization. Label the two protocols explicitly. [CrossLoc splits](https://github.com/TOPO-EPFL/CrossLoc-Benchmark-Datasets) · [UAVScenes §3](https://openaccess.thecvf.com/content/ICCV2025/papers/Wang_UAVScenes_A_Multi-Modal_Dataset_for_UAVs_ICCV_2025_paper.pdf)

Score each time-matched camera center in the fixed geographic frame. Never fit translation, rotation or scale to the test path for the absolute score. Zurich camera-position labels come from a GPS-initialized reconstruction. UAVD4L uses jointly refined pose labels. Report agreement with those labels separately from error at independently surveyed controls. A low reprojection error alone cannot establish absolute accuracy. [Zurich dataset paper](https://rpg.ifi.uzh.ch/docs/IJRR17_Majdik.pdf) · [UAVD4L paper](https://arxiv.org/pdf/2401.05971)

### Measurements to report

**Position and rotation.** For accepted, labeled frames, report median and 95th-percentile horizontal distance and absolute vertical error in meters, plus signed vertical bias. For full orientation labels, report the angle of the relative rotation. For course, use the shortest wrapped angular difference only on moving intervals. State the height datum, eligible count and number of fixes in each metric.

**Availability and wrong fixes.** Report accepted frames divided by all eligible frames. For irregular sampling, also report fraction of elapsed flight time with a current fix. Count a frame as a joint success only if its 3D position and rotation both meet the threshold. Include losses in that denominator. For UAVD4L, report recall at 1 m/1°, 3 m/3°, and 5 m/5°. Record accepted poses on out-of-map clips and large errors among accepted in-map poses. [UAVD4L paper](https://arxiv.org/pdf/2401.05971)

**Continuity and cost.** Measure time from clip start to first verified fix, longest lost interval, and delay from the return of usable imagery to the next fix. Report per-frame end-to-end processing time at the median and 95th percentile, processed frames per second, dropped frames, peak memory and the hardware used. Report warm tracking and full-map search separately.

### An example acceptance target

For a first workstation demo, propose at least 80% of eligible in-map frames within 5 m horizontally, with lost frames counting as failures. Where the vertical datum is verified, target median absolute height error at or below 3 m. Require zero accepted positions in one declared out-of-map clip and recovery within 2 s in one labeled dropout test. These are proposed targets. Record the actual counts and denominators, even if the prototype misses them.

If a global transform or height datum cannot be verified, show the local trajectory and leave the corresponding Earth fields empty. Treat that run as a partial demo. A recorded replay measures localization performance on that recording. Onboard flight guidance also requires a tested latency budget and independent navigation safeguards.
