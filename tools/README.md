# tools/

Scripts that are not part of the application. Nothing here is imported at
runtime or shipped in a packaged build. Run them from the repository root.

## `dev/` — things you run while working on the project

Diagnostics and tuning aids. They read the catalogue and write nothing but
their own output files, so they are safe to run against a live archive.

| Script | What it is for |
|---|---|
| `cdp_shot.py` | Screenshot a GUI route through headless Chrome (DevTools Protocol). |
| `shoot_all.py` | Shoot every screen in both themes, and diff two such runs. |
| `analyze_filenames.py` | Report which filename date patterns hit and which are missed. |
| `semantic_calibrate.py` | Print the evidence for retuning the semantic-search thresholds. |
| `faces_cluster_montage.py` | Contact sheet of face clusters, for eyeballing cluster quality. |
| `faces_umap_plot.py` | 2-D UMAP of face embeddings, coloured by assigned person. |

## `build/` — one-off producers whose output is committed or bundled

Run rarely, usually once. Each needs a heavier dev-only toolchain (torch,
transformers) that the application itself never imports.

| Script | Produces |
|---|---|
| `adaface_export.py` | The AdaFace ir101 face-embedding ONNX. |
| `adaface_net.py` | Vendored upstream model definition; used only by the export above. |
| `dinov2_pet_export.py` | The DINOv2 pet re-identification ONNX. |
| `render_logo.py` | `desktop/build/icon.png` + `icon.ico`, from the one canonical mark. |

`render_logo.py` is the exception to "run rarely": `desktop/scripts/build-backend.cjs`
invokes it on every desktop build, because `desktop/build/` is gitignored and so
the icons are never present in a fresh clone.

> Not to be confused with `packaging/tools/`, which is the manifest of **native
> binaries** (ffmpeg, ffprobe, ExifTool) that a packaged build bundles.
