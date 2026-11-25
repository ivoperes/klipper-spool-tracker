# Spool tracker indicator for the KlipperScreen title bar.
#
# Shows "<icon> 63%" for the spool tracked by Klipper's [spool_tracker]
# module on every screen. Tap it to switch spools.
#
# Installed into ~/KlipperScreen/ks_includes/ and hooked into
# panels/base_panel.py by install.sh.
#
# Copyright (C) 2026  ivoperes (https://github.com/ivoperes)
#
# This file is part of an add-on for KlipperScreen and, like KlipperScreen,
# may be distributed under the terms of the GNU AGPLv3 license.
# See klipperscreen/LICENSE.
import logging

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

POLL_SECONDS = 3


class SpoolIndicator:
    def __init__(self, base_panel):
        self.panel = base_panel
        self.screen = base_panel._screen
        gtk = base_panel._gtk
        size = gtk.img_scale * base_panel.bts
        self.label = Gtk.Label(label="--%")
        try:
            icon = gtk.Image("spool", size, size)
        except Exception:
            icon = gtk.Image("filament", size, size)
        box = Gtk.Box(spacing=3)
        box.pack_start(icon, False, False, 3)
        box.pack_start(self.label, False, False, 0)
        self.widget = Gtk.EventBox()
        self.widget.add(box)
        self.widget.connect("button-press-event", self.on_click)
        self.widget.set_no_show_all(True)
        box.show_all()
        titlebar = base_panel.titlebar
        titlebar.add(self.widget)
        # Place it just right of the title, before the clock
        titlebar.reorder_child(self.widget, 2)
        GLib.timeout_add_seconds(POLL_SECONDS, self.poll)

    def on_click(self, widget=None, event=None):
        self.screen._send_action(None, "printer.gcode.script",
                                 {"script": "SPOOL_SWITCH"})
        return True

    def poll(self):
        ws = getattr(self.screen, "_ws", None)
        if ws is not None and getattr(ws, "connected", False):
            ws.send_method("printer.objects.query",
                           {"objects": {"spool_tracker": None}},
                           self.on_result)
        return True

    def on_result(self, response, method, params, *args):
        try:
            status = response.get("result", {}).get("status", {}) \
                .get("spool_tracker")
            if not status:
                self.widget.hide()
                return False
            if status.get("loaded"):
                self.label.set_label("%.0f%%" % status["percent_remaining"])
            else:
                self.label.set_label("--%")
            ctx = self.label.get_style_context()
            if status.get("low"):
                ctx.add_class("spoolman_low")
            else:
                ctx.remove_class("spoolman_low")
            self.widget.show()
        except Exception as e:
            logging.debug(f"spool indicator update failed: {e}")
        # Called via GLib.idle_add: returning False stops it repeating
        return False
