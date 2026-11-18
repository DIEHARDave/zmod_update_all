#!/bin/sh
# zmod_update_all - run by Zmod's DISABLE_PLUGIN name=zmod_update_all.
# Removes install.sh's link and include.

[ -f /opt/config/mod/.shell/0.sh ] && . /opt/config/mod/.shell/0.sh
[ -f /usr/data/zmod/zmod/.shell/0.sh ] && . /usr/data/zmod/zmod/.shell/0.sh

set -u

NAME=zmod_update_all
COMP=zmod_update_all
CHROOT_PLUGIN_DIR="/opt/config/mod_data/plugins/$NAME"
PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
CONFIG_DIR=$(CDPATH= cd -- "$PLUGIN_DIR/../../.." && pwd -P)

if [ ! -f /ZMOD ] && [ -z "${ZMOD_UPDATE_ALL_CHROOT:-}" ] &&
    [ -n "${MOD:-}" ] && [ -d "$MOD" ]; then
    unset LD_LIBRARY_PATH LD_PRELOAD
    exec chroot "$MOD" /bin/sh -c \
        "ZMOD_UPDATE_ALL_CHROOT=1 $CHROOT_PLUGIN_DIR/uninstall.sh"
fi

FILE="$CONFIG_DIR/mod_data/plugins.moonraker.conf"
[ -f "$FILE" ] && sed -i "\|plugins/$NAME/$NAME\.moonraker\.conf|d" "$FILE"

MOON_DIR=
for dir in ${DATA:+"$DATA/zmod/moonraker"} /usr/data/zmod/moonraker \
    /data/zmod/moonraker /root/printer_data/config/base/moonraker \
    "$CONFIG_DIR/base/moonraker"; do
    if [ -d "$dir/components" ]; then
        MOON_DIR=$dir
        break
    fi
done
if [ -n "$MOON_DIR" ]; then
    TARGET="$MOON_DIR/components/$COMP.py"
    if [ -L "$TARGET" ]; then
        case "$(readlink "$TARGET")" in
            "$CHROOT_PLUGIN_DIR"/*) rm "$TARGET" ;;
        esac
    fi
    EXCLUDE="$MOON_DIR/.git/info/exclude"
    [ -f "$EXCLUDE" ] && sed -i "\|^/components/$COMP\.py\$|d" "$EXCLUDE"
fi

echo "$NAME uninstalled"
echo "REBOOT" >/tmp/printer
