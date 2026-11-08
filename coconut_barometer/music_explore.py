"""The music explorer (#/music/explore, issue #204): the whole artist graph, built once and kept, and the questions
the page asks of it.

The Artist network draws the top few hundred artists of a range, built on every load. The explorer starts anywhere
instead: an artist, a scene, a genre, a decade or a country, and grows from there. So it needs every link between
every artist with over MIN_PLAYS plays (the ones artist_profiles.py profiles), which takes a second or two to build.
It's built on the first request and kept until plays, MusicBrainz's answers or the profiles change
(store.music_graph_version), and then built again in the background at most every REBUILD_EVERY seconds.

Each query returns a piece of the graph: its artists, and the links among them and to the artists the page already
shows (`have`), so a piece joins the map without loose ends."""
from __future__ import annotations

import heapq
import logging
import threading
import time
from collections import Counter, defaultdict

from . import music_graph as G
from .artist_profiles import MIN_PLAYS
from .memory import heavy

logger = logging.getLogger(__name__)

MAX_PIECE = 120                   # artists in one answer at most
MAX_HAVE = 600                    # artists the page can say it already shows
FACET_GENRES = 300
REBUILD_EVERY = 300               # seconds: the most often the whole graph is built again


class NotFound(LookupError):
    """An artist the explorer doesn't have (fewer plays, or a misspelling): the message is safe to show."""


class Whole:
    """The whole graph, indexed for the queries."""

    def __init__(self, g: dict, built: float, seconds: float, plays: list[tuple[str, int]] | None = None):
        """plays: (main artist, played_at) of every play, for questions about when (music_ask.py)."""
        self.built, self.seconds = built, seconds
        self.plays = plays or []
        self.nodes = {n["id"]: n for n in g["nodes"]}
        self.edges = {(e["a"], e["b"]): e for e in g["edges"]}
        self.clusters = g["clusters"]
        self.adj: dict = defaultdict(list)
        for (a, b), e in self.edges.items():
            self.adj[a].append((b, e))
            self.adj[b].append((a, e))
        for links in self.adj.values():
            links.sort(key=lambda le: (-le[1]["weight"], le[0]))

    def artist(self, name: str) -> str:
        if name in self.nodes:
            return name
        low = (name or "").strip().casefold()
        hit = next((a for a in self.nodes if a.casefold() == low), None)
        if not hit:
            raise NotFound(f"{name} isn't among the artists with over {MIN_PLAYS} plays")
        return hit

    def piece(self, ids, have=(), **extra) -> dict:
        """These artists, and every link among them and between them and `have`."""
        ids = list(dict.fromkeys(i for i in ids if i in self.nodes))
        mine = set(ids)
        around = mine | {h for h in list(have)[:MAX_HAVE] if h in self.nodes}
        edges = [e for i in ids for other, e in self.adj[i] if other in around and (other not in mine or i < other)]
        return {"nodes": [self.nodes[i] for i in ids], "edges": edges, "clusters": self.clusters, **extra}

    # ---- the queries ----------------------------------------------------------------------------------------
    def neighbourhood(self, artist: str, depth: int = 1, size: int = 40, have=()) -> dict:
        """An artist and its strongest links, then theirs (depth 2), up to `size` artists."""
        start = self.artist(artist)
        picked, frontier = [start], [start]
        for step in range(max(1, min(2, depth))):
            room = size - len(picked)
            if room <= 0:
                break
            each = room if step == 0 else max(3, room // max(1, len(frontier)))
            nxt = []
            for a in frontier:
                for other, _ in self.adj[a][:each]:
                    if other not in picked and len(picked) < size:
                        picked.append(other)
                        nxt.append(other)
            frontier = nxt
        return self.piece(picked, have, focus=start)

    def matches(self, scene=None, genre=None, decade=None, country=None) -> list[str]:
        """Artists whose profile fits every filter given, most played first."""
        def fits(n):
            p = n.get("profile") or {}
            if scene and scene.casefold() not in p.get("scenes", []):
                return False
            if genre and genre.casefold() not in p.get("genres", []):
                return False
            if decade is not None and not (p.get("span") and p["span"][0] <= int(decade) + 9 and p["span"][1] >= int(decade)):
                return False
            return not (country and (p.get("country") or "").upper() != country.upper())
        return [a for a, n in sorted(self.nodes.items(), key=lambda kv: (-kv[1]["plays"], kv[0])) if fits(n)]

    def filter(self, size: int = 60, have=(), **want) -> dict:
        found = self.matches(**want)
        return self.piece(found[:size], have, matched=len(found))

    def path(self, a: str, b: str, have=()) -> dict:
        """The strongest chain of links from a to b: each step costs less the stronger its link."""
        a, b = self.artist(a), self.artist(b)
        best, back, todo = {a: 0.0}, {}, [(0.0, a)]
        while todo:
            cost, here = heapq.heappop(todo)
            if here == b:
                break
            if cost > best.get(here, float("inf")):
                continue
            for other, e in self.adj[here]:
                c = cost + 1 / max(0.05, e["weight"])
                if c < best.get(other, float("inf")):
                    best[other], back[other] = c, here
                    heapq.heappush(todo, (c, other))
        if b != a and b not in back:
            raise NotFound(f"{a} and {b} aren't connected by any link")
        chain = [b]
        while chain[-1] != a:
            chain.append(back[chain[-1]])
        chain.reverse()
        steps = [self.edges[G._pair(x, y)] for x, y in zip(chain, chain[1:])]
        return {**self.piece(chain, have), "path": chain, "steps": steps}

    def facets(self) -> dict:
        """What the pickers offer: every artist, and the scenes, genres, decades and countries in use, with counts."""
        scenes, genres, decades, countries = Counter(), Counter(), Counter(), Counter()
        for n in self.nodes.values():
            p = n.get("profile") or {}
            scenes.update(p.get("scenes", []))
            genres.update(p.get("genres", []))
            if p.get("span"):
                decades.update(range(p["span"][0] // 10 * 10, p["span"][1] + 1, 10))
            if p.get("country"):
                countries[p["country"]] += 1
        return {"artists": [{"id": a, "plays": n["plays"]} for a, n in sorted(self.nodes.items(), key=lambda kv: (-kv[1]["plays"], kv[0]))],
                "scenes": scenes.most_common(), "genres": genres.most_common(FACET_GENRES),
                "decades": sorted(decades.items()), "countries": countries.most_common(),
                "edges": len(self.edges), "min_plays": MIN_PLAYS, "built": int(self.built), "seconds": round(self.seconds, 2)}


class Explorer:
    """The whole graph: built when first asked for, and again once what it's made from changes, but at most every
    REBUILD_EVERY seconds and in the background, the graph already built answering meanwhile.

    While MusicBrainz is filling in credits its answers change every second or so, and rebuilding on each change put a
    whole build (and its peak) under every request (#207). open_store: a new Store for the background build, closed
    after it; without one (the tests), a rebuild that's due happens in the request."""

    def __init__(self, clock=time.time, open_store=None):
        self._lock = threading.Lock()           # the state below, held only briefly
        self._build_lock = threading.Lock()     # one build at a time
        self._key = None
        self._whole: Whole | None = None
        self._tried = 0.0                       # when the last build started, done or not
        self._building = False
        self.clock = clock
        self.open_store = open_store

    def describe(self) -> str:
        """For the memory watchdog's reports."""
        w = self._whole
        held = f"{len(w.nodes):,} artists, {len(w.edges):,} links, {len(w.plays):,} plays held" if w else "nothing built"
        return held + (", rebuilding" if self._building else "")

    def whole(self, store) -> Whole:
        key = store.music_graph_version()
        with self._lock:
            w = self._whole
            if w is not None and (key == self._key or self._building or self.clock() - self._tried < REBUILD_EVERY):
                return w
            later = w is not None and self.open_store is not None
            if later:
                self._building, self._tried = True, self.clock()
        if later:
            threading.Thread(target=self._rebuild, name="music-explorer", daemon=True).start()
            return w
        return self._build(store, key)          # the first build, which the request waits for

    def _rebuild(self) -> None:
        try:
            store = self.open_store()
            try:
                self._build(store, store.music_graph_version())
            finally:
                store.close()
        except Exception:   # noqa: BLE001 - the graph already built carries on; the next due request tries again
            logger.exception("music explorer: rebuilding the whole graph failed")
        finally:
            self._building = False

    def _build(self, store, key) -> Whole:
        with self._build_lock:
            if self._whole is not None and self._key == key:   # built by the request ahead of this one
                return self._whole
            self._tried = self.clock()
            with heavy("music explorer graph"):
                started = time.perf_counter()
                rows = store.music_graph_plays()
                g = G.build(rows, limit=None, mb=store.mb_graph(), profiles=store.profiles(), now=self.clock(),
                            min_plays=MIN_PLAYS)
                known = {r["artist"] for r in rows}
                main = {a: G.credited(a, "", known)[0] for a in known}
                plays = [(main[r["artist"]], r["played_at"]) for r in rows]
                del rows
                whole = Whole(g, self.clock(), time.perf_counter() - started, plays)
            with self._lock:
                self._whole, self._key = whole, key
            return whole
