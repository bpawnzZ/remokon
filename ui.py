#!/usr/bin/python3
"""Remokon — a Kodi remote for the desktop, driven from the keyboard.

One window, a sidebar of the installed video add-ons, and a poster grid for the
tree inside the selected add-on. Arrows move the selection spatially, Enter opens
or plays, Backspace goes up a level, Space toggles playback, `/` filters.

Run it with `desktop/run.sh`. GTK4 + libadwaita are already installed on Arch
(`gtk4`, `libadwaita`, `python-gobject`); nothing else is needed. The interpreter
matters: this is the SYSTEM python — a conda/miniforge `python3` has no `gi`.
"""
from __future__ import annotations

import sys
import os
import threading
import time
from collections import namedtuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")

from gi.repository import (Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene,  # noqa: E402
                           Gtk, Pango)

import kodi  # noqa: E402
from kodi import Addon, Entry, Host, KodiClient, KodiError  # noqa: E402

APP_ID = "io.github.bpawnzz.remokon"
POLL_MS = 1000
# Tile artwork is decoded and scaled to this before it reaches GTK, so a 512px
# add-on icon cannot size the grid (a Picture's natural size is its image's).
ART_SIZE = (128, 128)
# Named views, because "one grid" is not a choice. The number is height/width of
# the art box (1.5 = a 2:3 poster), the same way `dimensions` is written. FlowBox cannot size a child by its own
# height-for-width, so the box gets explicit metrics instead of an AspectFrame.
VIEWS = {"poster": (176, 1.5), "compact": (126, 1.5), "wide": (260, 9 / 16)}
VIEW_ORDER = ["poster", "compact", "wide", "rows"]
ZOOM_SCALE = {0: 0.85, 1: 1.0, 2: 1.3}
# The list has its own sizes, because "bigger items" has to mean something there
# too: art height drives the row height.
ROW_ZOOM = {0: (30, 42), 1: (40, 56), 2: (58, 82)}
NOW_ART_SIZE = (900, 560)
# `\` walks the three surfaces in this order and wraps: one key, three places to be.
# Pages absent from the map (settings, notice, loading) make the key inert there.
PAGE_CYCLE = {"grid": "remote", "remote": "now", "now": "grid"}
# A drag on the seek bar or the volume slider commits once it stops moving, so a
# drag is one JSON-RPC call instead of one per pixel.
DRAG_DEBOUNCE_MS = 300
SIDEBAR_WIDTH = 250
# The queue is the other side of the same window: same width as the add-on pane, so the
# two read as a pair rather than one being a stripe.
QUEUE_WIDTH = SIDEBAR_WIDTH

# One place for the two things GTK cannot infer: the focus ring (the app is
# keyboard first, so the selection must be impossible to lose) and the bar's
# separation from the content.
CSS = """
/* Cards, not bare icons: the grid is the app's face. */
.tile-card { padding: 10px; border-radius: 16px;
             background-color: alpha(currentColor, 0.045);
             box-shadow: 0 1px 2px alpha(black, 0.18); }
flowboxchild:hover .tile-card { background-color: alpha(@accent_bg_color, 0.14); }
.art-frame { border-radius: 12px;
              background-image: linear-gradient(180deg, alpha(@accent_bg_color, 0.13),
                                                alpha(currentColor, 0.03)); }
.placeholder-icon { opacity: 0.45; }
flowboxchild { padding: 6px; border-radius: 18px; }
/* Selection is an outline, not a fill: a filled tile hides the artwork it
   exists to show. */
flowboxchild:selected { background: none; }
flowboxchild:focus { outline: 3px solid @accent_bg_color; outline-offset: -2px; }
.tile-title { font-weight: 600; }
.dim-label { opacity: 0.65; }
.dpad { font-weight: 700; border-radius: 12px; }

/* Sidebar: a coloured initial instead of seven identical glyphs. */
.avatar { min-width: 26px; min-height: 26px; border-radius: 99px;
          background-color: alpha(@accent_bg_color, 0.25); font-weight: 800; }
.sidebar-name { font-weight: 600; }

/* Now playing: one stage that reads at a glance from across the room. */
.np-stage { border-radius: 22px; padding: 22px;
            background-image: linear-gradient(135deg, alpha(@accent_bg_color, 0.38),
                                              alpha(@accent_bg_color, 0.04)); }
.np-art { border-radius: 16px; background-color: alpha(currentColor, 0.06); }
.np-title { font-size: 22pt; font-weight: 800; }
.np-plot { font-size: 0.95em; opacity: 0.78; }
.np-chip { font-size: 0.85em; padding: 2px 12px; border-radius: 99px;
           background-color: alpha(currentColor, 0.14); }
.np-play { min-width: 58px; min-height: 58px; border-radius: 99px; }
/* One rhythm: every transport button the same width, so the play button is the row's
   centre and the icon-only stop is not a stub beside 69 px text buttons. */
.np-transport button { min-width: 58px; min-height: 44px; border-radius: 10px; }
/* A control, not a hairline: 4 px of trough read as a divider, and the page has none. */
.np-scale trough { min-height: 10px; border-radius: 99px;
                   background-color: alpha(currentColor, 0.18); }
.np-scale slider { min-width: 20px; min-height: 20px; }
.np-scale highlight { border-radius: 99px; }
.np-time { opacity: 0.8; font-feature-settings: "tnum"; }
.now-playing { border-top: 1px solid alpha(currentColor, 0.10); }

/* The queue: what comes next, docked on the right. */
.queue-head { font-weight: 700; }
.queue-row { border-radius: 10px; padding: 2px 2px; }
.queue-row.playing { background-color: alpha(@accent_bg_color, 0.20); }
.queue-row:focus { outline: 2px solid @accent_bg_color; outline-offset: -2px; }
/* 126 px of panel: four icon buttons at Adwaita's default padding need 144 px on
   their own, which sets the panel's minimum width and defeats Adw.Clamp. */
.queue-row button { min-width: 0; min-height: 0; padding: 1px 2px; }
.queue-num { opacity: 0.55; font-feature-settings: "tnum"; font-weight: 700; }
.queue-empty { opacity: 0.7; }
listbox.queue > row:selected { background: none; }

/* List view: one row per entry, so a 200-item plugin listing is readable and
   scrolls at a constant cost per row. */
.browse-row { padding: 4px 8px; border-radius: 10px; }
.browse-row:hover { background-color: alpha(@accent_bg_color, 0.12); }
/* The theme fills a hovered or selected row with the accent; a list of 200
   posters should not light up like a switch. We paint `.current` instead. */
listbox.browse > row:hover,
listbox.browse > row:selected { background: none; box-shadow: none; }
.browse-row.current { background-color: alpha(@accent_bg_color, 0.18);
                      outline: 3px solid @accent_bg_color; outline-offset: -1px; }
listbox.browse > row:selected .browse-row { background-color: alpha(@accent_bg_color, 0.22); }
listbox.browse > row:focus .browse-row { outline: 3px solid @accent_bg_color; outline-offset: -1px;
                                          border-radius: 12px; }
.row-title { font-weight: 600; }
.row-art { border-radius: 8px; background-color: alpha(currentColor, 0.06); }

/* Saved places: a mark on the card, and a row that is not an add-on. */
.fav-badge { font-size: 0.8em; min-width: 20px; min-height: 20px; border-radius: 99px;
             padding: 1px; color: white;
             background-color: alpha(@accent_bg_color, 0.92);
             box-shadow: 0 1px 3px alpha(black, 0.35); }
.fav-avatar { min-width: 26px; min-height: 26px; border-radius: 99px;
              background-color: alpha(@accent_bg_color, 0.30); }
"""

# Keys that mean the same thing everywhere: they drive the box, not this window.
# Measured against the live Input.Action enum — `home` is not in it, so Home is a
# different call (see KodiClient.home).
BOX_KEYS = {
    "BackSpace": "back",
    "o": "codecinfo",          # player process info: resolution/codecs/decoding
    "i": "info",
    "c": "contextmenu",
    "f": "fullscreen",
    "x": "stop",
    "m": "mute",
    "plus": "volumeup",
    # A bare `=` is `equal` in GDK; `+` needs Shift. Both raise the volume, so the key
    # under the fingers works without reaching for Shift. Ctrl+= is still zoom — that
    # branch runs before this lookup and is gated on CONTROL_MASK.
    "equal": "volumeup", "KP_Equal": "volumeup",
    "minus": "volumedown",
    "Page_Up": "bigstepback",
    "Page_Down": "bigstepforward",
    # No `h` here: it shadowed BOX_HOME_KEY below (the docs said "h home" while this
    # lookup sent `left` first). `j`/`k`/`l` and every arrow key still drive the box.
    "j": "down", "k": "up", "l": "right",
}

# Digits set the volume outright, 0 mutes — the same map the maemo remote uses.
DIRECTIONS = {"Up": Gtk.DirectionType.UP, "Down": Gtk.DirectionType.DOWN,
              "Left": Gtk.DirectionType.LEFT, "Right": Gtk.DirectionType.RIGHT}

ZOOM_KEYS = {"plus": 1, "KP_Add": 1, "equal": 1, "KP_Equal": 1,
             "minus": -1, "KP_Subtract": -1}

VOLUME_DIGITS = {"1": 10, "2": 20, "3": 30, "4": 40, "5": 50,
                 "6": 62, "7": 70, "8": 85, "9": 100}
BOX_HOME_KEY = "h"

# A replacement playback is stop → wait → open: a Player.Open issued while the
# previous film is still stopping fails intermittently on real hardware.
REPLACE_PAUSE = 1.5

# Local tile: what the grid shows, plus what activating it does.
Tile = namedtuple("Tile", "title detail art payload")
Location = namedtuple("Location", "title path")

Textures: dict[tuple[str, tuple[int, int]], Gdk.Texture] = {}
# The bytes already fetched from the host, so changing the view (which changes every
# tile's size) re-decodes instead of re-requesting. Bounded: a 200-item plugin folder
# would otherwise hold every poster at once.
ImageBytes: dict[str, bytes] = {}
MAX_IMAGE_BYTES = 256
SLOTS = threading.Semaphore(8)


def on_main(fn, *args):
    """Run `fn` on the GTK thread. GLib callbacks must return False to fire once."""
    GLib.idle_add(lambda: (fn(*args), False)[1])


def optional(fn, default=None):
    """A call the poll can live without: a refusal degrades that piece, not the tick."""
    try:
        return fn()
    except KodiError:
        return default


def run(fn, on_ok, on_error=None):
    """Call `fn()` on a worker thread and hand the result back on the GTK thread."""

    def work():
        try:
            result = fn()
        except Exception as error:  # noqa: BLE001 - every failure belongs to the UI
            on_main(on_error or (lambda e: print(f"error: {e}", file=sys.stderr)), error)
        else:
            on_main(on_ok, result)

    threading.Thread(target=work, daemon=True).start()


def load_texture(data: bytes, size: tuple[int, int] = ART_SIZE) -> GdkPixbuf.Pixbuf:
    """Decode artwork and shrink it to a tile before it reaches GTK.

    Runs on a worker thread: a GdkPixbuf is not tied to a display, so decoding
    there is safe; the texture is made from it on the GTK thread.
    """
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    pixbuf = loader.get_pixbuf()
    width, height = size
    scale = min(width / pixbuf.get_width(), height / pixbuf.get_height(), 1.0)
    if scale < 1.0:
        pixbuf = pixbuf.scale_simple(max(1, round(pixbuf.get_width() * scale)),
                                     max(1, round(pixbuf.get_height() * scale)),
                                     GdkPixbuf.InterpType.BILINEAR)
    return pixbuf


# -- what the app does, in one place ---------------------------------------
#
# The `?` button renders this. It is next to the bindings rather than in a doc file so
# a new key is one edit, and so the test that compares this table to `_on_key` has
# something to compare.
HELP = (
    ("Browsing", (
        ("\u2191 \u2193 \u2190 \u2192", "move the selection"),
        ("Enter", "open the folder, or play the item"),
        ("Backspace", "back — the level you came from, where you left it"),
        ("Shift+Backspace", "forward again"),
        ("/", "filter this list (Esc clears)"),
        ("Tab", "sidebar ↔ grid (also ← at the left edge, → back)"),
        ("s", "stop the playing video"),
        (",  .", "seek back / forward while a video plays"),
        ("v", "next view: posters, compact, wide, list"),
        ("Ctrl +  Ctrl -", "bigger / smaller tiles"),
        ("c", "menu for the selection: play, add to queue, \u2605 (also right-click)"),
        ("q", "queue panel \u2014 what the box will play next"),
        ("Ctrl+B", "hide or show the sidebar"),
        ("n", "now playing"),
    )),
    ("Remote", (
        ("\u2191 \u2193 \u2190 \u2192  jkl", "drive the box"),
        ("Enter", "select (OSD while a video is fullscreen)"),
        ("Escape", "back \u2014 sent to the box"),
        ("Backspace", "back"),
        ("Tab", "fullscreen"),
        ("h", "home on the box"),
        ("i", "info"),
        ("c", "context menu"),
        ("o", "player process info"),
        ("x   f", "stop \u00b7 fullscreen"),
        ("m   =  -", "mute \u00b7 volume up / down"),
        ("1\u20139   0", "set the volume \u00b7 mute"),
        ("PgUp  PgDn", "big step back / forward"),
        ("t", "type into the box's keyboard dialog"),
        ("r   \\", "back to browsing"),
    )),
    ("Now playing", (
        ("\u2190 \u2192", "seek 10 seconds back / forward"),
        ("Space", "play / pause"),
        ("Prev  Next", "the previous / next item in the queue"),
        ("drag the bar", "seek anywhere in the item"),
        ("\u221260 s  +60 s", "the long jumps on the page (also PgUp / PgDn)"),
        ("Enter", "select (OSD while a video is fullscreen)"),
        ("i   o", "info \u00b7 player process info"),
        ("c   f   m", "context menu \u00b7 fullscreen \u00b7 mute"),
        ("x   h", "stop \u00b7 home on the box"),
        ("1\u20139   0   =  -", "set the volume \u00b7 mute \u00b7 volume up / down"),
        ("t", "type into the box's keyboard dialog"),
        ("Escape  Backspace", "back to browsing"),
        ("\\", "on to the browse surface"),
    )),
    ("Everywhere", (
        ("Space", "play / pause, on any page"),
        ("x", "stop, on any page"),
        ("?  ", "this list"),
        ("\\", "browse \u2192 remote \u2192 now playing \u2192 browse"),
        ("gear (top right)", "settings: host, scheme, user, password"),
    )),
    ("Mouse", (
        ("click", "open a folder, or play an item"),
        ("right-click", "item menu: play, add to queue, \u2605 Favourites"),
        ("wheel", "scroll the grid, a list, or seek on the player's bar"),
    )),
)

class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Remokon")
        self.set_default_size(1100, 780)

        self.host: Host | None = None
        self.client: KodiClient | None = None
        self.addons: list[Addon] = []
        self.entries: list[Entry] = []
        self.locations: list[Location] = []
        self.player: dict | None = None
        # Saved places live beside the host in the config; `showing_favourites` is the
        # sidebar's Favourites destination rather than a folder in the add-on tree.
        self.favorites: list[dict] = kodi.load_favorites()
        if os.environ.get("REMOKON_DEBUG"):
            print(f"startup config={kodi.CONFIG_PATH} favourites={len(self.favorites)} "
                  f"{[f['title'] for f in self.favorites]}", flush=True)
        self.showing_favourites = False
        self.fav_row: Gtk.ListBoxRow | None = None
        self.fav_detail: Gtk.Label | None = None
        self._menu: Gtk.Popover | None = None
        # Two stacks, the browser's model: leaving a place pushes where it was onto
        # `back_stack`; going back pushes the place it left onto `forward_stack`. A new
        # move clears the forward half, so Forward never lands somewhere never visited.
        self.back_stack: list[dict] = []
        self.forward_stack: list[dict] = []
        self._pending_place: dict | None = None
        self._sidebar_rows: dict[str, Gtk.ListBoxRow] = {}
        # The text box that types into the box's own keyboard dialog, plus the window
        # id it was popped for: one popup per dialog, or cancelling it would raise a
        # new one every second the poll sees the same dialog.
        self._text_window: Gtk.Window | None = None
        self._text_popup_for: int | None = None
        self._text_entry: Gtk.Entry | None = None
        self._text_auto = False
        self.mode = kodi.view_mode()
        if self.mode == "cards":
            self.mode = "poster"
        self.zoom = kodi.view_zoom()

        self._gen = 0          # bumps on every render; stale image arrivals are dropped
        self._polling = False
        # A drag owns the seek handle until it commits: `_seek_target` is the pending
        # position and `_seek_timer` the one-shot that sends it. The volume slider
        # works the same way through `_volume_timer`.
        self._seek_target: int | None = None
        self._seek_timer: int | None = None
        self._volume_timer: int | None = None
        # Last queue the panel was built from, so a row's buttons act on the position
        # the box reported rather than one the app remembers.
        self._queue_items: list[Entry] = []

        self._build()
        self._update_nav_buttons()
        GLib.timeout_add(POLL_MS, self._tick)
        if os.environ.get("REMOKON_DEBUG"):
            GLib.timeout_add(3000, self._dump_geometry)
        self._start()

    # -- construction ------------------------------------------------------

    def _build(self):
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)

        self.toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic",
                                      tooltip_text="Back (Backspace)")
        self.back_button.connect("clicked", lambda *_: self._back())
        self.forward_button = Gtk.Button(icon_name="go-next-symbolic",
                                         tooltip_text="Forward (Shift+Backspace)")
        self.forward_button.connect("clicked", lambda *_: self._forward())

        # Collapsing the sidebar is a view control, so it sits first, with the history
        # pair beside it and before Now playing. `sidebar-show-symbolic` is the only
        # sidebar glyph Adwaita ships — there is no `sidebar-hide-symbolic`, and a
        # mistyped icon name draws nothing at all while staying clickable.
        self.sidebar_button = Gtk.Button(icon_name="sidebar-show-symbolic",
                                         tooltip_text="Add-on sidebar (Ctrl+B)")
        self.sidebar_button.connect("clicked", lambda *_: self._toggle_sidebar())
        header.pack_start(self.sidebar_button)
        # Back and Forward walk the browse history: always visible, insensitive when the
        # direction has nowhere to go, so the history's state reads without opening it.
        header.pack_start(self.back_button)
        header.pack_start(self.forward_button)

        self.title = Adw.WindowTitle(title="Remokon", subtitle="Not connected")
        header.set_title_widget(self.title)

        self.search = Gtk.SearchEntry(placeholder_text="Filter this list", width_request=240)
        self.search.connect("search-changed", lambda *_: self._render())
        self.search.connect("stop-search", lambda *_: self._hide_filter())
        self.search.set_visible(False)
        header.pack_end(self.search)
        self.now_button = Gtk.Button(icon_name="media-optical-symbolic",
                                     tooltip_text="Now playing")
        self.now_button.connect("clicked", lambda *_: self._show_now())
        header.pack_start(self.now_button)
        self.view_button = Gtk.MenuButton(icon_name="view-list-symbolic",
                                          tooltip_text="View: grid/list, size (v, Ctrl +/-)")
        popover = Gtk.Popover()
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        for label, handler in (("Posters", lambda: self._set_mode("poster")),
                               ("Compact", lambda: self._set_mode("compact")),
                               ("Wide thumbs", lambda: self._set_mode("wide")),
                               ("List", lambda: self._set_mode("rows")),
                               ("Smaller", lambda: self._set_zoom(self.zoom - 1)),
                               ("Larger", lambda: self._set_zoom(self.zoom + 1))):
            button = Gtk.Button(label=label, css_classes=["flat"])
            button.connect("clicked", lambda _b, h=handler: (h(), popover.popdown()))
            menu.append(button)
        popover.set_child(menu)
        self.view_button.set_popover(popover)
        header.pack_start(self.view_button)
        self.remote_button = Gtk.Button(icon_name="input-keyboard-symbolic",
                                        tooltip_text="Remote (r)")
        self.remote_button.connect("clicked", lambda *_: self._show_remote())
        header.pack_start(self.remote_button)
        # `media-playlist-consecutive-symbolic` is the playlist glyph Adwaita ships
        # (there is no plain `media-playlist`), and a mistyped icon name draws nothing
        # at all while staying clickable.
        self.queue_button = Gtk.Button(icon_name="media-playlist-consecutive-symbolic",
                                       tooltip_text="Queue: what the box plays next (q)")
        self.queue_button.connect("clicked", lambda *_: self._toggle_queue())

        # Settings is the host form (the first screen when there is no host) and help is
        # the only place the shortcut map is written down. Both sit at the end: neither
        # is a page, so both stay put on every page.
        self.settings_button = Gtk.Button(icon_name="emblem-system-symbolic",
                                          tooltip_text="Settings: Kodi host")
        self.settings_button.connect("clicked", lambda *_: self._toggle_settings())
        # The queue lives with the window chrome, not with the browsing controls.
        header.pack_end(self.queue_button)
        header.pack_end(self.settings_button)
        self.help_button = Gtk.Button(icon_name="help-about-symbolic",
                                      tooltip_text="Keyboard shortcuts (?)")
        self.help_button.connect("clicked", lambda *_: self._show_help())
        header.pack_end(self.help_button)

        self.toolbar.add_top_bar(header)
        # Level navigation: every level of the plugin tree is a button, so going back
        # three folders is one click instead of three.
        self.crumbs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                              margin_top=2, margin_bottom=6, margin_start=14, margin_end=14)
        self.toolbar.add_top_bar(self.crumbs)
        # Kept as a handle: the player page carries the same transport, so the bar hides
        # itself there rather than offering a second, differently-styled pause/stop.
        self.now_playing_bar = self._build_now_playing()
        self.toolbar.add_bottom_bar(self.now_playing_bar)

        sidebar = Gtk.ScrolledWindow(width_request=SIDEBAR_WIDTH,
                                     hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                   css_classes=["navigation-sidebar"])
        self.sidebar.connect("row-activated", self._on_sidebar_activated)
        sidebar.set_child(self.sidebar)

        self.stack = Gtk.Stack(hexpand=True, vexpand=True,
                               # The pages are alternatives, not siblings: a
                               # homogeneous stack sizes each page to the widest of
                               # them, so the list page could force the grid page
                               # wider than the pane.
                               hhomogeneous=False,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_settings_page(), "settings")
        self.stack.add_named(self._build_notice_page(), "notice")
        self.stack.add_named(self._build_grid_page(), "grid")
        self.stack.add_named(self._build_now_page(), "now")
        self.stack.add_named(self._build_remote_page(), "remote")
        self.stack.add_named(self._build_loading_page(), "loading")
        # One rule for every way a page changes (`\`, the gear, a back stack, a key):
        # the bar exists for the surfaces that have no player of their own.
        self.stack.connect(
            "notify::visible-child-name",
            lambda *_a: self.now_playing_bar.set_visible(
                self.stack.get_visible_child_name() != "now"))

        # The sidebar and its divider hide together: hiding only the scroller leaves a
        # 1 px separator against the window edge, and the freed width is the point.
        self.sidebar_pane = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.sidebar_pane.append(sidebar)
        self.sidebar_pane.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # The queue is the same idea on the other side: a pane, not a page, so the
        # browse surface keeps working while it is open. Plain visibility, like the
        # sidebar — a Gtk.Revealer here measured 108 px while shut (a transition that
        # never runs before the window is mapped), which silently stole the width from
        # the content pane on first layout.
        self.queue_pane = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.queue_pane.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        # An Adw.Clamp, because a Gtk.Widget has a MINIMUM width and no maximum: with a
        # bare width_request the body Box happily handed the pane its 460 px natural
        # width instead of the 126 it asked for. The clamp is the native cap.
        # `hexpand=False` is the point: an Adw.Clamp expands to fill the space it is
        # given and only caps its CHILD, so the pane was ~600 px of empty panel with the
        # 126 px content centred inside it.
        self.queue_pane.append(Adw.Clamp(maximum_size=QUEUE_WIDTH, tightening_threshold=0,
                                         hexpand=False))
        self.queue_pane.get_last_child().set_child(self._build_queue_panel())
        self.queue_pane.set_visible(False)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.append(self.sidebar_pane)
        body.append(self.stack)
        body.append(self.queue_pane)
        self.toolbar.set_content(body)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.toolbar)
        self.set_content(self.toasts)
        # Both panes exist now, so the player's breakpoint can account for them.
        self._size_now_page()

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", self._on_scroll)
        self.add_controller(wheel)

    def _build_settings_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Kodi host",
            description=f"The box this remote drives. Saved to {kodi.CONFIG_PATH}, "
                        f"readable only by you. Nothing here is built in: a fresh "
                        f"install starts on this page.")

        self.row_address = Adw.EntryRow(title="Address")
        self.row_port = Adw.EntryRow(title="Port")
        self.row_port.set_text(str(kodi.DEFAULT_PORT))
        self.row_scheme = Adw.ComboRow(title="Scheme",
                                       model=Gtk.StringList.new(["http", "https"]),
                                       subtitle="https if the box is behind a proxy")
        self.row_user = Adw.EntryRow(title="Username")
        self.row_pass = Adw.PasswordEntryRow(title="Password")
        for row in (self.row_address, self.row_port, self.row_scheme, self.row_user,
                    self.row_pass):
            group.add(row)

        action = Adw.ActionRow(title="Connect")
        self.connect_button = Gtk.Button(label="Save and connect",
                                         valign=Gtk.Align.CENTER,
                                         css_classes=["suggested-action"])
        self.connect_button.connect("clicked", lambda *_: self._apply_settings())
        action.add_suffix(self.connect_button)
        action.set_activatable_widget(self.connect_button)
        group.add(action)
        page.add(group)

        self.connect_status = Adw.ActionRow(title="", visible=False,
                                            css_classes=["dim-label"])
        status_group = Adw.PreferencesGroup()
        status_group.add(self.connect_status)
        page.add(status_group)

        for row in (self.row_address, self.row_port, self.row_user, self.row_pass):
            row.connect("apply", lambda *_: self._apply_settings())
        return page

    def _build_notice_page(self) -> Gtk.Widget:
        self.notice = Adw.StatusPage(icon_name="dialog-error-symbolic", title="",
                                     description="")
        return self.notice

    def _build_loading_page(self) -> Gtk.Widget:
        self.loading = Adw.StatusPage(icon_name="content-loading-symbolic",
                                      title="Connecting…")
        return self.loading

    def _on_scroll(self, _controller, _dx, dy) -> bool:
        """One wheel handler for the content pane.

        The scroller is a stock GtkScrolledWindow, but the wheel did not reach it
        from inside the FlowBox (measured: adjustment stayed at 0 with upper 1666
        while the wheel was turning), so the window drives its adjustment.
        """
        if self.stack.get_visible_child_name() != "grid":
            return False
        target = self.rows_scroll if self.mode == "rows" else self.cards_scroll
        adjustment = target.get_vadjustment()
        step = 90 if abs(dy) <= 1 else dy * 40
        adjustment.set_value(adjustment.get_value() + dy * step)
        return True

    def _build_grid_page(self) -> Gtk.Widget:
        # Two ways to read the same list. The grid is for posters (an add-on's
        # shelves); the list is for the 50-200 item plugin folders, where a poster
        # grid spends a screen on eight things and says nothing about the rest.
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                activate_on_single_click=True,
                                # Homogeneous keeps every card the same size and the
                                # gutters even. It also STRETCHES each card to fill
                                # its cell, which is why the cards are centred inside
                                # the cell (see _build_tile): the spare width becomes
                                # gutter instead of a bigger poster. Pinning the
                                # column count instead raises this widget's own
                                # minimum width (columns × card) and loops the layout.
                                homogeneous=True,
                                row_spacing=16, column_spacing=14,
                                valign=Gtk.Align.START,
                                margin_top=14, margin_bottom=28,
                                margin_start=14, margin_end=14)
        self.flow.connect("child-activated", self._on_activate)

        cards = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        cards.set_child(self.flow)
        self.cards_scroll = cards

        self.rows = Gtk.ListBox(css_classes=["browse"], selection_mode=Gtk.SelectionMode.NONE,
                                activate_on_single_click=True)
        self.rows.connect("row-activated", self._on_row_activated)
        rows = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        rows.set_child(self.rows)
        self.rows_scroll = rows

        self.view_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                    hhomogeneous=False, vhomogeneous=False)
        self.view_stack.add_named(cards, "cards")
        self.view_stack.add_named(rows, "rows")
        return self.view_stack

    def _build_now_page(self) -> Gtk.Widget:
        """The player: art beside the controls on a wide window, above them on a
        narrow one, filling whatever width the pane gives it.

        It reads the same 1 Hz snapshot as the bar, so the two can never disagree
        about what is playing.
        """

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                       css_classes=["np-stage"],
                       margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
                       hexpand=True, vexpand=True)

        self.now_stage = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22,
                                 hexpand=True, vexpand=True)

        # The art is the flexible half: an AspectFrame keeps it a 16:10 rectangle
        # whatever the window does, and the Gtk.Picture inside (CONTAIN) letterboxes
        # the poster in it. Fixed 240 x 160 before, which is what made the page a
        # 604 px poster that had to scroll.
        self.now_art_frame = Gtk.AspectFrame(ratio=16 / 10, obey_child=False,
                                             hexpand=True, vexpand=True)
        self.now_media = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 width_request=280, height_request=150,
                                 hexpand=True, vexpand=True,
                                 css_classes=["np-art"])
        self.now_media.append(Gtk.Image(icon_name="media-optical-symbolic", pixel_size=120,
                                        hexpand=True, vexpand=True,
                                        css_classes=["placeholder-icon"],
                                        halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER))

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                         hexpand=True, vexpand=True, valign=Gtk.Align.CENTER)
        self.now_title = Gtk.Label(label="Nothing playing", wrap=True, lines=2,
                                   xalign=0.0, justify=Gtk.Justification.LEFT,
                                   css_classes=["np-title"])
        self.now_subtitle = Gtk.Label(label="", css_classes=["np-chip"], wrap=True,
                                      halign=Gtk.Align.START)
        # Year and plot: a library item has both, a live stream neither, so the line
        # simply stays empty when the box reports nothing.
        self.now_plot = Gtk.Label(label="", wrap=True, lines=3, xalign=0.0,
                                  justify=Gtk.Justification.LEFT, css_classes=["np-plot"])

        self.now_elapsed = Gtk.Label(label="0:00", width_chars=7, xalign=1.0,
                                     css_classes=["np-time"])
        self.now_total = Gtk.Label(label="0:00", width_chars=7, xalign=0.0,
                                   css_classes=["np-time"])
        self.now_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.now_scale.set_hexpand(True)
        self.now_scale.add_css_class("np-scale")
        self.now_scale.set_draw_value(False)
        self.now_scale.set_sensitive(False)
        self.now_scale.connect("change-value", self._on_seek)
        seek_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, hexpand=True)
        seek_row.append(self.now_elapsed)
        seek_row.append(self.now_scale)
        seek_row.append(self.now_total)

        self.now_buttons: dict[str, Gtk.Button] = {}

        def button(box: Gtk.Box, name: str, tooltip: str, handler,
                   label: str | None = None, icon: str | None = None,
                   extra: tuple[str, ...] = ()) -> Gtk.Button:
            item = Gtk.Button(tooltip_text=tooltip, css_classes=list(extra))
            if label is not None:
                item.set_label(label)
            if icon is not None:
                item.set_icon_name(icon)
            item.connect("clicked", lambda *_a: handler())
            self.now_buttons[name] = item
            box.append(item)
            return item

        # `Player.GoTo` walks the box's own playlist: the queue panel and these two
        # buttons are the same list seen from two sides.
        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                            halign=Gtk.Align.CENTER, css_classes=["np-transport"])
        button(transport, "prev", "Previous item in the queue",
               lambda: self._player_goto("previous"), icon="media-skip-backward-symbolic")
        button(transport, "back60", "Back one minute (PgUp)",
               lambda: self._seek_by(-60), label="\u221260 s")
        button(transport, "back10", "Back 10 seconds (Left)",
               lambda: self._seek_by(-10), label="\u221210 s")
        button(transport, "play", "Play / pause (Space)", self._play_pause,
               icon="media-playback-start-symbolic",
               extra=("np-play", "suggested-action"))
        button(transport, "stop", "Stop", self._stop, icon="media-playback-stop-symbolic")
        button(transport, "fwd10", "Forward 10 seconds (Right)",
               lambda: self._seek_by(10), label="+10 s")
        button(transport, "fwd60", "Forward one minute (PgDn)",
               lambda: self._seek_by(60), label="+60 s")
        button(transport, "next", "Next item in the queue",
               lambda: self._player_goto("next"), icon="media-skip-forward-symbolic")

        # Volume and the box key that belongs beside it. `Application.GetProperties`
        # answers `volume`/`muted` only on this page, so the row costs nothing elsewhere.
        utilities = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                            halign=Gtk.Align.CENTER)
        button(utilities, "mute", "Mute (m)", lambda: self._send("mute"),
               icon="audio-volume-high-symbolic")
        self.now_volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.now_volume.set_size_request(160, -1)
        self.now_volume.set_draw_value(False)
        self.now_volume.add_css_class("np-scale")
        self.now_volume.set_sensitive(False)
        self.now_volume.connect("change-value", self._on_volume)
        utilities.append(self.now_volume)
        button(utilities, "fullscreen", "Fullscreen (f)", lambda: self._send("fullscreen"),
               icon="view-fullscreen-symbolic")
        button(utilities, "info", "Player process info (o)",
               lambda: self._send("codecinfo"), icon="dialog-information-symbolic")

        for widget in (self.now_title, self.now_subtitle, self.now_plot,
                       seek_row, transport, utilities):
            column.append(widget)
        column.append(Gtk.Label(
            label="\u2190 \u2192 seek 10 s  \u00b7  PgUp / PgDn 60 s  \u00b7  "
                  "Space play / pause  \u00b7  \\ cycles the pages",
            css_classes=["dim-label"], halign=Gtk.Align.CENTER, wrap=True))

        self.now_art_frame.set_child(self.now_media)
        self.now_stage.append(self.now_art_frame)
        self.now_stage.append(column)
        page.append(self.now_stage)

        self._now_art = ""
        # A breakpoint, not a width check in a signal handler: libadwaita flips the
        # stage when the window actually crosses 900 px, which is also what makes the
        # measured minimum width follow the narrow layout. (A `notify::default-width`
        # check did not fire on a programmatic resize, so the window stayed horizontal
        # at 780 px with the art crushed to 120.)
        self._now_breakpoint = Adw.Breakpoint(
            condition=Adw.BreakpointCondition.parse("max-width: 900px"))
        self._now_breakpoint.add_setter(self.now_stage, "orientation",
                                        GObject.Value(Gtk.Orientation, Gtk.Orientation.VERTICAL))
        self._now_breakpoint.add_setter(self.now_media, "height_request",
                                        GObject.Value(int, 200))
        self._now_breakpoint.add_setter(self.now_art_frame, "ratio",
                                        GObject.Value(float, 16 / 9))
        self.add_breakpoint(self._now_breakpoint)
        return page

    def _size_now_page(self):
        """Move the player's breakpoint with the panes, not just the window.

        A breakpoint can only see the window's width, but two open side panes take
        ~500 px the stage cannot use: at a 1100 px window with the sidebar and the queue
        both open the pane is 598 px, and a "wide" stage there is 800 px of layout in
        598 px of space.
        """
        sidebar = getattr(self, "sidebar_pane", None)
        queue = getattr(self, "queue_pane", None)
        # 810 px is what the side-by-side stage actually needs (280 art + the controls
        # column); measured, not guessed.
        inline = 810 + (SIDEBAR_WIDTH if sidebar is not None and sidebar.get_visible() else 0) \
            + ((QUEUE_WIDTH + 1) if queue is not None and queue.get_visible() else 0)
        self._now_breakpoint.set_condition(
            Adw.BreakpointCondition.parse(f"max-width: {inline}px"))


    def _build_queue_panel(self) -> Gtk.Widget:
        """The box's video playlist, docked on the right.

        Nothing is stored here: every row is what `Playlist.GetItems` said a moment
        ago, and every button is one `Playlist.*` call followed by a re-read, so the
        panel can never show an order the box does not have.
        """
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=QUEUE_WIDTH)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                         margin_top=8, margin_bottom=6, margin_start=8, margin_end=4)
        # Ellipsised, not natural-size: at 126 px the title is the part that gives way.
        self.queue_title = Gtk.Label(label="Queue", xalign=0.0, hexpand=True,
                                     ellipsize=Pango.EllipsizeMode.END,
                                     css_classes=["queue-head"])
        header.append(self.queue_title)
        # Only one button up here: at 126 px a title plus two icons leaves both icons a
        # 16 px target. Clear lives at the foot of the panel, where it has the width.
        # A width request, because a flat icon button's minimum is just the 16 px glyph
        # and the expanding title eats the rest; 24 keeps it a real target.
        self.queue_close_button = Gtk.Button(icon_name="window-close-symbolic",
                                             tooltip_text="Hide the queue (q)",
                                             css_classes=["flat"],
                                             width_request=24, height_request=24)
        self.queue_close_button.connect("clicked", lambda *_: self._toggle_queue(False))
        header.append(self.queue_close_button)
        panel.append(header)

        self.queue_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                      activate_on_single_click=False,
                                      css_classes=["queue"])
        self.queue_list.connect("row-activated", self._on_queue_activated)
        self.queue_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              vexpand=True)
        self.queue_scroll.set_child(self.queue_list)
        panel.append(self.queue_scroll)

        self.queue_empty = Gtk.Label(
            label="Nothing queued yet.\n\nPress c on an item \u2014 or right-click it "
                  "\u2014 and choose Add to queue.",
            wrap=True, justify=Gtk.Justification.CENTER, css_classes=["queue-empty"],
            max_width_chars=18, margin_top=24, margin_bottom=24,
            margin_start=10, margin_end=10)
        panel.append(self.queue_empty)
        self.queue_clear_button = Gtk.Button(label="Clear", css_classes=["flat"],
                                             margin_top=2, margin_bottom=8,
                                             margin_start=8, margin_end=8)
        self.queue_clear_button.connect("clicked", lambda *_: self._clear_queue())
        panel.append(self.queue_clear_button)
        self.queue_scroll.set_visible(False)
        self.queue_clear_button.set_visible(False)
        return panel

    def _build_remote_page(self) -> Gtk.Widget:
        """A keyboard-first remote: on this page the arrows go to the box, not to
        the grid, which is the whole point of having one window."""
        # A centred column, not a stretched one: the pad used to take whatever height
        # the window gave it (vexpand), which scattered the rows down the page and
        # left the cluster looking off-centre at any window that was not the size it
        # was built at. Everything here is its natural size and centred together.
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                        margin_top=20, margin_bottom=20, margin_start=16, margin_end=16,
                        halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)

        # The manual half of the text dialog: the automatic popup needs the box to
        # report its keyboard window, and a fallback has to exist for when it doesn't.
        # It rides the title row, not the utility row: a sixth 78 px button down there
        # made the cluster wider than a 700 px window's pane, so the page scrolled and
        # the whole pad went 46 px off-centre.
        self._remote_buttons = {}
        self._remote_buttons["text"] = Gtk.Button(
            label="Text", width_request=78, height_request=46, css_classes=["dpad"],
            tooltip_text="Type text into the box's dialog (t)")
        self._remote_buttons["text"].connect("clicked", lambda *_: self._open_text_dialog())
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        title_row.append(Gtk.Label(label="Remote", css_classes=["title-2"]))
        title_row.append(self._remote_buttons["text"])
        outer.append(title_row)
        outer.append(Gtk.Label(
            label="Arrows drive the box · Enter OSD/select · Backspace back · Tab fullscreen · "
                  "Esc is sent to the box too · h home · i info · c menu · o player info · "
                  "f fullscreen · x stop · m mute · = / - volume · 1–9 volume · PgUp/PgDn big steps · "
                  "jkl arrows · Ctrl+B hides the sidebar · left/right seek while a video plays",
            css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER,
            halign=Gtk.Align.CENTER, max_width_chars=64, xalign=0.5))

        self._remote_buttons["select"] = self._remote_button(
            "OK", "select", extra=["suggested-action"], width=112, height=84)

        # A d-pad keeps dashboard proportions: a fixed cluster, centred, and the page
        # gives it room. Expanding buttons looked wrong the moment the window got wide.
        pad = Gtk.Grid(row_spacing=10, column_spacing=10,
                       column_homogeneous=True, row_homogeneous=True,
                       halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        cells = [("up", "Up", "up", 1, 0), ("left", "Left", "left", 0, 1),
                 (self._remote_buttons["select"], 1, 1),
                 ("right", "Right", "right", 2, 1), ("down", "Down", "down", 1, 2)]
        for cell in cells:
            if isinstance(cell[0], Gtk.Widget):
                pad.attach(cell[0], cell[1], cell[2], 1, 1)
                continue
            name, label, action, column, row = cell
            button = self._remote_button(label, action, width=112, height=84)
            self._remote_buttons[name] = button
            pad.attach(button, column, row, 1, 1)

        self.remote_pad = pad
        outer.append(pad)

        utilities = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                            halign=Gtk.Align.CENTER)
        for label, action in (("Back", "back"), ("Home", "home"), ("Info", "info"),
                              ("Menu", "contextmenu"), ("Full", "fullscreen")):
            utilities.append(self._remote_button(label, action))
        outer.append(utilities)

        volume = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                         halign=Gtk.Align.CENTER)
        for label, action in (("Vol −", "volumedown"), ("Mute", "mute"), ("Vol +", "volumeup")):
            volume.append(self._remote_button(label, action))
        outer.append(volume)

        # Scrolled both ways: when the pane is smaller than the cluster the page
        # scrolls instead of putting controls outside it.
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(outer)
        return scroll

    def _remote_button(self, label: str, action: str, width: int = 78, height: int = 46,
                       extra: list[str] | None = None, expand: bool = False) -> Gtk.Button:
        button = Gtk.Button(label=label, width_request=width, height_request=height,
                            hexpand=expand, vexpand=expand,
                            css_classes=["dpad"] + (extra or []))
        button.connect("clicked", lambda *_: self._send(action))
        return button

    def _build_now_playing(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                      margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
                      css_classes=["now-playing"])
        self.np_title = Gtk.Label(label="Nothing playing", xalign=0.0, hexpand=True,
                                  ellipsize=3, css_classes=["tile-title"])
        self.np_progress = Gtk.ProgressBar(width_request=240, valign=Gtk.Align.CENTER)
        self.np_time = Gtk.Label(label="", width_chars=15, css_classes=["dim-label"])
        self.np_play = Gtk.Button(icon_name="media-playback-start-symbolic",
                                  tooltip_text="Play / pause (Space)")
        self.np_play.connect("clicked", lambda *_: self._play_pause())
        self.np_play.set_sensitive(False)
        self.np_stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop")
        self.np_stop.connect("clicked", lambda *_: self._stop())
        self.np_stop.set_sensitive(False)

        bar.append(self.np_title)
        bar.append(self.np_progress)
        bar.append(self.np_time)
        bar.append(self.np_play)
        bar.append(self.np_stop)
        return bar

    # -- connection --------------------------------------------------------

    def _start(self):
        data = kodi.load_config()
        if data:
            self._connect(kodi.host_from_config(data))
        else:
            # Nothing built in: a fresh clone lands here, with an empty form.
            self._show_settings()

    def _show_settings(self, message: str | None = None):
        page = self.stack.get_visible_child_name()
        if page not in ("settings", "loading"):
            self._page_before_settings = page
        self.stack.set_visible_child_name("settings")
        self.connect_status.set_visible(bool(message))
        self.connect_status.set_title(message or "")
        # A toast as well as the row: the row sits under the fields, which can be
        # below the fold on a short window, and a silent failure reads as a hung app.
        if message:
            self.toasts.add_toast(Adw.Toast(title=message, timeout=6))
        host = self.host
        if host is None:
            data = kodi.load_config()
            host = kodi.host_from_config(data) if data else None
        if host is not None:
            self.row_address.set_text(host.address)
            self.row_port.set_text(str(host.port))
            self.row_scheme.set_selected(1 if host.scheme == "https" else 0)
            self.row_user.set_text(host.username)
            self.row_pass.set_text(host.password)
        self.title.set_subtitle("Set your host to begin" if self.host is None
                                else f"Settings · {self.host.base}")
        self.row_address.grab_focus()

    def _toggle_settings(self):
        """The gear is a toggle: press it on the page and you go back to what you had."""
        if self.stack.get_visible_child_name() == "settings":
            self._leave_settings()
        else:
            self._show_settings()

    def _leave_settings(self):
        page = getattr(self, "_page_before_settings", None)
        if page in ("grid", "now", "remote"):
            self.stack.set_visible_child_name(page)
            self.title.set_subtitle(self.host.base if self.host else "Not connected")

    def _show_help(self):
        """One dialog, built the first time it is asked for: a help screen nobody
        opened costs nothing at startup."""
        dialog = getattr(self, "_help", None)
        if dialog is None:
            page = Adw.PreferencesPage()
            for title, rows in HELP:
                group = Adw.PreferencesGroup(title=title)
                for keys, what in rows:
                    row = Adw.ActionRow(title=what)
                    row.add_prefix(Gtk.Label(label=keys, css_classes=["monospace"],
                                             width_chars=16, xalign=0))
                    group.add(row)
                page.add(group)
            scroller = Gtk.ScrolledWindow(child=page, vexpand=True, hexpand=True,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER)
            dialog = Adw.Dialog(title="Keyboard shortcuts & usage",
                                content_width=640, content_height=720)
            dialog.set_child(scroller)
            self._help = dialog
        dialog.present(self)

    def _apply_settings(self):
        address = self.row_address.get_text().strip()
        if not address:
            self.connect_status.set_visible(True)
            self.connect_status.set_title("An address is required.")
            return
        try:
            port = int(self.row_port.get_text().strip() or kodi.DEFAULT_PORT)
        except ValueError:
            port = kodi.DEFAULT_PORT
        scheme = "https" if self.row_scheme.get_selected() == 1 else "http"
        self._connect(Host(address, port, self.row_user.get_text(),
                           self.row_pass.get_text(), scheme))

    def _connect(self, host: Host):
        self.host = host
        self.client = KodiClient(host)
        self.entries = []
        self.locations = []
        self.addons = []
        self.loading.set_title(f"Connecting to {host.base}…")
        self.stack.set_visible_child_name("loading")
        run(lambda: (self.client.ping(), self.client.video_addons()),
            self._connected, self._connect_failed)

    def _connected(self, result):
        _pong, addons = result
        kodi.save_config(self.host)
        self.addons = addons
        self.favorites = kodi.load_favorites()
        self.back_stack.clear()
        self.forward_stack.clear()
        self._fill_sidebar()
        self.showing_favourites = False
        self.entries = []
        self.locations = []
        self._render()
        if not addons:
            self._notice("No video add-ons", "This host has no add-ons whose id begins "
                                            "with plugin.video., so there is nothing to browse.")

    def _connect_failed(self, error):
        self._show_settings(str(error))

    # -- data --------------------------------------------------------------

    def _fill_sidebar(self):
        self._sidebar_rows.clear()
        self.fav_row = None
        child = self.sidebar.get_first_child()
        while child is not None:
            self.sidebar.remove(child)
            child = self.sidebar.get_first_child()

        # Favourites first: it is a destination, not an add-on, and it is where the
        # user returns to without walking a tree again.
        fav_row = Gtk.ListBoxRow()
        fav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                          margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        fav_box.append(Gtk.Label(label="★", css_classes=["fav-avatar"],
                                 valign=Gtk.Align.CENTER))
        fav_labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        fav_labels.append(Gtk.Label(label="Favourites", xalign=0.0,
                                    css_classes=["sidebar-name"]))
        self.fav_detail = Gtk.Label(label="", xalign=0.0, css_classes=["dim-label"])
        fav_labels.append(self.fav_detail)
        fav_box.append(fav_labels)
        fav_row.set_child(fav_box)
        fav_row.is_favourites = True
        self.fav_row = fav_row
        self.sidebar.append(fav_row)
        divider = Gtk.ListBoxRow(selectable=False, activatable=False)
        divider.set_child(Gtk.Separator(margin_top=4, margin_bottom=4))
        self.sidebar.append(divider)
        self._update_fav_row()

        for addon in self.addons:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                          margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
            avatar = Gtk.Label(label=(addon.display_name[:1] or "?").upper(),
                               css_classes=["avatar"], valign=Gtk.Align.CENTER)
            box.append(avatar)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            labels.append(Gtk.Label(label=addon.display_name, xalign=0.0, ellipsize=3,
                                    css_classes=["sidebar-name"]))
            if addon.detail():
                labels.append(Gtk.Label(label=addon.detail(), xalign=0.0, ellipsize=3,
                                        css_classes=["dim-label"]))
            box.append(labels)
            row.set_child(box)
            row.addon = addon
            self._sidebar_rows[addon.addonid] = row
            self.sidebar.append(row)

    def _update_fav_row(self):
        if self.fav_detail is not None:
            count = len(self.favorites)
            self.fav_detail.set_label("nothing saved" if not count
                                      else f"{count} saved")

    def _tiles(self) -> list[Tile]:
        if self.showing_favourites:
            tiles = [Tile(f["title"],
                          "folder" if f["kind"] == "directory" else "clip",
                          f["art"] or f["thumbnail"], f) for f in self.favorites]
        elif not self.locations:
            tiles = [Tile(a.display_name, "video add-on" if not a.detail() else a.detail(),
                          a.thumbnail, a) for a in self.addons]
        else:
            tiles = [Tile(e.heading, "folder" if e.is_folder else "",
                          e.art_url, e) for e in self.entries]
        query = self.search.get_text().strip().casefold()
        if query:
            tiles = [t for t in tiles if query in t.title.casefold()]
        return tiles

    def _render(self):
        self._gen += 1
        gen = self._gen
        tiles = self._tiles()
        self.visible_tiles = tiles

        self.flow.remove_all()
        row = self.rows.get_first_child()
        while row is not None:
            self.rows.remove(row)
            row = self.rows.get_first_child()

        # Only the view that will be seen is built. Building both meant two image
        # rounds for every render (a tile and a row per entry), which is most of why
        # artwork appeared to lag behind a view change.
        in_rows = self.mode == "rows"
        for tile in tiles:
            if in_rows:
                self.rows.append(self._build_row(tile, gen))
                continue
            child = Gtk.FlowBoxChild()
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["tile-card"])
            card.append(self._build_tile(tile, gen))
            child.set_child(card)
            self.flow.append(child)

        self.view_stack.set_visible_child_name("rows" if in_rows else "cards")
        self.view_button.set_icon_name("view-list-symbolic" if not in_rows
                                       else "view-grid-symbolic")
        self._update_header()
        if not tiles:
            self._notice("Nothing here",
                         "This folder is empty." if self.locations else
                         "No favourites yet — right-click an entry and choose "
                         "Favourites." if self.showing_favourites else
                         "No video add-ons matched." if self.search.get_text() else
                         "This host has no video add-ons.")
            return
        self.stack.set_visible_child_name("grid")
        place, self._pending_place = self._pending_place, None
        if place is not None:
            # Back or Forward landed here: its selection and scroll, not the first tile.
            self._select_index(place["index"] if place["index"] is not None else 0)
            GLib.idle_add(self._apply_scroll, place["scroll"], gen)
        else:
            GLib.idle_add(self._select_first, gen)

    def _select_first(self, gen):
        if gen != self._gen:
            return False
        # Never steal focus from the filter field: a render happens on every
        # keystroke there, and grabbing focus back would eat the next one.
        if self._in_text(self.get_focus()):
            return False
        self._select_index(0)
        return False

    def _dump_geometry(self) -> bool:
        """REMOKON_DEBUG=1 prints the grid's real state — a window cannot be seen here."""
        children = []
        child = self.flow.get_first_child()
        while child is not None:
            children.append((child.get_width(), child.get_height()))
            child = child.get_next_sibling()
        selected = self.flow.get_selected_children()
        index = selected[0].get_index() if selected else None
        focus = self.get_focus()
        focus_child = self.flow.get_focus_child()
        first = self.flow.get_first_child()
        metrics = ""
        if first is not None:
            child = first.get_child()
            minimum, natural, _, _ = child.measure(Gtk.Orientation.HORIZONTAL, -1)
            metrics = f" min={minimum} nat={natural}"
        print(f"window={self.get_width()}x{self.get_height()} flow={self.flow.get_width()}{metrics} "
              f"scroll={self.cards_scroll.get_vadjustment().get_value():.0f}"
              f"/{self.cards_scroll.get_vadjustment().get_upper():.0f} mode={self.mode} "
              f"pad={getattr(self, 'remote_pad', None) and self.remote_pad.get_width()}"
              f"/{self.stack.get_width()} "
              f"children={len(children)} sizes={children[:2]} selected={index} "
              f"focus={type(focus).__name__}({getattr(focus, 'get_label', lambda: '')() or getattr(focus, 'get_icon_name', lambda: '')()}) "
              f"focus_child={focus_child.get_index() if focus_child else None} "
              f"page={self.stack.get_visible_child_name()} "
              f"crumbs={self.title.get_subtitle()!r} "
              f"playing={bool(self.player)} speed={self.player['speed'] if self.player else None}",
              file=sys.stderr, flush=True)
        return True  # keep reporting; the first ticks happen before a client is attached

    def _update_header(self):
        if self.showing_favourites:
            levels = ["Favourites"]
        else:
            levels = ["Addons"] + [location.title for location in self.locations]
        subtitle = " › ".join(levels)
        if self.showing_favourites:
            subtitle = f"Favourites · {len(self.favorites)} saved"
        elif not self.locations and self.addons:
            subtitle = f"{subtitle} · {len(self.addons)} video add-ons"
        self.title.set_subtitle(subtitle)
        self._update_nav_buttons()
        self._rebuild_crumbs(levels)

    def _rebuild_crumbs(self, levels: list[str]):
        child = self.crumbs.get_first_child()
        while child is not None:
            self.crumbs.remove(child)
            child = self.crumbs.get_first_child()
        for depth, label in enumerate(levels):
            if depth:
                self.crumbs.append(Gtk.Label(label="›", css_classes=["dim-label"]))
            button = Gtk.Button(label=label, css_classes=["flat", "crumb"],
                                tooltip_text=f"Go to {label}")
            button.connect("clicked", lambda _b, d=depth: self._go_to_level(d))
            self.crumbs.append(button)

    def _go_to_level(self, depth: int):
        """depth 0 is the add-on list; deeper is a folder in the current tree."""
        if depth >= len(self.locations):
            return
        self._remember()
        self.locations = self.locations[:depth]
        if self.locations:
            self._load_directory()
        else:
            self.entries = []
            self._render()

    def _build_tile(self, tile: Tile, gen: int) -> Gtk.Widget:
        width, art_w, art_h = self._card_metrics()
        # An explicit art box: GtkFlowBox lays children out at their natural size and
        # cannot ask a child for its height at a given width, so an AspectFrame
        # collapsed to a thin strip and made every card look squashed.
        media = Gtk.Box(width_request=art_w, height_request=art_h,
                        overflow=Gtk.Overflow.HIDDEN, css_classes=["art-frame"])
        media.append(self._placeholder(tile))
        # The FlowBox's cell is wider than the card whenever the pane does not divide
        # evenly (homogeneous shares the line out). An Overlay keeps the requested
        # card size and carries the favourite mark on top of the artwork.
        frame = Gtk.Overlay(child=media, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        if self._is_saved(tile.payload):
            frame.add_overlay(Gtk.Label(label="★", css_classes=["fav-badge"],
                                        halign=Gtk.Align.END, valign=Gtk.Align.START,
                                        margin_top=6, margin_end=6, tooltip_text="Favourite"))

        # The card fills its (equal-width) cell; the ART box above is a fixed size, so
        # a wider cell widens the card's padding, never the poster. Bounding the card
        # itself was the wrong lever (a wrapped label sets a natural width that
        # `width_request` cannot cap) and it also raised the window's minimum width.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=width)
        box.append(frame)
        # Gtk.Inscription, not Gtk.Label: a Label always reports a natural width from
        # its text, and `width_request` is only a MINIMUM, so a long title widened a
        # 126 px card to 152 px and a 176 px poster to 185 px. Inscription's natural
        # width is its `nat_chars` (0 here), so the artwork's width decides the card
        # and the text wraps inside it.
        box.append(Gtk.Inscription(text=tile.title, nat_chars=0, min_chars=1,
                                   min_lines=2, nat_lines=2, xalign=0.5,
                                   wrap_mode=Pango.WrapMode.WORD_CHAR,
                                   text_overflow=Gtk.InscriptionOverflow.ELLIPSIZE_END,
                                   css_classes=["tile-title"]))
        if tile.detail:
            box.append(Gtk.Inscription(text=tile.detail, nat_chars=0, min_chars=1,
                                       xalign=0.5,
                                       text_overflow=Gtk.InscriptionOverflow.ELLIPSIZE_END,
                                       css_classes=["dim-label"]))

        url = kodi.artwork_url(tile.art, self.host)
        if url:
            self._apply_texture(media, url, gen, (art_w, art_h))
        self._add_context_gesture(box, tile.payload)
        return box

    def _add_context_gesture(self, widget: Gtk.Widget, payload):
        """Right-click on this widget opens the entry's menu.

        GTK4 has no context-menu signal on arbitrary widgets, so each card and row
        carries its own click controller for button 3.
        """
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_right_click, payload)
        widget.add_controller(click)

    def _build_row(self, tile: Tile, gen: int) -> Gtk.Widget:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                      css_classes=["browse-row"])
        art_w, art_h = ROW_ZOOM[self.zoom]
        art = Gtk.Box(width_request=art_w, height_request=art_h, valign=Gtk.Align.CENTER,
                      halign=Gtk.Align.CENTER, css_classes=["row-art"])
        art.append(self._placeholder(tile, size=max(18, art_w - 12), expand=False))
        box.append(art)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                         valign=Gtk.Align.CENTER)
        labels.append(Gtk.Label(label=tile.title, xalign=0.0, ellipsize=3,
                                css_classes=["row-title"]))
        if tile.detail:
            labels.append(Gtk.Label(label=tile.detail, xalign=0.0, ellipsize=3,
                                    css_classes=["dim-label"]))
        box.append(labels)
        row.set_child(box)
        row.inner = box

        url = kodi.artwork_url(tile.art, self.host)
        if url:
            self._apply_texture(art, url, gen, (art_w, art_h))
        self._add_context_gesture(box, tile.payload)
        return row

    def _on_row_activated(self, _listbox, row: Gtk.ListBoxRow):
        index = row.get_index()
        if index < len(self.visible_tiles):
            self._activate(self.visible_tiles[index].payload)

    def _placeholder(self, tile: Tile, size: int = 54, expand: bool = True) -> Gtk.Widget:
        """A tall empty frame of missing-image glyphs is what made the grid look
        broken; the shape of the thing (folder, add-on, playable) reads better."""
        payload = tile.payload
        if isinstance(payload, dict):  # a saved place
            icon = "folder-symbolic" if payload.get("kind") == "directory" \
                else "video-x-generic-symbolic"
        elif isinstance(payload, Addon):
            icon = "application-x-addon-symbolic"
        elif payload.is_folder:
            icon = "folder-symbolic"
        else:
            icon = "video-x-generic-symbolic"
        return Gtk.Image(icon_name=icon, pixel_size=size, hexpand=expand, vexpand=expand,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                         css_classes=["placeholder-icon"])

    def _apply_texture(self, container: Gtk.Widget, url: str, gen: int | None,
                       size: tuple[int, int] = ART_SIZE):
        key = (url, size)
        if (texture := Textures.get(key)) is not None:
            self._swap_picture(container, texture)
            return

        def paint(texture: Gdk.Texture):
            if gen is not None and gen != self._gen:
                return
            self._swap_picture(container, texture)

        def fetch():
            # Bounded so a 200-poster folder does not open 200 sockets at once.
            data = ImageBytes.get(url)
            if data is None:
                with SLOTS:
                    data = self.client.image(url)
                if len(ImageBytes) >= MAX_IMAGE_BYTES:
                    # ponytail: FIFO eviction, not LRU — the entries that get evicted
                    # are the ones scrolled off screen, so a refetch is cheap.
                    ImageBytes.pop(next(iter(ImageBytes)), None)
                ImageBytes[url] = data
            return load_texture(data, size)

        def cache(pixbuf: GdkPixbuf.Pixbuf):
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            Textures[key] = texture
            if os.environ.get("REMOKON_DEBUG"):
                print(f"art ok: {pixbuf.get_width()}x{pixbuf.get_height()} {url[:80]} "
                      f"gen={gen} current={self._gen}", file=sys.stderr, flush=True)
            paint(texture)

        def failed(error):
            # A missing tile art is not worth a toast, but it must be diagnosable.
            if os.environ.get("REMOKON_DEBUG"):
                print(f"art failed: {url} ({error})", file=sys.stderr, flush=True)

        run(fetch, cache, failed)

    @staticmethod
    def _swap_picture(container: Gtk.Widget, texture: Gdk.Texture):
        """Put the texture in place of the placeholder, whichever container it is."""
        picture = Gtk.Picture(paintable=texture, content_fit=Gtk.ContentFit.CONTAIN,
                              can_shrink=True, hexpand=True, vexpand=True)
        if isinstance(container, Gtk.AspectFrame):
            container.set_child(picture)
            return
        child = container.get_first_child()
        while child is not None:
            container.remove(child)
            child = container.get_first_child()
        container.append(picture)

    # -- navigation --------------------------------------------------------

    def _on_sidebar_activated(self, _listbox, row):
        if getattr(row, "is_favourites", False):
            self._show_favourites()
            return
        addon = getattr(row, "addon", None)
        if addon is not None:
            self._open_addon(addon)

    def _open_addon(self, addon: Addon):
        self._remember()
        self.showing_favourites = False
        self.locations = [Location(addon.display_name, addon.browse_path)]
        if (row := self._sidebar_rows.get(addon.addonid)) is not None:
            self.sidebar.select_row(row)
        self._load_directory()

    def _show_favourites(self):
        """The Favourites destination: saved places, not a folder in the tree."""
        if not self.showing_favourites:
            self._remember()
        self.showing_favourites = True
        self.locations = []
        self.entries = []
        # Disk is the source of truth: whatever was saved — by this window or a previous
        # run — is what this page shows.
        self.favorites = kodi.load_favorites()
        self._update_fav_row()
        if self.fav_row is not None:
            self.sidebar.select_row(self.fav_row)
        self._render()

    def _load_directory(self):
        location = self.locations[-1]
        self.loading.set_title(f"Loading {location.title}…")
        self.stack.set_visible_child_name("loading")
        self.entries = []
        self._update_header()
        path = location.path
        run(lambda: self.client.directory(path), self._directory_loaded, self._directory_failed)

    def _directory_loaded(self, entries: list[Entry]):
        self.entries = entries
        self._render()

    def _directory_failed(self, error):
        self._notice(f"Cannot open {self.locations[-1].title}", str(error))

    def _on_activate(self, _flow: Gtk.FlowBox, child: Gtk.FlowBoxChild):
        index = child.get_index()
        if index < len(self.visible_tiles):
            self._activate(self.visible_tiles[index].payload)

    def _activate(self, payload):
        if isinstance(payload, dict):  # a saved place
            if payload["kind"] == "media":
                self._play_file(payload["path"], payload["title"])
            else:
                self._remember()
                self.showing_favourites = False
                self.locations = [Location(payload["title"], payload["path"])]
                self._load_directory()
        elif isinstance(payload, Addon):
            self._open_addon(payload)
        elif payload.is_folder:
            self._remember()
            self.locations.append(Location(payload.heading, payload.file))
            self._load_directory()
        else:
            self._play(payload)

    # -- favourites --------------------------------------------------------

    @staticmethod
    def _place(payload) -> tuple[str, str]:
        """The (kind, path) a browse payload stands for — the identity of a saved place."""
        if isinstance(payload, dict):
            return payload["kind"], payload["path"]
        if isinstance(payload, Addon):
            return "directory", payload.browse_path
        return ("directory" if payload.is_folder else "media"), payload.file

    def _is_saved(self, payload) -> bool:
        kind, path = self._place(payload)
        return kodi.is_favorite(self.favorites, kind, path)

    def _favorite_for(self, payload) -> dict:
        if isinstance(payload, dict):
            return dict(payload)
        if isinstance(payload, Addon):
            return kodi.favorite_from_addon(payload)
        return kodi.favorite_from_entry(payload)

    def _toggle_favourite(self, payload):
        favorite = self._favorite_for(payload)
        kind, path = favorite["kind"], favorite["path"]
        if not str(path or "").strip():
            # A place with no path cannot be reopened, and load_favorites() drops it on
            # the next start (it validates), so saving it would look like a lost click.
            if os.environ.get("REMOKON_DEBUG"):
                print(f"favourite REFUSED, no path: {favorite}", flush=True)
            self.toasts.add_toast(Adw.Toast(title="Nothing to save: this entry has no path",
                                            timeout=4))
            return
        saved_before = kodi.is_favorite(self.favorites, kind, path)
        if saved_before:
            self.favorites = kodi.remove_favorite(self.favorites, kind, path)
            message = f"Removed {favorite['title']}"
        else:
            self.favorites = kodi.toggle_favorite(self.favorites, favorite)
            message = f"Saved {favorite['title']}"
        if os.environ.get("REMOKON_DEBUG"):
            print(f"favourite {'REMOVE' if saved_before else 'ADD'} kind={kind} "
                  f"path={path!r} title={favorite['title']!r} -> {len(self.favorites)} "
                  f"saved: {[f['title'] for f in self.favorites]}", flush=True)
        try:
            kodi.save_favorites(self.favorites)
        except OSError as error:
            if os.environ.get("REMOKON_DEBUG"):
                print(f"favourite SAVE FAILED: {error}", flush=True)
            self.toasts.add_toast(Adw.Toast(title=f"Cannot write the config: {error}",
                                            timeout=6))
            return
        self._update_fav_row()
        index = self._selected_index()
        self._render()
        GLib.idle_add(self._select_index_after_render, index)
        self.toasts.add_toast(Adw.Toast(title=message, timeout=3))

    def _entry_menu(self, payload, anchor: Gtk.Widget, x: float = 0, y: float = 0):
        """The item menu: Open/Play, then one Favourites item with a star.

        A plain popover of buttons — the same shape the header's view menu uses — so a
        click calls its handler directly. The Favourites row is ONE item, not a submenu:
        a submenu holding a single row is a second click for nothing, and (worse) the
        click had to resolve an action name through a nested section to do anything at
        all. The star is filled when the place is saved and the item toggles.

        One builder for the pointer and the keyboard, so `Shift+F10` offers exactly what
        a right-click offers.
        """
        kind, path = self._place(payload)
        saved = kodi.is_favorite(self.favorites, kind, path)
        if os.environ.get("REMOKON_DEBUG"):
            print(f"menu for kind={kind} path={path!r} saved={saved} "
                  f"(have {len(self.favorites)})", flush=True)

        self._close_menu()
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        popover.set_child(box)

        def item(icon: str, label: str, tooltip: str, handler) -> Gtk.Button:
            button = Gtk.Button(css_classes=["flat"])
            button.set_child(Adw.ButtonContent(icon_name=icon, label=label))
            button.set_tooltip_text(tooltip)
            button.connect("clicked", lambda *_a: (popover.popdown(), handler()))
            box.append(button)
            return button

        playable = kind == "media"
        self.menu_open = item(
            "media-playback-start-symbolic" if playable else "folder-open-symbolic",
            "Play" if playable else "Open",
            "Play this item" if playable else "Open this folder",
            lambda: self._activate(payload))
        self.menu_favourite = item(
            "starred-symbolic" if saved else "non-starred-symbolic", "Favourites",
            "Remove from Favourites" if saved else "Add to Favourites",
            lambda: self._toggle_favourite(payload))
        # Only something the box can actually play can be queued: a folder's plugin
        # path either queues a directory or is refused outright. Cleared first, so a
        # menu built for a folder cannot leave the previous item's button behind.
        self.menu_queue = None
        if playable:
            self.menu_queue = item("list-add-symbolic", "Add to queue",
                                   "Play this after whatever is playing now",
                                   lambda: self._add_to_queue(payload))

        # Parented to the WINDOW, not to the card: a FlowBoxChild is not a native, and
        # a popover parented to a non-native widget cannot create its surface.
        popover.set_parent(self)
        ok, point = anchor.compute_point(self, Graphene.Point().init(x, y))
        if not ok:
            point = Graphene.Point().init(0, 0)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(point.x), int(point.y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: p.unparent())
        self._menu = popover
        popover.popup()

    def _close_menu(self):
        if self._menu is not None:
            menu, self._menu = self._menu, None
            menu.popdown()
            if menu.get_parent() is not None:
                menu.unparent()

    def _on_right_click(self, gesture, _n_press, x, y, payload):
        widget = gesture.get_widget()
        if widget is None:
            return
        self._entry_menu(payload, widget, x, y)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _toggle_view(self):
        """Cycle the views in the order the menu lists them."""
        index = VIEW_ORDER.index(self.mode) if self.mode in VIEW_ORDER else 0
        self._set_mode(VIEW_ORDER[(index + 1) % len(VIEW_ORDER)])

    def _card_metrics(self) -> tuple[int, int, int]:
        base, ratio = VIEWS.get(self.mode, VIEWS["poster"])
        width = int(base * ZOOM_SCALE[self.zoom])
        return width, width, int(width * ratio)

    def _set_mode(self, mode: str):
        if mode == self.mode:
            return
        self.mode = mode
        kodi.save_view_mode(mode)
        index = self._selected_index()
        self._render()
        GLib.idle_add(self._select_index_after_render, index)

    def _set_zoom(self, zoom: int):
        zoom = max(0, min(2, zoom))
        if zoom == self.zoom:
            return
        self.zoom = zoom
        kodi.save_view_zoom(zoom)
        index = self._selected_index()
        self._render()
        GLib.idle_add(self._select_index_after_render, index)

    def _select_index_after_render(self, index):
        self._select_index(index if index is not None else 0)
        return False

    # -- place history -----------------------------------------------------
    #
    # A browser's model, in two stacks: leaving a place pushes where it was, going back
    # pushes the place it left the other way. A place is its folder trail (or the
    # Favourites destination) plus what was selected there and how far it was scrolled —
    # without those, "back" lands on the right folder at the top, which is not where the
    # user was.

    def _snapshot(self) -> dict:
        scroller = self.rows_scroll if self.mode == "rows" else self.cards_scroll
        return {"locations": list(self.locations),
                "favourites": self.showing_favourites,
                "index": self._selected_index(),
                "scroll": scroller.get_vadjustment().get_value()}

    def _remember(self):
        """Record the place on screen before moving away from it."""
        self.back_stack.append(self._snapshot())
        self.forward_stack.clear()
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        if getattr(self, "back_button", None) is None:
            return
        self.back_button.set_sensitive(bool(self.back_stack))
        self.forward_button.set_sensitive(bool(self.forward_stack))

    def _restore(self, place: dict):
        """Put a recorded place back on screen, selection and scroll included."""
        self.showing_favourites = place["favourites"]
        self.locations = list(place["locations"])
        self._pending_place = place
        if self.locations:
            self._load_directory()
        else:
            self.entries = []
            self._render()
        self._update_nav_buttons()

    def _apply_scroll(self, value: float, gen: int) -> bool:
        if gen != self._gen:
            return False
        scroller = self.rows_scroll if self.mode == "rows" else self.cards_scroll
        scroller.get_vadjustment().set_value(value)
        return False

    def _nav_back(self) -> bool:
        if not self.back_stack:
            return False
        place = self.back_stack.pop()
        self.forward_stack.append(self._snapshot())
        self._restore(place)
        return True

    def _forward(self) -> bool:
        if not self.forward_stack:
            return False
        place = self.forward_stack.pop()
        self.back_stack.append(self._snapshot())
        self._restore(place)
        return True

    def _back(self) -> bool:
        if os.environ.get("REMOKON_DEBUG"):
            print(f"back pressed: page={self.stack.get_visible_child_name()} "
                  f"locations={[l.title for l in self.locations]}", file=sys.stderr, flush=True)
        if self.stack.get_visible_child_name() == "remote":
            return self._leave_remote()
        if self.stack.get_visible_child_name() == "now":
            return self._leave_now()
        if self.back_stack:
            return self._nav_back()
        # Nothing recorded: the older ladder still covers a place reached without a move
        # (the Favourites destination opened before anything was browsed).
        if self.showing_favourites:
            self.showing_favourites = False
            self._render()
            return True
        if not self.locations:
            return False
        self.locations.pop()
        if self.locations:
            self._load_directory()
        else:
            self.entries = []
            self._render()
        return True

    # -- playback ----------------------------------------------------------

    def _play(self, entry: Entry):
        self._play_file(entry.file, entry.heading)

    def _play_file(self, file: str, title: str):
        def ok(_result):
            self.toasts.add_toast(Adw.Toast(title=f"Playing {title}", timeout=3))
            self._tick()

        # A Player.Open that lands while the previous film is still stopping fails
        # intermittently, so a replacement is stop → wait → open. The whole sequence
        # is on the worker: the pause costs the window nothing, and asking the box
        # beats trusting `self.player`, which can be a second stale either way.
        #
        # It plays THROUGH the box's video playlist rather than opening a bare file:
        # Kodi only advances to the next item when the current one came from the
        # playlist, so a direct `Player.Open {file}` left everything added to the queue
        # as dead weight — the classic "my queue never plays".
        def swap():
            players = self.client.active_players()
            if players:
                self.client.stop(players[0]["playerid"])
                time.sleep(REPLACE_PAUSE)
            self.client.playlist_clear()
            self.client.playlist_add(file)
            return self.client.playlist_open(0)

        run(swap, ok, lambda error: self.toasts.add_toast(Adw.Toast(title=str(error), timeout=5)))

    def _play_pause(self):
        if not self.player:
            return
        playerid = self.player["playerid"]
        run(lambda: self.client.play_pause(playerid), lambda _speed: self._tick(),
            lambda error: self.toasts.add_toast(Adw.Toast(title=str(error), timeout=5)))

    def _stop(self):
        if not self.player:
            return
        playerid = self.player["playerid"]
        run(lambda: self.client.stop(playerid), lambda _result: self._tick(),
            lambda error: self.toasts.add_toast(Adw.Toast(title=str(error), timeout=5)))

    def _tick(self) -> bool:
        if self.client is None or self._polling:
            return True
        # Keep polling while a folder loads: the bar used to freeze for as long as a
        # plugin took to answer, which reads as "the live player is broken".
        page = self.stack.get_visible_child_name()
        if page == "settings":
            return True
        self._polling = True
        client = self.client
        # A text dialog only means something on the page that drives the box, and it
        # keeps the idle cost at one call a second while browsing.
        watch_text = page in ("remote", "now")
        # The queue is re-read only while it is on screen: a closed panel asks the box
        # nothing at all.
        watch_queue = self.queue_pane.get_visible()
        # The volume row only exists on the player, so the extra call is only made there.
        watch_volume = page == "now"

        def poll():
            # Each of these is optional: one refusal used to raise, fail the whole tick
            # and freeze the window on its last snapshot (a finished video that never
            # cleared, a queue that stopped updating).
            state = kodi.playback_state(client)
            if state is not None and watch_volume:
                state.update(optional(lambda: kodi.volume_state(client), {}) or {})
            window = optional(lambda: kodi.current_window(client)) if watch_text else None
            queue = optional(lambda: client.playlist_items()) if watch_queue else None
            return state, window, queue

        def failed(_error):
            self._polling = False

        run(poll, lambda result: self._state(result[0], result[1], result[2]), failed)
        return True

    def _state(self, state: dict | None, window: dict | None = None,
               queue: list[Entry] | None = None):
        self._polling = False
        self.player = state
        self._watch_text_dialog(window)
        self._update_now_page(state)
        if queue is not None:
            self._fill_queue(queue, (state or {}).get("position"))
        if state:
            self.np_title.set_label(state["title"])
            self.np_progress.set_fraction(max(0.0, min(1.0, state["percentage"] / 100.0)))
            # Some plugin streams report a position past their own duration; saying
            # "0:29 / 0:15" looks broken, so show only what is true.
            usable = state["total_seconds"] > 0 and \
                state["elapsed_seconds"] <= state["total_seconds"]
            self.np_time.set_label(f'{state["time"]} / {state["total"]}' if usable
                                   else state["time"])
            self.np_play.set_icon_name("media-playback-pause-symbolic" if state["speed"]
                                       else "media-playback-start-symbolic")
            self.np_play.set_sensitive(True)
            self.np_stop.set_sensitive(True)
        else:
            self.np_title.set_label("Nothing playing")
            self.np_progress.set_fraction(0.0)
            self.np_time.set_label("")
            self.np_play.set_icon_name("media-playback-start-symbolic")
            self.np_play.set_sensitive(False)
            self.np_stop.set_sensitive(False)

    # -- the remote --------------------------------------------------------

    def _show_remote(self):
        page = self.stack.get_visible_child_name()
        if page in ("settings", "loading"):
            return
        if page == "remote":
            # Escape now belongs to the box, so the remote button and `r` toggle.
            self._leave_remote()
            return
        if page != "remote":
            self._page_before_remote = page
        self.stack.set_visible_child_name("remote")
        self._remote_buttons["select"].grab_focus()
        # Poll now rather than up to a second later: that is what pops the text box
        # when the box is already waiting for input.
        self._tick()

    def _leave_remote(self) -> bool:
        if self.stack.get_visible_child_name() != "remote":
            return False
        target = getattr(self, "_page_before_remote", "grid")
        self.stack.set_visible_child_name(target)
        if target == "grid":
            self._focus_content()
        return True

    def _set_volume(self, percent: int):
        if self.client is None:
            return
        run(lambda: self.client.set_volume(percent), lambda _result: None,
            lambda error: self.toasts.add_toast(Adw.Toast(title=str(error), timeout=4)))

    def _send(self, action: str):
        if self.client is None:
            return
        if os.environ.get("REMOKON_DEBUG"):
            print(f"action: {action} fullscreen={bool((self.player or {}).get('fullscreen_video'))}",
                  file=sys.stderr, flush=True)
        call = self.client.home if action == "home" else (lambda: self.client.action(action))
        run(call, lambda _result: None,
            lambda error: self.toasts.add_toast(Adw.Toast(title=str(error), timeout=4)))

    def _remote_key(self, name: str) -> bool:
        """One dispatcher for the keys that drive the box, shared by the Remote page
        and the player — "every Remote key works on the player" is only true if both
        pages run the same code.

        The letters and digits below this (BOX_KEYS, VOLUME_DIGITS) are page-agnostic
        and handled by the caller's shared branches, so this returns False for them.
        """
        if name == "Escape":
            # Escape is a key on this remote, not a way out of the page: it goes to
            # the box, which is what backs out of Kodi's own dialogs.
            self._send("back")
            return True
        directions = {"Up": "up", "Down": "down", "Left": "left", "Right": "right"}
        if name in directions:
            direction = directions[name]
            # In the fullscreen video window Kodi binds the arrow KEYS to
            # StepBack/StepForward; sending the navigation *actions* bypasses that
            # binding, so a remote that wants seeking has to say `stepback`.
            if direction in ("left", "right") and (self.player or {}).get("fullscreen_video"):
                self._send("stepback" if direction == "left" else "stepforward")
            else:
                self._send(direction)
            return True
        if name in ("Return", "KP_Enter"):
            # Keyboard-like Enter: in fullscreen video it opens/activates the OSD;
            # anywhere else it activates the focused control (measured on the maemo
            # reference remote).
            self._send("osd" if (self.player or {}).get("fullscreen_video") else "select")
            return True
        if name == "t":
            # The manual half of the text dialog: `t` is not on the box's map.
            self._open_text_dialog()
            return True
        return False

    # -- typing into the box's dialogs -------------------------------------

    def _open_text_dialog(self, automatic: bool = False) -> bool:
        """Pop the field that types into the box's keyboard dialog.

        Its own modal window on purpose: the app's key controller is attached to the
        main window, so a keystroke in here is a character and can never reach
        `_on_key` — `h`, `s`, `x`, digits and `\\` type like they should.
        """
        if self.client is None:
            return False
        if self._text_window is not None:
            self._text_window.present()
            return True
        window = Gtk.Window(transient_for=self, modal=True, title="Send text to Kodi",
                            default_width=460)
        entry = Gtk.Entry(placeholder_text="Text to send to the box…", hexpand=True)
        entry.connect("activate", lambda _entry: self._send_text())
        send = Gtk.Button(label="Send", css_classes=["suggested-action"])
        send.connect("clicked", lambda *_: self._send_text())
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._close_text_dialog())
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          halign=Gtk.Align.END)
        buttons.append(cancel)
        buttons.append(send)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=14, margin_bottom=14, margin_start=14, margin_end=14)
        box.append(entry)
        box.append(buttons)
        window.set_child(box)
        window.connect("close-request", lambda *_a: self._forget_text_dialog())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_text_key)
        window.add_controller(keys)
        self._text_window = window
        self._text_entry = entry
        self._text_auto = automatic
        window.present()
        entry.grab_focus()
        return True

    def _on_text_key(self, _controller, keyval, _keycode, _state) -> bool:
        if (Gdk.keyval_name(keyval) or "") == "Escape":
            self._close_text_dialog()
            return True
        return False

    def _forget_text_dialog(self) -> bool:
        """The window was closed by its own close button: drop the references only."""
        self._text_window = None
        self._text_entry = None
        self._text_auto = False
        return False

    def _close_text_dialog(self):
        window = self._text_window
        self._text_window = None
        self._text_entry = None
        self._text_auto = False
        if window is not None:
            window.destroy()

    def _send_text(self):
        entry = self._text_entry
        text = entry.get_text() if entry is not None else ""
        client = self.client
        self._close_text_dialog()
        if client is None or not text:
            return
        # `done: true` is the Enter key: the box's own dialog closes on it.
        run(lambda: client.send_text(text), lambda _result: None,
            lambda error: self.toasts.add_toast(Adw.Toast(title=str(error), timeout=4)))

    def _watch_text_dialog(self, window: dict | None):
        """One popup per dialog: the id stays marked until the box moves on."""
        if not kodi.is_text_input_window(window):
            if self._text_popup_for is not None:
                self._text_popup_for = None
                if self._text_auto:
                    # The box closed its dialog while ours was up; leaving a modal
                    # behind would keep the window unusable.
                    self._close_text_dialog()
            return
        if self._text_popup_for == (window or {}).get("id"):
            return
        self._text_popup_for = (window or {}).get("id")
        self._open_text_dialog(automatic=True)

    # -- the full player ---------------------------------------------------

    def _show_now(self):
        page = self.stack.get_visible_child_name()
        if page in ("settings", "loading"):
            return
        self._previous_page = page if page != "now" else getattr(self, "_previous_page", "grid")
        self.stack.set_visible_child_name("now")
        self._update_now_page(self.player)
        play = self.now_buttons["play"]
        if play.get_sensitive():
            play.grab_focus()
        else:
            self.now_button.grab_focus()
        self._tick()

    def _show_grid(self):
        """Back to the browse surface, with its folder, selection and scroll intact."""
        self.stack.set_visible_child_name("grid")
        self._focus_content()

    def _cycle_page(self) -> bool:
        """`\\` walks browse → Remote → player → browse. Pages that are not a surface
        (settings, notice, loading) are absent from PAGE_CYCLE, so it is inert there."""
        target = PAGE_CYCLE.get(self.stack.get_visible_child_name())
        if target is None:
            return False
        if target == "remote":
            self._show_remote()
        elif target == "now":
            self._show_now()
        else:
            self._show_grid()
        return True

    def _leave_now(self) -> bool:
        if self.stack.get_visible_child_name() != "now":
            return False
        self.stack.set_visible_child_name(getattr(self, "_previous_page", "grid"))
        if self.stack.get_visible_child_name() == "grid":
            self._focus_content()
        return True

    def _later(self, attribute: str, fn):
        """Restart a one-shot timer, so a drag commits once, when it stops moving."""
        old = getattr(self, attribute, None)
        if old:
            GLib.source_remove(old)

        def fire():
            setattr(self, attribute, None)
            fn()
            return False

        setattr(self, attribute, GLib.timeout_add(DRAG_DEBOUNCE_MS, fire))

    def _seek_failed(self, error):
        self.toasts.add_toast(Adw.Toast(title=str(error), timeout=5))

    def _seek_by(self, delta: int):
        if not self.player:
            return
        total = self.player["total_seconds"]
        if not total:
            # No duration — a live stream. Only Kodi's own step moves, and that is
            # what the Remote sends; an absolute seek has nowhere to land.
            self._send("stepback" if delta < 0 else "stepforward")
            return
        target = max(0, min(total, self.player["elapsed_seconds"] + delta))
        playerid = self.player["playerid"]
        run(lambda: self.client.seek(playerid, target), lambda _r: self._tick(),
            self._seek_failed)

    def _on_seek(self, _scale, _scroll, value):
        """A drag, a click on the trough, or a wheel: preview, then commit once.

        Sending a seek per `change-value` is a round trip per pixel, and the 1 Hz poll
        would then drag the handle back under the finger — so the target is held in
        `_seek_target` until the drag stops, and `_update_now_page` leaves the bar alone
        while it is set.
        """
        if not self.player or not self.player["total_seconds"]:
            return True
        total = self.player["total_seconds"]
        self._seek_target = max(0, min(total, int(value)))
        self.now_elapsed.set_label(kodi.clock(self._seek_target))
        self._later("_seek_timer", self._commit_seek)
        return True

    def _commit_seek(self):
        target, self._seek_target = self._seek_target, None
        if target is None or not self.player:
            return
        playerid = self.player["playerid"]
        run(lambda: self.client.seek(playerid, target), lambda _r: self._tick(),
            self._seek_failed)

    def _on_volume(self, _scale, _scroll, value):
        """The volume slider, same shape as the seek bar: one call when it settles."""
        self._later("_volume_timer", lambda: self._set_volume(int(value)))
        return True

    def _player_goto(self, to: str):
        """Previous / next inside the box's own playlist."""
        if not self.player:
            return
        playerid = self.player["playerid"]
        run(lambda: self.client.player_goto(playerid, to), lambda _r: self._tick(),
            self._seek_failed)

    def _update_now_page(self, state: dict | None):
        for button in self.now_buttons.values():
            button.set_sensitive(state is not None)
        self.now_volume.set_sensitive(bool(state) and state.get("volume") is not None)

        if not state:
            self.now_title.set_label("Nothing playing")
            self.now_subtitle.set_label("")
            self.now_plot.set_label("")
            self.now_plot.set_visible(False)
            self.now_elapsed.set_label("0:00")
            self.now_total.set_label("0:00")
            self.now_scale.set_range(0, 1)
            self.now_scale.set_value(0)
            self.now_scale.set_sensitive(False)
            self.now_buttons["play"].set_icon_name("media-playback-start-symbolic")
            if self._now_art:
                self._now_art = ""
                self._clear_media(self.now_media)
            return

        self.now_title.set_label(state["title"])
        chip = [part for part in (state.get("year"),
                                  "playing" if state["speed"] else "paused",
                                  f'{state["percentage"]:.0f}%') if part]
        self.now_subtitle.set_label(" \u00b7 ".join(chip))
        plot = state.get("plot") or ""
        self.now_plot.set_label(plot)
        self.now_plot.set_visible(bool(plot))
        self.now_buttons["play"].set_icon_name("media-playback-pause-symbolic" if state["speed"]
                                               else "media-playback-start-symbolic")

        # A pending drag owns the handle and the elapsed label until it commits.
        total = state["total_seconds"]
        if self._seek_target is None:
            if total > 0 and state["elapsed_seconds"] <= total:
                self.now_scale.set_range(0, total)
                self.now_scale.set_value(max(0, min(state["elapsed_seconds"], total)))
                self.now_elapsed.set_label(state["time"])
                self.now_total.set_label(state["total"])
                self.now_scale.set_sensitive(True)
            else:
                # Some plugin streams report a position with no duration at all.
                self.now_scale.set_range(0, 1)
                self.now_scale.set_value(state["percentage"] / 100.0)
                self.now_elapsed.set_label(state["time"])
                self.now_total.set_label("")
                self.now_scale.set_sensitive(False)

        if state.get("volume") is not None and self._volume_timer is None:
            self.now_volume.set_value(float(state["volume"]))
            self.now_buttons["mute"].set_icon_name(
                "audio-volume-muted-symbolic" if state.get("muted")
                else "audio-volume-high-symbolic")

        art = kodi.artwork_url(state.get("art") or "", self.host) or ""
        if art != self._now_art:
            self._now_art = art
            self._clear_media(self.now_media)
            if art:
                self._apply_texture(self.now_media, art, None, NOW_ART_SIZE)

    @staticmethod
    def _clear_media(container: Gtk.Widget):
        child = container.get_first_child()
        while child is not None:
            container.remove(child)
            child = container.get_first_child()
        container.append(Gtk.Image(icon_name="media-optical-symbolic", pixel_size=120,
                                   hexpand=True, vexpand=True, css_classes=["placeholder-icon"],
                                   halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER))

    # -- the queue (the box's own video playlist) --------------------------

    def _toggle_queue(self, show: bool | None = None) -> bool:
        """Slide the queue in or out. Shown, it re-reads the box's playlist."""
        want = (not self.queue_pane.get_visible()) if show is None else show
        self.queue_pane.set_visible(want)
        self._size_now_page()
        self.queue_button.set_tooltip_text(
            ("Hide" if want else "Show") + " the queue: what the box plays next (q)")
        if want:
            self._refresh_queue()
        elif self._in_queue():
            # The caret was in the pane that just slid away.
            self._focus_page_content()
        return want

    def _refresh_queue(self):
        if self.client is None:
            return
        client = self.client
        run(client.playlist_items,
            lambda items: self._fill_queue(items, (self.player or {}).get("position")),
            lambda error: self.toasts.add_toast(Adw.Toast(title=str(error), timeout=4)))

    def _fill_queue(self, items: list[Entry], position: int | None = None):
        """Rebuild every row — the box is the only order there is, so nothing is
        patched in place after a move.

        The 1 Hz tick calls this too, so it does nothing at all when neither the
        items nor the playing position changed: rebuilding once a second would fight
        the caret and the scroll position.
        """
        signature = (tuple(item.file for item in items), position)
        if signature == getattr(self, "_queue_sig", None):
            return
        self._queue_sig = signature

        # Where the caret is now, before the rows are destroyed: an action's own
        # target if it set one, else wherever the user was standing.
        focus, self._queue_focus = getattr(self, "_queue_focus", None), None
        if focus is None and self._in_queue():
            focus = getattr(self.queue_list.get_selected_row(), "position", None)

        self._queue_items = list(items)
        child = self.queue_list.get_first_child()
        while child is not None:
            self.queue_list.remove(child)
            child = self.queue_list.get_first_child()
        self.queue_scroll.set_visible(bool(items))
        self.queue_empty.set_visible(not items)
        self.queue_title.set_label("Queue" if not items else f"Queue \u00b7 {len(items)}")
        self.queue_clear_button.set_visible(bool(items))
        for index, item in enumerate(items):
            self.queue_list.append(self._queue_row(index, item, position == index))

        if items and focus is not None:
            row = self.queue_list.get_row_at_index(max(0, min(len(items) - 1, focus)))
            if row is not None:
                self.queue_list.select_row(row)
                GLib.idle_add(lambda: (row.grab_focus(), False)[1])

    def _queue_row(self, index: int, item: Entry, playing: bool) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(css_classes=["queue-row"] + (["playing"] if playing else []))
        # The row's own buttons act on the position the BOX reported, not on an index
        # the app is holding: the playlist can move under it.
        row.position = index
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3,
                      margin_top=2, margin_bottom=2, margin_start=8, margin_end=4)
        box.append(Gtk.Label(label=f"{index + 1}", width_chars=2, xalign=1.0,
                             css_classes=["queue-num"]))
        if playing:
            box.append(Gtk.Image(icon_name="media-playback-start-symbolic", pixel_size=12))
        box.append(Gtk.Label(label=item.heading, xalign=0.0, hexpand=True,
                             ellipsize=Pango.EllipsizeMode.END, max_width_chars=22,
                             tooltip_text=item.heading))
        for icon, tooltip, action in (("go-up-symbolic", "Move up", "up"),
                                      ("go-down-symbolic", "Move down", "down"),
                                      ("media-playback-start-symbolic", "Play now", "play"),
                                      ("list-remove-symbolic", "Remove", "remove")):
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip,
                                css_classes=["flat"], width_request=24, height_request=24)
            button.connect("clicked",
                           lambda _b, a=action, p=index: self._queue_action(a, p))
            box.append(button)
        row.set_child(box)
        return row

    def _queue_action(self, action: str, position: int):
        """One `Playlist.*` call, then a re-read, so the panel never guesses."""
        if self.client is None:
            return
        client = self.client
        count = len(self._queue_items)
        if action == "up":
            if position <= 0:
                return
            call = lambda: client.playlist_swap(position, position - 1)
        elif action == "down":
            if position >= count - 1:
                return
            call = lambda: client.playlist_swap(position, position + 1)
        elif action == "remove":
            call = lambda: client.playlist_remove(position)
        elif action == "play":
            call = lambda: client.playlist_open(position)
        else:
            return
        # Where the caret should land once the list is re-read: a moved item follows
        # its neighbour, a removed one leaves the caret where it was.
        self._queue_focus = {"up": position - 1, "down": position + 1}.get(action, position)

        def done(_result):
            if action == "play":
                self._tick()
            self._refresh_queue()

        run(call, done, self._seek_failed)

    def _clear_queue(self):
        if self.client is None:
            return
        run(self.client.playlist_clear, lambda _r: self._refresh_queue(), self._seek_failed)

    def _on_queue_activated(self, _listbox, row):
        self._queue_action("play", getattr(row, "position", 0))

    def _add_to_queue(self, payload):
        """Append to the box's video playlist — the next thing it plays."""
        if isinstance(payload, dict):
            if payload.get("kind") != "media":
                return
            path, title = payload.get("path"), payload.get("title")
        elif isinstance(payload, Addon) or payload.is_folder:
            return
        else:
            path, title = payload.file, payload.heading
        if not path:
            return

        def add():
            """Append, and start the queue if the box is idle — otherwise the item
            joins a list nothing will ever play."""
            self.client.playlist_add(path)
            if self.client.active_players():
                return False
            self.client.playlist_open(0)
            return True

        def done(started):
            self.toasts.add_toast(Adw.Toast(
                title=f"Playing {title}" if started else f"Queued {title}", timeout=3))
            if self.queue_pane.get_visible():
                self._refresh_queue()
            self._tick()

        run(add, done, self._seek_failed)

    def _in_queue(self) -> bool:
        widget = self.get_focus()
        while widget is not None:
            if widget is self.queue_list:
                return True
            widget = widget.get_parent()
        return False

    def _queue_key(self, name: str, state) -> bool:
        """The keys that belong to the queue panel while the caret is in it."""
        if name == "Escape":
            self._focus_page_content()
            return True
        row = self.queue_list.get_selected_row()
        position = getattr(row, "position", None)
        if position is None:
            return False
        if name in ("Return", "KP_Enter"):
            self._queue_action("play", position)
            return True
        if name == "Delete":
            self._queue_action("remove", position)
            return True
        if state & Gdk.ModifierType.SHIFT_MASK and name in ("Up", "Down"):
            self._queue_action("up" if name == "Up" else "down", position)
            return True
        # Up and Down belong to the ListBox itself.
        return False

    # -- notices and filtering ---------------------------------------------

    def _notice(self, title: str, description: str):
        self.notice.set_title(title)
        self.notice.set_description(description)
        self.stack.set_visible_child_name("notice")

    def _hide_filter(self):
        self.search.set_text("")
        self.search.set_visible(False)
        if self.stack.get_visible_child_name() == "grid":
            self._focus_content()
        else:
            self.notice.grab_focus()

    # -- keyboard ----------------------------------------------------------

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        name = Gdk.keyval_name(keyval) or ""
        focus = self.get_focus()
        in_text = self._in_text(focus)

        # The settings page is a FORM. Every binding below sends to the box or moves a
        # page, and a letter typed into an EntryRow must do neither — `in_text` is not
        # enough, because the gear can be pressed while the focus sits in the grid.
        # Escape is the one exception: leaving is what it does everywhere else.
        if self.stack.get_visible_child_name() == "settings":
            if name == "Escape":
                self._leave_settings()
                return True
            return False

        # Space is the transport everywhere, whatever holds the focus: the Remote
        # focuses its d-pad button on entry and Now playing focuses its play button,
        # and returning False for a focused Gtk.Button let the button swallow the key.
        # With no player there is nothing to toggle, so the key is left alone.
        if name == "space" and not in_text and self.player:
            self._play_pause()
            return True

        # `,`/`.` seek the playing video from the browse surface, so the window does
        # not have to go to the Remote to nudge the film. They send `stepback` /
        # `stepforward` (not `left`/`right`) for the reason the Remote's arrows do:
        # Kodi binds the arrow KEYS to step inside the fullscreen window, while the
        # navigation ACTIONS bypass that. No ladder here — repeated steps escalate
        # 10 s → 30 s → 1 min → 3 min → 5 min inside Kodi.
        page = self.stack.get_visible_child_name()
        if not in_text and name in ("comma", "period") and self.player and page != "remote":
            self._send("stepback" if name == "comma" else "stepforward")
            return True

        # `\` walks the three surfaces — browse → Remote → player → browse — in the same
        # window and at the same place in it: the stack keeps the browse page alive, so
        # its folder trail, selection and scroll are exactly where they were.
        if name == "backslash" and not in_text and self._cycle_page():
            return True

        # The queue panel: `q` anywhere it can be seen, and the panel's own keys while
        # the caret is in it, before any browse binding can read them.
        if name == "q" and not in_text and page in ("grid", "now"):
            self._toggle_queue()
            return True
        if not in_text and self._in_queue():
            if self._queue_key(name, state):
                return True
            if name in ("Up", "Down"):
                return False        # the ListBox moves the caret

        if name == "question" and not in_text:
            self._show_help()
            return True

        if self.stack.get_visible_child_name() == "remote":
            if self._remote_key(name):
                return True
        if self.stack.get_visible_child_name() == "now":
            # Leaving is Escape/Backspace everywhere in this app; the arrows are the
            # player's own — absolute ±10 s, so the bar, the label and the box agree.
            if name in ("Escape", "BackSpace"):
                return self._leave_now()
            if name in ("Left", "Right"):
                self._seek_by(-10 if name == "Left" else 10)
                return True
            if self._remote_key(name):
                return True
        # Backspace climbs the add-on tree on every browse page,
        # including the "loading" one: a folder that is still coming back must not
        # turn the key into Kodi's `back`. With nowhere to climb it stays `back`.
        # Only the browse pages: on the Remote page and Now playing, Backspace keeps
        # the meaning those pages give it.
        #
        # Shift+Backspace is the other way. It is consumed on the browse pages even when
        # there is nothing ahead: letting it fall through would reach `BOX_KEYS`
        # (`BackSpace` → `back`) and send the box a `back` the user never asked for.
        if name == "BackSpace" and not in_text and state & Gdk.ModifierType.SHIFT_MASK \
                and self.stack.get_visible_child_name() in ("grid", "loading", "notice"):
            self._forward()
            return True
        if name == "BackSpace" and not in_text and not state & Gdk.ModifierType.SHIFT_MASK \
                and self.stack.get_visible_child_name() \
                in ("grid", "loading", "notice") and (self.locations or self.showing_favourites) \
                and self._back():
            return True
        if self.stack.get_visible_child_name() == "grid" and not in_text:
            # A focused sidebar row is not the grid's selection: Return belongs to the
            # ListBox (its `row-activated` opens the add-on or Favourites), and the app's
            # item menu has no meaning for a sidebar row — but `c`/Menu must still be
            # consumed, or `BOX_KEYS` sends the box a context menu instead.
            if self._in_sidebar() and name in ("Return", "KP_Enter"):
                return False
            if self._in_sidebar() and name in ("Menu", "c"):
                return True
            # Stop from the browse surface. `_stop()` is the Now-playing button's own
            # call (Player.Stop, which closes the video) and is gated on a live player,
            # so there is nothing to check here. The Remote page keeps `x` for stop —
            # this branch is inside the grid page and can never fire there.
            if name in ("s", "S"):
                self._stop()
                return True
            # The menu key (and Shift+F10, which is what keyboards actually have) opens
            # the same menu as a right-click, on the selected entry. `c` is here too
            # because the help overlay has always promised it — and this branch runs
            # before `BOX_KEYS`, where `c` would send the box its own `contextmenu`.
            if name in ("Menu", "c") or (name == "F10" and state & Gdk.ModifierType.SHIFT_MASK):
                self._menu_for_selection()
                return True
            # Enter opens the folder or plays the item under the selection. Handled
            # here rather than left to FlowBox/ListBox's own `activate` binding, which
            # does nothing when the focus is on the container instead of a child.
            if name in ("Return", "KP_Enter"):
                index = self._selected_index()
                if index is not None and index < len(self.visible_tiles):
                    self._activate(self.visible_tiles[index].payload)
                return True
        if not in_text and self.stack.get_visible_child_name() == "grid" \
                and isinstance(focus, (Gtk.FlowBoxChild, Gtk.FlowBox, Gtk.ListBoxRow, Gtk.ListBox)) \
                and name in ("Up", "Down", "Left", "Right", "Home", "End"):
            # A sidebar row is a ListBoxRow too, so this branch used to catch the sidebar
            # as well and pull the focus back into the grid on the first arrow press —
            # a sidebar you could Tab into and never steer. Inside the sidebar the arrows
            # belong to the ListBox: hand Up/Down back to GTK, and make Right the way out.
            if self._in_sidebar():
                if name == "Right":
                    self._focus_content()
                    return True
                if name in ("Up", "Down"):
                    return False
                return True
            if name in ("Home", "End"):
                return self._select_index(0 if name == "Home" else -1)
            # The CONTAINER moves the focus: GtkFlowBox and GtkListBox know their own
            # geometry, including the short last row. Calling child_focus() on the
            # focused child only looks *inside* that child, so nothing ever moved.
            direction = DIRECTIONS[name]
            before = self._selected_index()
            if self.mode == "rows":
                if self.rows.child_focus(direction):
                    current = self.rows.get_focus_child()
                    if current is not None:
                        self._mark_row(current.get_index())
                else:
                    self._hand_off_left(name)
                return True
            if self.flow.child_focus(direction):
                child = self.flow.get_focus_child()
                if isinstance(child, Gtk.FlowBoxChild):
                    self.flow.select_child(child)
                    child.grab_focus()
            if name == "Left" and self._selected_index() == before:
                # The left edge of the line: the sidebar is the next thing that way.
                self._hand_off_left(name)
            return True
        # Ctrl+= is how people actually type "zoom in": GDK reports the key as
        # `equal` unless Shift is down.
        if state & Gdk.ModifierType.CONTROL_MASK and name in ZOOM_KEYS:
            self._set_zoom(self.zoom + ZOOM_KEYS[name])
            return True
        if state & Gdk.ModifierType.CONTROL_MASK and name == "b" and not in_text:
            return self._toggle_sidebar()
        if not in_text and name in VOLUME_DIGITS:
            self._set_volume(VOLUME_DIGITS[name])
            return True
        if name == "0" and not in_text:
            self._send("mute")
            return True
        if not in_text and name in BOX_KEYS:
            # Yatse's map: Backspace is the box's back, not this app's. Going up a
            # level in the browser is Escape (or the header button).
            self._send(BOX_KEYS[name])
            return True
        if name in ("Tab", "ISO_Left_Tab") and not in_text:
            if self.stack.get_visible_child_name() == "remote":
                self._send("fullscreen")
                return True
            return self._cycle_pane()
        if name == "Escape":
            if in_text:
                return False  # a text field clears itself
            if self.search.get_visible():
                self._hide_filter()
                return True
            return bool(self._back())
        # `h` is home on the REMOTE only. It used to fire from every page, so a stray
        # `h` while browsing threw the box onto its home screen mid-film; the browse
        # surface has Enter/Backspace for its own tree and no business touching the
        # box's home screen. (`i`/`c`/`o`/`f`/`m`/`x` are still page-agnostic — see the
        # openspec change if that ever bites.)
        if name == BOX_HOME_KEY and not in_text and page in ("remote", "now"):
            self._send("home")
            return True
        if name == "v" and not in_text and self.stack.get_visible_child_name() == "grid":
            self._toggle_view()
            return True
        if name == "n" and not in_text and \
                self.stack.get_visible_child_name() in ("grid", "remote"):
            self._show_now()
            return True
        if name == "r" and not in_text and \
                self.stack.get_visible_child_name() in ("grid", "now", "remote"):
            self._show_remote()
            return True
        if name == "slash" and not in_text:
            self.search.set_visible(True)
            self.search.grab_focus()
            return True
        return False

    # -- the sidebar --------------------------------------------------------

    def _toggle_sidebar(self) -> bool:
        """Fold the add-on sidebar away and give the width to the content pane."""
        show = not self.sidebar_pane.get_visible()
        self.sidebar_pane.set_visible(show)
        self._size_now_page()
        self.sidebar_button.set_tooltip_text(
            ("Hide" if show else "Show") + " the add-on sidebar (Ctrl+B)")
        if not show and self._in_sidebar():
            # The caret was in the list that just disappeared.
            self._focus_content()
        return True

    def _in_sidebar(self) -> bool:
        widget = self.get_focus()
        while widget is not None:
            if widget is self.sidebar:
                return True
            widget = widget.get_parent()
        return False

    def _focus_sidebar(self) -> bool:
        """Keyboard focus into the sidebar: the current row, else the first (Favourites)."""
        row = self.sidebar.get_selected_row() or self.sidebar.get_row_at_index(0)
        if row is None:
            return False
        row.grab_focus()
        return True

    def _hand_off_left(self, name: str) -> bool:
        """`Left` that had nowhere to go in the grid lands on the sidebar."""
        if name != "Left" or not self.sidebar_pane.get_visible():
            return False
        return self._focus_sidebar()

    def _cycle_pane(self) -> bool:
        """Tab moves between the sidebar and the grid, not through seven tiles."""
        if not self.sidebar_pane.get_visible():
            # Grabbing focus on a hidden row silently does nothing, and a Tab that
            # appears dead is worse than letting GTK's own focus chain run.
            return False
        if self._in_sidebar():
            self._focus_content()
            return True
        return self._focus_sidebar()

    def _mark_row(self, index: int):
        """One visible 'current' row, painted by us — the ListBox keeps no selection."""
        self._row_index = index
        row = self.rows.get_first_child()
        while row is not None:
            if hasattr(row, "inner"):
                if row.get_index() == index:
                    row.inner.add_css_class("current")
                else:
                    row.inner.remove_css_class("current")
            row = row.get_next_sibling()

    def _focus_content(self):
        self._select_index(self._selected_index() or 0)

    def _focus_page_content(self):
        """Put the caret back where the visible page keeps it."""
        if self.stack.get_visible_child_name() == "grid":
            self._focus_content()
        else:
            self.now_buttons["play"].grab_focus()

    def _menu_for_selection(self):
        """Open the context menu for the selected entry, anchored to its widget."""
        index = self._selected_index()
        if index is None or not self.visible_tiles:
            return
        if self.mode == "rows":
            row = self.rows.get_row_at_index(index)
            anchor = row.get_child() if row is not None else None
        else:
            anchor = self.flow.get_child_at_index(index)
        if anchor is not None:
            self._entry_menu(self.visible_tiles[index].payload, anchor)

    def _selected_index(self) -> int | None:
        if self.mode == "rows":
            return getattr(self, "_row_index", None)
        selected = self.flow.get_selected_children()
        return selected[0].get_index() if selected else None

    def _select_index(self, index: int) -> bool:
        count = len(self.visible_tiles) if hasattr(self, "visible_tiles") else 0
        if count == 0:
            return False
        index = count - 1 if index < 0 else max(0, min(count - 1, index))
        if self.mode == "rows":
            row = self.rows.get_row_at_index(index)
            if row is None:
                return False
            self._mark_row(index)
            row.grab_focus()
        else:
            child = self.flow.get_child_at_index(index)
            if child is None:
                return False
            self.flow.select_child(child)
            child.grab_focus()
        return True

    @staticmethod
    def _in_text(widget: Gtk.Widget | None) -> bool:
        while widget is not None:
            if isinstance(widget, (Gtk.Entry, Gtk.SearchEntry, Gtk.Text,
                                   Adw.EntryRow, Adw.PasswordEntryRow)):
                return True
            widget = widget.get_parent()
        return False


class App(Adw.Application):
    def __init__(self):
        # One window per user is the desktop behaviour, but a second instance has
        # to be possible for the Broadway test harness (REMOKON_NON_UNIQUE=1).
        flags = (Gio.ApplicationFlags.NON_UNIQUE if os.environ.get("REMOKON_NON_UNIQUE")
                 else Gio.ApplicationFlags.DEFAULT_FLAGS)
        super().__init__(application_id=APP_ID, flags=flags)

    def do_activate(self):
        # A second launch is an activation of this instance, not a second window:
        # `Window(self).present()` created a new one every time.
        window = self.get_active_window()
        if window is None:
            window = Window(self)
        window.present()


def main(argv: list[str]) -> int:
    # `--check` is what build.sh and install.sh run: it proves the archive carries
    # its own modules and that this interpreter has the GTK bindings, without
    # opening a window.
    if "--check" in argv:
        print(f"ok python={sys.version.split()[0]} gtk={Gtk.get_major_version()}."
              f"{Gtk.get_minor_version()} adw={Adw.get_major_version()}."
              f"{Adw.get_minor_version()} config={kodi.CONFIG_PATH}")
        return 0
    return App().run(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
