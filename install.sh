#!/bin/bash
# Spool tracker installer for Klipper + KlipperScreen.
#   ./install.sh            install / re-install (safe to run again)
#   ./install.sh uninstall  remove the KlipperScreen hook and symlinks
set -e

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
KS_DIR="${KS_DIR:-$HOME/KlipperScreen}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"
BASE_PANEL="$KS_DIR/panels/base_panel.py"
MARKER="# spool_tracker indicator"

exclude() { # keep git status clean for Moonraker's update manager
    local repo="$1" path="$2"
    [ -d "$repo/.git" ] || return 0
    grep -qxF "$path" "$repo/.git/info/exclude" 2>/dev/null || echo "$path" >> "$repo/.git/info/exclude"
}

patch_base_panel() {
    python3 - "$BASE_PANEL" "$MARKER" <<'EOF'
import sys
path, marker = sys.argv[1], sys.argv[2]
src = open(path).read()
if marker in src:
    print("KlipperScreen hook already present")
    sys.exit(0)
anchor = '        self.titlebar.add(self.control["battery_box"])\n'
if anchor not in src:
    sys.exit("Could not find the title bar code in base_panel.py; "
             "this KlipperScreen version is not supported by install.sh")
hook = (anchor +
        "        " + marker + "\n"
        "        try:\n"
        "            from ks_includes.spool_indicator import SpoolIndicator\n"
        "            self.spool_indicator = SpoolIndicator(self)\n"
        "        except Exception as e:\n"
        "            logging.error(f\"spool_tracker indicator failed: {e}\")\n")
open(path, "w").write(src.replace(anchor, hook, 1))
print("KlipperScreen hook added")
EOF
}

unpatch_base_panel() {
    python3 - "$BASE_PANEL" "$MARKER" <<'EOF'
import sys
path, marker = sys.argv[1], sys.argv[2]
lines = open(path).read().split("\n")
out, skip = [], 0
for line in lines:
    if line.strip() == marker:
        skip = 5  # try / import / create / except / log
        continue
    if skip:
        skip -= 1
        continue
    out.append(line)
open(path, "w").write("\n".join(out))
print("KlipperScreen hook removed")
EOF
}

if [ "$1" = "uninstall" ]; then
    rm -f "$KLIPPER_DIR/klippy/extras/spool_tracker.py" "$KS_DIR/ks_includes/spool_indicator.py"
    [ -f "$BASE_PANEL" ] && unpatch_base_panel
    echo "Removed. Also delete [include spool_tracker.cfg] from printer.cfg and the"
    echo "[menu __main spool] section from KlipperScreen.conf, then restart:"
    echo "  sudo systemctl restart klipper KlipperScreen"
    exit 0
fi

[ -d "$KLIPPER_DIR/klippy/extras" ] || { echo "Klipper not found at $KLIPPER_DIR (set KLIPPER_DIR)"; exit 1; }
ln -sf "$SRC/klipper/spool_tracker.py" "$KLIPPER_DIR/klippy/extras/spool_tracker.py"
exclude "$KLIPPER_DIR" "klippy/extras/spool_tracker.py"
echo "Klipper module linked"

if [ ! -f "$CONFIG_DIR/spool_tracker.cfg" ]; then
    cp "$SRC/klipper/spool_tracker.cfg" "$CONFIG_DIR/spool_tracker.cfg"
    echo "Copied spool_tracker.cfg to $CONFIG_DIR"
fi

if [ -f "$BASE_PANEL" ]; then
    ln -sf "$SRC/klipperscreen/spool_indicator.py" "$KS_DIR/ks_includes/spool_indicator.py"
    exclude "$KS_DIR" "ks_includes/spool_indicator.py"
    patch_base_panel
else
    echo "KlipperScreen not found at $KS_DIR (set KS_DIR); skipping the screen indicator"
fi

cat <<EOF

Done. Remaining steps:
 1. Add this line to printer.cfg:   [include spool_tracker.cfg]
 2. Add the menu button from klipperscreen/KlipperScreen.conf.snippet
    to $CONFIG_DIR/KlipperScreen.conf
 3. Restart:   sudo systemctl restart klipper KlipperScreen
 4. Tap "Spool" (or the % in the title bar) to load your first spool.

After a KlipperScreen update, run ./install.sh again to restore the indicator.
EOF
