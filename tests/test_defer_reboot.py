#!/usr/bin/env python3
"""Deferred plugin reboots: strip_reboots() and the Moonraker component's
hook, run against Zmod's real git_deploy.py update().

WSL, as root (Zmod's update() reads /opt/config/mod_data/plugins.cfg):
  wsl -u root -e python3 tests/test_defer_reboot.py [path/to/zmod_moonraker]
Creates /opt/config only if it is missing and removes it again.
"""
import asyncio
import importlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
MOON = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                    else ROOT.parent / "zmod_moonraker").resolve()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ua = load("update_all", ROOT / "update_all.py")
FLAG = ua.REBOOT_FLAG
fails = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        fails.append(what)


# A script shaped like Zmod's recommend/update.sh: settings through
# /tmp/printer ($OUT here), then a conditional REBOOT at the end.
RECOMMEND = b"""#!/bin/sh
cd "$(dirname -- "$0")" || exit 1
NEED_RESTART=0
[ "$V" -lt 1 ] && echo "SAVE_VARIABLE VARIABLE=fix_scv VALUE=1" >$OUT && NEED_RESTART=1
echo "Plugin installed, please reboot" >>"$LOG"
basename "$PWD" >>"$LOG"
[ "$NEED_RESTART" -eq 1 ] && echo "REBOOT" >/tmp/printer
"""


def strip_cases():
    new, ch = ua.strip_reboots(RECOMMEND)
    check(ch, "recommend: changed")
    check(b'echo "REBOOT" >' + FLAG.encode() in new,
          "recommend: REBOOT goes to the flag")
    check(b'VALUE=1" >$OUT' in new, "recommend: SAVE_VARIABLE untouched")
    check(b"please reboot" in new, "recommend: echo text untouched")
    cases = {
        b"#!/bin/sh\nreboot\n": True,
        b"#!/bin/sh\n  sudo reboot -f >/dev/null 2>&1\n": True,
        b"#!/bin/sh\n[ x ] && /sbin/reboot\n": True,
        b"#!/bin/sh\nif true; then systemctl reboot; fi\n": True,
        b"#!/bin/sh\nsudo systemctl reboot\n": True,
        b"#!/bin/sh\necho 'REBOOT' > /tmp/printer\n": True,
        b"#!/bin/sh\necho \"reboot needed\"\n": False,
        b"#!/bin/sh\necho SAVE_CONFIG >/tmp/printer\n": False,
        b"#!/bin/sh\nZMOD_UPDATE_ALL_NO_REBOOT=1 exec ./install.sh\n": False,
    }
    for src, want in cases.items():
        new, ch = ua.strip_reboots(src)
        check(ch == want, f"strip {src.splitlines()[1]!r} -> {ch}")
        if ch and b"_zua_reboot" in new:
            check(new.startswith(b"#!/bin/sh\n_zua_reboot()"),
                  "  stand-in defined after shebang")
            rest = new.replace(FLAG.encode(), b"").replace(b"_zua_reboot", b"")
            check(b"reboot" not in rest,
                  "  no reboot command left")
    # The rewritten scripts must still run and only set the flag.
    for src in (b"#!/bin/sh\n  sudo reboot -f >/dev/null 2>&1\necho after\n",
                b"#!/bin/sh\nif true; then systemctl reboot; fi\necho after\n"):
        new, _ = ua.strip_reboots(src)
        if os.path.exists(FLAG):
            os.remove(FLAG)
        r = subprocess.run(["/bin/sh", "-c", new.decode(), "x"],
                           capture_output=True, text=True)
        check(r.returncode == 0 and "after" in r.stdout
              and os.path.exists(FLAG), f"runs, flag set: {src[10:40]!r}")


def import_git_deploy():
    sys.path.insert(0, str(MOON.parent))
    # Moonraker wants the importlib_metadata backport; the stdlib has it.
    try:
        import importlib_metadata  # noqa: F401
    except ImportError:
        import importlib.metadata
        sys.modules["importlib_metadata"] = importlib.metadata
    pkg = MOON.name
    gd = importlib.import_module(f"{pkg}.components.update_manager.git_deploy")
    um = importlib.import_module(
        f"{pkg}.components.update_manager.update_manager")
    return gd, um


def fake_updater(gd, path):
    u = object.__new__(gd.GitDeploy)
    u.name = path.name
    u.path = path
    u._is_valid = True
    u.logs = []

    class Repo:
        async def wait_for_refresh(self): pass
        def is_dirty(self): return False
        def is_current(self): return False

    async def nop(*a, **k): return None
    u.repo = Repo()
    u.cmd_helper = types.SimpleNamespace(notify_update_response=lambda *a: 0)
    u._collect_dependency_info = nop
    u._pull_repo = nop
    u._update_dependencies = nop
    u._update_repo_state = nop
    u.restart_service = nop
    u.notify_status = lambda *a, **k: None
    u.log_info = lambda m: u.logs.append(m)
    return u


def hook_cases(work):
    gd, umod = import_git_deploy()
    comp = load("zmod_update_all_component",
                ROOT / "moonraker" / "zmod_update_all.py")
    comp.SCRIPT = str(ROOT / "update_all.py")
    c = object.__new__(comp.ZmodUpdateAll)
    c.um = object.__new__(umod.UpdateManager)
    check(c._hook_plugin_scripts(), "hook installed in git_deploy")
    check(gd.asyncio.sleep is asyncio.sleep, "proxy forwards asyncio")

    plugin = work / "mod_data" / "plugins" / "recommend"
    plugin.mkdir(parents=True)
    (plugin / "update.sh").write_bytes(RECOMMEND)
    (plugin / "update.sh").chmod(0o755)
    os.makedirs("/opt/config/mod_data", exist_ok=True)
    with open("/opt/config/mod_data/plugins.cfg", "w") as f:
        f.write("[include plugins/recommend/recommend.cfg]\n")
    log = work / "log"
    os.environ.update(V="0", OUT=str(work / "printer"), LOG=str(log))
    printer = pathlib.Path("/tmp/printer")
    had_printer = printer.exists()

    def run(defer):
        for p in (FLAG, ua.DEFER_PATH):
            if os.path.exists(p):
                os.remove(p)
        if printer.exists() and not had_printer:
            printer.unlink()
        log.write_text("")
        holder = None
        if defer:
            # A process whose cmdline says update_all.py, as the queue's.
            holder = subprocess.Popen(["sh", "-c", "sleep 30",
                                       "update_all.py"])
            with open(ua.DEFER_PATH, "w") as f:
                json.dump({"name": "recommend", "pid": holder.pid}, f)
        u = fake_updater(gd, plugin)
        asyncio.run(u.update())
        if holder:
            holder.kill()
        return u

    u = run(defer=True)
    out = log.read_text()
    check(os.path.exists(FLAG), "queued: reboot redirected to the flag")
    check(had_printer or not printer.exists(),
          "queued: nothing written to /tmp/printer")
    check("please reboot" in out and out.strip().endswith("recommend"),
          "queued: script ran in its own folder ($0 kept)")
    check(any("executed successfully" in m for m in u.logs),
          "queued: Zmod logs success")
    check((work / "printer").read_text().startswith("SAVE_VARIABLE"),
          "queued: other /tmp/printer writes still made")

    if not had_printer:
        run(defer=False)
        check(not os.path.exists(FLAG), "manual update: flag not set")
        check(printer.exists() and b"REBOOT" in printer.read_bytes(),
              "manual update: stock reboot request kept")
        printer.unlink()


def main():
    made_opt = not os.path.exists("/opt/config")
    work = pathlib.Path(tempfile.mkdtemp(prefix="zua_"))
    try:
        strip_cases()
        hook_cases(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if made_opt:
            shutil.rmtree("/opt/config", ignore_errors=True)
        for p in (FLAG, ua.DEFER_PATH):
            if os.path.exists(p):
                os.remove(p)
    print(f"\n{len(fails)} failed" if fails else "\nall passed")
    sys.exit(1 if fails else 0)


main()
