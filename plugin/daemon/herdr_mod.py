"""The herdr app's spaces sidebar and agents list, nested like peerlink's spaces and leads.

The agents list (herdr's `agents` mod) nests a sub-agent under the terminal hc says started it, which is fixed when a
terminal starts. Its copy, `peerlink-agents`, takes the parent from peerlink first: an agent under its space's lead, a
worker under whoever hired it, so agents already running nest too. Both copies read the same tree file.

herdr's own `spaces` mod nests a worktree's space under its repo's and keeps the order it shows by moving spaces.
The daemon copies it, from the herdr app's own folder on this machine (none of herdr's code is in this repo), to
`peerlink-spaces`, and patches the copy so it also reads peerlink's space tree, a file the daemon writes, and nests
each space under its parent. herdr's sync of that order (space.move) does the rest. When herdr ships a new spaces mod
the copy is made again. A layout shows a mod by name, so the daemon points the layouts' spaces block at the copy
(and back at herdr's own if it ever can't make one); herdr's own spaces mod stays installed and unused.
"""

import json
import re

MOD = "peerlink-spaces"
AGENTS_MOD = "peerlink-agents"
TREE_FILE = "herdr-tree.json"  # in peerlink's home: {"parents": {herdr space id: its parent's}, "agents": {terminal: parent terminal}}

# The nesting, added to the cloned mod. `out` holds rows {w, i, g, tree} in order; a row with `tree` is a worktree
# under the row before it, and moves with it.
LOAD_JS = r'''
// peerlink: nest as peerlink does (the tree comes from the file the peerlink daemon writes).
let peerlinkTree = { parents: {}, agents: {} }, peerlinkRead = 0;
function peerlinkLoad() {
  if (Date.now() - peerlinkRead < 3000) return;
  peerlinkRead = Date.now();
  readTextFile(PEERLINK_TREE).then((t) => {
    try { const got = JSON.parse(t || "{}"); if (got && typeof got === "object") peerlinkTree = got; } catch (e) {}
  }, () => {});
}
'''

NEST_JS = LOAD_JS + r'''function peerlinkNest(out) {
  peerlinkLoad();
  const parents = peerlinkTree.parents || {};
  const groups = [];
  for (const r of out) (r.tree && groups.length ? groups[groups.length - 1] : groups[groups.push([]) - 1]).push(r);
  const at = new Map(groups.map((g) => [g[0].w.id, g]));
  const kids = new Map();
  const tops = [];
  for (const g of groups) {
    const p = parents[g[0].w.id];
    if (p && p !== g[0].w.id && at.has(p)) (kids.get(p) || kids.set(p, []).get(p)).push(g);
    else tops.push(g);
  }
  const res = [], seen = new Set();
  // lines: the tree lines in front of a nested space, as in the agents list ("├", "└", "│" or "" per level).
  const walk = (g, depth, lines = []) => {
    if (seen.has(g[0].w.id)) return;
    seen.add(g[0].w.id);
    res.push({ ...g[0], depth, lines }, ...g.slice(1).map((r) => ({ ...r, depth })));
    const ks = (kids.get(g[0].w.id) || []).filter((k) => !seen.has(k[0].w.id));
    const lead = lines.map((l, j) => (j < lines.length - 1 ? l : l === "├" ? "│" : ""));
    ks.forEach((k, j) => walk(k, depth + 1, [...lead, j === ks.length - 1 ? "└" : "├"]));
  };
  tops.forEach((g) => walk(g, 0));
  groups.forEach((g) => walk(g, 0));  // a loop in the tree: whatever wasn't reached
  return res;
}
'''

# (what to find in herdr's spaces mod, what it becomes). Each must be found exactly once, or the copy isn't made.
PATCHES = [
    ('import { spaces, machines, panes, run, openOverlay, exec, hc, theme, shared, setShared, watchFolder, window, layout } from "herdr";',
     'import { spaces, machines, panes, run, openOverlay, exec, hc, theme, shared, setShared, watchFolder, window, layout, readTextFile } from "herdr";'
     + NEST_JS),
    ("    const out = [];\n    const placed = new Set();", "    let out = [];\n    const placed = new Set();"),
    ("    // Simulate the batch so later indexes include earlier moves.", "    out = peerlinkNest(out);\n    // Simulate the batch so later indexes include earlier moves."),
    ("  row({ w, i, g, tree }, t) {", "  row({ w, i, g, tree, lines }, t) {"),
    # A nested space's line drops from its parent's icon, as in the agents list. The 18px icon's middle is 9 in; the
    # first column's line sits there, and each deeper one 17 further in, under the icon of the row above it (a column
    # is 9 wide, then the row's gap of 9). Rows are 30px with the 18px middle 6 down, so each piece spans exactly 30px,
    # from the bottom of the icon above (-12) to where the next row's piece starts (18): no two pieces overlap, as a
    # translucent line would show. "├" and "└" are straight lines with a tick off them; "└" stops at its tick.
    ("          .when(!tree, el => el.child(h_flex().size(18)",
     "          .when(!tree && Boolean(lines?.length), el => el.child(h_flex().flex_none().h(18).children(lines.map((l, j) =>"
     " div().relative().flex_none().w(9).h(18).ml(j ? 17 : 8)"
     ".when(l === \"│\" || l === \"├\", c => c.child(div().absolute().left(0).top(-12).h(30).border_l(1.5).border_color(t.input)))"
          ".when(l === \"└\", c => c.child(div().absolute().left(0).top(-12).h(21).border_l(1.5).border_color(t.input)))"
     ".when(l === \"├\" || l === \"└\", c => c.child(div().absolute().left(1).top(8.5).w(8).h(1).bg(t.input)))))))\n"
     "          .when(!tree, el => el.child(h_flex().size(18)"),
]


AGENTS_JS = LOAD_JS + r'''
function peerlinkParent(k) {
  peerlinkLoad();
  const p = (peerlinkTree.agents || {})[k.address];
  return p && p !== k.address ? p : k.parent;
}
'''

AGENTS_PATCHES = [
    ('import { agents, terminals, panes, focused, spaces, run, theme, shared, setShared, setAgentOrder, openOverlay, closeOverlay, props, window, layout } from "herdr";',
     'import { agents, terminals, panes, focused, spaces, run, theme, shared, setShared, setAgentOrder, openOverlay, closeOverlay, props, window, layout, readTextFile } from "herdr";'
     + AGENTS_JS),
    ("  tree(all, addr, lead = []) {\n", "  tree(all, addr, lead = []) {\n    if (lead.length > 6) return [];\n"),
    ('    const kids = all.filter((k) => k.parent === addr && k.state === "live");',
     '    const kids = all.filter((k) => peerlinkParent(k) === addr && k.state === "live");'),
    ("      .child(div().flex_1().truncate().text_color(on ? t.foreground : t.muted).child(k.name || k.template || k.program?.name || k.address))",
     "      .child(div().flex_1().truncate().text_color(on ? t.foreground : t.muted).child((peerlinkTree.names || {})[k.address] || k.name || k.template || k.program?.name || k.address))"),
    # A sub-agent's line drops from its parent's icon, not from its name: the first column sits under the lead's
    # 16px icon (row padding 8, so its middle is at 16), and each deeper column under the 14px icon of the row
    # above it (7 in from where that row's icon starts).
    ("      .pl(39)\n", "      .pl(15)\n"),
    ("      .children(s.lines.map(l => div().self_start().mt(-4).w(9).h(17).mr(4).flex_none()",
     "      .children(s.lines.map((l, j) => div().self_start().mt(-4).w(9).h(17).mr(4).flex_none().when(j > 0, el => el.ml(7))"),
]

# herdr's mod -> (peerlink's copy, the patches that make it).
MODS = {"spaces": (MOD, PATCHES), "agents": (AGENTS_MOD, AGENTS_PATCHES)}


def patch(src, tree_path, patches=PATCHES):
    """herdr's mod made into peerlink's, reading the tree at tree_path: (patched source, None), or (None, the first
    piece it couldn't find)."""
    for old, new in patches:
        if src.count(old) != 1:
            return None, old.strip().splitlines()[0][:80]
        src = src.replace(old, new)
    return src.replace("readTextFile(PEERLINK_TREE)", "readTextFile(" + json.dumps(tree_path) + ")"), None


def point_layout(text, to, base="spaces"):
    """A layout file's text with its `base` block (herdr's mod or peerlink's copy of it) showing mod `to`."""
    block = re.compile(r'("mod"\s*:\s*")(' + re.escape(base) + "|" + re.escape(MODS[base][0]) + r')(")')
    return block.sub(lambda m: m.group(1) + to + m.group(3), text)


def tree(smap, parents, agents=None, names=None):
    """The file the patched mods read: each herdr space's parent herdr space, from peerlink's space tree, and each
    agent's parent terminal. smap: {peerlink space: herdr space id}; parents: {peerlink space: the space it sits
    inside}; agents: {terminal address: parent terminal address}; names: {terminal address: its agent's name}, shown
    on a nested row in place of the terminal's own name."""
    out = {}
    for space, hid in sorted((smap or {}).items()):
        p = (parents or {}).get(space)
        if hid and p and smap.get(p) and smap[p] != hid:
            out[hid] = smap[p]
    return {"parents": out, "agents": {a: p for a, p in sorted((agents or {}).items()) if a and p and a != p},
            "names": {a: n for a, n in sorted((names or {}).items()) if a and n}}


def tree_text(smap, parents, agents=None, names=None):
    return json.dumps(tree(smap, parents, agents, names), sort_keys=True, indent=1) + "\n"
