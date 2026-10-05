"""Serving the ETA model in-process (spec §8.1 "Serving", arrows 16-17).

    model = EtaRegistry(r).current()      # follows model:eta:current, hot-reloads within RELOAD_S
    secs = model.predict(...)             # the same core/features.py the trainer used

The registry is a folder per version (models/eta/vN: model.txt, vocab.json, baselines.json) plus one Redis
pointer, set by `python -m ml.publish`. B3 (OSRM distance / historical hex-hour speed) is the fallback when
OSRM doesn't answer in time: the matcher never blocks on the model.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import redis

from core import config, features

MODELS = Path(config.__file__).resolve().parent.parent / "models" / "eta"
POINTER = "model:eta:current"
RELOAD_S = 30.0


class EtaModel:
    def __init__(self, version: str, booster, vocab: features.Vocab, baselines):
        self.version = version
        self.booster = booster
        self.vocab = vocab
        self.baselines = baselines

    @classmethod
    def load(cls, version: str, root: Path = MODELS) -> "EtaModel":
        import lightgbm as lgb

        from ml.baselines import Baselines
        d = root / version
        return cls(version, lgb.Booster(model_file=str(d / "model.txt")), features.Vocab.load(d / "vocab.json"),
                   Baselines.load(d / "baselines.json"))

    def predict(self, plat, plng, dlat, dlng, ts, osrm_s, osrm_m) -> np.ndarray:
        """Predicted seconds for each leg (vectorised over the batch)."""
        X = features.build(plat, plng, dlat, dlng, ts, osrm_s, osrm_m, self.vocab)
        return np.expm1(self.booster.predict(X))

    def fallback(self, plat, plng, how, straight_m) -> np.ndarray:
        """B3 without OSRM: straight line x 1.32 (the measured road/straight median) / hex-hour speed."""
        return self.baselines.b3_predict(np.asarray(plat, float), np.asarray(plng, float),
                                         np.asarray(how), 1.32 * np.asarray(straight_m, float))


class EtaRegistry:
    """Follows the Redis pointer; reloads the model when it changes (checked at most every RELOAD_S)."""

    def __init__(self, r: redis.Redis, root: Path = MODELS):
        self.r = r
        self.root = root
        self.model: EtaModel | None = None
        self.checked = 0.0

    def current(self) -> EtaModel | None:
        now = time.monotonic()
        if self.model is not None and now - self.checked < RELOAD_S:
            return self.model
        self.checked = now
        version = self.r.get(POINTER)
        if not version:
            return self.model
        if self.model is None or self.model.version != version:
            try:
                self.model = EtaModel.load(version, self.root)
                print(f"eta: loaded model {version}", flush=True)
            except (OSError, ValueError) as e:
                print(f"eta: could not load {version} ({e}); keeping {getattr(self.model, 'version', None)}", flush=True)
        return self.model
