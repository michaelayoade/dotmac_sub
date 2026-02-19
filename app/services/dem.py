from __future__ import annotations

import math
import os
import struct
import zipfile
from collections import OrderedDict

from app.config import settings

SRTM_TILE_SIZE = 3601
SRTM_SAMPLES_PER_DEGREE = 3600
SRTM_VOID_VALUE = -32768
SRTM_SOURCE = "srtm_30m"
SRTM_CACHE_SIZE = 1000
_ELEVATION_CACHE: OrderedDict[tuple[str, int, int], dict] = OrderedDict()


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def _tile_name(latitude: float, longitude: float) -> tuple[str, int, int]:
    lat_floor = math.floor(latitude)
    lon_floor = math.floor(longitude)
    lat_prefix = "N" if lat_floor >= 0 else "S"
    lon_prefix = "E" if lon_floor >= 0 else "W"
    tile = f"{lat_prefix}{abs(lat_floor):02d}{lon_prefix}{abs(lon_floor):03d}"
    return tile, lat_floor, lon_floor


def _tile_indexes(latitude: float, longitude: float, lat_floor: int, lon_floor: int) -> tuple[int, int]:
    row = int(round((lat_floor + 1 - latitude) * SRTM_SAMPLES_PER_DEGREE))
    col = int(round((longitude - lon_floor) * SRTM_SAMPLES_PER_DEGREE))
    row = _clamp(row, 0, SRTM_SAMPLES_PER_DEGREE)
    col = _clamp(col, 0, SRTM_SAMPLES_PER_DEGREE)
    return row, col


def _read_value_from_bytes(data: bytes, row: int, col: int) -> int | None:
    offset = (row * SRTM_TILE_SIZE + col) * 2
    if offset + 2 > len(data):
        return None
    value = struct.unpack_from(">h", data, offset)[0]
    if value == SRTM_VOID_VALUE:
        return None
    return int(value)


def _read_value_from_file(path: str, row: int, col: int) -> int | None:
    offset = (row * SRTM_TILE_SIZE + col) * 2
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return None
    if offset + 2 > file_size:
        return None
    with open(path, "rb") as handle:
        handle.seek(offset)
        raw = handle.read(2)
    if len(raw) != 2:
        return None
    value = struct.unpack(">h", raw)[0]
    if value == SRTM_VOID_VALUE:
        return None
    return int(value)


def _read_value_from_zip(path: str, row: int, col: int, tile: str) -> int | None:
    with zipfile.ZipFile(path) as archive:
        hgt_name = f"{tile}.hgt"
        members = [name for name in archive.namelist() if name.lower().endswith(".hgt")]
        if hgt_name in archive.namelist():
            data = archive.read(hgt_name)
        elif members:
            data = archive.read(members[0])
        else:
            return None
    return _read_value_from_bytes(data, row, col)


def get_elevation(latitude: float, longitude: float, data_dir: str | None = None) -> dict:
    data_root = data_dir or settings.dem_data_dir
    tile, lat_floor, lon_floor = _tile_name(latitude, longitude)
    row, col = _tile_indexes(latitude, longitude, lat_floor, lon_floor)
    cache_key = (tile, row, col)
    cached = _ELEVATION_CACHE.get(cache_key)
    if cached is not None:
        _ELEVATION_CACHE.move_to_end(cache_key)
        return dict(cached)

    hgt_path = os.path.join(data_root, f"{tile}.hgt")
    zip_path = f"{hgt_path}.zip"

    if os.path.isfile(hgt_path):
        value = _read_value_from_file(hgt_path, row, col)
        available = True
    elif os.path.isfile(zip_path):
        value = _read_value_from_zip(zip_path, row, col, tile)
        available = True
    else:
        value = None
        available = False

    result = {
        "latitude": latitude,
        "longitude": longitude,
        "elevation_m": value,
        "tile": tile,
        "source": SRTM_SOURCE,
        "available": available,
        "void": available and value is None,
    }
    _ELEVATION_CACHE[cache_key] = dict(result)
    if len(_ELEVATION_CACHE) > SRTM_CACHE_SIZE:
        _ELEVATION_CACHE.popitem(last=False)
    return result
