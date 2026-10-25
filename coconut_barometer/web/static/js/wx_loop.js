// The Weather map loop's timeline (#74). The radar's frames run up to now (kept for radar.py's LOOP_WINDOW, ~2¼ h);
// the satellite's end 30–60 minutes ago (NASA GIBS publishes late), sometimes hours ago. Ending the loop where both
// ended cut the radar's newest hour or more from it (#103), so the radar leads:
//   radar (with or without satellite)   its last hour, up to its newest frame (from its oldest, while its history
//                                       fills after a deploy); satellite frames in that span are steps too, and each
//                                       step shows the satellite's newest frame at or before it (satAt), or none once
//                                       that's over SAT_STALE old
//   satellite only                      all its frames

export const LOOP = 3600;
export const SAT_STALE = 3600;   // a satellite frame held this far behind the step shown is hidden rather than shown

// radar, sat: each layer's frame times, ascending ([] when it's off) -> {times: the timeline, radar: the radar frames
// in it, start, end}.
export function loopWindow(radar, sat) {
  const last = (a) => a[a.length - 1];
  if (!radar.length) return { times: [...sat], radar: [], start: sat[0] ?? null, end: last(sat) ?? null };
  const end = last(radar), r = radar.filter((t) => t >= end - LOOP), start = r[0], within = (t) => t >= start && t <= end;
  return { times: [...new Set([...r, ...sat.filter(within)])].sort((a, b) => a - b), radar: r, start, end };
}

// frames: the satellite's, ascending ({t, ...}) -> the one to show at t: the newest at or before it (give or take
// 2½ minutes, as the radar's and the satellite's clocks don't line up), or null when there's none or it's stale.
export function satAt(frames, t) {
  let best = null;
  for (const f of frames) if (f.t <= t + 150) best = f;
  return best && t - best.t <= SAT_STALE ? best : null;
}
