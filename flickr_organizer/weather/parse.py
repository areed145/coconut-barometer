"""WeatherFlow's observation arrays, as named fields. Everything is metric, as sent."""
from __future__ import annotations

# obs_st (Tempest), by index. Fields after `interval` are Rain Check values we don't keep.
OBS_ST = ("ts", "wind_lull", "wind_avg", "wind_gust", "wind_dir", "wind_interval", "pressure", "temp",
          "humidity", "illuminance", "uv", "solar", "rain", "precip_type", "strike_dist", "strike_count",
          "battery", "report_interval")
FIELDS = OBS_ST        # the columns of wx_obs, in this order, after device_id


def obs_st(arr: list) -> dict | None:
    """One obs_st array as a dict, or None if it's too short or has no time. Missing values stay None."""
    if not isinstance(arr, (list, tuple)) or len(arr) < len(OBS_ST) or arr[0] is None:
        return None
    row = dict(zip(OBS_ST, arr))
    row["ts"] = int(row["ts"])
    return row


def sea_level(pressure_mb: float | None, temp_c: float | None, elevation_m: float | None) -> float | None:
    """Station pressure reduced to sea level (the hypsometric formula), what forecasts quote."""
    if pressure_mb is None or temp_c is None or not elevation_m:
        return pressure_mb
    return pressure_mb * (1 - 0.0065 * elevation_m / (temp_c + 0.0065 * elevation_m + 273.15)) ** -5.257
