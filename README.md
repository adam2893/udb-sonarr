# UDB-Sonarr

Automatically download missing episodes from Asian streaming sites (KissKh, AnimePahe, Asiaflix) straight into your Sonarr library.

UDB-Sonarr is a fork of [UDB](https://github.com/Prudhvi-pln/udb) (Ultimate-Download-Bot) that adds a **Sonarr integration daemon**. Instead of running UDB's interactive CLI, it polls Sonarr for monitored series with missing episodes, searches the site clients, downloads the episodes into Sonarr's series folders, and triggers a rescan so Sonarr imports them.

## How it works

```
Sonarr (monitored series + missing episodes)
   ▲                    │
   │  ① polls API       │  ④ drops files into series folder
   │                    ▼
┌──┴───────────────────────────────┐
│   udb_sonarr daemon               │
│   ② search KissKh → AnimePahe →   │
│      Asiaflix (in order, fallback)│
│   ③ download via UDB pipeline     │
│      or yt-dlp backend            │
└──────────────────────────────────┘
   │
   ▼  ⑤ POST /command RescanSeries
Sonarr imports, renames, moves into library
```

It acts as a **sidecar**, not a Sonarr indexer/download client — it monitors what Sonarr is missing, fetches it from sites that have no torrent/usenet releases, and hands Sonarr finished files for import.

## Supported sources

| Client    | Content                     | Method                              | DRM |
|-----------|-----------------------------|-------------------------------------|-----|
| KissKh    | Asian drama / movies / anime | JSON API + quickjs kkey generation  | No  |
| AnimePahe | Anime                       | JSON API + undetected Chrome        | No  |
| Asiaflix  | Asian drama / movies        | Server-rendered HTML + embed hosts  | No  |

The daemon tries each configured client in order until one has the series. Client order is configurable (`site_client: all | kisskh | [kisskh, asiaflix]`).

## Features

- **Sonarr API integration** — reads monitored series + missing episodes, triggers `RescanSeries` after downloads
- **Smart series matching** — fuzzy title + year matching, per-season episode offset mapping for multi-season shows
- **Resilient KissKh client** — kkey token caching, refresh-on-failure retry, optional [KissKH-Api](https://github.com/beorgsh/KissKH-Api) microservice fallback for Cloudflare blocks
- **Two downloader backends** — UDB's native HLS/MP4 pipeline (AES + encrypted-subtitle support), or a [kisskh-dl](https://github.com/debakarr/kisskh-dl)-style **yt-dlp** wrapper (`downloader_type: yt-dlp`) that also handles embed hosts (streamtape/mixdrop/vidmoly)
- **Dry-run mode** — see what would download before committing

## Requirements

- Python 3.8+
- `ffmpeg` on PATH (used by the HLS downloader)
- Chrome/Chromium (AnimePahe client)
- A running [Sonarr](https://sonarr.tv/) instance (v3 or v4)

## Quick start (native)

```bash
git clone https://github.com/adam2893/udb-sonarr.git
cd udb-sonarr
pip install -r requirements.txt

# ffmpeg required:
#   macOS:  brew install ffmpeg
#   Ubuntu: sudo apt install ffmpeg

cp config_sonarr.yaml.example config_sonarr.yaml
# edit config_sonarr.yaml: SonarrConfig.url + SonarrConfig.api_key
#   (Sonarr API key: Settings → General → API Key)

# Dry run first — lists missing episodes, downloads nothing:
python3 udb_sonarr.py -c config_sonarr.yaml --once --dry-run

# Real run (single pass):
python3 udb_sonarr.py -c config_sonarr.yaml --once

# Continuous daemon (default: polls every 30 min):
python3 udb_sonarr.py -c config_sonarr.yaml
```

## Docker

```bash
mkdir -p config downloads logs
cp config_sonarr.yaml.example config/config_sonarr.yaml
# edit config/config_sonarr.yaml, then:
docker compose up -d --build
```

- Mounts the `config/` directory at `/config` (contains `config_sonarr.yaml`)
- Mounts your TV library at `/tv` — **must match the path Sonarr uses**, so downloaded episodes land where Sonarr can import them
- Uses host networking so `localhost:8989` reaches a Sonarr running on the host
- If Sonarr runs in Docker on a shared network instead, comment out `network_mode: host` and use the `networks:` section + `http://sonarr:8989` in config

See `docker-compose.yml` for both networking options.

Pre-built image: `ghcr.io/adam2893/udb-sonarr:latest` (built automatically by CI on every push to `main`).

### Unraid (GUI setup)

An Unraid template is included at [`unraid/udb-sonarr.xml`](unraid/udb-sonarr.xml). Install it via the Apps tab, or drop it into Unraid's user templates folder:

```bash
# From the Unraid terminal (or via SMB), install the template:
cp unraid/udb-sonarr.xml /boot/config/plugins/dockerMan/templates-user/
```

Then in the Unraid web UI:

1. **Apps → Search "udb-sonarr"** → install the template (or **Docker → Add Container** and select *udb-sonarr* from the template dropdown).
2. **Edit the template** — set these paths (click the folder icons):
   - **Config path** → `/mnt/user/appdata/udb-sonarr/config` (create it: `mkdir -p /mnt/user/appdata/udb-sonarr/config`)
   - **TV library** → `/mnt/user/media/tv` — **must be the same path your Sonarr container uses for media** (e.g. if Sonarr maps `/tv` from `/mnt/user/media/tv`, use `/mnt/user/media/tv` here so files land in the right place)
   - **Downloads** → `/mnt/user/appdata/udb-sonarr/downloads`
   - **Logs** → `/mnt/user/appdata/udb-sonarr/logs`
   - **Timezone** → your TZ (e.g. `Europe/London`)
3. **Before starting the container**, create the config file on the Unraid console:
   ```bash
   mkdir -p /mnt/user/appdata/udb-sonarr/config
   cp /boot/config/plugins/dockerMan/templates-user/udb-sonarr.xml /dev/null 2>/dev/null; true
   # Fetch the example config:
   curl -o /mnt/user/appdata/udb-sonarr/config/config_sonarr.yaml \
     https://raw.githubusercontent.com/adam2893/udb-sonarr/main/config_sonarr.yaml.example
   # Edit it (nano is available on Unraid):
   nano /mnt/user/appdata/udb-sonarr/config/config_sonarr.yaml
   ```
   Set at minimum `SonarrConfig.url` (`http://localhost:8989` — host networking reaches Sonarr on the host) and `SonarrConfig.api_key` (Sonarr → Settings → General → API Key).
4. **Apply** the template → Unraid pulls the image and starts the container.
5. **Verify** — check the container log (Docker → click the container → Log). You should see `UDB-Sonarr daemon started` and a `Poll cycle started` line. First run will error if Sonarr is unreachable — fix the URL/API key in the config and restart the container.

Notes:
- The template uses **host networking** so the container can reach Sonarr via `localhost:8989` (and the media path is shared directly with the array). If you changed Sonarr's port, set `url` accordingly.
- If Sonarr runs in its own container on a custom Unraid network (e.g. `br0` or a custom bridge), change the template's network type to that network and set `SonarrConfig.url` to Sonarr's container name (e.g. `http://sonarr:8989`).

## Configuration

All settings live in `config_sonarr.yaml` (copy from `config_sonarr.yaml.example`).

Key options:

```yaml
SonarrConfig:
  url: http://localhost:8989
  api_key: YOUR_SONARR_API_KEY
  quality: 1080                    # 360 / 480 / 720 / 1080
  downloader_type: udb             # udb | yt-dlp
  site_client: all                 # all | kisskh | animepahe | asiaflix | [list]
  poll_interval_minutes: 30
  season_mappings: {}              # multi-season episode offset overrides
```

**Multi-season shows:** KissKh/Asiaflix use flat episode numbering while Sonarr uses S01E05. Single-season shows map 1:1 automatically. For multi-season shows, add `season_mappings` keyed by Sonarr series ID:

```yaml
SonarrConfig:
  season_mappings:
    123:          # Sonarr series ID (from the series URL)
      1: 0        # Season 1 starts at site ep 1
      2: 16       # Season 2 starts at site ep 17
```

## CLI

```
usage: udb_sonarr.py [-h] [-c CONF] [-D] [-l LOG_FILE] [-v] [--once] [--dry-run] [-dc] [--skip-update-check]

  -c, --conf CONF       configuration file (default: config_sonarr.yaml)
  -D, --debug           enable debug logging
  -l, --log-file FILE   custom log file name
  -v, --version         show version
  --once                run a single poll cycle then exit
  --dry-run             check for missing episodes but do not download
  -dc, --disable-colors disable colored output
```

## Credits

- [UDB](https://github.com/Prudhvi-pln/udb) — the underlying downloader framework and site clients
- [kisskh-dl](https://github.com/debakarr/kisskh-dl) — yt-dlp downloader pattern and subtitle decryption keys
- [KissKH-Api](https://github.com/beorgsh/KissKH-Api) — optional Cloudflare-bypass fallback microservice

## License

MIT (see LICENSE.md)

> **Note:** This tool interfaces with third-party streaming sites. Use responsibly and respect applicable copyright laws in your jurisdiction.
