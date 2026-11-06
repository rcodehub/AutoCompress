# Changelog

Newest first. The latest entries are copied into the Unraid template (shown in Community Applications) each time an image is published.

### 2026-11-06
- The folder pickers now only show the folders mapped into the container. They open on a list of those folders (without the app data folder), cannot browse anywhere else, and hide links that lead out of them. **Compress folder…** refuses a folder outside them.
- New **Cancel** button on every folder that is being compressed. It cancels just that folder: the unfinished archive is deleted, the folder is left exactly as it was and the other folders carry on. Scheduled scans leave a cancelled folder alone until it changes; Run now tries it again. Stop still ends the whole run.
- **Compress folder…** now scans the folder you pick: if it has sub-folders, each sub-folder is compressed into its own archive (instead of the whole folder as one archive). A folder without sub-folders is compressed as a whole.

### 2026-11-04
- Fix: files dated in the future (for example from a timezone mix-up on the computer that wrote them) no longer keep a folder waiting for hours. Their date is ignored and the time they arrived on the server is used. The Waiting card and the log now name the newest changed file, and warn about future-dated items.
- New **Waiting** card on the Status page: folders skipped because they changed too recently are listed with their size and a countdown to when they are old enough, soonest first. It stays until the next scan starts.
- The Status page now shows what happens to the original folders (Recycle Bin, delete, move or keep) and where: the Recycle Bin folder on your server and how many items it holds. The log names the Recycle Bin folder at the start of a run and for every folder moved there, and Stats has a Recycle Bin tile.
- The folder locations box in Settings is now live: it follows the folders you type or pick (sources, output, move folder, Recycle Bin) and shows where each one is on your server, instead of only checking the four template paths.
- The original folder now goes to the share's **`.Recycle.Bin`** by default (the place the Unraid Recycle Bin plugin uses for files deleted over SMB). Delete, move and keep are still available; existing settings are unchanged. If the share's root is not mapped into the container, Settings says so and a fresh install keeps the folders instead.
- New optional path **All shares** (`/mnt/user`, under advanced settings in the template): with it mapped, the folder pickers and **Compress folder…** can reach any share, not only the mapped folders.
- New **Compress folder…** button: pick any folder and compress it now as a one-off job, either as a single archive or as one archive per sub-folder. It ignores the scanned folders and the schedule, uses your compression settings, and lets you choose for that run only what happens to the folder afterwards and where the archive goes.

### 2026-11-03
- First release: every sub-folder of the folders you choose is compressed into its own RAR archive on a schedule or on demand.
- Settings for the compression level, solid archives, dictionary size, recovery record, RAR threads, split volumes, password (with optional file name encryption) and files to leave out.
- Each archive is tested and compared with the folder before anything is removed; a folder that changed while it was being compressed is never touched.
- CRC32 in the file name (`Show [14337E9B].rar`) or in an SFV file, for new and for existing archives; a Check CRCs button verifies them later.
- Original folders can be deleted, moved to a trash folder or kept; archives can go next to the folders or to a separate output folder.
- Web UI with live status, settings, history, statistics, 11 themes, optional login and a build number.
