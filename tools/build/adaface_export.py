#!/usr/bin/env python3
"""Rebuild the runtime AdaFace ONNX from the WebFace12M checkpoint.

The app embeds faces with onnxruntime and never needs torch. This one-off tool
(re)creates that ONNX: it downloads the AdaFace ir101 / WebFace12M checkpoint
from the Hugging Face Hub, loads it into the vendored IR-101 architecture
(adaface_net.py), and exports a single-file fp32 ONNX into

    <cache_dir>/models/adaface/adaface_ir101_w12m.onnx

Requires torch + onnx + huggingface_hub (dev-only; not runtime deps). The export
was validated numerically exact against the torch reference (cosine 1.000000)
and runs ~232 ms/face on a 4-thread CPU. Dynamic int8 quantization was tried and
rejected: on a non-VNNI CPU it was both slower and less accurate.

    python3 tools/build/adaface_export.py            # download ckpt + export onnx
    python3 tools/build/adaface_export.py --verify   # also check ONNX == torch
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from organize_archive.config import Config
from organize_archive.faces import backend as fb

HF_REPO = "marcelo-victor/adaface_ir101_webface12m"
HF_FILE = "adaface_weights.ckpt"


def _load_net():
    spec = importlib.util.spec_from_file_location(
        "adaface_net", Path(__file__).resolve().parent / "adaface_net.py")
    net = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(net)
    return net


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="check the exported ONNX matches the torch reference")
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    import torch
    from huggingface_hub import hf_hub_download

    cfg = Config.load()
    out = fb.adaface_model_path(cfg.cache_dir)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"downloading {HF_REPO}/{HF_FILE} …")
    ckpt = hf_hub_download(HF_REPO, HF_FILE, local_dir=str(out.parent))

    net = _load_net()
    model = net.build_model("ir_101").eval()
    sd = torch.load(ckpt, map_location="cpu")
    sd = sd.get("state_dict", sd)
    bb = {k[6:]: v for k, v in sd.items() if k.startswith("model.")}
    miss, unexp = model.load_state_dict(bb, strict=False)
    assert not miss and not unexp, f"state_dict mismatch: {len(miss)}/{len(unexp)}"

    class Embed(torch.nn.Module):
        def __init__(self, m): super().__init__(); self.m = m
        def forward(self, x): return self.m(x)   # (embedding_512_l2, norm)

    print(f"exporting ONNX -> {out}")
    torch.onnx.export(
        Embed(model).eval(), torch.randn(1, 3, 112, 112), str(out),
        input_names=["input"], output_names=["embedding", "norm"],
        dynamic_axes={"input": {0: "b"}, "embedding": {0: "b"}, "norm": {0: "b"}},
        opset_version=args.opset, do_constant_folding=True, dynamo=False)
    print(f"wrote {out}  ({out.stat().st_size/1e6:.0f} MB)")

    if args.verify:
        import numpy as np, onnxruntime as ort
        x = np.random.randn(4, 3, 112, 112).astype("float32")
        with torch.no_grad():
            t = np.stack([model(torch.from_numpy(x[i:i+1]))[0].numpy()[0]
                          for i in range(len(x))])
        s = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
        o = s.run(None, {"input": x})[0]
        cos = (t*o).sum(1) / (np.linalg.norm(t, axis=1)*np.linalg.norm(o, axis=1))
        print(f"verify: torch vs onnx cosine min={cos.min():.6f} mean={cos.mean():.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
