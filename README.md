# Discover Weekly for Apple Music

Spotify-style Discover Weekly playlist for Apple Music. Every Monday morning it takes 30 songs from your Apple Music **Discovery Station** and puts them in a playlist in a `Discover Weekly` folder.

* Songs go into the playlists only, and songs already in your library are skipped.
* It reads the station's upcoming songs without playing them, so it sends no plays or skips.
* It runs in the background and never opens the Music app. The only thing you'll see is a notification if something needs your attention.
* The script only uses Python's standard library.

## Installation

Requires macOS with an Apple Music subscription and **Sync Library** turned on.

### 1. Save your Apple Music sign-in token

1. Sign in at <https://music.apple.com> in Safari or Chrome.
2. Open the developer tools. In Safari, first turn on *Settings → Advanced → Show features for
   web developers*. Go to the **Console** tab and run this, which copies the token to your
   clipboard:
   ```js
   copy(document.cookie.match(/media-user-token=([^;]+)/)[1])
   ```
3. Save it to your Keychain. This reads the token from the clipboard and then clears the
   clipboard:
   ```bash
   /usr/bin/python3 discover_weekly.py set-token
   ```

### 2. Try a dry run (optional)

This fetches 30 songs and shows what would change, without changing anything:

```bash
/usr/bin/python3 discover_weekly.py run --dry-run --force
```

### 3. Install the background job

```bash
/usr/bin/python3 discover_weekly.py install
```

This does three things:

- copies the script to `~/Library/Application Support/discover-weekly/`, so the background job never needs access to `~/Downloads`;
- creates the launch agent `~/Library/LaunchAgents/com.dtaylor.discover-weekly.plist`, which runs the script **at login and every hour**;
- starts the launch agent.

On or after Monday, the first run after you open your laptop builds the week's playlist. Every
other run checks a local state file and exits immediately, without contacting Apple. If the
laptop was asleep at the scheduled time, launchd runs the missed job when it wakes. If it was
powered off, the job runs at login.

If you edit `discover_weekly.py`, run `install` again to update the installed copy.

### 4. Allow notifications

Notifications are sent with `osascript`, so macOS lists them under **Script Editor**. Send a test notification, and if macOS asks, allow it:

```bash
osascript -e 'display notification "Test" with title "Discover Weekly"'
```

If nothing appears, check *System Settings → Notifications → Script Editor*.

**Recommended:** in the same settings pane, set the alert style to **Persistent**. The default style (*Temporary*) disappears after a few seconds. The script only notifies you when something needs attention, such as an expired sign-int.

## Uninstall

Remove the background job. This leaves your playlists, the token and the log in place:

```bash
/usr/bin/python3 discover_weekly.py uninstall
```

To remove everything else:

```bash
rm -rf ~/Library/Application\ Support/discover-weekly
rm -f ~/Library/Logs/discover-weekly.log
security delete-generic-password -s apple-music-media-user-token
```

The playlists and the folder stay in your Apple Music library.

## Commands

| Command | What it does |
|---|---|
| `run` | Builds this week's playlist if it isn't built yet. This is what the launch agent runs. |
| `run --dry-run` | Fetches songs and prints what would change, without changing anything. |
| `run --force` | Replaces this week's playlist with a fresh set of songs and keeps last week's. |
| `set-token` | Saves a `media-user-token` from the clipboard to the Keychain. |
| `install` / `uninstall` | Adds or removes the background job. |

After installing, the script is at
`~/Library/Application Support/discover-weekly/discover_weekly.py`.

## When your sign-in expires

The token reportedly lasts a few months; how long exactly hasn't been confirmed. When Apple rejects it, you get a notification at most once a day:
*"Apple Music sign-in expired…"*. Until you replace the token, the script stops contacting
Apple. To fix it, repeat [step 1](#1-save-your-apple-music-sign-in-token) using the installed
copy:

```bash
/usr/bin/python3 ~/Library/Application\ Support/discover-weekly/discover_weekly.py set-token
```

The next hourly run will build the playlist.

## Multiple laptops

You can install it on more than one Mac. Each Mac needs the token in its own Keychain, and the
same token value works on both. Whichever Mac runs first builds the week's playlist, and the
others see it and do nothing.

If two Macs build at the same moment, both wait 45 seconds and check again. They agree on which
copy to keep (the one created first) and delete the other.

## Safety rules

The script only renames or deletes a playlist if **both** of these are true:

1. its description carries the script's tag, *"Discovery Station picks for the week of
   YYYY-MM-DD"*;
2. it's directly inside the `Discover Weekly` folder.

So playlists you put in the folder are never touched. If you drag an old week out of the folder
to keep it, it's safe too. Also:

- It deletes playlists only, never songs, and at most 3 playlists per run. If it would delete
  more, it stops and notifies you.
- If any playlist is tagged for a future week, it does nothing and notifies you. That usually
  means this Mac's clock or timezone is wrong.
- It creates the new week's playlist before tidying up, so a run that fails partway never loses
  last week's playlist.
- If this week's playlist has no songs and is more than 10 minutes old, it's rebuilt.

## Files

| Path | Purpose |
|---|---|
| `~/Library/Application Support/discover-weekly/discover_weekly.py` | Installed copy of the script |
| `~/Library/Application Support/discover-weekly/state.json` | Last completed week, station ID, the script's playlist IDs |
| `~/Library/LaunchAgents/com.dtaylor.discover-weekly.plist` | Launch agent |
| `~/Library/Logs/discover-weekly.log` | Log |
| Keychain item `apple-music-media-user-token` | Your sign-in token |

## Troubleshooting

- **See what happened:** `tail -n 50 ~/Library/Logs/discover-weekly.log`
- **Check that the job is loaded:**
  `launchctl print gui/$(id -u)/com.dtaylor.discover-weekly | head -20`
- **Run it now:** `launchctl kickstart gui/$(id -u)/com.dtaylor.discover-weekly`. This does
  nothing if the week is already built. Use `run --force` to rebuild.
- **"Station only yielded N new tracks":** the station ran out of new songs after skipping
  repeats and library songs. The next hourly run tries again.
- **Wrong station:** delete `station_id` from `state.json`. On the next run the script looks the
  Discovery Station up again: first in your recently played stations, then by working out its ID
  from your personal station's ID.
