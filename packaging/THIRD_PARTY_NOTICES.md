# Third-party notices

The final release must replace the bracketed fields below with the exact licence
texts and source URLs for the versions recorded in `packaging/tools/manifest.json`.
Do not publish a release while any entry is incomplete.

- Electron — MIT — https://github.com/electron/electron
- Python — PSF License — https://www.python.org/psf/license/
- PyInstaller — GPL-2.0-or-later with a bootloader exception — https://pyinstaller.org/
- OpenCV Zoo YOLOX-S model — Apache-2.0 —
  https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox
- FFmpeg / FFprobe — n7.1.5-9-gb9a218bc1e, BtbN GPL 7.1 build — GPL-3.0-or-later —
  https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-07-22-13-36
- ExifTool (Windows builds only) — 13.59, upstream self-contained Windows package —
  Perl Artistic License / GPL-1.0-or-later — https://exiftool.org/
- InsightFace `buffalo_l` models (SCRFD det_10g, ArcFace w600k_r50) — downloaded at
  first run, non-commercial research use — https://github.com/deepinsight/insightface
- DINOv2 pet re-identification model — bundled; exported from
  AvitoTech/DINO-v2-small-for-animal-identification, DINOv2 upstream Apache-2.0 —
  https://huggingface.co/AvitoTech/DINO-v2-small-for-animal-identification
- AdaFace IR-101 face embedding model — bundled; exported from
  marcelo-victor/adaface_ir101_webface12m, itself a copy of the AdaFace authors'
  WebFace12M checkpoint. The AdaFace *code* is MIT — (c) 2022 Minchul Kim et al.,
  https://github.com/mk-minchul/AdaFace — but these are *weights*, trained on
  WebFace12M (a subset of WebFace260M), which is released for non-commercial
  academic research. Treat the weights as non-commercial research use, as with
  buffalo_l above. Unlike buffalo_l this file is bundled and re-hosted by this
  project as a release asset, which is redistribution rather than a first-run
  download; confirm the terms still permit that before any commercial use.
- SigLIP 2 base/16 @256 search model (vision and text towers) — Apache-2.0 —
  downloaded at first indexing, not bundled. Weights are Google's
  https://huggingface.co/google/siglip2-base-patch16-256; the ONNX exports this
  app actually fetches are https://huggingface.co/onnx-community/siglip2-base-patch16-256-ONNX
  at a pinned revision. Unlike buffalo_l and AdaFace this carries no
  non-commercial clause.
- Gemma SentencePiece tokenizer (`tokenizer.json`, shipped inside the SigLIP 2
  repository above) — Gemma Terms of Use —
  https://ai.google.dev/gemma/terms — used only to turn a typed search query
  into token ids for the SigLIP 2 text tower.
- Bundled Python packages — [generated package/version/licence inventory from
  packaging/requirements-desktop.txt]
