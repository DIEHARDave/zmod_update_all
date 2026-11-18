#!/bin/sh
# zmod_update_all - run by Zmod's Moonraker after this plugin updated (only
# while the plugin is enabled). Puts the Moonraker link and include in place
# without a reboot: the update queue reboots at its end anyway.
cd "$(dirname -- "$0")" || exit 1
ZMOD_UPDATE_ALL_NO_REBOOT=1 exec ./install.sh
