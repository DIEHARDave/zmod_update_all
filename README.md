# zmod_update_all

An update queue for **Zmod** printers (Flashforge AD5X, Creator 5, AD5M).
One macro installs every available update and reboots as few times as
possible. You no longer click Update on each item and reboot after every one.

```
UPDATE_ALL            check, install every update, reboot when needed
UPDATE_ALL CHECK=1    only list the available updates
UPDATE_ALL REBOOT=0   plugins only, reboot yourself later
UPDATE_ALL CANCEL=1   drop a queue that is waiting for a reboot
```

Progress appears in the console (Fluidd, Mainsail, HelixScreen):

```
UPDATE_ALL: Checking for updates...
UPDATE_ALL: 4 update(s): fluidd v1.37.17 -> v1.37.18, ff_nfc v0.2.5 -> v0.2.6, moon 0.10.1-18 -> 0.10.1-19, Z-Mod 1.7.6-46 -> 1.7.6-55
UPDATE_ALL: moon, Z-Mod each reboot the printer by themselves. The queue carries on after each reboot.
UPDATE_ALL: Updating fluidd...
UPDATE_ALL: Updating ff_nfc...
UPDATE_ALL: Updating moon. The printer reboots afterwards and UPDATE_ALL carries on by itself.
   (reboot)
UPDATE_ALL: moon updated.
UPDATE_ALL: Resuming after the reboot, 1 left: Z-Mod
UPDATE_ALL: Updating Z-Mod. The printer reboots afterwards and UPDATE_ALL carries on by itself.
   (reboot)
UPDATE_ALL: Z-Mod updated.
UPDATE_ALL: Done. Updated 4: fluidd, ff_nfc, moon, Z-Mod.
```

## How many reboots

Zmod's Moonraker reboots the printer by itself straight after it updates
**Z-Mod**, **klippy** (Klipper) or **moon** (Moonraker). Nothing can stop
that, so each of those costs one reboot. Plugins and web UIs do not reboot.

| Updates waiting | Reboots |
|---|---|
| plugins only | 1, at the end |
| plugins + Z-Mod | 1 (the Z-Mod reboot also loads the plugins) |
| plugins + klippy + moon + Z-Mod | 3 |

You start it once; the reboots and the resume after each are automatic.

## What it does

1. Refuses to start while a print is running or while Moonraker is already
   updating something.
2. Asks Moonraker to check every update source (the same as **Refresh**).
3. Updates the plugins and web UIs first, one at a time, through Moonraker's
   normal update path. Plugin `update.sh` scripts still run.
4. Then klippy, moon and Z-Mod, in that order. Before each one it saves the
   queue to `/root/.zmod_update_all.json` in the chroot. After the reboot,
   `_UPDATE_ALL_RESUME` (a `delayed_gcode` at Klipper start) waits for
   Moonraker and Klipper, checks the item really updated, and carries on.
5. Skips items it cannot update safely (local changes, an invalid or corrupt
   repo, a detached HEAD) and says why. Fix those from the update panel
   (Recover), then run `UPDATE_ALL` again.
6. Keeps going if one item fails, and lists the failures at the end. An item
   that is still out of date after its reboot is not tried again, so a bad
   update cannot cause a reboot loop.
7. Stops if a print starts. After a reboot it waits until Moonraker's own
   startup refresh is done before checking versions. A queue is resumed
   at most 6 times, then dropped.
8. Reboots once with Zmod's `REBOOT` macro at the end, only if something was
   updated since the last reboot.

The work runs in the background inside Zmod's chroot, so Klipper stays
responsive. Full log: `mod_data/log/zmod_update_all.log` in the printer's config
folder (Fluidd/Mainsail show it), kept across the reboots.

## Install

Add this to `mod_data/user.moonraker.conf`:

```ini
[update_manager zmod_update_all]
type: git_repo
channel: stable
path: /root/printer_data/config/mod_data/plugins/zmod_update_all
origin: https://github.com/DIEHARDave/zmod_update_all.git
is_system_service: False
primary_branch: main
```

Then run `ENABLE_PLUGIN name=zmod_update_all` in the console.

## Uninstall

`DISABLE_PLUGIN name=zmod_update_all`, then remove the section from
`user.moonraker.conf`.

## Testing over SSH

```sh
chroot /usr/data/.mod/.zmod python3 \
    /root/printer_data/config/mod_data/plugins/zmod_update_all/update_all.py \
    --check --foreground
```

## License

GPLv3. See [LICENSE](LICENSE).
