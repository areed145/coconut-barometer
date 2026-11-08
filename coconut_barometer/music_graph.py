"""The artist network on the Music page (#/music): who you listen to, and how they connect.

Edges, each a different reason two artists are linked (playing two artists back to back isn't one: it says more
about a playlist or a shuffle than about the music):
  collab     named together: "A feat. B", "(with B)" in a song's title, or "A & B" when both are artists on their own
  credit     a composer in Apple's catalog who is also an artist you play, or who wrote for several of them
  musicbrainz  recording credits and band memberships from MusicBrainz (musicbrainz.py): one artist produced, wrote for,
             played on or featured on another's tracks, or is in their band; or two share a producer, writer, player or
             member. Artists are matched by MusicBrainz ID first (the main credit on their own tracks), by name only when
             exactly one drawn artist has it
  profile    alike on paper: each artist's profile (genres from Apple, MusicBrainz and Claude; Claude's scenes; their
             years; where they're from) compared here, with no model in the loop (profile_links). A rare genre counts
             for more than one everybody has, and each artist keeps only its closest few

Clusters are found by greedy modularity on the combined weights, and named by the scene or genres their members share.
Hidden bridges are the modestly played artists whose connections reach several clusters: the ones that tie your
listening together."""
from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
import math
import re
from array import array
from collections import Counter, defaultdict

import numpy as np

NODES = 100                      # the most played artists drawn
CREDIT_MAX_ARTISTS = 8           # a composer behind more artists than this ("Traditional") says nothing
WEIGHTS = {"collab": 1.0, "credit": 0.6, "musicbrainz": 0.9, "profile": 0.8}
MB_VERBS = {"featured": "featured on", "producer": "produced", "performer": "played on", "writer": "wrote for",
            "remixer": "remixed", "arranger": "arranged for"}
MAX_NODES = 200
GENERIC_CREDITS = {"traditional", "public domain", "various", "various artists", "unknown", "anonymous"}
# Profile matches: how much each part counts, how close is close enough, and how many each artist keeps.
PROFILE_PARTS = {"genre": 0.45, "scene": 0.45, "era": 0.15, "origin": 0.10}   # a scene alone links: lineage spans eras
PROFILE_MIN = 0.45               # the score a pair needs
PROFILE_MIN_GENRE = 0.3          # and genres at least this alike, or a scene in common: years and places alone aren't
PROFILE_PER_ARTIST = 4
SOURCE_WEIGHT = {"apple": 0.5, "musicbrainz": 1.0, "claude": 1.0}   # Apple's genres are broad ("Rock")
SAME_COUNTRY = 0.4               # of a same-city match
COMMON_GENRE = 0.25              # a genre more than this share of the artists have (and more than two) tells nothing

FEATURING = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with)\s+", re.I)
TITLE_FEAT = re.compile(r"[(\[]\s*(?:feat\.?|featuring|ft\.?|with)\s+([^)\]]+)[)\]]", re.I)
SPLIT = re.compile(r"\s*(?:,|&|\+)\s*|\s+(?:and|x)\s+", re.I)     # a lone "X" or "And" is a name, not a separator


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


def build(rows: list[dict], limit: int | None = NODES, mb: dict | None = None, profiles: dict | None = None,
          now: float | None = None, min_plays: int = 0) -> dict:
    """rows: music_graph_plays, in play order (artist, genres, composer, played_at, name, ms). mb: store.mb_graph().
    profiles: store.profiles(), what Claude said about each artist. limit: the most played drawn, or None for every
    artist with over min_plays plays (the explorer's whole graph, music_explore.py)."""
    known = {r["artist"] for r in rows}
    plays: Counter = Counter()
    minutes: Counter = Counter()
    genres: dict = defaultdict(Counter)
    collab: Counter = Counter()
    credit_artists: dict = defaultdict(Counter)       # composer -> artist -> tracks
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
    nodes = [a for a, n in plays.most_common(limit) if n > max(0, min_plays)]
    keep = set(nodes)
    edges: dict = defaultdict(lambda: {"kinds": {}, "weight": 0.0})

    def link(a, b, kind, strength, detail=None):
        if a == b or a not in keep or b not in keep:
            return
        e = edges[_pair(a, b)]
        e["kinds"][kind] = detail if detail is not None else round(strength, 3)
        e["weight"] += WEIGHTS[kind] * strength

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
    for (a, b), (strength, said) in musicbrainz_links(mb or {}, keep, known).items():
        link(a, b, "musicbrainz", strength, " · ".join(said[:3]))
    year = dt.datetime.fromtimestamp(now if now is not None else dt.datetime.now().timestamp(), dt.timezone.utc).year
    profile = artist_profiles(nodes, genres, mb or {}, profiles or {}, known, year)
    for (a, b), (score, why) in profile_links(profile).items():
        link(a, b, "profile", score, why)
    weight = {pair: e["weight"] for pair, e in edges.items()}
    group = communities(nodes, weight)
    members: dict = defaultdict(list)
    for a in nodes:
        members[group[a]].append(a)
    names = {cid: cluster_label(people, profile, genres) for cid, people in members.items()}
    degree: Counter = Counter()
    for a, b in weight:
        degree[a] += 1
        degree[b] += 1
    out_nodes = [{"id": a, "plays": plays[a], "minutes": round(minutes[a]), "cluster": group[a],
                  "genre": genres[a].most_common(1)[0][0] if genres[a] else None,
                  "degree": degree[a], "profile": summary(profile[a])} for a in nodes]
    return {"nodes": out_nodes,
            "edges": [{"a": a, "b": b, "kinds": e["kinds"], "weight": round(e["weight"], 3)} for (a, b), e in sorted(edges.items())],
            "clusters": [{"id": cid, "label": names[cid], "size": len(people), "members": people[:12],
                          "key": member_key(people)} for cid, people in sorted(members.items())],
            "bridges": bridges(nodes, weight, group, plays, names), "total_artists": len(plays)}


def musicbrainz_links(mb: dict, keep: set[str], known: set[str]) -> dict:
    """(a, b) -> (strength, what MusicBrainz says links them, the most tracks first), between drawn artists only."""
    from .musicbrainz import norm

    by_name: dict = defaultdict(set)
    for a in keep:
        by_name[norm(a)].add(a)
    rows = [{**c, "main": credited(c["artist"], "", known)[0]} for c in mb.get("credits") or []]
    node_of = mbid_nodes(rows, keep)
    has_id = defaultdict(set)
    for mbid, a in node_of.items():
        has_id[a].add(mbid)

    def node(mbid, name):
        if mbid in node_of:
            return node_of[mbid]
        hit = by_name.get(norm(name)) or set()
        if len(hit) != 1:
            return None
        a = next(iter(hit))
        return None if has_id[a] else a                  # same name, another MusicBrainz artist: someone else

    said: dict = defaultdict(Counter)                    # pair -> phrase -> tracks
    shared: dict = defaultdict(lambda: defaultdict(Counter))   # (mbid, name) not drawn -> drawn artist -> role -> tracks
    for c in rows:
        if c["role"] == "artist" or c["main"] not in keep:
            continue
        other = node(c["mbid"], c["name"])
        if other and other != c["main"]:
            said[_pair(c["main"], other)][f"{other} {MB_VERBS.get(c['role'], c['role'])} {c['main']}"] += c["tracks"]
        elif not other:
            shared[(c["mbid"], c["name"])][c["main"]][c["role"]] += c["tracks"]
    for r in mb.get("rels") or []:
        a, b = node(r["mbid"], r["name"]), node(r["other"], r["other_name"])
        if a and b and a != b:
            phrase = f"{a} is in {b}" if r["role"] == "in band" else f"{b} is in {a}" if r["role"] == "band of" else f"{a} and {b} collaborated"
            said[_pair(a, b)][phrase] += 1
        elif a and not b and r["role"] != "collaboration":
            shared[(r["other"], r["other_name"])][a]["member"] += 1
    out = {pair: (1.0, [p for p, _ in n.most_common()]) for pair, n in said.items()}
    for (mbid, name), by in shared.items():
        if not 2 <= len(by) <= CREDIT_MAX_ARTISTS or name.lower() in GENERIC_CREDITS:
            continue
        names = sorted(by)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                roles = sorted(set(by[a]) | set(by[b]))
                if _pair(a, b) not in out:
                    out[_pair(a, b)] = (0.7, [])
                out[_pair(a, b)][1].append(f"both credit {name} ({', '.join(roles)})")
    return out


def mbid_nodes(rows: list[dict], keep: set[str]) -> dict[str, str]:
    """MBID -> the drawn artist whose own tracks credit it first, by that name. rows: credits with their "main"."""
    from .musicbrainz import norm

    votes: dict = defaultdict(Counter)
    for c in rows:
        if c["role"] == "artist" and c["main"] in keep and norm(c["name"]) == norm(c["main"]):
            votes[c["mbid"]][c["main"]] += c["tracks"]
    return {mbid: v.most_common(1)[0][0] for mbid, v in votes.items()}


# ---- profiles ---------------------------------------------------------------------------------------------

def artist_profiles(nodes: list[str], genres: dict, mb: dict, said: dict, known: set[str], year: int) -> dict:
    """artist -> {genres: {label: weight}, scenes: set, span: (from, to) | None, city, country}, from what Apple's
    tracks, MusicBrainz and Claude say. Claude's decades place an artist best; MusicBrainz's years only for a group
    (a person's begin is their birth)."""
    keep = set(nodes)
    rows = [{**c, "main": credited(c["artist"], "", known)[0]} for c in mb.get("credits") or []]
    facts: dict = {}
    for mbid, a in mbid_nodes(rows, keep).items():
        if mbid in (mb.get("artists") or {}) and a not in facts:
            facts[a] = mb["artists"][mbid]
    out = {}
    for a in nodes:
        f, c = facts.get(a) or {}, said.get(a) or {}
        g: Counter = Counter()
        total = sum(genres[a].values()) or 1
        for label, n in genres[a].items():
            g[label.lower()] += SOURCE_WEIGHT["apple"] * n / total
        top = max((v for _, v in f.get("genres") or []), default=0)
        for label, votes in f.get("genres") or []:
            g[label] += SOURCE_WEIGHT["musicbrainz"] * votes / top
        for label in c.get("styles") or []:
            g[label] += SOURCE_WEIGHT["claude"]
        span = None
        if c.get("decades"):
            span = (min(c["decades"]), max(c["decades"]) + 9)
        elif f.get("type") == "Group" and f.get("begin_year"):
            span = (f["begin_year"], f.get("end_year") or year)
        out[a] = {"genres": dict(g), "scenes": set(c.get("scenes") or []), "span": span,
                  "city": f.get("area") if f.get("area") and f.get("country") else None,
                  "country": f.get("country") or c.get("country")}
    return out


def summary(p: dict) -> dict:
    """An artist's profile as the page shows it: scenes, genres (the strongest first), years, place."""
    return {"scenes": sorted(p["scenes"]), "genres": [g for g, _ in sorted(p["genres"].items(), key=lambda kv: (-kv[1], kv[0]))][:12],
            "span": list(p["span"]) if p["span"] else None, "era": _decades(*p["span"]) if p["span"] else None,
            "city": p["city"], "country": p["country"]}


def _decades(lo: int, hi: int) -> str:
    a, b = lo // 10 * 10, hi // 10 * 10
    return f"{a}s" if a == b else f"{a}s–{str(b)[2:]}s" if a // 100 == b // 100 else f"{a}s–{b}s"


def profile_links(profile: dict) -> dict:
    """(a, b) -> (score, why) for the pairs alike enough, each artist keeping its closest PROFILE_PER_ARTIST. Genres
    are compared by cosine, each weighted by how rare it is among the artists drawn, and one most of them share is
    left out: two artists known only as "Rock" aren't alike. The same goes for a scene most of them share.

    Only pairs sharing a genre that counts or a scene can link, and they're compared one artist at a time: holding
    every such pair at once took hundreds of MB with a few thousand artists (#207). The ones alike enough wait as
    numbers (8 bytes each part), and only the pairs kept are explained."""
    names = sorted(profile)
    n = len(names)
    common = max(2, COMMON_GENRE * n)
    df: Counter = Counter(label for a in names for label in profile[a]["genres"])
    idf = {label: 0.0 if k > common else math.log(n / k) for label, k in df.items()}
    vec = [{label: w * idf[label] for label, w in profile[a]["genres"].items() if idf[label] > 0} for a in names]
    size = [math.sqrt(sum(w * w for w in v.values())) for v in vec]
    scene_df: Counter = Counter(s for a in names for s in profile[a]["scenes"])
    scenes = [frozenset(s for s in profile[a]["scenes"] if scene_df[s] <= common) for a in names]
    holders: dict = defaultdict(list)                  # each list in name order, as names is
    for i in range(n):
        for label in vec[i]:
            holders[("genre", label)].append(i)
        for scene in scenes[i]:
            holders[("scene", scene)].append(i)

    def match(i: int, j: int, explain: bool = False):
        pa, pb = profile[names[i]], profile[names[j]]
        shared = vec[i].keys() & vec[j].keys()
        genre = sum(vec[i][x] * vec[j][x] for x in shared) / (size[i] * size[j]) if shared else 0.0
        both = scenes[i] & scenes[j]
        if genre < PROFILE_MIN_GENRE and not both:
            return None
        era, overlap = 0.0, None
        if pa["span"] and pb["span"]:
            lo, hi = max(pa["span"][0], pb["span"][0]), min(pa["span"][1], pb["span"][1])
            if hi >= lo:
                overlap = (lo, hi)
                era = min(1.0, (hi - lo + 1) / min(pa["span"][1] - pa["span"][0] + 1, pb["span"][1] - pb["span"][0] + 1))
        place = ""
        origin = 0.0
        if pa["city"] and pa["city"] == pb["city"]:
            origin, place = 1.0, pa["city"]
        elif pa["country"] and pa["country"] == pb["country"]:
            origin, place = SAME_COUNTRY, pa["country"]
        score = (PROFILE_PARTS["genre"] * genre + PROFILE_PARTS["scene"] * bool(both)
                 + PROFILE_PARTS["era"] * era + PROFILE_PARTS["origin"] * origin)
        if score < PROFILE_MIN:
            return None
        if not explain:
            return score
        said = sorted(both)
        top = sorted(shared, key=lambda x: -vec[i][x] * vec[j][x])[:2]
        why = said[:2] + [x for x in top if x not in said] + ([_decades(*overlap)] if overlap else []) + ([place] if place else [])
        return score, " · ".join(why)

    scores, left, right = array("d"), array("q"), array("q")
    for i in range(n):
        later: set = set()
        for key in [("genre", label) for label in vec[i]] + [("scene", s) for s in scenes[i]]:
            group = holders[key]
            later.update(group[bisect.bisect_right(group, i):])
        for j in later:
            score = match(i, j)
            if score is not None:
                scores.append(score)
                left.append(i)
                right.append(j)
    kept: Counter = Counter()
    out = {}
    if not scores:
        return out
    # Strongest first (then by name, as names is sorted), each pair kept while both ends have room.
    order = np.lexsort((np.frombuffer(right, dtype=np.int64), np.frombuffer(left, dtype=np.int64),
                        -np.frombuffer(scores, dtype=np.float64)))
    full = n * PROFILE_PER_ARTIST // 2                 # the most pairs that can be kept: no need to look further
    for start in range(0, len(order), 65536):          # a slice at a time as Python ints, not millions at once
        for k in order[start:start + 65536].tolist():
            i, j = left[k], right[k]
            if kept[i] < PROFILE_PER_ARTIST and kept[j] < PROFILE_PER_ARTIST:
                kept[i] += 1
                kept[j] += 1
                score, why = match(i, j, explain=True)
                out[(names[i], names[j])] = (round(score, 3), why)
        if len(out) >= full:
            break
    return out


def cluster_label(people: list[str], profile: dict, genres: dict) -> str:
    """A scene most of the cluster shares, else the genres they have most of (Apple's when there's nothing else)."""
    scenes = Counter(s for p in people for s in profile[p]["scenes"])
    if scenes:
        scene, k = scenes.most_common(1)[0]
        if k >= 2 and k >= 0.4 * len(people):
            return scene.title()
    specific: Counter = Counter()
    for p in people:
        apple = {g.lower() for g in genres[p]}
        specific.update({g: w for g, w in profile[p]["genres"].items() if g not in apple})
    if len(people) > 1 and specific:
        return " / ".join(g.title() for g, _ in specific.most_common(2))
    return _genre_label([genres[p] for p in people])


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


def report(store, days: int, now: float | None = None, limit: int = NODES) -> dict:
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    rows = store.music_graph_plays()
    if days < 3650:
        rows = [r for r in rows if r["played_at"] >= now - days * 86400]
    return build(rows, limit=max(10, min(MAX_NODES, limit)), mb=store.mb_graph(), profiles=store.profiles(), now=now)
