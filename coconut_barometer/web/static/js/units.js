// Weather units. Everything is stored metric; Settings → Display → Units only changes how it's shown.
// A module of its own so Home can show a temperature without loading the Weather pages.
const UNITS = {
  metric: { temp: ["°C", (v) => v], speed: ["m/s", (v) => v], pressure: ["hPa", (v) => v], rain: ["mm", (v) => v], dist: ["km", (v) => v], height: ["m", (v) => v] },
  imperial: { temp: ["°F", (v) => v * 1.8 + 32], speed: ["mph", (v) => v * 2.23694], pressure: ["inHg", (v) => v * 0.02953],
              rain: ["in", (v) => v / 25.4], dist: ["mi", (v) => v * 0.621371], height: ["ft", (v) => v * 3.28084] },
};
export const unitsFor = (system) => UNITS[system] || UNITS.metric;
export const fmt = (v, digits = 1) => (v == null ? "–" : Number(v).toFixed(digits));
export const show = (u, kind, v, digits = 1) => (v == null ? "–" : `${fmt(unitsFor(u)[kind][1](v), digits)} ${unitsFor(u)[kind][0]}`);
const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
export const compass = (deg) => (deg == null ? "" : COMPASS[Math.round((deg % 360) / 22.5) % 16]);
