// Justified rows (#128): photos at their own aspect ratios, each full row scaled to fill the width exactly, like Flickr.
// No imports, so the tests can run it as it is.

// The aspect ratio a tile is laid out at: width / height, or square when unknown. Very wide panoramas and very
// tall strips are cropped to these limits so one photo can't take a whole row or shrink to a sliver.
export const aspect = (p) => (p.width > 0 && p.height > 0 ? Math.min(3, Math.max(0.5, p.width / p.height)) : 1);

// Rows for these aspect ratios in `width` pixels: [{ start, end, height }], photos start..end-1, each `height` tall
// and `aspects[i] * height` wide, with `gap` between. A row closes when it's full at the target height, with or
// without its last photo, whichever leaves it nearer the target. The last row isn't full; it stays at the target.
export function justify(aspects, width, target, gap) {
  const rows = [];
  let start = 0, sum = 0;
  const fit = (n, s) => (width - gap * (n - 1)) / s;          // the height that makes n photos of total aspect s fill the row
  for (let i = 0; i < aspects.length; i++) {
    sum += aspects[i];
    const n = i - start + 1;
    if (sum * target + gap * (n - 1) < width) continue;
    const h = fit(n, sum);
    if (n > 1 && Math.abs(fit(n - 1, sum - aspects[i]) - target) < Math.abs(h - target)) {
      rows.push({ start, end: i, height: fit(n - 1, sum - aspects[i]) });
      start = i; sum = aspects[i];
      if (sum * target < width) continue;
      rows.push({ start, end: i + 1, height: fit(1, sum) });  // one photo wider than the row on its own
    } else rows.push({ start, end: i + 1, height: h });
    start = i + 1; sum = 0;
  }
  if (start < aspects.length) rows.push({ start, end: aspects.length, height: target, last: true });
  return rows;
}
