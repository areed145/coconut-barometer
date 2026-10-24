// A uPlot chart that sizes itself to its container and follows the light/dark colors, and the color gradient
// (Settings → Display → Colors) for a series that asks for it.
import uPlot from "../vendor/uPlot.esm.js";
import { html, useEffect, useRef, useThemeTick, shade } from "./lib.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const STEPS = 8;                                     // stops along the gradient: enough for its low, medium and high
const faint = (c, a) => c.replace("rgb(", "rgba(").replace(")", `,${a})`);
// The gradient up the plot: low at the bottom, high at the top, as the rest of the app shows an amount (#68).
const upward = (u, alpha = 1) => {
  const { top, height } = u.bbox, g = u.ctx.createLinearGradient(0, top + height, 0, top);
  for (let i = 0; i <= STEPS; i++) g.addColorStop(i / STEPS, alpha < 1 ? faint(shade(i / STEPS), alpha) : shade(i / STEPS));
  return g;
};

// series: [{label, values, color: "--chart-1", bars?, dash?: [on, off], fill?: false, gradient?: true}], x: day strings
// (YYYY-MM-DD, GMT). gradient: the line and its fill in the color gradient, instead of `color`.
export function Chart({ days, series, height = 220 }) {
  const box = useRef(null);
  const theme = useThemeTick();            // colors come from the CSS variables: redraw on a theme switch
  useEffect(() => {
    const el = box.current;
    if (!el || !days.length) return;
    const xs = days.map((d) => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10)) / 1000);
    const solid = (i) => (series[i - 1]?.gradient ? shade(1) : css(series[i - 1]?.color || "--muted"));
    const axis = { stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } };
    const opts = {
      width: el.clientWidth, height, tzDate: (ts) => uPlot.tzDate(new Date(ts * 1000), "Etc/UTC"),
      // A gradient isn't a color a legend marker or the hover point can take: a gradient series shows its high end there.
      cursor: { points: { size: 6, stroke: (u, i) => solid(i), fill: (u, i) => solid(i) } },
      legend: { live: true, markers: { stroke: (u, i) => solid(i), fill: (u, i) => (series[i - 1]?.gradient ? faint(shade(1), 0.2) : solid(i) + "22") } },
      scales: { x: { time: true } },
      axes: [axis, { ...axis, size: 56, values: (u, vals) => vals.map((v) => v == null ? "" : v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k" : v) }],
      series: [{}, ...series.map((s) => ({
        label: s.label, stroke: s.gradient ? (u) => upward(u) : css(s.color), width: 2, points: { show: false }, dash: s.dash,
        fill: s.fill === false ? undefined : s.gradient ? (u) => upward(u, 0.18) : s.bars ? css(s.color) : css(s.color) + "22",
        paths: s.bars ? uPlot.paths.bars({ size: [0.7, 24] }) : undefined,
      }))],
    };
    const plot = new uPlot(opts, [xs, ...series.map((s) => s.values)], el);
    const ro = new ResizeObserver(() => plot.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    return () => { ro.disconnect(); plot.destroy(); };
  }, [days, series, height, theme]);
  return days.length ? html`<div class="chart" ref=${box}></div>` : html`<p class="muted">No daily stats for this range yet.</p>`;
}
