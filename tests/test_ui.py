"""Checks for the desktop client's window: layout, artwork, favourites.

Needs a display but no host and no network — a fake client answers with generated
PNGs. Run it headless (the `EGL_PLATFORM=x11` is not optional on this box: GDK's
EGL probe loads libnvidia-egl-wayland inside X and segfaults):

    EGL_PLATFORM=x11 xvfb-run -a /usr/bin/python3 tests/test_ui.py

Every assert here encodes a bug reported from a real screen, or a rule the
spec (openspec/specs) states. A failure means the window would misbehave again.
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("GSETTINGS_BACKEND", "memory")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gtk  # noqa: E402

# The app lives one level up: this file is run as a script, so Python would put
# `tests/` on sys.path and never find `kodi`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kodi  # noqa: E402
import ui  # noqa: E402

if not os.environ.get("DISPLAY"):
    print("no DISPLAY: run this under xvfb-run -a")
    raise SystemExit(2)

# The test process must never read or write the user's real config, and must never
# dial the live box. Everything the window persists is recorded instead.
SAVED: list[list[dict]] = []
kodi.load_config = lambda: None
SAVED_HOSTS: list = []
kodi.save_config = lambda host: SAVED_HOSTS.append(host)
# Nothing in the test process may adopt the real file from before the rename.
kodi.CONFIG_PATH = "/nonexistent/remokon/config.json"
kodi.LEGACY_CONFIG_PATH = "/nonexistent/yatse-remote/config.json"
kodi.load_favorites = lambda: []
kodi.save_view_mode = lambda mode: None
kodi.save_view_zoom = lambda zoom: None


def _record_favorites(favorites):
    SAVED.append(list(favorites))


kodi.save_favorites = _record_favorites

ENTRY_COUNT = 40


# -- fakes ------------------------------------------------------------------

def _png(width=300, height=450):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, width, height)
    pixbuf.fill(0x3355CCFF)
    return pixbuf.save_to_bufferv("png", [], [])[1]


PNG = _png()


def make_entries(count=ENTRY_COUNT, art=True):
    # Long, realistic titles on purpose: a wrapped label used to raise the card's
    # natural width past the size its view asked for.
    return [kodi.Entry({"label": f"A fairly long clip title, number {i} (2024)",
                        "file": f"plugin://x/clip{i}.mkv",
                        "filetype": "file",
                        "thumbnail": f"image://%2fmedia%2fart{i}.jpg/" if art else "",
                        "art": {}}) for i in range(count)]


class FakeClient:
    """Answers like the host, and counts what was asked of it."""

    def __init__(self, entries=None, fail_urls=()):
        self.entries = entries if entries is not None else make_entries()
        self.image_calls: list[str] = []
        self.opened: list[str] = []
        self.directories: list[str] = []
        self.sent_text: list[str] = []
        self.stopped: list[int] = []
        self.fail_urls = set(fail_urls)
        # Every JSON-RPC method the window asked for (the 1 Hz tick polls
        # GUI.GetProperties, so tests filter by prefix rather than compare the whole list).
        self.rpc: list[str] = []
        # What `GUI.GetProperties` reports as the box's current window; 10000 is Home.
        self.window_id = 10000

    def _call(self, method, params=None, timeout=None):
        self.rpc.append(method)
        if method == "GUI.GetProperties":
            return {"currentwindow": {"id": self.window_id, "label": "Fake"}}
        return "OK"

    def send_text(self, text, done=True):
        self.sent_text.append(text)

    def stop(self, playerid):
        self.stopped.append(playerid)

    def image(self, url):
        self.image_calls.append(url)
        if url in self.fail_urls:
            raise OSError("refused for the test")
        return PNG

    def directory(self, path, media="files"):
        self.directories.append(path)
        return list(self.entries)

    def open_item(self, file):
        self.opened.append(file)

    def active_players(self):
        return []

    def ping(self):
        return "pong"

    def video_addons(self):
        return [kodi.Addon({"addonid": "plugin.video.fake",
                            "type": "xbmc.python.pluginsource",
                            "name": "Fake", "enabled": True})]


# -- harness ----------------------------------------------------------------

def pump(seconds=0.2):
    """Run the main loop for a while so idle callbacks and worker results land."""
    context = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + int(seconds * 1e6)
    while GLib.get_monotonic_time() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.004)


def pump_until(predicate, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.05)
        if predicate():
            return True
    return False


def pictures(widget):
    count = isinstance(widget, Gtk.Picture)
    child = widget.get_first_child()
    while child is not None:
        count += pictures(child)
        child = child.get_next_sibling()
    return count


def count_class(widget, css_class):
    count = 1 if css_class in widget.get_css_classes() else 0
    child = widget.get_first_child()
    while child is not None:
        count += count_class(child, css_class)
        child = child.get_next_sibling()
    return count


def cards(window):
    """Every card: where it sits, the size of its ART box, and whether it has art."""
    out = []
    child = window.flow.get_first_child()
    while child is not None:
        ok, point = child.compute_point(window.flow, Graphene.Point().init(0, 0))
        box = child.get_child()              # tile-card box
        inner = box.get_first_child() if box is not None else None   # the card box
        frame = inner.get_first_child() if inner is not None else None   # Gtk.Overlay
        art = frame.get_child() if isinstance(frame, Gtk.Overlay) else None
        out.append({"index": child.get_index(), "x": point.x, "y": point.y,
                    "cell_width": child.get_width(),
                    "card_width": inner.get_width() if inner is not None else 0,
                    "art_width": art.get_width() if art is not None else 0,
                    "right": point.x + child.get_width(),
                    "art": pictures(child) > 0})
        child = child.get_next_sibling()
    return out


def rows(window):
    out = []
    row = window.rows.get_first_child()
    while row is not None:
        out.append({"art": pictures(row) > 0, "label": row.get_index()})
        row = row.get_next_sibling()
    return out


_APPLICATION = None


def application():
    """One GtkApplication for the whole run: a second one cannot export the same
    D-Bus name, and every test opens its own window under this one."""
    global _APPLICATION
    if _APPLICATION is None:
        flags = Gio.ApplicationFlags.NON_UNIQUE if hasattr(Gio.ApplicationFlags, "NON_UNIQUE") else 0
        _APPLICATION = Adw.Application(application_id="io.github.bpawnzz.remokon.test", flags=flags)
        _APPLICATION.register(None)
    return _APPLICATION


class App:
    """One window per test, all under the single application."""

    def __init__(self):
        self.application = application()
        self.window = None

    def open(self, width=None, height=780):
        # No saved host: the window lands on the connection page. Tests attach a fake
        # client and drive the browse surface directly.
        #
        # A width is requested BEFORE present: without a window manager a mapped
        # window is not resized later, so each width needs its own window. `width=None`
        # opens at the app's own default, which is what the minimum-size check wants.
        window = ui.Window(self.application)
        if width is not None:
            window.set_size_request(width, height)
            window.set_default_size(width, height)
        window.present()
        pump(0.5)
        self.window = window
        return window

    def close(self):
        if self.window is not None:
            self.window.destroy()
            self.window = None
            pump(0.1)


def attach(window, entries=None, client=None):
    client = client or FakeClient(entries)
    window.host = kodi.Host("127.0.0.1", 8080)
    window.client = client
    window.addons = []
    window.entries = list(client.entries)
    window.locations = [ui.Location("Folder", "plugin://x/")]
    window.showing_favourites = False
    ui.Textures.clear()
    ui.ImageBytes.clear()
    return client


# -- layout -----------------------------------------------------------------

def test_cards_fit_the_pane():
    """Every card stays inside the pane, at any width, with no view change needed."""
    app = App()
    try:
        for width in (1400, 1100, 860, 700, 1900):
            window = app.open(width)
            attach(window)
            window.mode = "poster"
            window._render()
            pump(0.6)
            pane = window.stack.get_width()
            measured = cards(window)
            assert measured, "the grid rendered nothing"
            assert window.flow.get_width() <= pane + 1, (
                f"flow {window.flow.get_width()} wider than pane {pane} at {width}")
            over = [c for c in measured if c["right"] > window.flow.get_width() + 1]
            assert not over, f"{len(over)} cards outside the grid at {width}: {over[:2]}"
            wanted = window._card_metrics()[1]
            assert measured[0]["art_width"] == wanted, (
                f"art {measured[0]['art_width']} != requested {wanted} at {width}")
            assert window.get_width() >= width, (
                f"the window shrank to {window.get_width()} when asked for {width}")
            app.close()
    finally:
        app.close()


def test_window_can_be_made_small():
    """The content's own minimum width must stay small, or the window cannot shrink.

    Measured before this change: a 40-item poster grid with real (long) titles gave
    the window a 1189 px floor, because a wrapped label's natural width fed the
    FlowBox's minimum.
    """
    app = App()
    try:
        window = app.open()
        attach(window)
        window.mode = "poster"
        window._render()
        pump(0.6)
        minimum = window.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        assert minimum <= 620, (
            f"the window cannot be narrower than {minimum} px: content is setting a floor")
        # ... and the grid still fits whatever it is given, down to its own minimum.
        assert window.flow.get_width() <= window.stack.get_width() + 1
    finally:
        app.close()


def test_view_changes_keep_the_layout():
    """The reported bug: the first view is right, changing it puts cards off screen."""
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.mode = "poster"
        window._render()
        pump(0.5)
        pane = window.stack.get_width()
        window_width = window.get_width()

        for mode in ("compact", "wide", "rows", "poster", "compact", "wide", "poster"):
            window._set_mode(mode)
            pump(0.4)
            assert window.stack.get_width() == pane, (
                f"pane changed from {pane} to {window.stack.get_width()} entering {mode}")
            if mode == "rows":
                continue
            measured = cards(window)
            assert measured, f"{mode} rendered nothing"
            assert window.flow.get_width() <= pane + 1, (
                f"{mode}: flow {window.flow.get_width()} wider than pane {pane}")
            over = [c for c in measured if c["right"] > window.flow.get_width() + 1]
            assert not over, f"{mode}: {len(over)} cards off the right edge"
            wanted = window._card_metrics()[1]
            assert all(c["art_width"] == wanted for c in measured), (
                f"{mode}: artwork stretched to {measured[0]['art_width']} (wanted {wanted})")
        assert window.get_width() == window_width, (
            f"the window grew from {window_width} to {window.get_width()} while cycling views")
    finally:
        app.close()


def test_only_the_visible_view_is_built():
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.mode = "poster"
        window._render()
        pump(0.4)
        assert len(cards(window)) == ENTRY_COUNT
        assert window.rows.get_first_child() is None, "the list was built while in a grid"
        window._set_mode("rows")
        pump(0.4)
        assert len(rows(window)) == ENTRY_COUNT
        assert window.flow.get_first_child() is None, "the grid was built while in list mode"
    finally:
        app.close()


# -- artwork ----------------------------------------------------------------

def test_art_survives_a_view_change_without_refetching():
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window.mode = "poster"
        window._render()
        assert pump_until(lambda: all(c["art"] for c in cards(window)), 10), \
            "artwork never arrived for the first view"
        first_fetches = len(client.image_calls)
        assert first_fetches == ENTRY_COUNT, (
            f"{first_fetches} image requests for {ENTRY_COUNT} entries in one render")

        for mode in ("compact", "poster", "wide", "poster"):
            window._set_mode(mode)
            assert pump_until(lambda: all(c["art"] for c in cards(window)), 10), \
                f"artwork missing after switching to {mode}"
        assert len(client.image_calls) == first_fetches, (
            f"re-fetched {len(client.image_calls) - first_fetches} images on a view change")

        window._set_mode("rows")
        assert pump_until(lambda: all(r["art"] for r in rows(window)), 10), \
            "artwork missing in the list view"
        assert len(client.image_calls) == first_fetches, "the list view re-fetched images"
    finally:
        app.close()


def test_one_failed_image_leaves_the_rest():
    app = App()
    try:
        entries = make_entries(6)
        window = app.open(1100)
        client = FakeClient(entries, fail_urls={kodi.artwork_url(entries[2].art_url,
                                                                kodi.Host("127.0.0.1", 8080))})
        attach(window, client=client)
        window.mode = "poster"
        window._render()
        assert pump_until(lambda: sum(c["art"] for c in cards(window)) == 5, 10), \
            f"expected 5 of 6 with artwork, got {sum(c['art'] for c in cards(window))}"
    finally:
        app.close()


# -- favourites -------------------------------------------------------------

def test_sidebar_lists_favourites_first():
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.addons = [kodi.Addon({"addonid": "plugin.video.immich",
                                     "type": "xbmc.python.pluginsource",
                                     "name": "Immich", "enabled": True})]
        window._fill_sidebar()
        pump(0.2)
        first = window.sidebar.get_row_at_index(0)
        assert getattr(first, "is_favourites", False), "Favourites is not the first row"
        assert window.fav_detail.get_label() == "nothing saved"
        window.favorites = [{"kind": "directory", "path": "plugin://x/s1", "title": "S1",
                             "art": "", "thumbnail": ""}]
        window._update_fav_row()
        assert window.fav_detail.get_label() == "1 saved"
    finally:
        app.close()


def test_saving_and_removing_a_favourite():
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.mode = "poster"
        window._render()
        pump(0.4)
        SAVED.clear()
        first = window.visible_tiles[0].payload

        assert window._is_saved(first) is False
        window._toggle_favourite(first)
        pump(0.3)
        assert window._is_saved(first) is True
        assert SAVED and len(SAVED[-1]) == 1, SAVED
        assert count_class(window.flow, "fav-badge") == 1, "the saved card carries no mark"

        # Removing it takes the mark and the store entry away again.
        window._toggle_favourite(first)
        pump(0.3)
        assert window._is_saved(first) is False
        assert SAVED[-1] == [], SAVED
        assert count_class(window.flow, "fav-badge") == 0
    finally:
        app.close()


def test_favourites_destination_opens_and_plays():
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window.favorites = [
            {"kind": "directory", "path": "plugin://x/s1", "title": "Season 1",
             "art": "", "thumbnail": ""},
            {"kind": "media", "path": "plugin://x/s1/e1", "title": "Episode 1",
             "art": "", "thumbnail": ""},
        ]
        # `_show_favourites` reloads from disk, so the store the window reads has to
        # be the seeded one, not the harness's empty stub. Restored below: it is
        # module state and the other tests seed the window directly.
        original_load = kodi.load_favorites
        kodi.load_favorites = lambda: list(window.favorites)
        window._show_favourites()
        pump(0.4)
        assert window.showing_favourites is True
        assert len(window.visible_tiles) == 2
        assert window.title.get_subtitle().startswith("Favourites"), window.title.get_subtitle()

        # Opening a saved folder browses that path.
        window._activate(window.visible_tiles[0].payload)
        assert client.directories[-1] == "plugin://x/s1", client.directories
        assert window.showing_favourites is False

        # Playing a saved item asks the host to open its file.
        window._show_favourites()
        pump(0.2)
        window._activate(window.visible_tiles[1].payload)
        pump(0.3)
        assert client.opened == ["plugin://x/s1/e1"], client.opened
    finally:
        kodi.load_favorites = original_load
        app.close()


def test_empty_favourites_explains_itself():
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.favorites = []
        window._show_favourites()
        pump(0.3)
        assert window.stack.get_visible_child_name() == "notice"
        assert "Favourites" in window.notice.get_description(), window.notice.get_description()
    finally:
        app.close()


def test_the_item_menu_offers_favourites_in_one_click():
    """Favourites is one starred item, not a submenu, and pressing it toggles."""
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.mode = "poster"
        window._render()
        pump(0.4)
        SAVED.clear()
        payload = window.visible_tiles[0].payload
        anchor = window.flow.get_child_at_index(0)
        window._entry_menu(payload, anchor)
        pump(0.2)
        menu = window._menu
        assert menu is not None, "no menu was created"

        buttons = []
        child = menu.get_child().get_first_child()
        while child is not None:
            buttons.append(child)
            child = child.get_next_sibling()
        labels = [b.get_child().get_label() for b in buttons]
        assert labels == ["Play", "Favourites"], labels
        assert isinstance(menu, Gtk.Popover) and not isinstance(menu, Gtk.PopoverMenu), \
            "the item menu is still a model menu (actions to resolve, submenus to open)"

        # Unsaved: an outline star offering to save.
        favourite = buttons[1]
        assert favourite.get_child().get_icon_name() == "non-starred-symbolic", \
            favourite.get_child().get_icon_name()
        assert favourite.get_tooltip_text() == "Add to Favourites", favourite.get_tooltip_text()

        # The press itself — the signal a real click emits — saves the place.
        favourite.emit("clicked")
        pump(0.4)
        assert window._is_saved(payload) is True
        assert SAVED and len(SAVED[-1]) == 1, SAVED
        assert count_class(window.flow, "fav-badge") == 1, "the saved card carries no mark"

        # Reopened: a filled star that offers to remove, and it removes.
        window._entry_menu(payload, anchor)
        pump(0.2)
        second = window.menu_favourite
        assert second.get_child().get_icon_name() == "starred-symbolic", \
            second.get_child().get_icon_name()
        assert second.get_tooltip_text() == "Remove from Favourites"
        second.emit("clicked")
        pump(0.4)
        assert window._is_saved(payload) is False
        assert SAVED[-1] == [], SAVED
    finally:
        app.close()


# -- back and forward -------------------------------------------------------

def folders(count=30):
    """Folder entries: `_activate` on one moves deeper instead of playing."""
    return [kodi.Entry({"label": f"Folder {i}", "file": f"plugin://x/d{i}",
                        "filetype": "directory", "thumbnail": "", "art": {}})
            for i in range(count)]


def test_back_and_forward_restore_the_place():
    """Back returns to the folder as it was left: same tile, same scroll."""
    app = App()
    try:
        window = app.open(1100)
        attach(window, client=FakeClient(folders()))
        window.mode = "poster"
        window.locations = [ui.Location("Addons", "plugin://x/")]
        window._render()
        pump(0.4)

        adjustment = window.cards_scroll.get_vadjustment()
        adjustment.set_value(140)
        window._select_index(4)
        pump(0.2)
        scrolled = adjustment.get_value()
        assert scrolled > 0, "the surface did not scroll, so this test proves nothing"

        window._activate(window.visible_tiles[4].payload)
        pump(0.6)
        assert [l.title for l in window.locations] == ["Addons", "Folder 4"], \
            [l.title for l in window.locations]

        assert window._back() is True
        pump(0.8)
        assert [l.title for l in window.locations] == ["Addons"], \
            [l.title for l in window.locations]
        assert window._selected_index() == 4, window._selected_index()
        assert abs(adjustment.get_value() - scrolled) <= 1, \
            (adjustment.get_value(), scrolled)

        assert window._forward() is True
        pump(0.8)
        assert [l.title for l in window.locations] == ["Addons", "Folder 4"], \
            [l.title for l in window.locations]
    finally:
        app.close()


def test_a_new_move_drops_the_forward_history():
    app = App()
    try:
        window = app.open(1100)
        attach(window, client=FakeClient(folders()))
        window.mode = "poster"
        window.locations = [ui.Location("Addons", "plugin://x/")]
        window._render()
        pump(0.4)

        window._activate(window.visible_tiles[4].payload)
        pump(0.6)
        window._back()
        pump(0.8)
        assert window.forward_stack, "the back move did not leave a way forward"

        window._activate(window.visible_tiles[7].payload)
        pump(0.6)
        assert window.forward_stack == [], "a new move kept the old forward entries"
        assert window._forward() is False
    finally:
        app.close()


def test_the_header_offers_back_and_forward_between_pane_and_now_playing():
    app = App()
    try:
        window = app.open(1100)
        attach(window, client=FakeClient(folders()))
        window.mode = "poster"
        window._render()
        pump(0.4)

        # The header order asked for: the pane toggle, then Back, then Forward, then Now
        # playing.
        assert window.sidebar_button.get_next_sibling() is window.back_button, \
            "Back is not beside the pane toggle"
        assert window.back_button.get_next_sibling() is window.forward_button, \
            "Forward is not beside Back"
        assert window.forward_button.get_next_sibling() is window.now_button, \
            "Now playing is not after Forward"

        # Nothing moved yet: neither direction can act.
        assert window.back_button.get_sensitive() is False
        assert window.forward_button.get_sensitive() is False

        window._activate(window.visible_tiles[2].payload)
        pump(0.6)
        assert window.back_button.get_sensitive() is True
        assert window.forward_button.get_sensitive() is False

        window.back_button.emit("clicked")
        pump(0.8)
        assert window.forward_button.get_sensitive() is True, \
            "Forward stayed dead after Back"
        assert [l.title for l in window.locations] == ["Folder"], [l.title for l in window.locations]
    finally:
        app.close()


def test_shift_backspace_goes_forward():
    app = App()
    try:
        window = app.open(1100)
        client = attach(window, client=FakeClient(folders()))
        window.mode = "poster"
        window.locations = [ui.Location("Addons", "plugin://x/")]
        window._render()
        pump(0.4)

        window._activate(window.visible_tiles[4].payload)
        pump(0.6)
        assert press(window, Gdk.KEY_BackSpace) is True, "Backspace was not handled"
        pump(0.6)
        assert [l.title for l in window.locations] == ["Addons"], \
            [l.title for l in window.locations]
        assert press(window, Gdk.KEY_BackSpace, Gdk.ModifierType.SHIFT_MASK) is True, \
            "Shift+Backspace was not handled (forward is dead)"
        pump(0.6)
        assert [l.title for l in window.locations] == ["Addons", "Folder 4"], \
            "Shift+Backspace did not go forward"

        # With nothing ahead it does nothing — and it must never reach the box, where
        # `BackSpace` means `back`.
        window._back()
        pump(0.8)
        window.forward_stack.clear()
        window._update_nav_buttons()
        calls = list(client.rpc)
        assert press(window, Gdk.KEY_BackSpace, Gdk.ModifierType.SHIFT_MASK) is True, \
            "Shift+Backspace fell through to the box's own back"
        sent = [m for m in client.rpc[len(calls):] if m.startswith("Input.")]
        assert sent == [], sent
        assert [l.title for l in window.locations] == ["Addons"], \
            [l.title for l in window.locations]
    finally:
        app.close()


# -- the remote -------------------------------------------------------------

def test_remote_is_centred_and_inside_the_pane():
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window._render()
        pump(0.4)
        for width in (700, 1100, 1600):
            window.set_size_request(width, 780)
            window.set_default_size(width, 780)
            window._show_remote()
            pump(0.5)
            pane = window.stack.get_width()
            pad = window.remote_pad
            ok, point = pad.compute_point(window.stack, Graphene.Point().init(0, 0))
            assert ok, "the pad is not in the stack"
            centre = point.x + pad.get_width() / 2
            assert abs(centre - pane / 2) <= 2, (
                f"pad centre {centre:.0f} is not the pane centre {pane / 2:.0f} at {width}")
            assert point.x >= 0 and point.x + pad.get_width() <= pane + 1, (
                f"pad leaves the pane at {width}: x={point.x} w={pad.get_width()} pane={pane}")
            # Heights: the cluster is its natural size, not stretched to the window.
            assert pad.get_height() < 780, "the pad was stretched to the window height"
            window._leave_remote()
    finally:
        app.close()


# -- keyboard: the browse surface -------------------------------------------

def press(window, symbol, state=0):
    """Send one key through the app's capture-phase handler, then let it settle."""
    handled = window._on_key(None, symbol, 0, state)
    pump(0.15)
    return handled


def test_arrows_move_the_selection():
    """Arrows move the selection; `child_focus` on the child never did."""
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.mode = "poster"
        window._render()
        pump(0.5)
        assert window._selected_index() == 0, "the first render did not select entry 0"

        assert press(window, Gdk.KEY_Right) is True
        assert window._selected_index() == 1, "Right did not move the selection"
        assert press(window, Gdk.KEY_Right) is True
        assert window._selected_index() == 2
        before = window._selected_index() or 0
        assert press(window, Gdk.KEY_Down) is True
        after = window._selected_index() or 0
        assert after > before, "Down did not move to a lower line"
        focus = window.flow.get_focus_child()
        assert focus is not None and window._selected_index() == focus.get_index(), (
            "the selection and the keyboard focus disagree")

        assert press(window, Gdk.KEY_Home) is True
        assert window._selected_index() == 0
        assert press(window, Gdk.KEY_Up) is True, "Up on the first entry must still be handled"
        assert window._selected_index() == 0, "Up moved past the first entry"

        # The surface has to follow: a selection off-screen is not a selection.
        press(window, Gdk.KEY_End)
        assert window._selected_index() == len(window.visible_tiles) - 1
        adjustment = window.cards_scroll.get_vadjustment()
        assert adjustment.get_value() > 0, "the grid did not scroll to the last entry"

        # List view: the app's own record of the row must move with the focus.
        window._set_mode("rows")
        pump(0.5)
        window._select_index(0)
        pump(0.2)
        assert press(window, Gdk.KEY_Down) is True
        assert window._row_index == 1, f"the list did not mark row 1 ({window._row_index})"
        assert window.get_focus().get_index() == 1
        assert press(window, Gdk.KEY_Down) is True
        assert window._row_index == 2
        assert press(window, Gdk.KEY_Up) is True
        assert window._row_index == 1
    finally:
        app.close()


def test_enter_opens_and_plays():
    """Enter is the app's own: a folder opens, an item plays."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window.mode = "poster"
        window._render()
        pump(0.5)

        window._select_index(2)
        pump(0.2)
        wanted = window.visible_tiles[2].payload.file
        assert press(window, Gdk.KEY_Return) is True
        assert pump_until(lambda: client.opened == [wanted]), (
            f"Enter did not play the selected item: {client.opened} != [{wanted}]")

        # A folder: Enter asks for that folder's own path, not the parent's.
        client.entries = [kodi.Entry({"label": "mega-s4-crypt", "file": "plugin://x/mega/",
                                      "filetype": "directory", "thumbnail": "", "art": {}})]
        window.locations = [ui.Location("Add-on", "plugin://x/")]
        window._load_directory()
        pump(0.6)
        assert press(window, Gdk.KEY_Return) is True
        assert pump_until(lambda: "plugin://x/mega/" in client.directories), client.directories
        assert [location.path for location in window.locations] == [
            "plugin://x/", "plugin://x/mega/"], window.locations

        # An empty folder shows the notice page, so Enter is not ours — and must
        # not open or play anything either way.
        client.entries = []
        window._load_directory()
        pump(0.6)
        opened, directories = list(client.opened), list(client.directories)
        press(window, Gdk.KEY_Return)
        pump(0.3)
        assert client.opened == opened and client.directories == directories, (
            "Enter acted on an empty folder")
    finally:
        app.close()


def test_backspace_climbs_the_tree():
    """Backspace goes up a level; at the top it is Kodi's `back`."""
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.addons = [kodi.Addon({"addonid": "plugin.video.x", "name": "X", "type": "xbmc.python.pluginsource",
                                     "enabled": True, "thumbnail": ""})]
        sent = []
        window._send = lambda action: sent.append(action)

        window.locations = [ui.Location("Add-on", "plugin://x/"),
                            ui.Location("mega-s4-crypt", "plugin://x/mega/"),
                            ui.Location("Season 1", "plugin://x/mega/s1/")]
        window._render()
        pump(0.4)
        assert press(window, Gdk.KEY_BackSpace) is True
        assert pump_until(lambda: len(window.locations) == 2), window.locations
        assert [location.title for location in window.locations] == ["Add-on", "mega-s4-crypt"]
        assert sent == [], f"Backspace sent {sent} to the box while there was a level to climb"

        window.locations = [ui.Location("Add-on", "plugin://x/")]
        window._render()
        pump(0.4)
        assert press(window, Gdk.KEY_BackSpace) is True
        assert pump_until(lambda: window.locations == []), window.locations
        assert [tile.title for tile in window.visible_tiles] == ["X"], (
            f"the add-on list did not come back: {[t.title for t in window.visible_tiles]}")

        assert press(window, Gdk.KEY_BackSpace) is True
        pump(0.3)
        assert sent == ["back"], f"at the top Backspace must send `back`, sent {sent}"

        # A folder that is still loading is still a level: Backspace must climb it,
        # not fall through to the box (measured live: it sent `back` instead).
        sent.clear()
        window.locations = [ui.Location("Add-on", "plugin://x/"),
                            ui.Location("mega-s4-crypt", "plugin://x/mega/")]
        window.stack.set_visible_child_name("loading")
        assert press(window, Gdk.KEY_BackSpace) is True
        assert pump_until(lambda: len(window.locations) == 1), window.locations
        assert sent == [], f"Backspace during a load sent {sent} to the box"
    finally:
        app.close()


def test_remote_mode_keeps_its_keys():
    """Browse navigation must not leak onto the Remote page."""
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.locations = [ui.Location("Add-on", "plugin://x/"),
                            ui.Location("mega-s4-crypt", "plugin://x/mega/")]
        window._render()
        pump(0.4)
        sent = []
        window._send = lambda action: sent.append(action)
        window.player = {"playerid": 1, "fullscreen_video": False}
        window._show_remote()
        pump(0.2)

        assert press(window, Gdk.KEY_Right) is True
        assert sent == ["right"], f"the arrow did not drive the box: {sent}"
        assert len(window.locations) == 2, "an arrow climbed the add-on tree"

        sent.clear()
        assert press(window, Gdk.KEY_Return) is True
        assert sent == ["select"], f"Enter did not drive the box: {sent}"

        sent.clear()
        assert press(window, Gdk.KEY_BackSpace) is True
        assert sent == ["back"], f"Backspace must stay the box's back on the Remote: {sent}"
        assert len(window.locations) == 2, "Backspace climbed the add-on tree from the Remote"

        sent.clear()
        calls = []
        window._play_pause = lambda: calls.append("play_pause")
        # The poll clears `player` from the fake client's empty playback_state — the
        # earlier tick in `_show_remote()` lands before this line, so re-arm it here.
        window.player = {"playerid": 1, "fullscreen_video": False}
        assert press(window, Gdk.KEY_space) is True
        assert calls == ["play_pause"], f"Space did not pause from the Remote: {calls} {sent}"
        assert sent == [], f"Space sent an action instead of pausing: {sent}"
    finally:
        app.close()


def test_space_toggles_playback_anywhere():
    """Space is the transport on every surface, buttons included."""
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.mode = "poster"
        window._render()
        pump(0.5)
        calls = []
        window._play_pause = lambda: calls.append("play_pause")

        # Nothing playing: the key is not ours.
        window.player = None
        assert press(window, Gdk.KEY_space) is False, "Space was taken with no player"
        assert calls == []

        # A tile selected, a film playing.
        window.player = {"playerid": 1, "fullscreen_video": False}
        assert press(window, Gdk.KEY_space) is True
        assert calls == ["play_pause"], calls
        assert window._selected_index() == 0, "Space moved the selection"

        # The Remote page focuses a Gtk.Button, which used to eat Space.
        calls.clear()
        sent = []
        window._send = lambda action: sent.append(action)
        window._show_remote()
        pump(0.05)
        window.player = {"playerid": 1, "fullscreen_video": False}
        assert isinstance(window.get_focus(), Gtk.Button), "the remote did not focus a button"
        assert press(window, Gdk.KEY_space) is True
        assert calls == ["play_pause"], f"the button swallowed Space: {calls} {sent}"
        assert sent == [], f"the focused button was activated: {sent}"
        window._leave_remote()
        pump(0.3)

        # Typing in the filter is still typing.
        window.locations = [ui.Location("Add-on", "plugin://x/")]
        window._render()
        pump(0.3)
        calls.clear()
        window.search.set_visible(True)
        window.search.grab_focus()
        pump(0.2)
        assert press(window, Gdk.KEY_space) is False, "Space was stolen from the filter field"
        assert calls == [], "Space in the filter field touched playback"
    finally:
        app.close()


# -- keyboard: the browse surface's transport keys ---------------------------

def test_s_stops_from_the_browse_surface():
    """`s` stops from the browse pages and nowhere else."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window.mode = "poster"
        window._render()
        pump(0.5)
        stopped = []
        client.stop = lambda playerid: stopped.append(playerid)
        saved = list(window.favorites)

        # Nothing playing: the key is consumed but reaches nothing.
        window.player = None
        assert press(window, Gdk.KEY_s) is True, "s must be handled on the browse surface"
        pump(0.3)
        assert stopped == [], f"stop was called with no player: {stopped}"

        window.player = {"playerid": 7, "fullscreen_video": True}
        selected = window._selected_index()
        assert press(window, Gdk.KEY_s) is True
        assert pump_until(lambda: stopped == [7]), f"the player was not stopped: {stopped}"
        assert window._selected_index() == selected, "s moved the selection"
        assert window.favorites == saved, "s changed the favourites store"

        # The Remote page keeps its own map: `s` is nothing there, `x` is stop.
        sent, stopped_before = [], list(stopped)
        window._send = lambda action: sent.append(action)
        window._show_remote()
        pump(0.2)
        window._on_key(None, Gdk.KEY_s, 0, 0)
        pump(0.3)
        assert sent == [], f"s sent something from the Remote: {sent}"
        assert stopped == stopped_before, "s stopped playback from the Remote page"
        window._on_key(None, Gdk.KEY_x, 0, 0)
        pump(0.2)
        assert sent == ["stop"], f"x is no longer the Remote's stop: {sent}"
        window._leave_remote()
        pump(0.2)

        # Typing in the filter is still typing.
        window.search.set_visible(True)
        window.search.grab_focus()
        pump(0.2)
        stopped.clear()
        assert press(window, Gdk.KEY_s) is False, "s was stolen from the filter field"
        assert stopped == [], "typing s in the filter stopped the film"
    finally:
        app.close()


def test_o_toggles_player_info_on_both_pages():
    """`o` is one path: the browse page and the Remote send the same action."""
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.mode = "poster"
        window._render()
        pump(0.5)
        window.player = {"playerid": 1, "fullscreen_video": True}
        sent = []
        window._send = lambda action: sent.append(action)

        assert press(window, Gdk.KEY_o) is True
        assert sent == ["codecinfo"], f"o on the browse surface sent {sent}"
        # A toggle: the same action a second time is what closes it again.
        assert press(window, Gdk.KEY_o) is True
        assert sent == ["codecinfo", "codecinfo"], sent

        window._show_remote()
        pump(0.2)
        sent.clear()
        assert press(window, Gdk.KEY_o) is True
        assert sent == ["codecinfo"], f"o on the Remote sent {sent}"
        window._leave_remote()
        pump(0.2)

        sent.clear()
        window.search.set_visible(True)
        window.search.grab_focus()
        pump(0.2)
        assert press(window, Gdk.KEY_o) is False, "o was stolen from the filter field"
        assert sent == [], "typing o in the filter reached the box"
    finally:
        app.close()


# -- playback: replacing what is playing -------------------------------------

def test_replacing_playback_stops_first():
    """A second video is stop → 1.5 s → open; with nothing playing, open at once."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window.mode = "poster"
        window._render()
        pump(0.5)
        events = []
        client.stop = lambda playerid: events.append(("stop", playerid))
        client.open_item = lambda file: events.append(("open", file))
        window._select_index(1)
        pump(0.2)
        wanted = window.visible_tiles[1].payload.file

        # Something is playing: it has to be stopped, and the pause has to happen.
        client.active_players = lambda: [{"playerid": 3}]
        started = time.monotonic()
        assert press(window, Gdk.KEY_Return) is True
        assert pump_until(lambda: events == [("stop", 3), ("open", wanted)], timeout=6), events
        elapsed = time.monotonic() - started
        assert elapsed >= ui.REPLACE_PAUSE - 0.1, (
            f"the new file was opened {elapsed:.2f}s after the stop, "
            f"want >= {ui.REPLACE_PAUSE}")

        # Nothing playing: no stop, no wait.
        events.clear()
        client.active_players = lambda: []
        started = time.monotonic()
        assert press(window, Gdk.KEY_Return) is True
        assert pump_until(lambda: events == [("open", wanted)], timeout=4), events
        idle = time.monotonic() - started
        assert idle < 1.0, f"an idle selection waited {idle:.2f}s for nothing"
    finally:
        app.close()


# -- runner -----------------------------------------------------------------

def test_comma_and_period_seek_from_the_browse_surface():
    """`,`/`.` step the film from the browse pages, and only from there."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window.mode = "poster"
        window._render()
        pump(0.5)
        sent = []
        window._send = lambda action: sent.append(action)

        # Nothing playing: nothing to step, and the key is left alone.
        window.player = None
        assert press(window, Gdk.KEY_period) is False, \
            "period must not be consumed with no player"
        assert press(window, Gdk.KEY_comma) is False, \
            "comma must not be consumed with no player"
        assert sent == [], f"seek sent with no player: {sent}"

        window.player = {"playerid": 4, "fullscreen_video": True}
        selected = window._selected_index()
        assert press(window, Gdk.KEY_period) is True, "period must be handled while playing"
        assert press(window, Gdk.KEY_comma) is True, "comma must be handled while playing"
        assert sent == ["stepforward", "stepback"], f"wrong seeks: {sent}"
        assert window._selected_index() == selected, "a seek key moved the selection"

        # The ladder is Kodi's: five presses are five actions, no counter here.
        # (The fake client reports nothing from `playback_state`, so the tick clears
        # `player` — re-arm it per press, as the live poll does.)
        sent.clear()
        for _ in range(5):
            window.player = {"playerid": 4, "fullscreen_video": True}
            press(window, Gdk.KEY_period)
        assert sent == ["stepforward"] * 5, f"the app kept a ladder: {sent}"

        # Arrows still navigate and must not seek.
        sent.clear()
        press(window, Gdk.KEY_Right)
        assert sent == [], f"the arrow key seeked: {sent}"

        # The Remote page is untouched: its own arrows do the seeking.
        window._show_remote()
        pump(0.2)
        sent.clear()
        assert window._on_key(None, Gdk.KEY_period, 0, 0) is False, \
            "period is consumed on the Remote page"
        assert window._on_key(None, Gdk.KEY_comma, 0, 0) is False, \
            "comma is consumed on the Remote page"
        pump(0.2)
        assert sent == [], f"seek sent from the Remote: {sent}"
        window._leave_remote()
        pump(0.2)
    finally:
        app.close()

# -- the sidebar, the mode switch, and typed text ---------------------------

def test_the_sidebar_collapses_and_gives_its_width_away():
    """`Ctrl+B` and the header button fold the sidebar; the separator goes with it."""
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window.mode = "poster"
        window._render()
        pump(0.6)
        assert window.sidebar_pane.get_visible(), "the sidebar starts hidden"
        before = window.stack.get_width()

        assert press(window, Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK) is True, \
            "Ctrl+B was not handled"
        assert not window.sidebar_pane.get_visible(), "Ctrl+B did not collapse the sidebar"
        pump(0.2)
        after = window.stack.get_width()
        assert after > before, f"the pane did not take the freed width: {before} -> {after}"

        # Tab must not aim the caret at a list that is not on screen.
        assert window._cycle_pane() is False, "Tab still targeted the hidden sidebar"

        assert press(window, Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK) is True
        assert window.sidebar_pane.get_visible(), "Ctrl+B did not bring the sidebar back"

        # The header button runs the same method.
        window.sidebar_button.emit("clicked")
        pump(0.2)
        assert not window.sidebar_pane.get_visible(), "the header button did not collapse it"
        window.sidebar_button.emit("clicked")
        pump(0.2)
        assert window.sidebar_pane.get_visible(), "the header button did not restore it"
    finally:
        app.close()


def test_ctrl_b_toggles_the_sidebar_and_z_is_unbound():
    """`Ctrl+B` is the sidebar; `z` is not bound at all any more."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        sent = []
        window._send = lambda action: sent.append(action)
        window._render()
        pump(0.6)
        assert window.sidebar_pane.get_visible(), "the sidebar starts hidden"

        # `z` is nobody's: it must not toggle, send, or touch playback.
        window.player = {"playerid": 7, "fullscreen_video": True}
        assert press(window, Gdk.KEY_z) is False, "`z` is still handled"
        assert window.sidebar_pane.get_visible(), "`z` still toggles the sidebar"
        assert sent == [], f"`z` sent something to the box: {sent}"
        assert client.stopped == [], f"`z` touched playback: {client.stopped}"

        # Ctrl+B is the sidebar, and a playing video must not be touched by it.
        assert press(window, Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK) is True, \
            "Ctrl+B was not handled"
        assert not window.sidebar_pane.get_visible(), "Ctrl+B did not hide the sidebar"
        assert press(window, Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK) is True
        assert window.sidebar_pane.get_visible(), "Ctrl+B did not bring the sidebar back"
        assert sent == [], f"Ctrl+B sent something to the box: {sent}"
        assert client.stopped == [], f"Ctrl+B touched playback: {client.stopped}"

        # The Remote is no exception: the sidebar is not a page.
        window._show_remote()
        pump(0.2)
        window.player = {"playerid": 7, "fullscreen_video": False}
        assert press(window, Gdk.KEY_z) is False, "`z` acts on the Remote"
        assert press(window, Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK) is True
        assert not window.sidebar_pane.get_visible(), "Ctrl+B did not hide it on the Remote"
        assert press(window, Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK) is True
        assert window.sidebar_pane.get_visible()

        # `s` is still the browse surface's stop (the restored test pins it); `x` is
        # the box's own stop everywhere.
        sent.clear()
        assert press(window, Gdk.KEY_x) is True
        assert sent == ["stop"], f"x is no longer stop: {sent}"

        # `h` is home on the Remote — the Remote is showing from the block above.
        sent.clear()
        assert press(window, Gdk.KEY_h) is True, "`h` was not handled on the Remote"
        assert sent == ["home"], f"`h` is not home on the Remote: {sent}"

        # …and nowhere else: a stray `h` while browsing used to throw the box onto its
        # home screen, mid-film.
        window._leave_remote()
        pump(0.2)
        sent.clear()
        page = window.stack.get_visible_child_name()
        assert press(window, Gdk.KEY_h) is False, "`h` still acts on the browse surface"
        assert sent == [], f"`h` sent from the browse surface: {sent}"
        assert window.stack.get_visible_child_name() == page, "`h` moved the page"

        window._show_now()
        pump(0.2)
        sent.clear()
        assert press(window, Gdk.KEY_h) is False, "`h` acts on Now playing"
        assert sent == [], f"`h` sent from Now playing: {sent}"
        window._leave_now()
        pump(0.2)

        # The rest of the letter set is untouched.
        sent.clear()
        for symbol in (Gdk.KEY_j, Gdk.KEY_k, Gdk.KEY_l):
            assert press(window, symbol) is True, f"{Gdk.keyval_name(symbol)} was not handled"
        assert sent == ["down", "up", "right"], f"the letter arrows changed: {sent}"

        # A text field still swallows both (the shortcut test also lists these keys).
        window.search.set_visible(True)
        window.search.grab_focus()
        pump(0.2)
        visible, typed = window.sidebar_pane.get_visible(), list(sent)
        for symbol in (Gdk.KEY_z, Gdk.KEY_h):
            assert window._on_key(None, symbol, 0, 0) is False, \
                f"{Gdk.keyval_name(symbol)} was taken from a text field"
        pump(0.2)
        assert window.sidebar_pane.get_visible() == visible, "typing toggled the sidebar"
        assert sent == typed, f"typing sent actions: {sent}"
        assert client.sent_text == []
    finally:
        app.close()


def test_the_key_map_has_no_shadowed_letter():
    """One effect per letter, and every letter not in the map does nothing.

    Two letters lied in one session — `h` was documented as home but shadowed by
    `"h": "left"` in BOX_KEYS, and `s` was briefly taken from stop. Both were found by
    accident. This presses all 26 on the browse surface and compares the effect; `h` is
    deliberately absent from it, because home belongs to the Remote.
    """
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        sent: list[str] = []
        window._send = lambda action: sent.append(action)
        window._render()
        pump(0.5)

        # The map as documented. Anything absent must do nothing at all.
        expected = {
            "c": "contextmenu", "f": "fullscreen", "i": "info",
            "j": "down", "k": "up", "l": "right", "m": "mute", "o": "codecinfo",
            "x": "stop",
        }
        # Letters whose effect is not an Input action.
        special = {"r": "page:remote", "n": "page:now", "v": "view", "s": "stop"}

        problems = []
        # A live player, complete enough that `n` (Now playing) and `s` both work.
        live = {"playerid": 7, "fullscreen_video": False, "title": "T", "file": "x.mkv",
                "art": "", "speed": 1, "percentage": 0.0, "time": "0:00", "total": "0:00",
                "elapsed_seconds": 0, "total_seconds": 0}
        for letter in "abcdefghijklmnopqrstuvwxyz":
            # Reset: grid page, sidebar showing, nothing recorded, a live player.
            window.stack.set_visible_child_name("grid")
            window.sidebar_pane.set_visible(True)
            sent.clear()
            client.stopped.clear()
            pump(0.15)
            # After the pump, not before: a 1 Hz tick lands during it and clears
            # `player` from the fake client's empty playback_state.
            window.player = dict(live)
            mode_before = window.mode
            page_before = window.stack.get_visible_child_name()

            handled = press(window, Gdk.keyval_from_name(letter))
            pump(0.25)

            if client.stopped:
                effect = "stop"
            elif sent:
                effect = sent[0] if len(sent) == 1 else f"actions:{sent}"
            elif window.stack.get_visible_child_name() != page_before:
                effect = f"page:{window.stack.get_visible_child_name()}"
            elif window.sidebar_pane.get_visible() is False:
                effect = "sidebar"
            elif window.mode != mode_before:
                effect = "view"
            else:
                effect = None

            if letter in expected:
                want = expected[letter]
                if effect != want or handled is not True:
                    problems.append(f"{letter}: want {want}, got {effect} (handled={handled})")
            elif letter in special:
                want = special[letter]
                if effect != want or handled is not True:
                    problems.append(f"{letter}: want {want}, got {effect} (handled={handled})")
            else:
                # Unbound: GTK may still consume it (Tab-like keys), so only the effect
                # matters — and it must be nothing.
                if effect is not None:
                    problems.append(f"{letter}: unbound but did {effect}")

        assert not problems, "the key map disagrees with the code:\n  " + "\n  ".join(problems)
    finally:
        app.close()


def test_equal_raises_the_volume_and_ctrl_equal_still_zooms():
    """`=` is volume up, `-` is volume down, and Ctrl+= is still zoom.

    `+` needs Shift on a US layout, so the key under the fingers is `equal` in GDK;
    before this it bound to nothing outside Ctrl, so the volume only went one way.
    """
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        sent: list[str] = []
        window._send = lambda action: sent.append(action)
        window._render()
        pump(0.5)

        assert press(window, Gdk.KEY_equal) is True
        assert sent == ["volumeup"], sent
        sent.clear()
        assert press(window, Gdk.KEY_minus) is True
        assert sent == ["volumedown"], sent

        # Ctrl+= is zoom: it changes the tiles and never reaches the box.
        sent.clear()
        zoom_before = window.zoom
        assert press(window, Gdk.KEY_equal, Gdk.ModifierType.CONTROL_MASK) is True
        assert window.zoom != zoom_before, "Ctrl+= stopped zooming"
        assert sent == [], f"Ctrl+= sent to the box: {sent}"
    finally:
        app.close()


def test_a_fresh_install_starts_on_the_settings_page():
    """Nothing is built in: no config means the host form, with an empty address."""
    app = App()
    try:
        window = app.open(1100)
        assert window.stack.get_visible_child_name() == "settings", \
            window.stack.get_visible_child_name()
        assert window.row_address.get_text() == "", "a host was filled in from nowhere"
        assert window.title.get_subtitle() == "Set your host to begin"
        assert window.connect_status.get_visible() is False
        # The gear has nowhere to go back to, and must not blank the window.
        window.settings_button.emit("clicked")
        pump(0.2)
        assert window.stack.get_visible_child_name() == "settings"
    finally:
        app.close()


def test_the_gear_opens_settings_from_every_page_and_returns():
    """A settings screen you can only reach by deleting a file is not a settings
    screen: the gear works from the grid and from the Remote, and both come back."""
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window._render()
        pump(0.5)

        window.settings_button.emit("clicked")
        pump(0.2)
        assert window.stack.get_visible_child_name() == "settings"
        # Prefilled from the live host, not blank.
        assert window.row_address.get_text() == "127.0.0.1"
        assert window.row_port.get_text() == "8080"

        window.settings_button.emit("clicked")
        pump(0.2)
        assert window.stack.get_visible_child_name() == "grid", \
            "the gear did not come back to browsing"

        # The Remote, and Escape leaving the page.
        window._show_remote()
        pump(0.2)
        window.settings_button.emit("clicked")
        pump(0.2)
        assert window.stack.get_visible_child_name() == "settings"
        assert press(window, Gdk.KEY_Escape) is True, "Escape was not the way out"
        pump(0.2)
        assert window.stack.get_visible_child_name() == "remote", \
            "Escape did not return to the Remote"
    finally:
        app.close()


def test_settings_saves_what_was_typed_and_ignores_every_shortcut():
    """The page is a form: its fields must reach the config, and no letter may reach
    the box or move the window while it is open."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window._render()
        pump(0.5)
        sent = []
        window._send = lambda action: sent.append(action)
        window.player = {"playerid": 7, "fullscreen_video": False}

        window._show_settings()
        pump(0.2)
        # Focus nothing in particular: `in_text` is false here, so only the page's own
        # guard can keep the map away from the form.
        window.set_focus(None)
        pump(0.1)
        visible = window.sidebar_pane.get_visible()
        for symbol in (Gdk.KEY_z, Gdk.KEY_h, Gdk.KEY_s, Gdk.KEY_x, Gdk.KEY_backslash,
                       Gdk.KEY_space, Gdk.KEY_question, Gdk.KEY_1, Gdk.KEY_r,
                       Gdk.KEY_n, Gdk.KEY_j):
            assert window._on_key(None, symbol, 0, 0) is False, \
                f"{Gdk.keyval_name(symbol)} acted on the settings page"
        pump(0.2)
        assert window.stack.get_visible_child_name() == "settings", "a key left the page"
        assert window.sidebar_pane.get_visible() == visible, "a key toggled the sidebar"
        assert sent == [], f"a key sent actions from the form: {sent}"

        # Fill it in and save: everything reaches the Host, including the scheme.
        SAVED_HOSTS.clear()
        window.row_address.set_text("kodi.lan")
        window.row_port.set_text("9090")
        window.row_scheme.set_selected(1)
        window.row_user.set_text("me")
        window.row_pass.set_text("pw")
        real_client = ui.KodiClient
        ui.KodiClient = lambda host: client      # `_connect` constructs one; hand it the fake
        try:
            window._apply_settings()
        finally:
            ui.KodiClient = real_client
        assert pump_until(lambda: bool(SAVED_HOSTS)), "the save never reached the config"
        host = SAVED_HOSTS[-1]
        assert (host.address, host.port, host.scheme, host.username, host.password) == \
            ("kodi.lan", 9090, "https", "me", "pw"), vars(host)
        assert pump_until(
            lambda: window.stack.get_visible_child_name() == "grid"), \
            f"a good host did not open the grid: {window.stack.get_visible_child_name()}"
        assert sent == [], f"saving sent actions: {sent}"

        # An empty address is refused on the page, not sent anywhere.
        window._show_settings()
        pump(0.2)
        window.row_address.set_text("  ")
        SAVED_HOSTS.clear()
        window._apply_settings()
        pump(0.2)
        assert window.stack.get_visible_child_name() == "settings"
        assert window.connect_status.get_visible()
        assert SAVED_HOSTS == [], "a blank host was saved"
    finally:
        app.close()


def test_help_lists_the_whole_key_map():
    """The `?` dialog is the only written-down map, so it has to be complete — and it
    opens from the button and from the key.

    Adw.Dialog keeps `visible` set under xvfb even after force_close(), so what is
    asserted here is the build and that each entry point is handled, not the flag
    flipping back.
    """
    app = App()
    try:
        window = app.open(1100)
        attach(window)
        window._render()
        pump(0.4)

        # The text field first, on a window whose focus has not been through a dialog.
        window.search.set_visible(True)
        window.search.grab_focus()
        pump(0.2)
        assert window._in_text(window.get_focus()), "the filter field did not take focus"
        assert window._on_key(None, Gdk.KEY_question, 0, 0) is False, \
            "`?` was taken from the filter field"
        window._hide_filter()
        pump(0.2)

        window.help_button.emit("clicked")
        pump(0.2)
        dialog = window._help
        assert dialog is not None, "the help button opened nothing"
        assert dialog.get_visible(), "the help dialog was built but not shown"
        assert dialog.get_title() == "Keyboard shortcuts & usage"

        text = " ".join(f"{keys} {what}" for _title, rows in ui.HELP
                        for keys, what in rows)
        for token in ("\u2191", "Enter", "Backspace", "/", "Tab", "h", "s", "v",
                      "Ctrl +", "c", "Ctrl+B", "n", "Escape", "i", "o", "x", "m",
                      "1\u20139", "PgUp", "t", "Space", "?"):
            assert token in text, f"the help never mentions {token!r}"
        for word in ("click", "right-click", "wheel"):
            assert word in text, f"the help says nothing about the mouse ({word})"

        # The key takes the same path as the button.
        dialog.force_close()
        pump(0.2)
        window.set_focus(None)
        assert press(window, Gdk.KEY_question) is True, "`?` was not handled"
    finally:
        app.close()


def test_backslash_toggles_between_browsing_and_the_remote():
    """`\\` is the mode switch, and it comes back to the same spot in the same folder."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window.mode = "poster"
        window.locations = [ui.Location("Add-on", "plugin://x/"),
                            ui.Location("Plex Hub", "plugin://x/plex/")]
        window._render()
        pump(0.5)
        window._select_index(2)
        pump(0.2)
        requested = list(client.directories)
        trail = [location.path for location in window.locations]

        assert press(window, Gdk.KEY_backslash) is True, "backslash was not handled"
        assert window.stack.get_visible_child_name() == "remote", "backslash did not open the Remote"

        assert press(window, Gdk.KEY_backslash) is True, "backslash did not leave the Remote"
        assert window.stack.get_visible_child_name() == "grid", \
            f"backslash came back to {window.stack.get_visible_child_name()}"
        assert [location.path for location in window.locations] == trail, window.locations
        assert window._selected_index() == 2, f"the selection moved: {window._selected_index()}"
        assert client.directories == requested, "the toggle re-requested the folder"

        # From the player it goes to the Remote and comes back to the player.
        window._show_now()
        pump(0.2)
        assert press(window, Gdk.KEY_backslash) is True
        assert window.stack.get_visible_child_name() == "remote"
        assert press(window, Gdk.KEY_backslash) is True
        assert window.stack.get_visible_child_name() == "now", window.stack.get_visible_child_name()

        # A state, not a mode: the settings page has nothing to toggle to.
        window.stack.set_visible_child_name("settings")
        pump(0.1)
        assert press(window, Gdk.KEY_backslash) is False, "backslash acted on the settings page"
        assert window.stack.get_visible_child_name() == "settings"
    finally:
        app.close()


def test_the_box_keyboard_dialog_pops_the_text_box_once():
    """The box's keyboard dialog raises ours — once per dialog, not once per second."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window._render()
        pump(0.4)
        window._show_remote()
        pump(0.3)
        assert window._text_window is None, "a text box appeared with no dialog on the box"

        # The box opens its keyboard dialog: the next poll pops ours. Driving it
        # through the poll (not a hand-fed `_state`) keeps one source of truth — a
        # synthetic id would be contradicted by the next real tick.
        client.window_id = 10103
        assert pump_until(lambda: window._text_window is not None), \
            "the keyboard dialog did not pop a text box"
        first = window._text_window
        pump(1.3)                                  # at least one more poll, same dialog
        assert window._text_window is first, "a second text box was opened for the same dialog"

        # Cancelled while the box still waits: it must not come back on its own.
        window._close_text_dialog()
        pump(1.3)
        assert window._text_window is None, "the cancelled box came back on its own"

        client.window_id = 10000                   # the box leaves the dialog
        pump(1.3)
        client.window_id = 10156                   # the touch keyboard counts too
        assert pump_until(lambda: window._text_window is not None), \
            "a new keyboard dialog did not pop again"

        # An automatically opened box does not outlive the box's dialog.
        client.window_id = 10000
        assert pump_until(lambda: window._text_window is None), \
            "the automatic text box outlived the dialog"
    finally:
        app.close()


def test_the_text_box_sends_what_was_typed():
    """The Text button and `t` open the same box, and Enter sends to the box."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        window._render()
        pump(0.4)
        window._show_remote()
        pump(0.2)

        window._remote_buttons["text"].emit("clicked")
        pump(0.2)
        assert window._text_window is not None, "the Text button did not open the box"
        window._close_text_dialog()
        pump(0.1)

        assert press(window, Gdk.KEY_t) is True, "`t` was not handled on the Remote"
        pump(0.2)
        assert window._text_window is not None, "`t` did not open the text box"
        entry = window._text_entry
        assert entry is not None, "the box has no entry to type into"
        entry.set_text("the wire")
        entry.emit("activate")
        assert pump_until(lambda: client.sent_text == ["the wire"]), client.sent_text
        assert window._text_window is None, "the text box stayed open after sending"
    finally:
        app.close()


def test_a_text_field_swallows_every_shortcut():
    """Typing is typing: no app shortcut, and no box action, comes out of a field."""
    app = App()
    try:
        window = app.open(1100)
        client = attach(window)
        sent = []
        window._send = lambda action: sent.append(action)
        window._render()
        pump(0.4)
        window.search.set_visible(True)
        window.search.grab_focus()
        pump(0.2)
        # The focus lands on the entry's inner text widget, so ask the same helper the
        # key handler asks rather than guessing the concrete class.
        assert window._in_text(window.get_focus()), "the filter did not take the caret"

        for symbol in (Gdk.KEY_h, Gdk.KEY_s, Gdk.KEY_x, Gdk.KEY_v, Gdk.KEY_n, Gdk.KEY_r,
                       Gdk.KEY_backslash, Gdk.KEY_z, Gdk.KEY_1, Gdk.KEY_o, Gdk.KEY_i,
                       Gdk.KEY_c, Gdk.KEY_slash, Gdk.KEY_BackSpace):
            assert window._on_key(None, symbol, 0, 0) is False, \
                f"{Gdk.keyval_name(symbol)} was taken from a text field"
        assert window._on_key(None, Gdk.KEY_b, 0, Gdk.ModifierType.CONTROL_MASK) is False, \
            "Ctrl+B was taken from a text field"
        pump(0.2)
        assert sent == [], f"a text field sent box actions: {sent}"
        assert window.stack.get_visible_child_name() == "grid", "a text field changed the page"
        assert not window.search.get_text(), "a shortcut key typed into the field"
        assert client.sent_text == []
    finally:
        app.close()


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print(f"FAIL {test.__name__}: {error}")
        except Exception as error:  # noqa: BLE001 - report, keep going
            failures += 1
            print(f"ERROR {test.__name__}: {type(error).__name__}: {error}")
        else:
            print(f"ok   {test.__name__}")
    print(f"{len(tests) - failures}/{len(tests)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
