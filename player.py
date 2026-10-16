#!/usr/bin/env python3
"""Small, URL-first mpv controller for the Omarchy YouTube Player plugin."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

PLUGIN_NAME = "omarchy-youtube-player"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
SOCKET_PATH = RUNTIME_DIR / f"{PLUGIN_NAME}.sock"
STATE_PATH = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / PLUGIN_NAME / "state.json"
VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
MAX_STATE_BYTES = 64 * 1024
MAX_PROCESS_BYTES = 256 * 1024
MAX_SOCKET_BYTES = 64 * 1024
MAX_URL_CHARS = 2048
MAX_THUMBNAIL_CHARS = 512
MAX_TITLE_CHARS = 180
MAX_CHANNEL_CHARS = 100
MAX_WORKSPACE_CHARS = 64
MAX_QUEUE_ITEMS = 50
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)


# Optional tunables. Defaults preserve the previous behaviour; every value can be
# overridden from the environment without editing the plugin.
MAX_RESULTS = _env_int("OMARCHY_YOUTUBE_MAX_RESULTS", 8, 1, 20)
PLAYER_WIDTH = _env_int("OMARCHY_YOUTUBE_WIDTH", 480, 160, 3840)
PLAYER_HEIGHT = _env_int("OMARCHY_YOUTUBE_HEIGHT", 270, 90, 2160)
PLAYER_X = _env_int("OMARCHY_YOUTUBE_X", 24, 0, 20000)
PLAYER_Y = _env_int("OMARCHY_YOUTUBE_Y", 64, 0, 20000)
# 0 means "leave the cache size to the user's mpv.conf".
PLAYER_CACHE_MB = _env_int("OMARCHY_YOUTUBE_CACHE_MB", 0, 0, 4096)
VIDEO_FORMAT = "bestvideo[height<=720]+bestaudio/best[height<=720]"
AUDIO_FORMAT = "bestaudio/best"


class ResponseLimitError(subprocess.SubprocessError):
    """A child process or socket exceeded its bounded response size."""


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "")[:limit]


def _safe_http_url(value: object, limit: int = MAX_URL_CHARS) -> str:
    text = _bounded_text(value, limit)
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _bounded_number(value: object, maximum: float = 604800.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return min(max(number, 0.0), maximum) if math.isfinite(number) else 0.0


def _t(es: str, en: str, lang: str) -> str:
    return es if lang == "es" else en


def valid_video_url(value: str) -> bool:
    if not isinstance(value, str) or len(value) > MAX_URL_CHARS:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return parsed.scheme in {"http", "https"} and host in {h.removeprefix('www.') for h in VIDEO_HOSTS}


def format_duration(seconds: object) -> str:
    try:
        value = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if value < 0:
        return ""
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _queue_items(value: object) -> list:
    items = []
    if not isinstance(value, list):
        return items
    for raw in value[:MAX_QUEUE_ITEMS]:
        if not isinstance(raw, dict):
            continue
        url = _safe_http_url(raw.get("url"))
        if not url:
            continue
        items.append({
            "url": url,
            "title": _bounded_text(raw.get("title"), MAX_TITLE_CHARS) or "YouTube video",
            "channel": _bounded_text(raw.get("channel"), MAX_CHANNEL_CHARS),
            "thumbnail": _safe_http_url(raw.get("thumbnail"), MAX_THUMBNAIL_CHARS),
        })
    return items


def _state_data(data: object) -> dict:
    if not isinstance(data, dict):
        return {}
    queue = _queue_items(data.get("queue"))
    try:
        index = int(data.get("queueIndex") or 0)
    except (TypeError, ValueError):
        index = 0
    index = min(max(index, 0), max(len(queue) - 1, 0))
    return {
        "url": _safe_http_url(data.get("url")),
        "title": _bounded_text(data.get("title"), MAX_TITLE_CHARS),
        "channel": _bounded_text(data.get("channel"), MAX_CHANNEL_CHARS),
        "thumbnail": _safe_http_url(data.get("thumbnail"), MAX_THUMBNAIL_CHARS),
        "audioOnly": bool(data.get("audioOnly")),
        "workspace": _bounded_text(data.get("workspace"), MAX_WORKSPACE_CHARS),
        "queue": queue,
        "queueIndex": index,
    }


def _ensure_state_dir() -> None:
    directory = STATE_PATH.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("state directory is not a private user-owned directory")
    if info.st_mode & 0o077:
        os.chmod(directory, 0o700)


def write_state(data: dict) -> None:
    _ensure_state_dir()
    payload = json.dumps(_state_data(data), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise ResponseLimitError("state exceeds its size limit")
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=STATE_PATH.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_PATH)
        temporary = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def read_state() -> dict:
    try:
        descriptor = os.open(
            STATE_PATH,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            info = os.fstat(descriptor)
            if (
                info.st_uid != os.getuid()
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > MAX_STATE_BYTES
            ):
                return {}
            payload = os.read(descriptor, MAX_STATE_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(payload) > MAX_STATE_BYTES:
            return {}
        return _state_data(json.loads(payload.decode("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def socket_is_alive() -> bool:
    try:
        socket_info = SOCKET_PATH.stat(follow_symlinks=False)
        if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.getuid():
            return False
    except OSError:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.4)
            client.connect(str(SOCKET_PATH))
        return True
    except OSError:
        return False


def _run_bounded(command: list[str], timeout: float = 4.0, max_bytes: int = MAX_PROCESS_BYTES) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    for stream in (process.stdout, process.stderr):
        assert stream is not None
        selector.register(stream, selectors.EVENT_READ)
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            for key, _ in selector.select(min(remaining, 0.25)):
                stream = key.fileobj
                chunk = os.read(stream.fileno(), min(65536, max_bytes - len(stdout) - len(stderr) + 1))
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if len(stdout) + len(stderr) + len(chunk) > max_bytes:
                    raise ResponseLimitError("process response exceeds its size limit")
                (stdout if stream is process.stdout else stderr).extend(chunk)
        return process.wait(timeout=max(0, deadline - time.monotonic())), bytes(stdout), bytes(stderr)
    except (OSError, subprocess.TimeoutExpired, ResponseLimitError):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        process.wait()
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _run_bounded_text(command: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    code, stdout, stderr = _run_bounded(command, timeout=timeout)
    return code, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


def _hyprctl_json(arguments: list[str]) -> dict | list:
    try:
        code, stdout, _ = _run_bounded_text(["hyprctl", *arguments])
        data = json.loads(stdout)
        return data if code == 0 and isinstance(data, (dict, list)) else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def active_window_address() -> str:
    active = _hyprctl_json(["activewindow", "-j"])
    return _bounded_text(active.get("address"), MAX_WORKSPACE_CHARS) if isinstance(active, dict) else ""


def normalize_window(restore_address: str = "") -> None:
    """Keep the player as PiP without leaving focus on it."""
    clients = _hyprctl_json(["clients", "-j"])
    if not isinstance(clients, list):
        return
    player = next((item for item in clients if isinstance(item, dict) and item.get("title") == "Omarchy YouTube"), None)
    if not player:
        return
    address = _bounded_text(player.get("address"), MAX_WORKSPACE_CHARS)
    active_address = active_window_address()
    size = player.get("size") or [PLAYER_WIDTH, PLAYER_HEIGHT]

    def dispatch(expression: str) -> None:
        subprocess.run(["hyprctl", "dispatch", expression], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4, check=False)

    if address and address != active_address:
        dispatch(f'''hl.dsp.focus({{ window = "address:{address}" }})''')
    width_delta = PLAYER_WIDTH - int(size[0])
    height_delta = PLAYER_HEIGHT - int(size[1])
    if width_delta or height_delta:
        dispatch(f"hl.dsp.window.resize({{ x = {width_delta}, y = {height_delta}, relative = true }})")
    dispatch(f"hl.dsp.window.move({{ x = {PLAYER_X}, y = {PLAYER_Y}, relative = false }})")
    if restore_address and restore_address != address:
        dispatch(f'''hl.dsp.focus({{ window = "address:{restore_address}" }})''')


def move_player_to_workspace(workspace: str) -> None:
    workspace = _bounded_text(workspace, MAX_WORKSPACE_CHARS)
    if not workspace:
        return
    clients = _hyprctl_json(["clients", "-j"])
    if not isinstance(clients, list):
        return
    active_address = active_window_address()
    player = next((item for item in clients if isinstance(item, dict) and item.get("title") == "Omarchy YouTube"), None)
    if not player or not player.get("address"):
        return
    address = _bounded_text(player["address"], MAX_WORKSPACE_CHARS)

    def dispatch(expression: str) -> None:
        subprocess.run(["hyprctl", "dispatch", expression], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4, check=False)

    if address != active_address:
        dispatch(f'''hl.dsp.focus({{ window = "address:{address}" }})''')
    dispatch(f'''hl.dsp.window.move({{ workspace = "{workspace}", follow = false }})''')
    if active_address and active_address != address:
        dispatch(f'''hl.dsp.focus({{ window = "address:{active_address}" }})''')


def player_workspace() -> str:
    clients = _hyprctl_json(["clients", "-j"])
    if not isinstance(clients, list):
        return ""
    player = next((item for item in clients if isinstance(item, dict) and item.get("title") == "Omarchy YouTube"), None)
    workspace = player.get("workspace") if player else None
    return _bounded_text(workspace.get("name"), MAX_WORKSPACE_CHARS) if isinstance(workspace, dict) else ""


_PLAYER_MODE_OVERRIDE: bool | None = None


def _audio_only_state() -> bool:
    state = read_state()
    # An empty/idle state always carries "audioOnly": false because of
    # normalization, which would wrongly force a windowed player. When nothing
    # is loaded, defer to the configured default instead.
    if not state.get("url") and not state.get("queue"):
        return _audio_only_default()
    return bool(state.get("audioOnly"))


def _desired_windowed() -> bool:
    if _PLAYER_MODE_OVERRIDE is not None:
        return _PLAYER_MODE_OVERRIDE
    return not _audio_only_state()


def _raw_get_property(name: str) -> object:
    try:
        return _send_ipc(["get_property", name], request_id=7)
    except (OSError, RuntimeError):
        return None


def _player_pids() -> list[int]:
    """PIDs of mpv processes that own this plugin's IPC socket."""
    marker = str(SOCKET_PATH).encode("utf-8")
    pids: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read()
        except OSError:
            continue
        argv0 = cmdline.split(b"\x00", 1)[0]
        if not (argv0 == b"mpv" or argv0.endswith(b"/mpv")):
            continue
        if marker in cmdline:
            pids.append(int(entry))
    return pids


def _vo_name(value: object) -> str:
    # mpv reports ``vo`` as a list of objects, e.g. [{"name": "null", ...}].
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return str(first.get("name", ""))
    if value is None:
        return ""
    return str(value)


def _running_windowed() -> bool | None:
    if not socket_is_alive():
        return None
    voi = _raw_get_property("vo")
    if voi is None:
        return None
    return _vo_name(voi) not in ("", "null")


def _stop_player() -> None:
    if socket_is_alive():
        try:
            _send_ipc(["quit"])
        except (OSError, RuntimeError):
            pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _player_pids():
            return
        time.sleep(0.05)
    for pid in _player_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.1)


def ensure_player() -> None:
    desired_windowed = _desired_windowed()
    alive = socket_is_alive()
    if alive:
        running_windowed = _running_windowed()
        if running_windowed == desired_windowed:
            if desired_windowed:
                normalize_window(active_window_address())
            return
        # Audio and video need different players (windowed vs headless), so tear
        # the current one down and relaunch it below.
        _stop_player()
    elif _player_pids():
        # An mpv is still running but its socket is unreachable; clear it first so
        # we never end up with two players fighting over the same socket.
        _stop_player()
    restore_address = active_window_address() if desired_windowed else ""
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if desired_windowed:
        command = [
            "mpv",
            "--no-terminal",
            "--no-resume-playback",
            "--save-position-on-quit=no",
            "--force-window=immediate",
            "--focus-on=never",
            "--force-window-position",
            "--ontop-level=system",
            "--player-operation-mode=pseudo-gui",
            "--title=Omarchy YouTube",
            "--input-ipc-server=" + str(SOCKET_PATH),
            "--geometry=" + f"{PLAYER_WIDTH}x{PLAYER_HEIGHT}+{PLAYER_X}+{PLAYER_Y}",
            "--autofit=" + f"{PLAYER_WIDTH}x{PLAYER_HEIGHT}",
            "--ytdl-format=" + VIDEO_FORMAT,
            "--idle=yes",
            "--keep-open=yes",
        ]
    else:
        # Headless: no window at all for audio-only playback.
        command = [
            "mpv",
            "--no-terminal",
            "--no-resume-playback",
            "--save-position-on-quit=no",
            "--no-video",
            "--vo=null",
            "--force-window=no",
            "--input-ipc-server=" + str(SOCKET_PATH),
            "--ytdl-format=" + AUDIO_FORMAT,
            "--idle=yes",
            "--keep-open=yes",
        ]
    if PLAYER_CACHE_MB > 0:
        command.append("--demuxer-max-bytes=" + f"{PLAYER_CACHE_MB}MiB")
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if socket_is_alive():
            if desired_windowed:
                normalize_window(restore_address)
            return
        time.sleep(0.05)
    raise RuntimeError("mpv did not open its IPC socket")


def ipc(command: list[object], request_id: int = 1) -> object:
    ensure_player()
    return _send_ipc(command, request_id)


def _send_ipc(command: list[object], request_id: int = 1) -> object:
    payload = (json.dumps({"command": command, "request_id": request_id}) + "\n").encode("utf-8")
    if len(payload) > MAX_SOCKET_BYTES:
        raise ResponseLimitError("IPC request exceeds its size limit")
    deadline = time.monotonic() + 4
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.4)
        client.connect(str(SOCKET_PATH))
        client.sendall(payload)
        response_bytes = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("mpv IPC response timed out")
            client.settimeout(min(0.4, remaining))
            try:
                chunk = client.recv(min(65536, MAX_SOCKET_BYTES - len(response_bytes) + 1))
            except socket.timeout:
                continue
            if not chunk:
                break
            if len(response_bytes) + len(chunk) > MAX_SOCKET_BYTES:
                raise ResponseLimitError("IPC response exceeds its size limit")
            response_bytes.extend(chunk)
            if b"\n" in chunk:
                break
    for line in bytes(response_bytes).splitlines():
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if response.get("request_id") == request_id:
            if response.get("error") != "success":
                raise RuntimeError(_bounded_text(response.get("error"), MAX_TITLE_CHARS) or "mpv command failed")
            return response.get("data")
    return None


def get_property(name: str) -> object:
    try:
        return ipc(["get_property", name], request_id=2)
    except (OSError, RuntimeError):
        return None


def metadata_for(url: str) -> dict:
    # Ask yt-dlp for only the fields we use. A full --dump-single-json payload is
    # hundreds of KiB per video (every format/storyboard), which overruns the
    # bounded-process limit; field selection keeps it to a few hundred bytes.
    command = [
        "yt-dlp",
        "--flat-playlist",
        "--skip-download",
        "--no-warnings",
        "--playlist-end",
        "1",
        "--print",
        "%(.{id,title,channel,uploader,thumbnail,duration})j",
        url,
    ]
    code, stdout, stderr = _run_bounded_text(command, timeout=25)
    if code != 0:
        raise RuntimeError(stderr.strip()[:MAX_TITLE_CHARS] or "yt-dlp could not read this video")
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("yt-dlp returned no usable metadata")


def search(query: str, lang: str) -> dict:
    query = query.strip()[:160]
    if not query:
        return {"ok": False, "message": _t("Escribe algo para buscar.", "Type something to search.", lang)}
    # YouTube search returns channels/playlists as well as videos; ask for a few
    # extra so filtering still leaves MAX_RESULTS real videos.
    fetch_count = MAX_RESULTS + 5
    command = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--playlist-end",
        str(fetch_count),
        "--no-warnings",
        "--quiet",
        "ytsearch%d:" % fetch_count + query,
    ]
    code, stdout, _ = _run_bounded_text(command, timeout=35)
    if code != 0:
        return {"ok": False, "message": _t("No se pudo buscar en YouTube.", "YouTube search failed.", lang)}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "message": _t("La respuesta de YouTube no fue válida.", "YouTube returned an invalid response.", lang)}
    if not isinstance(data, dict):
        return {"ok": False, "message": _t("La respuesta de YouTube no fue válida.", "YouTube returned an invalid response.", lang)}
    entries = []
    source_entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    for item in source_entries:
        if len(entries) >= MAX_RESULTS:
            break
        if not isinstance(item, dict):
            continue
        # Skip channel/playlist results (YouTubeTab, YoutubePlaylist, ...): only
        # real videos have an 11-character video id and ie_key "Youtube".
        ie_key = _bounded_text(item.get("ie_key"), 32)
        if ie_key and ie_key != "Youtube":
            continue
        video_id = _bounded_text(item.get("id"), 64)
        if not VIDEO_ID_RE.fullmatch(video_id):
            continue
        entries.append({
            "id": video_id,
            "title": _bounded_text(item.get("title"), MAX_TITLE_CHARS) or "Untitled",
            "channel": _bounded_text(item.get("channel") or item.get("uploader"), MAX_CHANNEL_CHARS),
            "duration": format_duration(item.get("duration")),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": _safe_http_url(item.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg", MAX_THUMBNAIL_CHARS),
        })
    return {"ok": True, "results": entries}


def _audio_only_default() -> bool:
    value = os.environ.get("OMARCHY_YOUTUBE_AUDIO_ONLY", "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _play_queue(items: list, index: int, lang: str, audio_only: bool | None = None) -> dict:
    if not items:
        return {"ok": False, "message": _t("No hay nada para reproducir.", "Nothing to play.", lang)}
    index = min(max(index, 0), len(items) - 1)
    # Rotate so the chosen track is entry 0: mpv then plays it immediately and
    # keeps the rest as the following playlist (wrapping at the end). This avoids
    # racing playlist-play-index against the first file's yt-dlp resolution.
    ordered = items[index:] + items[:index]
    if audio_only is None:
        audio_only = _audio_only_default()
    global _PLAYER_MODE_OVERRIDE
    _PLAYER_MODE_OVERRIDE = not audio_only
    previous_state = read_state()
    restore_address = active_window_address() if not audio_only else ""
    try:
        ipc(["set_property", "ytdl-format", AUDIO_FORMAT if audio_only else VIDEO_FORMAT])
        ipc(["stop"])
        for position, item in enumerate(ordered):
            ipc(["loadfile", item["url"], "replace" if position == 0 else "append"])
        ipc(["set_property", "pause", False])
        current = ordered[0]
        ipc(["set_property", "force-media-title", current["title"]])
        if audio_only:
            # Headless player: there is no window to manage.
            ipc(["set_property", "vid", "no"])
        else:
            ipc(["set_property", "vid", "auto"])
            ipc(["set_property", "window-minimized", False])
            if previous_state.get("audioOnly") and previous_state.get("workspace"):
                move_player_to_workspace(previous_state.get("workspace", ""))
            normalize_window(restore_address)
        write_state({
            "url": current["url"],
            "title": current["title"],
            "channel": current.get("channel", ""),
            "thumbnail": current.get("thumbnail", ""),
            "audioOnly": audio_only,
            "windowed": not audio_only,
            "queue": ordered,
            "queueIndex": 0,
            "workspace": previous_state.get("workspace", ""),
        })
        return {
            "ok": True,
            "title": current["title"],
            "channel": current.get("channel", ""),
            "thumbnail": current.get("thumbnail", ""),
            "url": current["url"],
            "audioOnly": audio_only,
            "queueIndex": 0,
            "queueCount": len(ordered),
        }
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        return {"ok": False, "message": _t("No se pudo reproducir este video.", "This video could not be played.", lang), "detail": str(error)[:180]}


def play(url: str, lang: str) -> dict:
    if not valid_video_url(url):
        return {"ok": False, "message": _t("La URL no parece ser de YouTube.", "That URL does not look like YouTube.", lang)}
    try:
        data = metadata_for(url)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return {"ok": False, "message": _t("No se pudo reproducir este video.", "This video could not be played.", lang), "detail": str(error)[:180]}
    title = _bounded_text(data.get("title"), MAX_TITLE_CHARS) or "YouTube video"
    channel = _bounded_text(data.get("channel") or data.get("uploader"), MAX_CHANNEL_CHARS)
    thumbnail = _safe_http_url(data.get("thumbnail"), MAX_THUMBNAIL_CHARS)
    if not thumbnail:
        video_id = _bounded_text(data.get("id"), 64)
        if VIDEO_ID_RE.fullmatch(video_id):
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return _play_queue([{"url": url, "title": title, "channel": channel, "thumbnail": thumbnail}], 0, lang)


def playlist_command(start: str | None, payload: str | None, lang: str) -> dict:
    try:
        index = int(start or "0")
    except (TypeError, ValueError):
        index = 0
    try:
        raw = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return {"ok": False, "message": _t("Lista de reproducción no válida.", "Invalid playlist.", lang)}
    items = _queue_items(raw)
    if not items:
        return {"ok": False, "message": _t("No hay resultados para reproducir.", "Nothing to play.", lang)}
    return _play_queue(items, index, lang)


def _reload_format(audio_only: bool) -> None:
    """Reload the current queue with the matching yt-dlp format, keeping position.

    Toggling audio-only turns off the video track, but a stream already opened
    with a muxed format keeps downloading video. Reloading with bestaudio stops
    that, while keeping the whole queue (and its index) so next/previous and the
    media keys keep working.
    """
    state = read_state()
    queue = state.get("queue") if isinstance(state.get("queue"), list) else []
    url = state.get("url", "")
    if not queue and valid_video_url(url):
        queue = [{
            "url": url,
            "title": state.get("title", ""),
            "channel": state.get("channel", ""),
            "thumbnail": state.get("thumbnail", ""),
        }]
    if not queue:
        return
    try:
        index = int(state.get("queueIndex") or 0)
    except (TypeError, ValueError):
        index = 0
    index = min(max(index, 0), len(queue) - 1)
    ordered = queue[index:] + queue[:index]
    position = get_property("time-pos")
    paused = get_property("pause") is True
    global _PLAYER_MODE_OVERRIDE
    _PLAYER_MODE_OVERRIDE = not audio_only
    ipc(["set_property", "ytdl-format", AUDIO_FORMAT if audio_only else VIDEO_FORMAT])
    ipc(["stop"])
    for item_index, item in enumerate(ordered):
        ipc(["loadfile", item["url"], "replace" if item_index == 0 else "append"])
    if isinstance(position, (int, float)) and position > 0:
        try:
            ipc(["seek", float(position), "absolute"])
        except (OSError, RuntimeError):
            # The reloaded stream may not be seekable yet; ignore and let it play
            # from the start rather than failing the whole toggle.
            pass
    ipc(["set_property", "force-media-title", ordered[0]["title"]])
    if paused:
        ipc(["set_property", "pause", True])
    state["queue"] = ordered
    state["queueIndex"] = 0
    state["audioOnly"] = audio_only
    state["windowed"] = not audio_only
    write_state(state)


def action(name: str, lang: str, extra: str = "") -> dict:
    try:
        if name == "pause":
            ipc(["cycle", "pause"])
        elif name == "stop":
            write_state({})
            if socket_is_alive():
                ipc(["quit"])
        elif name == "next":
            ipc(["playlist-next"])
        elif name == "prev":
            ipc(["playlist-prev"])
        elif name == "seek":
            try:
                seconds = max(0.0, float(extra))
            except (TypeError, ValueError):
                return {"ok": False, "message": _t("Posición no válida.", "Invalid position.", lang)}
            ipc(["seek", seconds, "absolute"])
        elif name == "back":
            ipc(["seek", -10, "relative"])
        elif name == "forward":
            ipc(["seek", 10, "relative"])
        elif name == "volume-up":
            ipc(["add", "volume", 5])
        elif name == "volume-down":
            ipc(["add", "volume", -5])
        elif name == "fullscreen":
            # Headless (audio-only) players have no window to fullscreen.
            if _running_windowed() is not False:
                ipc(["cycle", "fullscreen"])
        elif name == "resume":
            state = read_state()
            queue = state.get("queue") or []
            if not queue or socket_is_alive():
                return {"ok": True, "resumed": False}
            result = _play_queue(queue, int(state.get("queueIndex") or 0), lang, audio_only=bool(state.get("audioOnly")))
            if result.get("ok"):
                result["resumed"] = True
            return result
        elif name == "show":
            if _running_windowed() is not False:
                ipc(["set_property", "window-minimized", False])
                normalize_window(active_window_address())
        elif name == "audio-only":
            workspace = player_workspace()
            if workspace and not workspace.startswith("special:"):
                current = read_state()
                current["workspace"] = workspace
                write_state(current)
            _reload_format(True)
        elif name == "show-video":
            _reload_format(False)
            state = read_state()
            move_player_to_workspace(state.get("workspace", ""))
            normalize_window(active_window_address())
        elif name == "quit":
            if socket_is_alive():
                ipc(["quit"])
            write_state({})
        else:
            return {"ok": False, "message": _t("Acción no reconocida.", "Unknown action.", lang)}
        return {"ok": True}
    except (OSError, RuntimeError) as error:
        return {"ok": False, "message": _t("El reproductor no está disponible.", "The player is not available.", lang), "detail": str(error)[:180]}


def _status_snapshot(values: dict, state: dict) -> dict:
    queue = state.get("queue") if isinstance(state.get("queue"), list) else []
    position = values.get("playlist-pos")
    if not isinstance(position, int) or position < 0:
        try:
            position = int(state.get("queueIndex") or 0)
        except (TypeError, ValueError):
            position = 0
    current = queue[position] if 0 <= position < len(queue) else {}
    title = _bounded_text(current.get("title") or values.get("media-title") or state.get("title"), MAX_TITLE_CHARS)
    idle = values.get("idle-active")
    pause = values.get("pause")
    active = bool(title) and not bool(idle)
    return {
        "ok": True,
        "active": active,
        "playing": active and pause is False,
        "title": title,
        "channel": _bounded_text(current.get("channel") or state.get("channel"), MAX_CHANNEL_CHARS),
        "thumbnail": _safe_http_url(current.get("thumbnail") or state.get("thumbnail"), MAX_THUMBNAIL_CHARS),
        "url": _safe_http_url(current.get("url") or state.get("url")),
        "position": _bounded_number(values.get("time-pos")),
        "durationSeconds": _bounded_number(values.get("duration")),
        "fullscreen": bool(values.get("fullscreen")),
        "audioOnly": bool(state.get("audioOnly")) or values.get("vid") == "no",
        "queueIndex": position if queue else -1,
        "queueCount": len(queue),
    }


def status() -> dict:
    state = read_state()
    if not socket_is_alive():
        return {"ok": True, "active": False, **state}
    values = {
        "idle-active": get_property("idle-active"),
        "media-title": get_property("media-title"),
        "pause": get_property("pause"),
        "time-pos": get_property("time-pos"),
        "duration": get_property("duration"),
        "fullscreen": get_property("fullscreen"),
        "vid": get_property("vid"),
        "playlist-pos": get_property("playlist-pos"),
        "playlist-count": get_property("playlist-count"),
    }
    return _status_snapshot(values, state)


WATCH_PROPERTIES = ("time-pos", "pause", "duration", "media-title", "idle-active", "vid", "fullscreen", "playlist-pos", "playlist-count")
WATCH_INTERVAL = 0.5


def watch() -> int:
    """Stream status as newline-delimited JSON, reconnecting across mpv restarts.

    One long-lived process replaces the per-poll ``status`` spawns, so the bar
    gets live updates without starting a Python interpreter every time. It stays
    alive when mpv exits and reconnects once a new player appears.
    """
    running = True

    def stop_handler(*_args: object) -> None:
        nonlocal running
        running = False

    for handled in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(handled, stop_handler)
        except (OSError, ValueError):
            pass

    def announce_inactive(state: dict) -> None:
        print(json.dumps({"ok": True, "active": False, **state}, ensure_ascii=False), flush=True)

    def stream(connection: socket.socket) -> None:
        values: dict[str, object] = {"idle-active": True}
        state = read_state()
        for index, name in enumerate(WATCH_PROPERTIES, start=1):
            connection.sendall(
                (json.dumps({"command": ["observe_property", index, name], "request_id": index}) + "\n").encode("utf-8")
            )
        buffer = b""
        last_emit = 0.0
        force_emit = True
        while running:
            try:
                connection.settimeout(WATCH_INTERVAL)
                chunk = connection.recv(65536)
            except socket.timeout:
                chunk = None
            except OSError:
                break
            if chunk == b"":
                break
            if chunk:
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for raw in lines:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        message = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    event = message.get("event")
                    name = message.get("name")
                    if event == "property-change" and name in WATCH_PROPERTIES:
                        values[name] = message.get("data")
                        if name != "time-pos":
                            force_emit = True
                        if name == "playlist-pos":
                            # Keep mpv's display title (and MPRIS metadata) in sync
                            # with our own queue so the shell OSD shows the right name.
                            state = read_state()
                            queue = state.get("queue") if isinstance(state.get("queue"), list) else []
                            position = message.get("data")
                            if isinstance(position, int) and 0 <= position < len(queue):
                                try:
                                    connection.sendall(
                                        (json.dumps({
                                            "command": ["set_property", "force-media-title", queue[position]["title"]],
                                            "request_id": 900,
                                        }) + "\n").encode("utf-8")
                                    )
                                except OSError:
                                    break
                    elif event == "end-file":
                        values["idle-active"] = True
                        force_emit = True
            now = time.monotonic()
            if force_emit or now - last_emit >= WATCH_INTERVAL:
                print(json.dumps(_status_snapshot(values, state), ensure_ascii=False), flush=True)
                last_emit = now
                force_emit = False

    announced_inactive = False
    while running:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(0.4)
        connected = False
        for _ in range(10):
            try:
                connection.connect(str(SOCKET_PATH))
                connected = True
                break
            except OSError:
                if not running:
                    break
                time.sleep(0.2)
        if not connected:
            connection.close()
            if not announced_inactive:
                announce_inactive(read_state())
                announced_inactive = True
            time.sleep(1.0)
            continue
        announced_inactive = False
        try:
            stream(connection)
        except OSError:
            pass
        finally:
            connection.close()
        if not announced_inactive:
            announce_inactive(read_state())
            announced_inactive = True
        time.sleep(0.5)

    return 0



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["search", "play", "playlist", "action", "status", "watch"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("extra", nargs="?")
    parser.add_argument("--lang", choices=["es", "en"], default="en")
    args = parser.parse_args()
    try:
        if args.command == "search":
            output = search(args.value or "", args.lang)
        elif args.command == "play":
            output = play(args.value or "", args.lang)
        elif args.command == "playlist":
            output = playlist_command(args.value, args.extra, args.lang)
        elif args.command == "action":
            output = action(args.value or "", args.lang, args.extra or "")
        elif args.command == "watch":
            return watch()
        else:
            output = status()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        output = {"ok": False, "message": str(error)[:200]}
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
