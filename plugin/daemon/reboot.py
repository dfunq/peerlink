"""Bringing agents back after the machine restarts.

A reboot ends every Claude Code process and nothing starts them again. The daemon keeps a list of the agents running
now (running.json beside its other state). When it starts after a reboot, it reads that list and asks you once
whether to resume them. It resumes only on your yes.

This module holds the parts that don't need the daemon's state: when this machine booted, which sessions count as
running, and the optional start-at-login entry (a LaunchAgent on macOS, a Run value on Windows, an autostart file on
Linux). Python stdlib only.
"""
import os
import re
import subprocess
import sys
import time

# An agent that left this long ago, or less, is still listed: a shutdown ends agents a moment before the daemon, and
# one that ends with nothing said (a closed terminal, a SIGTERM) looks the same as one the shutdown took. Once the
# daemon has outlived it by this much, it was no shutdown.
GRACE_S = 150
# How Claude Code says a session ended on purpose: /exit or Ctrl-D, and /logout. Anything else (a signal, a closed
# terminal, a shutdown) may be the reboot itself.
ON_PURPOSE = {"prompt_input_exit", "logout"}
# A list older than this isn't asked about: the machine was off, or the daemon away, too long for it to still be wanted.
SNAPSHOT_MAX_AGE_S = 7 * 86400
# Boot times closer than this are the same boot: they're read off clocks that drift.
SAME_BOOT_S = 120

LABEL = "dev.peerlink.daemon"


def boot_time():
    """When this machine last started, as a Unix time; 0 when it can't be told."""
    test = os.environ.get("PEERLINK_TEST_BOOT")
    if test:
        return float(test)
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"sec\s*=\s*(\d+)", out)
            return float(m.group(1)) if m else 0.0
        if os.name == "nt":
            import ctypes
            ticks = ctypes.windll.kernel32.GetTickCount64
            ticks.restype = ctypes.c_ulonglong
            return time.time() - ticks() / 1000.0
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    return 0.0


def rebooted(saved_boot, now_boot):
    """Whether the machine started again since a list was written at saved_boot."""
    return bool(saved_boot and now_boot and abs(now_boot - saved_boot) > SAME_BOOT_S)


def running(sessions, left, now, client=lambda rec: False):
    """The agents to bring back if the machine went down now, by agent: the sessions seen within GRACE_S, and those
    that left within it without ending on purpose. Not an agent you ended (Terminate), not an MCP client outside
    Claude Code. For each agent, its newest session (a /clear leaves the old one behind for a while)."""
    out = {}
    recs = [r for r in sessions.values() if now - r.get("last_seen", 0) < GRACE_S]
    recs += [r for r in left.values() if now - r.get("left", 0) < GRACE_S]
    for r in recs:
        if r.get("terminate") or client(r) or not r.get("cwd") or not r.get("id"):
            continue
        key = r.get("agent") or r["id"]
        seen = max(r.get("last_seen", 0), r.get("left", 0))
        if key in out and out[key]["seen"] >= seen:
            continue
        out[key] = {"agent": r.get("agent", ""), "sid": r["id"], "name": r.get("name", ""), "cwd": r["cwd"],
                    "terminal": r.get("terminal", ""), "space": r.get("space", ""), "seen": seen}
    return sorted(out.values(), key=lambda a: (a["name"].lower(), a["sid"]))


def to_ask(snapshot, now_boot, now):
    """The agents to ask about when the daemon starts: those in the saved list, when the machine has rebooted since it
    was written and it isn't too old. [] otherwise."""
    if not isinstance(snapshot, dict) or not rebooted(snapshot.get("boot") or 0, now_boot):
        return []
    if now - float(snapshot.get("ts") or 0) > SNAPSHOT_MAX_AGE_S:
        return []
    return [a for a in snapshot.get("agents") or [] if isinstance(a, dict) and a.get("sid") and a.get("cwd")]


# -- Start at login ------------------------------------------------------------------------------------------------

def _plist_path(home):
    return os.path.join(home, "Library", "LaunchAgents", LABEL + ".plist")


def _desktop_path(home):
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "autostart", "peerlink.desktop")


def _xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plist_text(argv):
    args = "".join(f"\n    <string>{_xml(a)}</string>" for a in argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
"""


def _win_quote(a):
    return '"' + str(a).replace('"', '\\"') + '"'


def _winreg():
    import winreg
    return winreg


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def start_command(python, daemon):
    """What runs at login: the daemon's own start, which starts it in the background and returns. On Windows through
    pythonw, so no console window opens."""
    if os.name == "nt":
        w = os.path.join(os.path.dirname(python), "pythonw.exe")
        python = w if os.path.exists(w) else python
    return [python, daemon, "start"]


def autostart_install(argv, home=None, platform=None):
    """Start the daemon at login with argv. Rewritten when it differs (a plugin update moves the daemon). '' when
    done, else why not."""
    home, platform = home or os.path.expanduser("~"), platform or sys.platform
    try:
        if platform == "win32":
            reg = _winreg()
            with reg.CreateKey(reg.HKEY_CURRENT_USER, RUN_KEY) as k:
                reg.SetValueEx(k, "peerlink", 0, reg.REG_SZ, " ".join(_win_quote(a) for a in argv))
            return ""
        path, text = ((_plist_path(home), plist_text(argv)) if platform == "darwin" else
                      (_desktop_path(home), "[Desktop Entry]\nType=Application\nName=peerlink\nExec=" + " ".join(_win_quote(a) for a in argv)
                       + "\nX-GNOME-Autostart-enabled=true\n"))
        try:
            with open(path) as f:
                if f.read() == text:
                    return ""
        except OSError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return ""
    except (OSError, ImportError) as err:
        return str(err)


def autostart_remove(home=None, platform=None):
    """Take the start-at-login entry away. '' when it's gone (or never was), else why not."""
    home, platform = home or os.path.expanduser("~"), platform or sys.platform
    try:
        if platform == "win32":
            reg = _winreg()
            try:
                with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as k:
                    reg.DeleteValue(k, "peerlink")
            except FileNotFoundError:
                pass
            return ""
        path = _plist_path(home) if platform == "darwin" else _desktop_path(home)
        if os.path.exists(path):
            os.remove(path)
        return ""
    except (OSError, ImportError) as err:
        return str(err)


def autostart_installed(home=None, platform=None):
    home, platform = home or os.path.expanduser("~"), platform or sys.platform
    if platform == "win32":
        try:
            reg = _winreg()
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as k:
                reg.QueryValueEx(k, "peerlink")
            return True
        except (OSError, ImportError):
            return False
    return os.path.exists(_plist_path(home) if platform == "darwin" else _desktop_path(home))
