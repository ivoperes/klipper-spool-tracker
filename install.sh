#!/bin/bash
# Spool tracker installer for Klipper + KlipperScreen.
#   ./install.sh            install / re-install (safe to run again)
#   ./install.sh uninstall  remove the KlipperScreen hook and symlinks
#
# Run as the user that runs Klipper (e.g. pi or biqu), NOT with sudo.
#
# Copyright (C) 2026  ivoperes (https://github.com/ivoperes)
# This file may be distributed under the terms of the GNU GPLv3 license.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Do not run install.sh as root or with sudo." >&2
    echo "Run it as the user that runs Klipper, e.g.:  ./install.sh" >&2
    exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
KS_DIR="${KS_DIR:-$HOME/KlipperScreen}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"
BASE_PANEL="$KS_DIR/panels/base_panel.py"

exclude() { # keep git status clean for Moonraker's update manager
    local repo="$1" path="$2"
    [ -d "$repo/.git" ] || return 0
    grep -qxF "$path" "$repo/.git/info/exclude" 2>/dev/null || echo "$path" >> "$repo/.git/info/exclude"
}

# Adds or removes the indicator hook between BEGIN/END markers.
# Writes atomically (temp file + rename), keeps a one-time backup, and
# refuses to touch a file it does not recognise.
ks_hook() {
    python3 - "$1" "$BASE_PANEL" <<'EOF'
import os, shutil, sys, tempfile
action, path = sys.argv[1], sys.argv[2]
BEGIN = "        # >>> spool_tracker indicator >>>\n"
END = "        # <<< spool_tracker indicator <<<\n"
ANCHOR = '        self.titlebar.add(self.control["battery_box"])\n'
HOOK = (BEGIN +
        "        try:\n"
        "            from ks_includes.spool_indicator import SpoolIndicator\n"
        "            self.spool_indicator = SpoolIndicator(self)\n"
        "        except Exception as e:\n"
        "            logging.error(f\"spool_tracker indicator failed: {e}\")\n"
        + END)
with open(path) as f:
    src = f.read()
present = BEGIN in src and END in src
if action == "install":
    if present:
        print("KlipperScreen hook already present"); sys.exit(0)
    if src.count(ANCHOR) != 1:
        sys.exit("base_panel.py does not look like a supported KlipperScreen "
                 "version; not modifying it")
    backup = path + ".spool_tracker.bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    new = src.replace(ANCHOR, ANCHOR + HOOK, 1)
else:
    if not present:
        print("KlipperScreen hook not present"); sys.exit(0)
    start = src.index(BEGIN)
    new = src[:start] + src[src.index(END) + len(END):]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as f:
    f.write(new)
shutil.copymode(path, tmp)
os.replace(tmp, path)
print("KlipperScreen hook %s" % ("added" if action == "install" else "removed"))
EOF
}

if [ "${1:-}" = "uninstall" ]; then
    rm -f "$KLIPPER_DIR/klippy/extras/spool_tracker.py" "$KS_DIR/ks_includes/spool_indicator.py"
    if [ -f "$BASE_PANEL" ]; then ks_hook uninstall; fi
    echo "Removed. Also delete [include spool_tracker.cfg] from printer.cfg, the"
    echo "[menu __main spool] section from KlipperScreen.conf and"
    echo "[update_manager spool_tracker] from moonraker.conf, then restart:"
    echo "  sudo systemctl restart moonraker klipper KlipperScreen"
    exit 0
fi

[ -d "$KLIPPER_DIR/klippy/extras" ] || { echo "Klipper not found at $KLIPPER_DIR (set KLIPPER_DIR)" >&2; exit 1; }
ln -sfn "$SRC/klipper/spool_tracker.py" "$KLIPPER_DIR/klippy/extras/spool_tracker.py"
exclude "$KLIPPER_DIR" "klippy/extras/spool_tracker.py"
echo "Klipper module linked"

if [ -d "$CONFIG_DIR" ] && [ ! -e "$CONFIG_DIR/spool_tracker.cfg" ]; then
    cp "$SRC/klipper/spool_tracker.cfg" "$CONFIG_DIR/spool_tracker.cfg"
    echo "Copied spool_tracker.cfg to $CONFIG_DIR"
fi

if [ -f "$BASE_PANEL" ]; then
    ln -sfn "$SRC/klipperscreen/spool_indicator.py" "$KS_DIR/ks_includes/spool_indicator.py"
    exclude "$KS_DIR" "ks_includes/spool_indicator.py"
    ks_hook install
else
    echo "KlipperScreen not found at $KS_DIR (set KS_DIR); skipping the screen indicator"
fi

cat <<EOF

Done. Remaining steps:
 1. Add this line to printer.cfg:   [include spool_tracker.cfg]
 2. Add the menu button from klipperscreen/KlipperScreen.conf.snippet
    to $CONFIG_DIR/KlipperScreen.conf
 3. Optional, for updates from Mainsail/Fluidd: add the section from
    moonraker/update_manager.conf.snippet to $CONFIG_DIR/moonraker.conf
 4. Restart:   sudo systemctl restart moonraker klipper KlipperScreen
 5. Tap "Spool" (or the % in the title bar) to load your first spool.

After a KlipperScreen update, run ./install.sh again to restore the indicator.
EOF
