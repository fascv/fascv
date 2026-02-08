from __future__ import annotations

from dataclasses import dataclass

from trading.alpha.base import AlphaModel
from trading.types import AlphaSignal, Features

try:
    import torch
    import torch.nn as nn
except Exception:  # pragma: no cover
    torch = None
    nn = None


@dataclass
class MLConfig:
    input_dim: int
    hidden_dim: int
    output: str  # "edge_bps" or "p_up"


class SimpleMLPAlpha(AlphaModel):
    def __init__(self, config: MLConfig):
        if torch is None:
            raise RuntimeError("PyTorch is required for ML alpha")
        self.config = config
        self.model = nn.Sequential(
            nn.Linear(config.input_dim, config.hidden_dim),
            nn.ReLU(),
            nn.Linear(config.hidden_dim, 1),
        )
        self.model.eval()

    def predict(self, features: Features) -> AlphaSignal:
        if torch is None:
            raise RuntimeError("PyTorch is required for ML alpha")
        x = torch.tensor([list(features.values.values())], dtype=torch.float32)
        with torch.no_grad():
            out = self.model(x).item()
        if self.config.output == "p_up":
            p_up = float(1 / (1 + torch.exp(torch.tensor(-out))))
            return AlphaSignal(ts=features.ts, edge_bps=0.0, p_up=p_up)
        return AlphaSignal(ts=features.ts, edge_bps=float(out), p_up=None)
