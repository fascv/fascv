from __future__ import annotations

from typing import Dict, Optional

from trading.types import Features, MarketEvent
from trading.utils.math import RollingWindow


class FeatureEngine:
    def __init__(
        self,
        return_window: int = 1,
        atr_window: int = 14,
        volume_z_window: int = 30,
        default_micro: Optional[Dict[str, float]] = None,
    ):
        self.return_window = return_window
        self.atr_window = atr_window
        self.volume_z_window = volume_z_window
        self.default_micro = default_micro or {}
        self._returns = RollingWindow(max(2, return_window + 1))
        self._atr = RollingWindow(atr_window)
        self._volume = RollingWindow(volume_z_window)
        self._prev_close: Optional[float] = None

    def compute(self, event: MarketEvent) -> Features:
        if self._prev_close is None:
            step_ret_bps = 0.0
        else:
            step_ret_bps = (event.close / self._prev_close - 1.0) * 10000.0
        self._returns.push(step_ret_bps)
        self._prev_close = event.close
        recent_returns = list(self._returns.values())[-self.return_window :]
        ret_bps = sum(recent_returns) if recent_returns else 0.0

        atr_proxy_bps = ((event.high - event.low) / max(event.close, 1e-9)) * 10000.0
        self._atr.push(atr_proxy_bps)

        self._volume.push(event.volume)
        vol_mean = self._volume.mean()
        vol_std = self._volume.std()
        if vol_mean is None or vol_std in (None, 0.0):
            volume_z = 0.0
        else:
            volume_z = (event.volume - vol_mean) / vol_std

        micro = dict(self.default_micro)
        micro.update(event.micro or {})

        values: Dict[str, float] = {
            "return_bps": ret_bps,
            "atr_bps": atr_proxy_bps,
            "volume_z": volume_z,
            "price": float(event.close),
            "spread_bps": float(micro.get("spread_bps", 0.0)),
            "depth": float(micro.get("depth", 0.0)),
            "imbalance": float(micro.get("imbalance", 0.0)),
        }
        return Features(ts=event.ts, values=values)
