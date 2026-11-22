"""herdr sync: the herdr app's spaces mirror peerlink spaces, one tab per peerlink agent.

peerlink is the source of truth. This module only plans: given the live agents, hc's terminals and their labels, the
herdr app's layout (when it's open) and the remembered peerlink space -> herdr space ids, it says what to change.
The daemon gathers the inputs and carries out the actions. herdr shows any hc terminal labelled
herdr.space=<id> / herdr.tab=<id> in that space and tab, and relabelling a live terminal moves it there (an id the
app doesn't know yet becomes a new space or tab), so every move is a relabel.

Nothing is moved the other way, with one exception (joins): agents can run hc and herdr-app themselves, so a herdr
layout is never taken as a reason to change a placed agent's peerlink space. A terminal relabelled by hand is put back
on the next pass. The exception is a Claude Code tab you open yourself in a herdr space that mirrors a peerlink space:
on the agent's first sighting, a new agent nobody placed joins that space.
"""

import re


def hc_address(terminal_key):
    """The hc terminal address in a terminal key ('hc:t7@123' -> 't7'), '' for tmux, tty or none."""
    m = re.match(r"hc:([^@\s]+)", terminal_key or "")
    return m.group(1) if m else ""


def key_pid(terminal_key):
    """The pid of the terminal's top shell in a terminal key ('hc:t7@123' -> 123), 0 when it has none."""
    m = re.search(r"@(\d+)$", terminal_key or "")
    return int(m.group(1)) if m else 0


def plan(agents, terms, layout, smap, new_id, keep=()):
    """What to change so the herdr app shows the peerlink grouping.

    agents: [{"agent", "name", "space", "lead", "address"}], live peerlink agents running in hc terminals.
    terms:  {address: {"space": herdr.space label, "tab": herdr.tab label, "agent": peerlink.agent label,
             "pspace": peerlink.space label, "parent": started by another terminal}} for every live hc terminal.
             A terminal with a parent (a worker, nested under its lead) is never moved: herdr shows it with its parent.
    layout: the herdr app's spaces, [{"id", "name", "tabs": [{"id", "name", "terminals": [address]}]}], or None
            when the app isn't running (then nothing is renamed or reordered).
    smap:   {peerlink space: herdr space id} remembered from earlier passes.
    new_id: a function (prefix) -> a fresh id, like 'sp' + 32 hex.
    keep:   addresses whose peerlink labels stay though no agent shows there now (it may only be slow to come back).

    Returns (actions, smap, notes). Actions, in order:
      ("label", address, {key: value})      set hc labels (a herdr.space/herdr.tab change moves the terminal)
      ("rename_space", herdr id, name)
      ("rename_tab", herdr tab id, name)  an empty name gives the tab back herdr's number
      ("first_tab", herdr tab id)           move a tab to the front of its space
    notes: lines for people, about what was left alone and why.
    """
    actions, notes = [], []
    smap = dict(smap or {})
    by_space = {}
    for a in agents:
        by_space.setdefault(a["space"], []).append(a)
    agent_at = {a["address"]: a for a in agents}

    # Which herdr spaces exist: the app's when it's open, else those hc's labels name.
    if layout is not None:
        herdr_names = {s["id"]: s.get("name") or "" for s in layout}
    else:
        herdr_names = {t["space"]: "" for t in terms.values() if t.get("space")}
    holds = {}  # herdr space id -> addresses in it
    for addr, t in terms.items():
        if t.get("space"):
            holds.setdefault(t["space"], []).append(addr)

    # 1. Each peerlink space with live agents gets a herdr space: the one remembered, else one already named after
    #    it, else the herdr space holding most of its agents (its lead breaks a tie), else a new one.
    claimed = {}
    # A herdr space remembered for a peerlink space with nobody live right now stays that space's.
    for space, hid in smap.items():
        if space not in by_space and hid:
            claimed.setdefault(hid, space)
    for space in sorted(by_space):
        hid = smap.get(space)
        if hid and (layout is None or hid in herdr_names) and hid not in claimed:
            claimed[hid] = space
    for space in sorted(by_space):
        if smap.get(space) in claimed and claimed[smap[space]] == space:
            continue
        hid = next((h for h, n in sorted(herdr_names.items()) if n.lower() == space.lower() and h not in claimed), "")
        if hid:
            claimed[hid] = space
            smap[space] = hid
    scores = []
    for space in sorted(by_space):
        if smap.get(space) in claimed and claimed[smap[space]] == space:
            continue
        for hid, addrs in holds.items():
            mine = [agent_at[x] for x in addrs if x in agent_at and agent_at[x]["space"] == space]
            if mine:
                scores.append((-len(mine), -any(a["lead"] for a in mine), space, hid))
    for _, _, space, hid in sorted(scores):
        if hid in claimed or (smap.get(space) in claimed and claimed[smap[space]] == space):
            continue
        claimed[hid] = space
        smap[space] = hid
    for space in sorted(by_space):
        if not (smap.get(space) in claimed and claimed[smap[space]] == space):
            smap[space] = new_id("sp")
            claimed[smap[space]] = space

    # 2. Move agents whose terminal sits in another herdr space. A tab moves whole, and only when everything in it
    #    is an agent of the same peerlink space; otherwise it's left where it is and said so.
    tabs = {}
    for addr, t in terms.items():
        if t.get("tab"):
            tabs.setdefault(t["tab"], []).append(addr)
    moved_tabs = {}
    # Where the app shows a terminal wins over its labels: it doesn't move one it shows when only the labels change.
    shown_in = {x: s["id"] for s in layout or [] for tb in s.get("tabs") or [] for x in tb.get("terminals") or []}
    for a in sorted(agents, key=lambda a: (a["space"], not a["lead"], a["name"].lower())):
        addr, target = a["address"], smap[a["space"]]
        t = terms.get(addr) or {}
        labels = {}
        if t.get("parent"):
            pass
        elif shown_in.get(addr, t.get("space")) != target or not t.get("tab"):
            mates = tabs.get(t.get("tab") or "", [addr]) if t.get("tab") else [addr]
            others = [x for x in mates if x != addr]
            mixed = [x for x in others if x not in agent_at or agent_at[x]["space"] != a["space"]]
            if mixed:
                what = ", ".join(sorted((agent_at[x]["name"] if x in agent_at else x) for x in mixed))
                notes.append(f"{a['name']} stays put: its tab also holds {what}")
            else:
                key = t.get("tab") or addr
                moved_tabs.setdefault(key, new_id("tb"))
                labels.update({"herdr.space": target, "herdr.tab": moved_tabs[key]})
        if t.get("agent") != a["agent"]:
            labels["peerlink.agent"] = a["agent"]
        if t.get("pspace") != a["space"]:
            labels["peerlink.space"] = a["space"]
        if labels:
            actions.append(("label", addr, labels))
    # A terminal no agent runs in any more (it quit, or moved to another pane) gives up peerlink's labels; its tab gives
    # up the agent's name, back to herdr's number, so it isn't taken for the agent (and a new agent there isn't
    # taken to be named after it). Only once: with the labels gone it's an ordinary terminal.
    stale = set()
    for addr, t in sorted(terms.items()):
        if addr not in agent_at and addr not in keep and (t.get("agent") or t.get("pspace")):
            actions.append(("label", addr, {k: "" for k, v in (("peerlink.agent", t.get("agent")), ("peerlink.space", t.get("pspace"))) if v}))
            stale.add(addr)

    if layout is None:
        return actions, smap, notes
    for s in layout:
        for tab in s.get("tabs") or []:
            addrs = tab.get("terminals") or []
            name = (tab.get("name") or "").strip()
            if len(addrs) == 1 and addrs[0] in stale and name and not name.isdigit():
                actions.append(("rename_tab", tab["id"], ""))

    # 3. With the app open: herdr spaces named after their peerlink space, tabs after their agent, the lead's tab first.
    #    Only tabs holding one agent are renamed or moved; a tab just moved shows up on the next pass.
    for hid, space in sorted(claimed.items(), key=lambda kv: kv[1]):
        if hid in herdr_names and herdr_names[hid] != space:
            actions.append(("rename_space", hid, space))
    for s in layout:
        space = claimed.get(s["id"])
        if not space:
            continue
        for i, tab in enumerate(s.get("tabs") or []):
            addrs = tab.get("terminals") or []
            if len(addrs) != 1 or addrs[0] not in agent_at:
                continue
            a = agent_at[addrs[0]]
            if a["space"] != space or (terms.get(a["address"]) or {}).get("space") != s["id"]:
                continue
            if tab.get("name") != a["name"]:
                actions.append(("rename_tab", tab["id"], a["name"]))
            if a["lead"] and i != 0:
                actions.append(("first_tab", tab["id"]))
    return actions, smap, notes


JOIN_FRESH_S = 600  # only an agent made this recently can join by where its tab is


def joins(agents, terms, layout, smap, now, fresh_s=JOIN_FRESH_S, closed=()):
    """Agents to put in the peerlink space their herdr space mirrors, on their first sighting.

    agents: as for plan, plus "created" (when the agent was first made), "explicit" (its space was chosen for it: set
            on the agent, or the space a spawn started it for) and "seen" (an earlier pass already judged it).
    terms:  as for plan, plus "parent" (another terminal started it) and "spawned" (peerlink started it).
    closed: peerlink spaces nobody joins this way (placing an agent there stays yours to do).

    Returns (joins, seen): joins is [(address, peerlink space)], seen the agent ids judged on this pass, whether they
    joined or not, so a later move in herdr never changes their space. Only where the app shows a terminal counts, not
    its labels, which any process can set; without the app open nobody is judged yet.
    """
    if layout is None:
        return [], []
    shown_in = {x: s["id"] for s in layout for tb in s.get("tabs") or [] for x in tb.get("terminals") or []}
    mirrors = {}
    for space, hid in sorted((smap or {}).items()):
        mirrors.setdefault(hid, space)
    out, seen = [], []
    for a in sorted(agents, key=lambda a: a["address"]):
        if a.get("seen") or a["address"] not in shown_in:
            continue
        seen.append(a["agent"])
        t = terms.get(a["address"]) or {}
        if a.get("explicit") or t.get("parent") or t.get("spawned") or now - (a.get("created") or 0) > fresh_s:
            continue
        space = mirrors.get(shown_in[a["address"]])
        if space and space != a["space"] and space not in closed:
            out.append((a["address"], space))
    return out, seen


def tab_names(agents, terms, layout, now, known=None, fresh_s=JOIN_FRESH_S):
    """Agents that take the name you gave their tab: [(address, name)].

    herdr tabs have no name until someone gives one, and the plan later names a tab after its agent. So a new agent
    shown alone in a tab with a name of its own (not its number, not the agent's name already) was opened there by
    you, under that name. Same gate as joins: not seen before, made in the last fresh_s, not started by peerlink.

    After that, known ({tab id: the name the tab was last left showing}) tells a rename apart (never for a space's
    lead, whose name stays as it is): a tab whose name
    changed since is one you renamed, and its agent takes the name; an agent that renamed itself leaves its tab's
    name as it was, and the plan renames the tab instead.
    """
    known = known or {}
    if layout is None:
        return []
    out = []
    by_addr = {a["address"]: a for a in agents}
    for s in layout:
        for tb in s.get("tabs") or []:
            addrs = tb.get("terminals") or []
            name = (tb.get("name") or "").strip()
            if len(addrs) != 1 or addrs[0] not in by_addr or not name or name.isdigit():
                continue
            a, t = by_addr[addrs[0]], terms.get(addrs[0]) or {}
            if name.lower() == a["name"].lower():
                continue
            # A space's lead keeps its name: other agents and people reach the space through it.
            renamed = tb.get("id") in known and known[tb["id"]] != name and not a.get("lead")
            first = not (a.get("seen") or t.get("spawned") or now - (a.get("created") or 0) > fresh_s)
            if renamed or first:
                out.append((a["address"], name))
    return sorted(out)


def tab_memory(agents, layout, failed_renames=()):
    """What each single-agent tab is left showing after a pass: its agent's name (the plan renames the tab to it),
    or, where that rename failed, the name it still has. {tab id: name}."""
    names = {a["address"]: a["name"] for a in agents}
    out = {}
    for s in layout or []:
        for tb in s.get("tabs") or []:
            addrs = tb.get("terminals") or []
            if len(addrs) == 1 and addrs[0] in names and tb.get("id"):
                out[tb["id"]] = (tb.get("name") or "") if tb["id"] in failed_renames else names[addrs[0]]
    return out


def describe(action, who=None, smap=None):
    """One line for people about an action. who: {address: agent name}; smap: {peerlink space: herdr space id}."""
    kind, who = action[0], who or {}
    space_of = {h: s for s, h in (smap or {}).items()}
    if kind == "label":
        labels, name = action[2], who.get(action[1]) or action[1]
        if "herdr.space" in labels:
            return f"move {name} ({action[1]}) to herdr space {space_of.get(labels['herdr.space'], labels['herdr.space'])}"
        if not any(labels.values()):
            return f"take peerlink's labels off {name} ({action[1]}): no agent runs there now"
        return f"label {name} ({action[1]}): " + ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
    if kind == "rename_space":
        return f"rename herdr space {action[1]} to {action[2]}"
    if kind == "rename_tab":
        return f"rename tab {action[1]} to {action[2]}" if action[2] else f"give tab {action[1]} back herdr's number"
    if kind == "first_tab":
        return f"put tab {action[1]} first in its space"
    return repr(action)
