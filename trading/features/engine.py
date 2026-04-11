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
        trend_window: int = 0,
        context_window: int = 0,
        default_micro: Optional[Dict[str, float]] = None,
    ):
        self.return_window = return_window
        self.atr_window = atr_window
        self.volume_z_window = volume_z_window
        self.trend_window = max(0, int(trend_window))
        self.context_window = max(0, int(context_window))
        self.default_micro = default_micro or {}
        self._returns = RollingWindow(max(2, return_window + 1))
        self._trend_returns = RollingWindow(max(2, self.trend_window + 1)) if self.trend_window > 0 else None
        self._atr = RollingWindow(atr_window)
        self._volume = RollingWindow(volume_z_window)
        self._context_prices = RollingWindow(max(2, self.context_window)) if self.context_window > 0 else None
        self._prev_close: Optional[float] = None

    def compute(self, event: MarketEvent) -> Features:
        if self._prev_close is None:
            step_ret_bps = 0.0
        else:
            step_ret_bps = (event.close / self._prev_close - 1.0) * 10000.0
        self._returns.push(step_ret_bps)
        if self._trend_returns is not None:
            self._trend_returns.push(step_ret_bps)
        self._prev_close = event.close
        recent_returns = list(self._returns.values())[-self.return_window :]
        ret_bps = sum(recent_returns) if recent_returns else 0.0
        if self._trend_returns is not None:
            trend_recent = list(self._trend_returns.values())[-self.trend_window :]
            trend_return_bps = sum(trend_recent) if trend_recent else 0.0
        else:
            trend_return_bps = ret_bps

        atr_proxy_bps = ((event.high - event.low) / max(event.close, 1e-9)) * 10000.0
        self._atr.push(atr_proxy_bps)

        self._volume.push(event.volume)
        vol_mean = self._volume.mean()
        vol_std = self._volume.std()
        if vol_mean is None or vol_std in (None, 0.0):
            volume_z = 0.0
        else:
            volume_z = (event.volume - vol_mean) / vol_std

        context_return_bps = 0.0
        context_drawdown_bps = 0.0
        context_rebound_bps = 0.0
        context_range_pos = 0.5
        if self._context_prices is not None:
            self._context_prices.push(float(event.close))
            prices = list(self._context_prices.values())
            if len(prices) >= 2:
                first = float(prices[0])
                last = float(prices[-1])
                high_ref = max(prices)
                low_ref = min(prices)
                if first > 0.0:
                    context_return_bps = (last / first - 1.0) * 10000.0
                if high_ref > 0.0:
                    context_drawdown_bps = (last / high_ref - 1.0) * 10000.0
                if low_ref > 0.0:
                    context_rebound_bps = (last / low_ref - 1.0) * 10000.0
                if high_ref > low_ref:
                    context_range_pos = max(0.0, min(1.0, (last - low_ref) / (high_ref - low_ref)))

        micro = dict(self.default_micro)
        micro.update(event.micro or {})

        values: Dict[str, float] = {
            "return_bps": ret_bps,
            "trend_return_bps": trend_return_bps,
            "atr_bps": atr_proxy_bps,
            "volume_z": volume_z,
            "price": float(event.close),
            "context_return_bps": context_return_bps,
            "context_drawdown_bps": context_drawdown_bps,
            "context_rebound_bps": context_rebound_bps,
            "context_range_pos": context_range_pos,
            "spread_bps": float(micro.get("spread_bps", 0.0)),
            "depth": float(micro.get("depth", 0.0)),
            "imbalance": float(micro.get("imbalance", 0.0)),
        }
        return Features(ts=event.ts, values=values)
