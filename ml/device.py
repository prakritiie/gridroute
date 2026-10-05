"""Pick the fastest LightGBM device that actually works here: CUDA, then OpenCL GPU, then CPU.

    python -m ml.device              # prints what it found (the GPU image's default command)

LightGBM only reports a missing GPU learner when you train, so each candidate is probed with a tiny real
training run. GRIDROUTE_DEVICE=cpu|gpu|cuda forces a choice. The PyPI wheel is CPU-only; the CUDA learner
comes from ml/Dockerfile.gpu (docker compose --profile ml run --rm train ...).
"""
from __future__ import annotations

import os
import shutil
import subprocess

import numpy as np


def gpu_name() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def pick_device(verbose: bool = True) -> str:
    import lightgbm as lgb

    forced = os.environ.get("GRIDROUTE_DEVICE")
    candidates = [forced] if forced else ["cuda", "gpu", "cpu"]
    rng = np.random.default_rng(0)
    X, y = rng.random((2000, 8)), rng.random(2000)
    for device in candidates:
        try:
            lgb.train({"objective": "regression", "device_type": device, "verbose": -1, "num_leaves": 7},
                      lgb.Dataset(X, y), num_boost_round=2)
        except Exception as e:  # noqa: BLE001 - LightGBM raises LightGBMError / plain Exception per backend
            if verbose:
                print(f"device {device}: unavailable ({str(e).splitlines()[0][:90]})", flush=True)
            continue
        if verbose:
            name = gpu_name()
            print(f"device {device}: OK" + (f" on {name}" if device != "cpu" and name else ""), flush=True)
        return device
    raise RuntimeError(f"no usable LightGBM device among {candidates}")


if __name__ == "__main__":
    print("using", pick_device())
