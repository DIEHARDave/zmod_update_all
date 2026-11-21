#!/usr/bin/env python3
# zmod_update_all: an update queue for Zmod. Installs every available
# Moonraker update in one go and reboots as few times as possible.
#
# Runs inside Zmod's chroot (Python 3.12, standard library only), started by
# the UPDATE_ALL macro. It forks into the background straight away so Klipper
# is not blocked, and reports progress to the console through _UPDATE_ALL_MSG.
#
# Plugins are updated first, without a reboot (a plugin's update.sh that asks
# for one gets it deferred to the end, see strip_reboots). Zmod's Moonraker reboots the
# printer by itself right after updating Z-Mod, klippy or moon, so those go
# last, one per boot: the queue is saved to disk before each of them and
# _UPDATE_ALL_RESUME (a delayed_gcode at Klipper start) carries on after the
# reboot. If only plugins changed, the queue ends with a single REBOOT.
#
# Copyright (C) 2026 DIEHARDave
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import hashlib
import json
import re
import shutil
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

MOONRAKER = "http://127.0.0.1:7125"
# mod_data/log: kept across reboots and visible in Fluidd/Mainsail.
LOG_PATH = "/root/printer_data/config/mod_data/log/zmod_update_all.log"
LOCK_PATH = "/tmp/zmod_update_all.pid"
# Kept across reboots (the chroot lives on /usr/data).
QUEUE_PATH = "/root/.zmod_update_all.json"
# A queue is resumed at most this many times. (No age limit: the printer
# boots with its clock at 2026-01-01 until NTP has synced.)
MAX_RESUMES = 6

# The web UIs whose "Update all" button Zmod's forks removed. The plugin
# puts its own back by adding web/zmod_update_all.js to their index.html.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_JS = os.path.join(PLUGIN_DIR, "web", "zmod_update_all.js")
WEB_DIRS = {"mainsail": "/root/mainsail", "fluidd": "/root/fluidd"}
WEB_MARK = "<!-- zmod_update_all -->"

# Zmod's Moonraker (git_deploy.py) runs "systemctl reboot" after updating any
# of these, so each one ends the boot. Updated last, in this order.
REBOOTS = ("klippy", "moon", "z-mod", "zmod")
# Not updated here: "system" is the OS package manager, which Zmod disables,
# and Zmod's real Klipper/Moonraker repos are the "klippy" and "moon" entries.
SKIP = ("system", "klipper", "moonraker")
# Shown in the console as a reason when an item is skipped.
PROBLEMS = (
    ("is_valid", False, "repo is not valid"),
    ("is_dirty", True, "repo has local changes"),
    ("corrupt", True, "repo is corrupt"),
    ("detached", True, "repo is on a detached HEAD"),
    ("channel_invalid", True, "configured channel is invalid"),
)


def log(text):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {text}", flush=True)


def request(method, path, query=None, body=None, timeout=30):
    url = MOONRAKER + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif method == "POST":
        data = b""
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp).get("result")
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e)["error"]["message"]
        except Exception:
            msg = str(e)
        raise RuntimeError(msg) from None


def say(text, complete=False):
    """Log a line and show it in the Klipper console and, when the
    zmod_update_all Moonraker component is loaded, in the web UI's update
    dialog (complete=True lets that dialog close)."""
    log(text)
    try:
        request("POST", "/server/zmod_update_all/message",
                body={"msg": text, "complete": complete}, timeout=10)
    except Exception:
        pass  # component not loaded (yet): console only
    # Klipper drops everything after ';' and the value cannot hold quotes.
    clean = text.replace('"', "'").replace(";", ",").replace("\n", " ")
    try:
        request("POST", "/printer/gcode/script",
                body={"script": f'_UPDATE_ALL_MSG MSG="{clean}"'})
    except Exception as e:
        log(f"(console message failed: {e})")


def is_printing():
    res = request("GET", "/printer/objects/query", {"print_stats": "state"})
    state = res["status"]["print_stats"]["state"]
    return state in ("printing", "paused")


def wait_ready(limit=300):
    """After a boot: wait until Moonraker and Klipper answer and Moonraker
    is not busy (it may still be finishing the update that rebooted)."""
    end = time.time() + limit
    while True:
        try:
            is_printing()
            if not request("GET", "/machine/update/status").get("busy"):
                return True
        except Exception:
            pass
        if time.time() > end:
            return False
        time.sleep(5)


def has_update(info):
    remote = info.get("remote_version")
    if info.get("configured_type") == "git_repo":
        if info.get("commits_behind_count", 0) > 0:
            return True
    if not remote or remote == "?":
        return False
    return info.get("version") != remote


def problem(info):
    for key, bad, reason in PROBLEMS:
        if key in info and info[key] == bad:
            return reason
    return None


PLUGINS_DIR = "/root/printer_data/config/mod_data/plugins"
# A plugin's update.sh that asks Zmod for a reboot, e.g. recommend's
# `echo "REBOOT" >/tmp/printer`. Zmod acts on it seconds later, which
# would cut into whatever the queue starts next.
REBOOT_RE = re.compile(rb"""REBOOT["']?\s*>+\s*/tmp/printer|^\s*(sudo\s+)?reboot\b"""
                       rb"|systemctl\s+reboot", re.M)


def plugin_reboots(name):
    try:
        with open(os.path.join(PLUGINS_DIR, name, "update.sh"), "rb") as f:
            return bool(REBOOT_RE.search(f.read()))
    except OSError:
        return False


# ---- deferred plugin reboots --------------------------------------------
#
# With the Moonraker component loaded, a plugin's update.sh runs with its
# reboot request redirected into REBOOT_FLAG instead of /tmp/printer (or
# instead of reboot/systemctl reboot). The queue then reboots once at its
# end. Only for the plugin named in DEFER_PATH, which work() writes around
# its own upgrade call, so a plugin updated by hand still reboots as usual.
# The script on disk is never changed: the repo stays clean and the
# plugin's current update.sh is what runs.
DEFER_PATH = "/tmp/zmod_update_all.defer"
REBOOT_FLAG = "/tmp/zmod_update_all.reboot"

# A reboot in command position: at the start of a line or after ; && || |
# ( or a then/do/else keyword. Not inside an echo "please reboot".
_CMD = rb"(?:^|(?<=[;&|(])|(?<=\bthen)|(?<=\bdo)|(?<=\belse))[ \t]*"
_STRIP = (
    # echo "REBOOT" >/tmp/printer: Zmod reboots seconds later.
    (re.compile(rb"""(echo[ \t]+["']?REBOOT["']?[ \t]*)>+[ \t]*/tmp/printer\b"""),
     rb"\1>" + REBOOT_FLAG.encode()),
    # sudo systemctl reboot, reboot, /sbin/reboot -f ...: only the command
    # word is swapped, its arguments and redirections stay as they were.
    (re.compile(rb"(" + _CMD + rb")(?:sudo[ \t]+)?(?:/usr)?(?:/s?bin/)?"
                rb"(?:systemctl[ \t]+reboot|reboot)\b", re.M),
     rb"\1_zua_reboot"),
)
_FUNC = b"_zua_reboot() { echo REBOOT >" + REBOOT_FLAG.encode() + b"; }\n"


def strip_reboots(text):
    """Return (script, changed): the script with every reboot it asks for
    redirected into REBOOT_FLAG. Used by the Moonraker component."""
    out = text
    for rx, repl in _STRIP:
        out = rx.sub(repl, out)
    if out == text:
        return text, False
    if b"_zua_reboot" in out:
        # Define the stand-in right after the shebang line.
        if out.startswith(b"#!"):
            head, _, rest = out.partition(b"\n")
            out = head + b"\n" + _FUNC + rest
        else:
            out = _FUNC + out
    return out, True


def defer_wanted(name):
    """For the component: is the queue updating this plugin right now?"""
    try:
        with open(DEFER_PATH) as f:
            d = json.load(f)
        with open(f"/proc/{int(d['pid'])}/cmdline", "rb") as f:
            alive = b"update_all.py" in f.read()
        return alive and d.get("name") == name
    except (OSError, ValueError, KeyError, TypeError):
        return False


_DEFER = None


def defer_ok():
    """True when the Moonraker component defers plugin reboots."""
    global _DEFER
    if _DEFER is None:
        try:
            res = request("GET", "/server/zmod_update_all/info", timeout=10)
            _DEFER = bool(res.get("defer_plugin_reboots"))
        except Exception:
            _DEFER = False
    return _DEFER


def set_defer(name):
    for path in (REBOOT_FLAG, DEFER_PATH):
        try:
            os.remove(path)
        except OSError:
            pass
    if name:
        with open(DEFER_PATH, "w") as f:
            json.dump({"name": name, "pid": os.getpid()}, f)


def reboot_deferred():
    """True when the last plugin's update.sh asked for a reboot."""
    return os.path.exists(REBOOT_FLAG)


def reboots(name):
    """True when updating this item may reboot the printer."""
    if name.lower() in REBOOTS:
        return True
    return plugin_reboots(name) and not defer_ok()


def order_key(name):
    core = {n: i for i, n in enumerate(REBOOTS)}
    if name.lower() in core:
        return core[name.lower()]
    return -1 if reboots(name) else -2


def plan(version_info, exclude=()):
    """Split the updaters into (to update, skipped with reason)."""
    todo, skipped = [], []
    for name, info in version_info.items():
        if name.lower() in SKIP or not info.get("configured_type"):
            continue
        if name in exclude or not has_update(info):
            continue
        reason = problem(info)
        if reason:
            skipped.append((name, reason))
        else:
            todo.append(name)
    # Plain plugins first, then plugins whose update.sh may reboot, then
    # klippy, moon, Z-Mod.
    todo.sort(key=order_key)
    return todo, skipped


def describe(name, info):
    return f"{name} {info.get('version')} -> {info.get('remote_version')}"


def status():
    return request("GET", "/machine/update/status")["version_info"]


def refresh(retry_for=0):
    """Refresh every updater. Right after a boot Moonraker is busy with its
    own startup refresh and every version reads '?' until one succeeds, so
    keep retrying for up to retry_for seconds."""
    end = time.time() + retry_for
    while True:
        try:
            request("POST", "/machine/update/refresh", timeout=600)
            return True
        except Exception as e:
            if time.time() > end:
                say(f"Refresh failed ({e}), using the last known versions.")
                return False
            log(f"Refresh not possible yet ({e}), retrying.")
            time.sleep(10)


# ---- the web UI button ------------------------------------------------

def _write(path, text):
    tmp = path + ".zua.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def inject_web(remove=False):
    """Add (or remove) the Update all button script in Mainsail's and
    Fluidd's index.html. The web updater replaces those folders, so this
    runs again after their updates and at every boot. Quiet and safe to
    repeat. Both are PWAs: the service worker serves a cached index.html,
    so its precache revision for index.html is changed with it."""
    try:
        with open(WEB_JS, "rb") as f:
            js = f.read()
    except OSError:
        return
    ver = hashlib.md5(js).hexdigest()[:10]
    tag = (f'{WEB_MARK}<script src="/zmod_update_all.js?v={ver}" defer>'
           '</script>')
    for name, d in WEB_DIRS.items():
        index = os.path.join(d, "index.html")
        try:
            with open(index, encoding="utf-8", newline="") as f:
                html = f.read()
        except OSError:
            continue
        new = re.sub(re.escape(WEB_MARK) + r"[^\n]*?</script>", "", html)
        if not remove:
            new = new.replace("</head>", tag + "</head>", 1)
        dest = os.path.join(d, "zmod_update_all.js")
        try:
            if remove:
                if os.path.exists(dest):
                    os.remove(dest)
            else:
                old = None
                try:
                    with open(dest, "rb") as f:
                        old = f.read()
                except OSError:
                    pass
                if old != js:
                    shutil.copyfile(WEB_JS, dest)
            if new == html:
                continue
            _write(index, new)
            sw = os.path.join(d, "sw.js")
            try:
                with open(sw, encoding="utf-8", newline="") as f:
                    swt = f.read()
            except OSError:
                swt = None
            if swt:
                rev = hashlib.md5(new.encode()).hexdigest()
                # Mainsail: {url:"index.html",revision:"..."}
                # Fluidd: {"revision":"...","url":"index.html"}
                swn = re.sub(r'(\{url:"index\.html",revision:")[0-9a-f]*(")',
                             lambda m: m.group(1) + rev + m.group(2), swt)
                swn = re.sub(r'(\{"revision":")[0-9a-f]*(","url":"index\.html")',
                             lambda m: m.group(1) + rev + m.group(2), swn)
                if swn != swt:
                    _write(sw, swn)
            log(f"{'Removed' if remove else 'Added'} the Update all button "
                f"in {name}.")
        except OSError as e:
            log(f"Could not patch {name}: {e}")


# ---- the queue file ---------------------------------------------------

def load_queue():
    try:
        with open(QUEUE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_queue(q):
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(q, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, QUEUE_PATH)
    os.sync()


def drop_queue():
    try:
        os.remove(QUEUE_PATH)
    except OSError:
        pass


# ---- the run -----------------------------------------------------------

def finish(q, reboot):
    drop_queue()
    done, failed = q["done"], q["failed"]
    summary = f"Done. Updated {len(done)}"
    if done:
        summary += ": " + ", ".join(done)
    if failed:
        summary += ". Failed: " + ", ".join(failed)
    say(summary + ".", complete=not q["need_reboot"])
    if not q["need_reboot"]:
        return
    if not reboot:
        say("Reboot skipped (REBOOT=0). Reboot to load the new versions.", complete=True)
        return
    say("Rebooting in 10 seconds to load the new versions.", complete=True)
    time.sleep(10)
    if is_printing():
        say("A print started, not rebooting.", complete=True)
        return
    request("POST", "/printer/gcode/script", body={"script": "REBOOT"},
            timeout=60)


def start(check, reboot):
    if is_printing():
        say("Refused: the printer is printing.", complete=True)
        return
    if request("GET", "/machine/update/status").get("busy"):
        say("Refused: Moonraker is already updating something.", complete=True)
        return
    if load_queue():
        say("Dropping an unfinished queue from an earlier run.")
        drop_queue()

    say("Checking for updates...")
    refresh(retry_for=120)
    info = status()
    todo, skipped = plan(info)
    for name, reason in skipped:
        say(f"Skipping {describe(name, info[name])}: {reason}.")
    if not todo:
        say("Everything is up to date." if not skipped else
            "Nothing else to update.", complete=True)
        return
    say(f"{len(todo)} update(s): " +
        ", ".join(describe(n, info[n]) for n in todo))
    core = [n for n in todo if reboots(n)]
    if core:
        say(f"May reboot the printer: {', '.join(core)}. These go last, "
            "one at a time, and the queue carries on after each reboot.")
    if check:
        say("Check only. Run UPDATE_ALL to install them.", complete=True)
        return
    if core and not reboot:
        say(f"REBOOT=0: leaving out {', '.join(core)}, "
            "they can reboot the printer.")
        todo = [n for n in todo if not reboots(n)]
        if not todo:
            say("Nothing left to update.", complete=True)
            return

    q = {"started": time.time(), "reboot": reboot, "done": [],
         "failed": [], "attempted": None, "attempts": {},
         "need_reboot": False}
    work(q, todo)


def resume():
    q = load_queue()
    if not q:
        return
    q["resumes"] = q.get("resumes", 0) + 1
    if q["resumes"] > MAX_RESUMES:
        drop_queue()
        say("Queue dropped after too many reboots. Run UPDATE_ALL again.", complete=True)
        return
    save_queue(q)
    if not wait_ready():
        say("Moonraker or Klipper did not come up, queue stopped. "
            "Run UPDATE_ALL again.", complete=True)
        drop_queue()
        return
    # We just rebooted, so everything updated so far is loaded.
    q["need_reboot"] = False
    if not refresh(retry_for=600):
        say("Could not check for updates after the reboot, queue stopped. "
            "Run UPDATE_ALL again.", complete=True)
        drop_queue()
        return
    info = status()
    name = q.get("attempted")
    if name:
        q["attempted"] = None
        tries = q.setdefault("attempts", {}).get(name, 1)
        if name in info and not has_update(info[name]):
            q["done"].append(name)
            say(f"{name} updated.")
        elif tries < 2:
            # The reboot may have come from something else (a plugin's
            # update.sh) before this update even started. Once more.
            say(f"{name} is not up to date yet, trying it once more.")
        else:
            q["failed"].append(name)
            say(f"{name} is still not up to date, leaving it out.")
    if is_printing():
        drop_queue()
        say("A print is running, queue stopped. Run UPDATE_ALL later.", complete=True)
        return
    todo, _ = plan(info, exclude=q["done"] + q["failed"])
    if not q["reboot"]:
        todo = [n for n in todo if not reboots(n)]
    if todo:
        say(f"Resuming after the reboot, {len(todo)} left: "
            + ", ".join(todo))
    work(q, todo)


def work(q, todo):
    for name in todo:
        if is_printing():
            say("A print started, stopping before " + name + ".")
            break
        # Saved before every item: if a reboot cuts in, the resume after
        # the boot checks this item and carries on.
        q["attempted"] = name
        q.setdefault("attempts", {})[name] = q["attempts"].get(name, 0) + 1
        save_queue(q)
        if reboots(name):
            # Zmod reboots inside this upgrade call (or the plugin's
            # update.sh asks for one). _UPDATE_ALL_RESUME picks the queue
            # up after the boot.
            say(f"Updating {name}. The printer may reboot afterwards; "
                "UPDATE_ALL carries on by itself.", complete=True)
            try:
                request("POST", "/machine/update/upgrade", {"name": name},
                        timeout=1800)
            except Exception as e:
                log(f"{name} upgrade call ended: {e}")
            # Normally the reboot ends us here. Still alive? Then no reboot
            # came (or it failed); check the result and keep going.
            time.sleep(120)
            try:
                info = status()
            except Exception as e:
                # Most likely the reboot is under way. Keep the queue.
                log(f"Moonraker gone after {name} ({e}), leaving the "
                    "queue for the resume after boot.")
                return
            q["attempted"] = None
            if not has_update(info.get(name, {})):
                q["done"].append(name)
                q["need_reboot"] = True
                say(f"{name} updated, no reboot came.")
            else:
                q["failed"].append(name)
                say(f"{name} failed.")
            save_queue(q)
            continue
        say(f"Updating {name}...")
        set_defer(name)
        try:
            request("POST", "/machine/update/upgrade", {"name": name},
                    timeout=1800)
        except Exception as e:
            say(f"{name} failed: {e}")
            q["failed"].append(name)
        else:
            q["done"].append(name)
            q["need_reboot"] = True
            if reboot_deferred():
                say(f"{name} asked for a reboot, UPDATE_ALL reboots once "
                    "at the end.")
            if name.lower() in WEB_DIRS:
                inject_web()
        finally:
            set_defer(None)
        q["attempted"] = None
        save_queue(q)
    finish(q, q["reboot"])


# ---- the support report -------------------------------------------------
#
# UPDATE_ALL REPORT=1: one text file with everything needed to see why a run
# did not do what was expected, saved next to the log so it can be
# downloaded from the web UI's config files. Only selected fields go in (no
# raw Moonraker dumps, no system info), and logins in URLs are blanked.

REPORT_PATH = "/root/printer_data/config/mod_data/log/zmod_update_all_report.txt"
REPORT_LOG_LINES = 200
PLUGINS_CONF = "/root/printer_data/config/mod_data/plugins.moonraker.conf"

_CRED_RE = re.compile(r"(\w+://)[^/@\s]+@")
_KEY_RE = re.compile(r"(?i)((?:api[_-]?key|token|password|passwd|secret)"
                     r"\W{0,3}[=:]\s*)\S+")


def redact(text):
    text = _CRED_RE.sub(r"\1***@", text)
    return _KEY_RE.sub(r"\1***", text)


def _git(*args):
    import subprocess
    try:
        out = subprocess.run(["git", "-C", PLUGIN_DIR] + list(args),
                             capture_output=True, text=True, timeout=10)
    except Exception as e:
        return f"(git failed: {e})"
    if out.returncode:
        err = (out.stderr.strip().splitlines() or ["?"])[0]
        return f"(git failed: {err})"
    return out.stdout.strip()


def _tail(path, n):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except OSError as e:
        return [f"(cannot read {path}: {e})\n"]


def _web_state(d):
    """Is the button script in this web UI, and does the PWA know?"""
    index = os.path.join(d, "index.html")
    try:
        with open(index, encoding="utf-8", newline="") as f:
            html = f.read()
    except OSError:
        return "not installed"
    parts = ["button in index.html" if WEB_MARK in html
             else "button NOT in index.html"]
    try:
        with open(os.path.join(d, "zmod_update_all.js"), "rb") as f,                 open(WEB_JS, "rb") as g:
            same = f.read() == g.read()
        parts.append("script current" if same else "script OUTDATED")
    except OSError:
        parts.append("script MISSING")
    try:
        with open(os.path.join(d, "sw.js"), encoding="utf-8") as f:
            sw = f.read()
        rev = hashlib.md5(html.encode()).hexdigest()
        if "index.html" not in sw:
            parts.append("sw.js has no index.html entry")
        else:
            parts.append("sw.js revision matches" if rev in sw
                         else "sw.js revision STALE (browser may show the "
                              "old page)")
    except OSError:
        parts.append("no sw.js")
    return ", ".join(parts)


def report():
    """Write REPORT_PATH and print a short summary for the console."""
    lines = []
    notes = []

    def add(text=""):
        lines.append(text)

    def section(title):
        add()
        add(f"== {title} ==")

    def ask(method, path, query=None):
        try:
            return request(method, path, query, timeout=5), None
        except Exception as e:
            return None, str(e) or e.__class__.__name__

    add("zmod_update_all support report")
    add(f"Written: {time.strftime('%Y-%m-%d %H:%M:%S %Z')} "
        "(the printer clock reads 2026-01-01 until NTP has synced)")
    add(f"Plugin: {_git('describe', '--tags', '--always', '--dirty')}"
        f" on branch {_git('rev-parse', '--abbrev-ref', 'HEAD')}")
    add(f"Last commit: {_git('log', '-1', '--format=%h %ci %s')}")
    status_lines = _git("status", "--porcelain")
    if status_lines and not status_lines.startswith("("):
        add("Local changes in the plugin folder:")
        for ln in status_lines.splitlines()[:20]:
            add("  " + ln)
    add(f"Python: {sys.version.split()[0]}")

    section("Moonraker")
    info, err = ask("GET", "/server/info")
    if err:
        add(f"Not reachable: {err}")
        notes.append("Moonraker not reachable")
    else:
        add(f"Version: {info.get('moonraker_version')}, "
            f"Klippy: {info.get('klippy_state')}")
        comps = info.get("components") or []
        failed = info.get("failed_components") or []
        loaded = "zmod_update_all" in comps
        add(f"zmod_update_all component: "
            f"{'loaded' if loaded else 'NOT loaded'}"
            f"{' (FAILED to load)' if 'zmod_update_all' in failed else ''}")
        if failed:
            add(f"Failed components: {', '.join(failed)}")
        for w in (info.get("warnings") or [])[:20]:
            add(f"Warning: {redact(str(w))}")
        if not loaded:
            notes.append("Moonraker component not loaded (reboot after "
                         "install or update?)")
        cinfo, err = ask("GET", "/server/zmod_update_all/info")
        add(f"Component info: {cinfo if cinfo is not None else err}")
    try:
        with open(PLUGINS_CONF, encoding="utf-8") as f:
            inc = "zmod_update_all.moonraker.conf" in f.read()
    except OSError:
        inc = False
    add(f"Include in mod_data/plugins.moonraker.conf: "
        f"{'yes' if inc else 'NO'}")
    if not inc:
        notes.append("Moonraker include missing (run ENABLE_PLUGIN again)")

    section("Printer")
    res, err = ask("GET", "/printer/objects/query", {"print_stats": "state"})
    add(f"Print state: {res['status']['print_stats']['state'] if res else err}")

    section("Web UI button")
    for name, d in WEB_DIRS.items():
        state = _web_state(d)
        add(f"{name}: {state}")
        if "NOT" in state or "MISSING" in state:
            notes.append(f"button missing in {name}")

    section("Queue")
    q = load_queue()
    add(f"Saved queue: {json.dumps(q) if q else 'none'}")
    add(f"Running now: {'yes' if already_running() else 'no'}")
    for path in (DEFER_PATH, REBOOT_FLAG):
        if os.path.exists(path):
            add(f"{path} exists")

    section("Updates (as Moonraker last saw them)")
    st, err = ask("GET", "/machine/update/status")
    if err:
        add(f"Not available: {err}")
    else:
        add(f"Moonraker busy: {st.get('busy')}")
        pending = 0
        for name, i in sorted((st.get("version_info") or {}).items()):
            if not isinstance(i, dict):
                continue
            flags = [reason for key, bad, reason in PROBLEMS
                     if key in i and i[key] == bad]
            if name.lower() in SKIP:
                flags.append("not handled by UPDATE_ALL")
            elif has_update(i):
                pending += 1
                flags.append("UPDATE")
                if plugin_reboots(name):
                    flags.append("update.sh reboots")
            behind = i.get("commits_behind_count")
            add(f"{name}: {i.get('configured_type')} "
                f"{i.get('version')} -> {i.get('remote_version')}"
                + (f", {behind} behind" if behind else "")
                + (f", channel {i['channel']}" if i.get("channel") else "")
                + (f" [{'; '.join(flags)}]" if flags else ""))
        notes.append(f"{pending} update(s) pending" if pending else
                     "no updates pending (the button hides itself then)")

    section(f"Last {REPORT_LOG_LINES} log lines ({LOG_PATH})")
    for ln in _tail(LOG_PATH, REPORT_LOG_LINES):
        add(redact(ln.rstrip("\n")))

    text = "\n".join(lines) + "\n"
    _write(REPORT_PATH, text)
    print("Report saved to mod_data/log/zmod_update_all_report.txt in the "
          "config files. Download it there and send it in with a short "
          "note of what you expected.")
    if notes:
        print("Summary: " + "; ".join(notes) + ".")


# ---- process handling ---------------------------------------------------

def already_running():
    try:
        with open(LOCK_PATH) as f:
            pid = int(f.read().strip())
        # After a reboot the PID may belong to something else.
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"update_all.py" in f.read()
    except (OSError, ValueError):
        return False


def daemonize():
    """Fork into the background; the parent returns False and exits."""
    if os.fork() > 0:
        return False
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    sys.stdout = os.fdopen(1, "w", buffering=1)
    sys.stderr = sys.stdout
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="list available updates without installing")
    parser.add_argument("--no-reboot", action="store_true",
                        help="do not reboot after updating")
    parser.add_argument("--resume", action="store_true",
                        help="carry on with a saved queue (run at boot)")
    parser.add_argument("--cancel", action="store_true",
                        help="drop a saved queue")
    parser.add_argument("--inject", action="store_true",
                        help="add the Update all button to Mainsail/Fluidd")
    parser.add_argument("--uninject", action="store_true",
                        help="remove the Update all button again")
    parser.add_argument("--report", action="store_true",
                        help="write a support report next to the log")
    parser.add_argument("--foreground", action="store_true",
                        help="run in this process (for testing over SSH)")
    args = parser.parse_args()

    if args.cancel:
        had = load_queue() is not None
        drop_queue()
        print("UPDATE_ALL queue dropped." if had else "No queue to drop.")
        return
    if args.report:
        report()
        return
    if args.inject or args.uninject:
        inject_web(remove=args.uninject)
        return
    if args.resume:
        # Every boot: put the button back if a web UI update removed it.
        inject_web()
        if load_queue() is None:
            return  # the usual boot: nothing queued, stay quiet
    if already_running():
        print("UPDATE_ALL is already running, see " + LOG_PATH)
        return
    if not args.foreground:
        if not daemonize():
            if not args.resume:
                print("UPDATE_ALL started, progress follows in the console.")
            return
    try:
        if args.resume:
            resume()
        else:
            start(args.check, not args.no_reboot)
    except Exception as e:
        log(traceback.format_exc())
        say(f"Stopped by an error: {e}", complete=True)
        drop_queue()
    finally:
        if not args.foreground:
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass


if __name__ == "__main__":
    main()
