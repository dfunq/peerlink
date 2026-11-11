"""The web UI: one page, served by peerlinkd at /. Its script talks to the daemon's /api, as the plugin does.
Kept beside the daemon so the daemon's own file is the daemon; it is part of the daemon's fingerprint."""

UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Peerlink</title><link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23c96442'/><path d='M7 11.5h17M19.5 7l4.5 4.5-4.5 4.5M25 20.5H8M12.5 16 8 20.5l4.5 4.5' fill='none' stroke='%23fff' stroke-width='2.8' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
:root{color-scheme:light dark;--bg:#faf9f7;--side:#f2f0ec;--fg:#1f1e1c;--mut:#6b6862;--line:#e4e1db;--card:#fff;--acc:#c96442;--ok:#2f7d4f;--warn:#b7791f;--bad:#c0392b;--well:rgba(127,127,127,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#1c1b19;--side:#161513;--fg:#ecebe8;--mut:#9b978f;--line:#34322e;--card:#252421;--acc:#e0805e;--ok:#5cb57f;--warn:#e0b04a;--bad:#e5675a;--well:rgba(255,255,255,.03)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif}
.app{display:flex;min-height:100vh}
nav{flex:0 0 210px;background:var(--side);border-right:1px solid var(--line);padding:18px 10px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:2px}
nav .brand{font-weight:700;font-size:16px;padding:0 10px 4px;display:flex;align-items:center;gap:8px}nav .brand .mark{width:20px;height:20px;flex:none}nav .host{font-size:11px;color:var(--mut);padding:0 10px 14px}
nav a{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;border-radius:6px;color:var(--fg);text-decoration:none}
nav a:hover{background:var(--line)}nav a.on{background:var(--card);box-shadow:0 0 0 1px var(--line);font-weight:600}
nav .count{font-size:11px;color:var(--mut);font-weight:400}nav .grp{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);padding:14px 10px 4px}
main{flex:1;min-width:0;max-width:1100px;padding:22px 24px 60px}
body.wide main{max-width:none}
#peers.charts{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}#peers.charts>.chart{margin:0;max-width:100%}
.ltree{display:flex;flex-direction:column;gap:14px}
.lspace-h{display:flex;gap:8px;align-items:baseline;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin-bottom:6px}.lspace-h .small{text-transform:none;letter-spacing:0;font-weight:400}
.lsub{margin-left:14px;border-left:2px solid var(--line);padding-left:12px;display:flex;flex-direction:column;gap:6px;margin-top:6px}.lsub.led{border-left-color:var(--acc)}
.lsub>.lspace,.lflat>.lspace{margin-top:8px}.lflat{display:flex;flex-direction:column;gap:6px}
.lrow{display:grid;grid-template-columns:minmax(150px,230px) minmax(0,1fr) auto;gap:4px 16px;align-items:center;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 12px;text-align:left;font:inherit;color:inherit;cursor:pointer}
.lrow:hover{border-color:var(--mut)}.lrow.is-lead{border-color:var(--acc)}.lrow.sel{box-shadow:0 0 0 2px var(--acc)}.lrow.off{opacity:.5;cursor:default;border-style:dashed}
.lrow .ln{font-weight:600;display:flex;align-items:center;gap:6px;min-width:0}.lrow .ln>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lrow .lid{display:flex;flex-direction:column;gap:1px;min-width:0}.lrow .lwhere{font-size:11px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:14px}
.lrow .lw{font-size:13px;color:var(--fg);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.lrow .lw.old{color:var(--mut)}
.lrow .lbusy{color:var(--warn)}.pill.acc{color:var(--acc);border-color:var(--acc)}
.panel .phead{display:flex;align-items:center;gap:10px;min-width:0}.panel .phead .plogo{flex:none;width:36px;height:36px;border-radius:10px;display:grid;place-items:center}
.panel .phead .ptitle{display:flex;flex-direction:column;min-width:0}.panel .phead .psub{font-size:12px;color:var(--mut);display:flex;align-items:center;gap:5px;flex-wrap:wrap}.panel .phead .psub .dot{margin:0}
.panel .pwhere{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:12px;color:var(--mut);min-width:0}.panel .pwhere .mono{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.panel .pnow{margin-top:10px;background:var(--well);border:1px solid var(--line);border-radius:8px;padding:8px 10px;line-height:1.45}.panel .pnow .k{margin-bottom:2px}
.panel .ptiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}.panel .ptiles>div{border:1px solid var(--line);border-radius:8px;padding:6px 10px;display:flex;flex-direction:column;gap:3px}
.panel .ptiles b{font-size:16px}.panel .ptiles .cmeter{width:100%}
.cmeter{display:inline-block;width:34px;height:5px;border-radius:3px;background:var(--line);overflow:hidden;vertical-align:middle}.cmeter i{display:block;height:100%;background:var(--ok)}.cmeter.mid i{background:var(--warn)}.cmeter.hi i{background:var(--bad)}
.lrow .lr{display:flex;gap:10px;align-items:center;white-space:nowrap;font-size:12px;justify-content:flex-end}
.lrow .badge{font-size:10px;font-weight:600;color:var(--acc);border:1px solid var(--acc);border-radius:4px;padding:0 4px}.lrow .badge.only{border-style:dashed}
@media (max-width:720px){.lrow{grid-template-columns:1fr auto}.lrow .lw{grid-column:1/-1;grid-row:2;white-space:normal}}
section{display:none}section.on{display:block}
h1{font-size:20px;margin:0 0 4px}.lead{color:var(--mut);margin:0 0 18px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:22px 0 10px}
.sub{margin-left:12px;padding-left:16px;border-left:2px solid var(--line)}.sub h2::before{content:"↳ ";color:var(--acc)}
.card.is-lead{border-color:var(--acc)}
.chart{overflow-x:auto;padding:4px 0 12px;margin-bottom:8px}
.chart+.chart{margin-top:6px}.sgrid{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}.sgrid .chart+.chart{margin-top:0}
.unit{display:inline-flex;flex-direction:column;align-items:center;border:1px dashed var(--line);border-radius:12px;padding:10px 12px 12px;min-width:0}
.unit-h{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin-bottom:10px;white-space:nowrap}
.orow{display:flex;gap:12px;align-items:flex-start;justify-content:safe center}
.ocol{display:flex;flex-direction:column;align-items:center}.orow.stack{flex-direction:column;gap:8px}
.down{width:2px;height:14px;background:var(--acc);flex:none}
.node{width:200px;height:136px;box-sizing:border-box;display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;cursor:pointer;text-align:left;font:inherit;color:inherit;overflow:hidden}
.node .state{margin-top:auto!important}
.node:hover{border-color:var(--mut)}.node.is-lead{border-color:var(--acc)}.node.sel{box-shadow:0 0 0 2px var(--acc)}
.node .nm{font-weight:600;display:flex;align-items:center;gap:4px 6px;justify-content:space-between;flex-wrap:wrap}.node .nm>span:first-child{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node .tag{font-size:11px;font-weight:500;color:var(--acc);white-space:nowrap}
.node .badge{display:inline-block;font-size:10px;font-weight:600;color:var(--acc);border:1px solid var(--acc);border-radius:4px;padding:0 4px;white-space:nowrap}
.node .nm .rt{display:inline-flex;align-items:center;gap:4px;flex-wrap:wrap}.node .badge.only{border-style:dashed}
.lrow .badge.guest,.node .badge.guest{color:var(--mut);border-color:var(--mut);font-weight:500}
.node.off{opacity:.5;cursor:default;border-style:dashed}.node.off:hover{border-color:var(--acc)}
.under{margin-left:14px;border-left:2px solid var(--acc);padding-left:12px;margin-top:8px}
.node .dir{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--mut);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
details.plat{margin:0 0 12px;max-width:860px;background:var(--card);border:1px solid var(--line);border-radius:12px}
#schedlist details.plat,#inboxlist details.plat,#fblist details.plat{max-width:none;margin:0}#schedadd{max-width:none}.grpbody{display:flex;flex-direction:column;gap:8px;padding-top:12px!important}
.plat.static .psum{display:flex;align-items:center;gap:12px;padding:12px 14px;font-size:14px}.plat .pact{flex:none;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
div.plat{margin:0 0 12px;max-width:860px;background:var(--card);border:1px solid var(--line);border-radius:12px}
#cfg details.plat .pbody{padding-top:12px;display:flex;flex-direction:column;gap:10px}#cfg .savebar{max-width:860px}
@media (max-width:640px){.plat.static .psum{flex-wrap:wrap}.plat .pact{width:100%;justify-content:flex-start}}
details.plat>summary{display:flex;align-items:center;gap:12px;padding:12px 14px;list-style:none;font-size:14px;color:var(--fg)}details.plat>summary::-webkit-details-marker{display:none}
.plat .plogo{flex:none;width:34px;height:34px;border-radius:9px;display:grid;place-items:center}.plat .plogo svg{width:20px;height:20px}
.plat .ptitle{display:flex;flex-direction:column;min-width:0;flex:1}.plat .psub{font-size:12px;color:var(--mut)}
details.plat .pstate{display:flex;align-items:center;gap:6px;font-size:13px;flex-wrap:wrap;justify-content:flex-end}details.plat .pstate .dstate{margin:0}
details.plat .chev{flex:none;width:8px;height:8px;border-right:2px solid var(--mut);border-bottom:2px solid var(--mut);transform:rotate(-45deg);transition:transform .15s;margin:0 4px}
details.plat[open] .chev{transform:rotate(45deg)}details.plat .pbody{padding:0 14px 14px;border-top:1px solid var(--line)}details.plat .pbody .steps .step{background:var(--bg)}
.mlogo{flex:none;width:22px;height:22px;border-radius:6px;display:grid;place-items:center}.mlogo svg{width:14px;height:14px}
details.conv{max-width:none;margin:0}details.conv .cnames{display:flex;align-items:center;gap:6px;flex-wrap:wrap}details.conv .psub{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
details.conv .pstate{flex-wrap:nowrap}.convbody{display:flex;flex-direction:column;gap:8px;padding-top:12px!important;max-height:70vh;overflow:auto}
.cline{border-left:3px solid var(--acc);padding:4px 0 4px 10px}.cline.other{border-left-color:var(--line)}.cline .mh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px}
.cline .mh .when{margin-left:auto;color:var(--mut);font-size:12px}.cline .mt{margin-top:4px;overflow-wrap:anywhere;max-height:7.4em;overflow:hidden;cursor:pointer;-webkit-mask-image:linear-gradient(#000 70%,transparent);mask-image:linear-gradient(#000 70%,transparent)}.cline.open .mt{max-height:none;cursor:auto;-webkit-mask-image:none;mask-image:none}
.md{overflow-wrap:anywhere;line-height:1.45}.md p{margin:0 0 6px}.md>:last-child{margin-bottom:0}.md ul,.md ol{margin:0 0 6px;padding-left:20px}.md li{margin:1px 0}
.md code{font-family:ui-monospace,Menlo,monospace;font-size:.88em;background:var(--well);border:1px solid var(--line);border-radius:4px;padding:0 4px}
.md pre{margin:0 0 6px;background:var(--well);border:1px solid var(--line);border-radius:6px;padding:8px 10px;overflow:auto;max-height:20em}.md pre code{border:0;padding:0;background:none}
.md blockquote{margin:0 0 6px;padding-left:10px;border-left:3px solid var(--line);color:var(--mut)}.md .mdh{font-weight:600}.md a{color:var(--acc)}.cline .chip{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid var(--line);color:var(--mut)}
details.plat .pbody h2{font-size:14px;margin:16px 0 6px}.pill.warn{color:var(--warn);border-color:var(--warn)}
.steps{list-style:none;padding:0;margin:8px 0 16px;display:flex;flex-direction:column;gap:12px;max-width:820px}
.step{display:flex;gap:12px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.step-n{flex:none;width:24px;height:24px;border-radius:50%;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:12px;color:var(--mut)}
.step.done .step-n{background:var(--ok);border-color:var(--ok);color:#fff}.step-body{flex:1;min-width:0}.step-t{font-weight:600;margin-bottom:4px}
.step .meta{color:var(--mut);font-size:13px;margin:4px 0 8px}.addline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.addline .grow{flex:1;min-width:200px}
.dmap{width:100%;border-collapse:collapse;margin-top:6px}.dmap td,.dmap th{padding:6px 6px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
.kinds{display:flex;flex-direction:column;gap:2px}.kinds label{display:flex;gap:6px;align-items:center;white-space:nowrap;margin:0;line-height:1.35}.kinds input{margin:0}
.dmap th{font-size:12px;color:var(--mut);font-weight:500}.dmap select{width:100%}.chips{display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.chip{display:inline-flex;gap:4px;align-items:center;font-size:12px;border:1px solid var(--line);border-radius:999px;padding:1px 4px 1px 8px}
.chip button{padding:0 5px;font-size:12px;line-height:1.4;background:transparent;color:var(--mut);border:0}
.dstate{display:inline-flex;align-items:center;gap:6px;font-size:13px;margin-bottom:8px}.dstate .dot{margin:0}
#channels a{color:var(--acc)}.dmap td:nth-child(2){min-width:190px}.dmap td:nth-child(1){min-width:150px}
#channels label.check{display:flex;gap:8px;align-items:flex-start;margin:6px 0;max-width:820px;font-size:13px}#channels label.check input{margin-top:3px}
.legend{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;font-size:13px;max-width:820px}
.ctx{font-size:11px;color:var(--mut);font-weight:500}.ctx.hi{color:var(--warn)}
.node .state{font-size:11px;color:var(--mut);margin-top:4px;display:flex;align-items:center;justify-content:space-between;gap:6px}.hot{color:var(--warn);font-weight:600}
.node .sum{font-size:12px;color:var(--mut);margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;width:240px;box-sizing:border-box;text-align:center;display:flex;flex-direction:column;min-height:150px}
.sbox .small.mut{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.sbox .spick{margin-top:auto;padding-top:8px}
.sbox.led{border-color:var(--acc)}.spick{display:flex;flex-direction:column;gap:4px;margin-top:8px;text-align:left}.spick label{display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:12px;color:var(--mut)}.spick select{font-size:12px;padding:2px 4px;width:160px}.sbox .small{margin-top:2px}
#detail:empty{display:none}
#detail{position:fixed;top:0;right:0;bottom:0;width:380px;max-width:100%;overflow-y:auto;background:var(--bg);border-left:1px solid var(--line);box-shadow:-8px 0 24px rgba(0,0,0,.08);padding:16px;z-index:10}
#detail .panel{border:0;padding:0;background:transparent}
.panel .top{display:flex;justify-content:space-between;align-items:center;gap:8px}.panel .name{font-size:16px}
.panel .x{padding:2px 8px;font-size:13px}.facts{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:10px 0 4px;font-size:13px}
.facts .k{color:var(--mut)}.threads{margin-top:8px;display:flex;flex-direction:column;gap:4px}.threads .k{color:var(--mut)}.panel form{margin-top:12px;border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:12px}
.panel label{display:flex;flex-direction:column;gap:4px;font-size:13px;font-weight:500}.panel label .hint{font-weight:400;font-size:12px;color:var(--mut)}
.panel label.check{flex-direction:row;align-items:center;gap:8px}.ok{color:var(--ok)}
#stalebar .mono{white-space:nowrap}#stalebar{background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:8px 14px;margin-bottom:14px;align-items:flex-start;gap:10px;max-width:1050px}
#stalebar details{flex:1;margin:0}#stalebar summary{font-size:13px;color:var(--fg)}#stalebar details .small{margin-top:6px}
#limitbar{background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
#offline{display:none;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px;color:var(--mut)}
#signedout{display:none;background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
.msgbar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}.msgbar select{width:auto}
.mlist{display:flex;flex-direction:column;gap:8px}
.task{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.task .mh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px}.task .mh .when{margin-left:auto;color:var(--mut);font-size:12px}
.task .note{margin-top:6px;overflow-wrap:anywhere;font-size:13px}.tblocked{margin-top:6px;font-size:13px;color:var(--warn);overflow-wrap:anywhere}
.tlog{margin-top:6px;font-size:12px}.tlog summary{cursor:pointer;color:var(--mut)}.tlog .te{margin-top:6px;padding-left:8px;border-left:2px solid var(--line);overflow-wrap:anywhere}.tlog .te.decision{border-left-color:var(--ok)}.tlog .te.blocked{border-left-color:var(--warn)}.tlog .te .k{color:var(--mut)}
.rule{display:flex;gap:8px;align-items:center;flex-wrap:wrap;border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--card,transparent)}.rule.off{opacity:.6}.rule input[type=text]{flex:1;min-width:220px}.rule select{width:150px}.task .tact{margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.task .tact select{width:auto}
.task.sub{margin-left:24px}button.danger{color:var(--bad);border-color:var(--bad)}nav .count.hot:not(:empty){background:var(--acc);color:#fff;border-radius:999px;padding:1px 7px;font-weight:600}.ititle{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.task .ftext{margin-top:6px;overflow-wrap:anywhere;font-size:13px;max-height:16em;overflow:auto}
.task .tact input{flex:1;min-width:160px}.tchip.new,.tchip.bug{color:var(--warn);border-color:var(--warn)}.tchip.accepted,.tchip.idea{color:var(--acc);border-color:var(--acc)}.tchip{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid var(--line);color:var(--mut)}
.fl[hidden]{display:none}.seg{display:inline-flex}.seg button{border-radius:0}.seg button:first-child{border-radius:8px 0 0 8px}.seg button:last-child{border-radius:0 8px 8px 0;margin-left:-1px}.seg button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.board{display:grid;grid-template-columns:repeat(4,minmax(0,1fr)) auto;gap:10px;align-items:start;margin-bottom:14px}
.lane-h{font-size:12px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:6px 0}
.col{background:var(--well);border:1px solid var(--line);border-radius:10px;padding:8px;min-height:80px;display:flex;flex-direction:column;gap:8px}
.col-h{font-size:12px;font-weight:600;color:var(--mut);display:flex;justify-content:space-between}
.col.can{border-color:var(--acc);border-style:dashed}.col.over,.tdrop .agent.over{background:rgba(217,119,87,.12);border-color:var(--acc)}
.col.shut{min-width:120px;max-width:130px}.col.shut .tcard{display:none}.col.shut.open{max-width:none;min-width:200px}.col.shut.open .tcard{display:block}
.col-h button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer}
.tcard{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px;cursor:grab}
.tcard.dragging{opacity:.4}.tcard .tt{font-weight:600;overflow-wrap:anywhere}.tcard .meta{font-size:12px;color:var(--mut);margin-top:3px}.tcard .meta.by{display:flex;gap:8px}.tcard .meta.by>span:first-child{min-width:0;overflow-wrap:anywhere}.tcard .meta.by .age{margin-left:auto;white-space:nowrap}.col .none{font-size:12px;color:var(--mut);padding:4px 2px}
.tcard .bar{height:4px;border-radius:2px;background:var(--line);margin-top:6px;overflow:hidden}.tcard .bar i{display:block;height:100%;background:var(--ok)}
.tcard .more{display:none;margin-top:6px}.tcard.open .more{display:block}.tcard .more .tact{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}.tcard .more select{width:auto;max-width:100%}
.tdrop{display:none;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 10px;font-size:12px;color:var(--mut)}.tdrop.on{display:flex}
.tdrop .agent{border:1px dashed var(--line);border-radius:999px;padding:3px 10px;color:var(--fg,inherit)}
@media (max-width:760px){.board{grid-template-columns:1fr}.col.shut{max-width:none}.col{min-height:0}}
.tchip.open,.tchip.in_progress{color:var(--acc);border-color:var(--acc)}.tchip.done{color:var(--ok);border-color:var(--ok)}.tchip.failed{color:var(--bad);border-color:var(--bad)}
.ptabs{display:flex;gap:4px;margin-top:12px;border-bottom:1px solid var(--line)}.ptabs button{background:transparent;color:var(--mut);border:0;border-bottom:2px solid transparent;border-radius:0;padding:6px 10px}
.ptabs button.on{color:var(--fg);border-bottom-color:var(--acc)}.ptabs+form{border-top:0}.ptasks{margin-top:12px;display:flex;flex-direction:column;gap:8px}.ptasks .k{color:var(--mut)}
.msg{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;cursor:pointer}
.msg .mh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px}.msg .mh .when{margin-left:auto;color:var(--mut);font-size:12px}
.msg .mt{margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.msg.open .mt{display:block}.msg .chip{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid var(--line);color:var(--mut)}
.chip.queued{border-color:var(--warn);color:var(--warn)}.chip.expired{border-color:var(--bad);color:var(--bad)}.chip.reply{color:var(--acc);border-color:var(--acc)}
.msg.inthread{margin-left:22px;border-left:3px solid var(--acc)}
@media (min-width:721px){body.drawer main{padding-right:404px;max-width:1504px}body.drawer.wide main{max-width:none}}
@media (max-width:720px){#detail{width:100%;border-left:0}}
.branch{display:flex;align-items:flex-start;justify-content:safe center}
.branch>.ocol{position:relative;padding:14px 6px 0}
.branch>.ocol::before{content:"";position:absolute;top:0;left:0;right:0;border-top:2px solid var(--acc)}
.branch>.ocol:first-child::before{left:50%}.branch>.ocol:last-child::before{right:50%}.branch>.ocol:only-child::before{display:none}
.branch>.ocol::after{content:"";position:absolute;top:0;left:calc(50% - 1px);width:2px;height:14px;background:var(--acc)}
@media (max-width:720px){
 .chart{overflow:visible}.unit{display:flex;width:100%;align-items:stretch}.unit-h{text-align:left}
 .orow{flex-direction:column;align-items:stretch;gap:8px}.ocol{align-items:stretch}.node{width:auto}
 .ocol>.down{display:none}.ocol>.unit{margin:8px 0 0 14px;width:auto;border-left:2px solid var(--acc);border-style:none none none solid;border-radius:0;padding:4px 0 4px 12px}
 .branch{flex-direction:column;align-items:stretch;margin-left:14px;border-left:2px solid var(--acc);padding-left:12px}
 .branch>.ocol{padding:8px 0 0}.branch>.ocol::before,.branch>.ocol::after{display:none}.sbox{max-width:none;text-align:left}
}
.mut{color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow-wrap:anywhere;min-width:0}
.peer .top{display:flex;justify-content:space-between;gap:8px;align-items:center}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--ok)}.dot.busy{background:var(--warn)}
.name{font-weight:600}.small{font-size:12px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}
.pill{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--mut)}
input:not([type=checkbox]):not([type=radio]),textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px;font:inherit}
input::placeholder,textarea::placeholder{color:var(--mut);opacity:.8}
textarea{min-height:180px;font-family:ui-monospace,Menlo,monospace;font-size:12px}
label{display:block;margin:8px 0 4px;font-size:12px;color:var(--mut)}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1 1 180px}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
button:hover:not(:disabled){filter:brightness(1.06)}button.ghost:hover:not(:disabled){border-color:var(--mut);filter:none}button:disabled{opacity:.55;cursor:default}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}input:focus-visible,select:focus-visible,textarea:focus-visible{outline-offset:0;border-color:var(--acc)}
.fl{display:inline-flex;gap:6px;align-items:center;white-space:nowrap}.fl label{margin:0}
input.search{width:auto;flex:1 1 220px;max-width:360px}
.panel .role{margin-top:8px;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere;border-left:2px solid var(--acc);padding-left:8px}.panel .role .k{color:var(--mut);font-size:12px}
.panel textarea[name=role]{min-height:64px;font:inherit;font-size:13px}
.sendbox{display:flex;flex-direction:column;gap:8px;margin-top:12px}.sendbox textarea{min-height:64px;font:inherit;font-size:13px}.sendbox .check{margin:0;font-weight:400}
#newtask{max-width:640px;margin:0 0 12px}#newtask[hidden]{display:none}#newtask select{width:100%}
details.adv{margin:0;border-top:1px dashed var(--line);padding-top:8px}details.adv summary{font-size:13px;color:var(--mut)}details.adv[open]{display:flex;flex-direction:column;gap:12px}
#cfg details.adv{margin-top:12px}#cfg details.adv[open]{gap:0}
button.copy{padding:0 6px;font-size:11px;margin-left:4px;font-family:inherit}
details.how{margin:-10px 0 16px;color:var(--mut)}details.how summary{font-size:13px;color:var(--acc);width:max-content}details.how p{margin:6px 0 0;max-width:760px}details.how ul{margin:6px 0 0;padding-left:18px;max-width:760px}
details{margin-top:8px}summary{cursor:pointer;font-size:12px;color:var(--mut)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:500}.msgtext{white-space:pre-wrap;max-height:6em;overflow:auto}
.tablewrap{overflow-x:auto}.empty{color:var(--mut);padding:14px 0}
.check{display:flex;align-items:center;gap:6px;margin-top:8px}
.err{color:var(--bad)}
@media (max-width:720px){.app{flex-direction:column}
 nav{position:sticky;top:0;z-index:5;height:auto;flex:none;flex-direction:row;align-items:center;overflow-x:auto;scrollbar-width:none;border-right:0;border-bottom:1px solid var(--line);padding:8px 12px}
 nav::-webkit-scrollbar{display:none}nav .brand{padding:6px 8px 6px 4px;flex:none}nav .host{display:none}nav a{padding:6px 10px;flex:none;white-space:nowrap}nav .count{margin-left:6px}
 nav .grp{padding:0;width:1px;height:18px;background:var(--line);overflow:hidden;color:transparent;flex:none;margin:0 4px}
 details.how{margin-top:-8px}
 main{padding:16px}}
</style></head><body><div class="app">
<nav>
<div class="brand"><svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="var(--acc)"/><path d="M7 11.5h17M19.5 7l4.5 4.5-4.5 4.5M25 20.5H8M12.5 16 8 20.5l4.5 4.5" fill="none" stroke="#fff" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/></svg>Peerlink</div><div class="host" id="hostline"></div>
<a href="#inbox" data-nav="inbox">Inbox <span class="count hot" id="n-inbox"></span></a>
<a href="#agents" data-nav="agents">Agents <span class="count" id="n-agents"></span></a>
<a href="#tasks" data-nav="tasks">Tasks <span class="count" id="n-tasks"></span></a>
<a href="#schedules" data-nav="schedules">Schedules <span class="count" id="n-sched"></span></a>
<a href="#messages" data-nav="messages">Messages <span class="count" id="n-msgs"></span></a>
<div class="grp" aria-hidden="true">Set up</div>
<a href="#spaces" data-nav="spaces">Spaces <span class="count" id="n-spaces"></span></a>
<a href="#channels" data-nav="channels">Channels <span class="count" id="n-discord"></span></a>
<div class="grp" aria-hidden="true">Peerlink</div>
<a href="#feedback" data-nav="feedback">Feedback <span class="count" id="n-feedback"></span></a>
<a href="#settings" data-nav="settings">Settings</a>
<a href="#controls" data-nav="controls">Controls</a>
</nav>
<main><div id="limitbar" style="display:none" class="small"></div><div id="stalebar" style="display:none" class="small"></div><div id="offline">Can't reach the peerlink daemon. This page reconnects once it's back.</div><div id="signedout">Signed out. Run <span class="mono">/peerlink web</span> in any agent to sign in again.</div>

<section id="agents"><h1>Agents</h1><p class="lead">Your live agents, by space. Click one to see or change it.</p>
<div class="msgbar"><span class="seg" role="group" aria-label="Layout"><button class="ghost small" data-alayout="chart">Chart</button><button class="ghost small" data-alayout="list">List</button></span>
<input id="asearch" class="search" type="search" placeholder="Search agents" title="Search by name, folder, branch or summary" aria-label="Search agents"></div>
<details class="how"><summary>How it works</summary><ul>
<li>Each space shows its lead first, outlined in orange, with its members beneath it. A space can sit inside another: its lead then sits with that space's agents, with its own space beneath it.</li>
<li>Work sent to a space goes to its lead: the agent you picked, or else the one that was alone in it. Pick leads, and which space sits inside which, on the Spaces page.</li>
<li>An agent only sees and messages agents in its own space. A lead also sees the space its space sits inside.</li>
<li>To move an agent, open it and set its space on the Details tab: its folder keeps it, for every agent opened there.</li>
<li>The bar is how full the agent's context window is: it turns yellow past half and red past 80%. A yellow dot means it's working, a green one that it's idle. Messages an hour show only when an agent is chatty, 20 or more.</li></ul></details><div class="orgwrap"><div id="peers"></div><div id="detail"></div></div></section>

<section id="inbox"><h1>Inbox</h1><p class="lead">Everything waiting on you, the longest waiting first. On Discord, <span class="mono">/inbox</span> shows the same list, and you answer by number, like <span class="mono">1 done</span>.</p>
<div class="msgbar"><button class="ghost small" id="notifybtn" data-notify>Desktop alerts: off</button></div>
<div class="mlist" id="inboxlist"></div></section>

<section id="spaces"><h1>Spaces</h1>
<p class="lead">Which agents can see and message each other.</p>
<details class="how"><summary>How spaces work</summary><p>An agent is in the space you put its folder in, from its Details tab on the Agents page. A folder you haven't placed takes the space in the nearest <span class="mono">.peerlink</span> file at or above it, else it's <b>unassigned</b>. Here you pick each space's lead, which gets work sent to the space, and which space it sits inside: its lead then speaks for it there.</p></details>
<div id="spacelist"></div>
<h2 style="margin-top:28px">Standing rules</h2>
<p class="lead">What you've told your agents for good. A rule reaches every agent in its space and the spaces inside it, in its instructions, so a cleared context still has it. One an agent proposes waits here for your approval.</p>
<div id="rules"><div id="rulelist" style="display:flex;flex-direction:column;gap:8px"></div>
<form id="ruleform" class="addline" style="margin-top:10px"><select name="space" id="rulespace" style="width:170px"></select><input name="text" type="text" class="grow" placeholder="Never merge to main on a Friday" required maxlength="600"><button type="submit">Add rule</button><span class="small err" id="rulemsg"></span></form></div>
</section>

<section id="schedules"><h1>Schedules</h1><p class="lead">Prompts the daemon sends an agent at a time or on a repeat, even with no terminal open.</p>
<details class="how"><summary>How schedules work</summary><p>Prompts the daemon sends an agent at a time or on a repeat, with no terminal open. A schedule is for one agent: it goes to that agent wherever it runs, never to another in its folder. When it isn't running, the run waits for it, or the agent is started again in its folder. Agents add them with their <span class="mono">schedule</span> tool; one that may start an agent waits for your approval. Edit any of them here.</p></details>
<details class="plat" id="schedadd"><summary><span class="plogo" style="background:var(--acc)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 5v14M5 12h14"/></svg></span><span class="ptitle"><b>Add a schedule</b><span class="psub">A prompt for one agent, once or on a repeat</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody" style="padding-top:12px">
<form id="schedform"><div class="addline"><select name="target" class="grow" id="schedagents" required></select>
<input name="when" type="text" placeholder="0 9 * * 1-5, or in 2h" title="A cron (minute hour day month weekday) repeats; 'in 30m', 'in 2h', 'in 1d' or 2026-10-25 09:00 runs once" style="width:190px"></div>
<textarea name="prompt" rows="2" placeholder="What the agent should do when it runs" style="margin-top:8px;width:100%"></textarea>
<input name="prompt_file" type="text" placeholder="Playbook file, read fresh at each run (optional): ~/Repos/app/PLAYBOOK.md" style="margin-top:8px">
<div class="addline" style="margin-top:8px"><label class="check small"><input type="checkbox" name="start" checked> Start it when it isn&#39;t running</label><button type="submit">Add schedule</button><span class="small" id="schedmsg"></span></div></form></div></details>
<div id="schedlist" style="margin-top:12px;display:flex;flex-direction:column;gap:8px"></div></section>

<section id="messages"><h1>Messages</h1><p class="lead">Conversations between agents, and with you on Discord, Telegram and Slack, the latest first. Open one for its whole history.</p>
<div class="msgbar"><span class="fl"><label class="small mut" for="msgspace">Space</label><select id="msgspace"><option value="">All spaces</option></select></span>
<span class="fl"><label class="small mut" for="msgagent">Agent</label><select id="msgagent"><option value="">Everyone</option></select></span>
<input id="msgq" class="search" type="search" placeholder="Search messages" aria-label="Search messages">
<label class="check small" style="margin:0"><input type="checkbox" id="msgnosched"> Hide schedule runs</label></div>
<div class="mlist" id="msgs"></div></section>

<section id="tasks"><h1>Tasks</h1><p class="lead">Work handed between agents. Click a card for details, or drag it.</p>
<details class="how"><summary>How tasks work</summary><ul>
<li>An agent hands out a task with <span class="mono">kind: task</span>. On Discord, start a post with <span class="mono">Task:</span>. A task stays open until its owner marks it done or failed, and whoever asked gets the owner's note.</li>
<li>A task sent to a lead is the lead's to split up. Its subtasks count toward it.</li>
<li>Drag a card to Cancelled to cancel it, from Done, Failed or Cancelled to Open to reopen it, or onto an agent to hand it over. Only the owner moves a task to In progress, Done or Failed. If the owner changed a task after the board was drawn, your move is refused.</li></ul></details>
<div class="msgbar"><button class="small" data-newtask>+ New task</button><span class="seg" role="group" aria-label="Layout"><button class="ghost small" data-tlayout="board">Board</button><button class="ghost small" data-tlayout="list">List</button></span>
<span id="tstatusw" class="fl"><label class="small mut" for="tstatus">Show</label><select id="tstatus"><option value="live">Open and in progress</option><option value="done">Done</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option><option value="">All</option></select></span>
<span class="fl"><label class="small mut" for="towner">Owner</label><select id="towner"><option value="">Everyone</option></select></span>
<span class="fl"><label class="small mut" for="treq">Asked by</label><select id="treq"><option value="">Anyone</option></select></span>
<span class="fl"><label class="small mut" for="tspace">Space</label><select id="tspace"><option value="">All spaces</option></select></span>
<span id="tgroupw" class="fl"><label class="small mut" for="tgroup">Rows</label><select id="tgroup"><option value="">One board</option><option value="space">By space</option><option value="owner">By owner</option></select></span></div>
<form id="newtask" class="card sendbox" hidden><label>To<select name="to" required></select></label>
<label>Task<textarea name="text" rows="3" placeholder="First line is the title. Add details below it." required></textarea></label>
<div class="addline"><button type="submit">Send task</button><button type="button" class="ghost" data-newtask>Cancel</button><span class="small" id="newtaskmsg"></span></div></form>
<div id="tdrop" class="tdrop"></div>
<div class="mlist" id="tasklist"></div></section>

<section id="feedback"><h1>Feedback</h1><p class="lead">Bugs and ideas agents report about peerlink. Accept one to hand it to the peerlink agent as a task.</p>
<div class="msgbar"><span class="fl"><label class="small mut" for="fstatus">Show</label><select id="fstatus"><option value="">All, by status</option><option value="new">New</option><option value="accepted">Accepted</option><option value="done">Done</option><option value="dismissed">Dismissed</option></select></span>
<span class="fl"><label class="small mut" for="ftype">Type</label><select id="ftype"><option value="">Bugs and ideas</option><option value="bug">Bugs</option><option value="idea">Ideas</option></select></span></div>
<div class="mlist" id="fblist"></div></section>

<section id="channels"><h1>Channels</h1>
<p class="lead">Talk to your agents from outside the terminal.</p>
<details class="plat" id="pdiscord"><summary><span class="plogo" style="background:#5865F2"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M20.317 4.37a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.865-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.74 19.74 0 0 0 3.677 4.37a.07.07 0 0 0-.032.028C.533 9.046-.32 13.58.099 18.058a.082.082 0 0 0 .031.056 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.1 13.1 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.291a.074.074 0 0 1 .078-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.009c.12.099.246.198.373.292a.077.077 0 0 1-.007.128 12.3 12.3 0 0 1-1.873.891.077.077 0 0 0-.041.107c.36.698.772 1.363 1.225 1.993a.076.076 0 0 0 .084.029 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.331c-1.183 0-2.157-1.086-2.157-2.42 0-1.333.956-2.418 2.157-2.418 1.21 0 2.176 1.095 2.157 2.419 0 1.333-.956 2.419-2.157 2.419zm7.975 0c-1.183 0-2.157-1.086-2.157-2.42 0-1.333.955-2.418 2.157-2.418 1.21 0 2.176 1.095 2.157 2.419 0 1.333-.946 2.419-2.157 2.419z"/></svg></span><span class="ptitle"><b>Discord</b><span class="psub">Channels in your server</span></span><span class="pstate" id="dstatus"></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<p class="meta">Each channel you map has agents: ones that <b>receive</b> get your messages there, ones that <b>send</b> can post there. Only you, the bot's owner, can talk to them.</p>
<ol class="steps">
<li class="step" id="dstep1"><span class="step-n">1</span><div class="step-body"><div class="step-t">Create the bot and paste its token</div><div id="dstep1b"></div></div></li>
<li class="step" id="dstep2"><span class="step-n">2</span><div class="step-body"><div class="step-t">Invite it to your server</div><div id="dstep2b"></div></div></li>
<li class="step" id="dstep3"><span class="step-n">3</span><div class="step-body"><div class="step-t">Map channels to agents</div><p class="meta">Under <b>Chats and agents</b>, below.</p></div></li>
</ol></div></details>
<details class="plat" id="ptelegram"><summary><span class="plogo" style="background:#26A5E4"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M4.6 11.6l13.1-5.05c.6-.22 1.13.15.93 1.06l-2.23 10.5c-.17.75-.6.93-1.23.58l-3.4-2.5-1.64 1.58c-.18.18-.33.33-.68.33l.24-3.46 6.3-5.69c.27-.24-.06-.38-.43-.14l-7.78 4.9-3.35-1.05c-.73-.23-.74-.73.15-1.07z"/></svg></span><span class="ptitle"><b>Telegram</b><span class="psub">A private chat or a group</span></span><span class="pstate" id="tgstatus"></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<p class="meta">A private chat with your bot, or a group it's in, works like a Discord channel. Telegram bots answer anyone who finds them, so peerlink ignores everyone until you allow them.</p>
<ol class="steps">
<li class="step" id="tstep1"><span class="step-n">1</span><div class="step-body"><div class="step-t">Create the bot and paste its token</div><div id="tstep1b"></div></div></li>
<li class="step" id="tstep2"><span class="step-n">2</span><div class="step-body"><div class="step-t">Message it, then allow yourself</div><div id="tstep2b"></div></div></li>
<li class="step" id="tstep3"><span class="step-n">3</span><div class="step-body"><div class="step-t">Map chats to agents</div><div id="tstep3b"></div></div></li>
</ol></div></details>
<details class="plat" id="pslack"><summary><span class="plogo" style="background:#4A154B"><svg viewBox="0 0 24 24" aria-hidden="true"><path stroke="#fff" stroke-width="2.4" stroke-linecap="round" fill="none" d="M9 4.5v15M15 4.5v15M4.5 9h15M4.5 15h15"/></svg></span><span class="ptitle"><b>Slack</b><span class="psub">A channel or a direct message</span></span><span class="pstate" id="slstatus"></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<p class="meta">A channel your Slack app is in, or a direct message with it, works like a Discord channel. Everyone in the workspace can message the app, so peerlink ignores everyone until you allow them.</p>
<ol class="steps">
<li class="step" id="sstep1"><span class="step-n">1</span><div class="step-body"><div class="step-t">Create the app and paste its two tokens</div><div id="sstep1b"></div></div></li>
<li class="step" id="sstep2"><span class="step-n">2</span><div class="step-body"><div class="step-t">Message it, then allow yourself</div><div id="sstep2b"></div></div></li>
<li class="step" id="sstep3"><span class="step-n">3</span><div class="step-body"><div class="step-t">Map channels to agents</div><div id="sstep3b"></div></div></li>
</ol></div></details>
<details class="plat" id="pmap"><summary><span class="plogo" style="background:var(--acc)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" d="M4 7h11m0 0-3-3m3 3-3 3M20 17H9m0 0 3-3m-3 3 3 3"/></svg></span><span class="ptitle"><b>Chats and agents</b><span class="psub">Which agents hear and post in each channel</span></span><span class="pstate" id="mapstatus"></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div id="dstep3b"></div></div></details>
<details class="plat" id="popts"><summary><span class="plogo" style="background:var(--mut)"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.2" fill="none" stroke="#fff" stroke-width="2"/><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/></svg></span><span class="ptitle"><b>Options</b><span class="psub">Progress reactions, buttons, files and who can talk</span></span><span class="pstate" id="optstatus"></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div id="dmore"></div></div></details>
</section>

<section id="settings"><h1>Settings</h1><p class="lead">Changes reach every agent within a few seconds.</p>
<form id="cfg">
<details class="plat" id="swake"><summary><span class="plogo" style="background:var(--acc)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg></span><span class="ptitle"><b>Waking and delivery</b><span class="psub">When a message interrupts an agent, and when it waits</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="check"><input type="checkbox" name="auto_wake_idle" id="aw"><label for="aw" style="margin:0">Wake idle agents when a message arrives</label></div>
<div class="check"><input type="checkbox" name="inject_while_busy" id="ib"><label for="ib" style="margin:0">Deliver into a running turn</label></div>
<div class="row"><div><label for="wf">Wake an idle agent for</label><select name="wake_for" id="wf" style="width:100%"><option value="needed">Questions, tasks, your messages and answers (saves tokens)</option><option value="all">Every message</option></select></div>
<div><label for="wd">Wait before waking for another agent's message (s)</label><input type="number" name="wake_delay_s" id="wd" min="0" max="120"></div></div>
<div class="mut small">Each wake-up is a full turn that re-reads the agent's context. Waiting a few seconds lets messages that arrive together share one turn; your own messages never wait.</div>
</div></details>
<details class="plat" id="sctx"><summary><span class="plogo" style="background:#7a6ff0"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 3v4M12 17v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M3 12h4M17 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8"/></svg></span><span class="ptitle"><b>Context and usage</b><span class="psub">Clearing context so it doesn't drain your usage limit</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="check"><input type="checkbox" name="clear_between_tasks" id="cbt"><label for="cbt" style="margin:0">Agents clear their own context after each task, saving notes to memory first</label></div>
<div class="row"><div><label for="tnh">Ask a task's owner where it stands after this many hours without an update (0 for never)</label><input type="number" name="task_nudge_h" id="tnh" min="0" step="12"></div><div></div></div>
<div class="row"><div><label for="cnt">Ask an agent to save and clear once its context passes (tokens, 0 for never)</label><input type="number" name="clear_nudge_tokens" id="cnt" min="0" step="50000"></div><div></div></div>
<div class="mut small">A large context is re-read on every turn and drains the usage limit fast. The ask arrives with the agent's next prompt, so it clears at a stopping point.</div>
</div></details>
<details class="plat" id="sapprove"><summary><span class="plogo" style="background:var(--ok)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4"/></svg></span><span class="ptitle"><b>Approvals</b><span class="psub">Whether agents ask you before starting agents and schedules</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="check"><input type="checkbox" name="auto_approve_all" id="aaa"><label for="aaa" style="margin:0">Let every agent start agents and schedules without asking me</label></div>
<div class="mut small">You're still told on Discord when one does. To allow only some agents, leave this off and turn it on in an agent's Controls tab.</div>
<div class="check" style="margin-top:10px"><input type="checkbox" name="ask_defaults" id="adf"><label for="adf" style="margin:0">Let an agent's question name a default it goes with when I don't answer in time</label></div>
<div class="row"><div><label for="adm">The shortest wait an agent may give me (minutes)</label><input type="number" name="ask_default_min_m" id="adm" min="0" step="5"></div><div></div></div>
<div class="mut small">The question says the default and the wait. When the wait runs out you're told under it, and a later answer from you still wins. A standing rule an agent proposes always waits for your approval.</div>
</div></details>
<details class="plat" id="ssee"><summary><span class="plogo" style="background:#3d8fd6"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/></svg></span><span class="ptitle"><b>What agents see</b><span class="psub">The live agent list, summaries and feedback</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="check"><input type="checkbox" name="roster_in_prompt" id="rp"><label for="rp" style="margin:0">Show each agent the live agent list when it changes</label></div>
<div class="check"><input type="checkbox" name="auto_summary" id="as"><label for="as" style="margin:0">Keep agent summaries current automatically</label></div>
<div class="row"><div><label>Summary model</label><input type="text" name="summary_model"></div><div></div></div>
<div class="check"><input type="checkbox" name="feedback" id="fbk"><label for="fbk" style="margin:0">Agents report bugs and ideas about peerlink itself, on the Feedback page</label></div>
<div class="mut small">For people developing peerlink. Agents started after you change this get the report_issue tool, or don't.</div>
</div></details>
<details class="plat" id="sguide"><summary><span class="plogo" style="background:#c58a2b"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h5"/></svg></span><span class="ptitle"><b>Guidance</b><span class="psub">What every agent is told about working with its peers</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<textarea name="guidance" id="guidance" aria-label="Guidance for every agent"></textarea><div class="mut small">Leave empty to use the default, shown in grey.</div>
</div></details>
<details class="plat" id="slimits"><summary><span class="plogo" style="background:var(--mut)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2"/></svg></span><span class="ptitle"><b>Limits and timing</b><span class="psub">Polling, timeouts and how much agents can send</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="row">
<div><label>Poll interval (ms)</label><input type="number" name="poll_interval_ms" min="500"></div>
<div><label>Agent timeout (s)</label><input type="number" name="peer_ttl_s" min="5"></div>
<div><label>Max reply hops</label><input type="number" name="max_hops" min="1"></div>
<div><label>Messages per minute per agent</label><input type="number" name="rate_limit_per_min" min="1"></div>
</div>
</div></details>
<details class="plat" id="sremote"><summary><span class="plogo" style="background:#4f6b7a"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M5 4h14v6H5zM5 14h14v6H5zM8 7h.01M8 17h.01"/></svg></span><span class="ptitle"><b>Other machines</b><span class="psub">Linking daemons on other computers</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<label>Their daemons, one URL per line</label><textarea name="remotes" style="min-height:60px"></textarea>
<div class="row"><div><label>Listen host (0.0.0.0 to accept other machines; needs a restart)</label><input type="text" name="listen_host"></div>
<div><label>Shared token</label><input type="text" name="token"></div></div>
</div></details>
<p class="savebar"><button type="submit">Save</button> <span class="mut small" id="saved"></span></p>
</form></section>

<section id="controls"><h1>Controls</h1><p class="lead">Sign-in, the key, and the daemon.</p>
<div class="plat static"><div class="psum"><span class="plogo" style="background:#3d8fd6"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10z"/></svg></span><span class="ptitle"><b>Browsers</b><span class="psub"><span id="browsers"></span></span></span><span class="pact"><button class="ghost" data-act="logout">Sign out here</button> <button class="ghost" data-act="logout-all">Sign out everywhere</button></span></div></div>
<div class="plat static"><div class="psum"><span class="plogo" style="background:#c58a2b"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M15.5 7.5a4.5 4.5 0 1 1-6.36 6.36A4.5 4.5 0 0 1 15.5 7.5zM15.5 7.5 21 2M18 5l2 2"/></svg></span><span class="ptitle"><b>Key</b><span class="psub">Agents and sign-in links use the key in <span class="mono" id="keypath"></span>. Rotating it signs out every browser.</span></span><span class="pact"><button class="ghost" data-act="rotate">Rotate key</button></span></div></div>
<div class="plat static"><div class="psum"><span class="plogo" style="background:var(--acc)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 2v10M18.4 6.6a9 9 0 1 1-12.8 0"/></svg></span><span class="ptitle"><b>Daemon</b><span class="psub">Agents reconnect by themselves after a restart. Queued messages are kept.</span></span><span class="pact"><button class="ghost" data-act="restart">Restart daemon</button></span></div></div>
<p class="mut small" id="ctlmsg"></p></section>
</main></div>
<script>
const $=s=>document.querySelector(s);let cfg={},editing=false,sel='';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Markdown as agents write it, made safe: everything is escaped first, then a small set of marks become tags.
// Links open only http(s) URLs. plain() drops the marks, for one-line previews and titles.
const md=src=>{const keep=[];const hold=h=>{keep.push(h);return `\u0001${keep.length-1}\u0001`};
  const inline=s=>{s=s.replace(/`([^`\n]+)`/g,(_,c)=>hold(`<code>${esc(c)}</code>`))
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,(_,l,u)=>hold(`<a href="${esc(u)}" target="_blank" rel="noreferrer">${esc(l)}</a>`))
      .replace(/https?:\/\/[^\s<>"'`]+[^\s<>"'`.,;:!?)\]]/g,u=>hold(`<a href="${esc(u)}" target="_blank" rel="noreferrer">${esc(u)}</a>`));
    return esc(s).replace(/\*\*(?=\S)([^*\n]+?)\*\*/g,'<b>$1</b>').replace(/__(?=\S)([^_\n]+?)__/g,'<b>$1</b>')
      .replace(/(^|[^\w*])\*(?=\S)([^*\n]+?)\*(?!\w)/g,'$1<i>$2</i>').replace(/(^|[^\w_])_(?=\S)([^_\n]+?)_(?!\w)/g,'$1<i>$2</i>')
      .replace(/~~(?=\S)([^~\n]+?)~~/g,'<s>$1</s>')};
  let t=String(src??'').replace(/\r/g,'').replace(/```[^\n]*\n?([\s\S]*?)(```|$)/g,(_,c)=>`\n${hold(`<pre><code>${esc(c.replace(/\n$/,''))}</code></pre>`)}\n`);
  const out=[];let list=null,para=[];
  const flush=()=>{if(para.length){out.push(`<p>${para.join('<br>')}</p>`);para=[]}if(list){out.push(`<${list.tag}${list.start>1?` start="${list.start}"`:''}>${list.items.map(i=>`<li>${i}</li>`).join('')}</${list.tag}>`);list=null}};
  for(const line of t.split('\n')){let m;
    if(!line.trim()){flush();continue}
    if(/^\u0001\d+\u0001$/.test(line.trim())){flush();out.push(line.trim());continue}
    if((m=line.match(/^\s{0,3}#{1,6}\s+(.*)$/))){flush();out.push(`<p class="mdh">${inline(m[1])}</p>`);continue}
    if((m=line.match(/^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/))){const tag=m[1]?'ol':'ul';if(para.length||list&&list.tag!==tag)flush();
      list=list||{tag,items:[],start:+m[1]||1};list.items.push(inline(m[2]));continue}
    if((m=line.match(/^\s*>\s?(.*)$/))){flush();out.push(`<blockquote>${inline(m[1])}</blockquote>`);continue}
    if(list&&/^\s{2,}\S/.test(line)){list.items[list.items.length-1]+='<br>'+inline(line.trim());continue}
    if(list)flush();para.push(inline(line))}
  flush();return out.join('').replace(/\u0001(\d+)\u0001/g,(_,i)=>keep[+i])};
const plain=s=>String(s??'').replace(/```[^\n]*\n?/g,'').replace(/\[([^\]\n]+)\]\([^)\s]+\)/g,'$1').replace(/(\*\*|__|~~|`)/g,'').replace(/^\s{0,3}#{1,6}\s+/gm,'').replace(/\s+/g,' ').trim();
const J={'Content-Type':'application/json'};
const tilde=p=>String(p||'').replace(/^\/Users\/[^/]+/,'~');
// Signed out (the cookie expired, or every browser was signed out): say so and stop polling, rather than reload.
let signedOut=false;
async function api(p,o){const r=await fetch(p,o);if(r.status===401){signedOut=true;$('#signedout').style.display='block';throw new Error('signed out')}return r.json()}
const post=(p,b)=>api(p,{method:'POST',headers:J,body:JSON.stringify(b||{})});
// Agents and Tasks lay out across the whole window; the reading pages keep a comfortable line length.
function show(){const id=(location.hash||'#agents').slice(1);document.body.classList.toggle('wide',id==='agents'||id==='tasks');
  document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===id));
  document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.dataset.nav===id))}
// Leaving Agents, or Escape, closes the agent panel: it should never linger over another page.
const closePanel=()=>{if(!sel)return;sel='';editing=false;refresh(true)};
// A page starts at its top: the browser would otherwise scroll to the section the link names, hiding its heading.
window.addEventListener('hashchange',()=>{show();closePanel();scrollTo(0,0)});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanel()});
function agentPanel(x,others=[]){const sc=cfg.sessions?.[x.id]||{};const setSpace=x.space_source==='set'?x.space:'';
  const wake=sc.auto_wake===true?'on':sc.auto_wake===false?'off':'default';
  const why=x.space_source==='started'?'the agent that started it asked for it':x.space_source==='set'?'you put its folder here':x.space_source==='file'?'from '+tilde(x.space_from):'no space set';
  const how={set:'you picked it',only:'it was the only agent in the space'}[x.leads]||'';
  const led=leadOf[x.space];
  const role=x.leads?`Leads the <b>${esc(x.space)}</b> space${x.parent?` inside <b>${esc(x.parent)}</b>`:''} <span class="mut">· ${how}</span>`:`Member${led?` · led by ${esc(led.name)}${led.live?'':' (offline)'}`:''}`;
  // Only you, here, pick a lead: the daemon refuses it from agents and bots.
  const leadBtn=x.remote||!x.space||x.space==='default'?'':x.leads?`<button class="ghost small" data-clearlead="${esc(x.space)}">Clear lead</button>`:`<button class="ghost small" data-makelead="${esc(x.space)}" data-cwd="${esc(x.cwd)}">${led?'Make lead instead of '+esc(led.name):'Make lead of '+esc(x.space)}</button>`;
  const opt=(v,t)=>`<option value="${v}" ${wake===v?'selected':''}>${t}</option>`;
  const tab=x.remote?'about':panelTab;
  return `<div class="card panel${x.leads?' is-lead':''}">
   <div class="top"><span class="phead"><span class="plogo" style="background:${x.leads?'var(--acc)':'var(--mut)'}"><b style="color:#fff">${initial(x.name)}</b></span>
     <span class="ptitle"><span><span class="name">${esc(x.name)}</span>${x.remote?` <span class="mut small">@${esc(x.host)}</span>`:''}</span>
     <span class="psub"><span class="dot ${x.busy?'busy':''}"></span>${x.busy?'working':'idle'}${x.busy_since?' '+ago(x.busy_since):''} · <span class="pill">${esc(x.space||'default')}</span>${x.leads?' <span class="pill acc">lead</span>':''}</span></span></span><button class="ghost x" data-close title="Close">✕</button></div>
   <div class="pwhere"><span class="mono">${esc(tilde(x.cwd))}${x.branch?' · ⎇ '+esc(x.branch):''}</span><button class="ghost small copy" data-copy="${esc(x.cwd)}" title="Copy the folder path">Copy</button></div>
   ${x.role?`<div class="role"><span class="k">Role</span> ${esc(x.role)}</div>`:''}
   <div class="pnow"><div class="k small">Now${x.summary_ts&&x.summary?` · set ${ago(x.summary_ts)} ago${oldSummary(x)?', may be out of date':''}`:''}</div>${oldSummary(x)?`<span class="mut">${esc(x.summary)}</span>`:esc(x.summary)||'<span class="mut">no summary yet</span>'}</div>
   ${x.remote?'':`<div class="ptiles"><div><span class="k small">Context</span><b>${x.context??'?'}%</b>${x.context!=null?`<span class="cmeter${x.context>=80?' hi':x.context>=50?' mid':''}"><i style="width:${Math.min(100,x.context)}%"></i></span>`:''}</div>
     <div><span class="k small">Sent / hour</span><b${x.sent_hour>=20?' style="color:var(--warn)"':''}>${x.sent_hour||0}</b></div><div><span class="k small">Open tasks</span><b>${x.open_tasks||0}</b></div></div>`}
   ${x.remote?'':`<div class="ptabs">${[['about','Agent'],['tasks','Tasks'+(x.open_tasks?' · '+x.open_tasks:'')],['details','Details'],['controls','Controls']].map(([k,t])=>`<button class="${tab===k?'on':''}" data-ptab="${k}">${t}</button>`).join('')}</div>`}
   ${tab==='tasks'?panelTasks(x):tab==='details'?`<form id="agentform" data-id="${esc(x.id)}" data-nick="${esc(sc.nickname||'')}" data-space="${esc(setSpace)}" data-role="${esc(sc.role||'')}" data-cwd="${esc(x.cwd)}" data-also="${esc((x.also||[]).join(', '))}">
    <label>Name<input name="nickname" type="text" value="${esc(sc.nickname||'')}" placeholder="${esc(x.name)}"><span class="hint">What other agents call it. Kept for this folder.</span></label>
    <label>Role<textarea name="role" rows="3" placeholder="${x.folder_role?'From the folder: '+esc(x.folder_role):'What this agent is here for, e.g. Owns the billing API: invoices, payments, the Stripe webhook.'}">${esc(sc.role||'')}</textarea><span class="hint">For this agent only, kept across /clear and resume. The agent gets it as its brief, and other agents see it.${x.folder_role?' Leave it empty to use the role: in its folder\'s .peerlink.':' A role: line in a folder\'s .peerlink is the role of every agent there without one of its own.'}</span></label>
    <label>Space<input name="space" type="text" list="spacenames" value="${esc(setSpace)}" placeholder="${x.space_source==='file'?esc(x.space)+' (from its .peerlink)':x.space_source==='started'?esc(x.space)+' (started for it)':'unassigned'}"><span class="hint">Kept for its folder: every agent opened there joins it.${x.space_source==='file'||!setSpace?'':' Leave it empty to go back to the folder\'s .peerlink.'}</span></label>
    <details class="adv"${(x.also||[]).length?' open':''}><summary>Also in other spaces</summary>
    <label>Also a member of<input name="also" type="text" value="${esc((x.also||[]).join(', '))}" placeholder="none"><span class="hint">Rarely needed: more spaces it sees and messages, on top of its own. Separate with commas.</span></label></details>
    <datalist id="spacenames">${Object.keys(leadOf).concat(livePeers.map(p=>p.space)).filter((n,i,a)=>n&&n!=='default'&&a.indexOf(n)===i).sort().map(n=>`<option value="${esc(n)}">`).join('')}</datalist>
    <div><button type="submit">Save</button> <span id="agentmsg" class="small"></span></div></form>`:tab==='controls'?`<form id="agentform" data-id="${esc(x.id)}" data-wake="${wake}" data-muted="${x.muted?'1':''}" data-auto="${x.auto_approve?'1':''}">
    <label>When a message arrives<select name="wake">${opt('default','Use the default')}${opt('on','Start a turn')}${opt('off','Wait for the next prompt')}</select></label>
    <label class="check"><input name="muted" type="checkbox" ${x.muted?'checked':''}> Mute: hold every message for this agent</label>
    <label class="check"><input name="auto_approve" type="checkbox" ${x.auto_approve||cfg.auto_approve_all?'checked':''} ${cfg.auto_approve_all?'disabled title="On for every agent in Settings"':''}> Let it start agents and schedules without asking me (its folder keeps this)</label>
    <div><button type="submit">Save</button> <span id="agentmsg" class="small"></span></div></form>
    ${leadBtn?`<div class="threads"><div class="k small">Lead</div><div class="small mut">${role}</div><div>${leadBtn}</div></div>`:''}
    <div class="threads"><div class="k small">Clear its context</div><div class="small mut">Runs /clear once its current turn ends: a fresh conversation in the same folder, keeping its name, space and role. What it still owes peers is given back to it.</div>
    <div><button class="ghost small" data-clearctx="${esc(x.id)}">Clear context</button> <span class="small ok" id="clearmsg"></span></div></div>
    <div class="threads"><div class="k small">End this agent</div><div class="small mut">It finishes its current turn, then closes. If it's still running a minute later, it's stopped.</div>
    <div><button class="ghost small danger" data-terminate="${esc(x.id)}" ${x.terminating?'disabled':''}>${x.terminating?'Ending…':'Terminate'}</button></div></div>`:`<div class="facts">${x.muted?'<span class="k">Status</span><span>muted</span>':''}
    <span class="k">Space</span><span><span class="pill">${esc(x.space||'default')}</span> <span class="mut">${esc(why)}</span>${(x.also||[]).length?` <span class="mut">· also in</span> ${x.also.map(n=>`<span class="pill">${esc(n)}</span>`).join(' ')}`:''}</span>
    <span class="k">Role</span><span>${role}</span>
    ${x.sent_hour>=20?'<span class="k">Messages</span><span style="color:var(--warn)">chatty: each one can cost its recipient a turn</span>':''}
    ${x.context>=80?'<span class="k">Context</span><span style="color:var(--warn)">close to compacting</span>':''}
    <span class="k">Plugin</span><span>${esc(x.plugin||'older than 0.6.6')}${x.stale&&!x.remote?` · <span style="color:var(--warn)">${x.reload_queued?`reload queued ${ago(x.reload_queued)} ago and not run yet: `:''}run /reload-plugins in this agent</span>`:''}</span></div>
   ${others.length?`<div class="threads"><div class="k small">Other sessions in this folder</div>${others.map(o=>`<button class="ghost small" data-sel="${esc(o.id)}" style="text-align:left">${esc(o.name)} · ${o.busy?'working':'idle'}${o.summary?' · '+esc(o.summary.slice(0,60)):''}</button>`).join('')}</div>`:''}
   ${(x.threads&&(x.threads.owes.length||x.threads.waiting.length))?`<div class="threads"><div class="k small">Open requests</div>
    ${x.threads.owes.map(t=>`<div class="small">owes <b>${esc(t.with)}</b> a reply · <span class="mut">${esc(t.id)}: ${esc(t.text.slice(0,90))}</span></div>`).join('')}
    ${x.threads.waiting.map(t=>`<div class="small">waiting on <b>${esc(t.with)}</b> · <span class="mut">${esc(t.id)}: ${esc(t.text.slice(0,90))}</span></div>`).join('')}</div>`:''}
   ${x.remote?'':`<form id="msgform" class="sendbox" data-to="${esc(x.id)}"><label>Message ${esc(x.name)}<textarea name="text" rows="3" placeholder="It arrives as a message from you; its reply shows in Messages."></textarea></label>
    <div class="addline"><label class="check"><input type="checkbox" name="astask"> Send as a task</label><button type="submit">Send</button><span class="small ${sentNote.err?'err':'ok'}" id="sendmsg">${sentNote.to===x.id?esc(sentNote.text):''}</span></div></form>`}
   ${x.remote?'<div class="small mut">This agent runs on another machine; change it there.</div>':''}`}</div>`}
// Messages as a conversation: newest first, each reply under the message it answers.
let msgFilter='',spaceFilter='',livePeers=[],leadOf={};
// A message's spaces: what it recorded when sent, else (older messages) the space of a live agent on either end.
const spacesOf=x=>{const by=n=>livePeers.find(p=>p.name===n)?.space;return [x.from_space||by(x.from_name),x.to_space||by(x.to_name)].filter(Boolean)};
function renderMessages(list){
  const opts=(el,all,vals)=>{const keep=el.value;el.innerHTML=`<option value="">${all}</option>`+vals.map(n=>`<option ${n===keep?'selected':''}>${esc(n)}</option>`).join('')};
  opts($('#msgspace'),'All spaces',[...new Set(list.flatMap(spacesOf))].sort());
  const inSpace=list.filter(x=>!spaceFilter||spacesOf(x).includes(spaceFilter));
  opts($('#msgagent'),'Everyone',[...new Set(inSpace.flatMap(x=>[x.from_name,x.to_name]))].sort());
  const q=($('#msgq').value||'').trim().toLowerCase();
  const noSched=$('#msgnosched').checked;
  const shown=inSpace.filter(x=>!(noSched&&x.from==='peerlink'&&/^schedule /.test(x.from_name||''))&&(!msgFilter||x.from_name===msgFilter||x.to_name===msgFilter)&&(!q||[x.text,x.from_name,x.to_name,x.id].join(' ').toLowerCase().includes(q)));
  // One panel per conversation: the two ends of each message, newest activity first. Opened, it shows the pair's whole
  // history from the log, oldest first, as a chat reads.
  const convs=new Map();
  for(const x of shown){const a=partyOf(x,'from'),b=partyOf(x,'to'),key=[a,b].sort().join('|');
    const c=convs.get(key)||{key,a:a<b?a:b,b:a<b?b:a,names:{},list:[]};c.names[a]=x.from_name;c.names[b]=x.to_name;c.list.push(x);convs.set(key,c)}
  const rows=[...convs.values()].map(c=>({...c,last:c.list.reduce((m,x)=>x.ts>m.ts?x:m)})).sort((p,q)=>q.last.ts-p.last.ts);
  const html=rows.map(c=>{const open=openConvs.has(c.key),hist=convHist[c.key];
    const pending=c.list.filter(x=>x.status==='queued').length,waiting=c.list.some(x=>x.expects_reply&&!c.list.some(y=>y.reply_to===x.id));
    const body=!open?'':hist?hist.list.map(x=>convLine(x,c)).join('')+(hist.list.length>=500?'<div class="small mut">Older messages are in ~/.peerlink/messages.jsonl.</div>':''):'<div class="small mut">Loading…</div>';
    return `<details class="plat conv" data-conv="${esc(c.key)}" data-a="${esc(c.a)}" data-b="${esc(c.b)}" data-last="${esc(c.last.id)}" ${open?'open':''}><summary>
      <span class="ptitle"><span class="cnames">${partyLabel(c.a,c.names[c.a])}<span class="mut">↔</span>${partyLabel(c.b,c.names[c.b])}</span>
      <span class="psub">${esc(c.last.from_name)}: ${esc(plain(c.last.text).slice(0,140))}</span></span>
      <span class="pstate">${pending?`<span class="pill warn">${pending} queued</span>`:''}${waiting?'<span class="pill">reply expected</span>':''}<span class="small mut">${when(c.last.ts)}</span></span><span class="chev" aria-hidden="true"></span></summary>
      <div class="pbody convbody">${body}</div></details>`}).join('');
  $('#msgs').innerHTML=html||'<div class="empty">No messages yet.</div>';
  document.querySelectorAll('#msgs .convbody').forEach(el=>el.scrollTop=el.scrollHeight);
  for(const c of rows)if(openConvs.has(c.key)&&(!convHist[c.key]||convHist[c.key].last!==c.last.id))loadConv(c.key,c.a,c.b,c.last.id);
}
const when=ts=>new Date(ts*1000).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const partyOf=(x,side)=>{const who=String(x[side]||'');return who.startsWith('discord:')?who:'n:'+String(x[side+'_name']||who).toLowerCase()};
// A chat channel shows its platform's logo, and its name without "#… on Telegram".
const partyLabel=(key,name)=>{if(!key.startsWith('discord:'))return `<b>${esc(name)}</b>`;
  return `${platLogo(key.slice(8))}<b>${esc(String(name).replace(/^#/,'').replace(/ on (Discord|Telegram|Slack)$/,''))}</b>`};
function convLine(x,c){const st=x.status==='delivered'?'':`<span class="chip ${esc(x.status)}">${esc(x.status)}</span>`;
  return `<div class="cline${partyOf(x,'from')===c.a?'':' other'}${openLines.has(x.id)?' open':''}" data-line="${esc(x.id)}"><div class="mh"><b>${esc(x.from_name)}</b>
    ${x.kind==='announcement'?`<span class="chip reply">announcement · ${(x.recipients||[]).length} agents</span>`:''}${x.task?`<span class="chip reply">task ${esc(x.task)}</span>`:x.expects_reply?'<span class="chip">reply expected</span>':''}${x.reply_to?`<span class="chip reply">re ${esc(x.reply_to)}</span>`:''}${st}
    <span class="when">${when(x.ts)} · ${esc(x.id)}</span></div><div class="mt md">${md(x.text)}</div></div>`}
const openConvs=new Set(),convHist={},openLines=new Set();
// Schedule runs are hidden unless you've asked to see them.
try{$('#msgnosched').checked=localStorage.getItem('pl.msg.nosched')!=='0'}catch(e){$('#msgnosched').checked=true}
document.addEventListener('click',e=>{const l=e.target.closest('.cline');if(!l||e.target.closest('a')||getSelection().toString())return;l.classList.toggle('open');
  l.classList.contains('open')?openLines.add(l.dataset.line):openLines.delete(l.dataset.line)});let lastMsgs=[];
async function loadConv(key,a,b,last){convHist[key]=convHist[key]||null;
  const r=await api(`/api/conversation?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);convHist[key]={last,list:r.messages||[]};
  const el=document.querySelector(`details.conv[data-conv="${CSS.escape(key)}"] .convbody`);if(!el)return;
  const c={a};el.innerHTML=convHist[key].list.map(x=>convLine(x,c)).join('')||'<div class="small mut">Nothing in the log.</div>';el.scrollTop=el.scrollHeight}
// <details> toggles don't bubble: listened for on the way down.
document.addEventListener('toggle',e=>{const d=e.target;if(!d.matches?.('details.conv'))return;const k=d.dataset.conv;
  if(d.open){openConvs.add(k);if(!convHist[k]||convHist[k].last!==d.dataset.last)loadConv(k,d.dataset.a,d.dataset.b,d.dataset.last)}else openConvs.delete(k)},true);
document.addEventListener('change',e=>{
  if(e.target.id==='msgagent'){msgFilter=e.target.value;renderMessages(lastMsgs)}
  if(e.target.id==='msgnosched'){try{localStorage.setItem('pl.msg.nosched',e.target.checked?'1':'0')}catch(err){}renderMessages(lastMsgs)}
  if(e.target.id==='msgspace'){spaceFilter=e.target.value;msgFilter='';$('#msgagent').value='';renderMessages(lastMsgs)}});
// What a render shows; the page is redrawn only when this changes, so it never flickers or loses your place.
let lastView='',lastPanel='';
// What the page shows comes in one snapshot: pushed by /api/stream as it changes, or fetched when the stream is down.
let lastSnap=null,streamOk=false,live=null;
async function refresh(force,snap){
  if(signedOut)return;
  if(!snap){snap=await api('/api/snapshot');lastSnap=snap}
  const {status:st,peers:p,messages:m,tasks:tk,issues:fb,inbox:ib}=snap;
  allTasks=tk.tasks||[];allFeedback=fb.issues||[];inbox=ib.items||[];allSched=snap.schedules?.schedules||[];
  const lim=st.limit;$('#limitbar').style.display=lim?'block':'none';
  $('#limitbar').innerHTML=lim?`<b>⏸️ Usage limit reached</b> (${lim.resets_at?'resets '+new Date(lim.resets_at*1000).toLocaleString([], {weekday:'short',hour:'numeric',minute:'2-digit'}):'no reset time given'}; hit by ${esc(lim.by)}). Agents hold their messages and schedules wait. <button class="ghost small" data-limresume>Resume now</button> after switching accounts.`:'';
  const view=JSON.stringify([p.peers.map(({last_seen_s,...x})=>x),p.leads,m.messages.map(x=>[x.id,x.status]),allTasks.map(t=>[t.id,t.status,t.owner,t.note]),allFeedback.map(f=>[f.id,f.status,f.note]),inbox.map(i=>i.id),allSched.map(c=>[c.id,c.status,c.runs,c.note]),sel,panelTab,cfg.sessions]);
  if(view===lastView&&!force)return;lastView=view;
  $('#hostline').textContent=`${st.host} · daemon ${st.version}`;
  $('#browsers').textContent=`${st.browsers} signed in · links work once and expire after 60 s`;
  $('#keypath').textContent=st.home+'/key';
  const groups={};for(const x of p.peers)(groups[x.space||'default']??=[]).push(x);
  const lbl=n=>n==='default'?'unassigned':n;
  const names=[...new Set(p.peers.map(x=>x.space||'default'))];
  // An org chart. A space is a box of its own agents; the lead of a space inside it sits in that box too, with
  // its team's box hanging beneath it. Charts with teams in them come first, so the top of the org is at the top.
  // Two sessions open in a lead's folder are both leads: all sit in the parent's box, the team under the first.
  // A lead that's offline keeps its place at the top of its space, greyed, so you can see who work waits for.
  leadOf=p.leads||{};
  const ghost=(n,l)=>({id:'',name:l.name,cwd:l.cwd,space:n,parent:l.parent&&l.parent!==n?l.parent:'',leads:l.how,offline:true,summary:'offline; work for the space waits for it'});
  const ghosts=Object.entries(leadOf).filter(([n,l])=>!l.live).map(([n,l])=>ghost(n,l));
  const inside={},kids={},leads={};for(const x of [...p.peers,...ghosts])if(x.parent){(leads[x.space]??=[]).push(x);if(!inside[x.space]){inside[x.space]={parent:x.parent,lead:x.name,peer:x};(kids[x.parent]??=[]).push(x.space)}}
  for(const n in inside)inside[n].lead=leads[n].map(x=>x.name).join(', ');
  for(const n of Object.keys(kids))if(!groups[n])groups[n]=[];
  // An agent you added to other spaces shows once, in its own space, with a badge for each space it also joined.
  // The Spaces chart still names it in each of them, with its own space beside it.
  const joined={};for(const x of p.peers)for(const n of x.also||[])(joined[n]??=[]).push(x);
  const size=(n,seen=new Set())=>{if(seen.has(n))return 0;seen.add(n);return (groups[n]||[]).length+(kids[n]||[]).reduce((t,c)=>t+size(c,seen),0)};
  const order=(a,b)=>a==='default'?1:b==='default'?-1:((kids[b]||[]).length>0)-((kids[a]||[]).length>0)||size(b)-size(a)||a.localeCompare(b);
  const roots=Object.keys(groups).filter(n=>!inside[n]||n===inside[n].parent).sort(order);
  // A lead of a space at the top sits pinned above its members, who hang beneath it.
  const tops=n=>[...(groups[n]||[]).filter(x=>x.leads&&!x.parent),...ghosts.filter(x=>x.space===n&&!x.parent)].sort(rank);
  // A space's box: its members, then the leads of the spaces under it.
  const isLead=(x,n)=>!!(inside[n]&&inside[n].parent!==n&&x.parent&&x.space===n);
  const members=n=>[...groups[n].filter(x=>!isLead(x,n)&&!(x.leads&&!x.parent)).sort(rank).sort((a,b)=>a.name.localeCompare(b.name)),
    ...(kids[n]||[]).filter(c=>c!==n).sort().map(c=>inside[c].peer)];
  const short=d=>{const t=tilde(d).split('/');return t.length>3?'…/'+t.slice(-2).join('/'):t.join('/')};
  const state=x=>(x.busy?'working':'idle')+(x.busy_since?' '+ago(x.busy_since):'');
  // Messages sent in the last hour, flagged from 20: each one can cost its recipient a turn.
  const rate=x=>x.sent_hour?`<span class="${x.sent_hour>=20?'hot':'mut'}" title="messages sent in the last hour">${x.sent_hour}/h</span>`:'';
  // Context as a small meter: how full the agent's window is, its number on hover.
  // The list shows a few words; the side panel the whole summary. Older plugins send no headline: the first sentence, cut.
  const headline=x=>{if(x.headline)return x.headline;const first=String(x.summary||'').split(/(?<=[.;!?])\s|\s[—–-]\s/)[0].trim().replace(/[;:,]$/,'');
    return first.length>80?first.slice(0,first.lastIndexOf(' ',78)>40?first.lastIndexOf(' ',78):78)+'…':first};
  const ctx=x=>x.context==null?'':`<span class="cmeter${x.context>=80?' hi':x.context>=50?' mid':''}" title="${x.context}% of its context window in use"><i style="width:${Math.min(100,x.context)}%"></i></span>`;
  // A lead is told apart by its outline (and says which team on hover), so every card keeps its context in one place.
  // The badge sits in the name row, left of the context; an offline lead's clear link goes beneath.
  // An implicit lead (it was alone in the space) has a dashed badge, and says so on hover, so the name keeps its room.
  const badge=x=>(x.also||[]).map(n=>`<span class="badge guest" title="Also a member of ${esc(n)}">+${esc(n)}</span>`).join('')+(x.leads?`<span class="badge${x.leads==='only'?' only':''}" title="${x.leads==='only'?'Lead (only agent): it was the only agent in the space':'Lead'}">Lead</span>`:'');
  const clearOff=x=>x.leads&&x.offline?`<div><span class="small mut" data-clearlead="${esc(x.space)}" title="Clear this lead" style="cursor:pointer">clear</span></div>`:'';
  const node=x=>`<${x.offline?'div':'button'} class="node${x.leads?' is-lead':''}${x.offline?' off':''}${x.id&&x.id===sel?' sel':''}"${x.offline?'':` data-sel="${esc(x.id)}"`}${x.leads?` title="Leads the ${esc(x.space)} space${x.offline?' (offline)':''}"`:''}>
    <div class="nm"><span title="${esc(x.name)}">${x.offline?'':`<span class="dot ${x.busy?'busy':''}"></span>`}${esc(x.name)}</span><span class="rt">${badge(x)}</span></div>${clearOff(x)}
    <div class="dir" title="${esc(tilde(x.cwd))}${x.branch?' · '+esc(x.branch):''}">${esc(short(x.cwd))}${x.branch?` · ⎇ ${esc(x.branch)}`:''}</div><div class="sum" title="${esc(x.summary||x.role||'')}">${esc(headline(x)||x.role)||'no summary yet'}</div>${x.offline?'':`<div class="state"><span>${state(x)}${x.sent_hour?' · '+rate(x):''}${x.open_tasks?` · <span style="color:var(--acc)">${x.open_tasks} task${x.open_tasks>1?'s':''}</span>`:''}</span>${ctx(x)}</div>`}</${x.offline?'div':'button'}>`;
  // Every live agent gets its own card, two sessions in one folder included: a hidden one can't be found to message.
  const rank=(a,b)=>(b.busy-a.busy)||(a.last_seen_s-b.last_seen_s);
  const unit=(n,seen)=>{if(seen.has(n))return '';seen.add(n);
    const list=members(n),branches=list.some(x=>x.parent&&x.space!==n);
    const top=tops(n);
    const count=top.filter(x=>!x.offline).length+list.reduce((t,x)=>t+(x.parent&&x.space!==n?leads[x.space].filter(l=>!l.offline).length:x.offline?0:1),0);
    const cols=list.map(x=>x.parent&&x.space!==n&&!seen.has(x.space)
      ?`<div class="ocol"><div class="orow stack">${leads[x.space].slice().sort(rank).map(node).join('')}</div><div class="down"></div>${unit(x.space,seen)}</div>`:`<div class="ocol">${node(x)}</div>`);
    // A team with no teams under it stacks, so each lead's column stays one card wide.
    const stack=inside[n]&&inside[n].parent!==n&&!branches;
    const row=`<div class="orow${stack?' stack':''}">${cols.join('')||'<span class="mut small">no other live agents</span>'}</div>`;
    return `<div class="unit"><div class="unit-h">${esc(lbl(n))} · ${count}</div>${top.length?`<div class="orow stack">${top.map(node).join('')}</div>${cols.length?`<div class="under">${row}</div>`:''}`:row}</div>`};
  // Each space's box picks its lead and where it sits. The lead is any folder with a live agent in the space.
  const pick=(n)=>{if(n==='default')return '';const cur=leadOf[n];const folders=[...new Map((groups[n]||[]).map(x=>[x.cwd,x.name])).entries()];
    if(cur&&!folders.some(([c])=>c===cur.cwd))folders.unshift([cur.cwd,cur.name+' (offline)']);
    const others=names.concat(Object.keys(leadOf)).filter((s,i,a)=>s!==n&&s!=='default'&&a.indexOf(s)===i).sort();const up=(groups[n]||[])[0]?.space_parent??leadOf[n]?.parent??'';
    return `<div class="spick"><label>Lead<select data-splead="${esc(n)}"><option value="">${cur?'no lead':'none yet'}</option>${folders.map(([c,nm])=>`<option value="${esc(c)}" ${cur&&cur.cwd===c?'selected':''}>${esc(nm)}${cur&&cur.cwd===c&&cur.how==='only'?' (was alone)':''}</option>`).join('')}</select></label>
      <label>Inside<select data-spparent="${esc(n)}"><option value="">top level</option>${others.map(o=>`<option ${o===up?'selected':''}>${esc(o)}</option>`).join('')}</select></label></div>`};
  const sunit=(n,seen)=>{if(seen.has(n))return '';seen.add(n);
    const own=[...groups[n].filter(x=>!isLead(x,n)&&!(x.leads&&!x.parent)).map(x=>esc(x.name)),...(joined[n]||[]).map(x=>esc(x.name)+` <span class="mut">(${esc(x.space||'default')})</span>`)];
    const ledBy=inside[n]?inside[n].lead:tops(n).map(x=>x.name+(x.offline?' (offline)':'')).join(', ');
    const ch=(kids[n]||[]).filter(c=>c!==n&&!seen.has(c)).sort();
    return `<div class="ocol"><div class="sbox${ledBy?' led':''}"><div class="name">${esc(lbl(n))}</div>
      ${ledBy?`<div class="small" style="color:var(--acc)">led by ${esc(ledBy)}</div>`:''}
      <div class="small mut">${own.join(', ')||'no other agents'}</div>${pick(n)}</div>
      ${ch.length?`<div class="down"></div><div class="branch">${ch.map(c=>sunit(c,seen)).join('')}</div>`:''}</div>`};
  $('#n-agents').textContent=p.peers.length||'';$('#n-spaces').textContent=names.length||'';$('#n-msgs').textContent=m.messages.length||'';
  // Spaces caught in a parent loop have no root; they are shown at the top level after the rest.
  const all=(fn)=>{const seen=new Set();let out=roots.map(n=>fn(n,seen)).join('');for(const n of Object.keys(groups).sort())if(!seen.has(n))out+=fn(n,seen);return out};
  // The list view: the same tree as the chart, one row per agent, each space indented under the lead that speaks for it.
  const lrow=x=>{const where=[short(x.cwd),x.branch?'⎇ '+x.branch:''].filter(Boolean).map(esc).join(' · ');
    return `<${x.offline?'div':'button'} class="lrow${x.leads?' is-lead':''}${x.offline?' off':''}${x.id&&x.id===sel?' sel':''}"${x.offline?'':` data-sel="${esc(x.id)}"`}>
      <span class="lid"><span class="ln"><span title="${esc(x.name)}">${x.offline?'':`<span class="dot ${x.busy?'busy':''}"></span>`}${esc(x.name)}</span>${badge(x)}</span><span class="lwhere mono" title="${esc(tilde(x.cwd))}">${where}</span></span>
      <span class="lw${oldSummary(x)?' old':''}" title="${esc((x.summary||x.role||'')+(oldSummary(x)?` (set ${ago(x.summary_ts)} ago)`:''))}">${esc(headline(x)||x.role||(x.offline?'offline':''))}</span>
      <span class="lr">${x.busy&&!x.offline?`<span class="lbusy">${state(x)}</span>`:''}${x.sent_hour>=20?rate(x):''}${x.open_tasks?`<span class="pill acc">${x.open_tasks} task${x.open_tasks>1?'s':''}</span>`:''}${ctx(x)}</span></${x.offline?'div':'button'}>`};
  const lspace=(n,seen)=>{if(seen.has(n))return '';seen.add(n);
    const nested=inside[n]&&inside[n].parent!==n;
    const head=nested?(leads[n]||[]):tops(n);
    const own=[...groups[n].filter(x=>!isLead(x,n)&&!(x.leads&&!x.parent)).sort(rank).sort((a,b)=>a.name.localeCompare(b.name))];
    const ch=(kids[n]||[]).filter(c=>c!==n&&!seen.has(c)).sort();
    const body=own.map(lrow).join('')+ch.map(c=>lspace(c,seen)).join('');
    // Counted as the chart counts: a top-level lead and the leads of teams inside sit with this space; a team's own lead sits with its parent.
    const count=own.length+(nested?0:head.filter(x=>!x.offline).length)+ch.reduce((t,c)=>t+(leads[c]||[]).filter(x=>!x.offline).length,0);
    return `<div class="lspace"><div class="lspace-h">${esc(lbl(n))} · ${count}${nested?` <span class="small">inside ${esc(inside[n].parent)}</span>`:''}</div>
      ${head.slice().sort(rank).map(lrow).join('')}${!body?'':head.length?`<div class="lsub led">${body}</div>`:`<div class="lflat">${body}</div>`}</div>`};
  document.querySelectorAll('[data-alayout]').forEach(b=>b.classList.toggle('on',b.dataset.alayout===aLayout));
  $('#peers').classList.toggle('charts',aLayout==='chart'&&!($('#asearch').value||'').trim());
  // A search shows the matching agents as a flat list, whichever view is on.
  const aq=($('#asearch').value||'').trim().toLowerCase();
  const hits=aq?p.peers.filter(x=>[x.name,x.cwd,x.branch,x.summary,x.space].join(' ').toLowerCase().includes(aq)):null;
  $('#peers').innerHTML=!p.peers.length?'<div class="empty">No live agents. Start Claude Code with the peerlink plugin.</div>'
    :hits?`<div class="lspace-h">${hits.length} match${hits.length===1?'':'es'}</div><div class="lflat">${hits.map(lrow).join('')||'<div class="empty">No agent matches.</div>'}</div>`
    :aLayout==='list'?`<div class="ltree">${all(lspace)}</div>`
    :all((n,seen)=>{const u=unit(n,seen);return u?`<div class="chart">${u}</div>`:''});
  // Agents still on older plugin code: they keep working, but get fixes only after /reload-plugins. The daemon runs
  // the newest peerlink installed, so the fix is a reload, not an update. Each reloads itself when next idle; one
  // whose queued reload hasn't run in 5 minutes is stuck (a dialog open in its terminal, or no turn ever ending).
  const behind=p.peers.filter(x=>x.stale&&!x.remote);
  const stuck=behind.filter(x=>!x.reload_queued||Date.now()/1000-x.reload_queued>300);
  // One line, the names behind a click. Dismissed, it stays away until the set of agents on an old plugin changes.
  const staleKey=stuck.map(x=>x.name).sort().join(','),bar=$('#stalebar');let gone='';try{gone=localStorage.getItem('pl.stale.dismissed')||''}catch(e){}
  bar.style.display=stuck.length&&gone!==staleKey?'flex':'none';bar.dataset.key=staleKey;
  if(stuck.length&&bar.dataset.shown!==staleKey){bar.dataset.shown=staleKey;
    bar.innerHTML=`<details><summary><b>${stuck.length} agent${stuck.length>1?'s':''} need${stuck.length>1?'':'s'} <span class="mono">/reload-plugins</span></b> <span class="mut">for the newer peerlink plugin · show which</span></summary>
      <div class="small">${stuck.map(x=>esc(x.name)+(x.reload_queued?` <span class="mut">(reload waiting ${ago(x.reload_queued)})</span>`:'')).join(', ')}. Run <span class="mono">/reload-plugins</span> in ${stuck.length>1?'each':'it'}, or terminate it.</div></details>
      <button class="ghost small" data-stalehide title="Hide until another agent needs it">Dismiss</button>`}
  const picked=p.peers.find(x=>x.id===sel);
  const panel=picked?agentPanel(picked,p.peers.filter(y=>y.id!==picked.id&&y.cwd===picked.cwd&&y.space===picked.space)):'';
  // Never redraw the panel under someone editing it.
  if(panel!==lastPanel&&!(editing&&picked&&$('#agentform')?.dataset.id===picked.id)){$('#detail').innerHTML=panel;lastPanel=panel}
  // The open panel takes room beside the chart rather than covering part of it.
  document.body.classList.toggle('drawer',!!picked);
  // The default space is always listed: it's where every agent with no space set lands.
  const dflt=groups.default?'':`<div class="chart"><div class="orow" style="justify-content:flex-start"><div class="ocol"><div class="sbox" style="min-height:0"><div class="name">unassigned</div><div class="small mut">agents whose folder has no space · none live</div></div></div></div></div>`;
  $('#spacelist').innerHTML=`<div class="sgrid">${all((n,seen)=>{const u=sunit(n,seen);return u?`<div class="chart">${u}</div>`:''})}</div>`+dflt;
  livePeers=p.peers;lastMsgs=m.messages;renderMessages(lastMsgs);renderTasks();renderFeedback();renderInbox();renderSchedules();
}
// Schedules: what's active or waiting first; the ones that ended fold away. Each says who'd get it now.
let allSched=[],schedEdit='';
const openSched=new Set(),SCHED_ICON='<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2"/></svg>';
document.addEventListener('toggle',e=>{const d=e.target;if(!d.matches?.('details.sched'))return;d.open?openSched.add(d.dataset.sched):openSched.delete(d.dataset.sched)},true);
function renderSchedules(force){
  const live=c=>c.status==='active'||c.status==='pending';
  $('#n-sched').textContent=allSched.filter(live).length||'';
  // Pick an agent, by name: several can share a folder.
  const agents=livePeers.filter(x=>!x.remote).sort((a,b)=>a.name.localeCompare(b.name));
  const opts=(cur,extra='')=>extra+agents.map(x=>`<option value="${esc(x.name)}" ${x.name===cur?'selected':''}>${esc(x.name)} · ${esc(tilde(x.cwd))}</option>`).join('');
  const pick=$('#schedagents');if(document.activeElement!==pick){const was=pick.value;pick.innerHTML=opts(was,'<option value="">Pick an agent</option>')}
  // Never redraw a schedule under someone editing it.
  if(schedEdit&&!force)return;
  const when=t=>t?new Date(t*1000).toLocaleString([], {weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'';
  const local=t=>{const d=new Date(t*1000),z=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${z(d.getMonth()+1)}-${z(d.getDate())} ${z(d.getHours())}:${z(d.getMinutes())}`};
  const btn=(attr,id,label,cls='')=>`<button class="ghost small ${cls}" ${attr}="${esc(id)}">${label}</button>`;
  const actions=c=>btn('data-schededit',c.id,'Edit')+(c.status==='pending'?`<button class="ghost small" data-schedok="${esc(c.id)}" data-act="approve">Approve</button><button class="ghost small" data-schedok="${esc(c.id)}" data-act="deny">Deny</button>`
    :c.status==='active'?btn('data-schedcancel',c.id,'Cancel','danger'):['cancelled','denied','expired'].includes(c.status)?btn('data-schedon',c.id,'Enable again'):'');
  // Who gets it: the agent named while it runs in the folder, else the folder's agent, else a new one or none.
  const route=c=>c.goes_to?`goes to <b>${esc(c.goes_to)}</b>`
    :c.agent?`<span style="color:var(--warn)">${esc(c.agent)} isn't running</span>: ${c.start?`starts it again in ${esc(tilde(c.folder))}`:'the run waits an hour for it'}`
    :c.start?`no agent running there: starts one`:`no agent running there: waits for one`;
  const card=c=>schedEdit===c.id?`<form class="card" data-schedform="${esc(c.id)}"><div class="addline"><b>${esc(c.id)}</b><span class="chip">${esc(c.status)}</span>
      <select name="target" class="grow">${opts(c.agent,c.agent&&!agents.some(x=>x.name===c.agent)?`<option value="${esc(c.agent)}" selected>${esc(c.agent)} (not running)</option>`:!c.agent?`<option value="" selected>Any agent in ${esc(tilde(c.folder))}</option>`:'')}</select>
      <input name="when" type="text" value="${esc(c.cron||local(c.at))}" placeholder="0 9 * * 1-5, or in 2h" style="width:190px"></div>
    <textarea name="prompt" rows="4" style="margin-top:8px;width:100%">${esc(c.prompt)}</textarea>
    <input name="prompt_file" type="text" value="${esc(c.prompt_file?tilde(c.prompt_file):'')}" placeholder="Playbook file, read fresh at each run (optional)" style="margin-top:8px">
    <div class="addline" style="margin-top:8px"><label class="check small"><input type="checkbox" name="start" ${c.start?'checked':''}> Start it when it isn&#39;t running</label>
      <button type="submit">Save</button><button type="button" class="ghost" data-schededit="">Cancel</button><span class="small err" data-schedmsg></span></div></form>`
    :`<details class="plat sched" data-sched="${esc(c.id)}" ${openSched.has(c.id)||c.status==='pending'?'open':''}><summary>
      <span class="plogo" style="background:${c.status==='active'?'var(--acc)':c.status==='pending'?'var(--warn)':'var(--mut)'}">${SCHED_ICON}</span>
      <span class="ptitle"><b>${esc(c.agent||tilde(c.folder))} <span class="mut" style="font-weight:400">· ${esc(c.when_text)}</span></b><span class="psub">${esc(c.prompt.replace(/\s+/g,' ').slice(0,160))}</span></span>
      <span class="pstate">${c.status==='active'?'':`<span class="pill${c.status==='pending'?' warn':''}">${esc(c.status==='pending'?'needs approval':c.status)}</span>`}<span class="small mut">${c.next_run&&live(c)?'next '+when(c.next_run):c.runs?'last '+when(c.last_run):''}</span></span><span class="chev" aria-hidden="true"></span></summary>
    <div class="pbody" style="padding-top:12px"><div class="md">${md(c.prompt)}</div>${c.prompt_file?`<div class="small" style="margin-top:6px">Playbook <span class="mono">${esc(tilde(c.prompt_file))}</span>, read fresh at each run${c.book_err?` · <span style="color:var(--warn)">couldn't be read last run</span>`:''}</div>`:''}
    <div class="small mut" style="margin-top:8px">${esc(c.id)} · ${live(c)?route(c)+' · ':''}by ${esc(c.requester?.name||'you')}${c.agent?` · in <span class="mono">${esc(tilde(c.folder))}</span>`:''}${c.runs?` · ran ${c.runs}×, last ${when(c.last_run)}${c.note?': '+esc(c.note):''}`:''}${c.status==='pending'?' · waiting for your approval':''}</div>
    <div class="addline" style="margin-top:10px">${actions(c)}</div></div></details>`;
  const on=allSched.filter(live),ended=allSched.filter(c=>!live(c));
  $('#schedlist').innerHTML=(on.map(card).join('')||'<div class="empty">No schedules running.</div>')
    +(ended.length?`<details ${ended.some(c=>c.id===schedEdit)?'open':''}><summary class="small mut" style="cursor:pointer;margin-top:4px">Ended (${ended.length})</summary><div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">${ended.map(card).join('')}</div></details>`:'')}
document.addEventListener('click',e=>{const b=e.target.closest('[data-schededit]');if(!b)return;schedEdit=b.dataset.schededit;renderSchedules(true)});
document.addEventListener('submit',async e=>{const f=e.target.closest('[data-schedform]');if(!f)return;e.preventDefault();const v=n=>f.elements[n].value.trim();
  const w=v('when'),cron=/^\S+\s+\S+\s+\S+\s+\S+\s+\S+$/.test(w)&&!/^in\s/i.test(w)&&!/^\d{4}-/.test(w);
  const r=await post(`/api/schedules/${encodeURIComponent(f.dataset.schedform)}`,{...(v('target')?{agent:v('target')}:{}),prompt:v('prompt'),prompt_file:v('prompt_file'),...(cron?{cron:w}:{at:w}),start:f.elements.start.checked});
  if(r.error){f.querySelector('[data-schedmsg]').textContent=r.error;return}
  schedEdit='';refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-schedok]');if(!b)return;
  const r=await post('/api/inbox',{kind:'approval',id:b.dataset.schedok,action:b.dataset.act});if(r.error)alert(r.error);refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-schedon]');if(!b)return;
  const r=await post(`/api/schedules/${encodeURIComponent(b.dataset.schedon)}/enable`);if(r.error)alert(r.error);refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-schedcancel]');if(!b)return;
  if(!confirm(`Cancel ${b.dataset.schedcancel}?`))return;const r=await post(`/api/schedules/${encodeURIComponent(b.dataset.schedcancel)}/cancel`);if(r.error)alert(r.error);refresh(true)});
document.addEventListener('submit',async e=>{if(e.target.id!=='schedform')return;e.preventDefault();const f=e.target,v=n=>f.elements[n].value.trim();
  const w=v('when'),cron=/^\S+\s+\S+\s+\S+\s+\S+\s+\S+$/.test(w)&&!/^in\s/i.test(w)&&!/^\d{4}-/.test(w);
  const r=await post('/api/schedules',{agent:v('target'),prompt:v('prompt'),prompt_file:v('prompt_file'),...(cron?{cron:w}:{at:w}),start:f.elements.start.checked});
  const out=$('#schedmsg');out.className='small '+(r.error?'err':'ok');out.textContent=r.error||`Added ${r.schedule.id}`;
  if(!r.error){f.elements.prompt.value='';f.elements.prompt_file.value='';f.elements.when.value=''}refresh(true)});
// The Inbox: one row per thing waiting on you, with the action it needs.
let inbox=[];
let seenInbox=null;
// A list split into panels (Inbox by agent, Feedback by status); one you open or close stays that way across redraws.
const grpState={};
document.addEventListener('toggle',e=>{const d=e.target;if(d.matches?.('details.grp'))grpState[d.dataset.grp]=d.open},true);
const initial=n=>esc(String(n||'?').replace(/^[^a-z0-9]+/i,'').slice(0,1).toUpperCase()||'?');
const grp=(key,{title,sub='',state='',bg='var(--mut)',icon,open=true},body)=>`<details class="plat grp" data-grp="${esc(key)}" ${(grpState[key]??open)?'open':''}><summary>
  <span class="plogo" style="background:${bg}">${icon||`<b style="color:#fff">${initial(title)}</b>`}</span><span class="ptitle"><b>${esc(title)}</b><span class="psub">${sub}</span></span>
  <span class="pstate">${state}</span><span class="chev" aria-hidden="true"></span></summary><div class="pbody grpbody">${body}</div></details>`;
function renderInbox(){
  $('#n-inbox').textContent=inbox.length||'';
  document.title=inbox.length?`(${inbox.length}) Peerlink`:'Peerlink';
  // A desktop alert for each new item, when you've turned them on and the page isn't in front.
  const ids=new Set(inbox.map(i=>i.kind+i.id));
  if(seenInbox&&notifyOn()&&document.hidden)for(const i of inbox)if(!seenInbox.has(i.kind+i.id)){
    try{const n=new Notification(`Peerlink · ${i.who||'an agent'}`,{body:i.title,tag:i.kind+i.id});n.onclick=()=>{focus();location.hash='#inbox';n.close()}}catch{}}
  seenInbox=ids;
  $('#notifybtn').textContent=notifyOn()?'Desktop alerts: on':'Desktop alerts: off';
  if(document.activeElement?.closest?.('#inboxlist'))return;
  const sched=i=>i.kind==='approval'&&/^c\d+$/.test(i.id);
  const label={approval:'wants a new agent',task:'task for you',question:'waiting on your answer',feedback:'feedback'};
  const btn=(a,t)=>`<button class="ghost small" data-iact="${a}">${t}</button>`;
  const item=i=>`<div class="task" data-ikind="${esc(i.kind)}" data-iid="${esc(i.id)}"><div class="mh"><span class="tchip ${i.kind==='task'||i.kind==='approval'?'open':i.kind==='question'?'new':esc(i.type)}">${sched(i)?'wants a schedule':label[i.kind]}</span><b class="ititle">${esc(plain(i.title))}</b><span class="when">${esc(i.id)} · waiting ${ago(i.since)}</span></div>
    <div class="small mut">from <b>${esc(i.who)}</b>${i.kind==='feedback'?' · '+esc(i.type):''}</div>
    ${(()=>{// The title is the first line, cut: when it was cut, the body keeps the whole text so nothing is lost mid-word.
      const t=i.text.trim(),[first,...rest]=t.split('\n'),more=first.trim()===i.title.trim()?rest.join('\n').trim():t;return more?`<div class="ftext md">${md(more)}</div>`:''})()}
    <div class="tact">${i.kind==='approval'?`${btn('approve',sched(i)?'Approve':'Approve and start it')}${btn('deny','Deny')}`:i.kind==='task'?`<input class="small" data-itext placeholder="Note for ${esc(i.who)} (optional)">${btn('done','Done')}${btn('failed',"Couldn't do it")}`
      :i.kind==='question'?`<input class="small" data-itext placeholder="Your answer">${btn('reply','Send')}${btn('dismiss','Dismiss')}`
      :`${btn('accepted','Accept')}${btn('dismissed','Dismiss')}`}</div></div>`;
  // By who's asking, the longest waiting first; a lone item still sits in its agent's panel.
  const byWho=new Map();for(const i of inbox){const k=i.who||'an agent';if(!byWho.has(k))byWho.set(k,[]);byWho.get(k).push(i)}
  const kinds=l=>[...new Set(l.map(i=>sched(i)?'schedule':i.kind==='approval'?'new agent':i.kind))].join(', ');
  $('#inboxlist').innerHTML=[...byWho].sort((a,b)=>Math.min(...a[1].map(i=>i.since))-Math.min(...b[1].map(i=>i.since))).map(([who,l])=>grp('inbox:'+who,{title:who,sub:esc(kinds(l)),
      state:`<span class="pill warn">${l.length}</span><span class="small mut">waiting ${ago(Math.min(...l.map(i=>i.since)))}</span>`,bg:'var(--acc)'},l.map(item).join(''))).join('')
    ||'<div class="empty">Nothing is waiting on you.</div>'}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-iact]');if(!b)return;const row=b.closest('[data-iid]');
  const text=row.querySelector('[data-itext]')?.value||'';
  if(b.dataset.iact==='reply'&&!text.trim()){row.querySelector('[data-itext]').focus();return}
  if(b.dataset.iact==='approve'&&!/^c\d+$/.test(row.dataset.iid)&&!confirm('Start a new Claude Code session in that folder?'))return;
  const r=await post('/api/inbox',{kind:row.dataset.ikind,id:row.dataset.iid,action:b.dataset.iact,text});
  if(r.error)alert(r.error);document.activeElement?.blur();refresh(true)});
document.addEventListener('change',async e=>{const t=e.target;if(!t.dataset.splead&&!t.dataset.spparent)return;
  const r=t.dataset.spparent?await post(`/api/spaces/${encodeURIComponent(t.dataset.spparent)}/parent`,{parent:t.value})
    :await post(`/api/spaces/${encodeURIComponent(t.dataset.splead)}/lead`,{cwd:t.value});
  if(r.error)alert(r.error);lastView='';refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-makelead],[data-clearlead]');if(!b)return;e.stopPropagation();
  const space=b.dataset.makelead||b.dataset.clearlead;
  if(b.dataset.clearlead&&!confirm(`Clear the lead of ${space}? Work sent to the space won't reach anyone until you pick a new lead.`))return;
  const r=await post(`/api/spaces/${encodeURIComponent(space)}/lead`,{cwd:b.dataset.cwd||''});if(r.error)alert(r.error);refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-clearctx]');if(!b)return;
  const x=livePeers.find(p=>p.id===b.dataset.clearctx);if(!confirm(`Clear ${x?x.name:'this agent'}'s context? It starts a fresh conversation after the turn it's in.`))return;
  const r=await post(`/api/sessions/${encodeURIComponent(b.dataset.clearctx)}/clear`);if(r.error)alert(r.error);else{const m=$('#clearmsg');if(m)m.textContent='Asked: it clears after its current turn.'}});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-terminate]');if(!b)return;
  const x=livePeers.find(p=>p.id===b.dataset.terminate);if(!confirm(`End ${x?x.name:'this agent'}? Its session closes after the turn it's in.`))return;
  const r=await post(`/api/sessions/${encodeURIComponent(b.dataset.terminate)}/terminate`);if(r.error)alert(r.error);refresh(true)});
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches?.('#inboxlist [data-itext]')){e.preventDefault();e.target.closest('[data-iid]').querySelector('[data-iact]').click()}});
// Feedback: newest first; accepting one opens a task for peerlink's maintainer.
let allFeedback=[];
function renderFeedback(){
  $('#n-feedback').textContent=allFeedback.filter(f=>f.status==='new').length||'';
  if(document.activeElement?.closest?.('#fblist'))return;
  const st=$('#fstatus').value,ty=$('#ftype').value;
  const shown=allFeedback.filter(f=>(!st||f.status===st)&&(!ty||f.type===ty)).sort((a,b)=>b.ts-a.ts);
  const btn=(s,label)=>`<button class="ghost small" data-fact="${s}">${label}</button>`;
  const fbRow=f=>{const task=allTasks.find(t=>t.id===f.task);let [head,...rest]=f.text.split('\n');
    // A long first line is cut to a headline; the rest of it reads below.
    if(head.length>120){const cut=head.lastIndexOf(' ',120)>60?head.lastIndexOf(' ',120):120;rest=[head.slice(cut).trim(),...rest];head=head.slice(0,cut)+'…'}
    return `<div class="task" data-fb="${esc(f.id)}"><div class="mh"><span class="tchip ${esc(f.type)}">${esc(f.type)}</span><span class="tchip ${esc(f.status)}">${esc(f.status)}</span><b>${esc(head)}</b><span class="when">${esc(f.id)} · ${ago(f.ts)} ago</span></div>
      <div class="small mut">${esc(f.from_name||'an agent')}${f.space?' · '+esc(f.space):''} · plugin ${esc(f.plugin||'?')}, daemon ${esc(f.daemon||'?')}${task?` · task ${esc(task.id)} ${esc(TSTATE[task.status]||task.status)} with <b>${esc(task.owner)}</b>`:''}</div>
      ${rest.join('\n').trim()?`<div class="ftext md">${md(rest.join('\n').trim())}</div>`:''}
      <div class="tact"><input class="small" data-fnote placeholder="Note (optional)" value="${esc(f.note||'')}">
      ${f.status==='new'?btn('accepted','Accept')+btn('dismissed','Dismiss'):f.status==='accepted'?btn('done','Mark done')+btn('dismissed','Dismiss'):btn('new','Back to new')}</div></div>`};
  // All statuses: a panel each, the ones that need you open. One status picked: just its list.
  const FB={new:['New','Not looked at yet','var(--acc)',true],accepted:['Accepted','Handed to the peerlink agent as a task','var(--warn)',true],done:['Done','Fixed or built','var(--ok)',false],dismissed:['Dismissed','Not acted on','var(--mut)',false]};
  $('#fblist').innerHTML=(st?shown.map(fbRow).join(''):Object.entries(FB).map(([k,[title,sub,bg,open]])=>{const l=shown.filter(f=>f.status===k);
      return l.length?grp('fb:'+k,{title,sub,bg,open,state:`<span class="pill">${l.length}</span>`},l.map(fbRow).join('')):''}).join(''))
    ||`<div class="empty">${allFeedback.length?'Nothing here.':'No feedback yet.'}</div>`}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-fact]');if(!b)return;const row=b.closest('[data-fb]');
  const r=await post('/api/issues/'+encodeURIComponent(row.dataset.fb),{status:b.dataset.fact,note:row.querySelector('[data-fnote]').value});
  if(r.error)alert(r.error);document.activeElement?.blur();refresh(true)});
document.addEventListener('change',async e=>{const t=e.target;
  if(t.id==='fstatus'||t.id==='ftype'){renderFeedback();return}
  if(t.dataset.fnote!==undefined){const r=await post('/api/issues/'+encodeURIComponent(t.closest('[data-fb]').dataset.fb),{note:t.value});if(r.error)alert(r.error);t.blur();refresh(true)}});
// Tasks: one row each, its subtasks under it. The Tasks page and an agent's Tasks tab share the row.
let allTasks=[],panelTab='about';
const TSTATE={open:'open',in_progress:'in progress',done:'done',failed:'failed',cancelled:'cancelled'};
const isLive=t=>t.status==='open'||t.status==='in_progress';
// A task's record: what its owner reported, what it's blocked on, notes from above and what you decided.
const TKIND={progress:'progress',blocked:'blocked',note:'note',decision:'decision',closed:'closed'};
const taskLog=t=>(t.blocked&&isLive(t)?`<div class="tblocked"><b>Blocked:</b> ${esc(t.blocked)}</div>`:'')
  +((t.log||[]).length?`<details class="tlog" data-tlog="${esc(t.id)}" ${openLogs.has(t.id)?'open':''}><summary>Record (${t.log.length})</summary>${t.log.slice().reverse().map(e=>`<div class="te ${esc(e.kind)}"><span class="k">${esc(TKIND[e.kind]||e.kind)} · ${esc(e.by)} · ${ago(e.ts)} ago</span><div class="md">${md(e.text)}</div></div>`).join('')}</details>`:'');
const openLogs=new Set();
document.addEventListener('toggle',e=>{const d=e.target;if(!d.dataset||!d.dataset.tlog)return;d.open?openLogs.add(d.dataset.tlog):openLogs.delete(d.dataset.tlog)},true);
// A summary unchanged for two hours may describe work that's long done: shown dimmed, with its age.
const oldSummary=x=>!!(x.summary&&x.summary_ts&&Date.now()/1000-x.summary_ts>7200);
const ago=ts=>{const d=Date.now()/1000-ts;return d<90?Math.round(d)+'s':d<5400?Math.round(d/60)+'m':d<129600?Math.round(d/3600)+'h':Math.round(d/86400)+'d'};
function taskRow(t,sub){const kids=allTasks.filter(x=>x.parent===t.id);
  return `<div class="task${sub?' sub':''}" data-task="${esc(t.id)}"><div class="mh"><span class="tchip ${esc(t.status)}">${TSTATE[t.status]||esc(t.status)}</span><b>${esc(t.title)}</b><span class="when">${esc(t.id)} · ${ago(t.created)} ago</span></div>
    <div class="small mut">${esc(t.requester?.name||'?')} → <b>${esc(t.owner)}</b>${t.space?' · '+esc(t.space):''}${kids.length?` · ${kids.filter(k=>k.status==='done').length} of ${kids.length} subtasks done`:''}${!isLive(t)?' · closed '+ago(t.updated)+' ago':''}</div>
    ${t.note?`<div class="note md">${md(t.note)}</div>`:''}${taskLog(t)}
    <div class="tact">${isLive(t)?'<button class="ghost small" data-tact="cancelled">Cancel</button>':'<button class="ghost small" data-tact="open">Reopen</button>'}${agentSelect('','data-treassign','Reassign to…')}</div></div>`}
// Open and in-progress first, then the rest, newest first within each.
const byLive=list=>list.slice().sort((a,b)=>(isLive(b)-isLive(a))||(b.created-a.created));
const withSubs=list=>{const ids=new Set(list.map(t=>t.id));return byLive(list.filter(t=>!t.parent||!ids.has(t.parent))).map(t=>taskRow(t,false)+byLive(list.filter(k=>k.parent===t.id)).map(k=>taskRow(k,true)).join('')).join('')};
// The board's layout, rows and open cards are this browser's own; storage can be missing (a private window), so every use is guarded.
const pref=(k,d)=>{try{return localStorage.getItem('peerlink.'+k)??d}catch{return d}};
var aLayout=pref('alayout','list');
const notifyOn=()=>pref('notify','')==='1'&&window.Notification&&Notification.permission==='granted';
document.addEventListener('click',async e=>{if(!e.target.closest('[data-notify]'))return;
  if(notifyOn()){try{localStorage.setItem('peerlink.notify','')}catch{}}
  else if(!window.Notification)alert('This browser has no desktop notifications.');
  else if(await Notification.requestPermission()==='granted'){try{localStorage.setItem('peerlink.notify','1')}catch{}}
  else alert('Notifications are blocked for this page. Allow them in the browser\'s site settings.');
  renderInbox()});
document.addEventListener('click',e=>{const b=e.target.closest('[data-alayout]');if(!b)return;aLayout=b.dataset.alayout;try{localStorage.setItem('peerlink.alayout',aLayout)}catch{};refresh(true)});
const keep=(k,v)=>{try{localStorage.setItem('peerlink.'+k,v)}catch{}};
let tLayout=pref('tlayout','board'),dragging=null;const openCards=new Set();
$('#tgroup').value=pref('tgroup','');
const COLS=[['open','Open'],['in_progress','In progress'],['done','Done'],['failed','Failed'],['cancelled','Cancelled']];
// What a drop on a column does: only what the web UI could already do. The owner's own statuses are never set from here.
const dropAction=(t,col)=>col==='cancelled'&&isLive(t)?'cancelled':col==='open'&&!isLive(t)?'open':'';
const reqName=t=>t.requester?.name||'?';
function taskCard(t){const kids=allTasks.filter(x=>x.parent===t.id),done=kids.filter(k=>k.status==='done').length;
  return `<div class="tcard${openCards.has(t.id)?' open':''}" draggable="true" data-task="${esc(t.id)}" data-st="${esc(t.status)}" tabindex="0" aria-label="${esc(t.title)}, ${TSTATE[t.status]}, owner ${esc(t.owner)}">
    <div class="tt">${esc(t.title)}</div>
    <div class="meta by"><span><b>${esc(t.owner)}</b> · asked by ${esc(reqName(t))}</span><span class="age" title="${new Date(t.created*1000).toLocaleString()}">${ago(t.created)}</span></div>
    ${t.parent?`<div class="meta">↳ part of ${esc(t.parent)}</div>`:''}${t.blocked&&isLive(t)?`<div class="meta" style="color:var(--warn)">blocked</div>`:''}
    ${kids.length?`<div class="meta">${done} of ${kids.length} subtasks done</div><div class="bar"><i style="width:${Math.round(100*done/kids.length)}%"></i></div>`:''}
    <div class="more">${t.space?`<div class="meta">${esc(t.id)} · ${esc(t.space)}</div>`:`<div class="meta">${esc(t.id)}</div>`}${t.note?`<div class="note md">${md(t.note)}</div>`:''}${taskLog(t)}
    <div class="tact">${isLive(t)?'<button class="ghost small" data-tact="cancelled">Cancel</button>':'<button class="ghost small" data-tact="open">Reopen</button>'}${agentSelect('','data-treassign','Reassign to…')}</div></div></div>`}
function board(list){const shut=!openCards.has('col:cancelled');
  return `<div class="board">${COLS.map(([k,label])=>{const cards=list.filter(t=>t.status===k).sort((a,b)=>b.created-a.created);
    return `<div class="col${k==='cancelled'?' shut'+(shut?'':' open'):''}" data-col="${k}"><div class="col-h">${k==='cancelled'?`<button data-tcol="cancelled" aria-expanded="${!shut}">${shut?'▸':'▾'} ${label}</button>`:label}<span>${cards.length}</span></div>${cards.map(taskCard).join('')||(k==='cancelled'?'':'<div class="none">None</div>')}</div>`}).join('')}</div>`}
function renderTasks(){
  const opts=(el,all,vals)=>{const keep=el.value;el.innerHTML=`<option value="">${all}</option>`+vals.map(n=>`<option ${n===keep?'selected':''}>${esc(n)}</option>`).join('')};
  opts($('#towner'),'Everyone',[...new Set(allTasks.map(t=>t.owner))].sort());
  opts($('#treq'),'Anyone',[...new Set(allTasks.map(reqName))].sort());
  opts($('#tspace'),'All spaces',[...new Set(allTasks.map(t=>t.space).filter(Boolean))].sort());
  const st=$('#tstatus').value,ow=$('#towner').value,rq=$('#treq').value,sp=$('#tspace').value,grp=$('#tgroup').value;
  const picked=allTasks.filter(t=>(!ow||t.owner===ow)&&(!rq||reqName(t)===rq)&&(!sp||t.space===sp));
  $('#n-tasks').textContent=allTasks.filter(isLive).length||'';
  document.querySelectorAll('[data-tlayout]').forEach(b=>b.classList.toggle('on',b.dataset.tlayout===tLayout));
  $('#tstatusw').hidden=tLayout!=='list';$('#tgroupw').hidden=tLayout!=='board';
  // Never redraw under an open reassign menu or a card being dragged.
  if(dragging||document.activeElement?.matches?.('#tasklist [data-treassign]'))return;
  const none=`<div class="empty">${allTasks.length?'No tasks match.':'No tasks yet.'}</div>`;
  if(tLayout==='list'){$('#tasklist').innerHTML=withSubs(picked.filter(t=>st==='live'?isLive(t):!st||t.status===st))||none;return}
  if(!picked.length){$('#tasklist').innerHTML=none;return}
  const key=grp==='space'?t=>t.space||'no space':grp==='owner'?t=>t.owner:null;
  $('#tasklist').innerHTML=key?[...new Set(picked.map(key))].sort().map(g=>`<div class="lane-h">${esc(g)}</div>${board(picked.filter(t=>key(t)===g))}`).join(''):board(picked)}
// A board card opens in place to show its note and the same buttons as a list row, so nothing needs a drag.
document.addEventListener('click',e=>{const h=e.target.closest('[data-tcol]');if(h){const k='col:'+h.dataset.tcol;openCards.has(k)?openCards.delete(k):openCards.add(k);renderTasks();return}
  const c=e.target.closest('.tcard');if(!c||e.target.closest('.tact'))return;
  openCards.has(c.dataset.task)?openCards.delete(c.dataset.task):openCards.add(c.dataset.task);c.classList.toggle('open')});
document.addEventListener('keydown',e=>{const c=e.target.closest?.('.tcard');if(c&&e.target===c&&(e.key==='Enter'||e.key===' ')){e.preventDefault();c.click()}});
document.addEventListener('click',e=>{const b=e.target.closest('[data-tlayout]');if(!b)return;tLayout=b.dataset.tlayout;keep('tlayout',tLayout);renderTasks()});
// Dragging: the columns and agents a card may go to light up; anything else refuses the drop.
document.addEventListener('dragstart',e=>{const c=e.target.closest?.('.tcard');if(!c)return;
  dragging=allTasks.find(t=>t.id===c.dataset.task);if(!dragging)return;c.classList.add('dragging');e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',dragging.id);
  document.querySelectorAll('.col').forEach(col=>col.classList.toggle('can',!!dropAction(dragging,col.dataset.col)));
  const agents=agentFolders().filter(a=>a.cwd!==dragging.owner_cwd);
  $('#tdrop').innerHTML=agents.length?'Hand it to: '+agents.map(a=>`<span class="agent" data-dropcwd="${esc(a.cwd)}">${esc(a.name)}</span>`).join(''):'';$('#tdrop').classList.toggle('on',agents.length>0)});
const target=e=>{if(!dragging)return null;const a=e.target.closest?.('[data-dropcwd]');if(a)return a;const col=e.target.closest?.('.col');return col&&dropAction(dragging,col.dataset.col)?col:null};
document.addEventListener('dragover',e=>{const t=target(e);document.querySelectorAll('.over').forEach(x=>x!==t&&x.classList.remove('over'));if(!t)return;e.preventDefault();t.classList.add('over')});
document.addEventListener('dragend',()=>{dragging=null;$('#tdrop').classList.remove('on');document.querySelectorAll('.can,.over,.dragging').forEach(x=>x.classList.remove('can','over','dragging'));renderTasks()});
document.addEventListener('drop',async e=>{const t=target(e),task=dragging;if(!t||!task)return;e.preventDefault();dragging=null;
  let body;
  if(t.dataset.dropcwd){if(!confirm(`Hand task ${task.id} to ${t.textContent}? ${task.owner} is told to stop.`))return;body={owner:t.dataset.dropcwd}}
  else{const st=dropAction(task,t.dataset.col);if(st==='cancelled'&&!confirm(`Cancel task ${task.id}? Its owner is told to stop.`))return;body={status:st}}
  // expect: refused if the owner changed the task since the board was drawn, so its own update is never overridden.
  const r=await post('/api/tasks/'+encodeURIComponent(task.id),{...body,expect:task.status});if(r.error)alert(r.error);refresh(true)});
function panelTasks(x){
  const mine=allTasks.filter(t=>t.owner_cwd&&t.owner_cwd===x.cwd),asked=allTasks.filter(t=>t.requester?.id===x.id||(x.cwd&&t.requester?.cwd===x.cwd));
  const sec=(title,list,none)=>`<div class="k small">${title}</div>${list.length?byLive(list).map(t=>taskRow(t,false)).join(''):`<div class="small mut">${none}</div>`}`;
  // Its schedules: named for it, or for its folder with no agent named.
  const sched=allSched.filter(c=>(c.status==='active'||c.status==='pending')&&(c.goes_to?c.goes_to===x.name:c.folder===x.cwd));
  const srow=c=>`<div class="small" style="margin:4px 0"><b>${esc(c.id)}</b> ${esc(c.when_text)}${c.agent?'':' <span class="mut">(its folder)</span>'}: ${esc(c.prompt.length>120?c.prompt.slice(0,119)+'…':c.prompt)}${c.status==='pending'?` <span class="mut">waiting for your approval</span> <button class="ghost small" data-schedok="${esc(c.id)}" data-act="approve">Approve</button><button class="ghost small" data-schedok="${esc(c.id)}" data-act="deny">Deny</button>`:''}</div>`;
  return `<div class="ptasks">${sec('Owns',mine,'No tasks for this agent.')}${sec('Asked for',asked,'It hasn\'t handed out any tasks.')}<div class="k small">Schedules</div>${sched.length?sched.map(srow).join(''):'<div class="small mut">None. Add one on the Schedules page.</div>'}</div>`}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-ptab],[data-tact]');if(!b)return;
  if(b.dataset.ptab){panelTab=b.dataset.ptab;editing=false;refresh(true);return}
  const id=b.closest('[data-task]').dataset.task,was=allTasks.find(t=>t.id===id)?.status;
  if(b.dataset.tact==='cancelled'&&!confirm(`Cancel task ${id}? Its owner is told to stop.`))return;
  const r=await post('/api/tasks/'+encodeURIComponent(id),{status:b.dataset.tact,expect:was});if(r.error)alert(r.error);refresh(true)});
document.addEventListener('change',async e=>{const t=e.target;
  if(['tstatus','towner','treq','tspace','tgroup'].includes(t.id)){if(t.id==='tgroup')keep('tgroup',t.value);renderTasks();return}
  if(t.dataset.treassign!==undefined&&t.value){const id=t.closest('[data-task]').dataset.task;
    const r=await post('/api/tasks/'+encodeURIComponent(id),{owner:t.value});if(r.error)alert(r.error);t.blur();refresh(true)}});
// You, to an agent or a space's lead, from the panel's message box or the Tasks page's New task form.
// The panel's note outlives the redraw a send causes.
let sentNote={};
document.addEventListener('submit',async e=>{const f=e.target;if(f.id!=='msgform'&&f.id!=='newtask')return;e.preventDefault();
  const text=f.elements.text.value.trim(),out=f.id==='msgform'?$('#sendmsg'):$('#newtaskmsg');if(!text)return;
  const body=f.id==='msgform'?{to:f.dataset.to,text,kind:f.elements.astask.checked?'task':'message'}:{to:f.elements.to.value,text,kind:'task'};
  const r=await post('/api/web/send',body);
  const note=r.error||`Sent to ${r.to}${r.task?` as task ${r.task}`:''}.`;
  if(f.id==='msgform'){sentNote={to:body.to,text:note,err:!!r.error};setTimeout(()=>{if(sentNote.text===note){sentNote={};refresh(true,lastSnap||undefined)}},4000)}
  if(r.error){out.className='small err';out.textContent=r.error;return}
  f.elements.text.value='';editing=false;out.className='small ok';out.textContent=note;
  if(f.id==='newtask')setTimeout(()=>{f.hidden=true;out.textContent=''},1500);refresh(true)});
document.addEventListener('click',e=>{const b=e.target.closest('[data-newtask]');if(!b)return;const f=$('#newtask');f.hidden=!f.hidden;if(f.hidden)return;
  // Agents by name, and each space that has a lead, which hands the work on.
  const spaces=Object.entries(leadOf).filter(([n])=>n!=='default').sort();
  f.elements.to.innerHTML=`<option value="">Choose who it's for…</option>`+(spaces.length?`<optgroup label="A space's lead">${spaces.map(([n,l])=>`<option value="space:${esc(n)}">${esc(n)} (lead: ${esc(l.name)}${l.live?'':', offline'})</option>`).join('')}</optgroup>`:'')
    +`<optgroup label="An agent">${livePeers.filter(x=>!x.remote).sort((a,b)=>a.name.localeCompare(b.name)).map(x=>`<option value="${esc(x.id)}">${esc(x.name)} · ${esc(x.space||'default')}</option>`).join('')}</optgroup>`;
  f.elements.text.focus()});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-copy]');if(!b)return;e.stopPropagation();
  try{await navigator.clipboard.writeText(b.dataset.copy);b.textContent='Copied'}catch{b.textContent='Copy failed'}setTimeout(()=>{b.textContent='Copy'},1200)});
document.addEventListener('input',e=>{if(e.target.closest('#agentform,#msgform'))editing=true;
  if(e.target.id==='asearch'&&lastSnap)refresh(true,lastSnap);if(e.target.id==='msgq')renderMessages(lastMsgs)});
document.addEventListener('change',e=>{if(e.target.closest('#agentform,#msgform'))editing=true});
// Save applies only what changed, in one go, and says what happened. Details and Controls each show part of the
// agent's settings in this one form, so a field the open tab doesn't show is left alone.
document.addEventListener('submit',async e=>{if(e.target.id!=='agentform')return;e.preventDefault();
  const f=e.target,d=f.dataset,id=d.id,msg=$('#agentmsg'),has=n=>!!f.elements[n],v=n=>f.elements[n].value.trim();
  const patch={},errs=[];
  if(has('nickname')&&v('nickname')!==d.nick)patch.nickname=v('nickname');
  if(has('wake')&&f.elements.wake.value!==d.wake)patch.auto_wake=f.elements.wake.value==='default'?null:f.elements.wake.value==='on';
  if(has('muted')&&f.elements.muted.checked!==!!d.muted)patch.muted=f.elements.muted.checked;
  if(has('auto_approve')&&!f.elements.auto_approve.disabled&&f.elements.auto_approve.checked!==!!d.auto)patch.auto_approve=f.elements.auto_approve.checked;
  if(has('role')&&v('role')!==d.role)patch.role=v('role');
  msg.className='small';msg.textContent='Saving…';
  if(Object.keys(patch).length){const r=await post(`/api/sessions/${encodeURIComponent(id)}/config`,patch);if(r.error)errs.push(r.error);else cfg.sessions=r.sessions}
  if(has('space')&&v('space')!==d.space){const r=await post('/api/folders/space',{cwd:d.cwd,space:v('space')});if(r.error)errs.push(r.error)}
  const also=has('also')?v('also').split(',').map(t=>t.trim()).filter(Boolean):[];
  if(has('also')&&also.join(', ')!==d.also){const r=await post('/api/members',{cwd:d.cwd,spaces:also});if(r.error)errs.push(r.error)}
  editing=false;lastPanel='';await refresh(true);
  const m2=$('#agentmsg');if(m2){m2.className='small '+(errs.length?'err':'ok');m2.textContent=errs.length?errs.join(' '):'Saved'}});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t)return;
  if(t.dataset.sel!==undefined){sel=sel===t.dataset.sel?'':t.dataset.sel;editing=false;refresh(true);return}
  if(t.dataset.close!==undefined){sel='';editing=false;refresh(true);return}
  const act=t.dataset.act;if(!act)return;
  if(act==='logout'){await post('/api/web/logout');location.reload()}
  if(act==='logout-all'&&confirm('Sign out every browser, including this one?')){await post('/api/web/logout',{all:true});location.reload()}
  if(act==='rotate'&&confirm('Rotate the key? Every browser is signed out; agents pick up the new key by themselves.')){await post('/api/key/rotate');location.reload()}
  if(act==='restart'){await post('/api/restart');$('#ctlmsg').textContent='Restarting…';setTimeout(()=>location.reload(),2500)}});
// -- Discord: the setup walkthrough and the channel-to-agent mapping --------
let dState=null,dGuilds=null,dDraft=null,dBusy=false;
const agentFolders=()=>{const m=new Map();for(const p of livePeers)if(!p.remote&&!m.has(p.cwd))m.set(p.cwd,{cwd:p.cwd,name:p.name,space:p.space});return [...m.values()].sort((a,b)=>a.name.localeCompare(b.name))};
const folderLabel=cwd=>{const a=agentFolders().find(x=>x.cwd===cwd);return a?`${a.name} · ${a.space}`:`${(cfg.folder_names||{})[cwd]||cwd.split('/').filter(Boolean).pop()||cwd} (not running)`};
// A channel maps agents by folder and name, so where several share a folder each is its own pick.
const liveAgents=()=>{const m=new Map();for(const p of livePeers)if(!p.remote&&!m.has(p.cwd+'\t'+p.name))m.set(p.cwd+'\t'+p.name,{cwd:p.cwd,name:p.name,space:p.space});return [...m.values()].sort((a,b)=>a.name.localeCompare(b.name))};
const entryLabel=e=>{if(!e.name)return folderLabel(e.cwd);const a=liveAgents().find(x=>x.cwd===e.cwd&&x.name.toLowerCase()===e.name.toLowerCase());return a?`${a.name} · ${a.space}`:`${e.name} (not running)`};
function entrySelect(attrs,placeholder){return `<select ${attrs}><option value="">${placeholder}</option>${liveAgents().map(o=>`<option value="${esc(o.cwd+'\t'+o.name)}">${esc(o.name+' · '+o.space)}</option>`).join('')}</select>`}
function agentSelect(val,attrs,placeholder){const opts=agentFolders();const extra=val&&!opts.some(o=>o.cwd===val)?[{cwd:val}]:[];
  return `<select ${attrs}><option value="">${placeholder}</option>${[...opts,...extra].map(o=>`<option value="${esc(o.cwd)}" ${o.cwd===val?'selected':''}>${esc(folderLabel(o.cwd))}</option>`).join('')}</select>`}
function channelSelect(val,attrs){const gs=dGuilds||[];
  return `<select ${attrs}><option value="">Pick a channel…</option>${gs.map(g=>`<optgroup label="${esc(g.name)}">${g.channels.map(c=>`<option value="${esc(c.id)}" ${c.id===val?'selected':''}>${g.id==='telegram'||g.id==='slack'?'':'#'}${esc(c.name)}</option>`).join('')}</optgroup>`).join('')}</select>`}
const chanName=id=>{for(const g of dGuilds||[])for(const c of g.channels)if(c.id===id)return (g.id==='telegram'||g.id==='slack'?'':'#')+c.name;return 'channel '+id};
async function loadDiscord(check){
  dState=await api('/api/discord'+(check?'?check=1':''));
  if(dGuilds===null){const r=await api('/api/discord/channels');dGuilds=r.guilds||[]}
  if(!dDraft)dDraft={routes:JSON.parse(JSON.stringify(dState.routes||[])),dm:dState.dm||''};
  renderDiscord()}
const DISCORD_LOGO='<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M20.317 4.37a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.865-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.74 19.74 0 0 0 3.677 4.37a.07.07 0 0 0-.032.028C.533 9.046-.32 13.58.099 18.058a.082.082 0 0 0 .031.056 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.1 13.1 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.291a.074.074 0 0 1 .078-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.009c.12.099.246.198.373.292a.077.077 0 0 1-.007.128 12.3 12.3 0 0 1-1.873.891.077.077 0 0 0-.041.107c.36.698.772 1.363 1.225 1.993a.076.076 0 0 0 .084.029 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.331c-1.183 0-2.157-1.086-2.157-2.42 0-1.333.956-2.418 2.157-2.418 1.21 0 2.176 1.095 2.157 2.419 0 1.333-.956 2.419-2.157 2.419zm7.975 0c-1.183 0-2.157-1.086-2.157-2.42 0-1.333.955-2.418 2.157-2.418 1.21 0 2.176 1.095 2.157 2.419 0 1.333-.946 2.419-2.157 2.419z"/></svg>',TG_LOGO='<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M4.6 11.6l13.1-5.05c.6-.22 1.13.15.93 1.06l-2.23 10.5c-.17.75-.6.93-1.23.58l-3.4-2.5-1.64 1.58c-.18.18-.33.33-.68.33l.24-3.46 6.3-5.69c.27-.24-.06-.38-.43-.14l-7.78 4.9-3.35-1.05c-.73-.23-.74-.73.15-1.07z"/></svg>';
// The Channels page's panels: open by default when there's something to do; your own opening or closing one sticks.
const panelSet=new Set();
function panels(want){for(const [id,open] of Object.entries(want)){const el=document.getElementById(id);if(!el||panelSet.has(id))continue;panelSet.add(id);
  let saved=null;try{saved=localStorage.getItem('pl.panel.'+id)}catch(e){}
  el.open=saved===null?open:saved==='1';el.addEventListener('toggle',()=>{try{localStorage.setItem('pl.panel.'+id,el.open?'1':'0')}catch(e){}})}}
const platState=(configured,state,label,user,error)=>!configured?'<span class="pill">Not set up</span>'
  :`<span class="dstate"><span class="dot ${state==='online'?'':'busy'}"></span><b>${esc(label)}</b>${user?` <span class="mut">as ${esc(user)}</span>`:''}${error?` · <span class="err">${esc(error)}</span>`:''}</span>`;
function renderDiscord(){if(!dState)return;const d=dState,invited=(dGuilds||[]).some(g=>!OTHER(g)),mapped=(d.routes||[]).some(r=>(r.agents||[]).length);
  const tgChats=(dGuilds||[]).some(g=>OTHER(g)&&g.channels.length);
  $('#n-discord').textContent=d.state==='online'?'on':'';
  const label={online:'Online',connecting:'Connecting…',error:'Error',off:'Turned off',unconfigured:'Not set up'}[d.state]||d.state;
  $('#dstatus').innerHTML=platState(d.configured,d.state,label,d.user,d.error);
  const nmap=(d.routes||[]).filter(r=>(r.agents||[]).length).length;$('#mapstatus').innerHTML=nmap?`<span class="pill">${nmap} mapped</span>`:'';
  panels({pdiscord:!d.configured||d.state==='error',pmap:true,popts:false});
  $('#dstep1').classList.toggle('done',d.configured);$('#dstep2').classList.toggle('done',d.configured&&invited);$('#dstep3').classList.toggle('done',(d.routes||[]).some(r=>platOf(r.channel_id)==='discord'&&(r.agents||[]).length));
  $('#tstep3').classList.toggle('done',(d.routes||[]).some(r=>r.channel_id.startsWith('tg:')&&(r.agents||[]).length));
  $('#sstep3').classList.toggle('done',(d.routes||[]).some(r=>r.channel_id.startsWith('slack:')&&(r.agents||[]).length));
  $('#dstep1b').innerHTML=d.configured?`<p class="meta">Token <span class="mono">${esc(d.token_hint)}</span> saved. <button class="ghost small" data-dact="forget">Disconnect</button></p>`
   :`<p class="meta">On the <a href="https://discord.com/developers/applications" target="_blank" rel="noreferrer">Developer Portal ↗</a>, click <b>New Application</b> and give it a name. Open <b>Bot</b>, click <b>Reset Token</b>, and copy it. Optional: on the same page, turn on <b>Message Content Intent</b> so you can talk to it without @mentioning it every time.</p>
     <form class="addline" id="dtoken"><input type="password" class="mono grow" autocomplete="off" spellcheck="false" placeholder="Bot token" name="token"><button type="submit" ${dBusy?'disabled':''}>${dBusy?'Checking…':'Connect'}</button></form><p class="small err" id="dtokerr"></p>`;
  $('#dstep2b').innerHTML=!d.configured?'':`<p class="meta">${invited?`In ${dGuilds.filter(g=>!OTHER(g)).map(g=>esc(g.name)).join(', ')}. `:`Pick your server on Discord's page, then come back here. This step completes on its own. `}
    ${d.invite_url?`<a href="${esc(d.invite_url)}" target="_blank" rel="noreferrer">${invited?'Invite to another server ↗':'<button>Invite the bot ↗</button>'}</a>`:''}</p>`;
  if(!(d.configured&&invited)&&!tgChats){$('#dstep3b').innerHTML='<p class="meta">Set up Discord, Telegram or Slack above; the channels and chats its bot can see are mapped to agents here.</p>';$('#dmore').innerHTML='';return}
  const rows=dDraft.routes.map((r,i)=>`<tr>
    <td><div class="addline" style="flex-wrap:nowrap">${r.channel_id?platLogo(r.channel_id):''}${channelSelect(r.channel_id,`data-droute="${i}" data-dfield="channel_id"`)}</div></td>
    <td><div class="kinds">${(r.agents||[]).map((a,j)=>`<div class="addline small"><b>${esc(entryLabel(a))}</b>
        <label class="small"><input type="checkbox" data-dflag="${i}:${j}:receives" ${a.receives?'checked':''}> Receives</label>
        <label class="small"><input type="checkbox" data-dflag="${i}:${j}:sends" ${a.sends?'checked':''}> Sends</label>
        <button class="ghost small" title="Remove" data-dunagent="${i}:${j}">✕</button></div>`).join('')}
      ${entrySelect(`data-daddagent="${i}"`,'+ add agent')}</div></td>
    <td style="white-space:nowrap"><button class="ghost small" data-dtest="${esc(r.channel_id)}" ${r.channel_id&&({telegram:tState,slack:sState,discord:d}[platOf(r.channel_id)]||{}).state==='online'?'':'disabled'}>Send a test</button> <button class="ghost small" data-dremove="${i}" title="Remove this channel">✕</button></td></tr>`).join('');
  $('#dstep3b').innerHTML=`<p class="meta">Your messages go to the first agent that receives. To reach another that receives, start with its name and a colon: <span class="mono">storefront: how's the build?</span> An agent that sends posts its messages and updates there; any agent answers you where you asked.</p>
    <table class="dmap"><thead><tr><th>Channel</th><th>Agents</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="3" class="mut small">No channels mapped yet.</td></tr>'}</tbody></table>
    <p class="small mut" style="margin-top:6px">Start a post with <span class="mono">Task:</span> to hand it over as a task, or <span class="mono">Announcement:</span> to tell the receiving agent's whole space.</p>
    <div class="addline" style="margin-top:8px"><button class="ghost" data-dact="addroute">+ Map a channel</button><button class="ghost" data-dact="refresh" title="Look for new servers and channels">↻ Refresh channels</button></div>
    ${d.configured?`<div class="addline" style="margin-top:12px"><label class="small mut">Direct messages to the Discord bot go to</label>${agentSelect(dDraft.dm,'data-ddm','Nobody (ignored)')}</div>`:''}
    <div class="addline" style="margin-top:12px"><button data-dact="save">Save mapping</button><span class="small" id="dsaved"></span></div>`;
  const reading=d.reads_messages?`<p class="meta">Everything you write in a mapped channel goes to its first receiving agent. No @mention needed.</p>`
    :d.message_content_switch?`<p class="meta">The Message Content switch is on in Discord, but the bot connected before it was. <button class="ghost small" data-dact="reconnect">Reconnect now</button></p>`
    :`<p class="meta">${d.message_content_switch===false?'<b>Discord says the Message Content switch is off.</b> ':''}@mention the bot each time, or DM it. To talk without @mentioning, turn on <b>Message Content Intent</b> on the <a href="${d.app_id?`https://discord.com/developers/applications/${esc(d.app_id)}/bot`:'https://discord.com/developers/applications'}" target="_blank" rel="noreferrer">Bot page ↗</a> (under Privileged Gateway Intents) and save. <button class="ghost small" data-dact="check">I turned it on</button></p>`;
  $('#dmore').innerHTML=`<h2>Talking to your agents</h2>${d.configured?reading:''}
    <p class="meta">A reaction on your message shows its progress:</p>
    <div class="legend"><span>👀</span><span>delivered to the agent</span><span>🤔</span><span>thinking</span><span>🛠️</span><span>using tools</span><span>❓</span><span>asked you something</span><span>✅</span><span>answered</span></div>
    <p class="meta" style="margin-top:12px">Agents can message you first, labelled with their name. If an agent isn't running, your message waits up to an hour for it.</p>
    <h2>What agents can send</h2>
    <label class="check"><input type="checkbox" data-dopt="buttons" ${d.buttons?'checked':''}> <span><b>Buttons</b> for choices: tap one and it goes back to the agent as your answer. Off, the choices are a numbered list you reply to.</span></label>
    <label class="check"><input type="checkbox" data-dopt="files_out" ${d.files_out?'checked':''}> <span><b>Files from agents:</b> an agent can attach files from this machine to its messages.</span></label>
    <label class="check"><input type="checkbox" data-dopt="files_in" ${d.files_in?'checked':''}> <span><b>Files to agents:</b> what you attach is saved here, under <span class="mono">~/.peerlink/files</span>, and the agent gets the paths.</span></label>
    <div class="addline" style="margin-top:8px"><label class="small mut" for="dmaxmb">Largest file, either way</label><input id="dmaxmb" type="number" min="1" max="500" step="1" value="${esc(d.max_file_mb)}" style="width:90px" data-dopt="max_file_mb"> <span class="small mut">MB (Discord allows 10 MB without Nitro; Telegram bots get files up to 20 MB)</span></div>
    ${d.configured?`<h2>Discord: people who can talk to agents</h2>
    <p class="meta">Only you can, as the bot's owner (or its team's members). Add other people's user IDs here, separated by commas: they can then message agents and answer buttons as you do. To find one, turn on Developer Mode in Discord, right-click the person and choose Copy User ID.</p>
    <div class="addline"><input id="dusers" class="mono" placeholder="e.g. 1562000000000000000" value="${esc((d.allowed_users||[]).join(', '))}" data-dusers style="flex:1;min-width:0"></div>
    <h2>Discord: bots that can post to agents</h2>
    <p class="meta">Other bots are ignored unless listed here. Add their user IDs, separated by commas. To find one, turn on Developer Mode in Discord, right-click the bot and choose Copy User ID. List only bots you control.</p>
    <div class="addline"><input id="dbots" class="mono" placeholder="e.g. 1562000000000000000" value="${esc((d.trusted_bots||[]).join(', '))}" data-dbots style="flex:1;min-width:0"></div>
    <h2>Discord bot</h2>
    <label class="check"><input type="checkbox" data-dact="enabled" ${d.enabled?'checked':''}> Bot running</label>`:''}`}
document.addEventListener('click',e=>{if(!e.target.closest('[data-stalehide]'))return;const bar=$('#stalebar');
  try{localStorage.setItem('pl.stale.dismissed',bar.dataset.key)}catch(e){}bar.style.display='none'});
document.addEventListener('click',e=>{if(e.target.closest('[data-limresume]'))post('/api/limit/resume',{}).then(()=>refresh(true))});
document.addEventListener('submit',async e=>{if(e.target.id!=='dtoken')return;e.preventDefault();const t=e.target.token.value.trim();if(!t)return;
  dBusy=true;renderDiscord();const r=await api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({token:t})});dBusy=false;
  if(r.error){renderDiscord();$('#dtokerr').textContent=r.error;return}dGuilds=null;dDraft=null;await loadDiscord()});
document.addEventListener('change',e=>{const t=e.target;if(!dDraft)return;
  if(t.dataset.droute!==undefined){dDraft.routes[+t.dataset.droute][t.dataset.dfield]=t.value;renderDiscord()}
  if(t.dataset.daddagent!==undefined&&t.value){const r=dDraft.routes[+t.dataset.daddagent];r.agents=r.agents||[];
    // The first agent added receives and sends; after that, one that only posts here is the likelier want.
    const [cwd,name]=t.value.split('\t');
    if(!r.agents.some(a=>a.cwd===cwd&&(a.name||'').toLowerCase()===name.toLowerCase()))r.agents.push({cwd,name,receives:!r.agents.some(a=>a.receives),sends:true});renderDiscord()}
  if(t.dataset.dflag){const [i,j,f]=t.dataset.dflag.split(':');dDraft.routes[+i].agents[+j][f]=t.checked;renderDiscord()}
  if(t.dataset.ddm!==undefined){dDraft.dm=t.value}
  if(t.dataset.dopt){const v=t.type==='checkbox'?t.checked:Number(t.value);api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({[t.dataset.dopt]:v})}).then(()=>loadDiscord())}
  if(t.dataset.dbots!==undefined)api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({trusted_bots:t.value})}).then(()=>loadDiscord());
  if(t.dataset.dusers!==undefined)api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({allowed_users:t.value})}).then(()=>loadDiscord());
  if(t.dataset.dact==='enabled')api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({enabled:t.checked})}).then(()=>loadDiscord())});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t||!dDraft&&!t.dataset.dact)return;
  if(t.dataset.dremove!==undefined){dDraft.routes.splice(+t.dataset.dremove,1);renderDiscord()}
  if(t.dataset.dunagent){const [i,j]=t.dataset.dunagent.split(':').map(Number);dDraft.routes[i].agents.splice(j,1);renderDiscord()}
  if(t.dataset.dtest){const r=await post('/api/discord/test',{channel_id:t.dataset.dtest});t.textContent=r.error?'Failed':'Sent ✓';if(r.error)alert(r.error)}
  const a=t.dataset.dact;if(!a)return;
  if(a==='addroute'){dDraft.routes.push({channel_id:'',agents:[]});renderDiscord()}
  if(a==='refresh'){const r=await api('/api/discord/channels?fresh=1');dGuilds=r.guilds||[];renderDiscord()}
  if(a==='forget'&&confirm('Disconnect the Discord bot? The channel mapping is kept.')){await api('/api/discord',{method:'DELETE'});dGuilds=null;await loadDiscord()}
  if(a==='reconnect'){await post('/api/discord/reconnect');setTimeout(()=>loadDiscord(),2000)}
  if(a==='check')await loadDiscord(true);
  if(a==='save'){const bad=dDraft.routes.find(r=>!r.channel_id||!(r.agents||[]).some(a=>a.receives||a.sends));if(bad){$('#dsaved').className='small err';$('#dsaved').textContent='Every row needs a channel and an agent that receives or sends.';return}
    await api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({routes:dDraft.routes,dm:dDraft.dm})});dState=null;dDraft=null;await loadDiscord();
    $('#dsaved').className='small ok';$('#dsaved').textContent='Saved'}});
// A chat's platform from its id, and that platform's logo.
const SLACK_LOGO='<svg viewBox="0 0 24 24" aria-hidden="true"><path stroke="#fff" stroke-width="2.6" stroke-linecap="round" fill="none" d="M9 4.5v15M15 4.5v15M4.5 9h15M4.5 15h15"/></svg>';
const PLAT={discord:['Discord','#5865F2',DISCORD_LOGO],telegram:['Telegram','#26A5E4',TG_LOGO],slack:['Slack','#4A154B',SLACK_LOGO]};
const platOf=id=>String(id).startsWith('tg:')?'telegram':String(id).startsWith('slack:')?'slack':'discord';
const platLogo=id=>{const [name,color,logo]=PLAT[platOf(id)];return `<span class="mlogo" title="${name}" style="background:${color}">${logo}</span>`};
const OTHER=g=>g.id==='telegram'||g.id==='slack';
// -- Telegram: the bot's token, who may talk to it, and the chats it has heard from ------------------
let tState=null,tBusy=false;
async function loadTelegram(){const before=JSON.stringify(Object.keys(tState?.chats||{}));tState=await api('/api/telegram');renderTelegram();
  // A chat the bot heard from for the first time joins the mapping's picker.
  if(before!==JSON.stringify(Object.keys(tState.chats||{}))&&dState){dGuilds=null;loadDiscord()}}
let tShown=null;
function renderTelegram(){const t=tState;if(!t)return;
  const label={online:'Online',connecting:'Connecting…',error:'Error',off:'Turned off',unconfigured:'Not set up'}[t.state]||t.state;
  $('#tgstatus').innerHTML=platState(t.configured,t.state,label,t.user&&'@'+t.user,t.error)+((t.strangers||[]).length?` <span class="pill warn">${t.strangers.length} waiting</span>`:'');
  // The mapping's test buttons follow the bot's state; redrawn only when it changes, not on every poll mid-edit.
  if(dState&&t.state!==tShown){tShown=t.state;renderDiscord()}
  panels({ptelegram:!t.configured||t.state==='error'||(t.strangers||[]).length>0});
  $('#tstep1').classList.toggle('done',t.configured);$('#tstep2').classList.toggle('done',t.configured&&(t.allowed_users||[]).length>0);
  $('#tstep1b').innerHTML=t.configured?`<p class="meta">Token <span class="mono">${esc(t.token_hint)}</span> saved. <label class="small"><input type="checkbox" data-tact="enabled" ${t.enabled?'checked':''}> Bot running</label> <button class="ghost small" data-tact="forget">Disconnect</button></p>`
   :`<p class="meta">In Telegram, message <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather ↗</a>, send <span class="mono">/newbot</span>, and pick a name and a username. It answers with a token: paste it here.</p>
     <form class="addline" id="ttoken"><input type="password" class="mono grow" autocomplete="off" spellcheck="false" placeholder="123456789:AAE…" name="token"><button type="submit" ${tBusy?'disabled':''}>${tBusy?'Checking…':'Connect'}</button></form><p class="small err" id="ttokerr"></p>`;
  const strangers=(t.strangers||[]).map(x=>`<div class="addline small"><b>${esc(x.name)}</b>${x.username?` @${esc(x.username)}`:''} <span class="mono mut">${esc(x.id)}</span> <span class="mut">in ${esc(x.chat_name||x.chat)}, ${ago(x.ts)} ago</span>
      <button class="small" data-tallow="${esc(x.id)}">Allow</button><button class="ghost small" data-tdismiss="${esc(x.id)}">Dismiss</button></div>`).join('');
  $('#tstep2b').innerHTML=!t.configured?'':`<p class="meta">Open <a href="https://t.me/${esc(t.user)}" target="_blank" rel="noreferrer">@${esc(t.user)} ↗</a> and send it any message. You show up here; allow yourself, and the bot answers you from then on.</p>
    ${strangers?`<p class="meta"><b>Wrote to the bot, not allowed yet:</b></p>${strangers}`:'<p class="small mut">Nobody waiting. This updates by itself.</p>'}
    <p class="meta" style="margin-top:8px">Allowed (Telegram user ids, separated by commas): they can message agents and answer buttons as you do.</p>
    <div class="addline"><input class="mono" placeholder="e.g. 123456789" value="${esc((t.allowed_users||[]).join(', '))}" data-tusers style="flex:1;min-width:0"></div>`;
  $('#tstep3b').innerHTML=!t.configured?'':`<p class="meta">Every chat the bot has heard from, your private chat with it or a group you added it to, is listed under <b>Chats and agents</b>, below.
    ${t.reads_messages?'In groups it reads every message.':'In groups, Telegram shows the bot only commands, @mentions and replies to it. To talk without @mentioning, send <span class="mono">/setprivacy</span> to @BotFather, choose Disable, then remove the bot from the group and add it again.'}
    Commands work too: <span class="mono">/status</span>, <span class="mono">/inbox</span>, <span class="mono">/peerlink run loop 5m check the deploy</span>.</p>`}
document.addEventListener('submit',async e=>{if(e.target.id!=='ttoken')return;e.preventDefault();const tok=e.target.token.value.trim();if(!tok)return;
  tBusy=true;renderTelegram();const r=await api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({token:tok})});tBusy=false;
  if(r.error){renderTelegram();$('#ttokerr').textContent=r.error;return}await loadTelegram()});
document.addEventListener('change',e=>{const t=e.target;
  if(t.dataset.tusers!==undefined)api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({allowed_users:t.value})}).then(loadTelegram);
  if(t.dataset.tact==='enabled')api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({enabled:t.checked})}).then(loadTelegram)});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t)return;
  if(t.dataset.tallow){await api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({allow:t.dataset.tallow})});loadTelegram()}
  if(t.dataset.tdismiss){await api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({dismiss:t.dataset.tdismiss})});loadTelegram()}
  if(t.dataset.tact==='forget'&&confirm('Disconnect the Telegram bot? The chat mapping and allowed people are kept.')){await api('/api/telegram',{method:'DELETE'});loadTelegram()}});
// -- Slack: the app's two tokens, who may talk to it, and the conversations it is in --------------------
let sState=null,sBusy=false,sShown=null;
async function loadSlack(){const before=JSON.stringify(sState?.chats||{});sState=await api('/api/slack');renderSlack();
  if(before!==JSON.stringify(sState.chats||{})&&dState){dGuilds=null;loadDiscord()}}
function renderSlack(){const t=sState;if(!t)return;
  const label={online:'Online',connecting:'Connecting…',error:'Error',off:'Turned off',unconfigured:'Not set up'}[t.state]||t.state;
  $('#slstatus').innerHTML=platState(t.configured,t.state,label,t.user&&`${t.user}${t.team?' in '+t.team:''}`,t.error)+((t.strangers||[]).length?` <span class="pill warn">${t.strangers.length} waiting</span>`:'');
  if(dState&&t.state!==sShown){sShown=t.state;renderDiscord()}
  panels({pslack:t.configured&&(t.state==='error'||(t.strangers||[]).length>0)});
  $('#sstep1').classList.toggle('done',t.configured);$('#sstep2').classList.toggle('done',t.configured&&(t.allowed_users||[]).length>0);
  // The token form is left alone while it's being filled in: the page polls.
  if(t.configured||!$('#stoken')||sBusy!==($('#stoken').dataset.busy==='1'))$('#sstep1b').innerHTML=t.configured?`<p class="meta">Tokens <span class="mono">${esc(t.token_hint)}</span> and <span class="mono">${esc(t.app_token_hint)}</span> saved. <label class="small"><input type="checkbox" data-sact="enabled" ${t.enabled?'checked':''}> App running</label> <button class="ghost small" data-sact="forget">Disconnect</button></p>`
   :`<p class="meta"><a href="${esc(t.manifest_url)}" target="_blank" rel="noreferrer"><button type="button">Create the app on Slack ↗</button></a> The link fills in everything the app needs; pick your workspace and click <b>Create</b>.</p>
     <p class="meta"><b>App-level token:</b> on the app's <b>Basic Information</b> page, under <b>App-Level Tokens</b>, click <b>Generate Token and Scopes</b>, add the scope <span class="mono">connections:write</span>, and copy the token (it starts <span class="mono">xapp-</span>).</p>
     <p class="meta"><b>Bot token:</b> on <b>Install App</b>, click <b>Install to Workspace</b>, allow it, and copy the <b>Bot User OAuth Token</b> (it starts <span class="mono">xoxb-</span>).</p>
     <form class="addline" id="stoken" data-busy="${sBusy?1:0}"><input type="password" class="mono grow" autocomplete="off" spellcheck="false" placeholder="App-level token, xapp-…" name="app_token"><input type="password" class="mono grow" autocomplete="off" spellcheck="false" placeholder="Bot token, xoxb-…" name="token"><button type="submit" ${sBusy?'disabled':''}>${sBusy?'Checking…':'Connect'}</button></form><p class="small err" id="stokerr"></p>`;
  const strangers=(t.strangers||[]).map(x=>`<div class="addline small"><b>${esc(x.name)}</b> <span class="mono mut">${esc(x.id)}</span> <span class="mut">in ${esc(x.chat_name||x.chat)}, ${ago(x.ts)} ago</span>
      <button class="small" data-sallow="${esc(x.id)}">Allow</button><button class="ghost small" data-sdismiss="${esc(x.id)}">Dismiss</button></div>`).join('');
  if(document.activeElement?.dataset?.susers===undefined)$('#sstep2b').innerHTML=!t.configured?'':`<p class="meta">In Slack, find <b>${esc(t.user||'peerlink')}</b> under <b>Apps</b> and send it any message. You show up here; allow yourself, and it answers you from then on.</p>
    ${strangers?`<p class="meta"><b>Wrote to the app, not allowed yet:</b></p>${strangers}`:'<p class="small mut">Nobody waiting. This updates by itself.</p>'}
    <p class="meta" style="margin-top:8px">Allowed (Slack member ids, separated by commas): they can message agents and answer buttons as you do.</p>
    <div class="addline"><input class="mono" placeholder="e.g. U012AB3CD" value="${esc((t.allowed_users||[]).join(', '))}" data-susers style="flex:1;min-width:0"></div>`;
  $('#sstep3b').innerHTML=!t.configured?'':`<p class="meta">Your direct message with the app, and every channel it's in, is listed under <b>Chats and agents</b>, below. To add it to a channel, type <span class="mono">/invite @${esc(t.user||'peerlink')}</span> there; it reads every message in that channel.
    Commands work too: <span class="mono">/peerlink status</span>, <span class="mono">/peerlink inbox</span>, <span class="mono">/peerlink run loop 5m check the deploy</span>.</p>`}
document.addEventListener('submit',async e=>{if(e.target.id!=='stoken')return;e.preventDefault();const tok=e.target.token.value.trim(),app=e.target.app_token.value.trim();if(!tok||!app){$('#stokerr').textContent='Paste both tokens.';return}
  sBusy=true;const r=await api('/api/slack',{method:'PUT',headers:J,body:JSON.stringify({token:tok,app_token:app})});sBusy=false;
  if(r.error){$('#stokerr').textContent=r.error;return}await loadSlack()});
document.addEventListener('change',e=>{const t=e.target;
  if(t.dataset.susers!==undefined)api('/api/slack',{method:'PUT',headers:J,body:JSON.stringify({allowed_users:t.value})}).then(()=>{t.blur();loadSlack()});
  if(t.dataset.sact==='enabled')api('/api/slack',{method:'PUT',headers:J,body:JSON.stringify({enabled:t.checked})}).then(loadSlack)});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t)return;
  if(t.dataset.sallow){await api('/api/slack',{method:'PUT',headers:J,body:JSON.stringify({allow:t.dataset.sallow})});loadSlack()}
  if(t.dataset.sdismiss){await api('/api/slack',{method:'PUT',headers:J,body:JSON.stringify({dismiss:t.dataset.sdismiss})});loadSlack()}
  if(t.dataset.sact==='forget'&&confirm('Disconnect the Slack app? The channel mapping and allowed people are kept.')){await api('/api/slack',{method:'DELETE'});loadSlack()}});
// Someone messaging the bot, or a chat it joins, shows up without a reload.
setInterval(()=>{if(location.hash==='#channels'&&tState?.configured&&!document.hidden)loadTelegram()},4000);
setInterval(()=>{if(location.hash==='#channels'&&!document.hidden&&(sState?.configured||!sState))loadSlack()},4000);
// Invited in another tab, or back from the Developer Portal: look again when this tab gets focus.
window.addEventListener('focus',()=>{if(location.hash==='#channels'&&dState?.configured){dGuilds=null;loadDiscord(true)}});
window.addEventListener('hashchange',()=>{if(location.hash==='#discord')location.hash='#channels';if(location.hash==='#channels'){loadDiscord();loadTelegram();loadSlack()}});

// Standing rules, on the Spaces page: yours to add, change, turn off or delete; an agent's proposal to approve.
let allRules=[];
async function loadRules(){const r=await api('/api/rules');if(!r||r.error)return;allRules=r.rules||[];
  const spaces=[...new Set([...livePeers.map(x=>x.space).filter(Boolean),...allRules.map(x=>x.space).filter(x=>x!=='*')])].sort();
  const sopts=sel=>`<option value="*" ${sel==='*'?'selected':''}>Every space</option>`+spaces.map(n=>`<option value="${esc(n)}" ${sel===n?'selected':''}>${esc(n)}</option>`).join('');
  const sel=$('#rulespace');if(sel&&document.activeElement!==sel){const was=sel.value||'*';sel.innerHTML=sopts(was)}
  if(document.activeElement&&document.activeElement.closest('#rulelist'))return;
  const row=x=>x.status==='pending'?`<div class="rule" data-rule="${esc(x.id)}"><span class="pill warn">needs approval</span><span class="grow"><b>${esc(x.by)}</b> proposes for <b>${esc(x.space==='*'?'every space':x.space)}</b>: ${esc(x.text)}</span><button data-ruleok="approve">Approve</button><button class="ghost" data-ruleok="deny">Deny</button></div>`
    :`<div class="rule${x.status==='off'?' off':''}" data-rule="${esc(x.id)}"><select data-rulespace aria-label="Space">${sopts(x.space)}</select><input type="text" data-ruletext value="${esc(x.text)}" maxlength="600" aria-label="Rule ${esc(x.id)}"><span class="small mut">${esc(x.id)}${x.by?' · '+esc(x.by):''}</span><button class="ghost small" data-ruleset="${x.status==='off'?'active':'off'}">${x.status==='off'?'Turn on':'Turn off'}</button><button class="ghost small danger" data-ruledel>Delete</button></div>`;
  const shown=allRules.filter(x=>['pending','active','off'].includes(x.status)).sort((a,b)=>(a.status==='pending'?0:1)-(b.status==='pending'?0:1)||a.created-b.created);
  $('#rulelist').innerHTML=shown.map(row).join('')||'<div class="empty">No rules yet.</div>'}
const ruleOf=e=>e.target.closest('[data-rule]')?.dataset.rule;
document.addEventListener('submit',async e=>{if(e.target.id!=='ruleform')return;e.preventDefault();const f=e.target;
  const r=await post('/api/rules',{space:f.elements.space.value,text:f.elements.text.value});$('#rulemsg').textContent=r.error||'';if(!r.error)f.elements.text.value='';loadRules()});
document.addEventListener('click',async e=>{const id=ruleOf(e);if(!id)return;const b=e.target.closest('button');if(!b)return;let r;
  if(b.dataset.ruleok)r=await post('/api/inbox',{kind:'approval',id,action:b.dataset.ruleok});
  else if(b.dataset.ruleset)r=await post('/api/rules/'+encodeURIComponent(id),{status:b.dataset.ruleset});
  else if(b.dataset.ruledel!==undefined){if(!confirm('Delete this rule? Agents stop getting it from their next prompt.'))return;r=await post('/api/rules/'+encodeURIComponent(id),{delete:true})}
  if(r&&r.error)alert(r.error);b.blur();loadRules();refresh(true)});
document.addEventListener('change',async e=>{const id=ruleOf(e);if(!id)return;const t=e.target;
  const r=t.dataset.ruletext!==undefined?await post('/api/rules/'+encodeURIComponent(id),{text:t.value}):t.dataset.rulespace!==undefined?await post('/api/rules/'+encodeURIComponent(id),{space:t.value}):null;
  if(r&&r.error)alert(r.error);t.blur();loadRules()});
window.addEventListener('hashchange',()=>{if(location.hash==='#spaces')loadRules()});
setInterval(()=>{if(location.hash==='#spaces'&&!document.hidden)loadRules()},5000);
async function loadCfg(){cfg=await api('/api/config');const f=$('#cfg');
  panels({swake:true,sctx:true,sapprove:true,ssee:false,sguide:false,slimits:false,sremote:!!(cfg.remotes||[]).length});
  for(const el of f.elements){if(!el.name)continue;const v=cfg[el.name];
    if(el.type==='checkbox')el.checked=!!v;else if(el.name==='remotes')el.value=(v||[]).join('\n');else el.value=v??''}
  $('#guidance').placeholder=cfg.default_guidance||'';
}
$('#cfg').addEventListener('submit',async e=>{e.preventDefault();const f=e.target,b={};
  for(const el of f.elements){if(!el.name)continue;
    b[el.name]=el.type==='checkbox'?el.checked:el.type==='number'?Number(el.value):el.name==='remotes'?el.value.split('\n').map(s=>s.trim()).filter(Boolean):el.value}
  const r=await api('/api/config',{method:'PUT',headers:J,body:JSON.stringify(b)});
  if(r&&r.error){$('#saved').textContent='Not saved: '+r.error;return}
  cfg=r;$('#saved').textContent='Saved '+new Date().toLocaleTimeString()});
// Poll every 2 s; while the daemon is unreachable say so, back off to 10 s, and pick up again once it's back.
let misses=0;
function connect(){if(!window.EventSource||signedOut)return;live?.close();live=new EventSource('/api/stream');
  live.onopen=()=>{streamOk=true};
  live.onmessage=e=>{const d=JSON.parse(e.data);lastSnap=d;
    if(misses){misses=0;$('#offline').style.display='none';loadCfg().catch(()=>{})}
    refresh(false,d).catch(()=>{})};
  // The browser retries the stream by itself; until it's back, tick() polls.
  live.onerror=()=>{streamOk=false}}
// With the stream up, tick only redraws now and then, so "idle 12m" stays true; without it, it polls.
async function tick(){
  try{if(streamOk&&lastSnap)await refresh(true,lastSnap);else{await refresh();if(misses){misses=0;$('#offline').style.display='none';await loadCfg();await refresh(true)}}}
  catch(e){if(!signedOut){misses++;$('#offline').style.display='block'}}
  if(signedOut){live?.close();return}
  setTimeout(tick,streamOk?30000:misses?10000:2000)}
show();loadCfg().then(()=>refresh(true)).then(()=>{if(location.hash==='#discord')location.hash='#channels';else if(location.hash==='#channels'){loadDiscord();loadTelegram();loadSlack()}}).catch(()=>{misses=1;$('#offline').style.display='block'}).finally(()=>{connect();setTimeout(tick,2000)});
</script></body></html>
"""
