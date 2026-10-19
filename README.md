

# <p align="center">Jukebox Plugin for Enigma2 (E²) ![GitHub repo size](https://img.shields.io/github/repo-size/oe-alliance-plugins/Jukebox.svg)</p>

**Music player and library for OpenATV 8 with extended tags and cover art**


## OpenATV 8 / Python 3

Jukebox plays your music with a classic illuminated jukebox interface, large
album artwork and a music library. The embedded skin scales to HD (1280×720)
and Full HD (1920×1080) and uses its own screen names and colours.
The screen saver moves a dimmed jukebox panel with cover art, a vinyl motif
and separate title, artist and album lines across a dark background.

Mutagen reads MP3 ID3v1/v2 and APEv2 tags, FLAC/Vorbis, MP4 and ASF metadata.
The tag view includes custom fields, Unicode and multiple values. Playback,
scanning, cover downloads and lyrics lookup do not modify music files. The MP3
tag editor writes only when Save is pressed. M3U/M3U8, PLS and multi-file CUE playlists are supported.
Actual codec playback depends on the receiver's Enigma2/GStreamer installation.

Cover priority is embedded artwork, a nearby image, then MusicBrainz and the
Cover Art Archive. Online covers use OpenATV's native Twisted downloader with
cancellation, timeout and a cache. Cover display and downloads are independently
switchable. Lyrics come from tags or accompanying LRC/TXT files first. The
optional [LRCLIB lookup](https://lrclib.net/docs) uses the native OpenATV
downloader and checks artist, title and duration. Matching texts are cached in
`/media/hdd/jukebox/lyrics` (inside the configured database folder). Opening
Tags / Lyrics displays lyrics; yellow switches to all tags.
During playback, BLUE or INFO opens a synchronized view when timestamps are
available in LRC files, embedded LRC text, millisecond SYLT tags or LRCLIB's
synced lyrics. The current line is highlighted between the previous and next
lines. Timing follows the service position, including pause and seeking; it
does not run on a separate clock. This is line synchronization, not word-by-word
karaoke. BLUE pauses/resumes within this view. Plain text remains scrollable
and is labelled as lacking timestamps. LRC offsets and repeated timestamps
are supported; SYLT timestamps stored as MPEG frame numbers remain plain text.

### Using Jukebox

Open **Jukebox** for the file browser. **PVR / FILE** opens its database for
albums, artists, genres and playlists, including during playback. MENU opens
settings directly in the browser and player; **yellow** saves settings and opens
the database. There is one shared plugin entry. The colour key labels
show the actions available in each view; INFO returns to the current player.
In the player, **green** plays/resumes and **yellow** pauses. Repeating either
colour key keeps that state; Play/Pause toggles playback. **Left/right on the
directional pad** selects the previous/next track. Rewind/fast-forward seeks
within the current track; 1/3, 4/6 and 7/9 use Enigma2's configured skip sizes.
The library menu scans the selected music folder into the database. Select a
song and press MENU: **Edit MP3 tags** and **Edit database entry** appear first.
The footer shows a MENU hint for the selected entry. Database-only edits
leave the file unchanged and a later scan reads its original tags again. The
MP3 editor updates the library too and preserves artwork and unrelated frames.
Text fields accept Unicode; use semicolons for multiple artists or genres.
Track/disc accept `number/total`; dates accept `YYYY`, `YYYY-MM` or `YYYY-MM-DD`.

Jukebox uses its own `config.plugins.jukebox` options and `Jukebox.db` database.
The default storage folder is `/media/hdd/jukebox`: `Jukebox.db` stores the
library and `covers/` keeps downloaded artwork across reboots. Missing folders
are created when first used. Previous plugin settings and databases are not imported. The package replaces
the old plugin package.

### Playlist editor and MediaPlayer compatibility

Press **TEXT** in the player or file browser to open the playlist editor;
UP/DOWN in the player also opens it. **MENU** adds individual music files,
the audio files in the current folder, or another playlist; it also removes,
moves or clears entries. **CH+/CH−** moves the selected entry directly.
**Yellow** loads a playlist, **blue** saves, and **green/OK** plays the edited
list from the selected entry. Editing leaves the currently playing queue
unchanged until Play is selected. Cancel discards uncommitted edits; saving
writes the file without interrupting playback.

The default location is Enigma2's shared MediaPlayer playlist folder
(`SCOPE_PLAYLIST`, normally `/etc/enigma2/playlist`). The file picker also
browses USB/HDD folders. Save as `.e2pls` for native MediaPlayer interchange
or `.m3u` for a UTF-8 M3U export readable by MediaPlayer. Existing files require
overwrite confirmation. Order, duplicates and Unicode titles are retained;
E2PLS also preserves service types and stream references.
Jukebox loads `.e2pls`, `.m3u`, `.m3u8`, `.pls` and `.cue` in its editor and
file browser. CUE lists can be played, but their track positions cannot be reordered, merged
or exported to E2PLS/M3U. These formats do not retain CUE track boundaries.

### Build and translations

This repository follows the AutoTimer template with the same GitHub workflows
and CI scripts, `src/setup.py` and `src/setup_translate.py`.
Only PO/POT translation sources belong in Git. GitHub CI updates the catalogs;
the package build compiles MO files before collecting the installed plugin.

On Linux with Python 3, setuptools and GNU gettext:

```sh
cd src
python3 setup.py build
python3 setup.py install --root=/tmp/jukebox-package --install-lib=/usr/lib/enigma2/python/Plugins
```

Run `./updatepot.sh` in `src/Jukebox/locale` to refresh catalogs.
The OE-Alliance recipe is `meta-oe/recipes-oe-alliance/enigma2-plugins/enigma2-plugin-extensions-jukebox.bb`.
It inherits `setuptools3-openplugins` and builds Jukebox as an optional OpenATV
feed package. The source repository must be published before a feed build can fetch it.

### Skinning

`src/Jukebox/skin/skin.xml` contains the embedded screens and uses Enigma2's
`resolution="1280,720"` scaling. Screens include `JukeboxPlayer`, `JukeboxLibrary`,
`JukeboxFileList`, `JukeboxPlaylistFiles`, `JukeboxTags`, `JukeboxTagEditor`, `JukeboxSongList` and
`JukeboxScreenSaver`. `JukeboxSetup` includes the `key_yellow` database action.
`JukeboxTags` additionally provides `previousLine`, `currentLine`, `nextLine`
and `key_blue` for synchronized playback lyrics and pause/resume.
`JukeboxLibrary` exposes a context-sensitive `menuHint` in the same footer row
as the colour keys. `JukeboxPlaybackActions` handles transport keys and
`JukeboxSeekActions` handles seeking without overlapping colour-key events.
The plugin browser uses a framed 300×120 PNG; PVR/VIDEO/FILE are mapped in
`keymap.xml` under `JukeboxLibraryActions`. `JukeboxPlaylistActions` maps TEXT
to the editor; `JukeboxPlaylistEditActions` maps CH+/CH− and Delete. The player,
file browser and song list expose a `playlistHint` source.
`coverArt` uses the size provided by the skin. The player exposes `AlbumArtist`,
`Composer`, `TrackNumber`, `DiscNumber`, `Year`, `Bitrate`, `SampleRate`, `Channels`,
`nowPlaying` and `upNext` sources. Progress uses the native ServicePosition converter.

### Front-panel displays

Jukebox supplies front-panel summaries independently of the TV skin. Colour LCDs
show cover art, title, artist, album, playback state, time and progress. Small
displays use a compact text layout. Browsers and the playlist editor show the
current selection. Playback information remains available during lyrics and the
screen saver. Covers reuse the player's embedded, local or downloaded image;
the display does not start additional downloads.

The active display skin may override `JukeboxPlayerSummary`,
`JukeboxScreenSaverSummary`, `JukeboxTVSummary` and `JukeboxTagsSummary`.
`JukeboxSummary` is their shared fallback name. Available StaticText sources are
`Brand`, `Title`, `Artist`, `Album`, `NextTitle`, `PlaybackState`, `PlaybackMode`
and `CoverPath`. Use `name="coverArt"` for the asynchronously decoded cover;
`session.CurrentService` with the native ServicePosition converter supplies
position and progress. Use VFDPosition/VFDRemaining to respect display time
preferences. The `text1`, `text3`, `text4` labels remain available.

List summaries use `JukeboxFileListSummary`, `JukeboxLibrarySummary` and
`JukeboxSongListSummary`, with `JukeboxSummaryText` as their shared fallback.
They expose `ScreenTitle`, `Title`, `PreviousTitle`, `NextTitle` and the three
text labels. Define these screens in the selected display skin, with `id="1"` where required
by the skin loader.
Without an override, `skin/display.xml` supplies scalable landscape, portrait,
compact and browser layouts using the LCD desktop's own resolution.

## Github status
[![Build](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/buildbot.yml/badge.svg)](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/buildbot.yml)
[![Lint Status](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/pylint.yml/badge.svg)](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/pylint.yml)
[![Ruff Status](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/ruff.yml/badge.svg)](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/ruff.yml)
[![Build Status](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/compile.yml/badge.svg)](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/compile.yml)
[![AUTOTAG](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/autotag.yml/badge.svg)](https://github.com/oe-alliance-plugins/Jukebox/actions/workflows/autotag.yml)


[![Plugin Version](https://img.shields.io/github/v/tag/oe-alliance-plugins/Jukebox?label=Latest%20Version&color=darkviolet)](https://github.com/oe-alliance-plugins/Jukebox/tags)
[![Latest Release](https://img.shields.io/github/release-date/oe-alliance-plugins/Jukebox?label=From&color=darkviolet)](https://github.com/oe-alliance-plugins/Jukebox/releases/latest)
[![Github last commit](https://img.shields.io/github/last-commit/oe-alliance-plugins/Jukebox)](https://github.com/oe-alliance-plugins/Jukebox)
[![GitHub Activity](https://img.shields.io/github/commit-activity/y/oe-alliance-plugins/Jukebox.svg?label=commits)](https://github.com/oe-alliance-plugins/Jukebox/commits)
[![GitHub Activity](https://img.shields.io/github/commit-activity/m/oe-alliance-plugins/Jukebox.svg?label=commits)](https://github.com/oe-alliance-plugins/Jukebox/commits)

## SonarCloud status
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=oe-alliance-plugins_Jukebox&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=oe-alliance-plugins_Jukebox)
[![Vulnerabilities](https://sonarcloud.io/api/project_badges/measure?project=oe-alliance-plugins_Jukebox&metric=vulnerabilities)](https://sonarcloud.io/summary/new_code?id=oe-alliance-plugins_Jukebox)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=oe-alliance-plugins_Jukebox&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=oe-alliance-plugins_Jukebox)
[![Bugs](https://sonarcloud.io/api/project_badges/measure?project=oe-alliance-plugins_Jukebox&metric=bugs)](https://sonarcloud.io/summary/new_code?id=oe-alliance-plugins_Jukebox)
[![Code Smells](https://sonarcloud.io/api/project_badges/measure?project=oe-alliance-plugins_Jukebox&metric=code_smells)](https://sonarcloud.io/summary/new_code?id=oe-alliance-plugins_Jukebox)
[![Duplicated Lines (%)](https://sonarcloud.io/api/project_badges/measure?project=oe-alliance-plugins_Jukebox&metric=duplicated_lines_density)](https://sonarcloud.io/summary/new_code?id=oe-alliance-plugins_Jukebox)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=oe-alliance-plugins_Jukebox&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=oe-alliance-plugins_Jukebox)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=oe-alliance-plugins_Jukebox&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=oe-alliance-plugins_Jukebox)

[![SonarQube Cloud](https://sonarcloud.io/images/project_badges/sonarcloud-light.svg)](https://sonarcloud.io/summary/new_code?id=oe-alliance-plugins_Jukebox)

---

### License

Jukebox is licensed under the **GNU General Public License version 3**
(`GPL-3.0-only`). See [LICENSE.txt](LICENSE.txt).

---

### 🤝 Contributing & Contact

Jukebox is created by users for users and we welcome every contribution. There are no highly paid developers. There are only users who have seen a problem and done their best to fix it. This means Jukebox will always need the contributions of users like you. How can you get involved?

For questions or feedback, feel free and please open an issue or contribute with a Pull Request!

Pull requests are very welcome for:
- **Coding:** Developers can help by fixing a bug, adding new features, Integration improvements, Feature enhancements
- **Localization:** Translate into your native language.
- **Helping users:** Our support process relies on enthusiastic contributors like you to help others.

Your contribution is very welcome! Follow these steps:

1. 🍴 Fork this repository
2. 🔄 Create a branch for your feature
3. 💻 Make your changes
4. ✅ Commit using conventional messages
5. 📤 Push to your branch
6. 🔍 Open a Pull Request

Enjoy and help us improve it today. :)

### 🚨 Disclaimer

The project author is not responsible for how this software is used by others. It is not intended to be used for accessing or distributing copyrighted materials without authorization.
Users are solely responsible for determining the legality of their actions.

This repository has no control over the streams, links, or the legality of the content provided by the different hosts (including all mirror sites). It is the end user's responsibility to ensure the legal use of these streams, and we strongly recommend verifying that the content complies with all applicable laws, including copyright laws and regulations of your countrys jurisdiction before use.

---

### 🤝 Contributing Details

For detailed contributing guidelines including testing procedures and AI policy, please see [CONTRIBUTING.md](https://github.com/oe-alliance-plugins/.github/blob/main/docs/CONTRIBUTING.md).

---

⭐️ If you find this plugin useful, please give it a star on GitHub!
Thanks! ❤️ 💞 💖 ❤️‍🔥 💗
