# Spool Tracker for Klipper + KlipperScreen

Keeps a running estimate of how much filament is left on the loaded spool.

- **Load / switch spool on the touchscreen.** First you see what's left on the
  current spool (so you can write it on the spool), then you're asked **how full
  the new spool is** (100%, 95% … 5%) and whether it's the **same type as last
  time** (e.g. PLA, 1 kg). You only pick the material and size when they change.
- **Tracks usage automatically.** Filament extruded during every print is taken
  off the spool. The amount is saved every 30 s and survives restarts.
- **Checks before printing.** When a print starts, the slicer's filament estimate
  (+5% margin) is compared with what's left. If it might run out, the print is
  held and you get **Cancel / Switch spool / Print anyway**. After you load a new
  spool it offers to start the held print.
- **Always-visible indicator.** The KlipperScreen title bar shows the spool icon
  and `63%` on every screen. It turns red when low, and tapping it opens Switch Spool.

The dialogs use Klipper's built-in prompts, so they also show up in Mainsail and
Fluidd.

## Files

| File | Goes to |
|---|---|
| `klipper/spool_tracker.py` | `~/klipper/klippy/extras/` (symlinked by installer) |
| `klipper/spool_tracker.cfg` | `~/printer_data/config/` (options, all optional) |
| `klipperscreen/spool_indicator.py` | `~/KlipperScreen/ks_includes/` (symlinked) |
| `klipperscreen/KlipperScreen.conf.snippet` | paste into `KlipperScreen.conf` |
| `install.sh` | run on the printer's Pi |
| `tests/` | off-printer tests (`python -m unittest discover -s tests`) |

## Install (on the Pi)

1. Copy this folder to the Pi, e.g. `~/spool_tracker` (WinSCP, or
   `scp -r "D:\Claude\3D Printer Klipper" pi@<printer-ip>:~/spool_tracker`).
2. Run:
   ```bash
   cd ~/spool_tracker && chmod +x install.sh && ./install.sh
   ```
3. Add `[include spool_tracker.cfg]` to `printer.cfg`.
4. Paste `klipperscreen/KlipperScreen.conf.snippet` into `KlipperScreen.conf`
   (adds a **Spool** button to the main menu).
5. `sudo systemctl restart klipper KlipperScreen`
6. Tap **Spool** and load your current spool.

Needs `[virtual_sdcard]` (standard with Moonraker) and an up-to-date
KlipperScreen (one that shows macro `action:prompt` dialogs; update it first if yours is old).

## Commands

| Command | What it does |
|---|---|
| `SPOOL_SWITCH` | Show what's left on the current spool, then load a new one |
| `SPOOL_LOAD` | Load a spool (asks on screen). Direct: `SPOOL_LOAD PERCENT=80 MATERIAL=PETG WEIGHT=1000` |
| `SPOOL_SET PERCENT=40` | Correct the remaining amount without changing spool |
| `SPOOL_STATUS` | Console report: remaining, last print's usage, previous spool |
| `SDCARD_PRINT_FILE ... SPOOL_SKIP_CHECK=1` | Start a print without the check |

Status is available to macros and UIs as `printer.spool_tracker`
(`percent_remaining`, `remaining_mm`, `remaining_g`, `material`, `low`, …).

## Good to know

- **Only filament extruded during prints is counted.** Purges and manual
  load/unload outside a print are not counted, so use `SPOOL_SET` now and then to
  correct it. This mostly balances out against the safety margin.
- **Accuracy depends on your % guess when loading.** "Write N% on the spool"
  rounds down to the nearest 5% so you can pick that same button when you reuse it.
- **Supported slicer estimates:** PrusaSlicer, SuperSlicer, OrcaSlicer,
  Bambu Studio, Cura, Simplify3D and ideaMaker. If a file has no estimate, the
  print starts normally and the console says the check was skipped.
- **KlipperScreen updates:** the indicator needs a small hook in
  `panels/base_panel.py`, so Moonraker's update manager will show KlipperScreen
  as modified. Run `./install.sh uninstall` before updating, then `./install.sh`
  again afterwards. The Klipper module isn't affected by Klipper updates.
- Densities, spool sizes, margin and the low-warning level can all be changed in
  `spool_tracker.cfg`.
