"""Off-printer tests for klipper/spool_tracker.py using fake Klipper objects.

Run:  python -m unittest discover -s tests -v
"""
import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "klipper"))
import spool_tracker as st  # noqa: E402


class CommandError(Exception):
    pass


class FakeGCodeCommand:
    error = CommandError

    def __init__(self, gcode, params):
        self.gcode = gcode
        self.params = {k.upper(): str(v) for k, v in params.items()}

    def _conv(self, name, default, conv, minval=None, maxval=None):
        if name not in self.params:
            return default
        v = conv(self.params[name])
        if minval is not None and v < minval:
            raise CommandError("below min")
        if maxval is not None and v > maxval:
            raise CommandError("above max")
        return v

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        return self._conv(name, default, int, minval, maxval)

    def get_float(self, name, default=None, minval=None, maxval=None):
        return self._conv(name, default, float, minval, maxval)

    def respond_info(self, msg):
        self.gcode.infos.append(msg)

    def respond_raw(self, msg):
        self.gcode.raw.append(msg)


class FakeGCode:
    error = CommandError

    def __init__(self):
        self.handlers = {}
        self.raw = []
        self.infos = []

    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        if func is None:
            return self.handlers.pop(cmd, None)
        assert cmd not in self.handlers, cmd
        self.handlers[cmd] = func

    def create_gcode_command(self, command, commandline, params):
        return FakeGCodeCommand(self, params)

    def respond_raw(self, msg):
        self.raw.append(msg)

    def respond_info(self, msg, log=True):
        self.infos.append(msg)

    def run(self, cmd, **params):
        return self.handlers[cmd](FakeGCodeCommand(self, params))

    def prompts(self):
        return [r for r in self.raw if r.startswith("// action:")]


class FakeReactor:
    NOW = 0.
    NEVER = 9e99

    def __init__(self):
        self.t = 100.
        self.timers = []

    def monotonic(self):
        return self.t

    def register_timer(self, cb, when):
        self.timers.append(cb)
        return cb


class FakePrintStats:
    def __init__(self):
        self.filament_used = 0.
        self.state = "standby"
        self.filename = ""

    def get_status(self, eventtime):
        return {"filament_used": self.filament_used, "state": self.state,
                "filename": self.filename}


class FakeVSD:
    def __init__(self, dirname, printer):
        self.sdcard_dirname = dirname
        self.active = False
        self.started = []
        self.printer = printer

    def is_active(self):
        return self.active

    def get_file_list(self, check_subdirs=False):
        out = []
        for root, _, files in os.walk(self.sdcard_dirname):
            for n in files:
                full = os.path.join(root, n)
                out.append((os.path.relpath(full, self.sdcard_dirname), 0))
        return out

    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if self.active:
            raise gcmd.error("SD busy")
        self.printer.send_event("virtual_sdcard:reset_file")
        self.started.append(gcmd.get("FILENAME"))
        self.active = True


class FakePrinter:
    def __init__(self, cfgdir, gcodedir):
        self.reactor = FakeReactor()
        self.gcode = FakeGCode()
        self.events = {}
        self.objects = {"gcode": self.gcode,
                        "print_stats": FakePrintStats()}
        self.cfgdir = cfgdir
        vsd = FakeVSD(gcodedir, self)
        self.objects["virtual_sdcard"] = vsd
        self.gcode.register_command("SDCARD_PRINT_FILE",
                                    vsd.cmd_SDCARD_PRINT_FILE)

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, ev, cb):
        self.events.setdefault(ev, []).append(cb)

    def send_event(self, ev, *args):
        for cb in self.events.get(ev, []):
            cb(*args)

    def get_start_args(self):
        return {"config_file": os.path.join(self.cfgdir, "printer.cfg")}


class FakeConfig:
    SENTINEL = object()

    class error(Exception):
        pass

    def __init__(self, printer, opts=None):
        self.printer = printer
        self.opts = opts or {}

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "spool_tracker"

    def get(self, name, default=SENTINEL):
        return self.opts.get(name, default)

    def getfloat(self, name, default=None, **kw):
        return float(self.opts.get(name, default))

    def getint(self, name, default=None, **kw):
        return int(self.opts.get(name, default))

    def getboolean(self, name, default=None):
        return bool(self.opts.get(name, default))


PRUSA_TAIL = """G1 X10 Y10 E1
; filament used [mm] = 2000.50
; filament used [cm3] = 4.81
; total filament used [g] = 5.97
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.gdir = os.path.join(self.tmp, "gcodes")
        os.makedirs(self.gdir)
        self.printer = FakePrinter(self.tmp, self.gdir)
        self.make_tracker()

    def make_tracker(self, opts=None):
        self.printer.gcode.handlers = {
            "SDCARD_PRINT_FILE":
                self.printer.objects["virtual_sdcard"].cmd_SDCARD_PRINT_FILE}
        self.printer.events = {}
        self.t = st.load_config(FakeConfig(self.printer, opts))
        self.printer.send_event("klippy:connect")
        self.printer.send_event("klippy:ready")
        self.g = self.printer.gcode
        self.ps = self.printer.objects["print_stats"]
        self.vsd = self.printer.objects["virtual_sdcard"]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_gcode(self, name, text):
        with open(os.path.join(self.gdir, name), "w") as f:
            f.write(text)

    def poll(self):
        self.printer.reactor.t += 2.
        self.t._poll(self.printer.reactor.t)


class TestParsing(unittest.TestCase):
    def test_prusa(self):
        self.assertAlmostEqual(st.parse_filament_estimate(PRUSA_TAIL), 2000.5)

    def test_prusa_multi_tool(self):
        self.assertAlmostEqual(st.parse_filament_estimate(
            "; filament used [mm] = 100.0, 250.5, 0.00\n"), 350.5)

    def test_orca_header(self):
        self.assertAlmostEqual(st.parse_filament_estimate(
            "; HEADER_BLOCK_START\n; total filament length [mm] : 1234.56\n"),
            1234.56)

    def test_cura(self):
        self.assertAlmostEqual(st.parse_filament_estimate(
            ";FLAVOR:Marlin\n;Filament used: 3.21579m\n"), 3215.79)

    def test_simplify3d(self):
        self.assertAlmostEqual(st.parse_filament_estimate(
            ";   Filament length: 5678.9 mm (5.68 m)\n"), 5678.9)

    def test_ideamaker(self):
        self.assertAlmostEqual(st.parse_filament_estimate(
            ";Material#1 Used: 4321.0\n"), 4321.0)

    def test_none(self):
        self.assertIsNone(st.parse_filament_estimate("G28\nG1 X1\n"))

    def test_large_file_tail(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "big.gcode")
            with open(p, "w") as f:
                f.write("G1 X1 Y1 E0.1\n" * 200000)  # ~2.8 MB
                f.write(PRUSA_TAIL)
                f.write("; config_line = 1\n" * 2000)
            self.assertAlmostEqual(st.read_filament_estimate(p), 2000.5)
        finally:
            shutil.rmtree(tmp)


class TestMath(Base):
    def test_1kg_pla_is_about_335m(self):
        mm = self.t.mm_for_weight(1000, "PLA")
        self.assertAlmostEqual(mm / 1000., 335.3, delta=0.5)
        self.assertAlmostEqual(self.t.grams_for_mm(mm, "PLA"), 1000., 3)


class TestLoadAndTrack(Base):
    def load(self, pct=100, mat="PLA", w=1000):
        self.g.run("SPOOL_LOAD", PERCENT=pct, MATERIAL=mat, WEIGHT=w)

    def test_interactive_flow(self):
        self.g.run("SPOOL_SWITCH")  # nothing loaded -> asks % directly
        p = self.g.prompts()
        self.assertIn("// action:prompt_begin Load new spool", p)
        self.assertIn("// action:prompt_button 75%|SPOOL_LOAD PERCENT=75|"
                      "secondary", p)
        self.g.raw.clear()
        self.g.run("SPOOL_LOAD", PERCENT=75)
        p = self.g.prompts()
        self.assertTrue(any("same type as last time: PLA 1 kg" in x
                            for x in p))
        self.assertIn("// action:prompt_footer_button Yes|SPOOL_LOAD "
                      "PERCENT=75 MATERIAL=PLA WEIGHT=1000|primary", p)
        self.g.run("SPOOL_LOAD", PERCENT=75, STEP="material")
        self.assertTrue(any("PETG|SPOOL_LOAD PERCENT=75 MATERIAL=PETG" in x
                            for x in self.g.prompts()))
        self.g.run("SPOOL_LOAD", PERCENT=75, MATERIAL="petg")
        self.assertTrue(any("2 kg|SPOOL_LOAD PERCENT=75 MATERIAL=PETG "
                            "WEIGHT=2000" in x for x in self.g.prompts()))
        self.g.run("SPOOL_LOAD", PERCENT=75, MATERIAL="PETG", WEIGHT=2000)
        s = self.t.get_status(0)
        self.assertTrue(s["loaded"])
        self.assertEqual(s["material"], "PETG")
        self.assertEqual(s["percent_remaining"], 75.0)
        self.assertEqual(self.g.prompts()[-1], "// action:prompt_end")

    def test_usage_tracking_and_persistence(self):
        self.load(100)
        cap = self.t.state["capacity_mm"]
        self.ps.state = "printing"
        self.ps.filename = "a.gcode"
        for used in (1000., 5000., 33530.):
            self.ps.filament_used = used
            self.poll()
        self.assertAlmostEqual(self.t.state["remaining_mm"], cap - 33530., 3)
        self.assertAlmostEqual(self.t.get_status(0)["percent_remaining"],
                               90.0, delta=0.1)
        # new print: counter resets
        self.printer.send_event("virtual_sdcard:reset_file")
        self.ps.filament_used = 1000.
        self.poll()
        self.assertAlmostEqual(self.t.state["remaining_mm"],
                               cap - 34530., 3)
        self.assertEqual(self.t.state["last_print"]["used_mm"], 1000.)
        # state survives a Klipper restart
        self.t._save_state()
        self.make_tracker()
        self.assertAlmostEqual(self.t.state["remaining_mm"],
                               cap - 34530., 3)

    def test_low_warning_once(self):
        self.load(12)
        self.ps.state = "printing"
        cap = self.t.state["capacity_mm"]
        self.ps.filament_used = cap * 0.03
        self.poll()
        self.ps.filament_used = cap * 0.04
        self.poll()
        lows = [i for i in self.g.infos if "filament low" in i]
        self.assertEqual(len(lows), 1)
        self.assertTrue(self.t.get_status(0)["low"])

    def test_switch_shows_remaining_and_records_previous(self):
        self.load(100)
        self.t.state["remaining_mm"] = self.t.state["capacity_mm"] * 0.37
        self.g.raw.clear()
        self.g.run("SPOOL_SWITCH")
        text = [x for x in self.g.prompts() if "prompt_text" in x][0]
        self.assertIn("37% left", text)
        self.assertIn("Write 35% on the spool", text)
        self.load(50, "PETG", 1000)
        prev = self.t.state["previous_spool"]
        self.assertEqual(prev["percent"], 37.0)
        self.assertEqual(prev["material"], "PLA")

    def test_switch_blocked_while_printing(self):
        self.load(100)
        self.ps.state = "printing"
        self.g.run("SPOOL_SWITCH")
        self.assertTrue(any("Pause the print" in x for x in self.g.prompts()))


class TestPrePrintCheck(Base):
    def setUp(self):
        super().setUp()
        self.g.run("SPOOL_LOAD", PERCENT=100, MATERIAL="PLA", WEIGHT=1000)
        self.g.raw.clear()

    def test_enough_starts_print(self):
        self.write_gcode("small.gcode", PRUSA_TAIL)
        self.g.run("SDCARD_PRINT_FILE", FILENAME="small.gcode")
        self.assertEqual(self.vsd.started, ["small.gcode"])
        self.assertEqual(self.g.prompts(), [])

    def test_unknown_estimate_starts_print(self):
        self.write_gcode("x.gcode", "G28\n")
        self.g.run("SDCARD_PRINT_FILE", FILENAME="/x.gcode")
        self.assertEqual(self.vsd.started, ["/x.gcode"])

    def test_not_enough_prompts_then_print_anyway(self):
        self.g.run("SPOOL_SET", PERCENT=1)  # ~3.35 m left
        self.write_gcode("Big Part.gcode",
                         "; filament used [mm] = 10000\n")
        self.g.run("SDCARD_PRINT_FILE", FILENAME="Big Part.gcode")
        self.assertEqual(self.vsd.started, [])
        p = self.g.prompts()
        self.assertIn("// action:prompt_begin Not enough filament", p)
        self.assertIn("// action:prompt_footer_button Switch spool|"
                      "SPOOL_SWITCH|primary", p)
        self.g.run("SPOOL_PRINT_ANYWAY")
        self.assertEqual(self.vsd.started, ["Big Part.gcode"])
        self.assertIsNone(self.t.pending_file)

    def test_safety_margin(self):
        cap = self.t.state["capacity_mm"]
        self.t.state["remaining_mm"] = 1020.
        self.write_gcode("m.gcode", "; filament used [mm] = 1000\n")
        self.g.run("SDCARD_PRINT_FILE", FILENAME="m.gcode")  # needs 1050
        self.assertEqual(self.vsd.started, [])

    def test_switch_spool_then_start(self):
        self.g.run("SPOOL_SET", PERCENT=1)
        os.makedirs(os.path.join(self.gdir, "sub"))
        self.write_gcode("sub/B.gcode", "; filament used [mm] = 10000\n")
        self.g.run("SDCARD_PRINT_FILE", FILENAME="sub/b.gcode")  # case
        self.assertEqual(self.vsd.started, [])
        self.g.run("SPOOL_SWITCH")
        self.g.run("SPOOL_LOAD", PERCENT=100, MATERIAL="PLA", WEIGHT=1000)
        p = self.g.prompts()
        self.assertIn("// action:prompt_begin Spool loaded", p)
        self.g.run("SPOOL_PRINT_ANYWAY")
        self.assertEqual(self.vsd.started, ["sub/b.gcode"])

    def test_cancel_clears_pending(self):
        self.g.run("SPOOL_SET", PERCENT=1)
        self.write_gcode("c.gcode", "; filament used [mm] = 10000\n")
        self.g.run("SDCARD_PRINT_FILE", FILENAME="c.gcode")
        self.g.run("SPOOL_PROMPT_CLOSE")
        with self.assertRaises(CommandError):
            self.g.run("SPOOL_PRINT_ANYWAY")
        self.assertEqual(self.vsd.started, [])

    def test_skip_check_param(self):
        self.g.run("SPOOL_SET", PERCENT=1)
        self.write_gcode("c.gcode", "; filament used [mm] = 10000\n")
        self.g.run("SDCARD_PRINT_FILE", FILENAME="c.gcode",
                   SPOOL_SKIP_CHECK=1)
        self.assertEqual(self.vsd.started, ["c.gcode"])


if __name__ == "__main__":
    unittest.main()
