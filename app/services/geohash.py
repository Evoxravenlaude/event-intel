"""
Minimal geohash implementation (encode + neighbours) — no dependency needed.

Precision 5 ≈ 4.9 km × 4.9 km cells, which is the granularity coverage is
tracked at: a scan centred anywhere in a cell marks that cell fresh.
"""
from __future__ import annotations
import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

# Approximate cell height in km per precision (at the equator; narrower in longitude nearer the poles)
CELL_KM = {3: 156.0, 4: 39.1, 5: 4.9, 6: 1.2, 7: 0.15}


def precision_for_radius(radius_km: float) -> int:
    """Pick a cell size so a scan touches a handful of cells, not hundreds."""
    if radius_km <= 12:
        return 5
    if radius_km <= 60:
        return 4
    return 3


def prefixes(cell: str, min_len: int = 3) -> list[str]:
    """The cell and all of its parent cells (geohash prefixes), longest first."""
    return [cell[:n] for n in range(len(cell), min_len - 1, -1)]


def encode(lat: float, lng: float, precision: int = 5) -> str:
    lat_range = [-90.0, 90.0]
    lng_range = [-180.0, 180.0]
    bits = 0
    bit_count = 0
    even = True
    out = []
    while len(out) < precision:
        if even:
            mid = (lng_range[0] + lng_range[1]) / 2
            if lng >= mid:
                bits = (bits << 1) | 1
                lng_range[0] = mid
            else:
                bits <<= 1
                lng_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                bits = (bits << 1) | 1
                lat_range[0] = mid
            else:
                bits <<= 1
                lat_range[1] = mid
        even = not even
        bit_count += 1
        if bit_count == 5:
            out.append(_BASE32[bits])
            bits = 0
            bit_count = 0
    return "".join(out)


def decode(cell: str) -> tuple[float, float]:
    """Centre point of a cell."""
    lat_range = [-90.0, 90.0]
    lng_range = [-180.0, 180.0]
    even = True
    for ch in cell:
        idx = _BASE32.index(ch)
        for mask in (16, 8, 4, 2, 1):
            if even:
                mid = (lng_range[0] + lng_range[1]) / 2
                if idx & mask:
                    lng_range[0] = mid
                else:
                    lng_range[1] = mid
            else:
                mid = (lat_range[0] + lat_range[1]) / 2
                if idx & mask:
                    lat_range[0] = mid
                else:
                    lat_range[1] = mid
            even = not even
    return (lat_range[0] + lat_range[1]) / 2, (lng_range[0] + lng_range[1]) / 2


def cells_covering(lat: float, lng: float, radius_km: float, precision: int = 5) -> list[str]:
    """
    All cells whose centre lies within a square of side 2*radius around the
    point (a cheap superset of the circle). Capped so a huge radius cannot
    explode into thousands of cells.
    """
    size_km = CELL_KM.get(precision, 4.9)
    steps = max(1, min(int(math.ceil(radius_km / size_km)), 12))
    d_lat = size_km / 111.32
    cos_lat = max(math.cos(math.radians(lat)), 0.05)
    d_lng = size_km / (111.32 * cos_lat)
    seen: list[str] = []
    for i in range(-steps, steps + 1):
        for j in range(-steps, steps + 1):
            la = max(-90.0, min(90.0, lat + i * d_lat))
            lo = ((lng + j * d_lng + 180.0) % 360.0) - 180.0
            cell = encode(la, lo, precision)
            if cell not in seen:
                seen.append(cell)
    return seen
