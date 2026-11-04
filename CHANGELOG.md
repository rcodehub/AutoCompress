# Changelog

Newest first. The latest entries are copied into the Unraid template (shown in Community Applications) each time an image is published.

### 2026-11-04
- New optional path **All shares** (`/mnt/user`, under advanced settings in the template): with it mapped, the folder pickers and **Compress folder…** can reach any share, not only the mapped folders.
- New **Compress folder…** button: pick any folder and compress it now as a one-off job, either as a single archive or as one archive per sub-folder. It ignores the scanned folders and the schedule, uses your compression settings, and lets you choose for that run only what happens to the folder afterwards and where the archive goes.

### 2026-11-03
- First release: every sub-folder of the folders you choose is compressed into its own RAR archive on a schedule or on demand.
- Settings for the compression level, solid archives, dictionary size, recovery record, RAR threads, split volumes, password (with optional file name encryption) and files to leave out.
- Each archive is tested and compared with the folder before anything is removed; a folder that changed while it was being compressed is never touched.
- CRC32 in the file name (`Show [14337E9B].rar`) or in an SFV file, for new and for existing archives; a Check CRCs button verifies them later.
- Original folders can be deleted, moved to a trash folder or kept; archives can go next to the folders or to a separate output folder.
- Web UI with live status, settings, history, statistics, 11 themes, optional login and a build number.
