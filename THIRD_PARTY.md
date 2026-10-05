# Third-Party Open Source Intelligence Provenance & Integration

This document records the provenance, licensing, architectural rationale, interfaces, and upstream update strategies for all third-party open-source implementations integrated into the Multi-Model Event Intelligence System, in compliance with Design Platform requirements (§2, §3, §4, §10, §13, §17).

---

## 1. Integrated Open-Source Systems

### 1.1 NetworkX (Graph Algorithms & Topology)
* **Repository URL:** https://github.com/networkx/networkx
* **Version:** 3.7
* **License:** 3-Clause BSD
* **Component Provided:**
  * In-memory graph representation and routing topologies.
  * Dijkstra shortest-path calculations weighted by physical distance (haversine metres).
  * Connected components analysis (`connected_components`, `node_connected_component`).
  * Network severance and corridor detour derivation.
* **Why Selected:**
  * Eliminates custom hand-rolled Dijkstra implementations that lacked robustness and real metric edge costs.
  * Provides true topological connectivity metrics based on largest connected component ratios, replacing naive node-counting heuristics.
  * Actively maintained, mathematically rigorous graph engine.
* **What We Modified / Wrapped:**
  * Integrated behind `InfrastructureGraph` in `src/infraimpact/graph/model.py`.
  * Dynamic creation of undirected weighted routing graphs (`_routing_graph`) with real haversine distance edge weights between node geometry centroids.
  * Added `corridor_detour(blocked_node_ids)` which derives critical endpoints from unblocked neighbors of severed infrastructure and finds the optimal detour around closures.
  * Added `connectivity_ratio(blocked_node_ids)` and `reachable_count(origin_node_id, blocked_node_ids)`.
* **How It Connects to Our Architecture:**
  * Powers `RoadOverlapCapability` (`src/infraimpact/analysis/geospatial.py`).
  * Directly produces `GeographicNetworkAnalysis` metrics (`network_connectivity_ratio`, `blocked_edges`, `detour_distance_m`, `detour_percentage`, `alternate_routes_available`).
  * Drives user exposure routing and cascading infrastructure disruption evaluations.
* **Upstream Update Strategy:** Standard PyPI dependency management via `pyproject.toml` (`networkx>=3.4`).

---

### 1.2 Shapely & GEOS (Computational Geometry)
* **Repository URL:** https://github.com/shapely/shapely
* **Version:** 2.1.2 (GEOS C-library backing)
* **License:** 3-Clause BSD
* **Component Provided:**
  * Exact 2D planar and projected computational geometry.
  * Point-in-polygon containment (`contains`), polygon-linestring intersections (`intersects`).
  * Buffer derivation (`buffer_geometry`) for dynamic event footprint expansion and spatial proximity envelopes.
  * Metric geodesic and equirectangular polygon area and segment distance measurements.
* **Why Selected:**
  * Eliminates fragile custom ray-casting and line-segment distance approximations that produced boundary edge errors and inaccurate spatial overlaps.
  * Industry standard for spatial analysis, directly mirroring PostGIS `ST_Contains`, `ST_Intersects`, and `ST_Buffer` semantics.
* **What We Modified / Wrapped:**
  * Fully adapted in `src/infraimpact/domain/geo.py`.
  * Encapsulated behind native GeoJSON dict interfaces (`_to_shapely`, `_from_shapely`, `contains`, `intersects`, `distance_m`, `buffer_geometry`, `geometry_area_m2`).
  * Employs local equirectangular metric projection centered at local latitudes for millimetre-accurate metric distance calculations without heavy coordinate transforms.
* **How It Connects to Our Architecture:**
  * Drives spatial observation clustering and event footprint derivation (`EventState.geometry`).
  * Resolves infrastructure overlap in `RoadOverlapCapability`, `TransitDisruptionCapability`, and `CriticalFacilityCapability`.
  * Evaluates user route and saved-place exposure in `src/infraimpact/users/exposure.py`.
* **Upstream Update Strategy:** PyPI dependency management via `pyproject.toml` (`shapely>=2.0`).

---

### 1.3 Ruptures (Statistical Change-Point Detection)
* **Repository URL:** https://github.com/deepcharles/ruptures
* **Version:** 1.1.10
* **License:** 2-Clause BSD
* **Component Provided:**
  * Offline and streaming change-point detection algorithms.
  * Pruned Exact Linear Time (PELT) search method with L2 cost function.
* **Why Selected:**
  * Purpose-built for identifying discrete structural regime shifts in numerical time series.
  * Far superior to naive threshold comparisons: accounts for baseline variances and temporal clustering.
* **What We Modified / Wrapped:**
  * Implemented `StatisticalChangeCapability` in `src/infraimpact/analysis/statistics.py`.
  * Wrapped `rpt.Pelt(model="l2", min_size=2, jump=1)` with a variance-adaptive Bayesian Information Criterion (BIC) penalty (`pen = 2.0 * np.log(N) * sigma^2`).
  * Added graceful handling for sparse time series (<8 observations).
* **How It Connects to Our Architecture:**
  * Analyzes traffic speed ratio series from probe and sensor observations.
  * Injects `traffic_change_points` and `traffic_regime_shift` into the event feature set.
  * Informs the State Delta Engine (`src/infraimpact/delta/engine.py`) and orchestrator when a situation has materially changed versus ordinary sensor noise.
* **Upstream Update Strategy:** PyPI dependency management via `pyproject.toml` (`ruptures>=1.1`).

---

### 1.4 NumPy & SciPy (Scientific & Mathematical Computation)
* **Repository URL:** https://github.com/numpy/numpy & https://github.com/scipy/scipy
* **Version:** NumPy 2.5.3, SciPy 1.15.x
* **License:** 3-Clause BSD
* **Component Provided:**
  * Numerical arrays, variance calculations, vector operations.
  * Least-squares linear trend fitting (`linear_trend` via `np.polyfit`).
* **Why Selected:**
  * Standard numerical backbone for Python machine learning and statistical modeling.
* **How It Connects to Our Architecture:**
  * Derives rate-of-change, velocity vectors, and acceleration trends in `TemporalAnalysis`.
  * Computes observation arrival rate acceleration in `StatisticalChangeCapability`.
* **Upstream Update Strategy:** PyPI dependency management via `pyproject.toml` (`numpy>=1.26`).

---

---

### 1.5 Lifelines (Survival & Time-to-Event Analysis)
* **Repository URL:** https://github.com/CamDavidsonPilon/lifelines
* **Version:** 0.30.3
* **License:** MIT
* **Component Provided:**
  * Survival analysis routines: Kaplan-Meier estimator, Cox Proportional Hazards, Nelson-Aalen estimators.
* **Why Selected:**
  * Solves the right-censored time-to-impact and event duration prediction problem (Model C) using established survival statistics rather than heuristic duration guesses.
* **How It Connects to Our Architecture:**
  * Model C interface in `src/infraimpact/models/` and `docs/models/MODEL_C_TIME_TO_IMPACT.md`.
  * Provides statistical baseline hazard rates for incident duration and time until road clearance.
* **Upstream Update Strategy:** PyPI dependency management via `pyproject.toml` (`lifelines>=0.28`).

---

### 1.6 Shapely STRtree (GEOS C-Backed Spatial Indexing)
* **Repository URL:** https://github.com/shapely/shapely
* **Version:** 2.1.2
* **License:** 3-Clause BSD
* **Component Provided:**
  * GEOS C-backed Sort-Tile-Recursive (STR) R-Tree spatial index (`shapely.STRtree`).
  * $O(\log N)$ spatial candidate queries: bounding box intersection (`predicate="intersects"`), distance containment (`predicate="dwithin"`), and nearest-neighbor search (`query_nearest`).
* **Why Selected:**
  * Replaces $O(N)$ linear scans across thousands of graph nodes, routes, and infrastructure impacts.
  * Direct C-GEOS performance without needing a separate C-extension like `libspatialindex`/`rtree`.
* **What We Modified / Wrapped:**
  * Created `SpatialIndex[T]` generic class in `src/infraimpact/domain/geo.py`.
  * Integrated into `InfrastructureGraph` in `src/infraimpact/graph/model.py` for `nodes_near` and `nodes_intersecting`.
  * Integrated into `ExposureEngine` in `src/infraimpact/users/exposure.py` for route impact and saved-place candidate pruning.
* **How It Connects to Our Architecture:**
  * Accelerates all spatial joins between incident footprints and the infrastructure network.
* **Upstream Update Strategy:** Part of core `shapely>=2.0`.

---

### 1.7 Scikit-Learn (NearestNeighbors, DBSCAN, Evaluation Metrics & Calibration)
* **Repository URL:** https://github.com/scikit-learn/scikit-learn
* **Version:** 1.9.1
* **License:** 3-Clause BSD
* **Component Provided:**
  * `sklearn.neighbors.NearestNeighbors`: High-dimensional vector space nearest-neighbor retrieval with Manhattan (L1) and Euclidean distance metrics.
  * `sklearn.cluster.DBSCAN`: Density-based spatial clustering of applications with noise, using the great-circle `haversine` metric on spherical coordinates.
  * `sklearn.metrics` & `sklearn.calibration`: Standardized machine learning evaluation metrics (`brier_score_loss`, `precision_score`, `recall_score`, `f1_score`, `mean_absolute_error`, `root_mean_squared_error`, `calibration_curve`).
* **Why Selected:**
  * Industry standard, rigorously tested, C/Cython-optimized machine learning primitives.
  * Avoids custom reimplementation of neighbor retrieval, clustering, calibration, and classification metrics.
* **What We Modified / Wrapped:**
  * **Historical Similarity Retrieval:** In `src/infraimpact/analysis/similarity.py`, `HistoricalSimilarityEngine` builds weighted multidimensional feature vectors (crowd size, duration, arterial overlap, transit overlap, mobility, rush hour) and queries top-$k$ historical analogues using `NearestNeighbors(metric="l1")` combined with geodesic spatial proximity.
  * **Observation Burst Clustering:** In `src/infraimpact/events/resolver.py`, `cluster_unresolved_observations` clusters incoming bursts of multi-source observations using `DBSCAN(metric="haversine")` within sliding temporal windows.
  * **Evaluation Framework:** In `src/infraimpact/evaluation/metrics.py`, replaces hand-rolled metrics with scikit-learn standard implementations.
* **Upstream Update Strategy:** PyPI dependency management via `pyproject.toml` (`scikit-learn>=1.5`).

---

### 1.8 Bayesian Evidence Fusion & Source Reliability Tracker
* **Component Provided:**
  * Conjugate Beta-Binomial updating model (`BayesianSourceReliabilityTracker`) for dynamic source reliability tracking.
  * Multi-source log-likelihood ratio updating (`fuse_evidence_probabilities`) for hypothesis testing and evidence fusion.
* **Why Selected:**
  * Implements mathematically principled, non-heuristic evidence fusion (Design Platform §36, §37).
  * Solves the source corroboration and contradiction problem using exact Bayesian odds multiplication rather than ad-hoc heuristics or LLM opinions.
* **What We Modified / Wrapped:**
  * Implemented in `src/infraimpact/analysis/uncertainty.py`.
  * Integrated directly into `SourceConflictCapability` to produce `probabilistic_fusion_score`, `source_reliability_score`, and `information_confidence`.
* **How It Connects to Our Architecture:**
  * Feeds the `ConfidenceBreakdown` in `src/infraimpact/analysis/metrics.py` and informs Jev bounded decision thresholds.

---

## 2. Evaluated Candidate Technologies & Architectural Seams

Per the design guidelines (§2, §3, §13), candidate systems were researched and evaluated. Below are the architectural decisions and designated seams:

| Subsystem Domain | Evaluated Candidates | Selected Implementation / Architectural Seam | Rationale |
| :--- | :--- | :--- | :--- |
| **Road Network Routing** | OSRM, Valhalla, GraphHopper, OSMnx, NetworkX | **NetworkX 3.7** (In-Memory Engine) + `RoutingProvider` ABC Seam | OSRM and Valhalla require external daemon microservices, multi-gigabyte PBF extracts, and pre-built contraction hierarchies unsuitable for self-contained, in-repo testability. `src/infraimpact/graph/routing.py` defines `RoutingProvider` as an explicit adapter seam to plug in OSRM/Valhalla for production deployments. |
| **Geospatial Processing & Indexing** | PostGIS, GeoPandas, GDAL, Shapely / GEOS | **Shapely 2.1 (GEOS + STRtree)** | Shapely 2.1 provides C-speed GEOS geometry algorithms and STRtree spatial indexing directly in-process with zero database dependency. Matches PostGIS ST_* function signatures cleanly. |
| **Time-Series Change Detection** | PyOD, River, ADTK, Ruptures | **Ruptures 1.1 (PELT)** | Ruptures is the most reliable, mathematically verified change-point framework with exact PELT optimization. River/PyOD are suited for outlier detection, whereas Ruptures detects state transitions and regime shifts. |
| **Historical Analogue Retrieval** | Faiss, Annoy, ChromaDB, Scikit-Learn | **Scikit-Learn (NearestNeighbors L1)** | Eliminates external vector database infrastructure. Scikit-learn's `NearestNeighbors` provides deterministic, exact feature matching and spatial indexing without external service overhead. |
| **Spatiotemporal Observation Clustering** | HDBSCAN, Scikit-learn DBSCAN | **Scikit-Learn DBSCAN (Haversine)** | DBSCAN with haversine metric reliably groups spatial bursts of unverified and multi-source observations into coherent candidate event clusters prior to resolution. |
| **Probabilistic Evidence Fusion** | Dempster-Shafer, Bayesian Odds, LLM | **Bayesian Conjugate Beta + Log-Odds Fusion** | Strictly adheres to §36 & §37: probabilistic evidence fusion with explicit source reliability tracking rather than subjective LLM consensus. |
| **GTFS Transit Ingestion** | Partridge, GTFS-Kit, pygtfs | **Domain GTFS Normalizer** + `TransitDisruptionCapability` | Ingests GTFS-RT feed protobufs and static alerts directly into standardized `Observation` records (`TRANSIT_SERVICE_ALERT`, `VEHICLE_POSITION`), avoiding complex relational schema overhead in memory. |
| **Bounded Decision Layer** | LLM vs. Rule Engine vs. Jev | **Jev Decision Layer** | Strictly adheres to §5 & §6: deterministic algorithms and ML produce structured analytical metrics; Jev provides bounded, explainable decision-making without hallucination risk. |


