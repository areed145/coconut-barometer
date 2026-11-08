"""Questions about your listening, answered by Claude over your own artist graph (#/music/explore, issue #204 step 2).

You ask "what connects my bluegrass and my jazz?" and Claude answers by calling tools over the explorer's whole graph
(music_explore.Whole): find artists by profile, an artist's details and links, a neighbourhood, the chain between two
artists, and what you played or found in a period. It ends by calling show_answer with a short answer and the artists
and links behind it, which the page lights up.

Only what's real is shown: the artists must be in the graph and each link must be one of its links, whatever Claude
says. A few thousand tokens a question, asked only when you ask, on the Anthropic connection Vision uses. The tool
results are your own library's data; the question is yours."""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict

from . import activity
from . import music_graph as G
from .artist_profiles import can_ask
from .music_explore import NotFound, Whole

MAX_TURNS = 8                     # requests in one answer, tools included
MAX_TOKENS = 4000
MAX_LIST = 40                     # artists or rows in one tool result
MAX_QUESTION = 500

SYSTEM = """You answer questions about one person's music listening, using only the tools here. They read the person's \
own artist graph: every artist they've played over {min_plays} times, each with a profile (scenes, genres, the decades \
they mattered most in, where they're from) and links between artists. Each link says why, strongest first: \
bandmates (one is in the other, a shared member, family, the same person), made music together (featured on each \
other's tracks, collaborated), worked on their records (produced, wrote for, played on, remixed), a collaborator they \
share (the same producer, songwriter or player), or alike on paper (a shared scene or rare genre). Today is {today}.

Look things up before answering; don't rely on what you know about the artists for anything the tools can tell you. \
When you're done, call show_answer exactly once: a short answer (under 120 words, plain sentences, no headings), the \
artists it's about, and the links that support it. Only include links a tool returned. If the tools can't answer the \
question, say so in show_answer rather than guessing."""

TOOLS = [
    {"name": "find_artists", "description": "Artists in the graph matching every filter given, most played first. "
     "Use for questions about a scene, genre, decade, country or a name.",
     "input_schema": {"type": "object", "properties": {
         "scene": {"type": "string"}, "genre": {"type": "string"}, "decade": {"type": "integer", "description": "e.g. 1970"},
         "country": {"type": "string", "description": "two-letter code"}, "name": {"type": "string", "description": "part of a name"},
         "limit": {"type": "integer"}}}},
    {"name": "artist_info", "description": "One artist: plays, when first and last played, plays by year, profile, and "
     "its strongest links with their reasons.",
     "input_schema": {"type": "object", "properties": {"artist": {"type": "string"}}, "required": ["artist"]}},
    {"name": "neighbourhood", "description": "An artist and its strongest links, one or two steps out, with the reason "
     "for each link.",
     "input_schema": {"type": "object", "properties": {"artist": {"type": "string"}, "depth": {"type": "integer", "enum": [1, 2]}},
                      "required": ["artist"]}},
    {"name": "connect", "description": "The strongest chain of links between two artists, with the reason for each step.",
     "input_schema": {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]}},
    {"name": "listening", "description": "What was played in a period, grouped by scene, genre, decade, country or "
     "artist, most plays first. new_only counts only artists first played in that period (what they got into).",
     "input_schema": {"type": "object", "properties": {
         "since": {"type": "string", "description": "YYYY-MM-DD, inclusive"}, "until": {"type": "string", "description": "YYYY-MM-DD, exclusive"},
         "group_by": {"type": "string", "enum": ["scene", "genre", "decade", "country", "artist"]},
         "new_only": {"type": "boolean"}}, "required": ["group_by"]}},
    {"name": "show_answer", "description": "The final answer, shown with the artists and links lit up on the map. Call once, last.",
     "input_schema": {"type": "object", "properties": {
         "text": {"type": "string"}, "artists": {"type": "array", "items": {"type": "string"}},
         "links": {"type": "array", "items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
                   "description": "pairs of artists, each a link a tool returned"}},
         "required": ["text", "artists", "links"]}},
]


class AskError(RuntimeError):
    """The question couldn't be answered (no key, a refusal, too long): the message is safe to show."""


def _link(e: dict) -> dict:
    return {"a": e["a"], "b": e["b"], "why": [r["said"] for r in e["rels"]]}


def _brief(n: dict) -> dict:
    p = n.get("profile") or {}
    return {"artist": n["id"], "plays": n["plays"], "scenes": p.get("scenes", []), "genres": p.get("genres", [])[:5],
            "era": p.get("era"), "from": ", ".join(x for x in (p.get("city"), p.get("country")) if x) or None}


def _day(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp())
    except ValueError:
        raise NotFound(f"{text} isn't a date (YYYY-MM-DD)") from None


class Tools:
    """What Claude can call, over one whole graph."""

    def __init__(self, whole: Whole):
        self.w = whole

    def run(self, name: str, args: dict) -> dict:
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            raise NotFound(f"no tool {name}")
        return fn(**{k: v for k, v in (args or {}).items() if v not in (None, "")})

    def t_find_artists(self, scene=None, genre=None, decade=None, country=None, name=None, limit=MAX_LIST):
        found = self.w.matches(scene=scene, genre=genre, decade=decade, country=country) if (scene or genre or decade or country) \
            else sorted(self.w.nodes, key=lambda a: -self.w.nodes[a]["plays"])
        if name:
            found = [a for a in found if name.casefold() in a.casefold()]
        return {"matched": len(found), "artists": [_brief(self.w.nodes[a]) for a in found[:max(1, min(MAX_LIST, int(limit)))]]}

    def t_artist_info(self, artist):
        a = self.w.artist(artist)
        when = [t for who, t in self.w.plays if who == a]
        years = Counter(dt.datetime.fromtimestamp(t, dt.timezone.utc).year for t in when)
        day = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat()   # noqa: E731
        return {**_brief(self.w.nodes[a]), "first_played": day(min(when)) if when else None,
                "last_played": day(max(when)) if when else None, "plays_by_year": dict(sorted(years.items())),
                "links": [{"artist": other, "why": _link(e)["why"]} for other, e in self.w.adj[a][:15]]}

    def t_neighbourhood(self, artist, depth=1):
        piece = self.w.neighbourhood(artist, depth=int(depth), size=MAX_LIST)
        return {"artists": [_brief(n) for n in piece["nodes"]], "links": [_link(e) for e in piece["edges"]]}

    def t_connect(self, a, b):
        p = self.w.path(a, b)
        return {"path": p["path"], "steps": [_link(e) for e in p["steps"]]}

    def t_listening(self, group_by, since=None, until=None, new_only=False):
        lo, hi = _day(since), _day(until)
        first: dict = {}
        for who, t in self.w.plays:
            first.setdefault(who, t)
        counts: Counter = Counter()
        artists: dict = defaultdict(Counter)
        for who, t in self.w.plays:
            if (lo is not None and t < lo) or (hi is not None and t >= hi) or who not in self.w.nodes:
                continue
            if new_only and not ((lo is None or first[who] >= lo) and (hi is None or first[who] < hi)):
                continue
            p = self.w.nodes[who].get("profile") or {}
            keys = {"artist": [who], "scene": p.get("scenes") or [], "genre": (p.get("genres") or [])[:3],
                    "country": [p["country"]] if p.get("country") else [],
                    "decade": list(range(p["span"][0] // 10 * 10, p["span"][1] + 1, 10)) if p.get("span") else []}[group_by]
            for k in keys:
                counts[k] += 1
                artists[k][who] += 1
        return {"group_by": group_by, "since": since, "until": until, "new_only": bool(new_only),
                "rows": [{"value": k, "plays": n, "top_artists": [a for a, _ in artists[k].most_common(5)]}
                         for k, n in counts.most_common(MAX_LIST)]}


def finish(whole: Whole, said: dict) -> dict:
    """What show_answer said, kept to what's real: artists in the graph, links that are links."""
    artists = []
    for name in said.get("artists") or []:
        try:
            artists.append(whole.artist(str(name)))
        except NotFound:
            continue
    links = []
    for pair in said.get("links") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            try:
                key = G._pair(whole.artist(str(pair[0])), whole.artist(str(pair[1])))
            except NotFound:
                continue
            if key in whole.edges and key not in links:
                links.append(key)
    for a, b in links:                                 # a link's ends are part of the answer
        artists += [x for x in (a, b) if x not in artists]
    return {"text": str(said.get("text") or "").strip(), "artists": list(dict.fromkeys(artists)),
            "links": [whole.edges[k] for k in links]}


def _client(cfg: dict):
    import anthropic

    v = cfg["vision"]
    return anthropic.Anthropic(api_key=v["api_key"], max_retries=2, **({"base_url": v["base_url"]} if v.get("base_url") else {}))


def ask(cfg: dict, whole: Whole, question: str, today: dt.date | None = None, create=None) -> dict:
    """One question, answered: {text, artists, links, tools (the calls made), input_tokens, output_tokens}.
    create: messages.create, or a stand-in for tests."""
    question = (question or "").strip()
    if not question:
        raise AskError("ask a question")
    if len(question) > MAX_QUESTION:
        raise AskError(f"keep the question under {MAX_QUESTION} characters")
    if create is None:
        if not can_ask(cfg):
            raise AskError("needs the Anthropic key in Settings → Vision (provider claude)")
        create = _client(cfg).messages.create
    from .artist_profiles import MIN_PLAYS

    system = SYSTEM.format(min_plays=MIN_PLAYS, today=(today or dt.date.today()).isoformat())
    tools = Tools(whole)
    messages: list = [{"role": "user", "content": question}]
    used, tin, tout = [], 0, 0
    for _ in range(MAX_TURNS):
        with activity.log.call("vision", "music question", question[:80]) as call:
            resp = create(model=cfg["vision"]["model"], max_tokens=MAX_TOKENS, system=system, tools=TOOLS, messages=messages)
            call["status"] = resp.stop_reason
        tin, tout = tin + resp.usage.input_tokens, tout + resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            raise AskError("Claude declined to answer that")
        if resp.stop_reason == "max_tokens":
            raise AskError("the answer ran too long: try a narrower question")
        messages.append({"role": "assistant", "content": resp.content})     # unchanged, thinking blocks and all
        calls = [b for b in resp.content if b.type == "tool_use"]
        final = next((b for b in calls if b.name == "show_answer"), None)
        if final is not None:
            return {**finish(whole, dict(final.input)), "tools": used, "input_tokens": tin, "output_tokens": tout}
        if not calls:                                  # it answered in plain text: show that, with nothing lit up
            text = " ".join(b.text for b in resp.content if b.type == "text").strip()
            return {"text": text, "artists": [], "links": [], "tools": used, "input_tokens": tin, "output_tokens": tout}
        results = []
        for b in calls:
            used.append({"tool": b.name, "input": dict(b.input)})
            try:
                out, err = tools.run(b.name, dict(b.input)), False
            except (NotFound, TypeError, ValueError) as e:
                out, err = {"error": str(e)}, True
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, ensure_ascii=False), "is_error": err})
        messages.append({"role": "user", "content": results})
    raise AskError("that took too many steps: try a narrower question")
