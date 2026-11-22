"""The herdr app's spaces sidebar, nested like peerlink's spaces.

herdr's own `spaces` mod nests a worktree's space under its repo's and keeps the order it shows by moving spaces.
peerlink makes a copy of it, `peerlink-spaces` (herdr's mod.clone, never a copy of its code in this repo), and patches
the copy so it also reads peerlink's space tree, a file the daemon writes, and nests each space under its parent.
herdr's sync of that order (space.move) does the rest. When herdr ships a new spaces mod the copy is made again.
"""

import json

MOD = "peerlink-spaces"
TREE_FILE = "herdr-tree.json"  # in peerlink's home: {"parents": {herdr space id: parent herdr space id}}

# The nesting, added to the cloned mod. `out` holds rows {w, i, g, tree} in order; a row with `tree` is a worktree
# under the row before it, and moves with it.
NEST_JS = r'''
// peerlink: nest spaces as peerlink nests its spaces (the tree comes from ~/.peerlink/herdr-tree.json).
let peerlinkTree = { parents: {} }, peerlinkRead = 0;
function peerlinkLoad() {
  if (Date.now() - peerlinkRead < 3000) return;
  peerlinkRead = Date.now();
  readTextFile(home() + "/.peerlink/herdr-tree.json").then((t) => {
    try { const got = JSON.parse(t || "{}"); if (got && typeof got.parents === "object") peerlinkTree = got; } catch (e) {}
  }, () => {});
}
function peerlinkNest(out) {
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
  const walk = (g, depth) => {
    if (seen.has(g[0].w.id)) return;
    seen.add(g[0].w.id);
    res.push({ ...g[0], depth }, ...g.slice(1).map((r) => ({ ...r, depth })));
    for (const k of kids.get(g[0].w.id) || []) walk(k, depth + 1);
  };
  tops.forEach((g) => walk(g, 0));
  groups.forEach((g) => walk(g, 0));  // a loop in the tree: whatever wasn't reached
  return res;
}
'''

# (what to find in herdr's spaces mod, what it becomes). Each must be found exactly once, or the copy isn't made.
PATCHES = [
    ('import { spaces, machines, panes, run, openOverlay, exec, hc, theme, shared, setShared, watchFolder, window, layout } from "herdr";',
     'import { spaces, machines, panes, run, openOverlay, exec, hc, theme, shared, setShared, watchFolder, window, layout, readTextFile, home } from "herdr";'
     + NEST_JS),
    ("    const out = [];\n    const placed = new Set();", "    let out = [];\n    const placed = new Set();"),
    ("    // Simulate the batch so later indexes include earlier moves.", "    out = peerlinkNest(out);\n    // Simulate the batch so later indexes include earlier moves."),
    ("  row({ w, i, g, tree }, t) {", "  row({ w, i, g, tree, depth }, t) {"),
    ("          .when(!tree, el => el.child(h_flex().size(18)",
     "          .when(!tree && Boolean(depth), el => el.pl(18 * depth))\n          .when(!tree, el => el.child(h_flex().size(18)"),
]


def patch(src):
    """herdr's spaces mod made into peerlink's: (patched source, None), or (None, the first piece it couldn't find)."""
    for old, new in PATCHES:
        if src.count(old) != 1:
            return None, old.strip().splitlines()[0][:80]
        src = src.replace(old, new)
    return src, None


def tree(smap, parents):
    """The file the patched mod reads: each herdr space's parent herdr space, from peerlink's space tree.
    smap: {peerlink space: herdr space id}; parents: {peerlink space: the space it sits inside}."""
    out = {}
    for space, hid in sorted((smap or {}).items()):
        p = (parents or {}).get(space)
        if hid and p and smap.get(p) and smap[p] != hid:
            out[hid] = smap[p]
    return {"parents": out}


def tree_text(smap, parents):
    return json.dumps(tree(smap, parents), sort_keys=True, indent=1) + "\n"
