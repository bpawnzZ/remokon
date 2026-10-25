"""Kodi JSON-RPC for the desktop client.

Stdlib only: `urllib` for transport, `threading` used by the caller. Every rule here
was measured against a live Kodi 21.3 (JSON-RPC API 13.5.0); the comments say what
was observed, because several of these calls look like they should work and do not.

Run `python3 test_kodi.py` for the checks, or `python3 kodi.py --probe` to talk to
the host named in the config.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PORT = 8080
TIMEOUT = 8.0
# A plugin browse is not a local read: YouTube's add-on calls out to YouTube, and
# 8 s timed out on the live box while the same host answered JSON-RPC instantly.
DIRECTORY_TIMEOUT = 30.0

APP_NAME = "remokon"


def _config_dir() -> str:
    """Where this app keeps its settings — never the source tree.

    XDG_CONFIG_HOME first: a public app should not assume one machine's home layout,
    and the tests get a throwaway directory for free.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


CONFIG_PATH = os.path.join(_config_dir(), "config.json")

# What this file was called before the project had a name of its own. Adopted once,
# see load_config(); the old file is left where it is.
LEGACY_CONFIG_PATH = os.path.expanduser("~/.config/yatse-remote/config.json")

# `filetype` and `label` are OUTPUT-ONLY: naming either in `properties` makes Kodi
# reject the whole call with -32602, which reads like a missing plugin rather than
# a bad request. Measured on the live box, field by field.
DIRECTORY_PROPERTIES = ["title", "file", "thumbnail", "art", "runtime"]
ADDON_PROPERTIES = ["name", "version", "enabled", "summary", "description",
                    "thumbnail", "author", "broken"]
# `id` is refused by Player.GetItem (-32602) and never volunteered by it, so a
# player item is identified by `file`. Same list as the iOS client.
PLAYER_ITEM_PROPERTIES = ["title", "artist", "album", "thumbnail", "fanart",
                          "showtitle", "season", "episode", "runtime", "file", "art",
                          # The player page shows these beside the title. Both are
                          # ordinary video-details fields; an item without them simply
                          # omits them, and Kodi does not refuse the call.
                          "plot", "year"]
# `position` is the item's index in the playlist, which is how the queue panel knows
# which row is playing without a second call.
PLAYER_PROPERTIES = ["time", "totaltime", "percentage", "speed", "position"]
# Kodi's video playlist. Audio is 0, video is 1; pictures and mixed are 2 and 3.
PLAYLIST_ID = 1

# Add-on types whose content Kodi exposes as a browsable virtual path.
BROWSABLE_TYPES = {"xbmc.python.pluginsource", "xbmc.addon.video",
                   "xbmc.addon.audio", "xbmc.addon.image", "kodi.addon.game"}

# Windows that take typed text, from Kodi's own guilib/WindowIDs.h:
# WINDOW_DIALOG_KEYBOARD and WINDOW_DIALOG_KEYBOARD_TOUCH. They matter because
# `GUI.GetProperties`' `currentwindow.id` is GetActiveWindowOrDialog(), so a dialog
# reports itself while it is up — which is how a client notices the box wants text.
TEXT_INPUT_WINDOWS = {10103, 10156}


class KodiError(Exception):
    """A refusal from the host, or a transport failure. Carries the RPC code."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class Host:
    """Where the box lives and how to authenticate to it."""

    def __init__(self, address: str, port: int = DEFAULT_PORT, username: str = "",
                 password: str = "", scheme: str = "http"):
        self.address = address.strip()
        self.port = int(port or DEFAULT_PORT)
        self.username = username
        self.password = password
        self.scheme = scheme or "http"

    @property
    def base(self) -> str:
        return f"{self.scheme}://{self.address}:{self.port}"

    @property
    def rpc_url(self) -> str:
        return self.base + "/jsonrpc"

    @property
    def auth_header(self) -> str | None:
        if not self.username:
            return None
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return "Basic " + token

    def as_dict(self) -> dict:
        return {"address": self.address, "port": self.port, "username": self.username,
                "password": self.password, "scheme": self.scheme}


class Addon:
    __slots__ = ("addonid", "type", "name", "version", "thumbnail", "enabled", "broken")

    def __init__(self, raw: dict):
        self.addonid = str(raw.get("addonid") or "")
        self.type = str(raw.get("type") or "")
        self.name = str(raw.get("name") or "")
        self.version = str(raw.get("version") or "")
        self.thumbnail = str(raw.get("thumbnail") or "")
        self.enabled = bool(raw.get("enabled", False))
        self.broken = bool(raw.get("broken", False))

    @property
    def display_name(self) -> str:
        return strip_kodi_markup(self.name or self.addonid)

    @property
    def browse_path(self) -> str:
        return f"plugin://{self.addonid}/"

    @property
    def is_browsable(self) -> bool:
        return self.type in BROWSABLE_TYPES

    @property
    def is_video_addon(self) -> bool:
        """`plugin.video.*` AND browsable.

        Measured on the live box: all 68 installed add-ons answer
        `xbmc.python.pluginsource`, and `xbmc.addon.video` occurs zero times — so
        filtering on the type lists nothing, silently. `script.pystone.benchmark`
        is the counter-example: a plugin source that is not a video add-on.
        """
        return self.is_browsable and self.addonid.startswith("plugin.video.")

    def detail(self) -> str:
        parts = [self.version] if self.version else []
        if not self.enabled:
            parts.append("disabled")
        if self.broken:
            parts.append("broken")
        return " · ".join(parts)


class Entry:
    """One line of `Files.GetDirectory` — a folder or something playable.

    Also what `Playlist.GetItems` returns, which is why the queue panel reuses it
    instead of inventing a second reader for the same fields.
    """

    __slots__ = ("label", "file", "filetype", "thumbnail", "art", "runtime", "title",
                 "plot", "year")

    def __init__(self, raw: dict):
        self.label = strip_kodi_markup(str(raw.get("label") or ""))
        self.title = strip_kodi_markup(str(raw.get("title") or ""))
        self.file = str(raw.get("file") or "")
        self.filetype = str(raw.get("filetype") or "")
        art = raw.get("art")
        self.art = art if isinstance(art, dict) else {}
        self.runtime = raw.get("runtime") or 0
        self.thumbnail = str(raw.get("thumbnail") or "")
        self.plot = strip_kodi_markup(str(raw.get("plot") or ""))
        self.year = str(raw.get("year") or "")

    @property
    def heading(self) -> str:
        return self.title or self.label or self.file

    @property
    def is_folder(self) -> bool:
        return self.filetype == "directory"

    @property
    def art_url(self) -> str:
        """Poster first, then the plain thumbnail — the order the library uses."""
        return self.art.get("poster") or self.art.get("thumb") or self.thumbnail or ""


class KodiClient:
    """One host. Safe to call from worker threads: each call opens its own request."""

    def __init__(self, host: Host):
        self.host = host
        self._next_id = 0

    # -- transport ---------------------------------------------------------

    def _call(self, method: str, params: dict | None = None, timeout: float | None = None):
        self._next_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": self._next_id,
                              "method": method, "params": params or {}}).encode()
        request = urllib.request.Request(self.host.rpc_url, data=payload, method="POST")
        request.add_header("Content-Type", "application/json")
        if auth := self.host.auth_header:
            request.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(request, timeout=timeout or TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            # Kodi answers 401 with an EMPTY body, which parses as {} and looks
            # like a success. Report the status, never the parsed body.
            raise KodiError(f"The host refused the request (HTTP {error.code}).",
                            error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise KodiError(f"Cannot reach {self.host.base} ({error}).") from None
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise KodiError("The host answered with something that is not JSON.") from None
        if "error" in body:
            rpc = body["error"] or {}
            raise KodiError(str(rpc.get("message") or "The host refused the request."),
                            rpc.get("code"))
        return body.get("result")

    def _list(self, method: str, params: dict, key: str, timeout: float | None = None) -> list:
        result = self._call(method, params, timeout=timeout) or {}
        return result.get(key) or []

    # -- reads -------------------------------------------------------------

    def ping(self) -> bool:
        return self._call("JSONRPC.Ping") == "pong"

    def addons(self) -> list[Addon]:
        raw = self._list("Addons.GetAddons", {"properties": ADDON_PROPERTIES}, "addons")
        return [Addon(item) for item in raw]

    def video_addons(self) -> list[Addon]:
        return sorted((a for a in self.addons() if a.is_video_addon),
                      key=lambda a: a.display_name.casefold())

    def directory(self, path: str, media: str = "files") -> list[Entry]:
        raw = self._list("Files.GetDirectory",
                         {"directory": path, "media": media,
                          "properties": DIRECTORY_PROPERTIES},
                         "files", timeout=DIRECTORY_TIMEOUT)
        return [Entry(item) for item in raw]

    def active_players(self) -> list[dict]:
        return self._call("Player.GetActivePlayers") or []

    def player_item(self, playerid: int) -> Entry | None:
        result = self._call("Player.GetItem",
                            {"playerid": playerid, "properties": PLAYER_ITEM_PROPERTIES}) or {}
        item = result.get("item")
        return Entry(item) if item else None

    def player_properties(self, playerid: int) -> dict:
        return self._call("Player.GetProperties",
                          {"playerid": playerid, "properties": PLAYER_PROPERTIES}) or {}

    def image(self, url: str) -> bytes:
        """Fetch artwork through the host's own `/image/` endpoint (auth required)."""
        request = urllib.request.Request(url)
        if auth := self.host.auth_header:
            request.add_header("Authorization", auth)
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()

    # -- writes ------------------------------------------------------------

    def open_item(self, file: str) -> None:
        self._call("Player.Open", {"item": {"file": file}})

    def play_pause(self, playerid: int) -> str:
        result = self._call("Player.PlayPause", {"playerid": playerid}) or {}
        return str(result.get("speed", ""))

    def stop(self, playerid: int) -> None:
        self._call("Player.Stop", {"playerid": playerid})

    def action(self, action: str) -> None:
        """One `Input.Action` value (215 lowercase names — `Input.ExecuteAction`
        rejects anything else with -32602). `home` is not one of them."""
        self._call("Input.ExecuteAction", {"action": action})

    def home(self) -> None:
        """The only way back to the box's home screen: `Input.Home` (`home` is not
        in the action enum, and `GUI.ActivateWindow {window: Home}` is refused)."""
        self._call("Input.Home")

    def send_text(self, text: str, done: bool = True) -> None:
        """Type into the box's open keyboard dialog. `done` closes it, which is what
        pressing Enter on a real keyboard does; with no dialog up the same call sets
        the text of whatever control has focus."""
        self._call("Input.SendText", {"text": text, "done": done})

    def seek(self, playerid: int, seconds: float) -> None:
        """Absolute seek, as a full `time` object.

        Measured on Kodi 21.3: `{"time": {"seconds": 600}}` is refused with -32602 —
        the `Time` type wants hours/minutes/seconds together, and a partial object is
        an invalid param, not a shorthand. `{"percentage": p}` works, and a bare
        `{"seconds": n}` works but means "jump n seconds from here", which is a
        different thing. So the target is decomposed, and this is the only place any
        caller has to get it right.
        """
        total = int(max(0, seconds))
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        self._call("Player.Seek",
                   {"playerid": playerid,
                    "value": {"time": {"hours": hours, "minutes": minutes, "seconds": secs}}})

    def player_goto(self, playerid: int, to: str) -> None:
        """`previous` / `next` within the playlist; `to` may also be a position."""
        self._call("Player.GoTo", {"playerid": playerid, "to": to})

    def set_volume(self, percent: int) -> None:
        self._call("Application.SetVolume", {"volume": int(percent)})

    # -- the video playlist ------------------------------------------------

    def playlist_items(self) -> list[Entry]:
        raw = self._list("Playlist.GetItems",
                         {"playlistid": PLAYLIST_ID,
                          "properties": ["title", "file", "art", "thumbnail", "runtime"]},
                         "items")
        return [Entry(item) for item in raw]

    def playlist_add(self, file: str) -> None:
        self._call("Playlist.Add",
                   {"playlistid": PLAYLIST_ID, "item": {"file": file}})

    def playlist_remove(self, position: int) -> None:
        self._call("Playlist.Remove",
                   {"playlistid": PLAYLIST_ID, "position": int(position)})

    def playlist_swap(self, first: int, second: int) -> None:
        """Two positions, not a move: Kodi swaps them, so moving down one step is
        swap(n, n+1) and moving up is swap(n, n-1)."""
        self._call("Playlist.Swap",
                   {"playlistid": PLAYLIST_ID, "position1": int(first),
                    "position2": int(second)})

    def playlist_open(self, position: int) -> None:
        """Play that slot now — the playlist is opened AT the position, so the
        items before it are skipped rather than dropped."""
        self._call("Player.Open", {"item": {"playlistid": PLAYLIST_ID,
                                            "position": int(position)}})

    def playlist_clear(self) -> None:
        self._call("Playlist.Clear", {"playlistid": PLAYLIST_ID})


def playback_state(client: KodiClient) -> dict | None:
    """What is playing right now, or None. Two round trips, used by the 1 Hz poll."""
    players = client.active_players()
    if not players:
        return None
    playerid = players[0].get("playerid", 0)
    try:
        item = client.player_item(playerid)
        props = client.player_properties(playerid)
    except KodiError:
        # The item ended between GetActivePlayers and these two calls, which Kodi
        # answers with "Failed to execute method" for a player that is already gone.
        # Report "nothing playing": raising here fails the whole 1 Hz poll, and the
        # last snapshot then stays on screen for ever — a finished video that never
        # clears itself.
        return None
    # Which window has focus decides what an arrow key means: in the fullscreen
    # video window Kodi binds left/right to StepBack/StepForward, and a client that
    # sends the navigation *actions* bypasses that binding (measured on iOS).
    try:
        gui = client._call("GUI.GetProperties", {"properties": ["fullscreen", "currentwindow"]})
    except KodiError:
        gui = {}
    gui = gui if isinstance(gui, dict) else {}  # a host that answers "OK" must not kill the poll
    window = gui.get("currentwindow") or {}
    elapsed = _hms(props.get("time"))
    total = _hms(props.get("totaltime"))
    return {"playerid": playerid,
            "title": item.heading if item else "Playing",
            "file": item.file if item else "",
            "art": item.art_url if item else "",
            "plot": item.plot if item else "",
            "year": item.year if item else "",
            "percentage": float(props.get("percentage") or 0.0),
            "speed": int(props.get("speed") or 0),
            "position": int(props.get("position") or 0),
            # Filled by `volume_state()` only on the page that shows a volume row.
            "volume": None,
            "muted": False,
            "time": _clock(elapsed),
            "total": _clock(total),
            "elapsed_seconds": elapsed,
            "total_seconds": total,
            "fullscreen_video": bool(gui.get("fullscreen")) and window.get("id") == 12005}


def volume_state(client: KodiClient) -> dict:
    """`Application.GetProperties`, NOT `GUI.GetProperties`.

    Measured on Kodi 21.3: `GUI.GetProperties ["volume"]` is refused with -32602 and
    takes the whole call with it — the same failure shape as naming `filetype` in a
    directory listing. This one is a separate call, so it is asked for only while the
    player page is on screen.
    """
    result = client._call("Application.GetProperties", {"properties": ["volume", "muted"]})
    if not isinstance(result, dict):
        return {}
    return {"volume": result.get("volume"), "muted": bool(result.get("muted"))}


def current_window(client: KodiClient) -> dict:
    """The window or dialog the box has focused — `{"id": …, "label": …}`.

    One call, and the only signal Kodi offers that a keyboard dialog is up: there is
    no notification for it, and `Input.SendText` reads nothing back. `id` is
    `GetActiveWindowOrDialog()`, so dialogs report themselves (12005 fullscreen video
    is already trusted from this same property).
    """
    gui = client._call("GUI.GetProperties", {"properties": ["currentwindow"]}) or {}
    if not isinstance(gui, dict):  # a host that answers "OK" must not kill the poll
        return {}
    return gui.get("currentwindow") or {}


def is_text_input_window(window: dict | None) -> bool:
    return bool(window) and window.get("id") in TEXT_INPUT_WINDOWS


def _hms(value) -> int:
    """Kodi sends {"hours":0,"minutes":9,"seconds":23} — reading only `seconds`
    reports 0:23 for a nine-minute position, which is what made every time label
    (and the seek bar range) wrong."""
    if isinstance(value, dict):
        return (int(value.get("hours") or 0) * 3600
                + int(value.get("minutes") or 0) * 60
                + int(value.get("seconds") or 0))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _clock(seconds) -> str:
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# Public alias: the player previews a seek target's label while the handle is held.
clock = _clock


# -- pure rules ------------------------------------------------------------

# The tag list the iOS client measured against real plugin output. Deliberately
# not "any [word]" — `[REC]` is a label, not markup.
_MARKUP = re.compile(r"\[/?(?:B|I|U|S|CR|COLOR|LIGHT|LOWERCASE|UPPERCASE|CAPITALIZE"
                     r"|FONT|SIZE|URL|IMG)[^\]]*\]", re.IGNORECASE)


def strip_kodi_markup(text: str) -> str:
    return _MARKUP.sub("", text or "").strip()


def is_video_addon(raw: dict) -> bool:
    return Addon(raw).is_video_addon


def artwork_url(value: str, host: Host | None) -> str | None:
    """A URL the host's `/image/` endpoint will actually serve.

    Measured on the box, one rule covers every shape it hands out: **normalise the
    value to an `image://` URI whose inner part is percent-encoded, then quote the
    whole URI once more** — which is exactly what `Files.PrepareDownload` returns.

    - `image://%2fstorage%2f…/icon.png/` → `image%3a%2f%2f%252fstorage%252f…` (14,820 B)
    - `image://special%3a%2f%2fhome%2f…` (what plugin add-ons send) → 35,947 B
    - `special://home/addons/…` → wrapped first, then the same treatment

    The shortcuts that look obvious all 404: `/image/image://…` (scheme kept raw),
    the value with its trailing slash, and a `special://` file path sent bare in any
    encoding. Absolute URLs are fetched directly and need none of this.
    """
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    if host is None:
        return None
    path = value.rstrip("/")
    if path.startswith("image://"):
        inner = path[len("image://"):]
        if "%" not in inner:
            inner = urllib.parse.quote(inner, safe="")
        uri = "image://" + inner
    else:
        uri = "image://" + urllib.parse.quote(path, safe="")
    return f"{host.base}/image/{urllib.parse.quote(uri, safe='')}"


def _read_config(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_config() -> dict | None:
    data = _read_config(CONFIG_PATH)
    if not data and not os.path.exists(CONFIG_PATH):
        # Adopt the file this app wrote before it had a name of its own, once, so a
        # rename does not cost the user their host and favourites. The old file stays.
        legacy = _read_config(LEGACY_CONFIG_PATH)
        if legacy:
            _write_config(legacy)
            data = legacy
    return data if data and data.get("address") else None


def _write_config(data: dict) -> None:
    """0600 from the first byte: the file can hold the host's password."""
    directory = os.path.dirname(CONFIG_PATH)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def save_config(host: Host) -> None:
    data = load_config() or {}
    data.update(host.as_dict())
    _write_config(data)


VIEW_MODES = ("poster", "compact", "wide", "rows")


def view_mode() -> str:
    """Which layout the browser opens in, kept beside the host."""
    mode = (load_config() or {}).get("view")
    if mode == "cards":  # the name this setting had before there were views
        return "poster"
    return mode if mode in VIEW_MODES else "poster"


def view_zoom() -> int:
    """0 small, 1 normal, 2 large — the tile size the grid is built from."""
    zoom = (load_config() or {}).get("zoom")
    return zoom if zoom in (0, 1, 2) else 1


def save_view_zoom(zoom: int) -> None:
    data = load_config() or {}
    data["zoom"] = zoom if zoom in (0, 1, 2) else 1
    _write_config(data)


def save_view_mode(mode: str) -> None:
    data = load_config() or {}
    data["view"] = mode if mode in VIEW_MODES else "poster"
    _write_config(data)


def host_from_config(data: dict) -> Host:
    return Host(str(data.get("address", "")), data.get("port", DEFAULT_PORT),
                str(data.get("username", "")), str(data.get("password", "")),
                str(data.get("scheme", "http")))


# -- favourites ------------------------------------------------------------
#
# A favourite is a PLACE, not a label: `kind` says how it is reopened
# (`directory` → browse that path, `media` → play that file) and `path` is what the
# host was asked for. Identity is kind+path, so saving a renamed folder twice
# replaces the entry instead of adding a second one.

FAVORITE_KINDS = ("directory", "media")


def favorite_id(kind: str, path: str) -> str:
    return f"{kind}:{path}"


def load_favorites() -> list[dict]:
    """The saved places, or [] — a config that no longer parses costs favourites,
    never the launch."""
    raw = (load_config() or {}).get("favorites")
    if not isinstance(raw, list):
        return []
    favorites = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind, path = str(item.get("kind") or ""), str(item.get("path") or "")
        if kind not in FAVORITE_KINDS or not path:
            continue
        favorites.append({"kind": kind, "path": path,
                          "title": str(item.get("title") or path),
                          "art": str(item.get("art") or ""),
                          "thumbnail": str(item.get("thumbnail") or "")})
    return favorites


def save_favorites(favorites: list[dict]) -> None:
    data = load_config() or {}
    data["favorites"] = favorites
    _write_config(data)


def is_favorite(favorites: list[dict], kind: str, path: str) -> bool:
    wanted = favorite_id(kind, path)
    return any(favorite_id(f["kind"], f["path"]) == wanted for f in favorites)


def toggle_favorite(favorites: list[dict], favorite: dict) -> list[dict]:
    """Add the place, or replace the existing entry for it. Returns a new list."""
    wanted = favorite_id(favorite["kind"], favorite["path"])
    for index, existing in enumerate(favorites):
        if favorite_id(existing["kind"], existing["path"]) == wanted:
            updated = list(favorites)
            updated[index] = favorite
            return updated
    return list(favorites) + [favorite]


def remove_favorite(favorites: list[dict], kind: str, path: str) -> list[dict]:
    wanted = favorite_id(kind, path)
    return [f for f in favorites
            if favorite_id(f["kind"], f["path"]) != wanted]


def favorite_from_addon(addon: Addon) -> dict:
    """An add-on is saved as the plugin root it browses from."""
    return {"kind": "directory", "path": addon.browse_path,
            "title": addon.display_name, "art": "", "thumbnail": addon.thumbnail}


def favorite_from_entry(entry: Entry) -> dict:
    """A folder is saved as the place it opens; a playable entry as the file it plays."""
    return {"kind": "directory" if entry.is_folder else "media",
            "path": entry.file, "title": entry.heading,
            "art": entry.art_url, "thumbnail": entry.thumbnail}


def favorite_from_playback(state: dict | None) -> dict | None:
    """What is playing, saved as a replayable item. Both a file and a title are
    required: one without the other is a favourite that cannot be opened or named."""
    if not state:
        return None
    file = str(state.get("file") or "")
    title = str(state.get("title") or "")
    if not file or not title:
        return None
    return {"kind": "media", "path": file, "title": title,
            "art": str(state.get("art") or ""), "thumbnail": ""}


def main() -> int:
    """Read-only probe: connect with the saved config and print what the app sees."""
    data = load_config()
    if not data:
        print(f"No saved host at {CONFIG_PATH}.")
        return 1
    client = KodiClient(host_from_config(data))
    print("ping:", client.ping())
    addons = client.video_addons()
    print(f"video add-ons: {len(addons)}")
    for addon in addons[:8]:
        print(f"  {addon.addonid:34} {addon.display_name!r} {addon.detail()}")
    if addons:
        icon = artwork_url(addons[0].thumbnail, client.host)
        print("add-on icon:", icon)
        try:
            print("  bytes:", len(client.image(icon)))
        except Exception as error:  # noqa: BLE001 - probe reports, never dies
            print("  ERROR:", error)
    if addons:
        path = addons[0].browse_path
        print(f"browse {path}:")
        for entry in client.directory(path)[:8]:
            kind = "dir " if entry.is_folder else "play"
            print(f"  {kind} {entry.heading[:48]!r} art={entry.art_url[:40]}")
            url = artwork_url(entry.art_url, client.host)
            if url:
                try:
                    print(f"       image bytes: {len(client.image(url))}")
                except Exception as error:  # noqa: BLE001 - probe reports, never dies
                    print(f"       image error: {error}")
                break
    state = playback_state(client)
    print("playing:", state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
