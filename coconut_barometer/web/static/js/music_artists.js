// Music → Artists (#/music/artists): the artist graph two ways. "Top in this period" draws the most played artists of
// the period shared with Stats (music_graph.js); "Whole graph" grows a map from anywhere in every artist with over 10
// plays, all time (music_explore.js), since that graph is built once and kept. Below both, where the links come from.
import { html } from "./lib.js";
import { ArtistNetwork, LinkData } from "./music_graph.js";
import { Explorer } from "./music_explore.js";
import { usePeriod } from "./period.js";

const VIEWS = [["top", "Top in this period"], ["whole", "Whole graph"]];

export function Artists({ params }) {
  const view = params.view === "whole" ? "whole" : "top";
  const [period, setPeriod] = usePeriod("music");
  const open = (v) => {
    const q = new URLSearchParams({ ...(params.range ? { range: params.range } : {}), ...(v === "whole" ? { view: "whole" } : {}) }).toString();
    location.hash = `#/music/artists${q ? "?" + q : ""}`;
  };
  const lead = html`<span class="seg" role="group" aria-label="View">${VIEWS.map(([v, label]) => html`<button key=${v} class=${view === v ? "primary" : ""}
    aria-pressed=${view === v} onClick=${() => view !== v && open(v)}>${label}</button>`)}</span>`;
  return html`${view === "whole" ? html`<${Explorer} start=${params.artist} lead=${lead} />`
      : html`<${ArtistNetwork} focus=${params.artist} period=${period} setPeriod=${setPeriod} lead=${lead} />`}
    <${LinkData} />`;
}
