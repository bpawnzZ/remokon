"""Checks for the desktop client's protocol layer. No display, no real host.

`python3 tests/test_kodi.py` — every assert here encodes a rule that was
measured against a live Kodi 21.3; a failure means the client would talk to Kodi wrong.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import urllib.request

from http.server import BaseHTTPRequestHandler, HTTPServer

# The app lives one level up: this file is run as a script, so Python would put
# `tests/` on sys.path and never find `kodi`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kodi
from kodi import Addon, Entry, Host, KodiClient, KodiError, artwork_url, strip_kodi_markup


# -- fake host -------------------------------------------------------------

# What `Input.SendText` was asked for, in order.
SENT: list[dict] = []
# Every (method, params) the fake host received, so a test can pin the wire shape.
CALLS: list[tuple] = []


class _Handler(BaseHTTPRequestHandler):
    """Answers the two shapes that matter: a JSON-RPC POST and an image GET."""

    def log_message(self, *args):  # keep the output clean
        pass

    def _send(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.headers.get("Authorization"):
            # Kodi's real behaviour: 401 with an EMPTY body.
            return self._send(401)
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        method = request.get("method")
        CALLS.append((method, request.get("params")))
        if method == "JSONRPC.Ping":
            return self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1,
                                               "result": "pong"}).encode())
        if method == "Boom":
            payload = {"jsonrpc": "2.0", "id": 1,
                       "error": {"code": -32602, "message": "Invalid params."}}
            return self._send(200, json.dumps(payload).encode())
        if method == "Files.GetDirectory":
            payload = {"jsonrpc": "2.0", "id": 1, "result": {"files": [
                {"label": "[B]My Subscriptions[/B]", "file": "plugin://x/?a",
                 "filetype": "directory", "thumbnail": "image://%2ficon.png"},
                {"label": "A Movie", "file": "plugin://x/play", "filetype": "unknown",
                 "thumbnail": "special://home/thumb.jpg", "art": {"poster": "https://cdn/p.jpg"}},
                {"label": "[REC] Raw feed", "file": "plugin://x/rec", "filetype": "file"},
            ]}}
            return self._send(200, json.dumps(payload).encode())
        if method == "Player.GetActivePlayers":
            return self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1,
                                               "result": [{"playerid": 1, "type": "video"}]}).encode())
        if method == "Player.GetItem":
            payload = {"jsonrpc": "2.0", "id": 1, "result": {"item": {
                "label": "[B]S01E02[/B]", "file": "/media/e.mkv", "type": "episode"}}}
            return self._send(200, json.dumps(payload).encode())
        if method == "GUI.GetProperties":
            payload = {"jsonrpc": "2.0", "id": 1, "result": {
                "fullscreen": True, "currentwindow": {"id": 12005}}}
            return self._send(200, json.dumps(payload).encode())
        if method == "Application.GetProperties":
            payload = {"jsonrpc": "2.0", "id": 1, "result": {
                "volume": 55, "muted": False}}
            return self._send(200, json.dumps(payload).encode())
        if method == "Player.GetProperties":
            payload = {"jsonrpc": "2.0", "id": 1, "result": {
                "percentage": 42.5, "speed": 1, "position": 2,
                "time": {"hours": 0, "minutes": 9, "seconds": 23},
                "totaltime": {"hours": 0, "minutes": 49, "seconds": 15}}}
            return self._send(200, json.dumps(payload).encode())
        if method == "Playlist.GetItems":
            payload = {"jsonrpc": "2.0", "id": 1, "result": {"items": [
                {"label": "Queued A", "file": "/m/a.mkv", "runtime": 300},
                {"label": "Queued B", "file": "/m/b.mkv", "runtime": 600},
            ]}}
            return self._send(200, json.dumps(payload).encode())
        if method == "Input.SendText":
            SENT.append(request.get("params"))
            return self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1,
                                               "result": "OK"}).encode())
        return self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1,
                                          "result": "OK"}).encode())

    def do_GET(self):
        if not self.headers.get("Authorization"):
            return self._send(401)
        self._send(200, b"\x89PNG-not-really", "image/png")


def _serve():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# -- pure rules ------------------------------------------------------------

def test_markup():
    assert strip_kodi_markup("[B]My Subscriptions[/B]") == "My Subscriptions"
    assert strip_kodi_markup("[COLOR red]Heat[/COLOR]") == "Heat"
    assert strip_kodi_markup("[REC] Raw feed") == "[REC] Raw feed"
    assert strip_kodi_markup("") == ""
    assert strip_kodi_markup("  [I]Italic[/I]  ") == "Italic"


def test_video_addons_only():
    youtube = {"addonid": "plugin.video.youtube", "type": "xbmc.python.pluginsource",
               "name": "YouTube", "enabled": True}
    script = {"addonid": "script.pystone.benchmark", "type": "xbmc.python.pluginsource",
              "name": "Bench", "enabled": True}
    skin = {"addonid": "skin.estuary", "type": "xbmc.gui.skin", "name": "Estuary",
            "enabled": True}
    repo = {"addonid": "repository.kodi.yatse.tv", "type": "xbmc.addon.repository",
            "name": "Yatse repo", "enabled": True}
    assert Addon(youtube).is_video_addon is True
    assert Addon(script).is_video_addon is False, "a plugin source is not a video add-on"
    assert Addon(skin).is_video_addon is False
    assert Addon(repo).is_video_addon is False
    assert kodi.is_video_addon(youtube) is True
    assert Addon(youtube).browse_path == "plugin://plugin.video.youtube/"
    assert Addon(youtube).display_name == "YouTube"
    assert Addon({"addonid": "plugin.video.x", "type": "xbmc.python.pluginsource",
                  "enabled": False}).detail() == "disabled"


def test_artwork_url():
    host = Host("10.0.0.5", 8080)
    assert artwork_url("https://cdn/p.jpg", host) == "https://cdn/p.jpg"
    # Measured: the host 404s `/image/image://…` and the trailing slash, and
    # serves `/image/%2fstorage%2f…` — so the scheme and the slash are stripped.
    # One rule: normalise to image:// with an encoded inner part, then quote the
    # whole URI once. Every shortcut 404s on the box (measured).
    assert (artwork_url("image://%2fstorage%2f.kodi%2ficon.png/", host)
            == "http://10.0.0.5:8080/image/image%3A%2F%2F%252fstorage%252f.kodi%252ficon.png")
    assert (artwork_url("image://special%3a%2f%2fhome%2ficon.png", host)
            == "http://10.0.0.5:8080/image/image%3A%2F%2Fspecial%253a%252f%252fhome%252ficon.png")
    assert (artwork_url("special://home/addons/x/icon.png", host)
            == "http://10.0.0.5:8080/image/image%3A%2F%2Fspecial%253A%252F%252Fhome%252Faddons%252Fx%252Ficon.png")
    assert artwork_url("", host) is None
    assert artwork_url("image://x.png", None) is None


def test_property_lists():
    # Output-only fields: naming them rejects the whole call (-32602) and reads
    # like a missing plugin.
    assert "filetype" not in kodi.DIRECTORY_PROPERTIES
    assert "label" not in kodi.DIRECTORY_PROPERTIES
    # Player.GetItem refuses `id` and never returns one.
    assert "id" not in kodi.PLAYER_ITEM_PROPERTIES


def test_clock_and_state():
    fake = type("Fake", (), {
        "active_players": lambda self: [{"playerid": 1, "type": "video"}],
        "player_item": lambda self, pid: Entry({"label": "[B]Title[/B]", "file": "/x"}),
        "_call": lambda self, method, params=None: {"fullscreen": True,
                                                    "currentwindow": {"id": 12005}},
        "player_properties": lambda self, pid: {"percentage": 25.0, "speed": 0,
                                                "time": {"seconds": 65},
                                                "totaltime": {"seconds": 3725}},
    })()
    state = kodi.playback_state(fake)
    assert state["title"] == "Title", state
    assert state["time"] == "1:05" and state["total"] == "1:02:05", state
    assert state["percentage"] == 25.0
    assert state["fullscreen_video"] is True
    idle = type("Idle", (), {"active_players": lambda self: []})()
    assert kodi.playback_state(idle) is None


def test_entry_rules():
    folder = Entry({"label": "x", "file": "plugin://x/", "filetype": "directory"})
    playable = Entry({"label": "y", "file": "plugin://x/play", "filetype": "unknown"})
    assert folder.is_folder is True and playable.is_folder is False
    assert Entry({"label": "L"}).heading == "L"
    assert Entry({"label": "L", "title": "T"}).heading == "T"
    assert Entry({"label": "l", "thumbnail": "t.png"}).art_url == "t.png"
    assert Entry({"label": "l", "thumbnail": "t.png",
                  "art": {"poster": "p.png"}}).art_url == "p.png"


# -- config ----------------------------------------------------------------

def test_a_config_from_the_old_name_is_adopted_once():
    """The rename must not cost the user their host. Read once from the old path,
    written to the new one 0600, and the old file is left alone."""
    with tempfile.TemporaryDirectory() as tmp:
        original, original_legacy = kodi.CONFIG_PATH, kodi.LEGACY_CONFIG_PATH
        kodi.CONFIG_PATH = os.path.join(tmp, "remokon", "config.json")
        kodi.LEGACY_CONFIG_PATH = os.path.join(tmp, "yatse-remote", "config.json")
        try:
            assert kodi.load_config() is None, "a host was invented from nothing"

            os.makedirs(os.path.dirname(kodi.LEGACY_CONFIG_PATH), mode=0o700)
            with open(kodi.LEGACY_CONFIG_PATH, "w", encoding="utf-8") as handle:
                json.dump({"address": "10.0.0.9", "port": 8080, "view": "wide"}, handle)

            data = kodi.load_config()
            assert data["address"] == "10.0.0.9", data
            assert os.path.exists(kodi.CONFIG_PATH), "the old config was not adopted"
            assert stat.S_IMODE(os.stat(kodi.CONFIG_PATH).st_mode) == 0o600
            assert os.path.exists(kodi.LEGACY_CONFIG_PATH), "adoption deleted the old file"

            # From here the new file is the source, even with the old one gone.
            os.unlink(kodi.LEGACY_CONFIG_PATH)
            assert kodi.load_config()["address"] == "10.0.0.9"
            assert kodi.view_mode() == "wide", "the adopted file lost its view"
        finally:
            kodi.CONFIG_PATH = original
            kodi.LEGACY_CONFIG_PATH = original_legacy

def test_config_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        original, original_legacy = kodi.CONFIG_PATH, kodi.LEGACY_CONFIG_PATH
        kodi.CONFIG_PATH = os.path.join(tmp, "remokon", "config.json")
        kodi.LEGACY_CONFIG_PATH = os.path.join(tmp, "legacy.json")
        try:
            assert kodi.load_config() is None
            host = Host("10.0.0.5", 8080, "kodi", "secret")
            kodi.save_config(host)
            mode = stat.S_IMODE(os.stat(kodi.CONFIG_PATH).st_mode)
            assert mode == 0o600, oct(mode)
            kodi.save_view_mode("rows")
            assert kodi.view_mode() == "rows"
            kodi.save_view_mode("nonsense")
            assert kodi.view_mode() == "poster", "an unknown mode falls back, never crashes"
            kodi.save_view_mode("wide")
            assert kodi.view_mode() == "wide", "every view name round-trips"
            kodi.save_view_zoom(2)
            assert kodi.view_zoom() == 2
            kodi.save_view_zoom(9)
            assert kodi.view_zoom() == 1
            loaded = kodi.host_from_config(kodi.load_config())
            assert (loaded.address, loaded.port, loaded.username, loaded.password) == (
                "10.0.0.5", 8080, "kodi", "secret")
        finally:
            kodi.CONFIG_PATH = original
            kodi.LEGACY_CONFIG_PATH = original_legacy


def test_favorites():
    # Pure rules: identity is the place, so a renamed folder is the same favourite.
    a = {"kind": "directory", "path": "plugin://x/", "title": "A", "art": "", "thumbnail": ""}
    b = {"kind": "directory", "path": "plugin://x/", "title": "A renamed", "art": "",
         "thumbnail": ""}
    media = {"kind": "media", "path": "plugin://x/play", "title": "Clip", "art": "", "thumbnail": ""}
    assert kodi.favorite_id("directory", "plugin://x/") == "directory:plugin://x/"
    saved = kodi.toggle_favorite([], a)
    assert len(saved) == 1 and kodi.is_favorite(saved, "directory", "plugin://x/")
    replaced = kodi.toggle_favorite(saved, b)
    assert len(replaced) == 1 and replaced[0]["title"] == "A renamed", replaced
    assert kodi.is_favorite(replaced, "media", "plugin://x/play") is False
    both = kodi.toggle_favorite(replaced, media)
    assert len(both) == 2
    assert len(kodi.remove_favorite(both, "directory", "plugin://x/")) == 1
    assert kodi.remove_favorite(both, "media", "nope") == both

    # Builders: a folder is a place, a playable entry is an item, an add-on is its root.
    addon = Addon({"addonid": "plugin.video.youtube", "type": "xbmc.python.pluginsource",
                   "name": "YouTube", "enabled": True, "thumbnail": "image://i.png/"})
    assert kodi.favorite_from_addon(addon)["path"] == "plugin://plugin.video.youtube/"
    folder = Entry({"label": "Season 1", "file": "plugin://x/s1", "filetype": "directory"})
    clip = Entry({"label": "E01", "file": "plugin://x/s1/e1", "filetype": "file",
                  "thumbnail": "image://t.jpg/"})
    assert kodi.favorite_from_entry(folder)["kind"] == "directory"
    assert kodi.favorite_from_entry(clip)["kind"] == "media"
    assert kodi.favorite_from_entry(clip)["path"] == "plugin://x/s1/e1"

    # A playing item with nothing to replay is not a favourite.
    assert kodi.favorite_from_playback(None) is None
    assert kodi.favorite_from_playback({"file": "", "title": "x"}) is None
    assert kodi.favorite_from_playback({"file": "/m/e.mkv", "title": ""}) is None
    playing = kodi.favorite_from_playback({"file": "/m/e.mkv", "title": "E01",
                                           "art": "image://a.jpg/"})
    assert playing["kind"] == "media" and playing["path"] == "/m/e.mkv"

    # Persistence, including a list that no longer makes sense.
    with tempfile.TemporaryDirectory() as tmp:
        original, original_legacy = kodi.CONFIG_PATH, kodi.LEGACY_CONFIG_PATH
        kodi.CONFIG_PATH = os.path.join(tmp, "remokon", "config.json")
        kodi.LEGACY_CONFIG_PATH = os.path.join(tmp, "legacy.json")
        try:
            assert kodi.load_favorites() == []
            kodi.save_config(Host("10.0.0.5", 8080, "kodi", "secret"))
            kodi.save_favorites([playing])
            assert kodi.load_favorites() == [playing]
            assert stat.S_IMODE(os.stat(kodi.CONFIG_PATH).st_mode) == 0o600
            # The host details survive a favourites write.
            assert kodi.host_from_config(kodi.load_config()).address == "10.0.0.5"
            kodi._write_config({"address": "10.0.0.5", "favorites": "not a list"})
            assert kodi.load_favorites() == [], "garbage costs favourites, not the launch"
            kodi._write_config({"favorites": [{"kind": "nonsense", "path": "x"},
                                              {"kind": "media", "path": ""},
                                              {"kind": "media", "path": "/ok"}],
                                "address": "10.0.0.5"})
            assert [f["path"] for f in kodi.load_favorites()] == ["/ok"]
        finally:
            kodi.CONFIG_PATH = original
            kodi.LEGACY_CONFIG_PATH = original_legacy


# -- transport -------------------------------------------------------------

def test_text_input_windows():
    """A keyboard dialog is recognisable, and nothing else looks like one."""
    for window_id in (10103, 10156):        # WINDOW_DIALOG_KEYBOARD, ..._TOUCH
        assert kodi.is_text_input_window({"id": window_id}), window_id
    for other in ({"id": 10000}, {"id": 12005}, {"id": 10109}, {"id": 10032}, {}, None):
        assert not kodi.is_text_input_window(other), other


def test_transport():
    server = _serve()
    port = server.server_address[1]
    try:
        authed = KodiClient(Host("127.0.0.1", port, "kodi", "pw"))
        assert authed.ping() is True

        # A 401 has an EMPTY body: it must read as a refusal, not as success.
        anon = KodiClient(Host("127.0.0.1", port))
        try:
            anon.ping()
            raise AssertionError("an unauthenticated call must not look successful")
        except KodiError as error:
            assert "401" in str(error), error

        try:
            authed._call("Boom")
            raise AssertionError("an RPC error must raise")
        except KodiError as error:
            assert error.code == -32602 and "Invalid params" in str(error), error

        entries = authed.directory("plugin://x/")
        assert [e.is_folder for e in entries] == [True, False, False]
        assert entries[0].heading == "My Subscriptions"
        assert entries[2].heading == "[REC] Raw feed"
        assert entries[1].art_url == "https://cdn/p.jpg"

        assert authed.image("http://127.0.0.1:%d/image/x" % port).startswith(b"\x89PNG")
        assert kodi.playback_state(authed)["title"] == "S01E02"

        # The current window is one call, and the fake host is on the fullscreen
        # video window — a window, not a keyboard dialog.
        assert kodi.current_window(authed) == {"id": 12005}
        assert not kodi.is_text_input_window(kodi.current_window(authed))

        # Typing goes out as Input.SendText with done, which is the box's Enter.
        SENT.clear()
        authed.send_text("the wire")
        assert SENT == [{"text": "the wire", "done": True}], SENT

        dead = KodiClient(Host("127.0.0.1", 1))
        try:
            dead.ping()
            raise AssertionError("an unreachable host must raise")
        except KodiError as error:
            assert "Cannot reach" in str(error), error
    finally:
        server.shutdown()


def test_playlist_calls():
    """The queue is the box's playlist, one call per button.

    Positions are the whole risk here: `Playlist.Remove` takes `position`,
    `Playlist.Swap` takes `position1`/`position2`, and a wrong key moves or deletes
    the wrong item without complaint.
    """
    server = _serve()
    port = server.server_address[1]
    try:
        client = KodiClient(Host("127.0.0.1", port, "kodi", "pw"))
        items = client.playlist_items()
        assert [item.heading for item in items] == ["Queued A", "Queued B"], items
        assert items[0].file == "/m/a.mkv" and items[0].runtime == 300

        CALLS.clear()
        client.playlist_add("/m/c.mkv")
        client.playlist_swap(1, 2)
        client.playlist_remove(3)
        client.playlist_open(2)
        client.playlist_clear()
        client.player_goto(1, "next")
        client.set_volume(35)
        assert CALLS == [
            ("Playlist.Add", {"playlistid": 1, "item": {"file": "/m/c.mkv"}}),
            ("Playlist.Swap", {"playlistid": 1, "position1": 1, "position2": 2}),
            ("Playlist.Remove", {"playlistid": 1, "position": 3}),
            ("Player.Open", {"item": {"playlistid": 1, "position": 2}}),
            ("Playlist.Clear", {"playlistid": 1}),
            ("Player.GoTo", {"playerid": 1, "to": "next"}),
            ("Application.SetVolume", {"volume": 35}),
        ], CALLS

        # The state carries what the queue panel marks; volume comes from the one call
        # Kodi 21.3 actually accepts it on.
        state = kodi.playback_state(client)
        assert state["position"] == 2, state
        assert state["volume"] is None, "playback_state must not claim a volume it did not ask for"
        assert kodi.volume_state(client) == {"volume": 55, "muted": False}
    finally:
        server.shutdown()


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
