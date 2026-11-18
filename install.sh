#!/bin/sh
# zmod_update_all - run by Zmod's ENABLE_PLUGIN name=zmod_update_all (and,
# rerun by hand, to repair the link after a Zmod update).
#
# In Zmod's chroot (Moonraker): links moonraker/zmod_update_all.py into
# Moonraker's components and includes zmod_update_all.moonraker.conf. Then
# asks Zmod for a reboot, so Moonraker loads it (not when update.sh runs it:
# UPDATE_ALL reboots by itself at the end of the queue).

[ -f /opt/config/mod/.shell/0.sh ] && . /opt/config/mod/.shell/0.sh
[ -f /usr/data/zmod/zmod/.shell/0.sh ] && . /usr/data/zmod/zmod/.shell/0.sh

set -eu

NAME=zmod_update_all
COMP=zmod_update_all
# /opt/config is the config folder both on the host and in the chroot.
CHROOT_PLUGIN_DIR="/opt/config/mod_data/plugins/$NAME"
PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
CONFIG_DIR=$(CDPATH= cd -- "$PLUGIN_DIR/../../.." && pwd -P)

# Moonraker runs in Zmod's chroot (unless /ZMOD says this is the chroot)
if [ ! -f /ZMOD ] && [ -z "${ZMOD_UPDATE_ALL_CHROOT:-}" ] &&
    [ -n "${MOD:-}" ] && [ -d "$MOD" ]; then
    unset LD_LIBRARY_PATH LD_PRELOAD
    exec chroot "$MOD" /bin/sh -c \
        "ZMOD_UPDATE_ALL_CHROOT=1 ZMOD_UPDATE_ALL_NO_REBOOT=${ZMOD_UPDATE_ALL_NO_REBOOT:-} $CHROOT_PLUGIN_DIR/install.sh"
fi

# Zmod 1.7+ keeps Moonraker in $DATA/zmod/moonraker, older ones in
# config/base/moonraker.
MOON_DIR=
for dir in ${DATA:+"$DATA/zmod/moonraker"} /usr/data/zmod/moonraker \
    /data/zmod/moonraker /root/printer_data/config/base/moonraker \
    "$CONFIG_DIR/base/moonraker"; do
    if [ -d "$dir/components" ]; then
        MOON_DIR=$dir
        break
    fi
done
if [ -z "$MOON_DIR" ]; then
    echo "$NAME: Zmod's Moonraker not found" >&2
    exit 1
fi
if ! grep -q "_handle_full_update_request"     "$MOON_DIR/components/update_manager/update_manager.py" 2>/dev/null; then
    echo "$NAME: this Moonraker's update manager is not supported,"         "UPDATE_ALL still works from the console" >&2
    exit 0
fi

TARGET="$MOON_DIR/components/$COMP.py"
if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
    echo "$NAME: refusing to replace $TARGET" >&2
    exit 1
fi
ln -sfn "$CHROOT_PLUGIN_DIR/moonraker/$COMP.py" "$TARGET"
# Keep Moonraker's update manager from reporting the link as an untracked
# file in the moonraker repo
if [ -d "$MOON_DIR/.git" ]; then
    EXCLUDE="$MOON_DIR/.git/info/exclude"
    mkdir -p "$MOON_DIR/.git/info"
    touch "$EXCLUDE"
    grep -qxF "/components/$COMP.py" "$EXCLUDE" ||
        echo "/components/$COMP.py" >>"$EXCLUDE"
fi

INC="[include plugins/$NAME/$NAME.moonraker.conf]"
FILE="$CONFIG_DIR/mod_data/plugins.moonraker.conf"
touch "$FILE"
grep -qF "$INC" "$FILE" || echo "$INC" >>"$FILE"

# Put the Update all button back into Mainsail and Fluidd
python3 "$CHROOT_PLUGIN_DIR/update_all.py" --inject || true

echo "$NAME installed; Update all in Mainsail/Fluidd uses the update queue after the reboot"
[ -n "${ZMOD_UPDATE_ALL_NO_REBOOT:-}" ] || echo "REBOOT" >/tmp/printer
