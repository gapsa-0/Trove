# Third-party notices

The final release must replace the bracketed fields below with the exact licence
texts and source URLs for the versions recorded in `packaging/tools/manifest.json`.
Do not publish a release while any entry is incomplete.

- Electron — MIT — https://github.com/electron/electron
- Python — PSF License — https://www.python.org/psf/license/
- PyInstaller — GPL-2.0-or-later with a bootloader exception — https://pyinstaller.org/
- OpenCV Zoo YOLOX-S model — Apache-2.0 —
  https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox
- FFmpeg / FFprobe — n7.1.5-9-gb9a218bc1e, BtbN GPL 7.1 *shared* build —
  GPL-3.0-or-later — https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-07-22-13-36
  Shipped as the two executables plus the `libav*`/`av*` shared libraries they
  link against, rather than as two static binaries. Same upstream payload and the
  same single GPL obligation; the libraries are simply no longer duplicated inside
  each executable.
- ExifTool (Windows builds only) — 13.59, upstream self-contained Windows package —
  Perl Artistic License / GPL-1.0-or-later — https://exiftool.org/
- InsightFace `buffalo_l` models (SCRFD det_10g, ArcFace w600k_r50) — downloaded at
  first run, non-commercial research use — https://github.com/deepinsight/insightface
- DINOv2 pet re-identification model — downloaded at first run; exported from
  AvitoTech/DINO-v2-small-for-animal-identification, DINOv2 upstream Apache-2.0 —
  https://huggingface.co/AvitoTech/DINO-v2-small-for-animal-identification
- AdaFace IR-101 face embedding model — downloaded at first run; exported from
  marcelo-victor/adaface_ir101_webface12m, itself a copy of the AdaFace authors'
  WebFace12M checkpoint. The AdaFace *code* is MIT — (c) 2022 Minchul Kim et al.,
  https://github.com/mk-minchul/AdaFace — but these are *weights*, trained on
  WebFace12M (a subset of WebFace260M), which is released for non-commercial
  academic research. Treat the weights as non-commercial research use, as with
  buffalo_l above. Unlike buffalo_l this file is re-hosted by this project as a
  release asset — the installer no longer carries it, but publishing that asset is
  still redistribution; confirm the terms permit it before any commercial use.
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
- RapidOCR — Apache-2.0 — https://github.com/RapidAI/RapidOCR
  Bundled, together with the PP-OCRv6 detection and recognition models and the
  PP-OCR angle classifier that ship inside its wheel. The engineering code is
  RapidAI's; the model weights are Baidu's, released under Apache-2.0 as part of
  PaddleOCR — https://github.com/PaddlePaddle/PaddleOCR
  These are the only model weights the installer carries rather than fetching at
  first use, so the obligation travels with the package rather than with a
  download. No non-commercial clause on either part.
- multilingual-e5-small text embedding model — MIT — downloaded at first use, not
  bundled. Weights and the ONNX export are both upstream's, at a pinned revision:
  https://huggingface.co/intfloat/multilingual-e5-small
  Used to turn document passages and typed searches into vectors for searching
  documents by meaning. Unlike buffalo_l and AdaFace this carries no
  non-commercial clause, and unlike the Gemma tokenizer above it is a standard
  OSI licence.
- pypdfium2 / PDFium — Apache-2.0 or BSD-3-Clause (pypdfium2's own code) over a
  prebuilt PDFium, BSD-3-Clause — https://github.com/pypdfium2-team/pypdfium2
  Bundled in the installer, not downloaded: the wheel carries the PDFium shared
  library with it, which is why the desktop build needs no compiler for it. Used
  to read a PDF's text layer, and later to rasterise pages for Text in images.
  Note this is a *binary* redistribution of PDFium — the BSD-3-Clause notice
  belongs in any release that ships it, not only the Python-side licence.
- Bundled Python packages — [generated package/version/licence inventory from
  packaging/requirements-desktop.txt]
