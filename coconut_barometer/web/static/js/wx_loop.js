// The Weather map loop's timeline (#74). The radar's frames run up to now (kept for radar.py's LOOP_WINDOW, ~2¼ h);
// the satellite's end 30–60 minutes ago (NASA GIBS publishes late). Looping the union of both left each one blank
// or frozen for most of the loop, so:
//   radar and satellite   the hour ending at the newest frame both have (in practice the satellite's), so each
//                         moves on every step
//   radar only            its last hour, up to now
//   satellite only        all its frames
// If the radar has nothing in the shared hour yet (its history still filling after a deploy), it loops its own hour.

export const LOOP = 3600;

// radar, sat: each layer's frame times, ascending ([] when it's off) -> {times: the timeline, radar: the radar frames
// in it, start, end}.
export function loopWindow(radar, sat) {
  const last = (a) => a[a.length - 1];
  const within = (start, end) => (t) => t >= start && t <= end;
  if (!radar.length) return { times: [...sat], radar: [], start: sat[0] ?? null, end: last(sat) ?? null };
  if (sat.length) {
    const end = Math.min(last(radar), last(sat)), start = end - LOOP;
    const r = radar.filter(within(start, end));
    if (r.length) return { times: [...new Set([...r, ...sat.filter(within(start, end))])].sort((a, b) => a - b), radar: r, start, end };
  }
  const end = last(radar), start = end - LOOP, r = radar.filter(within(start, end));
  return { times: r, radar: r, start, end };
}
