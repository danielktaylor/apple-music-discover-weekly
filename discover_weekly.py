#!/usr/bin/python3
"""Discover Weekly for Apple Music.

Each week, snapshots ~30 songs from the user's Apple Music Discovery Station into
a "Discover Weekly" playlist, keeping last week's as "Discover Weekly – <Mon d>",
both inside a "Discover Weekly" folder.

Talks to the music.apple.com web player's private API (amp-api) using the web
player's embedded developer token and the user's media-user-token cookie, which
lives in the macOS Keychain. Fetching station tracks this way plays nothing, so
it sends no play/skip signals.
"""
import argparse
import base64
import hashlib
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NAME = "Discover Weekly"
TRACK_COUNT = 30
# Normal week deletes 1; a two-laptop collision or empty playlist adds 1 each.
MAX_DELETES = 3
SETTLE_SECONDS = 45
API = "https://amp-api.music.apple.com"
KEYCHAIN_SERVICE = "apple-music-media-user-token"
LABEL = "com.dtaylor.discover-weekly"
APP_DIR = os.path.expanduser("~/Library/Application Support/discover-weekly")
STATE_FILE = os.path.join(APP_DIR, "state.json")
LOG_FILE = os.path.expanduser("~/Library/Logs/discover-weekly.log")
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
AUTH_MESSAGE = "Apple Music sign-in expired. Copy a new token, then run: discover_weekly.py set-token"
WEEK_RE = re.compile(r"week of (\d{4}-\d{2}-\d{2})")


class AuthError(Exception):
    pass


class NetworkError(Exception):
    pass


def log(msg):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def notify(state, title, message):
    # Throttled so hourly retries don't spam.
    last = state.get("last_notified", 0)
    if time.time() - last < 20 * 3600:
        return
    script = (f"display notification {json.dumps(message, ensure_ascii=False)} "
              f"with title {json.dumps(title, ensure_ascii=False)}")
    subprocess.run(["osascript", "-e", script], capture_output=True)
    state["last_notified"] = time.time()
    save_state(state)


def this_monday():
    today = dt.date.today()
    return today - dt.timedelta(days=today.weekday())


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, OSError) as e:
        raise NetworkError(str(e))


def _jwt_payload(tok):
    seg = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def fetch_developer_token():
    """The web player embeds a short-lived developer token in its JS bundle."""
    html = _http_get("https://music.apple.com/us/browse")
    bundle = re.search(r'/assets/index~[\w-]+\.js', html)
    if not bundle:
        raise RuntimeError("web player bundle not found; page layout changed?")
    js = _http_get("https://music.apple.com" + bundle.group(0))
    now = time.time()
    for tok in re.findall(r'eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+', js):
        try:
            p = _jwt_payload(tok)
        except ValueError:
            continue
        if p.get("iss") == "AMPWebPlay" and p.get("exp", 0) > now:
            return tok
    raise RuntimeError("no valid developer token in web player bundle")


def media_user_token():
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise AuthError("media-user-token missing from Keychain")
    return r.stdout.strip()


def wait_for_network(timeout=180):
    """Right after wake, Wi-Fi often isn't up yet."""
    deadline = time.time() + timeout
    while True:
        try:
            _http_get("https://music.apple.com/")
            return
        except NetworkError:
            if time.time() > deadline:
                raise
            time.sleep(15)


class Client:
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {fetch_developer_token()}",
            "media-user-token": media_user_token(),
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "User-Agent": UA,
            "Content-Type": "application/json",
        }
        self.storefront = self.req("GET", "/v1/me/storefront")["data"][0]["id"]

    def req(self, method, path, params=None, body=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "ignore")[:300]
            if e.code in (401, 403):
                raise AuthError(f"{e.code} on {method} {path}: {text}")
            raise RuntimeError(f"{e.code} on {method} {path}: {text}")
        except (urllib.error.URLError, OSError) as e:
            raise NetworkError(str(e))

    def paged(self, path, params=None):
        params = dict(params or {}, limit=100)
        out, offset = [], 0
        while True:
            try:
                res = self.req("GET", path, params=dict(params, offset=offset))
            except RuntimeError as e:
                if str(e).startswith("404"):  # empty folder/playlist
                    return out
                raise
            out.extend(res.get("data", []))
            if not res.get("next"):
                return out
            offset += 100

    def discovery_station_id(self, state):
        if state.get("station_id"):
            return state["station_id"]
        recent = self.req("GET", "/v1/me/recent/radio-stations", params={"limit": 30})
        for s in recent.get("data", []):
            if s.get("attributes", {}).get("name") == "Discovery Station":
                return s["id"]
        # Discovery's id is a protobuf wrapping the personal station's hash.
        personal = self.req("GET", f"/v1/catalog/{self.storefront}/stations",
                            params={"filter[identity]": "personal"})["data"][0]["id"]
        user_hash = personal.split("ra.u-", 1)[1].encode()
        payload = b"\x18\x02\x3a" + bytes([len(user_hash)]) + user_hash
        return "ra.q-" + base64.urlsafe_b64encode(payload).decode().rstrip("=")

    def in_library(self, song_ids):
        """Catalog song ids that are already in the user's library."""
        owned = set()
        for i in range(0, len(song_ids), 25):
            res = self.req("GET", f"/v1/catalog/{self.storefront}/songs",
                           params={"ids": ",".join(song_ids[i:i + 25]), "relate": "library"})
            for s in res.get("data", []):
                if s.get("relationships", {}).get("library", {}).get("data"):
                    owned.add(s["id"])
        return owned

    def station_tracks(self, station_id, count, exclude):
        """New-to-the-user songs: skips repeats and anything already in the library."""
        tracks, seen = [], set(exclude)
        for _ in range(20):
            res = self.req("POST", f"/v1/me/stations/next-tracks/{station_id}",
                           params={"limit": 10})
            batch = []
            for t in res.get("data", []):
                if t.get("type") == "songs" and t["id"] not in seen:
                    seen.add(t["id"])
                    batch.append(t)
            owned = self.in_library([t["id"] for t in batch]) if batch else set()
            if owned:
                log(f"skipping {len(owned)} song(s) already in library")
            tracks += [t for t in batch if t["id"] not in owned]
            if len(tracks) >= count:
                break
        return tracks[:count]

    def find_or_create_folder(self, create=True):
        for item in self.paged("/v1/me/library/playlist-folders/p.playlistsroot/children"):
            if item["type"] == "library-playlist-folders" and item["attributes"]["name"] == NAME:
                return item["id"]
        if not create:
            return None
        res = self.req("POST", "/v1/me/library/playlist-folders",
                       body={"attributes": {"name": NAME}})
        log(f"created folder {res['data'][0]['id']}")
        return res["data"][0]["id"]

    def get_playlist(self, pid):
        """Returns None for deleted playlists, which come back as nameless stubs."""
        try:
            pl = self.req("GET", f"/v1/me/library/playlists/{pid}",
                          params={"include": "parent"})["data"][0]
        except RuntimeError as e:
            if str(e).startswith("404"):
                return None
            raise
        return pl if pl.get("attributes", {}).get("name") else None

    def our_playlists(self, folder_id, known_ids=()):
        """Playlists this tool made that are still in its folder.

        Folder listings lag a few seconds behind writes, so known ids are checked
        too. Requiring both the folder and our description tag means a playlist the
        user adds to the folder, or drags out of it, is never touched.
        """
        items = self.paged(f"/v1/me/library/playlist-folders/{folder_id}/children")
        ids = [i["id"] for i in items if i["type"] == "library-playlists"]
        ids += [i for i in known_ids if i not in ids]
        out = []
        for pid in ids:
            pl = self.get_playlist(pid)
            if not pl or playlist_week(pl) is None:
                continue
            parents = pl.get("relationships", {}).get("parent", {}).get("data", [])
            if [x["id"] for x in parents] == [folder_id]:
                out.append(pl)
        return out

    def playlist_catalog_ids(self, playlist_id):
        tracks = self.paged(f"/v1/me/library/playlists/{playlist_id}/tracks")
        ids = set()
        for t in tracks:
            pp = t.get("attributes", {}).get("playParams", {})
            ids.add(pp.get("catalogId") or pp.get("id") or t["id"])
        return ids

    def delete_playlist(self, pl):
        try:
            self.req("DELETE", f"/v1/me/library/playlists/{pl['id']}")
        except RuntimeError:
            # Already deleted (e.g. by another laptop) returns a 500, not a 404.
            if self.get_playlist(pl["id"]) is not None:
                raise
        log(f"deleted {pl['attributes']['name']!r} ({pl['id']})")

    def rename_playlist(self, pl, name):
        week = playlist_week(pl)
        self.req("PATCH", f"/v1/me/library/playlists/{pl['id']}", body={"attributes": {
            "name": name, "description": description_for(week)}})
        log(f"renamed {pl['attributes']['name']!r} ({pl['id']}) to {name!r}")


def playlist_week(pl):
    """The week from our description tag; None means the playlist isn't ours."""
    desc = (pl.get("attributes", {}).get("description") or {}).get("standard", "")
    m = WEEK_RE.search(desc)
    return dt.date.fromisoformat(m.group(1)) if m else None


def dated_name(week):
    return f"{NAME} – {week.strftime('%b')} {week.day}"


def description_for(week):
    return f"Discovery Station picks for the week of {week.isoformat()}"


def _age_seconds(pl):
    added = pl.get("attributes", {}).get("dateAdded")
    if not added:
        return float("inf")
    t = dt.datetime.strptime(added, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds()


def _creation_order(pl):
    # Every laptop must pick the same survivor when duplicates exist.
    return (pl.get("attributes", {}).get("dateAdded") or "", pl["id"])


def plan(playlists, track_ids, week):
    """Decide (keep_current, keep_previous, deletions, renames) for the folder."""
    def usable(p):
        # Fresh playlists may not list their tracks yet; don't mistake them for empty.
        return track_ids.get(p["id"]) or _age_seconds(p) < 600

    future = [p for p in playlists if playlist_week(p) > week]
    if future:
        raise RuntimeError(f"{future[0]['attributes']['name']!r} is tagged for a future week; "
                           "is this Mac's clock or timezone wrong?")
    this_week = sorted((p for p in playlists if playlist_week(p) == week and usable(p)),
                       key=_creation_order)
    past = [p for p in playlists if playlist_week(p) < week and usable(p)]
    current = this_week[0] if this_week else None
    previous = None
    if past:
        last = max(playlist_week(p) for p in past)
        previous = sorted((p for p in past if playlist_week(p) == last), key=_creation_order)[0]
    keep = {p["id"] for p in (current, previous) if p}
    deletions = [p for p in playlists if p["id"] not in keep]
    renames = []
    if current and current["attributes"]["name"] != NAME:
        renames.append((current, NAME))
    if previous and previous["attributes"]["name"] != dated_name(playlist_week(previous)):
        renames.append((previous, dated_name(playlist_week(previous))))
    return current, previous, deletions, renames


def tidy(c, folder, known_ids, week, dry_run):
    """Bring the folder to exactly this week's playlist plus the most recent prior one."""
    playlists = c.our_playlists(folder, known_ids)
    track_ids = {p["id"]: c.playlist_catalog_ids(p["id"]) for p in playlists}
    current, previous, deletions, renames = plan(playlists, track_ids, week)
    if len(deletions) > MAX_DELETES:
        raise RuntimeError(f"refusing to delete {len(deletions)} playlists (limit {MAX_DELETES}): "
                           + ", ".join(p["attributes"]["name"] for p in deletions))
    if dry_run:
        print("would delete:", [p["attributes"]["name"] for p in deletions],
              "| would rename:", [(p["attributes"]["name"], n) for p, n in renames])
        return current, playlists, track_ids
    # Delete first so renames never collide with a stale dated name.
    for p in deletions:
        c.delete_playlist(p)
    for p, name in renames:
        c.rename_playlist(p, name)
    return current, playlists, track_ids


def run(dry_run=False, force=False, settle=SETTLE_SECONDS):
    state = load_state()
    week = this_monday()
    if not force and state.get("last_week") == week.isoformat():
        return
    try:
        token_hash = hashlib.sha256(media_user_token().encode()).hexdigest()[:16]
        if state.get("auth_failed_token") == token_hash:
            # Same rejected token: don't hit Apple again until set-token.
            notify(state, NAME, AUTH_MESSAGE)
            return
        wait_for_network()
        c = Client()
        station = c.discovery_station_id(state)
        folder = c.find_or_create_folder(create=not dry_run)
        known = state.get("playlist_ids", [])
        if force and folder:
            for p in c.our_playlists(folder, known):
                if playlist_week(p) != week:
                    continue
                if dry_run:
                    print(f"would delete {p['attributes']['name']!r} (--force)")
                else:
                    c.delete_playlist(p)
        current, playlists, track_ids = (tidy(c, folder, known, week, dry_run)
                                         if folder else (None, [], {}))
        if force and dry_run:
            current = None

        if current:
            log(f"week {week} already built ({current['id']}, {len(track_ids[current['id']])} tracks)")
        else:
            exclude = set().union(*track_ids.values()) if track_ids else set()
            tracks = c.station_tracks(station, TRACK_COUNT, exclude)
            if len(tracks) < TRACK_COUNT:
                raise RuntimeError(f"station only yielded {len(tracks)} new tracks")
            log(f"fetched {len(tracks)} tracks from {station} (excluded {len(exclude)} repeats)")
            if dry_run:
                for t in tracks:
                    a = t["attributes"]
                    print(f"  {a.get('name')} — {a.get('artistName')}")
                return
            res = c.req("POST", "/v1/me/library/playlists", body={
                "attributes": {"name": NAME, "description": description_for(week)},
                "relationships": {
                    "tracks": {"data": [{"id": t["id"], "type": "songs"} for t in tracks]},
                    "parent": {"data": [{"id": folder, "type": "library-playlist-folders"}]},
                },
            })
            new_id = res["data"][0]["id"]
            log(f"created {NAME!r} ({new_id}) for week of {week}")
            known = known + [new_id]
            # Let listings catch up so a concurrent run on another laptop is visible,
            # then resolve any duplicate this-week playlists.
            time.sleep(settle)
            current, playlists, _ = tidy(c, folder, known, week, dry_run)

        if dry_run:
            return
        if not current:
            raise RuntimeError("no playlist for this week after build")
        state.update(last_week=week.isoformat(), station_id=station,
                     playlist_ids=[p["id"] for p in c.our_playlists(folder, known)])
        state.pop("auth_failed_token", None)
        save_state(state)
    except NetworkError as e:
        log(f"network unavailable, will retry next run: {e}")
    except AuthError as e:
        log(f"auth failed: {e}")
        try:
            state["auth_failed_token"] = hashlib.sha256(media_user_token().encode()).hexdigest()[:16]
        except AuthError:
            pass
        save_state(state)
        notify(state, NAME, AUTH_MESSAGE)
        sys.exit(1)
    except Exception as e:
        log(f"failed: {e!r}")
        notify(state, NAME, f"Failed to build this week's playlist: {e}"[:200])
        sys.exit(1)


def set_token():
    """Store the token from the clipboard; `security -w` prompts truncate at 128 chars."""
    token = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"[\w.+/=-]{100,}", token):
        sys.exit("Clipboard doesn't look like a media-user-token. On music.apple.com, run in "
                 "the browser console: copy(document.cookie.match(/media-user-token=([^;]+)/)[1])")
    # Fed via stdin so the token never appears in the process list.
    cmd = (f'add-generic-password -U -a "{os.environ["USER"]}" '
           f'-s {KEYCHAIN_SERVICE} -w "{token}"\n')
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Keychain write failed: {r.stderr.strip()}")
    subprocess.run(["pbcopy"], input="", text=True)
    state = load_state()
    state.pop("last_notified", None)
    state.pop("auth_failed_token", None)
    save_state(state)
    print(f"Saved token ({len(token)} chars) and cleared clipboard.")


def install():
    os.makedirs(APP_DIR, exist_ok=True)
    target = os.path.join(APP_DIR, "discover_weekly.py")
    shutil.copy2(os.path.abspath(__file__), target)
    os.chmod(target, 0o755)
    # Hourly + at login; launchd fires missed calendar slots on wake, and run()
    # exits immediately once this week is done.
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>{target}</string><string>run</string></array>
  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>0</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>{LOG_FILE}</string>
</dict>
</plist>
"""
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    with open(PLIST, "w") as f:
        f.write(plist)
    subprocess.run(["launchctl", "bootstrap", domain, PLIST], check=True)
    print(f"Installed {target}\nLaunch agent {PLIST}\nLog {LOG_FILE}")


def uninstall():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("Launch agent removed. Playlists and Keychain token left in place.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="build this week's playlist if not already done")
    p.add_argument("--dry-run", action="store_true", help="fetch tracks but change nothing")
    p.add_argument("--force", action="store_true", help="rebuild even if this week is done")
    sub.add_parser("set-token", help="save media-user-token from clipboard to Keychain")
    sub.add_parser("install", help="install launch agent")
    sub.add_parser("uninstall", help="remove launch agent")
    a = ap.parse_args()
    if a.cmd == "run":
        run(dry_run=a.dry_run, force=a.force)
    elif a.cmd == "set-token":
        set_token()
    elif a.cmd == "install":
        install()
    else:
        uninstall()


if __name__ == "__main__":
    main()
