"""The artist network on the Music page (#/music): who you listen to, and how they connect.

Edges, each a different reason two artists are linked:
  colisten   played back to back (a session), counted from the play order
  collab     named together: "A feat. B", "(with B)" in a song's title, or "A & B" when both are artists on their own
  credit     a composer in Apple's catalog who is also an artist you play, or who wrote for several of them
  similar    Claude's judgement, from the Anthropic connection Vision uses. Asked on request (ask_similar), kept in
             the database, and the graph is complete without it

Clusters are found by greedy modularity on the combined weights. Hidden bridges are the modestly played artists whose
connections reach several clusters: the ones that tie your listening together."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections import Counter, defaultdict

NODES = 100                      # the most played artists drawn
SESSION_GAP = 15 * 60            # seconds between two plays that are still one sitting
MIN_COLISTEN = 3                 # fewer back-to-back plays than this is chance
MIN_COLISTEN_SCORE = 0.04        # and of the plays the two share out between them
CREDIT_MAX_ARTISTS = 8           # a composer behind more artists than this ("Traditional") says nothing
WEIGHTS = {"colisten": 1.0, "collab": 1.0, "credit": 0.6, "similar": 0.8}
GENERIC_CREDITS = {"traditional", "public domain", "various", "various artists", "unknown", "anonymous"}
META_SIMILAR, META_LABELS = "music_graph_similar", "music_graph_labels"

FEATURING = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with)\s+", re.I)
TITLE_FEAT = re.compile(r"[(\[]\s*(?:feat\.?|featuring|ft\.?|with)\s+([^)\]]+)[)\]]", re.I)
SPLIT = re.compile(r"\s*(?:,|&|\+)\s*|\s+(?:and|x)\s+", re.I)     # a lone "X" or "And" is a name, not a separator


class GraphError(RuntimeError):
    """Claude can't be asked: the message is safe to show."""


def _people(text: str) -> list[str]:
    return [p.strip() for p in SPLIT.split(text) if p.strip()]


def credited(artist: str, title: str, known: set[str]) -> tuple[str, list[str]]:
    """(the main artist, the others) of one play. Plain "&" is left alone unless both halves are artists on their
    own: "Simon & Garfunkel" is one act, "Drake & Future" when each is played alone is two."""
    others: list[str] = []
    main = artist
    m = FEATURING.search(artist)
    if m:
        main, rest = artist[:m.start()].strip(), artist[m.end():]
        others += _people(rest)
    if main not in known and (parts := _people(main)) and len(parts) > 1 and all(p in known for p in parts):
        main, others = parts[0], parts[1:] + others
    for t in TITLE_FEAT.finditer(title or ""):
        others += _people(t.group(1))
    return main, [o for o in dict.fromkeys(others) if o and o != main]


def composers(text: str | None) -> list[str]:
    return [c for c in _people(text or "") if c.lower() not in GENERIC_CREDITS]


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def build(rows: list[dict], similar: list[dict] | None = None, labels: dict | None = None,
          limit: int = NODES) -> dict:
    """rows: music_graph_plays, in play order (artist, genres, composer, played_at, name, ms)."""
    known = {r["artist"] for r in rows}
    plays: Counter = Counter()
    minutes: Counter = Counter()
    genres: dict = defaultdict(Counter)
    collab: Counter = Counter()
    colisten: Counter = Counter()
    credit_artists: dict = defaultdict(Counter)       # composer -> artist -> tracks
    previous = None
    for r in rows:
        main, others = credited(r["artist"], r.get("name") or "", known)
        plays[main] += 1
        minutes[main] += (r.get("ms") or 0) / 60000
        for g in json.loads(r["genres"] or "[]"):
            genres[main][g] += 1
        for o in others:
            collab[_pair(main, o)] += 1
            plays[o] += 0
        for c in composers(r.get("composer")):
            credit_artists[c][main] += 1
        if previous and previous[0] != main and r["played_at"] - previous[1] <= SESSION_GAP:
            colisten[_pair(previous[0], main)] += 1
        previous = (main, r["played_at"])
    nodes = [a for a, n in plays.most_common(limit) if n > 0]
    keep = set(nodes)
    edges: dict = defaultdict(lambda: {"kinds": {}, "weight": 0.0})

    def link(a, b, kind, strength, detail=None):
        if a == b or a not in keep or b not in keep:
            return
        e = edges[_pair(a, b)]
        e["kinds"][kind] = detail if detail is not None else round(strength, 3)
        e["weight"] += WEIGHTS[kind] * strength

    for (a, b), n in colisten.items():
        score = n / math.sqrt(max(1, plays[a]) * max(1, plays[b]))
        if n >= MIN_COLISTEN and score >= MIN_COLISTEN_SCORE:
            link(a, b, "colisten", min(1.5, score * 4), n)
    for (a, b), n in collab.items():
        link(a, b, "collab", 1.0, n)
    for composer, by in credit_artists.items():
        if composer in keep:                           # the composer is an artist you play: they wrote for the others
            for a in by:
                link(a, composer, "credit", 1.0, by[a])
        elif 2 <= len(by) <= CREDIT_MAX_ARTISTS:
            names = sorted(by)
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    link(a, b, "credit", 1.0, composer)
    for s in similar or []:
        link(s["a"], s["b"], "similar", 1.0, s.get("why") or "")
    weight = {pair: e["weight"] for pair, e in edges.items()}
    group = communities(nodes, weight)
    members: dict = defaultdict(list)
    for a in nodes:
        members[group[a]].append(a)
    names = {}
    for cid, people in members.items():
        names[cid] = (labels or {}).get(member_key(people)) or _genre_label([genres[p] for p in people])
    out_nodes = [{"id": a, "plays": plays[a], "minutes": round(minutes[a]), "cluster": group[a],
                  "genre": genres[a].most_common(1)[0][0] if genres[a] else None,
                  "degree": sum(1 for p in weight if a in p)} for a in nodes]
    return {"nodes": out_nodes,
            "edges": [{"a": a, "b": b, "kinds": e["kinds"], "weight": round(e["weight"], 3)} for (a, b), e in sorted(edges.items())],
            "clusters": [{"id": cid, "label": names[cid], "size": len(people), "members": people[:12],
                          "key": member_key(people)} for cid, people in sorted(members.items())],
            "bridges": bridges(nodes, weight, group, plays, names), "total_artists": len(plays)}


def _genre_label(counters: list[Counter]) -> str:
    total: Counter = Counter()
    for c in counters:
        total.update(c)
    top = [g for g, _ in total.most_common(2)]
    return " / ".join(top) if top else "Mixed"


def member_key(people: list[str]) -> str:
    return hashlib.sha1("\n".join(sorted(people)).encode()).hexdigest()[:16]


def communities(nodes: list[str], weight: dict) -> dict[str, int]:
    """Greedy modularity (the local-moving phase of Louvain), in a fixed order so a page doesn't reshuffle between
    loads. Clusters are numbered by size, largest first."""
    adj: dict = {a: {} for a in nodes}
    for (a, b), w in weight.items():
        adj[a][b] = adj[a].get(b, 0) + w
        adj[b][a] = adj[b].get(a, 0) + w
    degree = {a: sum(adj[a].values()) for a in nodes}
    m2 = sum(degree.values())
    group = {a: i for i, a in enumerate(nodes)}
    if m2:
        total = {group[a]: degree[a] for a in nodes}
        for _ in range(20):
            moved = False
            for a in nodes:
                here = group[a]
                to: Counter = Counter()
                for b, w in adj[a].items():
                    to[group[b]] += w
                total[here] -= degree[a]
                best, gain = here, to[here] - total[here] * degree[a] / m2
                for g in sorted(to):
                    g_gain = to[g] - total[g] * degree[a] / m2
                    if g_gain > gain + 1e-12:
                        best, gain = g, g_gain
                total[best] = total.get(best, 0) + degree[a]
                if best != here:
                    group[a], moved = best, True
            if not moved:
                break
    size = Counter(group.values())
    order = {g: i for i, (g, _) in enumerate(sorted(size.items(), key=lambda kv: (-kv[1], kv[0])))}
    return {a: order[g] for a, g in group.items()}


def bridges(nodes, weight, group, plays, names, limit: int = 8) -> list[dict]:
    """Artists whose connections spread over several clusters, favouring ones played less than the clusters' stars."""
    reach: dict = defaultdict(Counter)
    for (a, b), w in weight.items():
        reach[a][group[b]] += w
        reach[b][group[a]] += w
    biggest = max(plays.values(), default=1) or 1
    out = []
    for a in nodes:
        r = reach[a]
        total = sum(r.values())
        if len(r) < 2 or total <= 0:
            continue
        mix = 1 - sum((w / total) ** 2 for w in r.values())
        outside = 1 - r[group[a]] / total
        if outside < 0.25:
            continue
        hidden = 1 - min(1, plays[a] / biggest)
        out.append({"id": a, "plays": plays[a], "score": round(mix * (0.5 + 0.5 * hidden), 3),
                    "links": [names[c] for c, _ in r.most_common(4)]})
    return sorted(out, key=lambda b: -b["score"])[:limit]


def report(store, days: int, now: float | None = None, ai: bool = False) -> dict:
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    rows = store.music_graph_plays()
    if days < 3650:
        rows = [r for r in rows if r["played_at"] >= now - days * 86400]
    similar = _load(store, META_SIMILAR, [])
    labels = _load(store, META_LABELS, {})
    g = build(rows, similar=similar, labels=labels)
    g["ai"] = {"asked": bool(similar), "links": len([s for s in similar if s["a"] in {n["id"] for n in g["nodes"]}])}
    return g


def _load(store, key: str, default):
    try:
        return json.loads(store.get_meta(key) or "") or default
    except ValueError:
        return default


# ---- Claude -----------------------------------------------------------------------------------------------

def can_ask(cfg: dict) -> bool:
    v = cfg.get("vision") or {}
    return v.get("provider") in ("claude", "anthropic") and bool(v.get("api_key"))


def _ask(cfg: dict, prompt: str, tool: str, schema: dict) -> dict:
    if not can_ask(cfg):
        raise GraphError("needs the Anthropic key in Settings → Vision (provider claude)")
    import anthropic
    v = cfg["vision"]
    client = anthropic.Anthropic(api_key=v["api_key"], max_retries=3, **({"base_url": v["base_url"]} if v.get("base_url") else {}))
    resp = client.messages.create(
        model=v["model"], max_tokens=8000, tools=[{"name": tool, "description": "Record the answer.", "input_schema": schema}],
        tool_choice={"type": "auto"}, messages=[{"role": "user", "content": f"{prompt}\n\nRecord your answer by calling {tool}."}])
    for block in resp.content:
        if block.type == "tool_use" and block.name == tool:
            return dict(block.input)
    raise GraphError("the model didn't answer")


SIMILAR_SCHEMA = {"type": "object", "required": ["links"], "properties": {"links": {"type": "array", "items": {
    "type": "object", "required": ["a", "b", "why"], "properties": {
        "a": {"type": "string"}, "b": {"type": "string"}, "why": {"type": "string", "description": "under 12 words"}}}}}}
LABEL_SCHEMA = {"type": "object", "required": ["labels"], "properties": {"labels": {"type": "array", "items": {
    "type": "object", "required": ["id", "label"], "properties": {
        "id": {"type": "string"}, "label": {"type": "string", "description": "2 to 4 words naming the shared sound or scene"}}}}}}


def ask_similar(cfg: dict, store, days: int = 3650) -> dict:
    """Claude links the artists that sound alike or share a scene, era or lineage, then names the clusters those links
    (and the rest) make. Only names from the list are kept. Kept in the database; returns the new graph."""
    first = report(store, days)
    names = [n["id"] for n in first["nodes"]]
    if len(names) < 3:
        raise GraphError("not enough artists yet")
    top = {n["id"]: n["genre"] or "" for n in first["nodes"]}
    listing = "\n".join(f"- {a}" + (f" ({g})" if g else "") for a, g in top.items())
    answer = _ask(cfg, "These are the artists one person listens to most. Link pairs that are closely related: same "
                  "scene, era, label, lineage, influence or sound, or members of the same group. Use each name exactly "
                  "as written, only names from the list, at most 4 links per artist and about two per artist overall "
                  "across the list; leave out loose matches. Never invent relationships.\n\n" + listing, "record_links", SIMILAR_SCHEMA)
    valid = set(names)
    seen: dict = {}
    for s in answer.get("links", []):
        a, b = s.get("a"), s.get("b")
        if a in valid and b in valid and a != b:
            seen[_pair(a, b)] = {"a": a, "b": b, "why": str(s.get("why") or "")[:100]}
    store.set_meta(META_SIMILAR, json.dumps(list(seen.values())))
    g = report(store, days)
    labels = _load(store, META_LABELS, {})
    groups = "\n".join(f"{c['key']}: {', '.join(c['members'])}" for c in g["clusters"] if c["size"] >= 2)
    if groups:
        named = _ask(cfg, "Each line is a cluster of artists from one person's listening: an id, then its members. Give "
                     "each a short name for what they share (a sound, scene or era), not a list of the artists.\n\n" + groups,
                     "record_labels", LABEL_SCHEMA)
        ids = {c["key"] for c in g["clusters"]}
        labels.update({x["id"]: str(x["label"])[:40] for x in named.get("labels", []) if x.get("id") in ids})
        store.set_meta(META_LABELS, json.dumps(labels))
    return report(store, days)
