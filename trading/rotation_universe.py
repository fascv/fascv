from __future__ import annotations

from collections.abc import Iterable

# Baseline universe that has been used in production so far.
BASE_POOL: tuple[str, ...] = (
    "NEAR",
    "DOT",
    "OP",
    "ENA",
    "RENDER",
    "HBAR",
    "ESP",
    "KITE",
    "MORPHO",
    "PHA",
    "XRP",
    "SUI",
    "ETH",
    "ADA",
    "DOGE",
    "LINK",
    "AVAX",
    "ARB",
    "APT",
    "ATOM",
    "SEI",
    "TIA",
    "TRX",
    "UNI",
    "AAVE",
    "FIL",
    "ALGO",
    "XLM",
    "INJ",
    "TON",
    "SOL",
    "BNB",
    "LTC",
    "BCH",
    "ETC",
    "VET",
    "ICP",
    "FET",
    "IMX",
    "GRT",
    "SAND",
    "CRV",
    "COMP",
    "SNX",
    "SHIB",
    "LDO",
    "EGLD",
    "ZEC",
    "XTZ",
    "NEO",
)

# Expansion set to reach 100 symbols with intentionally mixed profile/behavior.
EXTRA_POOL: tuple[str, ...] = (
    "BTC",
    "AR",
    "APE",
    "API3",
    "ARKM",
    "AXS",
    "CAKE",
    "CFX",
    "CHZ",
    "COTI",
    "CVX",
    "DASH",
    "DYDX",
    "DYM",
    "EIGEN",
    "ENJ",
    "ENS",
    "ETHFI",
    "FLUX",
    "GALA",
    "GMT",
    "GMX",
    "IOTA",
    "JUP",
    "JTO",
    "KAIA",
    "LPT",
    "MANTA",
    "MASK",
    "MINA",
    "NOT",
    "ONDO",
    "ORDI",
    "PENDLE",
    "PEPE",
    "POL",
    "PYTH",
    "QTUM",
    "RUNE",
    "SAGA",
    "SKL",
    "STRK",
    "STX",
    "SUSHI",
    "THETA",
    "TRB",
    "TWT",
    "WIF",
    "WLD",
    "YGG",
)

# Additional 24h gainers (not yet in baseline rotation pool) promoted into full universe.
# Snapshot source: Binance USDC universe scan on 2026-03-06.
BOOST_POOL_24H: tuple[str, ...] = (
    "SIGN",
    "BANANAS31",
    "SENT",
    "RESOLV",
    "INIT",
    "EDEN",
    "FOGO",
    "MMT",
    "ALLO",
    "PLUME",
    "2Z",
    "BREV",
    "KAITO",
    "ZAMA",
    "PAXG",
    "PARTI",
    "EUR",
    "EURI",
    "U",
)

# Phase-A shadow scan keep/review names promoted into the full lane universe so
# they can be tested as fixed live pools without ad-hoc port wiring.
SHADOW_PHASE_A_POOL: tuple[str, ...] = (
    "ZK",
    "BONK",
    "TST",
    "PUMP",
    "SKY",
    "ROSE",
    "MANTRA",
    "WLFI",
    "GUN",
    "ROBO",
    "PEOPLE",
    "VIRTUAL",
    "KERNEL",
    "ORCA",
    "ZRO",
    "S",
)

POOL: tuple[str, ...] = BASE_POOL + EXTRA_POOL + BOOST_POOL_24H + SHADOW_PHASE_A_POOL

# Keep lane ports clear of globally shared local services.
RESERVED_PORTS: frozenset[int] = frozenset({8940})

LEGACY_PORTS: dict[str, tuple[int, int, int, int, int]] = {
    "OP": (8004, 8114, 8124, 8134, 8144),
    "NEAR": (8008, 8410, 8420, 8430, 8440),
    "ENA": (8010, 8510, 8520, 8530, 8540),
    "RENDER": (8012, 8610, 8620, 8630, 8640),
    "DOT": (8014, 8710, 8720, 8730, 8740),
    "HBAR": (8016, 8810, 8820, 8830, 8840),
    "ESP": (8018, 8910, 8920, 8930, 8950),
    "KITE": (8020, 9010, 9020, 9030, 9040),
    "PHA": (8022, 9110, 9120, 9130, 9140),
    "XRP": (8024, 9210, 9220, 9230, 9240),
    "ADA": (8026, 9310, 9320, 9330, 9340),
    "DOGE": (8028, 9410, 9420, 9430, 9440),
    "LINK": (8030, 9510, 9520, 9530, 9540),
    "AVAX": (8032, 9610, 9620, 9630, 9640),
    "ARB": (8034, 9710, 9720, 9730, 9740),
    "APT": (8036, 9810, 9820, 9830, 9840),
    "ATOM": (8038, 9910, 9920, 9930, 9940),
    "SEI": (8040, 10010, 10020, 10030, 10040),
    "TIA": (8042, 10110, 10120, 10130, 10140),
    "TRX": (8044, 10210, 10220, 10230, 10240),
    "UNI": (8046, 10310, 10320, 10330, 10340),
    "AAVE": (8048, 10410, 10420, 10430, 10440),
    "FIL": (8050, 10510, 10520, 10530, 10540),
    "ALGO": (8052, 10610, 10620, 10630, 10640),
    "XLM": (8054, 10710, 10720, 10730, 10740),
    "INJ": (8056, 10810, 10820, 10830, 10840),
    "TON": (8058, 10910, 10920, 10930, 10940),
    "MORPHO": (8060, 11010, 11020, 11030, 11040),
    "SUI": (8062, 11110, 11120, 11130, 11140),
    "ETH": (8064, 11210, 11220, 11230, 11240),
    "SOL": (8066, 11310, 11320, 11330, 11340),
    "BNB": (8068, 11410, 11420, 11430, 11440),
    "LTC": (8070, 11510, 11520, 11530, 11540),
    "BCH": (8072, 11610, 11620, 11630, 11640),
    "ETC": (8074, 11710, 11720, 11730, 11740),
    "VET": (8076, 11810, 11820, 11830, 11840),
    "ICP": (8078, 11910, 11920, 11930, 11940),
    "FET": (8080, 12010, 12020, 12030, 12040),
    "IMX": (8082, 12110, 12120, 12130, 12140),
    "GRT": (8084, 12210, 12220, 12230, 12240),
    "SAND": (8086, 12310, 12320, 12330, 12340),
    "CRV": (8088, 12410, 12420, 12430, 12440),
    "COMP": (8090, 12510, 12520, 12530, 12540),
    "SNX": (8092, 12610, 12620, 12630, 12640),
    "SHIB": (8094, 12710, 12720, 12730, 12740),
    "LDO": (8096, 12810, 12820, 12830, 12840),
    "EGLD": (8098, 12910, 12920, 12930, 12940),
    "ZEC": (8100, 13010, 13020, 13030, 13040),
    "XTZ": (8102, 13110, 13120, 13130, 13140),
    "NEO": (8104, 13210, 13220, 13230, 13240),
}


def build_ports(pool: Iterable[str] | None = None) -> dict[str, tuple[int, int, int, int, int]]:
    ports = dict(LEGACY_PORTS)
    extra_idx = 0
    for symbol in (pool or POOL):
        if symbol in ports:
            continue
        control = 8206 + (extra_idx * 2)
        exec_port = 13310 + (extra_idx * 100)
        ports[str(symbol).upper()] = (
            control,
            exec_port,
            exec_port + 10,
            exec_port + 20,
            exec_port + 30,
        )
        extra_idx += 1
    for symbol, lane_ports in ports.items():
        overlap = RESERVED_PORTS.intersection(lane_ports)
        if overlap:
            used = ", ".join(str(port) for port in sorted(overlap))
            raise ValueError(f"{symbol} uses reserved local port(s): {used}")
    return ports


PORTS: dict[str, tuple[int, int, int, int, int]] = build_ports(POOL)


def build_lanes(pool: Iterable[str] | None = None) -> dict[str, dict[str, object]]:
    symbols = [str(symbol).upper() for symbol in (pool or POOL)]
    ports = build_ports(symbols)
    lanes: dict[str, dict[str, object]] = {}
    for symbol in symbols:
        slug = symbol.lower()
        lanes[symbol] = {
            "slug": slug,
            "config": f"configs/live_binance_{slug}_usdc_rotation.yaml",
            "ports": ports[symbol],
        }
    return lanes
