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

## Install (on the printer's Pi)

SSH into the Pi (e.g. `ssh biqu@<printer-ip>`; the user is often `pi` or
`biqu`), then:

1. **Download and install:**
   ```bash
   git clone https://github.com/ivoperes/klipper-spool-tracker.git ~/spool_tracker
   cd ~/spool_tracker && ./install.sh
   ```
2. Add `[include spool_tracker.cfg]` to `printer.cfg`.
3. Paste [`klipperscreen/KlipperScreen.conf.snippet`](klipperscreen/KlipperScreen.conf.snippet)
   into `KlipperScreen.conf` (adds a **Spool** button to the main menu).
4. Paste [`moonraker/update_manager.conf.snippet`](moonraker/update_manager.conf.snippet)
   into `moonraker.conf` (adds Spool Tracker to Mainsail/Fluidd's update manager).
5. Restart: `sudo systemctl restart moonraker klipper KlipperScreen`
6. Tap **Spool** and load your current spool.

Steps 2 to 4 can also be done in Mainsail/Fluidd's config editor.

Needs `[virtual_sdcard]` (standard with Moonraker) and an up-to-date
KlipperScreen (one that shows macro `action:prompt` dialogs; update it first if
yours is old).

## Updating

New versions show up in Mainsail/Fluidd under **Settings → Update Manager**
(Machine → Update Manager in Fluidd). Click **Update** and Klipper and
KlipperScreen restart automatically.

Without the update manager:

```bash
cd ~/spool_tracker && git pull && sudo systemctl restart klipper KlipperScreen
```

Your spool data (`spool_tracker.json`) and `spool_tracker.cfg` settings are kept.

## Files

| File | What it is |
|---|---|
| `klipper/spool_tracker.py` | Klipper module (linked into `~/klipper/klippy/extras/` by the installer) |
| `klipper/spool_tracker.cfg` | Options, all optional (copied to `~/printer_data/config/`) |
| `klipperscreen/spool_indicator.py` | Title bar indicator (linked into `~/KlipperScreen/ks_includes/`) |
| `klipperscreen/KlipperScreen.conf.snippet` | Main-menu **Spool** button |
| `moonraker/update_manager.conf.snippet` | Update manager entry |
| `install.sh` | Installer (`./install.sh uninstall` removes it) |
| `tests/` | Off-printer tests: `python -m unittest discover -s tests` |
| `docs/DEVELOPER_NOTES.md` | Design, internals, security review |

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
  as modified. Run `~/spool_tracker/install.sh uninstall` before updating
  KlipperScreen, then `~/spool_tracker/install.sh` again afterwards.
- Densities, spool sizes, margin and the low-warning level can all be changed in
  `spool_tracker.cfg`.

## Security and development

- [SECURITY.md](SECURITY.md): how to report a vulnerability, and how to keep
  your printer's web interface safe.
- [docs/DEVELOPER_NOTES.md](docs/DEVELOPER_NOTES.md): how it works, the
  Klipper/KlipperScreen internals it relies on, the OWASP security review, and
  the coding rules for contributors.

## License

Copyright (C) 2026 ivoperes.

- Everything in this repository is licensed under the
  [GNU General Public License v3.0](LICENSE), the same license as Klipper,
  **except** the `klipperscreen/` folder.
- `klipperscreen/` is licensed under the
  [GNU Affero General Public License v3.0](klipperscreen/LICENSE), the same
  license as KlipperScreen, because it runs inside KlipperScreen.

You may use, change and share this software under those licenses. Anyone who
distributes it, or a modified version, must also make their source code
available under the same license. There is no warranty.

## Acknowledgements

This add-on builds on, and only works because of, these projects:

- **[Klipper](https://github.com/Klipper3d/klipper)** by Kevin O'Connor and
  contributors (GPLv3). Spool Tracker is a Klipper module and uses its
  module API, `print_stats` (filament used per print), `virtual_sdcard`
  (print start), and its G-code command and status systems.
- **[KlipperScreen](https://github.com/KlipperScreen/KlipperScreen)**, started
  by Jordan Ruthe and maintained by Alfredo Monclus and contributors (AGPLv3).
  The indicator plugs into its title bar (`panels/base_panel.py`), reuses its
  spool icon and low-filament style, and the dialogs rely on its support for
  Klipper prompts.
- **[Moonraker](https://github.com/Arksine/moonraker)** by Eric Callahan
  (Arksine) and contributors (GPLv3). It carries the status and commands
  between Klipper and the screens, and provides the update manager.
- **[Mainsail](https://github.com/mainsail-crew/mainsail)** and
  **[Fluidd](https://github.com/fluidd-core/fluidd)** teams, for the
  `action:prompt` dialog convention used for all on-screen questions.
- Filament densities are typical manufacturer values for each material.

No code from these projects is copied into this repository. The installer adds
a short, clearly marked hook to your local copy of KlipperScreen's
`panels/base_panel.py` (keeping a `.spool_tracker.bak` backup) and removes it
again on uninstall.
