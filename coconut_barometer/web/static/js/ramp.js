// Colors along a ramp of stops, [{[key]: value, color: "#rrggbb"}]: the radar's dBZ (radar_layer.js), and the
// temperature and wind speed (surface_layer.js). Between two stops the color is blended; below the first or above
// the last, theirs; on a stop (or two stops at one value), that stop's own.

const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));

// [the stop at or below v, the one above, how far v is from the first to the second]; one stop: v is outside them.
function around(stops, v, key) {
  const s = [...stops].sort((a, b) => a[key] - b[key]);
  if (v <= s[0][key]) return [s[0]];
  const j = s.findIndex((x) => x[key] >= v);
  if (j < 0) return [s[s.length - 1]];
  const [a, b] = [s[j - 1], s[j]];
  if (b[key] === a[key] || b[key] === v) return [b];
  return [a, b, (v - a[key]) / (b[key] - a[key])];
}

// The CSS color at v: a stop's own hex, else rgb() blended.
export function rampColor(stops, v, key = "at") {
  const [a, b, k] = around(stops, v, key);
  if (!b) return a.color;
  return `rgb(${hex(a.color).map((x, i) => Math.round(x + (hex(b.color)[i] - x) * k)).join(",")})`;
}

// [r, g, b] at v, for a canvas.
export function rampRGB(stops, v, key = "at") {
  const [a, b, k] = around(stops, v, key);
  if (!b) return hex(a.color);
  return hex(a.color).map((x, i) => Math.round(x + (hex(b.color)[i] - x) * k));
}

// A legend's CSS gradient from lo to hi, n colors along it.
export function rampGradient(stops, lo, hi, key = "at", n = 24) {
  return `linear-gradient(90deg, ${Array.from({ length: n }, (_, i) => {
    const v = lo + ((hi - lo) * i) / (n - 1);
    return `${rampColor(stops, v, key)} ${((i / (n - 1)) * 100).toFixed(2)}%`;
  }).join(", ")})`;
}
