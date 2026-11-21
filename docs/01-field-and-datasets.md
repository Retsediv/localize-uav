# Visual localization over mapped terrain

Start with one mapped site and one question per frame. Where was the camera when it saw this view? A georeferenced 3D map gives the answer metric scale and a place on Earth. A Gaussian splat can then show what the scene should look like from that estimated pose.

## How an image becomes a position

Place recognition ranks reference images that look like the query. A match to a roof may put the camera in the right neighborhood, but the roof and the camera occupy different positions. In hierarchical localization, corresponding image features and their known 3D positions constrain the camera pose. Sarlin et al. (CVPR 2019) describe this division of labor. MeshLoc (Panek et al., ECCV 2022) shows how rendered views of a mesh can supply those correspondences. [Hierarchical localization paper](https://arxiv.org/abs/1812.03506) · [MeshLoc](https://arxiv.org/abs/2207.10762)

A second family learns to predict a world coordinate for each image patch, then solves for the camera. CrossLoc (Yan et al., CVPR 2022) trains with real and synthetic aerial views. This is a sensible comparison once the site has training coverage. The geometric baseline needs a reference bank and calibration and can be evaluated before training a site-specific model. [CrossLoc project](https://crossloc.github.io/)

## What the UAV benchmark tells us

Wu et al. (3DV 2024) built a georeferenced textured mesh from a mapping survey, then localized images from a different camera. In Table 3, SIFT with nearest-neighbor matching and three retrieved views placed 91.26% of queries within both 3 m and 3° in the column without gravity guidance. Only 34.79% met 1 m and 1°. The paper used OpenIBL retrieval and reconstructed pose labels. These are image results on this site. A replay still needs measurements of dropouts and latency. [UAVD4L paper](https://arxiv.org/pdf/2401.05971)

## What time and splats add

Visual odometry estimates motion between frames. Monocular reconstruction alone leaves scale and global alignment free. Matching multiple landmarks with known metric coordinates recovers pose at the scale of the reference map. ORB-SLAM3 (Campos et al., IEEE T-RO 2021) is a reference for temporal tracking and relocalization. [ORB-SLAM3](https://arxiv.org/abs/2007.11898)

Kerbl et al. (SIGGRAPH 2023) render an image by projecting oriented Gaussians and blending their color and opacity in depth order. GSLoc uses differences between a rendered image and the observed one to refine an initial pose. Its experiments used indoor scenes. For aerial footage, measure whether that extra step improves pose error. [3D Gaussian Splatting](https://repo-sam.inria.fr/fungraph/3d-gaussian-splatting/) · [GSLoc](https://arxiv.org/abs/2410.06165)

## The geometry behind the map marker

### Projection and camera pose

Use a local metric world frame and undistorted pixels. Let X be a map point, K the camera intrinsics, and R and t the world-to-camera rotation and translation. The function π divides by the third coordinate to recover pixel coordinates. The camera center C is expressed in the map frame. [OpenCV pose solvers](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html) · [COLMAP pose convention](https://colmap.github.io/format.html#images-txt)

$$
u = \pi\!\left(K(RX+t)\right), \qquad C = -R^{\mathsf T}t
$$

Each reference feature needs a map coordinate. With a reference pixel p = (u, v, 1) and its metric optical-axis depth z, back-project to the reference camera, then invert its extrinsics. For ray-length depth, normalize the ray to unit length first. A downsampled depth image also needs the correct pixel-center mapping. [CrossLoc coordinate loader](https://github.com/TOPO-EPFL/CrossLoc-Benchmark-Datasets/blob/main/datasets/setup_dataset_utils.py)

$$
X_{\mathrm{world}} = R^{\mathsf T}\!\left(zK^{-1}p-t\right)
$$

Perspective-n-Point (PnP) estimates R and t from query pixels paired with those 3D points. RANSAC samples hypotheses, projects the points, and counts matches inside a pixel-error tolerance. Refine on the inliers by minimizing summed squared reprojection errors. Points spread across the image constrain pose better than a tight cluster on one roof. [EPnP](https://doi.org/10.1007/s11263-008-0152-6) · [OpenCV pose solvers](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html)

### Recovering coordinates on Earth

A free-scale reconstruction needs a similarity transform with scale s, rotation Q, and translation b. Fit it to well-distributed control points in a metric Earth frame. Check the fit on separate controls. A dataset with a documented metric transform can use that transform directly. [COLMAP georegistration](https://colmap.github.io/faq.html#geo-registration)

$$
X_{\mathrm{Earth}} = s Q X_{\mathrm{map}} + b, \qquad H = h - N
$$

Earth-centered, Earth-fixed (ECEF) coordinates convert to WGS 84 latitude, longitude, and ellipsoidal height h. For an output such as EGM2008 height above mean sea level, subtract the geoid undulation N at that location. For example, h = 150 m and N = +35 m give H = 115 m. A national height datum needs its documented conversion. Local east, north, up (ENU) is convenient for differences in meters. [PROJ local frames](https://proj.org/en/stable/operations/conversions/topocentric.html) · [GeographicLib height conversion](https://geographiclib.sourceforge.io/html/GeoidEval.1.html)

### Interpreting direction

Compute course clockwise from true north using atan2(east displacement, north displacement), then wrap to 0 through 360 degrees, excluding 360. The horizontal displacement must exceed positional noise. Camera bearing uses the projection of the optical axis onto ENU and becomes undefined at nadir. Body heading needs the camera-to-body rotation, including the gimbal angle at exposure. A hovering aircraft can change its heading while course stays undefined.

## Choosing the data

Use CrossLoc for the first absolute fix, Zurich for recorded motion, and UAVD4L to compare the pose solver with an aerial benchmark. Each answers a different question.

### CrossLoc for Earth coordinates

Urbanscape and Naturescape include real drone photographs, calibrated poses and synthetic scene-coordinate maps. Start with Urbanscape. Index independently sampled `train_sim` views. Use `val_drone_real` for development and `test_drone_real` for still-image evaluation. Exclude `test_drone_sim`, whose renders reproduce the test cameras. The authors split images randomly. A flight-level holdout needs a separate recording. Dryad lists four Urbanscape archive parts totaling about 38 GB. [CrossLoc splits](https://github.com/TOPO-EPFL/CrossLoc-Benchmark-Datasets) · [Dryad files](https://doi.org/10.5061/dryad.mgqnk991c)

### UAVD4L for the closest published benchmark

The paper covers about 2.5 km² and five query trajectories. Queries include two-second capture intervals and manually triggered shots. The release describes wide and zoom images, camera parameters, rendered RGB and depth, and a digital surface model (DSM). The README lists a textured OBJ as an input to rendering. Its download description does not establish that it supplies the OBJ, original video, or a trained splat. Begin with the supplied renders if their geometry and data terms check out. [UAVD4L paper](https://arxiv.org/pdf/2401.05971) · [UAVD4L release](https://github.com/RingoWRW/UAVD4L)

### Zurich for a timed flight

The Zurich Urban Micro Aerial Vehicle dataset has 81,169 aerial images over 2 km, calibration and synchronized logs. The roughly 200 MB sample is enough to check decoding and timestamp joins. Use the full 28 GB release to find a reference pass and a later overlapping pass. Photogrammetric camera-position labels were produced at 1 Hz. Zurich also publishes citywide terrain and roof geometry for independent checks on identifiable points. [Zurich data and sample](https://rpg.ifi.uzh.ch/zurichmavdataset.html) · [Zurich dataset paper](https://rpg.ifi.uzh.ch/docs/IJRR17_Majdik.pdf) · [Zurich city model](https://www.stadt-zuerich.ch/de/planen-und-bauen/bauvorschriften-und-planerische-grundlagen/planbezug-datenbezug/3d-stadtmodell.html)

### Other options with a specific use

**Ready-made splats.** CityGaussian V1 publishes Mill 19 Building and Rubble checkpoints. The original drone photographs are available from Mega-NeRF. These support a same-site rendering experiment. Verify the training split and Earth transform before using them for a geographic accuracy claim. The checkpoints carry CC BY-NC-SA 4.0. [CityGaussian checkpoints](https://huggingface.co/TeslaYang123/CityGaussian/tree/main) · [Mill 19 imagery](https://meganerf.cmusatyalab.org/)

**Multi-run 3D maps.** UAVScenes supplies repeated runs and reconstructed meshes, with academic-only terms. Some runs share the same reconstructed map. OrthoLoC supplies local meshes and orthographic data paired to each query, useful for isolating the pose solver after the search area is known. [UAVScenes release](https://github.com/sijieaaa/UAVScenes) · [UAVScenes §3](https://openaccess.thecvf.com/content/ICCV2025/papers/Wang_UAVScenes_A_Multi-Modal_Dataset_for_UAVs_ICCV_2025_paper.pdf) · [OrthoLoC](https://deepscenario.github.io/OrthoLoC/)
