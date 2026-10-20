// A uPlot chart that sizes itself to its container and follows the light/dark colours.
import uPlot from "../vendor/uPlot.esm.js";
import { html, useEffect, useRef } from "./lib.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// series: [{label, values, color: "--chart-1", bars?}], x: day strings (YYYY-MM-DD, GMT)
export function Chart({ days, series, height = 220 }) {
  const box = useRef(null);
  useEffect(() => {
    const el = box.current;
    if (!el || !days.length) return;
    const xs = days.map((d) => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10)) / 1000);
    const axis = { stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } };
    const opts = {
      width: el.clientWidth, height, tzDate: (ts) => uPlot.tzDate(new Date(ts * 1000), "Etc/UTC"),
      cursor: { points: { size: 6 } }, legend: { live: true },
      scales: { x: { time: true } },
      axes: [axis, { ...axis, size: 56, values: (u, vals) => vals.map((v) => v == null ? "" : v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k" : v) }],
      series: [{}, ...series.map((s) => ({
        label: s.label, stroke: css(s.color), width: 2, points: { show: false },
        fill: s.bars ? css(s.color) : css(s.color) + "22",
        paths: s.bars ? uPlot.paths.bars({ size: [0.7, 24] }) : undefined,
      }))],
    };
    const plot = new uPlot(opts, [xs, ...series.map((s) => s.values)], el);
    const ro = new ResizeObserver(() => plot.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    return () => { ro.disconnect(); plot.destroy(); };
  }, [days, series, height]);
  return days.length ? html`<div class="chart" ref=${box}></div>` : html`<p class="muted">No daily stats for this range yet.</p>`;
}
