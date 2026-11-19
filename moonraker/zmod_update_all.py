# zmod_update_all: route Moonraker's "Update all" through the update queue.
#
# Mainsail's UPDATE ALL COMPONENTS and Fluidd's update-all button call
# machine.update.full (Fluidd: machine.update.upgrade without a name).
# Stock Moonraker then walks every updater in a row: on Zmod that stops at
# the first repo with local changes, tries the OS package updater, and gets
# cut off when Zmod reboots after Z-Mod, klippy or moon.
#
# This component points both at update_all.py instead, the same script the
# UPDATE_ALL macro runs, so the button and the macro share one path: skip
# System/OS packages and broken repos, plugins first, the reboot-triggering
# items last with a resume after each reboot.
#
# No Zmod file is changed: install.sh links this file into Moonraker's
# components and zmod_update_all.moonraker.conf loads it. The swap replaces
# the callback of the cached API definition, which both the HTTP route and
# the websocket method use.
#
# Zmod's Mainsail and Fluidd forks have no Update all button any more;
# update_all.py --inject adds one (web/zmod_update_all.js). A web UI update
# replaces those files, so the mainsail/fluidd updaters are wrapped here to
# put the button back right after each of their updates.
#
# It also adds POST /server/zmod_update_all/message, which update_all.py
# uses to show its progress in the web UI's update dialog.
#
# Plugin reboots: while the queue updates a plugin, that plugin's update.sh
# runs as a copy with its reboot request redirected to a flag file (see
# update_all.strip_reboots), so the queue reboots once at its end. The
# script on disk is untouched. GET /server/zmod_update_all/info tells
# update_all.py whether this hook is in place.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
from typing import Any

PLUGIN_DIR = "/root/printer_data/config/mod_data/plugins/zmod_update_all"
SCRIPT = os.path.join(PLUGIN_DIR, "update_all.py")
PYTHON = "/usr/bin/python3"
FULL_ENDPOINTS = ("/machine/update/full",)
WEB_UPDATERS = ("mainsail", "fluidd")


class ZmodUpdateAll:
    def __init__(self, config: Any) -> None:
        self.server = config.get_server()
        self.um: Any = None
        self.request_type: Any = None
        self.hooked = False
        self.defer = False

    async def component_init(self) -> None:
        # Every component exists by now, update_manager's endpoints too.
        um = self.server.lookup_component("update_manager", None)
        if um is None:
            logging.info("zmod_update_all: no update_manager, not hooking")
            return
        um_mod = sys.modules[type(um).__module__]
        rtype = um_mod.RequestType
        common = sys.modules[rtype.__module__]
        api_cache = common.APIDefinition.get_cache()
        swapped = []
        for ep in FULL_ENDPOINTS:
            api_def = api_cache.get(ep)
            if api_def is None:
                continue
            # APIDefinition is a frozen dataclass; the HTTP handler and the
            # JSON-RPC method hold this same object.
            object.__setattr__(api_def, "callback", self._handle_full)
            swapped.append(ep)
        # machine.update.upgrade / client without a name call this method
        # on the instance.
        um._handle_full_update_request = self._handle_full
        self.um = um
        self.request_type = rtype
        self.hooked = True
        for name in WEB_UPDATERS:
            updater = um.get_updaters().get(name)
            if updater is not None:
                updater.update = self._wrap_web_update(updater.update)
        self.defer = self._hook_plugin_scripts()
        self.server.register_endpoint(
            "/server/zmod_update_all/message", rtype.POST,
            self._handle_message)
        self.server.register_endpoint(
            "/server/zmod_update_all/info", rtype.GET, self._handle_info)
        logging.info("zmod_update_all: full update routed to update_all.py "
                     f"({', '.join(swapped) or 'upgrade only'})")

    def _hook_plugin_scripts(self) -> bool:
        """Zmod's GitDeploy.update() runs a plugin's update.sh right after
        the pull with asyncio.create_subprocess_exec. Give git_deploy its
        own asyncio stand-in whose create_subprocess_exec runs a copy of
        that script with its reboot redirected (update_all.strip_reboots),
        but only while the queue is updating that very plugin."""
        mod = sys.modules.get(type(self.um).__module__.rsplit(".", 1)[0]
                              + ".git_deploy")
        real = getattr(mod, "asyncio", None)
        if mod is None or real is not asyncio:
            logging.info("zmod_update_all: git_deploy not as expected, "
                         "plugin reboots are not deferred")
            return False
        mod.asyncio = _AsyncioProxy(real, self._plugin_exec)
        return True

    async def _plugin_exec(self, program: Any, *args: Any,
                           **kwargs: Any) -> Any:
        real = asyncio.create_subprocess_exec
        path = str(program)
        if args or not (path.endswith("/update.sh")
                        and "/mod_data/plugins/" in path):
            return await real(program, *args, **kwargs)
        name = os.path.basename(os.path.dirname(path))
        try:
            ua = _load_update_all()
            if not ua.defer_wanted(name):
                return await real(program, *args, **kwargs)
            with open(path, "rb") as f:
                text = f.read()
            new, changed = ua.strip_reboots(text)
        except Exception as e:
            logging.info(f"zmod_update_all: {name}: {e}, running update.sh "
                         "unchanged")
            return await real(program, *args, **kwargs)
        if not changed:
            return await real(program, *args, **kwargs)
        interp = ["/bin/sh"]
        first = text.split(b"\n", 1)[0]
        if first.startswith(b"#!") and first[2:].split():
            interp = [p.decode() for p in first[2:].split()]
        logging.info(f"zmod_update_all: running {name}/update.sh with its "
                     "reboot deferred to the end of the queue")
        # sh -c SCRIPT PATH: $0 is the real path, so dirname "$0" still
        # finds the plugin folder.
        return await real(*interp, "-c", new.decode("utf-8", "replace"),
                          path, **kwargs)

    def _wrap_web_update(self, orig: Any) -> Any:
        async def update(*args: Any, **kwargs: Any) -> Any:
            try:
                return await orig(*args, **kwargs)
            finally:
                await self._inject()
        return update

    async def _inject(self) -> None:
        python = PYTHON if os.path.exists(PYTHON) else sys.executable
        try:
            proc = await asyncio.create_subprocess_exec(
                python, SCRIPT, "--inject", env=self._env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 30)
        except Exception as e:
            logging.info(f"zmod_update_all: re-adding the button failed: {e}")

    @staticmethod
    def _env() -> dict:
        return {k: v for k, v in os.environ.items()
                if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")}

    async def _handle_full(self, web_request: Any) -> str:
        if self.um.kconn.is_printing():
            raise self.server.error("Update Refused: Klippy is printing", 503)
        if self.um.cmd_helper.is_update_busy():
            raise self.server.error("An update is already running", 503)
        self.send("Starting UPDATE_ALL. Progress also shows in the console.")
        env = self._env()
        python = PYTHON if os.path.exists(PYTHON) else sys.executable
        try:
            # update_all.py forks into the background by itself; its own
            # session keeps it alive across a Moonraker restart.
            subprocess.Popen([python, SCRIPT], env=env,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        except Exception as e:
            self.send(f"Could not start UPDATE_ALL: {e}", True)
            raise self.server.error(f"Could not start UPDATE_ALL: {e}", 500)
        return "ok"

    async def _handle_info(self, web_request: Any) -> dict:
        return {"defer_plugin_reboots": self.defer}

    async def _handle_message(self, web_request: Any) -> str:
        msg = web_request.get_str("msg", "")
        complete = web_request.get_boolean("complete", False)
        self.send(msg, complete)
        return "ok"

    def send(self, msg: str, complete: bool = False) -> None:
        self.server.send_event("update_manager:update_response", {
            "message": "UPDATE_ALL: " + msg.strip(),
            "application": "full",
            "proc_id": 0,
            "complete": complete})


def _load_update_all() -> Any:
    """update_all.py as a module, read fresh each time so a plugin update
    takes effect without a Moonraker restart."""
    spec = importlib.util.spec_from_file_location("zmod_update_all_script",
                                                  SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _AsyncioProxy:
    """asyncio for git_deploy alone, with one function replaced."""

    def __init__(self, real: Any, exec_fn: Any) -> None:
        self._real = real
        self.create_subprocess_exec = exec_fn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def load_component(config: Any) -> ZmodUpdateAll:
    return ZmodUpdateAll(config)
