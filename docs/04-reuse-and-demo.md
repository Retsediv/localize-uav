# Reusable tools and a Gaussian splat demo

Pause the flight and render the mapped scene from the estimated pose. A reviewer can compare rooflines in the video and the render, then reveal the measured error at that timestamp.

## The shortest path to a working estimator

**Data and map.** Read CrossLoc’s prepared 3D coordinates at matched reference pixels. For Zurich, use COLMAP to reconstruct the reference pass and register it to known controls. Save the feature tracks and Earth transform. [CrossLoc splits](https://github.com/TOPO-EPFL/CrossLoc-Benchmark-Datasets) · [CrossLoc coordinate loader](https://github.com/TOPO-EPFL/CrossLoc-Benchmark-Datasets/blob/main/datasets/setup_dataset_utils.py) · [COLMAP georegistration](https://colmap.github.io/faq.html#geo-registration)

**Retrieval and matching.** Use hloc for NetVLAD retrieval and local matching. Compare SIFT nearest-neighbor matches with DISK plus LightGlue on the same candidates. A tiny reference bank can be searched exhaustively. Write the adapter that maps each reference keypoint to XYZ. [hloc toolbox](https://github.com/cvg/Hierarchical-Localization) · [NetVLAD](https://www.di.ens.fr/willow/research/netvlad/) · [LightGlue models and terms](https://github.com/cvg/LightGlue)

**Pose and video.** Reuse OpenCV’s `solvePnPRansac`, `solvePnPRefineLM` and `calcOpticalFlowPyrLK`. Store the map landmark ID on each accepted local feature. PyAV supplies the frame timestamps. Write the rejection rules and the timestamp join for evaluation. [OpenCV pose solvers](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html) · [OpenCV sparse optical flow](https://docs.opencv.org/4.x/dc/d6b/group__video__track.html) · [PyAV timestamps](https://pyav.org/docs/stable/api/time.html)

**Display and export.** Use CesiumJS for the globe and timestamped positions. Draw the camera frustum from its rotation and field of view. Store the same samples the viewer reads in a local CSV or JSONL file. [Cesium sampled positions](https://cesium.com/learn/cesiumjs/ref-doc/SampledPositionProperty.html) · [Cesium Cartesian3](https://cesium.com/learn/cesiumjs/ref-doc/Cartesian3.html)

## Which assets can go in the handoff

Dryad distributes research datasets under CC0. For separately obtained Swiss geodata follow swisstopo’s attribution terms. The Zurich dataset page explicitly permits commercial use. The UAVD4L README gives no clear dataset redistribution grant. Link to its release if rights are unresolved. UAVScenes specifies academic use only. CityGaussian V1 checkpoints are CC BY-NC-SA 4.0. For a reusable startup demo, train your own splat from appropriately licensed reference imagery. [Dryad data policy](https://datadryad.org/publication_policy) · [swisstopo usage terms](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices) · [Zurich data and sample](https://rpg.ifi.uzh.ch/zurichmavdataset.html) · [UAVD4L release](https://github.com/RingoWRW/UAVD4L) · [UAVScenes release](https://github.com/sijieaaa/UAVScenes) · [CityGaussian checkpoints](https://huggingface.co/TeslaYang123/CityGaussian/tree/main)

LightGlue releases its matcher code and weights under Apache 2.0. Its README separately identifies restrictive SuperPoint terms and permits the DISK pairing under Apache 2.0. Check the exact retrieval weights as well. Keep an asset list with source, version, license and attribution. [LightGlue models and terms](https://github.com/cvg/LightGlue)

## What to show with the splat

### Use a scene that the camera actually saw

For a quick rendering experiment, CityGaussian V1 publishes a 4.48 GB Rubble checkpoint and a 6.11 GB Building checkpoint. Both have corresponding Mill 19 drone imagery. Use the matching V1 viewer or exporter. Reproduce a published camera view, then test a held-out image from that same scene once the training split is known. The checkpoint listing does not establish a WGS 84 transform or height datum. Keep this experiment in local coordinates until those are recovered. [CityGaussian checkpoints](https://huggingface.co/TeslaYang123/CityGaussian/tree/main) · [CityGaussian releases](https://github.com/Linketic/CityGaussian/releases) · [Mill 19 imagery](https://meganerf.cmusatyalab.org/)

For the main flight, train Splatfacto on the permitted mapping images of the same site. Initialize from the mapping reconstruction. Preserve the training software’s centering, scale and camera-axis transforms so the localizer and the renderer agree. Render a known reference view and project a few landmarks before rendering a held-out pose. Splatfacto can export a Gaussian PLY. gsplat provides a CUDA renderer under Apache 2.0. [Splatfacto](https://docs.nerf.studio/nerfology/methods/splat.html) · [Nerfstudio camera axes](https://docs.nerf.studio/quickstart/data_conventions.html) · [gsplat license](https://github.com/nerfstudio-project/gsplat/blob/main/LICENSE)

### A replay worth pausing

Show the original video beside the render at the estimated pose, with a map and a shared scrubber. Give the reviewer a wipe between imagery and rendering. On pause, let them inspect inlier matches and the age of the last map fix. Toggle the withheld trajectory and error only after estimation finishes. On a turn, place the camera frustum and course arrow together. When a weak view causes loss, leave a visible break and show where the system reacquired the map.

### An experiment that decides whether splats help

Run the same held-out clip three ways. A uses mesh or sparse-map references. B replaces the reference RGB with splat renders at the same cameras and keeps the same 3D landmarks for depth. C starts from B’s verified pose and tests local photometric refinement against the splat, initialized identically for each comparison. Freeze retrieval budget, thresholds and query frames. Measure joint recall, wrong fixes and extra latency for each variant. [MeshLoc](https://arxiv.org/abs/2207.10762) · [GSLoc](https://arxiv.org/abs/2410.06165)

Splats blend transparent samples along each ray. Expected depth at a roof edge can land between roof and ground. Validate splat depths against independent geometry before using them for PnP. If the renderer improves visual explanation while leaving pose accuracy flat, keep it in the viewer and report that result. For a CPU-only presentation laptop, render the estimated camera path once on a GPU and label that panel as an offline render. [gsplat rendering API](https://docs.gsplat.studio/main/apis/rasterization.html)
