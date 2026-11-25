# Spool tracker: estimate how much filament is left on the loaded spool.
#
# - Load/switch a spool with a couple of on-screen questions (% full, type)
# - Deducts filament as it is extruded during prints (print_stats)
# - Before a print starts, compares the slicer's filament estimate with
#   what is left and asks whether to continue, switch spool, or cancel
# - Exposes `printer.spool_tracker` status for KlipperScreen / Mainsail
#
# Install: copy or symlink into ~/klipper/klippy/extras/ and add a
# [spool_tracker] section to printer.cfg (see spool_tracker.cfg).
#
# Copyright (C) 2026  ivoperes (https://github.com/ivoperes)
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# See the LICENSE file in the repository root.
#
# Security notes (see docs/DEVELOPER_NOTES.md for the full threat model):
# - Every value that reaches a G-code parameter, a dialog line or the state
#   file is validated here; nothing trusts the state file or slicer output.
# - Files are only read if virtual_sdcard itself would print them.
# - Nothing in the polling timer may raise: an exception in a Klipper timer
#   shuts the printer down.
import json
import logging
import math
import os
import re
import time

DEFAULT_MATERIALS = "PLA:1.24, PETG:1.27, ABS:1.04, ASA:1.07, TPU:1.21, " \
                    "NYLON:1.14, PC:1.20"
DEFAULT_WEIGHTS = "250, 500, 750, 1000, 2000, 3000"
PERCENT_STEPS = list(range(100, 0, -5))   # 100, 95, ... 5

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 512 * 1024

# Input limits
MAX_SPOOL_WEIGHT = 100000          # grams (100 kg)
MAX_DENSITY = 25.                  # g/cm3, far above any filament
MAX_LENGTH_MM = 1e8                # 100 km; anything larger is corrupt data
MAX_TEXT = 200                     # characters shown in a dialog line
MATERIAL_RE = re.compile(r"^[A-Z0-9_+\-]{1,16}$")
# Characters that would break the line-based "// action:prompt_*" protocol:
# control characters (incl. CR/LF) and the "|" field separator.
UNSAFE_TEXT_RE = re.compile(r"[\x00-\x1f\x7f|]")


def clean_text(value, limit=MAX_TEXT):
    """Make a value safe to show in a prompt line (output encoding)."""
    return UNSAFE_TEXT_RE.sub(" ", str(value))[:limit]


def is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))

# Slicer comments holding the total filament length.  Each entry is
# (regex, multiplier-to-mm, multi) where multi means the line holds one
# comma separated value per tool, to be added up.
ESTIMATE_PATTERNS = [
    # PrusaSlicer / SuperSlicer / OrcaSlicer / Bambu Studio
    (re.compile(r"^;\s*filament used \[mm\]\s*=\s*(.+)$", re.I | re.M),
     1., True),
    # OrcaSlicer / Bambu Studio header block
    (re.compile(r"^;\s*total filament length \[mm\]\s*:\s*(.+)$",
                re.I | re.M), 1., True),
    # Cura  (";Filament used: 12.3456m" or "1.2m, 0.5m")
    (re.compile(r"^;\s*Filament used:\s*(.+)$", re.I | re.M), 1000., True),
    # Simplify3D (";   Filament length: 12345.6 mm (12.35 m)")
    (re.compile(r"^;\s*Filament length:\s*(.+)$", re.I | re.M), 1., False),
    # ideaMaker (";Material#1 Used: 1234.5")
    (re.compile(r"^;\s*Material#\d+ Used:\s*(.+)$", re.I | re.M), 1., False),
]
NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def parse_filament_estimate(text):
    """Return total filament length in mm found in g-code text, or None."""
    for regex, scale, multi in ESTIMATE_PATTERNS:
        matches = regex.findall(text)
        if not matches:
            continue
        # Use the last occurrence (summary blocks come after the header)
        values = [float(v) for v in NUMBER_RE.findall(matches[-1])]
        if not multi:
            values = values[:1]
        if values:
            total = sum(v for v in values if v > 0.) * scale
            # Reject inf/absurd values from malformed or hostile files
            if 0. < total <= MAX_LENGTH_MM:
                return total
    return None


def read_filament_estimate(path):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
            tail = b""
            if size > HEAD_BYTES:
                f.seek(max(HEAD_BYTES, size - TAIL_BYTES))
                tail = f.read()
    except (IOError, OSError):
        logging.exception("spool_tracker: unable to read %s", path)
        return None
    text = (head + b"\n" + tail).decode("utf-8", "ignore")
    return parse_filament_estimate(text)


def parse_materials(text):
    materials = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, dens = item.partition(":")
        name = name.strip().upper()
        # Names end up inside G-code commands, so allow-list the characters
        if not MATERIAL_RE.match(name):
            raise ValueError("Invalid material name '%s' (use A-Z, 0-9, "
                             "_ + -, max 16 characters)" % (clean_text(name),))
        density = float(dens)
        if not (math.isfinite(density) and 0. < density <= MAX_DENSITY):
            raise ValueError("Invalid density for %s" % (name,))
        materials.append((name, density))
    if not materials:
        raise ValueError("No materials configured")
    return materials


def parse_weights(text):
    weights = [int(float(w)) for w in text.split(",") if w.strip()]
    if not weights or any(not 0 < w <= MAX_SPOOL_WEIGHT for w in weights):
        raise ValueError("spool_weights must be 1-%d grams" % MAX_SPOOL_WEIGHT)
    return weights


class SpoolTracker:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.name = config.get_name()
        # Configuration
        self.diameter = config.getfloat("filament_diameter", 1.75, above=0.)
        try:
            self.materials = parse_materials(
                config.get("materials", DEFAULT_MATERIALS))
            self.weights = parse_weights(
                config.get("spool_weights", DEFAULT_WEIGHTS))
        except ValueError as e:
            raise config.error("[%s] %s" % (self.name, e))
        self.densities = dict(self.materials)
        self.default_material = config.get(
            "default_material", self.materials[0][0]).upper()
        if self.default_material not in self.densities:
            raise config.error("[%s] default_material %s is not in materials"
                               % (self.name, self.default_material))
        self.default_weight = config.getint("default_spool_weight", 1000,
                                            minval=1, maxval=MAX_SPOOL_WEIGHT)
        self.safety_margin = config.getfloat("safety_margin", 5., minval=0.,
                                             maxval=1000.)
        self.low_percent = config.getfloat("low_warning_percent", 10.,
                                           minval=0., maxval=100.)
        self.check_before_print = config.getboolean("check_before_print",
                                                    True)
        self.save_interval = config.getfloat("save_interval", 30., minval=1.)
        state_file = config.get("state_file", None)
        if state_file is None:
            cfg = self.printer.get_start_args().get("config_file", "")
            state_file = os.path.join(os.path.dirname(cfg) or ".",
                                      "spool_tracker.json")
        self.state_file = os.path.expanduser(state_file)
        # Runtime state
        self.state = self._default_state()
        self._load_state()
        self.print_stats = None
        self.v_sd = None
        self.orig_print_file = None
        self.last_used = 0.
        self.last_file = ""
        self.new_print = False
        self.dirty = False
        self.last_save = 0.
        self.low_warned = False
        self.pending_file = None
        self.poll_timer = None
        # Events
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.printer.register_event_handler("klippy:ready",
                                            self._handle_ready)
        self.printer.register_event_handler("klippy:disconnect",
                                            self._save_state)
        self.printer.register_event_handler("virtual_sdcard:reset_file",
                                            self._handle_reset_file)
        # Commands
        for cmd in ["SPOOL_STATUS", "SPOOL_SWITCH", "SPOOL_LOAD", "SPOOL_SET",
                    "SPOOL_PRINT_ANYWAY", "SPOOL_PROMPT_CLOSE"]:
            self.gcode.register_command(
                cmd, getattr(self, "cmd_" + cmd),
                desc=getattr(self, "cmd_" + cmd + "_help"))

    # ------------------------------------------------------------------
    # State
    def _default_state(self):
        return {"loaded": False, "material": self.default_material,
                "spool_weight": self.default_weight, "capacity_mm": 0.,
                "remaining_mm": 0., "loaded_percent": 0., "loaded_at": None,
                "used_since_load_mm": 0., "last_print": None,
                "previous_spool": None}

    def _load_state(self):
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
        except (IOError, OSError):
            return   # first run: no state yet
        except ValueError:
            logging.exception("spool_tracker: corrupt state file %s, "
                              "starting with no spool loaded",
                              self.state_file)
            return
        self.state = self.sanitize_state(data)

    def sanitize_state(self, data):
        """Rebuild state from untrusted JSON, keeping only valid fields.

        The state file lives in the config folder and can be edited from
        Mainsail/Fluidd, so it is treated like any other external input.
        """
        st = self._default_state()
        if not isinstance(data, dict):
            logging.warning("spool_tracker: ignoring state file (not a dict)")
            return st

        def num(src, key, lo, hi, default):
            v = src.get(key, default)
            return float(v) if is_number(v) and lo <= v <= hi else default

        material = data.get("material")
        if material in self.densities:
            st["material"] = material
        elif data.get("loaded"):
            logging.warning("spool_tracker: unknown material in state file, "
                            "using %s", self.default_material)
        w = data.get("spool_weight")
        if is_number(w) and 0 < w <= MAX_SPOOL_WEIGHT:
            st["spool_weight"] = int(w)
        st["capacity_mm"] = num(data, "capacity_mm", 0., MAX_LENGTH_MM, 0.)
        st["remaining_mm"] = num(data, "remaining_mm", -MAX_LENGTH_MM,
                                 MAX_LENGTH_MM, 0.)
        st["loaded_percent"] = num(data, "loaded_percent", 0., 100., 0.)
        st["used_since_load_mm"] = num(data, "used_since_load_mm",
                                       -MAX_LENGTH_MM, MAX_LENGTH_MM, 0.)
        st["loaded_at"] = num(data, "loaded_at", 0., 1e11, None)
        st["loaded"] = data.get("loaded") is True and st["capacity_mm"] > 0.
        lp = data.get("last_print")
        if isinstance(lp, dict):
            st["last_print"] = {
                "file": clean_text(lp.get("file") or "", 255),
                "used_mm": num(lp, "used_mm", 0., MAX_LENGTH_MM, 0.),
                "time": num(lp, "time", 0., 1e11, None)}
        prev = data.get("previous_spool")
        if isinstance(prev, dict):
            pw = prev.get("spool_weight")
            st["previous_spool"] = {
                "material": clean_text(prev.get("material") or "?", 16),
                "spool_weight": int(pw) if is_number(pw)
                and 0 < pw <= MAX_SPOOL_WEIGHT else 0,
                "percent": num(prev, "percent", 0., 100., 0.),
                "remaining_mm": num(prev, "remaining_mm", -MAX_LENGTH_MM,
                                    MAX_LENGTH_MM, 0.),
                "removed_at": num(prev, "removed_at", 0., 1e11, None)}
        return st

    def _save_state(self, *args):
        """Atomic, crash-safe write: temp file + fsync + rename."""
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                # allow_nan=False: never persist NaN/inf (invalid JSON)
                json.dump(self.state, f, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
            self.dirty = False
            self.last_save = self.reactor.monotonic()
        except (IOError, OSError, ValueError, TypeError):
            logging.exception("spool_tracker: unable to save %s",
                              self.state_file)

    # ------------------------------------------------------------------
    # Math helpers
    def _area(self):
        return math.pi * (self.diameter / 2.) ** 2

    def mm_for_weight(self, grams, material):
        density = self.densities.get(material, self.densities.get(
            self.default_material))
        # g / (g/cm^3) = cm^3 -> mm^3 (x1000) / mm^2 = mm
        return grams / density * 1000. / self._area()

    def grams_for_mm(self, mm, material):
        density = self.densities.get(material, self.densities.get(
            self.default_material))
        return mm * self._area() * density / 1000.

    def percent_remaining(self):
        cap = self.state["capacity_mm"]
        if not self.state["loaded"] or cap <= 0.:
            return 0.
        return max(0., min(100., self.state["remaining_mm"] / cap * 100.))

    def describe_spool(self):
        return "%s %s" % (self.state["material"],
                          self._fmt_weight(self.state["spool_weight"]))

    @staticmethod
    def _fmt_weight(grams):
        if grams >= 1000 and grams % 1000 == 0:
            return "%d kg" % (grams // 1000,)
        return "%d g" % (grams,)

    def _fmt_amount(self, mm):
        return "%.1f m / %d g" % (mm / 1000., round(self.grams_for_mm(
            max(mm, 0.), self.state["material"])))

    # ------------------------------------------------------------------
    # Klippy events
    def _handle_connect(self):
        self.print_stats = self.printer.lookup_object("print_stats", None)
        self.v_sd = self.printer.lookup_object("virtual_sdcard", None)
        if self.v_sd is not None and self.check_before_print:
            self.orig_print_file = self.gcode.register_command(
                "SDCARD_PRINT_FILE", None)
            if self.orig_print_file is None:
                logging.warning("spool_tracker: SDCARD_PRINT_FILE not found;"
                                " pre-print check disabled")
            else:
                self.gcode.register_command(
                    "SDCARD_PRINT_FILE", self.cmd_SDCARD_PRINT_FILE,
                    desc="Loads a SD file and starts the print (with "
                         "spool_tracker filament check)")

    def _handle_ready(self):
        if self.print_stats is not None:
            eventtime = self.reactor.monotonic()
            stats = self.print_stats.get_status(eventtime)
            self.last_used = stats["filament_used"]
            self.last_file = stats.get("filename") or ""
        self.poll_timer = self.reactor.register_timer(
            self._poll, self.reactor.NOW)

    def _handle_reset_file(self):
        # print_stats has just been reset for a new file
        self.last_used = 0.
        self.new_print = True

    def _poll(self, eventtime):
        # An exception escaping a reactor timer shuts Klipper down, possibly
        # mid-print. Tracking filament is never worth that: log and carry on.
        try:
            if self.print_stats is not None:
                self.update_usage(self.print_stats.get_status(eventtime))
            if self.dirty and eventtime - self.last_save >= self.save_interval:
                self._save_state()
        except Exception:
            logging.exception("spool_tracker: error while tracking usage")
        return eventtime + 2.

    def update_usage(self, stats):
        used = stats.get("filament_used")
        if not is_number(used):
            return
        fname = stats.get("filename") or ""
        new_print = self.new_print
        if fname != self.last_file:
            # A different file was loaded, so print_stats was reset even if
            # we did not see the event. Same-file reprints rely on the event.
            # A drop in filament_used is NOT a reset: it goes down on every
            # retraction (e.g. the end-of-print retract) and coming back
            # from it must not count the whole print a second time.
            self.last_used = 0.
            new_print = True
        self.last_file = fname
        delta = used - self.last_used
        self.last_used = used
        if abs(delta) < 1e-6:
            return
        self.new_print = False
        lp = self.state.get("last_print")
        if not lp or new_print or lp.get("file") != fname:
            lp = {"file": fname, "used_mm": 0., "time": time.time()}
        lp["used_mm"] = used
        self.state["last_print"] = lp
        if not self.state["loaded"]:
            self.dirty = True
            return
        self.state["remaining_mm"] -= delta
        self.state["used_since_load_mm"] += delta
        self.dirty = True
        pct = self.percent_remaining()
        if pct <= self.low_percent and not self.low_warned:
            self.low_warned = True
            self.gcode.respond_info(
                "Spool tracker: filament low, about %.0f%% (%s) left"
                % (pct, self._fmt_amount(self.state["remaining_mm"])))
        elif pct > self.low_percent:
            self.low_warned = False

    def get_status(self, eventtime):
        st = self.state
        remaining = st["remaining_mm"] if st["loaded"] else 0.
        return {
            "loaded": st["loaded"],
            "material": st["material"],
            "spool_weight": st["spool_weight"],
            "percent_remaining": round(self.percent_remaining(), 1),
            "remaining_mm": round(remaining, 1),
            "remaining_g": round(self.grams_for_mm(max(remaining, 0.),
                                                   st["material"]), 1),
            "capacity_mm": round(st["capacity_mm"], 1),
            "low": st["loaded"] and self.percent_remaining()
                   <= self.low_percent,
            "low_warning_percent": self.low_percent,
            "pending_print": self.pending_file,
        }

    # ------------------------------------------------------------------
    # Prompt helpers (Klipper "action:prompt" dialogs, shown by
    # KlipperScreen, Mainsail and Fluidd)
    def _respond(self, msg):
        self.gcode.respond_raw("// action:%s" % (msg,))

    def _button(self, kind, label, cmd, style):
        # The G-code behind a button is built only from validated values, so
        # an unsafe character here means a bug: refuse rather than emit it.
        if UNSAFE_TEXT_RE.search(cmd) or UNSAFE_TEXT_RE.search(style):
            logging.error("spool_tracker: refusing unsafe button %r", cmd)
            return
        self._respond("%s %s|%s|%s" % (kind, clean_text(label, 40), cmd,
                                       style))

    def _prompt(self, title, text, groups=(), footer=()):
        # Every line is one "// action:" message; clean_text() strips
        # newlines and "|" so data can never add lines or buttons.
        self._respond("prompt_begin %s" % (clean_text(title, 60),))
        self._respond("prompt_text %s" % (clean_text(text, 400),))
        for group in groups:
            self._respond("prompt_button_group_start")
            for label, cmd, style in group:
                self._button("prompt_button", label, cmd, style)
            self._respond("prompt_button_group_end")
        for label, cmd, style in footer:
            self._button("prompt_footer_button", label, cmd, style)
        self._respond("prompt_show")

    def _prompt_end(self):
        self._respond("prompt_end")

    @staticmethod
    def _chunks(items, n):
        return [items[i:i + n] for i in range(0, len(items), n)]

    def _is_printing(self):
        if self.print_stats is None:
            return False
        state = self.print_stats.get_status(
            self.reactor.monotonic())["state"]
        return state == "printing"

    # ------------------------------------------------------------------
    # Pre-print check
    def resolve_gcode_path(self, filename):
        """Map a FILENAME to a path, but only for files virtual_sdcard itself
        would print (allow-list). Blocks "../" and absolute-path traversal.
        """
        if self.v_sd is None:
            return None
        if filename.startswith("/"):
            filename = filename[1:]   # same rule as virtual_sdcard
        try:
            files = [f for f, _ in self.v_sd.get_file_list(
                check_subdirs=True)]
        except Exception:
            logging.exception("spool_tracker: unable to list gcode files")
            return None
        if filename not in files:
            # virtual_sdcard matches file names case-insensitively
            by_lower = {f.lower(): f for f in files}
            filename = by_lower.get(filename.lower())
            if filename is None:
                return None
        return os.path.join(self.v_sd.sdcard_dirname, filename)

    def check_file(self, filename):
        """Return (enough, needed_mm). needed_mm is None if unknown."""
        path = self.resolve_gcode_path(filename)
        needed = read_filament_estimate(path) if path else None
        if needed is None or not self.state["loaded"]:
            return True, needed
        required = needed * (1. + self.safety_margin / 100.)
        return required <= self.state["remaining_mm"], needed

    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if (gcmd.get_int("SPOOL_SKIP_CHECK", 0)
                or not self.state["loaded"]
                or self.v_sd is None or self.v_sd.is_active()):
            self.pending_file = None
            return self.orig_print_file(gcmd)
        filename = gcmd.get("FILENAME")
        try:
            enough, needed = self.check_file(filename)
        except Exception:
            # Fail open: a bug in the advisory check must never stop the
            # user from printing. The real command still validates FILENAME.
            logging.exception("spool_tracker: filament check failed")
            enough, needed = True, None
        if enough:
            self.pending_file = None
            if needed is not None:
                gcmd.respond_info(
                    "Spool tracker: print needs %s, spool has %s (%.0f%%)"
                    % (self._fmt_amount(needed),
                       self._fmt_amount(self.state["remaining_mm"]),
                       self.percent_remaining()))
            else:
                gcmd.respond_info("Spool tracker: no filament estimate found"
                                  " in file, skipping check")
            return self.orig_print_file(gcmd)
        self.pending_file = filename
        self._show_not_enough(filename, needed)

    def _show_not_enough(self, filename, needed):
        short = os.path.basename(filename)
        self._prompt(
            "Not enough filament",
            "%s needs about %s. The loaded %s spool has about %s left "
            "(%.0f%%)." % (short, self._fmt_amount(needed),
                           self.describe_spool(),
                           self._fmt_amount(self.state["remaining_mm"]),
                           self.percent_remaining()),
            footer=[("Cancel", "SPOOL_PROMPT_CLOSE", "secondary"),
                    ("Switch spool", "SPOOL_SWITCH", "primary"),
                    ("Print anyway", "SPOOL_PRINT_ANYWAY", "warning")])

    def _start_print(self, filename):
        gcmd = self.gcode.create_gcode_command(
            "SDCARD_PRINT_FILE", "SDCARD_PRINT_FILE",
            {"FILENAME": filename})
        self.orig_print_file(gcmd)

    cmd_SPOOL_PRINT_ANYWAY_help = "Start the print that was held back by " \
                                  "the spool tracker filament check"
    def cmd_SPOOL_PRINT_ANYWAY(self, gcmd):
        self._prompt_end()
        if self.pending_file is None:
            raise gcmd.error("No print is waiting to start")
        if self.orig_print_file is None:
            raise gcmd.error("Pre-print check is not active")
        filename, self.pending_file = self.pending_file, None
        self._start_print(filename)

    cmd_SPOOL_PROMPT_CLOSE_help = "Close the spool tracker dialog"
    def cmd_SPOOL_PROMPT_CLOSE(self, gcmd):
        self.pending_file = None
        self._prompt_end()

    # ------------------------------------------------------------------
    # Spool switching / loading
    cmd_SPOOL_STATUS_help = "Report the filament left on the tracked spool"
    def cmd_SPOOL_STATUS(self, gcmd):
        st = self.state
        if not st["loaded"]:
            msg = "No spool loaded. Use SPOOL_SWITCH to load one."
        else:
            msg = ("Spool: %s\nRemaining: %.1f%% (%s)\n"
                   "Used since loaded: %.1f m"
                   % (self.describe_spool(), self.percent_remaining(),
                      self._fmt_amount(st["remaining_mm"]),
                      st["used_since_load_mm"] / 1000.))
        lp = st.get("last_print")
        if lp:
            msg += "\nLast print: %s used %.1f m" % (
                os.path.basename(lp.get("file") or "?"),
                lp.get("used_mm", 0.) / 1000.)
        prev = st.get("previous_spool")
        if prev:
            msg += "\nPrevious spool: %s %s, removed at %.0f%%" % (
                prev.get("material"), self._fmt_weight(prev.get(
                    "spool_weight", 0)), prev.get("percent", 0.))
        gcmd.respond_info(msg)

    cmd_SPOOL_SWITCH_help = "Show what is left on the current spool, then " \
                            "load a new one"
    def cmd_SPOOL_SWITCH(self, gcmd):
        if self._is_printing():
            self._prompt("Switch spool",
                         "A print is running. Pause the print before "
                         "switching spools.",
                         footer=[("OK", "SPOOL_PROMPT_CLOSE", "primary")])
            return
        if not self.state["loaded"]:
            self._prompt_percent()
            return
        pct = self.percent_remaining()
        label_pct = int(pct // 5 * 5)
        self._prompt(
            "Switch spool",
            "Current spool: %s with %.0f%% left (about %s). Write %d%% on "
            "the spool before you remove it, so you can enter it when you "
            "reuse it." % (self.describe_spool(), pct,
                           self._fmt_amount(self.state["remaining_mm"]),
                           label_pct),
            footer=[("Cancel", "SPOOL_PROMPT_CLOSE", "secondary"),
                    ("Load new spool", "SPOOL_LOAD", "primary")])

    def _prompt_percent(self):
        buttons = [("%d%%" % p, "SPOOL_LOAD PERCENT=%d" % p,
                    "primary" if p == 100 else "secondary")
                   for p in PERCENT_STEPS]
        self._prompt("Load new spool",
                     "How full is the new spool? (100% = brand new)",
                     groups=self._chunks(buttons, 4),
                     footer=[("Cancel", "SPOOL_PROMPT_CLOSE", "secondary")])

    cmd_SPOOL_LOAD_help = "Load a new spool. Without parameters asks on " \
        "screen. PERCENT=<0-100> MATERIAL=<PLA..> WEIGHT=<grams>"
    @staticmethod
    def _get_percent(gcmd, required):
        if required:
            percent = gcmd.get_float("PERCENT", minval=0., maxval=100.)
        else:
            percent = gcmd.get_float("PERCENT", None, minval=0., maxval=100.)
        # Klipper's range check lets "nan" through (NaN compares false)
        if percent is not None and not math.isfinite(percent):
            raise gcmd.error("PERCENT must be a number from 0 to 100")
        return percent

    def cmd_SPOOL_LOAD(self, gcmd):
        percent = self._get_percent(gcmd, required=False)
        material = gcmd.get("MATERIAL", None)
        weight = gcmd.get_int("WEIGHT", None, minval=1,
                              maxval=MAX_SPOOL_WEIGHT)
        step = gcmd.get("STEP", "").lower()
        if material is not None:
            material = material.upper()
            if material not in self.densities:   # allow-list
                raise gcmd.error("Unknown material '%s'. Known: %s" % (
                    clean_text(material, 16), ", ".join(self.densities)))
        if percent is None:
            self._prompt_percent()
            return
        last_mat = self.state.get("material") or self.default_material
        if last_mat not in self.densities:
            last_mat = self.default_material
        last_weight = self.state.get("spool_weight") or self.default_weight
        if material is None and weight is None and step != "material":
            self._prompt(
                "Load new spool",
                "%d%% full. Is it the same type as last time: %s %s?"
                % (percent, last_mat, self._fmt_weight(last_weight)),
                footer=[("Change type",
                         "SPOOL_LOAD PERCENT=%g STEP=material" % percent,
                         "secondary"),
                        ("Yes", "SPOOL_LOAD PERCENT=%g MATERIAL=%s WEIGHT=%d"
                         % (percent, last_mat, last_weight), "primary")])
            return
        if material is None:
            buttons = [(m, "SPOOL_LOAD PERCENT=%g MATERIAL=%s" % (percent, m),
                        "primary" if m == last_mat else "secondary")
                       for m, _ in self.materials]
            self._prompt("Load new spool", "Which material?",
                         groups=self._chunks(buttons, 4),
                         footer=[("Cancel", "SPOOL_PROMPT_CLOSE",
                                  "secondary")])
            return
        if weight is None:
            buttons = [(self._fmt_weight(w), "SPOOL_LOAD PERCENT=%g "
                        "MATERIAL=%s WEIGHT=%d" % (percent, material, w),
                        "primary" if w == last_weight else "secondary")
                       for w in self.weights]
            self._prompt("Load new spool",
                         "%s spool size (net filament weight when new)?"
                         % (material,),
                         groups=self._chunks(buttons, 3),
                         footer=[("Cancel", "SPOOL_PROMPT_CLOSE",
                                  "secondary")])
            return
        self.load_spool(percent, material, weight)
        self._prompt_end()
        gcmd.respond_info("Spool loaded: %s at %.0f%% (%s)" % (
            self.describe_spool(), percent,
            self._fmt_amount(self.state["remaining_mm"])))
        self._after_load()

    def load_spool(self, percent, material, weight):
        st = self.state
        if st["loaded"]:
            st["previous_spool"] = {
                "material": st["material"],
                "spool_weight": st["spool_weight"],
                "percent": round(self.percent_remaining(), 1),
                "remaining_mm": round(st["remaining_mm"], 1),
                "removed_at": time.time()}
        capacity = self.mm_for_weight(weight, material)
        st.update({"loaded": True, "material": material,
                   "spool_weight": weight, "capacity_mm": capacity,
                   "remaining_mm": capacity * percent / 100.,
                   "loaded_percent": percent, "loaded_at": time.time(),
                   "used_since_load_mm": 0.})
        self.low_warned = False
        self._save_state()

    def _after_load(self):
        # If a print was held back, offer to start it now
        if self.pending_file is None:
            return
        filename = self.pending_file
        enough, needed = self.check_file(filename)
        if not enough:
            self._show_not_enough(filename, needed)
            return
        self._prompt(
            "Spool loaded",
            "The new spool has enough filament for %s. Start the print now?"
            % (os.path.basename(filename),),
            footer=[("Not now", "SPOOL_PROMPT_CLOSE", "secondary"),
                    ("Start print", "SPOOL_PRINT_ANYWAY", "primary")])

    cmd_SPOOL_SET_help = "Correct the remaining amount on the current " \
                         "spool: PERCENT=<0-100>"
    def cmd_SPOOL_SET(self, gcmd):
        if not self.state["loaded"]:
            raise gcmd.error("No spool loaded. Use SPOOL_LOAD first.")
        percent = self._get_percent(gcmd, required=True)
        self.state["remaining_mm"] = self.state["capacity_mm"] * percent / 100.
        self.low_warned = False
        self._save_state()
        gcmd.respond_info("Spool set to %.0f%% (%s)" % (
            percent, self._fmt_amount(self.state["remaining_mm"])))


def load_config(config):
    return SpoolTracker(config)
