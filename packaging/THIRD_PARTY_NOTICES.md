# Third-party notices

The final release must replace the bracketed fields below with the exact licence
texts and source URLs for the versions recorded in `packaging/tools/manifest.json`.
Do not publish a release while any entry is incomplete.

- Electron — MIT — https://github.com/electron/electron
- Python — PSF License — https://www.python.org/psf/license/
- PyInstaller — GPL-2.0-or-later with a bootloader exception — https://pyinstaller.org/
- OpenCV Zoo YOLOX-S model — Apache-2.0 —
  https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox
- FFmpeg / FFprobe — n7.1.5-12-g1fdbca85aa, BtbN GPL 7.1 *shared* build —
  GPL-3.0-or-later — https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-08-08-13-06
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
  The engineering code is bundled. The PP-OCRv6 detection and recognition models
  and the PP-OCR angle classifier are **not**: they ship inside RapidAI's wheel,
  but the installer filters them out and the app downloads them at first use.
  The weights are Baidu's, released under Apache-2.0 as part of PaddleOCR —
  https://github.com/PaddlePaddle/PaddleOCR
  Those three files are re-hosted by this project as release assets, byte-identical
  to RapidAI's published copies (SHA-256s in packaging/models/manifest.json), which
  is redistribution and is permitted: Apache-2.0 §4 allows it in any medium, with
  or without modification, and these are unmodified. No non-commercial clause on
  either part. Note the wheel ships **no licence file of its own** — only
  `License-Expression: Apache-2.0` metadata — so the Apache-2.0 text must be
  supplied by this release rather than copied out of the package. Apache-2.0 §6
  grants no trademark rights: name RapidAI, Baidu and PaddleOCR as the source, do
  not imply their endorsement.
- Bergamot Translator runtime and the Spanish-to-English Firefox Translations
  model — Mozilla Public License 2.0 —
  https://github.com/browsermt/bergamot-translator and
  https://github.com/mozilla/translations
  The loader and worker scripts are bundled (trove/web/vendor/). The WASM runtime
  and the three model files are downloaded with Search by description and served
  to the page from the cache. Like the PP-OCR weights above they are re-hosted
  unmodified as release assets, which MPL-2.0 permits; the licence text and the
  notice in trove/web/vendor/BERGAMOT-NOTICE.txt travel with them. Used only to
  translate a typed Spanish query into English before it is embedded — search
  text and inference stay on the user's machine.
- pypdfium2 / PDFium — Apache-2.0 or BSD-3-Clause (pypdfium2's own code) over a
  prebuilt PDFium, BSD-3-Clause — https://github.com/pypdfium2-team/pypdfium2
  Bundled in the installer, not downloaded: the wheel carries the PDFium shared
  library with it, which is why the desktop build needs no compiler for it. Used
  to read a PDF's text layer, and later to rasterise pages for reading the writing
  in them.
  Note this is a *binary* redistribution of PDFium — the BSD-3-Clause notice
  belongs in any release that ships it, not only the Python-side licence.
- Bundled Python packages — [generated package/version/licence inventory from
  packaging/requirements-desktop.txt]
