#!/usr/bin/env python3
"""Rebuild the runtime DINOv2 pet-reID ONNX from the fine-tuned checkpoint.

The app groups animals with onnxruntime and never needs torch. This one-off tool
(re)creates that ONNX: it downloads the AvitoTech DINOv2-small model fine-tuned
for individual cat/dog re-identification from the Hugging Face Hub and exports the
CLS-token embedding head to a single-file fp32 ONNX into

    <cache_dir>/models/dinov2_pet/dinov2_pet.onnx

The model is a standard ``Dinov2Model`` (hidden 384, image 224, patch 14); the
re-ID embedding is the 384-d CLS token of ``last_hidden_state``. Preprocessing at
runtime: RGB crop resized to 224x224, /255, ImageNet mean/std normalization, NCHW.

Requires torch + transformers + onnx (dev-only; not runtime deps).

    python3 tools/build/dinov2_pet_export.py            # download + export onnx
    python3 tools/build/dinov2_pet_export.py --verify   # also check ONNX == torch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from organize_archive.config import Config
from organize_archive.pets import backend as pb

HF_REPO = "AvitoTech/DINO-v2-small-for-animal-identification"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--verify", action="store_true", help="check the exported ONNX matches the torch reference"
    )
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    import torch
    from huggingface_hub import hf_hub_download
    from safetensors.torch import load_file
    from transformers import Dinov2Config, Dinov2Model

    cfg = Config.load()
    out = pb.dinov2_model_path(cfg.cache_dir)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"downloading {HF_REPO} …")
    config = Dinov2Config.from_pretrained(HF_REPO)
    # The checkpoint's position embeddings are stored at DINOv2's native 518px
    # grid (37x37+1 = 1370); build the model at that size so they load, then feed
    # 224px crops at runtime (Dinov2 interpolates the pos-encoding to the grid).
    config.image_size = 518
    model = Dinov2Model(config)
    weights = load_file(hf_hub_download(HF_REPO, "model.safetensors"))
    # The checkpoint stores the ViT under a `backbone.` prefix (the re-ID "head"
    # was just the CLS token), so a plain from_pretrained matches NONE of its keys
    # and silently random-initializes. Strip the prefix and load explicitly.
    state = {k[len("backbone.") :]: v for k, v in weights.items() if k.startswith("backbone.")}
    missing, unexpected = model.load_state_dict(state, strict=False)
    # Only an unused pooler may be missing; if anything real is missing the
    # embeddings would be garbage, so fail loudly instead of exporting noise.
    real_missing = [k for k in missing if not k.startswith("pooler.")]
    assert not unexpected and not real_missing, (
        f"state_dict mismatch: missing={real_missing[:4]} unexpected={unexpected[:4]}"
    )
    model.eval()
    print(
        f"loaded {len(state)} tensors "
        f"(missing pooler-only: {len(missing)}, unexpected: {len(unexpected)})"
    )

    class Embed(torch.nn.Module):
        """Output the 384-d CLS-token embedding (the re-ID feature)."""

        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, x):
            return self.m(pixel_values=x).last_hidden_state[:, 0]

    print(f"exporting ONNX -> {out}")
    torch.onnx.export(
        Embed(model).eval(),
        torch.randn(1, 3, 224, 224),
        str(out),
        input_names=["input"],
        output_names=["embedding"],
        dynamic_axes={"input": {0: "b"}, "embedding": {0: "b"}},
        opset_version=args.opset,
        do_constant_folding=True,
        dynamo=False,
    )
    print(f"wrote {out}  ({out.stat().st_size / 1e6:.0f} MB)")

    if args.verify:
        import numpy as np
        import onnxruntime as ort

        x = np.random.randn(4, 3, 224, 224).astype("float32")
        with torch.no_grad():
            t = Embed(model)(torch.from_numpy(x)).numpy()
        s = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
        o = s.run(None, {"input": x})[0]
        cos = (t * o).sum(1) / (np.linalg.norm(t, axis=1) * np.linalg.norm(o, axis=1))
        print(f"verify: torch vs onnx cosine min={cos.min():.6f} mean={cos.mean():.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
