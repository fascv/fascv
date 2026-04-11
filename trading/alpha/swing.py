from __future__ import annotations

from dataclasses import dataclass

from trading.alpha.base import AlphaModel
from trading.types import AlphaSignal, Features


@dataclass
class SwingConfig:
    lookback: int = 20
    buy_band: float = 0.42
    sell_band: float = 0.67
    momentum_lookback: int = 7
    reversal_threshold_bps: float = 0.4
    edge_scale: float = 1.1
    min_range_bps: float = 52.0
    micro_rebound_max_spread_bps: float = 8.0
    micro_rebound_max_context_range_pos: float = 0.38
    micro_rebound_min_context_rebound_bps: float = 0.0
    micro_rebound_min_ret_bps: float = -2.0
    micro_rebound_confirm_rebound_bps: float = 0.0
    micro_rebound_confirm_min_ret_bps: float = 0.0
    max_edge_bps: float = 155.0


class SwingAlpha(AlphaModel):
    """
    Peak/valley swing model:
    - buy near the lower part of a recent range when short momentum turns up
    - sell near the upper part of a recent range when short momentum turns down
    - allow a small "micro rebound" entry when the local range is still tight but
      the broader context is rebounding cleanly from the lower part of the range
    """

    def __init__(self, config: SwingConfig):
        self.config = config
        if not (0.0 < float(config.buy_band) < float(config.sell_band) < 1.0):
            raise ValueError("SwingConfig requires 0 < buy_band < sell_band < 1")
        self._prices: list[float] = []
        self._ret_bps: list[float] = []

    def predict(self, features: Features) -> AlphaSignal:
        price = float(features.values.get("price", 0.0))
        ret_bps = float(features.values.get("return_bps", 0.0))
        if price <= 0.0:
            return AlphaSignal(ts=features.ts, edge_bps=0.0, p_up=None, meta={"swing_state": "invalid_price"})

        self._prices.append(price)
        self._ret_bps.append(ret_bps)
        keep_n = max(int(self.config.lookback) + 1, int(self.config.momentum_lookback) + 2)
        if len(self._prices) > keep_n:
            self._prices = self._prices[-keep_n:]
        if len(self._ret_bps) > keep_n:
            self._ret_bps = self._ret_bps[-keep_n:]

        if len(self._prices) <= int(self.config.lookback):
            return AlphaSignal(ts=features.ts, edge_bps=0.0, p_up=None, meta={"swing_state": "warmup"})

        prev = self._prices[-(int(self.config.lookback) + 1) : -1]
        high_ref = max(prev)
        low_ref = min(prev)
        if high_ref <= low_ref:
            return AlphaSignal(ts=features.ts, edge_bps=0.0, p_up=None, meta={"swing_state": "flat_range"})

        range_bps = (high_ref / low_ref - 1.0) * 10000.0
        oscillator = (price - low_ref) / (high_ref - low_ref)
        oscillator = max(0.0, min(1.0, oscillator))
        m_lb = max(1, int(self.config.momentum_lookback))
        momentum_bps = sum(self._ret_bps[-m_lb:])
        threshold = float(self.config.reversal_threshold_bps)
        context_range_pos = float(features.values.get("context_range_pos", oscillator))
        context_rebound_bps = max(0.0, float(features.values.get("context_rebound_bps", 0.0) or 0.0))
        spread_bps = max(0.0, float(features.values.get("spread_bps", 0.0) or 0.0))
        volume_z = float(features.values.get("volume_z", 0.0) or 0.0)
        trend_return_bps = float(features.values.get("trend_return_bps", 0.0) or 0.0)

        if range_bps < float(self.config.min_range_bps):
            micro_buy_band = min(0.58, float(self.config.buy_band) + 0.13)
            micro_rebound_max_context_range_pos = max(
                0.0,
                min(1.0, float(self.config.micro_rebound_max_context_range_pos)),
            )
            micro_rebound_min_context_rebound_bps = float(
                self.config.micro_rebound_min_context_rebound_bps
            )
            if micro_rebound_min_context_rebound_bps <= 0.0:
                micro_rebound_min_context_rebound_bps = max(
                    90.0,
                    float(self.config.min_range_bps) * 4.0,
                )
            micro_rebound_min_ret_bps = float(self.config.micro_rebound_min_ret_bps)
            micro_rebound_candidate = (
                range_bps >= max(3.0, float(self.config.min_range_bps) * 0.08)
                and oscillator <= micro_buy_band
                and context_range_pos <= micro_rebound_max_context_range_pos
                and context_rebound_bps >= micro_rebound_min_context_rebound_bps
                and spread_bps <= float(self.config.micro_rebound_max_spread_bps)
                and volume_z >= -1.1
                and ret_bps >= micro_rebound_min_ret_bps
                and momentum_bps >= threshold
                and trend_return_bps >= -260.0
            )
            micro_rebound_confirm_rebound_bps = max(
                0.0,
                float(self.config.micro_rebound_confirm_rebound_bps),
            )
            micro_rebound_confirm_min_ret_bps = float(
                self.config.micro_rebound_confirm_min_ret_bps
            )
            micro_rebound_confirm_ok = (
                micro_rebound_confirm_rebound_bps <= 0.0
                or context_rebound_bps < micro_rebound_confirm_rebound_bps
                or ret_bps >= micro_rebound_confirm_min_ret_bps
            )
            if micro_rebound_candidate and micro_rebound_confirm_ok:
                buy_strength = (micro_buy_band - oscillator) / max(micro_buy_band, 1e-9)
                edge = context_rebound_bps * 0.05
                edge += max(0.0, momentum_bps) * 0.45
                edge += buy_strength * 8.0
                edge -= max(0.0, spread_bps - 2.0) * 0.8
                edge -= max(0.0, context_range_pos - 0.38) * 10.0
                edge -= max(0.0, (-trend_return_bps) - 180.0) * 0.02
                max_edge = max(0.0, float(self.config.max_edge_bps))
                edge = max(12.0, edge)
                if max_edge > 0.0:
                    edge = min(edge, max_edge)
                return AlphaSignal(
                    ts=features.ts,
                    edge_bps=edge,
                    p_up=None,
                    meta={
                        "swing_state": "micro_valley_rebound",
                        "swing_oscillator": oscillator,
                        "swing_momentum_bps": momentum_bps,
                        "swing_range_bps": range_bps,
                        "context_range_pos": context_range_pos,
                        "context_rebound_bps": context_rebound_bps,
                        "micro_rebound_min_context_rebound_bps": micro_rebound_min_context_rebound_bps,
                        "micro_rebound_max_context_range_pos": micro_rebound_max_context_range_pos,
                        "micro_rebound_min_ret_bps": micro_rebound_min_ret_bps,
                        "micro_rebound_confirm_rebound_bps": micro_rebound_confirm_rebound_bps,
                        "micro_rebound_confirm_min_ret_bps": micro_rebound_confirm_min_ret_bps,
                        "swing_high_ref": high_ref,
                        "swing_low_ref": low_ref,
                    },
                )
            if micro_rebound_candidate and not micro_rebound_confirm_ok:
                return AlphaSignal(
                    ts=features.ts,
                    edge_bps=0.0,
                    p_up=None,
                    meta={
                        "swing_state": "micro_rebound_wait_green",
                        "range_bps": range_bps,
                        "context_range_pos": context_range_pos,
                        "context_rebound_bps": context_rebound_bps,
                        "micro_rebound_confirm_rebound_bps": micro_rebound_confirm_rebound_bps,
                        "micro_rebound_confirm_min_ret_bps": micro_rebound_confirm_min_ret_bps,
                        "micro_rebound_min_ret_bps": micro_rebound_min_ret_bps,
                    },
                )
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={
                    "swing_state": "range_too_small",
                    "range_bps": range_bps,
                },
            )

        edge = 0.0
        state = "none"

        if oscillator <= float(self.config.buy_band) and momentum_bps >= threshold and ret_bps >= 0.0:
            buy_strength = (float(self.config.buy_band) - oscillator) / max(float(self.config.buy_band), 1e-9)
            amplitude_bps = min(range_bps, max(0.0, (high_ref - price) / price * 10000.0))
            edge = amplitude_bps * buy_strength * float(self.config.edge_scale)
            state = "valley_rebound"
        elif oscillator >= float(self.config.sell_band) and momentum_bps <= -threshold and ret_bps <= 0.0:
            sell_strength = (oscillator - float(self.config.sell_band)) / max(1.0 - float(self.config.sell_band), 1e-9)
            amplitude_bps = min(range_bps, max(0.0, (price - low_ref) / price * 10000.0))
            edge = -amplitude_bps * sell_strength * float(self.config.edge_scale)
            state = "peak_fade"

        max_edge = max(0.0, float(self.config.max_edge_bps))
        if max_edge > 0.0:
            edge = max(-max_edge, min(max_edge, edge))

        return AlphaSignal(
            ts=features.ts,
            edge_bps=edge,
            p_up=None,
            meta={
                "swing_state": state,
                "swing_oscillator": oscillator,
                "swing_momentum_bps": momentum_bps,
                "swing_range_bps": range_bps,
                "swing_high_ref": high_ref,
                "swing_low_ref": low_ref,
            },
        )
