import unittest

from trading.rotation_universe import RESERVED_PORTS, build_lanes


class TestRotationUniverse(unittest.TestCase):
    def test_lane_ports_avoid_reserved_ports(self) -> None:
        for symbol, lane in build_lanes().items():
            ports = set(lane["ports"])
            overlap = ports.intersection(RESERVED_PORTS)
            self.assertFalse(overlap, f"{symbol} uses reserved ports: {sorted(overlap)}")


if __name__ == "__main__":
    unittest.main()
