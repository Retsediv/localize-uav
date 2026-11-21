# Building the video localization prototype

The first deliverable is a recorded flight over one known site. Drop in the video, watch the estimated camera move on a map, and scrub to any frame to inspect the evidence behind its position. Process frames in time order so the same estimator can later consume a stream.

## Inputs and outputs

The operator selects a prepared site and provides a video plus its camera profile. The profile describes resolution, focal length, principal point and distortion for that recording mode. Site selection provides the search area. Query GPS, subtitles, filenames encoding positions and flight-controller poses stay outside the estimator. A separate program reads the truth for scoring.

Write one timestamped record per processed frame, including failures. Store the camera center in the site frame, WGS 84 latitude and longitude in degrees, ellipsoidal height and named mean-sea-level height in meters. Store the full camera rotation and its convention, course in degrees true, status, and time since the last verified map match. Unknown fields are null. If mount and gimbal calibration exist, also report body position and heading.

## Prepare the site once

1. Import an independently captured map. For CrossLoc, ingest `train_sim` RGB, intrinsics, camera-to-world poses and scene-coordinate maps. For a mesh, create a bank of reference RGB and metric depth views spanning the camera angles present at the site. For Zurich, reconstruct a designated mapping pass with COLMAP and preserve its feature-to-landmark tracks. Save which inputs built each map. [CrossLoc splits](https://github.com/TOPO-EPFL/CrossLoc-Benchmark-Datasets) · [MeshLoc](https://arxiv.org/abs/2207.10762) · [COLMAP georegistration](https://colmap.github.io/faq.html#geo-registration)

2. Resolve the map coordinate system. CrossLoc Urbanscape subtracts a site origin in ECEF, leaving the axes parallel to ECEF. Add the recorded origin to local camera centers before geodetic conversion. Use double precision. For Zurich, identify stable ground or roof features on the mapping pass and the city model. Fit the transform on one set and check horizontal and vertical residuals on unused controls. Keep camera poses and landmarks in the same local metric frame. [Urbanscape origin](https://github.com/TOPO-EPFL/CrossLoc-Benchmark-Datasets/blob/main/datasets/setup_urbanscape.py) · [CrossLoc coordinate loader](https://github.com/TOPO-EPFL/CrossLoc-Benchmark-Datasets/blob/main/datasets/setup_dataset_utils.py) · [Zurich city model](https://www.stadt-zuerich.ch/de/planen-und-bauen/bauvorschriften-und-planerische-grundlagen/planbezug-datenbezug/3d-stadtmodell.html)

3. Build the searchable reference bank. For each view, cache its global descriptor, local features, and a world point for each feature with valid geometry. CrossLoc supplies downsampled scene coordinates. Use the loader’s sampling offset and mask its missing-value sentinel. Preserve view ID, camera model, image dimensions, map version and vertical reference in the site package. [CrossLoc coordinate loader](https://github.com/TOPO-EPFL/CrossLoc-Benchmark-Datasets/blob/main/datasets/setup_dataset_utils.py) · [hloc toolbox](https://github.com/cvg/Hierarchical-Localization)

## From the video to a trajectory

### Cold start and verification

Decode the selected camera stream in presentation order. Use each frame’s presentation timestamp multiplied by its time base. Resize or undistort into the camera profile once and transform intrinsics consistently. For image sequences, recover capture times from the source logs and preserve them when encoding the replay video. [PyAV timestamps](https://pyav.org/docs/stable/api/time.html) · [Zurich data and sample](https://rpg.ifi.uzh.ch/zurichmavdataset.html)

Rank reference views by descriptor similarity. On a small site, an exact dot product of normalized descriptors is sufficient. Start with the best five views as a development setting. Match local features, attach reference world points, and run PnP with RANSAC on each plausible view or group of overlapping views. Refine the best supported solution. [hloc toolbox](https://github.com/cvg/Hierarchical-Localization) · [OpenCV pose solvers](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html)

Accept a fix only when positive-depth inliers span enough of the image and the pixel residuals pass thresholds selected on validation data. Compare competing hypotheses from different parts of the site. If two distant poses explain the same repeated pattern, leave the frame unresolved. Log inlier count, spatial coverage, median residual and runner-up support.

### Updating position between keyframes

After a fix, carry the map point IDs forward with sparse pyramidal Lucas–Kanade optical flow. Drop tracks that fail a forward/backward consistency check. Re-solve PnP with the surviving 3D anchors each frame. This keeps intermediate positions in meters. Full image retrieval refreshes features periodically or when support deteriorates. [OpenCV sparse optical flow](https://docs.opencv.org/4.x/dc/d6b/group__video__track.html) · [OpenCV pose solvers](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html)

Emit “matched” after verified retrieval, “tracked” while tracked map points still constrain the pose, and “lost” when either route fails. During loss, publish null current coordinates and draw a gap. Retry a full-site search on subsequent keyframes. The UI may show the last known position separately with its age.

### Map playback

Use the video clock to drive the map marker and camera frustum. Convert signed east/north displacement into course only when motion is distinguishable from position noise. Feed ellipsoidal height to Cesium and display the named mean-sea-level height alongside it. Hide the nose arrow unless body heading is known. Split playback samples at losses so interpolation cannot bridge a gap. [Cesium sampled positions](https://cesium.com/learn/cesiumjs/ref-doc/SampledPositionProperty.html) · [Cesium Cartesian3](https://cesium.com/learn/cesiumjs/ref-doc/Cartesian3.html) · [GeographicLib height conversion](https://geographiclib.sourceforge.io/html/GeoidEval.1.html)

## Build order and completion tests

Suggested time box for one engineer with a workstation and local storage. Data access, control points and download time can move the schedule. Finish a measurable stage before introducing a more expensive model.

### Day 1. One image in the right place

Use one CrossLoc Urbanscape synthetic reference pose to check a pixel-to-world-to-pixel round trip at the correct depth resolution. Confirm that a known Earth coordinate survives import and export, including height. Next, take one real validation image, hide its pose, search the bank and save the recovered position with inlier overlays. The two tests isolate coordinate errors from matching errors.

### Day 2. An image benchmark

Freeze the reference bank and tune acceptance on validation images. Compare the position of the top retrieved reference camera with the geometrically estimated camera center. Score both against held-out labels, count missing estimates, and save the hardest false match. Run a second test with query GPS fields stripped. Both estimator outputs should agree.

### Days 3 and 4. A timed video

Decode the Zurich sample first. In the full recording, choose two nonadjacent passes with visible common landmarks, leave a time buffer, and record the frame ranges before matching. One pass builds the map. A held-out interval supplies the encoded video and original capture times. Score only frame IDs with truth. If the sample has no revisit, use it for timing only. If a suitable full-recording overlap or vertical control cannot be established, use a second, independently recorded survey of the same mapped site for the missing test. [Zurich data and sample](https://rpg.ifi.uzh.ch/zurichmavdataset.html) · [Zurich dataset paper](https://rpg.ifi.uzh.ch/docs/IJRR17_Majdik.pdf)

Replay a clear segment and one with a naturally weak view or a deliberately masked interval. The latter must be labeled as injected. Save the full trace, the time to first fix, and how long it took to recover. Inspect the latitude, longitude, named height and direction at two paused timestamps.

### Day 5. Reviewable demo

Deliver one replay, the reference/query split manifest, the pose trace and one scorecard. Show the estimated trajectory first, then reveal the withheld reference path. Record the hardware, input resolution, processed frames per second, latency and any null fields. Add Gaussian rendering only after this replay can be reproduced from the same inputs.
