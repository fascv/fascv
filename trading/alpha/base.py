from __future__ import annotations

from trading.types import AlphaSignal, Features


class AlphaModel:
    def predict(self, features: Features) -> AlphaSignal:
        raise NotImplementedError
