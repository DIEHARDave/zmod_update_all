#!/usr/bin/env python3
# zmod_update_all: an update queue for Zmod. Installs every available
# Moonraker update in one go and reboots as few times as possible.
#
# Runs inside Zmod's chroot (Python 3.12, standard library only), started by
# the UPDATE_ALL macro. It forks into the background straight away so Klipper
# is not blocked, and reports progress to the console through _UPDATE_ALL_MSG.
#
# Plugins are updated first, without a reboot. Zmod's Moonraker reboots the
# printer by itself right after updating Z-Mod, klippy or moon, so those go
# last, one per boot: the queue is saved to disk before each of them and
# _UPDATE_ALL_RESUME (a delayed_gcode at Klipper start) carries on after the
# reboot. If only plugins changed, the queue ends with a single REBOOT.
#
# Copyright (C) 2026 DIEHARDave
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import json
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


def say(text):
    """Log a line and show it in the Klipper console."""
    log(text)
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


def reboots(name):
    return name.lower() in REBOOTS


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
    order = {n: i for i, n in enumerate(REBOOTS)}
    todo.sort(key=lambda n: order.get(n.lower(), -1))
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
    say(summary + ".")
    if not q["need_reboot"]:
        return
    if not reboot:
        say("Reboot skipped (REBOOT=0). Reboot to load the new versions.")
        return
    say("Rebooting in 10 seconds to load the new versions.")
    time.sleep(10)
    if is_printing():
        say("A print started, not rebooting.")
        return
    request("POST", "/printer/gcode/script", body={"script": "REBOOT"},
            timeout=60)


def start(check, reboot):
    if is_printing():
        say("Refused: the printer is printing.")
        return
    if request("GET", "/machine/update/status").get("busy"):
        say("Refused: Moonraker is already updating something.")
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
            "Nothing else to update.")
        return
    say(f"{len(todo)} update(s): " +
        ", ".join(describe(n, info[n]) for n in todo))
    core = [n for n in todo if reboots(n)]
    if core and len(core) > 1:
        say(f"{', '.join(core)} each reboot the printer by themselves. "
            f"The queue carries on after each reboot.")
    if check:
        say("Check only. Run UPDATE_ALL to install them.")
        return
    if core and not reboot:
        say(f"REBOOT=0: leaving out {', '.join(core)}, "
            "Zmod reboots after updating them.")
        todo = [n for n in todo if not reboots(n)]
        if not todo:
            return

    q = {"started": time.time(), "reboot": reboot, "done": [],
         "failed": [], "attempted": None, "need_reboot": False}
    work(q, todo)


def resume():
    q = load_queue()
    if not q:
        return
    q["resumes"] = q.get("resumes", 0) + 1
    if q["resumes"] > MAX_RESUMES:
        drop_queue()
        say("Queue dropped after too many reboots. Run UPDATE_ALL again.")
        return
    save_queue(q)
    if not wait_ready():
        say("Moonraker or Klipper did not come up, queue stopped. "
            "Run UPDATE_ALL again.")
        drop_queue()
        return
    # We just rebooted, so everything updated so far is loaded.
    q["need_reboot"] = False
    if not refresh(retry_for=600):
        say("Could not check for updates after the reboot, queue stopped. "
            "Run UPDATE_ALL again.")
        drop_queue()
        return
    info = status()
    name = q.get("attempted")
    if name:
        q["attempted"] = None
        if name in info and not has_update(info[name]):
            q["done"].append(name)
            say(f"{name} updated.")
        else:
            q["failed"].append(name)
            say(f"{name} is still not up to date, leaving it out.")
    if is_printing():
        drop_queue()
        say("A print is running, queue stopped. Run UPDATE_ALL later.")
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
        if reboots(name):
            # Zmod reboots inside this upgrade call. Save first, then
            # _UPDATE_ALL_RESUME picks the queue up after the boot.
            q["attempted"] = name
            save_queue(q)
            say(f"Updating {name}. The printer reboots afterwards and "
                "UPDATE_ALL carries on by itself.")
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
        try:
            request("POST", "/machine/update/upgrade", {"name": name},
                    timeout=1800)
        except Exception as e:
            say(f"{name} failed: {e}")
            q["failed"].append(name)
        else:
            q["done"].append(name)
            q["need_reboot"] = True
        save_queue(q)
    finish(q, q["reboot"])


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
    parser.add_argument("--foreground", action="store_true",
                        help="run in this process (for testing over SSH)")
    args = parser.parse_args()

    if args.cancel:
        had = load_queue() is not None
        drop_queue()
        print("UPDATE_ALL queue dropped." if had else "No queue to drop.")
        return
    if args.resume and load_queue() is None:
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
        say(f"Stopped by an error: {e}")
        drop_queue()
    finally:
        if not args.foreground:
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass


if __name__ == "__main__":
    main()
