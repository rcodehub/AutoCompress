# AutoCompress

A Docker container for Unraid (or any Docker host) that scans folders on a schedule and compresses every **sub-folder** into its own **RAR archive**:

- compression level, solid archives, dictionary size, recovery record, RAR threads, optional split volumes and optional password,
- tests each archive and compares it with the folder before anything is removed,
- adds a **CRC32** to the file name (`Show [14337E9B].rar`) or writes an **SFV** file next to the archive,
- then **deletes** the original folder, **moves** it to a trash folder, or keeps it.

Everything is set up in the web UI, which has a live status page, settings, scan history, statistics and themes.

> **Created entirely by AI (Claude).** It has been tested, but not in every situation. It can **delete your original folders**: try a **Dry run** first, keep backups of data you can't replace, and do your own due diligence before exposing the web UI to the internet.

## Install (Unraid)

Install **AutoCompress** from the **Apps** tab, or download the template by hand:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-AutoCompress.xml \
  https://raw.githubusercontent.com/rcodehub/Unraid-CA-Apps/main/templates/autocompress.xml
```

then **Docker → Add Container → Template: my-AutoCompress**.

> Keep only `my-AutoCompress.xml` in `templates-user`. If you also have another copy, such as `autocompress.xml`, Unraid may load it when you edit or update the container, and your paths go back to the defaults.

## Template settings

| Setting | Container path / variable | Description |
|---|---|---|
| Web UI port | `8080` (host default `8845`) | Web interface |
| App data | `/data` | Settings, history, statistics, logs. e.g. `/mnt/user/appdata/autocompress` |
| Folders to compress | `/source` | Every sub-folder of this folder becomes a `.rar`. Read/write. Add more folders with *Add another Path* and add them as sources in the web UI |
| Archive output (optional) | `/output` | If set, archives are written here instead of next to the folders. Must not be inside a source folder |
| Trash folder (optional) | `/trash` | If set, compressed folders are **moved** here instead of deleted. Must not be inside a source folder or a parent of one. Use the same kind of path as the sources (both `/mnt/user/…` or both `/mnt/diskN/…`) so the move is instant |
| Timezone | `TZ` | For times in the logs |
| UI username | `UI_USERNAME` | Login name, default `admin` |
| UI password | `UI_PASSWORD` | Turns on the login page. Leave blank for no login (then anyone who can reach the port can use the UI) |

Only change the *Host Path* side of the paths. On first start `/source` is set as the source; if `/output` is mapped the archives go there, and if `/trash` is mapped compressed folders are moved there. Everything else is configured in the web UI and saved in `/data`.

## Web UI

- **Status** – next/last scan, progress, what each job is doing (with percent), problems and a live log. Buttons: **Run now**, **Dry run** (shows what would happen without changing anything), **Compress folder…**, **Stop**, **Check CRCs**.
- **Settings** – all options below, with a folder picker and live checks that show each folder's real location on the server.
- **History** – every scan with counts, sizes and problems.
- **Stats** – lifetime totals (data compressed, space saved, success rate, speed), a 30-day chart, results per compression level and records.
- **Theme** – 11 colour themes with light and dark versions, plus corner rounding, spacing, font, shadows and page width. Use ☾/☀ to switch light/dark for your browser.

## Settings (web UI)

| Setting | Default | Description |
|---|---|---|
| Folders to compress | `/source` | Each sub-folder becomes one archive. Use container paths |
| Put the archives in | next to the folder | Optional output folder; *flat* or one sub-folder per source |
| Checksum | in the file name | `Show [CRC32].rar`, an SFV file (`Show.sfv`), or none. The CRC is that of the finished `.rar` file |
| Add checksum to existing .rar files | off | Renames existing archives (or writes an SFV). Parts of split sets are never renamed; they get an SFV |
| Keep the folder name inside the archive | on | Off stores only the contents |
| Level | 5 – Best | RAR `-m0`…`-m5` |
| Recovery record | 15 % | Lets RAR repair a damaged archive. 0 = none |
| Dictionary size | Auto | 32 MB – 1 GB. Bigger can be smaller but needs more memory per running folder |
| RAR threads | 0 (auto) | Threads used by each RAR |
| Split into volumes | 0 (off) | MB per volume: `Show.part1.rar`, `.part2.rar`… split sets always get an SFV instead of a CRC in the name |
| Solid archive | on | Smaller for many similar files; extracting one file reads more |
| Leave out of the archives | – | File/folder names with wildcards, matched at every level (`Thumbs.db`, `*.tmp`) |
| Password / encrypt file names | none / on | Stored in plain text in `/data/settings.json`, never sent to the browser. **A lost password means the archives cannot be opened** |
| Test the archive and compare it with the folder | on | `rar t`, plus a check that the archive holds as many files as the folder |
| The original folder | delete | Delete, move to the move folder, or keep |
| Move folder | `/trash` if mapped | Layout: `<move folder>/<source name>/<folder>`; name clashes get `.1`, `.2` |
| Sub-folders that are never compressed | `@eaDir`, `.Recycle.Bin`, `#recycle`, `.Trash*` | Wildcards allowed |
| Scan automatically / interval | off / 60 min | Interval counts from the end of the previous scan |
| Also scan at container start | off | |
| Folders at the same time | 1 | Each runs its own RAR, so memory multiplies |
| Skip folders newer than | 10 min | Leaves a folder alone if anything in it changed recently (copy in progress). 0 = off |
| Set permissions | on, 666 / 777 | For archives and SFV files created, and folders the app creates |
| Also set the owner | off, 99:100 | `nobody:users` on Unraid |

## How it works

For each source folder, every direct sub-folder is a candidate. Empty folders, folders on the skip list, symbolic links and folders that already have an archive of the same name (`name.rar`, `name [CRC].rar` or `name.part1.rar`) are skipped.

Each archive is built in a hidden work folder (`.autocompress-…`) next to its final place, then:

1. **tested** with `rar t`, and the number of files inside is compared with the folder (skipped when *Leave out of the archives* is used);
2. the **CRC32** is calculated and the archive is moved into place under its final name (or an SFV file is written);
3. the folder is checked again – **if anything in it changed while it was compressing, the archive is discarded and the folder is left alone**, to be tried again on the next scan;
4. only then is the folder deleted or moved.

Nothing is ever overwritten. A folder that fails is left untouched and listed with the reason; scheduled scans do not retry it until its contents change, **Run now** always does. Symbolic links inside a folder are stored as links, not followed.

**Compress folder…** is a one-off job that does not depend on the scanned folders or the schedule: choose any folder inside the container and compress it as one archive, or compress each of its sub-folders. It uses the compression settings from the Settings page; for that run you can override what happens to the folder afterwards (delete, move, keep) and the output folder. Folders you pick this way are compressed regardless of the exclude list and the minimum age. It cannot start while a scan is running.

**Check CRCs** recalculates the CRC of every archive that has one in its name or in an SFV file in the source/output folders and reports any that no longer match.

## RAR licence

The `rar` program is proprietary software by RARLAB (installed from Debian's non-free repository). It runs in trial mode until you register it. If you own a licence, put your `rarreg.key` in the app data folder (`/data`) and restart the container.

## Versions

The build number (top left of the web UI, and in the container log at start) goes up with every published image. Hover it to see the commit and build time. What changed is in [CHANGELOG.md](CHANGELOG.md), and the newest entries are also shown in Community Applications. Images are tagged `latest` and `build-N`, so you can pin a specific build by setting the repository to `ghcr.io/rcodehub/autocompress:build-N`.

## Docker Compose

See [`docker-compose.yml`](docker-compose.yml).

## License

MIT – see [LICENSE](LICENSE). (This covers AutoCompress itself, not the RAR program.)
