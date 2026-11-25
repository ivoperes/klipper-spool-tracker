# Developer notes

Everything a contributor needs to change Spool Tracker safely: how it works,
which Klipper/KlipperScreen internals it depends on, the data it stores, the
security review, and the rules for changing it.

Contents

1. [Repository layout](#1-repository-layout)
2. [Architecture](#2-architecture)
3. [How each feature works](#3-how-each-feature-works)
4. [Klipper internals we depend on](#4-klipper-internals-we-depend-on)
5. [KlipperScreen internals we depend on](#5-klipperscreen-internals-we-depend-on)
6. [The prompt (dialog) protocol](#6-the-prompt-dialog-protocol)
7. [State file](#7-state-file)
8. [Maths and units](#8-maths-and-units)
9. [Commands and dialog flow](#9-commands-and-dialog-flow)
10. [Security review (OWASP)](#10-security-review-owasp)
11. [Secure coding rules for this project](#11-secure-coding-rules-for-this-project)
12. [Testing](#12-testing)
13. [Releasing changes](#13-releasing-changes)
14. [Known limitations and ideas](#14-known-limitations-and-ideas)

---

## 1. Repository layout

| Path | Runs where | Purpose |
|---|---|---|
| `klipper/spool_tracker.py` | Inside the Klipper process (`klippy`) | All logic: tracking, pre-print check, dialogs, state |
| `klipper/spool_tracker.cfg` | Klipper config | Documented options with defaults |
| `klipperscreen/spool_indicator.py` | Inside the KlipperScreen process | Title-bar `% left` indicator |
| `klipperscreen/KlipperScreen.conf.snippet` | KlipperScreen config | Main-menu **Spool** button |
| `moonraker/update_manager.conf.snippet` | Moonraker config | Update-manager entry |
| `install.sh` | Shell on the printer host | Symlinks files, hooks KlipperScreen, uninstalls |
| `tests/test_spool_tracker.py` | Any PC with Python 3 | Off-printer tests with fake Klipper objects |
| `LICENSE`, `klipperscreen/LICENSE` | | GPLv3 (repo) and AGPLv3 (`klipperscreen/`) |

The installer **symlinks** the two Python files into the Klipper and
KlipperScreen checkouts, so `git pull` in `~/spool_tracker` (or the update
manager) updates the running code. A restart of the service is still needed.

## 2. Architecture

```
            Mainsail / Fluidd / KlipperScreen  (any Moonraker client)
                      |  G-code commands      ^  "// action:prompt_*" lines
                      v  + status queries     |  + printer.spool_tracker status
                 +-----------------------------------+
                 |             Moonraker             |
                 +-----------------------------------+
                      |                       ^
                      v                       |
  +--------------------------------------------------------------+
  | klippy (Klipper host process)                                 |
  |                                                              |
  |  SDCARD_PRINT_FILE --> SpoolTracker.cmd_SDCARD_PRINT_FILE     |
  |                          | reads slicer estimate (allow-listed |
  |                          | file, 64 KB head + 512 KB tail)     |
  |                          v                                     |
  |                    enough? --yes--> original SDCARD_PRINT_FILE |
  |                          | no                                  |
  |                          v                                     |
  |                    dialog: Cancel / Switch spool / Print anyway|
  |                                                              |
  |  reactor timer (every 2 s) --> print_stats.filament_used     |
  |        delta --> state.remaining_mm --> spool_tracker.json    |
  +--------------------------------------------------------------+

  KlipperScreen title bar: SpoolIndicator polls printer.objects.query
  {"spool_tracker": null} every 3 s through KlipperScreen's own websocket.
```

Design decisions

- **Everything lives in one Klipper module.** Dialogs use Klipper's standard
  `action:prompt` protocol, so KlipperScreen, Mainsail and Fluidd all show them
  without any UI code of ours. The only UI code is the title-bar indicator.
- **The check wraps `SDCARD_PRINT_FILE`** instead of asking users to edit
  slicer start G-code. Every way of starting a print (KlipperScreen, Mainsail,
  Fluidd, Moonraker API, job queue) ends in that command.
- **A held-back print never starts.** It doesn't start and then pause, so no
  heaters come on and no `PAUSE`/`RESUME` macro quirks are involved.
- **The check fails open.** If anything goes wrong reading the estimate, the
  print starts as normal. The check is advisory and must never stop someone
  printing.
- **The indicator polls instead of subscribing.** Moonraker allows only one
  object subscription per connection, and KlipperScreen owns it. Adding our
  object would mean patching `screen.py` too.

## 3. How each feature works

### 3.1 Usage tracking

`_handle_ready` registers a reactor timer `_poll` that runs every 2 s:

1. Read `print_stats.get_status(eventtime)["filament_used"]`: mm of filament
   pushed by the extruder in the current print, net of retractions and
   corrected for the extrusion multiplier (`M221`).
2. `delta = used - last_used`. Subtract it from `remaining_mm` and add it to
   `used_since_load_mm`.
3. If `used` went down by more than 1 mm, the counter was reset (a new print).
   The `virtual_sdcard:reset_file` event also sets `last_used = 0`.
4. Mark the state dirty. Save at most every `save_interval` seconds (30 by
   default) to limit SD-card writes.
5. When `percent_remaining` first drops to `low_warning_percent` or below,
   send one console warning.

Only extrusion **during a print** is counted, because `print_stats` only
counts while a file is printing.

### 3.2 Pre-print check

`_handle_connect` swaps out the `SDCARD_PRINT_FILE` handler:
`gcode.register_command(name, None)` returns the old handler, and we register
ours, which calls the old one. Our handler:

1. Skips the check (calls the original directly) when `SPOOL_SKIP_CHECK=1` is
   passed, no spool is loaded, or a print is already active.
2. `resolve_gcode_path()` maps `FILENAME` to a path **only if it appears in
   `virtual_sdcard.get_file_list(check_subdirs=True)`**, which is exactly the set
   of files the original command can print. Matching is case-insensitive,
   like `virtual_sdcard`.
3. `read_filament_estimate()` reads at most 64 KB from the start and 512 KB
   from the end of the file and runs `parse_filament_estimate()`.
4. `required = needed × (1 + safety_margin/100)`. If it's no more than
   `remaining_mm`, it starts the print and logs the numbers to the console.
5. Otherwise it stores `pending_file` and shows the "Not enough filament"
   dialog. `SPOOL_PRINT_ANYWAY` later starts `pending_file` through the
   original handler. `SPOOL_PROMPT_CLOSE` clears it.

Slicer comment formats recognised, in priority order (the last match in the
file wins within a pattern):

| Slicer | Line | Unit | Multi-tool |
|---|---|---|---|
| PrusaSlicer, SuperSlicer, Orca, Bambu | `; filament used [mm] = 123.4, 0` | mm | summed |
| Orca, Bambu header | `; total filament length [mm] : 123.4` | mm | summed |
| Cura | `;Filament used: 1.234m, 0m` | m | summed |
| Simplify3D | `;   Filament length: 1234.5 mm (1.23 m)` | mm | first number only |
| ideaMaker | `;Material#1 Used: 1234.5` | mm | first number only |

### 3.3 Load / switch spool

The dialogs form a small state machine driven entirely by G-code parameters,
so the module itself keeps no "wizard" state. See [section 9](#9-commands-and-dialog-flow).

### 3.4 Title-bar indicator

`install.sh` adds a marked hook block to `panels/base_panel.py`, just after
the battery box is added to the title bar. The hook constructs
`SpoolIndicator(self)`, which:

- builds an `EventBox` (spool icon + label), adds it to the title bar and moves
  it to index 2 (after the title, before the clock);
- every 3 s sends `printer.objects.query {"spool_tracker": null}` through
  `screen._ws.send_method`;
- shows `NN%`, adds the `spoolman_low` CSS class (KlipperScreen's own red
  style) when `low` is true, and hides itself if the object doesn't exist;
- sends `SPOOL_SWITCH` when tapped.

## 4. Klipper internals we depend on

These aren't all public API. Check them whenever Klipper changes these files.
Last verified against Klipper `master` on 2026-11-25.

| What | Where in Klipper | Used for | If it changes |
|---|---|---|---|
| `print_stats.get_status()["filament_used"]`, `["state"]`, `["filename"]` | `klippy/extras/print_stats.py` | Usage tracking, "is printing" | Tracking stops; `_poll` logs errors but Klipper keeps running |
| `virtual_sdcard.sdcard_dirname`, `get_file_list(check_subdirs)`, `is_active()` | `klippy/extras/virtual_sdcard.py` | File allow-list, path, busy check | Check fails open (print starts) |
| Event `virtual_sdcard:reset_file` | `virtual_sdcard._reset_file()` | Reset usage counter on a new print | The `used < last_used - 1` fallback still catches resets |
| `gcode.register_command(cmd, None)` returns the old handler | `klippy/gcode.py` | Wrapping `SDCARD_PRINT_FILE` | Logged; pre-print check disabled |
| `gcode.create_gcode_command()` | `klippy/gcode.py` | Starting the held print | `SPOOL_PRINT_ANYWAY` errors |
| `gcode.respond_raw()` | `klippy/gcode.py` | Emitting `// action:` lines | Dialogs disappear |
| `gcmd.get_float/get_int(minval, maxval)` | `klippy/gcode.py` | Parameter validation (**does not reject NaN**, see SEC-2) | |
| Reactor timers (`register_timer`, return next waketime) | `klippy/reactor.py` | Polling | |
| `printer.get_start_args()["config_file"]` | `klippy/klippy.py` | Default state-file location | Set `state_file:` explicitly |

**Reactor rule:** Klipper runs single-threaded on a cooperative reactor. Code
in a timer or command handler must not block for long (it delays everything,
including communication with the printer board) and must not let exceptions
escape a timer (Klipper shuts down). The only blocking I/O we do is the
bounded file read at print start, when the printer is idle, and the small
state-file write.

## 5. KlipperScreen internals we depend on

Last verified against KlipperScreen `master` on 2026-11-25.

| What | Where | Notes |
|---|---|---|
| Anchor line `self.titlebar.add(self.control["battery_box"])` | `panels/base_panel.py` `BasePanel.__init__` | The installer refuses to patch unless it appears **exactly once** |
| `base_panel.titlebar`, `_screen`, `_gtk`, `bts` | `BasePanel` / `ScreenPanel` | Widget placement and sizing |
| `gtk.Image("spool")` | Theme icons | Falls back to `"filament"` |
| CSS class `spoolman_low` | KlipperScreen styles | Red text for the low state |
| `screen._ws.send_method(method, params, callback)` | `ks_includes/KlippyWebsocket.py` | Callback runs via `GLib.idle_add` with `(response, method, params)`. It **must return `False`** or GLib calls it again forever |
| `screen._send_action(widget, "printer.gcode.script", {...})` | `screen.py` | Sends `SPOOL_SWITCH` on tap |
| Prompt support (`ks_includes/widgets/prompts.py`) | | See the limits in section 6 |

The hook is wrapped in `try/except`, so a broken indicator logs an error and
KlipperScreen keeps working.

## 6. The prompt (dialog) protocol

Klipper module → console lines → clients render a dialog:

```
// action:prompt_begin <title>
// action:prompt_text <one line of text>
// action:prompt_button_group_start
// action:prompt_button <label>|<gcode>|<style>
// action:prompt_button_group_end
// action:prompt_footer_button <label>|<gcode>|<style>
// action:prompt_show
// action:prompt_end
```

Styles: `primary`, `secondary`, `info`, `warning`, `error`.

Protocol facts that shape the code:

- **Each line is one message.** A newline inside any value starts a new
  protocol line. That is the injection risk fixed in SEC-4.
- **`|` separates label, G-code and style.** It must never appear in a label,
  and the G-code is built only from validated values.
- **KlipperScreen shows only the last `prompt_text` line**, so each dialog has
  one text line. Mainsail shows all of them.
- **Pressing a button doesn't close the dialog.** Each command we bind either
  opens the next dialog (`prompt_begin` replaces the old one) or sends
  `prompt_end`.
- **Closing with the X** sends `RESPOND ... action:prompt_end`, not our command,
  so `pending_file` may stay set. That's harmless: it's replaced or cleared by
  the next check, and starting it needs a deliberate `SPOOL_PRINT_ANYWAY`.

## 7. State file

`spool_tracker.json`, next to `printer.cfg` by default (`state_file:`
overrides it). It's written atomically: `.tmp` file, `fsync`, then
`os.replace`. It's saved at most every `save_interval` seconds while
printing, immediately after load/set, and on Klipper disconnect.

| Key | Type / range | Meaning |
|---|---|---|
| `loaded` | bool | A spool is being tracked (forced `false` if `capacity_mm` ≤ 0) |
| `material` | one of the configured materials | Selects density |
| `spool_weight` | int 1–100000 g | Net filament weight of a full spool |
| `capacity_mm` | 0–1e8 | Length of a full spool |
| `remaining_mm` | −1e8–1e8 | Estimated length left (may go negative if you print past empty) |
| `loaded_percent` | 0–100 | % chosen when loaded |
| `loaded_at` | Unix time or null | |
| `used_since_load_mm` | −1e8–1e8 | |
| `last_print` | `{file, used_mm, time}` or null | Last print's usage |
| `previous_spool` | `{material, spool_weight, percent, remaining_mm, removed_at}` or null | The spool removed at the last switch |

**The file is untrusted input.** It sits in the config folder that
Mainsail/Fluidd can edit, and power loss can corrupt it. `sanitize_state()`
rebuilds the state field by field. Anything missing, of the wrong type,
out of range, NaN or infinite falls back to its default, and text is cleaned.
Invalid JSON means starting with no spool loaded, with an error logged.

## 8. Maths and units

Internal unit: **mm of filament**. Grams are only for display.

```
area_mm2   = π × (diameter / 2)²                       1.75 mm → 2.405 mm²
length_mm  = grams / density[g/cm³] × 1000 / area_mm2  (cm³ → mm³ is ×1000)
grams      = length_mm × area_mm2 × density / 1000
capacity   = length_mm(spool_weight, material)
remaining  = capacity × percent / 100                  (at load)
percent    = clamp(remaining / capacity × 100, 0, 100)
```

Example: 1 kg PLA (1.24 g/cm³) is 1000 / 1.24 × 1000 / 2.405 ≈ 335,300 mm,
or about 335 m.

"Write N% on the spool" uses `floor(percent / 5) × 5`, so the number always
matches a button (buttons go in 5% steps) and errs on the low side.

## 9. Commands and dialog flow

| Command | Parameters | Notes |
|---|---|---|
| `SPOOL_STATUS` | | Console report |
| `SPOOL_SWITCH` | | Refuses while printing (pause first). Loaded → summary dialog; nothing loaded → % dialog |
| `SPOOL_LOAD` | `PERCENT` 0–100, `MATERIAL` (allow-listed), `WEIGHT` 1–100000, `STEP=material` | Fills in whatever's missing by asking |
| `SPOOL_SET` | `PERCENT` 0–100 (required) | Corrects the remaining amount |
| `SPOOL_PRINT_ANYWAY` | | Starts `pending_file` |
| `SPOOL_PROMPT_CLOSE` | | Closes the dialog, clears `pending_file` |
| `SDCARD_PRINT_FILE` | adds `SPOOL_SKIP_CHECK=1` | Wrapped original |

```
SPOOL_SWITCH ─(loaded)─> [Switch spool: N% left, write M%] ─Load new spool─┐
     └──(not loaded)──────────────────────────────────────────────────────>┤
                                                                           v
                         SPOOL_LOAD ─> [How full? 100..5%] ─> SPOOL_LOAD PERCENT=p
                                                                           v
                   [Same type as last time: PLA 1 kg?] ─Yes─> SPOOL_LOAD PERCENT MATERIAL WEIGHT ─> saved
                                │ Change type                                         │
                                v                                                     v
              [Which material?] ─> [Spool size?] ─> SPOOL_LOAD ... ─> saved     pending print?
                                                                         └─> [Spool loaded: start print?]
```

## 10. Security review (OWASP)

Review date 2026-11-25, against the **OWASP Top 10:2025** and the **OWASP
Secure Coding Practices** checklist. These are web-application lists, but
most categories map onto a printer add-on. The irrelevant ones are marked N/A.

### 10.1 Threat model

**Assets**
- The print in progress (hours of work and material)
- Printer safety and uptime: a Klipper shutdown stops heaters and motion mid-print
- The integrity of what the user is shown in dialogs
- The host's files

**Actors and trust boundaries**

| Actor | Trust | Notes |
|---|---|---|
| Moonraker API clients (KlipperScreen, Mainsail, Fluidd, scripts) | **Trusted** with full printer control | Any client that can send G-code can already run `FIRMWARE_RESTART`, edit config and upload files through Moonraker. Moonraker's `[authorization]` (trusted clients, API key, login) is the access-control boundary, not this module |
| G-code files and their slicer comments | **Untrusted data** | Downloaded models are often sliced elsewhere or shared |
| `spool_tracker.json` | **Untrusted data** | Editable from the web UI's config editor, and can be corrupted by power loss |
| `printer.cfg` options | Trusted, but validated | A typo must give a clear config error, not a crash later |
| This GitHub repository | Trusted code supply | With the update manager on `channel: dev`, every push to `main` can be installed with one click |

**What we defend against:** malformed or hostile slicer output and state
files; accidental bad input (typos, `nan`); breaking out of the dialog
protocol; crashing the printer; the installer damaging other software.

**Out of scope:** an attacker who already has Moonraker access. They control
the printer anyway, so harden Moonraker instead (see SECURITY.md).

### 10.2 OWASP Top 10:2025 mapping

| # | Category | Applies? | Status |
|---|---|---|---|
| A01 | Broken Access Control | Yes: file access | **Fixed SEC-1.** Only files `virtual_sdcard` would print can be read. Command access is delegated to Moonraker auth (by design) |
| A02 | Security Misconfiguration | Yes: config, installer | **Fixed SEC-5, SEC-7.** Config values are range-checked; the installer refuses root and uses strict mode. Deployment guidance in SECURITY.md |
| A03 | Software Supply Chain Failures | Yes | No third-party dependencies (standard library only). Update channel risk documented (SEC-8); maintainer checklist in section 13 |
| A04 | Cryptographic Failures | N/A | No secrets, credentials or crypto |
| A05 | Injection | Yes: dialog protocol, G-code parameters | **Fixed SEC-4.** All dialog text is output-encoded, button G-code is built only from allow-listed values, and unsafe buttons are refused. No shell, `eval`, SQL or HTML |
| A06 | Insecure Design | Yes | Fail-open advisory check, held print never starts, no wizard state; see section 2 |
| A07 | Authentication Failures | N/A | No authentication in this component (Moonraker's job) |
| A08 | Software or Data Integrity Failures | Yes: state file | **Fixed SEC-3, SEC-6.** Schema validation on load, strict JSON (`allow_nan=False`), atomic writes with `fsync` |
| A09 | Logging and Alerting Failures | Yes | Every failure path logs to `klippy.log` with context. A silent `except: pass` was removed (SEC-9). Logs hold no secrets |
| A10 | Mishandling of Exceptional Conditions | Yes | **Fixed SEC-2, SEC-3.** NaN/inf rejected, the timer can't raise, the pre-print check fails open, bad JSON is handled |

### 10.3 Findings

Severity takes into account that Moonraker clients are already trusted, so
most items are defence in depth. Availability bugs that can stop a print
rank highest.

| ID | Finding | Severity | Fix | Regression test |
|---|---|---|---|---|
| SEC-1 | **Path traversal.** `resolve_gcode_path` joined user `FILENAME` onto the gcodes dir and opened it if it existed, so `../x` or `//etc/x` read files outside it (only a parsed number was revealed) | Low | Allow-list against `virtual_sdcard.get_file_list()`; no path building from raw input | `test_traversal_outside_gcodes_not_read` |
| SEC-2 | **NaN/inf input.** Klipper's `get_float(minval, maxval)` accepts `nan`, which corrupted and saved the state and could break clients' JSON. `WEIGHT` had no upper bound | Medium | `_get_percent()` rejects non-finite values; `WEIGHT` ≤ 100000; estimates must be finite and ≤ 1e8 mm; `json.dump(allow_nan=False)` | `test_nan_and_inf_rejected`, `test_weight_upper_limit`, `test_estimate_inf_ignored` |
| SEC-3 | **Tampered/corrupt state could shut Klipper down.** Wrong types loaded verbatim made `_poll` raise inside a reactor timer, which stops the printer, even mid-print | High | `sanitize_state()` schema validation; `_poll` wrapped in `try/except` with logging; `update_usage` ignores non-numeric input | `test_tampered_state_types`, `test_state_not_a_dict_or_not_json`, `test_poll_never_raises` |
| SEC-4 | **Dialog protocol injection.** Text from the state file (material) or file names went into `// action:` lines unencoded. A newline could add a fake button running any G-code | Medium | `clean_text()` strips control characters and `\|` from every title, text and label, with length limits; `_button()` refuses G-code with unsafe characters; material names are allow-listed (`[A-Z0-9_+-]{1,16}`) | `test_state_material_cannot_inject_prompt_lines`, `test_filename_cannot_inject_prompt_lines`, `test_unsafe_button_refused` |
| SEC-5 | **Config not range-checked.** A density of 0 caused division by zero at spool load; negative or huge weights were accepted | Low | `parse_materials()` (0 < density ≤ 25) and `parse_weights()` (1–100000) raise config errors at startup | `test_bad_material_config_rejected` |
| SEC-6 | **State write not crash-safe.** No `fsync` before rename, so power loss could leave an empty file | Low | `flush()` + `os.fsync()` before `os.replace()` | `test_saved_state_is_strict_json` |
| SEC-7 | **Installer robustness.** No `-u`/`pipefail`; could run as root (files end up in `/root`, root-owned symlinks); patched KlipperScreen non-atomically; uninstall removed a fixed number of lines (an off-by-one there, caught in testing before release, would have deleted a KlipperScreen line) | Medium | `set -euo pipefail`; refuses root; BEGIN/END markers; anchor must match exactly once; temp file + rename; one-time `.spool_tracker.bak` backup | Manual round-trip test (section 12) |
| SEC-8 | **Update channel.** `channel: dev` installs whatever is on `main` | Accepted risk | Maintainer controls (section 13); users can pin a commit or tag | |
| SEC-9 | **Swallowed error.** `except Exception: pass` hid file-listing failures | Low | Logged with `logging.exception` | |

### 10.4 Accepted risks and residual notes

- **No access control inside the module.** That is by design and matches all
  Klipper modules. Anyone who can send G-code can change the spool level or
  start a held print. Secure Moonraker.
- **Blocking read at print start.** Up to 576 KB is read on the reactor
  thread. It's bounded, and it only happens when no print is running.
- **The indicator lives in KlipperScreen's address space** and patches one of
  its files. If KlipperScreen changes the anchor line, the installer refuses
  to patch, and the indicator is simply missing.
- **`pending_file`** can be started by any client with `SPOOL_PRINT_ANYWAY`.
  The original command still validates the file name.
- **The estimate is only as honest as the G-code file.** A file can claim to
  need less filament than it does. The check is a convenience, not a safety
  interlock.

## 11. Secure coding rules for this project

Follow these in every change. Reviewers should check them.

1. **Validate at the boundary, with allow-lists.** G-code parameters, config,
   the state file and G-code file content are all input. Prefer allow-lists
   (material names, the file list) to deny-lists. Always bound numbers, and
   reject non-finite floats (`math.isfinite`). Klipper's `minval`/`maxval`
   won't do it for you.
2. **Encode output for its destination.** Anything in a `// action:` line goes
   through `clean_text()`. Button G-code is built only from validated values
   and goes through `_button()`. Never put raw user or file text into G-code.
3. **Never let an exception escape a reactor timer or event handler.** Catch,
   log with `logging.exception`, and carry on. Command handlers should raise
   `gcmd.error(...)` for user errors, which Klipper reports in the console.
4. **Fail safe for the print.** Advisory features fail open (the print
   proceeds). Never pause, cancel or block a print because of a tracker
   error.
5. **Don't block the reactor.** Bound all I/O. Do nothing slow during a print.
6. **Write files atomically:** temp file, `fsync`, `os.replace`. Never
   `json.dump` NaN.
7. **No new dependencies** without a strong reason. Klipper's Python
   environment is shared.
8. **No shell, `eval`, `exec`, `pickle` or `subprocess`** in the Klipper
   module.
9. **Keep logs useful and free of secrets.** Log what failed and the file or
   command involved.
10. **Every security fix gets a regression test** in `TestSecurity`.

## 12. Testing

Off-printer, on any OS with Python 3.8+:

```bash
python -m unittest discover -s tests -v
```

`tests/test_spool_tracker.py` builds fake `printer`, `gcode`, `reactor`,
`print_stats` and `virtual_sdcard` objects that mirror the internals in
section 4, then drives the real module through its commands and timer.
Expected log noise: the security tests deliberately feed bad data, so
tracebacks from `logging.exception` appear in the output. That's normal.

Covered: estimate parsing for every supported slicer, length/weight maths,
the interactive load flow, usage tracking across prints, persistence across
restarts, low warning, switch flow, every pre-print check branch, and all SEC
findings.

**Not covered automatically**

- Real Klipper and KlipperScreen. Check by hand after changes: start a print
  with too little filament; *Switch spool → load* → start; watch the % fall
  during a print; restart Klipper mid-print and check the value persists.
- The GTK indicator (`spool_indicator.py`): check on the screen.
- `install.sh`: run `bash -n install.sh`, then test the hook round trip on a
  copy of KlipperScreen's `panels/base_panel.py`. Install twice (no duplicate),
  check it parses (`python3 -c "import ast; ast.parse(open('base_panel.py').read())"`),
  uninstall twice, and `diff` it against the original.

## 13. Releasing changes

Users with the Moonraker update manager receive whatever is on `main`. So:

- Run the test suite and `bash -n install.sh` before every push.
- Protect the account: **GitHub two-factor authentication on**. Consider
  branch protection on `main` (no force-push, no deletion).
- Keep commits small, with clear messages. Never commit `spool_tracker.json`
  or anything from a printer's config folder (`.gitignore` covers the state
  file).
- Keep `.gitattributes` (`eol=lf`): `install.sh` must keep LF line endings to
  run on Linux.
- If a change needs new config, say so in the commit message and README.
  Existing state files must keep loading; `sanitize_state()` gives defaults
  for new keys.
- For a more conservative release flow, tag releases (`v1.0.0`) and switch
  the update manager to `channel: stable`, which only installs tagged
  versions.

## 14. Known limitations and ideas

- Only extrusion during prints is counted. Load/unload macros and manual
  extrusion could be counted by tracking the extruder position when idle.
- Single extruder only. Multi-tool would need one state per tool.
- The estimate comes from slicer comments. Moonraker's metadata
  (`filament_total`) could be used as a fallback, but Klipper can't query
  Moonraker directly.
- KlipperScreen updates require re-running `install.sh`. An upstream
  "title-bar plugin" hook in KlipperScreen would remove the patch.
- Optional Spoolman sync if the user ever adopts Spoolman.
