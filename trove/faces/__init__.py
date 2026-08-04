"""Phase 6 — local face detection, embedding and clustering.

Everything runs on the machine: OpenCV's YuNet detector + SFace embedder
(models cached under the config cache dir) produce faces and 128-d vectors;
scikit-learn's DBSCAN groups the vectors into people. No image ever leaves the
machine — only the model weights are fetched once, over the network, the first
time faces run (like the map's street tiles).
"""
