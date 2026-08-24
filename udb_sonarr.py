#!/usr/bin/env python3
__author__ = 'UDB-Sonarr Fork'

'''
UDB-Sonarr: Daemon that polls Sonarr for missing episodes and downloads
them via UDB's site clients (KissKh, etc.) automatically.

This replaces UDB's interactive CLI with a Sonarr-driven polling loop.
The download infrastructure (BaseClient, HLSDownloader, etc.) is reused as-is.

Usage:
    python udb_sonarr.py                          # Run with default config
    python udb_sonarr.py -c config_sonarr.yaml    # Run with custom config
    python udb_sonarr.py --once                   # Run a single check cycle then exit
    python udb_sonarr.py --dry-run                # Check but don't download
    python udb_sonarr.py -D                       # Debug logging
'''

import argparse
import os
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Any
import os

# Set permissive umask so created dirs (0775) and files (0664) are
# group-writable — Sonarr (which may run as a different user) can then
# edit/move/delete files the daemon creates.
os.umask(0o002)

# UDB internals
from Utils.commons import (
    colprint_init, colprint, PRINT_THEMES, ExitException,
    create_logger, load_yaml, pretty_time, strip_ansi,
    threaded, delete_old_logs, get_ffmpeg_version, DownloadController
)

# Sonarr integration
from Clients.SonarrClient import SonarrClient
from Clients.SeriesMatcher import SeriesMatcher

# UDB site clients (imported lazily in init_site_client to avoid requiring
# all dependencies just for --help / --version)

__version__ = '0.1.0'
get_current_time = lambda fmt='%F %T': datetime.now().strftime(fmt)
VALID_FFMPEG_VERSION = (7, 1, 1)


class UDBSonarrDaemon:
    '''
    Main daemon controller. Polls Sonarr for missing episodes,
    downloads them via site clients, and triggers Sonarr rescans.
    '''

    def __init__(self, config: Dict[str, Any], args):
        self.config = config
        self.args = args
        self.logger = None
        self.sonarr = None
        self.matcher = None
        self.site_clients: Dict[str, Any] = {}
        self.disable_colors = args.disable_colors
        self.dry_run = args.dry_run
        self.once = args.once

        # Sonarr config — env vars (Unraid GUI container variables) override YAML
        self.config.setdefault('SonarrConfig', {})
        sonarr_config = self.config['SonarrConfig']
        env_overrides = {
            'UDB_SONARR_URL': 'url',
            'UDB_API_KEY': 'api_key',
            'UDB_QUALITY': 'quality',
            'UDB_DOWNLOADER_TYPE': 'downloader_type',
            'UDB_SITE_CLIENT': 'site_client',
            'UDB_POLL_INTERVAL_MINUTES': 'poll_interval_minutes',
            'UDB_ROOT_FOLDER': 'root_folder',
            'UDB_TAGS': 'tags',
            'UDB_TMDB_API_KEY': 'tmdb_api_key',
        }
        for env_key, cfg_key in env_overrides.items():
            if os.environ.get(env_key):
                sonarr_config[cfg_key] = os.environ[env_key]

        self.poll_interval = int(sonarr_config.get('poll_interval_minutes', 30)) * 60

        # File ownership: chown created dirs/files to this UID/GID so Sonarr
        # (which may run as a different user, e.g. 'nobody' on Unraid) can
        # edit/move/delete them. Defaults to nobody:users (99:100) — the
        # standard Unraid user. Override with PUID/PGID env vars.
        self.puid = int(os.environ.get('PUID', 99))
        self.pgid = int(os.environ.get('PGID', 100))

        # Only process series that carry at least one of these Sonarr tags.
        # Empty list = no filtering. Accepts YAML list or comma-separated env
        # value (e.g. UDB_TAGS=asiandrama,kdrama).
        tags_cfg = sonarr_config.get('tags', [])
        if isinstance(tags_cfg, str):
            tags_cfg = [t.strip() for t in tags_cfg.split(',') if t.strip()]
        self.filter_tags = [str(t).strip().lower() for t in (tags_cfg or []) if str(t).strip()]

        # Downloader config (reused from UDB)
        self.downloader_config = config.get('DownloaderConfig', {})
        self.downloader_config.setdefault('max_parallel_downloads', 2)
        self.downloader_config.setdefault('download_dir', '/tmp/udb-sonarr')
        self.downloader_config.setdefault('temp_download_dir', 'auto')
        self.downloader_config.setdefault('concurrency_per_file', 'auto')
        self.downloader_config.setdefault('request_timeout', 30)

        # Quality preference(s) — single value or comma-separated list.
        # e.g. "1080" or "1080,720" (prefer 1080, fall back to 720).
        # Accepts YAML list or comma-separated env value (UDB_QUALITY).
        quality_cfg = sonarr_config.get('quality', '1080')
        if isinstance(quality_cfg, list):
            quality_cfg = [str(q).strip() for q in quality_cfg]
        else:
            quality_cfg = [q.strip() for q in str(quality_cfg).split(',')]
        self.qualities = [q for q in quality_cfg if q]
        if not self.qualities:
            self.qualities = ['1080']
        # primary quality kept for display/back-compat
        self.quality = self.qualities[0]

        # Downloader backend: 'udb' (UDB's HLSDownloader/BaseDownloader) or
        # 'yt-dlp' (kisskh-dl-style yt-dlp wrapper — more robust, needs yt-dlp installed)
        self.downloader_type = sonarr_config.get('downloader_type', 'udb').lower()
        if self.downloader_type not in ('udb', 'yt-dlp'):
            colprint('error', f'Unknown downloader_type: {self.downloader_type}. Valid: udb, yt-dlp')
            raise ExitException(1)

        # Which site clients to use — can be a single name or a list.
        # The daemon tries each in order until one has the series.
        # Options: kisskh, animepahe, asiaflix
        # Default: all active clients
        site_client_config = sonarr_config.get('site_client', 'all')
        if isinstance(site_client_config, str):
            if site_client_config.lower() == 'all':
                self.site_client_names = ['kisskh', 'animepahe', 'asiaflix']
            elif ',' in site_client_config:
                # e.g. env UDB_SITE_CLIENT="kisskh,asiaflix"
                self.site_client_names = [s.strip().lower() for s in site_client_config.split(',') if s.strip()]
            else:
                self.site_client_names = [site_client_config.lower()]
        elif isinstance(site_client_config, list):
            self.site_client_names = [s.lower() for s in site_client_config]
        else:
            self.site_client_names = ['kisskh']

        # Track downloads to avoid re-downloading in same session
        self.completed_downloads: Dict[str, bool] = {}

    def init_logging(self):
        '''Initialize logging using UDB's logger system.'''
        log_config = self.config.get('LoggerConfig', {})
        log_config['log_file_name'] = self.args.log_file or f'udb_sonarr_{get_current_time("%Y%m%d%H%M%S")}.log'
        if self.args.debug:
            log_config['log_level'] = 'DEBUG'
        self.logger = create_logger(**log_config)
        self.logger.info(f'--- UDB-Sonarr v{__version__} started ---')
        self.logger.info(f'CLI args: {self.args}')

        # Clean old logs
        delete_old_logs(
            log_config.get('log_dir', 'logs'),
            log_config.get('log_retention_days', 7),
            log_config.get('log_backup_count', 3)
        )

    def check_ffmpeg(self):
        '''Verify ffmpeg is installed and version is sufficient.'''
        ffmpeg_version = get_ffmpeg_version()
        if not ffmpeg_version:
            colprint('error', 'ffmpeg not found in PATH. Install ffmpeg to proceed.')
            raise ExitException(1)
        elif ffmpeg_version < VALID_FFMPEG_VERSION:
            self.logger.warning(
                f'ffmpeg version {".".join(map(str, ffmpeg_version))} is below recommended '
                f'{".".join(map(str, VALID_FFMPEG_VERSION))}. Some features may not work.'
            )
        else:
            self.logger.debug(f'ffmpeg version: {".".join(map(str, ffmpeg_version))}')

    def init_sonarr(self):
        '''Initialize Sonarr API client and test connection.'''
        sonarr_config = self.config.get('SonarrConfig', {})
        if not sonarr_config.get('url') or not sonarr_config.get('api_key'):
            colprint('error', 'SonarrConfig.url and SonarrConfig.api_key are required in config')
            raise ExitException(1)

        self.sonarr = SonarrClient(sonarr_config)
        if not self.sonarr.test_connection():
            colprint('error', 'Cannot connect to Sonarr. Check URL and API key in config.')
            raise ExitException(1)

    def init_matcher(self):
        '''Initialize series/episode matcher + optional TMDB alias lookup.'''
        sonarr_config = self.config.get('SonarrConfig', {})
        matcher_config = {
            'season_mappings': sonarr_config.get('season_mappings', {}),
            'match_threshold': sonarr_config.get('match_threshold', 0.6),
            'verify_year': sonarr_config.get('verify_year', True),
            'verify_country': sonarr_config.get('verify_country', True),
        }
        self.matcher = SeriesMatcher(matcher_config)

        # Optional TMDB integration: resolves alternate/original titles so the
        # matcher can find shows whose site title differs from Sonarr's.
        # Requires a free TMDB API key (https://www.themoviedb.org/settings/api)
        tmdb_api_key = sonarr_config.get('tmdb_api_key', '')
        self.tmdb_client = None
        if tmdb_api_key:
            from Clients.TmdbClient import TmdbClient
            self.tmdb_client = TmdbClient(tmdb_api_key)
            self.logger.info('TMDB alternate-title lookup enabled')

    # Map client names to their UDB config keys and import paths
    CLIENT_REGISTRY = {
        'kisskh': {
            'config_key': 'Anime, Drama, Movies & TV Shows (Kisskh)',
            'import_path': 'Clients.KissKhClient',
            'class_name': 'KissKhClient'
        },
        'animepahe': {
            'config_key': 'Anime (Animepahe)',
            'import_path': 'Clients.AnimePaheClient',
            'class_name': 'AnimePaheClient'
        },
        'asiaflix': {
            'config_key': 'Asian Dramas & Movies (Asiaflix)',
            'import_path': 'Clients.AsiaflixClient',
            'class_name': 'AsiaflixClient'
        }
    }

    def init_site_client(self):
        '''Initialize all configured site clients for downloading.'''
        for client_name in self.site_client_names:
            if client_name not in self.CLIENT_REGISTRY:
                colprint('error', f'Unknown site client: {client_name}')
                continue

            registry = self.CLIENT_REGISTRY[client_name]
            client_config = dict(self.config.get(registry['config_key'], {}))

            # Merge with downloader config for download dir
            if 'download_dir' not in client_config:
                client_config['download_dir'] = self.downloader_config['download_dir']
            client_config['request_timeout'] = self.downloader_config.get('request_timeout', 30)
            client_config['daemon_mode'] = True

            try:
                # Lazy import
                import importlib
                module = importlib.import_module(registry['import_path'])
                client_class = getattr(module, registry['class_name'])
                self.site_clients[client_name] = client_class(client_config)
                self.logger.info(f'{client_name} client initialized')
            except Exception as e:
                self.logger.error(f'Failed to initialize {client_name} client: {e}')
                colprint('error', f'Failed to initialize {client_name}: {e}')

        if not self.site_clients:
            colprint('error', 'No site clients could be initialized. Check config and dependencies.')
            raise ExitException(1)

    def find_series_on_clients(self, series_title: str, sonarr_series: Dict):
        '''
        Try each configured site client to find the series.
        Returns: (client_name, client_instance, matched_series_dict, variant_series_list) or None.
        variant_series_list holds additional above-threshold matches for the
        same series — used when a site splits seasons into separate entries
        (e.g. Asiaflix lists "X" and "X Season 2" separately). Empty for
        sites that keep one series (KissKh). Tries clients in order; returns
        first client with a match.
        '''
        matches = []
        for client_name in self.site_client_names:
            client = self.site_clients.get(client_name)
            if not client:
                continue

            # KissKh's search API is literal-ish: 'q=Us' does NOT return
            # "Us (2025)", but 'q=Us 2025' does. If a title-only search
            # fails to match, retry with "<title> <year>" appended.
            queries = [series_title]
            year = str(sonarr_series.get('year', '')).strip()
            if year and year.isdigit():
                queries.append(f'{series_title} {year}')

            search_results = None
            for query in queries:
                self.logger.debug(f'Searching for [{query}] on {client_name}')
                try:
                    search_results = client.search(query)
                except Exception as e:
                    self.logger.warning(f'{client_name} search failed for [{query}]: {e}')
                    search_results = None
                    continue

                if not search_results:
                    self.logger.debug(f'No results on {client_name} for [{query}]')
                    continue

                # Optional: fetch TMDB alternate titles to widen the match
                # (helps when the site's title differs from Sonarr's, e.g. Thai BL)
                extra_titles = []
                sonarr_synopsis = ''
                if self.tmdb_client:
                    try:
                        extra_titles = self.tmdb_client.get_series_aliases(
                            tmdb_id=sonarr_series.get('tmdbId'),
                            tvdb_id=sonarr_series.get('tvdbId'),
                        )
                        sonarr_synopsis = self.tmdb_client.get_series_overview(
                            tmdb_id=sonarr_series.get('tmdbId'),
                            tvdb_id=sonarr_series.get('tvdbId'),
                        )
                    except Exception as e:
                        self.logger.debug(f'TMDB lookup failed for [{series_title}]: {e}')

                # Post-filter by country: drop results from wrong regions before scoring.
                # KissKh's search API doesn't support country filtering, so we filter
                # the results client-side. Only filter when both sides have country data
                # — if Sonarr has no country or the result has no country, skip the check
                # (don't be too aggressive).
                sonarr_country = sonarr_series.get('countryCode') or sonarr_series.get('country') or ''
                if sonarr_country and search_results:
                    filtered = {}
                    dropped = 0
                    for idx, result in search_results.items():
                        result_country = result.get('country') or ''
                        if result_country and not SeriesMatcher._countries_match(sonarr_country, result_country):
                            self.logger.debug(
                                f'Post-filter: dropping [{result.get("title")}] ({result_country}) '
                                f'— doesn\'t match Sonarr country ({sonarr_country})'
                            )
                            dropped += 1
                            continue
                        filtered[idx] = result
                    if dropped:
                        self.logger.info(
                            f'Post-filter: dropped {dropped} result(s) from wrong region(s) '
                            f'(Sonarr country: {sonarr_country})'
                        )
                    search_results = filtered

                # Score all results; collect every above-threshold match.
                # The best becomes the primary; the rest are "variants"
                # (season-split entries) if they belong to the same show.
                # Qualification is tiered: raw title alone above 0.8, or
                # marginal (0.6-0.8) confirmed by year + country — blocks
                # false matches like "Temporary Mom" -> "Angry Mom" (2011).
                scored = self.matcher.score_all_results(
                    sonarr_series, search_results,
                    extra_titles=extra_titles, sonarr_synopsis=sonarr_synopsis,
                )
                above = [(score, idx, res, raw) for score, idx, res, raw in scored
                         if self.matcher.is_qualified(sonarr_series, res, raw, sonarr_synopsis)]
                if not above:
                    self.logger.debug(f'No match on {client_name} for query [{query}], trying next query')
                    continue

                score, idx, primary, raw = above[0]
                self.logger.info(f'Found [{series_title}] on {client_name} -> [{primary.get("title")}] (raw title: {raw:.2f})')
                colprint('results',
                         f'  MATCHED [{series_title}] on {client_name} -> [{primary.get("title")}] '
                         f'({primary.get("country", "?")}, {primary.get("year", "?")}, raw title {raw:.2f})')

                # Detect season-split variants ("X" + "X Season 2"). Variants
                # are scored against BOTH the primary's title and the Sonarr
                # title — a "Season 2" entry may share words with the Sonarr
                # title but not with the primary's full title (e.g. Sonarr
                # "Apple" vs primary "Apple My Love" vs variant "(Your) Apple
                # Season 2" — the variant matches Sonarr but not the primary).
                variants = []
                sonarr_norm = self.matcher._normalize_title(series_title)
                primary_norm = self.matcher._normalize_title(primary.get('title', ''))
                self.logger.info(
                    f'  Variant detection: sonarr_norm="{sonarr_norm}", '
                    f'primary_norm="{primary_norm}", '
                    f'scored_count={len(scored)}, match_threshold={self.matcher.match_threshold}'
                )
                for v_score, v_idx, v_res, v_raw in scored:
                    if (v_idx, v_res) == (idx, primary):
                        continue
                    v_norm = self.matcher._normalize_title(v_res.get('title', ''))
                    v_sim_primary = self.matcher._similarity(primary_norm, v_norm)
                    v_sim_sonarr = self.matcher._similarity(sonarr_norm, v_norm)
                    
                    # Stricter variant detection: require either
                    # 1. Season marker in title (e.g. "Season 2", "Part 2", "S2")
                    # 2. High similarity with multiple shared words (not just single-word containment)
                    has_season_marker = self.matcher._title_matches_season(v_res.get('title', ''), 2) or \
                                        self.matcher._title_matches_season(v_res.get('title', ''), 3) or \
                                        self.matcher._title_matches_season(v_res.get('title', ''), 4)
                    
                    # Word overlap check: require at least 2 meaningful words shared
                    # (prevents single-word containment like "Runaway" matching "Runaway Healer")
                    sonarr_words = set(sonarr_norm.split()) - SeriesMatcher.COMMON_WORDS
                    v_words = set(v_norm.split()) - SeriesMatcher.COMMON_WORDS
                    word_overlap = len(sonarr_words & v_words) / max(len(sonarr_words | v_words), 1)
                    min_shared_words = 2
                    
                    is_variant = False
                    if has_season_marker:
                        is_variant = True
                    elif v_sim_sonarr >= 0.75 and word_overlap >= 0.3 and len(sonarr_words & v_words) >= min_shared_words:
                        # High similarity with meaningful word overlap (not just single-word containment)
                        is_variant = True
                    elif v_sim_primary >= 0.75 and word_overlap >= 0.3 and len(sonarr_words & v_words) >= min_shared_words:
                        is_variant = True
                    
                    self.logger.info(
                        f'  Variant check: [{v_res.get("title")}] -> '
                        f'v_norm="{v_norm}", vs_primary={v_sim_primary:.2f}, '
                        f'vs_sonarr={v_sim_sonarr:.2f}, word_overlap={word_overlap:.2f}, '
                        f'has_season_marker={has_season_marker}, is_variant={is_variant}'
                    )
                    if is_variant:
                        variants.append(v_res)

                if variants:
                    self.logger.info(
                        f'  {len(variants)} additional match(es) on {client_name}: '
                        f'{[v.get("title") for v in variants]} (treating as season variants)'
                    )
                matches.append((client_name, client, primary, variants))

        if not matches:
            return None

        # Choose client: prefer KissKh if available (user requests it), even
        # though its episodes are split into parts (8.1, 8.2, 8.3, 8.4). The
        # split parts will be merged via ffmpeg before import.
        chosen = None
        for client_name, client, primary, variants in matches:
            if client_name == 'kisskh':
                chosen = (client_name, client, primary, variants)
                break
        if chosen is None and matches:
            chosen = matches[0]
        if chosen is None:
            return None

        client_name, client, primary, variants = chosen
        # Fetch episodes list (will be split for KissKh)
        try:
            episodes = client.fetch_episodes_list(primary)
        except Exception as e:
            self.logger.error(f'Failed to fetch episode list for [{series_title}] from {client_name}: {e}')
            return (client_name, client, primary, variants, None)

        if self._is_split_episodes(episodes):
            self.logger.info(
                f'KissKh split episodes detected for [{series_title}]; '
                f'will merge 4 parts (8.1-8.4) via ffmpeg before import'
            )
        return (client_name, client, primary, variants, episodes)

    def _merge_kisskh_split_episodes(self, client, series, split_episodes,
                                         site_episodes, series_path):
        '''Download all 4 KissKh split episode parts (8.1-8.4), merge them
        with ffmpeg into one file named per Sonarr convention (S01E08.mkv),
        then clean up the part files.

        Returns True on success, False on failure.
        '''
        import subprocess
        import os
        import time as _time

        series_title = series.get('title', '')
        season_num = series.get('seasonNumber', 1)

        if not split_episodes:
            return False

        # Identify the 4 parts: KissKh episode numbers are 8.1, 8.2, 8.3, 8.4
        # (returned as float by KissKhClient, not string)
        parts = {}
        for ep in split_episodes:
            ep_no = ep.get('episode', '')
            try:
                ep_str = str(ep_no)
                if '.' not in ep_str:
                    continue
                part = int(ep_str.split('.')[-1])
            except (ValueError, AttributeError):
                continue
            parts[part] = ep

        required = [1, 2, 3, 4]
        if not all(p in parts for p in required):
            self.logger.warning(
                f'Missing KissKh split parts for [{series_title}]; '
                f'found: {sorted(parts.keys())}, needed: {required}'
            )
            return False

        # Determine the integer episode number (8.1 -> 8)
        first_ep_no = parts[1].get('episode', '1')
        try:
            ep_num = int(float(first_ep_no))
        except (ValueError, TypeError):
            ep_num = 1

        # Build the Sonarr season folder path
        season_folder = os.path.join(series_path, f'Season {season_num:02d}')
        os.makedirs(season_folder, exist_ok=True)

        # Download each part individually using the site client
        part_files = []
        ep_ranges = {}  # Not used for batch; we download one at a time
        for part_num in sorted(parts.keys()):
            ep_dict = parts[part_num]
            ep_no_str = ep_dict.get('episode', '')
            self.logger.info(f'  Downloading split part {part_num}/4 (ep {ep_no_str}) for [{series_title}]')

            # Build a fake sonarr_ep with the part episode number so
            # download_episode generates a unique temp filename per part
            part_sonarr_ep = {
                'seasonNumber': season_num,
                'episodeNumber': ep_num,
                'title': f'{series_title} Part {part_num}',
            }
            # Build ep_ranges for just this one part
            ep_ranges_part = {
                'start': float(ep_no_str),
                'end': float(ep_no_str),
                'specific_no': []
            }

            try:
                download_links = client.fetch_episode_links(site_episodes, ep_ranges_part)
                ep_float = float(ep_no_str)
                if not download_links or ep_float not in download_links:
                    self.logger.error(f'No download links for split part {ep_no_str}')
                    return False

                ep_links = download_links[ep_float]
                if 'error' in ep_links:
                    self.logger.error(f'Site error for part {ep_no_str}: {ep_links["error"]}')
                    return False

                available_res = [k for k in ep_links.keys() if k not in ('error', 'original')]
                if not available_res:
                    self.logger.error(f'No resolutions for part {ep_no_str}')
                    return False

                selected_res = None
                for q in self.qualities:
                    if q in available_res:
                        selected_res = q
                        break
                if not selected_res:
                    selected_res = client._resolution_selector(
                        available_res, self.qualities[0], client.selector_strategy
                    )
                if not selected_res:
                    selected_res = available_res[0]

                res_data = ep_links.get(selected_res)
                if not res_data or 'downloadLink' not in res_data:
                    self.logger.error(f'No download link for part {ep_no_str} res {selected_res}')
                    return False

                download_link = res_data['downloadLink']
                download_type = res_data.get('downloadType', 'hls')

                # Use a temp filename for each part
                part_filename = f'_kisskh_part_{part_num}.mp4'
                part_path = os.path.join(season_folder, part_filename)

                ep_details = {
                    'episodeName': part_filename,
                    'downloadLink': download_link,
                    'downloadType': download_type,
                    'season': season_num,
                    'type': 'tv'
                }
                if 'audioLink' in res_data and res_data['audioLink']:
                    ep_details['audio'] = res_data['audioLink']

                site_ep_data = client.udb_episode_dict.get(ep_float, {})
                if 'subtitles' in site_ep_data:
                    ep_details['subtitles'] = site_ep_data['subtitles']
                if 'encrypted_subs_details' in site_ep_data:
                    ep_details['encrypted_subs_details'] = site_ep_data['encrypted_subs_details']

                from Utils.YtDlpDownloader import YtDlpDownloader
                dl_config = dict(self.downloader_config)
                dl_config['download_dir'] = season_folder
                dl_config['quality'] = int(selected_res)
                dl_config['referer'] = getattr(client, 'base_url', '')
                dl_config['_aes_decrypt'] = getattr(client, '_aes_decrypt', None)
                dl_client = YtDlpDownloader(dl_config, ep_details)

                links_to_try = [download_link] + list(res_data.get('alternateLinks', []))
                status, msg = 1, 'no sources'
                for i, link in enumerate(links_to_try):
                    if i > 0:
                        self.logger.info(f'  Trying alternate source for part {part_num}')
                    status, msg = dl_client.start_download(link)
                    if status == 0:
                        break
                    self.logger.warning(f'  Part {part_num} source {i + 1} failed: {msg}')

                # Last resort: sniff m3u8 for embed types
                if status != 0 and download_type == 'embed':
                    self.logger.info(f'  Sniffing m3u8 for part {part_num}')
                    from Utils.M3u8Sniffer import M3u8Sniffer
                    sniffer = M3u8Sniffer(timeout=30)
                    m3u8_url = None
                    for link in links_to_try:
                        m3u8_url = sniffer.sniff(link, referer=getattr(client, 'base_url', ''))
                        if m3u8_url:
                            break
                    if m3u8_url:
                        dl_config2 = dict(self.downloader_config)
                        dl_config2['download_dir'] = season_folder
                        dl_config2['_controller'] = DownloadController()
                        from Utils.HLSDownloader import HLSDownloader
                        dl_client_hls = HLSDownloader(dl_config2, ep_details)
                        status, msg = dl_client_hls.start_download(m3u8_url)

                if status != 0:
                    self.logger.error(f'Failed to download split part {ep_no_str}: {msg}')
                    return False

                # yt-dlp may save with a slightly different name; find it
                # by looking for recently created mp4/mkv files
                _time.sleep(0.5)
                found = None
                for f in os.listdir(season_folder):
                    if f.startswith('_kisskh_part_') and f.endswith(part_filename):
                        found = os.path.join(season_folder, f)
                        break
                if not found:
                    # yt-dlp sometimes names the file based on the episodeName
                    for f in os.listdir(season_folder):
                        fp = os.path.join(season_folder, f)
                        if os.path.isfile(fp) and f.endswith('.mp4') and f not in [p for p in part_files]:
                            found = fp
                            break
                if found:
                    part_files.append(found)
                    self.logger.info(f'  Part {part_num} downloaded: {found}')
                else:
                    self.logger.error(f'Part {part_num} file not found after download')
                    return False

            except Exception as e:
                self.logger.error(f'Error downloading split part {ep_no_str}: {e}')
                return False

        if len(part_files) != 4:
            self.logger.error(f'Expected 4 part files, got {len(part_files)}')
            return False

        # Merge with ffmpeg
        output_filename = f'{series_title}.S{season_num:02d}E{ep_num:02d}.mkv'
        output_path = os.path.join(season_folder, output_filename)

        # Build concat file list for ffmpeg
        concat_list_path = os.path.join(season_folder, '_kisskh_concat.txt')
        with open(concat_list_path, 'w') as clf:
            for pf in part_files:
                clf.write(f"file '{os.path.abspath(pf)}'\n")

        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat', '-safe', '0',
            '-i', concat_list_path,
            '-c', 'copy',
            output_path
        ]

        self.logger.info(f'Merging 4 KissKh parts into {output_path}')
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                self.logger.error(f'ffmpeg merge failed: {result.stderr[-500:]}')
                if os.path.exists(output_path):
                    os.remove(output_path)
                return False
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                self.logger.error('ffmpeg did not produce a valid output file')
                return False
            self.logger.info(f'Successfully merged KissKh episodes -> {output_path}')
        except Exception as e:
            self.logger.error(f'ffmpeg merge exception: {e}')
            if os.path.exists(output_path):
                os.remove(output_path)
            return False

        # Clean up part files and concat list
        for pf in part_files:
            try:
                os.remove(pf)
            except OSError:
                pass
        try:
            os.remove(concat_list_path)
        except OSError:
            pass

        return True

    @staticmethod
    def _is_split_episodes(episodes):
        '''True if any episode number is a decimal part (e.g. 8.1, 8.2).'''
        for ep in episodes:
            ep_no = ep.get('episode')
            try:
                f = float(ep_no)
            except (TypeError, ValueError):
                continue
            if f != int(f):
                return True
        return False

    def run_cycle(self):
        '''
        Run a single polling cycle:
        1. Get monitored series from Sonarr
        2. For each series, find missing episodes
        3. Search site client for the series
        4. Match and download missing episodes
        5. Trigger Sonarr rescan
        '''
        cycle_start = time.time()
        self.logger.info(f'--- Poll cycle started at {get_current_time()} ---')
        colprint('header', f'\n[{"DRY-RUN" if self.dry_run else "ACTIVE"}] Poll cycle started at {get_current_time()}')

        # Step 1: Get monitored series
        series_list = self.sonarr.get_monitored_series()
        if not series_list:
            self.logger.info('No monitored series found in Sonarr')
            return

        # Tag filter: only process series with at least one configured tag
        if self.filter_tags:
            series_list = self.sonarr.filter_series_by_tags(series_list, self.filter_tags)
            if not series_list:
                self.logger.info('No monitored series match the configured tags')
                return

        total_downloaded = 0
        total_skipped = 0
        total_failed = 0

        for series in series_list:
            series_id = series['id']
            series_title = series['title']
            self.logger.info(f'Processing series: [{series_title}] (id: {series_id})')

            # Step 2: Get missing episodes
            missing_eps = self.sonarr.get_missing_episodes(series_id)
            if not missing_eps:
                self.logger.debug(f'No missing episodes for [{series_title}]')
                continue

            colprint('results', f'  [{series_title}]: {len(missing_eps)} missing episode(s)')

            if self.dry_run:
                for ep in missing_eps:
                    colprint('predefined', f'    DRY-RUN: Would download S{ep["seasonNumber"]:02d}E{ep["episodeNumber"]:02d}')
                continue

            # Step 3: Search across all configured site clients for the series
            found = self.find_series_on_clients(series_title, series)
            if not found:
                self.logger.warning(f'No match for [{series_title}] on any site client ({", ".join(self.site_client_names)})')
                total_skipped += len(missing_eps)
                continue

            client_name, client, matched_series, variant_series, site_episodes = found

            # If S02+ episodes are missing but no variants were found, search
            # for season-specific entries that weren't in the original results.
            # KissKh lists seasons as separate shows with different titles.
            missing_seasons_gt1 = set(
                ep.get('seasonNumber', 1) for ep in missing_eps
                if ep.get('seasonNumber', 1) > 1
            )
            if missing_seasons_gt1 and not variant_series:
                self.logger.info(
                    f'No variants found for [{series_title}] but S02+ episodes '
                    f'are missing — searching for season-specific entries'
                )
                sonarr_country = series.get('countryCode') or series.get('country') or ''
                for season in sorted(missing_seasons_gt1):
                    season_query = f'{series_title} Season {season}'
                    self.logger.debug(f'Searching for season variant: [{season_query}]')
                    try:
                        season_results = client.search(season_query)
                    except Exception as e:
                        self.logger.debug(f'Search for [{season_query}] failed: {e}')
                        continue
                    if not season_results:
                        continue
                    # Post-filter by country (same as main search)
                    if sonarr_country:
                        filtered = {}
                        for idx, res in season_results.items():
                            res_country = res.get('country') or ''
                            if res_country and not SeriesMatcher._countries_match(sonarr_country, res_country):
                                continue
                            filtered[idx] = res
                        season_results = filtered
                    # Score and find the best match
                    scored = self.matcher.score_all_results(series, season_results)
                    above = [(s, i, r, raw) for s, i, r, raw in scored
                             if self.matcher.is_qualified(series, r, raw)]
                    if above:
                        _, _, season_match, _ = above[0]
                        self.logger.info(
                            f'  Found season {season} variant: [{season_match.get("title")}] '
                            f'({season_match.get("country", "?")}, {season_match.get("year", "?")})'
                        )
                        variant_series.append(season_match)

            # Use pre-fetched episode list from find_series_on_clients when available
            # (it already chose the best client, preferring whole-episode sources).
            if site_episodes is None:
                try:
                    site_episodes = client.fetch_episodes_list(matched_series)
                except Exception as e:
                    self.logger.error(f'Failed to fetch episode list for [{series_title}] from {client_name}: {e}')
                    total_failed += len(missing_eps)
                    continue

            if not site_episodes:
                self.logger.warning(f'No episodes found on {client_name} for [{matched_series.get("title")}]')
                total_skipped += len(missing_eps)
                continue

            # Fetch episode lists for season-variant entries too (Asiaflix
            # splits "X" and "X Season 2" into separate series). Only used
            # when flat mapping fails; sites with one series pass [] here.
            variant_episodes = []
            for v_series in variant_series:
                try:
                    v_eps = client.fetch_episodes_list(v_series)
                    if v_eps:
                        variant_episodes.append((v_series, v_eps))
                except Exception as e:
                    self.logger.debug(f'Failed to fetch variant episodes for [{v_series.get("title")}]: {e}')
            if variant_episodes:
                self.logger.info(f'Loaded {len(variant_episodes)} season-variant episode list(s) for [{series_title}]')

            # Step 5: Download each missing episode
            series_path = self.sonarr.get_series_path(series)
            if not series_path:
                self.logger.error(f'No path found for series [{series_title}]')
                total_failed += len(missing_eps)
                continue

            # Ensure download directory exists
            os.makedirs(series_path, exist_ok=True)
            os.chmod(series_path, 0o775)
            try:
                os.chown(series_path, self.puid, self.pgid)
            except (PermissionError, OSError) as e:
                self.logger.debug(f'Could not chown {series_path}: {e}')
            self.logger.info(
                f'Download target for [{series_title}]: {series_path} '
                f'(Sonarr reports: {series.get("path", "?")})'
            )

            # If KissKh split episodes (8.1-8.4), download all 4 parts and
            # merge with ffmpeg before the normal per-episode download loop.
            if site_episodes and self._is_split_episodes(site_episodes):
                self.logger.info(f'Downloading and merging KissKh split episodes for [{series_title}]')
                try:
                    merged = self._merge_kisskh_split_episodes(
                        client, series, site_episodes, site_episodes, series_path
                    )
                except Exception as e:
                    self.logger.error(f'Error merging KissKh split episodes: {e}')
                    merged = False
                if merged:
                    for ep in missing_eps:
                        ep_key = f'{series_id}-S{ep["seasonNumber"]:02d}E{ep["episodeNumber"]:02d}'
                        self.completed_downloads[ep_key] = True
                    total_downloaded += len(missing_eps)
                    colprint('success', f'    Merged 4 parts -> S{missing_eps[0]["seasonNumber"]:02d}E{missing_eps[0]["episodeNumber"]:02d}')
                else:
                    self.logger.warning(f'Failed to merge KissKh split episodes; skipping [{series_title}]')
                    total_skipped += len(missing_eps)
                # Skip the normal download loop — parts are already merged
                continue

            for ep in missing_eps:
                season = ep.get('seasonNumber', 1)
                ep_num = ep.get('episodeNumber', 1)
                ep_key = f'{series_id}-S{season:02d}E{ep_num:02d}'

                # Skip if already downloaded this session
                if ep_key in self.completed_downloads:
                    self.logger.debug(f'Already downloaded {ep_key}, skipping')
                    continue

                # Map Sonarr episode to site episode.
                # variant_episodes are consulted when the flat map fails
                # (Asiaflix-style season splits).
                site_ep = self.matcher.map_episode(ep, site_episodes, series_id,
                                                   variant_episodes=variant_episodes)
                if not site_ep:
                    self.logger.warning(f'  Could not map S{season:02d}E{ep_num:02d} to {client_name} episode')
                    total_skipped += 1
                    continue

                self.logger.info(f'  Downloading S{season:02d}E{ep_num:02d} -> {client_name} ep {site_ep.get("episode")}')
                colprint('predefined',
                         f'  Downloading S{season:02d}E{ep_num:02d} from {client_name} '
                         f'(ep {site_ep.get("episode")} of {matched_series.get("title")})')

                try:
                    success = self.download_episode(
                        client, series, ep, site_ep, matched_series, site_episodes, series_path
                    )
                    if success:
                        total_downloaded += 1
                        self.completed_downloads[ep_key] = True
                        colprint('success', f'    Downloaded: S{season:02d}E{ep_num:02d}')
                    else:
                        total_failed += 1
                        colprint('error', f'    Failed: S{season:02d}E{ep_num:02d}')
                except Exception as e:
                    self.logger.error(f'  Download failed for S{season:02d}E{ep_num:02d}: {e}')
                    self.logger.debug(f'  Stacktrace: {traceback.format_exc()}')
                    total_failed += 1

            # Step 6: Trigger Sonarr rescan for this series
            if total_downloaded > 0 or any(
                f'{series_id}-S{ep.get("seasonNumber", 1):02d}E{ep.get("episodeNumber", 1):02d}' in self.completed_downloads
                for ep in missing_eps
            ):
                self.logger.info(f'Triggering Sonarr rescan for [{series_title}]')
                rescan_cmd = self.sonarr.trigger_rescan(series_id)

                if rescan_cmd is None:
                    # The rescan command could not even be triggered
                    # (Sonarr unreachable / API timeout). Skip verification —
                    # claiming PATH MISMATCH here would be a false alarm.
                    self.logger.warning(
                        f'Could not trigger Sonarr rescan for [{series_title}] '
                        f'(Sonarr unreachable/timeout). Will re-trigger next cycle.'
                    )
                    continue

                # Wait for the rescan command to actually COMPLETE before
                # verifying — RescanSeries is async, and a fixed sleep can
                # check hasFile before the scan/import finishes, producing
                # false "PATH MISMATCH" errors.
                if rescan_cmd.get('id'):
                    self.sonarr.wait_for_command(rescan_cmd['id'], timeout=120)
                else:
                    time.sleep(5)

                expected = {}
                for ep in missing_eps:
                    season = ep.get('seasonNumber', 1)
                    ep_num = ep.get('episodeNumber', 1)
                    if f'{series_id}-S{season:02d}E{ep_num:02d}' in self.completed_downloads:
                        expected.setdefault(season, []).append(ep_num)
                if expected:
                    detected = self.sonarr.check_files_detected(series_id, expected)
                    if detected is None:
                        # Verification failed (Sonarr unreachable/timeout) —
                        # NOT a path mismatch. Don't cry wolf.
                        self.logger.warning(
                            f'Could not verify imports with Sonarr for [{series_title}] '
                            f'(Sonarr unreachable/timeout). Will re-check next cycle.'
                        )
                        continue
                    undetected = sum(
                        len([e for e in eps if e not in detected.get(s, [])])
                        for s, eps in expected.items()
                    )
                    if undetected:
                        self.logger.error(
                            f'SONARR PATH MISMATCH: {undetected} downloaded episode(s) for [{series_title}] '
                            f'were NOT detected by Sonarr. Daemon wrote to {series_path} but Sonarr is not '
                            f'scanning that path. Check container path mappings.'
                        )

                    # Report the FINAL Sonarr path for each imported episode
                    # (Sonarr renames/moves files during import).
                    imported = self.sonarr.get_imported_episode_paths(series_id, expected)
                    if imported is None:
                        self.logger.warning(
                            f'Could not fetch import paths from Sonarr for [{series_title}] (unreachable/timeout).'
                        )
                        imported = {}
                    for (season, ep_num), path in sorted(imported.items()):
                        self.logger.info(
                            f'IMPORTED [{series_title}] S{season:02d}E{ep_num:02d} -> {path}'
                        )
                        colprint('success',
                                 f'    Imported: S{season:02d}E{ep_num:02d} -> {path}')

        # Cycle summary
        cycle_time = time.time() - cycle_start
        summary = (
            f'Cycle complete in {pretty_time(int(cycle_time), fmt="h m s")}: '
            f'{total_downloaded} downloaded, {total_skipped} skipped, {total_failed} failed'
        )
        self.logger.info(summary)
        colprint('header', f'\n{summary}')

    def download_episode(self, client, sonarr_series: Dict, sonarr_ep: Dict,
                         site_ep: Dict, site_series: Dict, all_site_eps: List,
                         series_path: str) -> bool:
        '''
        Download a single episode using the site client's infrastructure.
        Returns True on success, False on failure.
        '''
        try:
            # Build episode range for just this one episode
            ep_num = float(site_ep.get('episode', 0))
            ep_ranges = {
                'start': ep_num,
                'end': ep_num,
                'specific_no': []
            }

            # Fetch episode links from site client
            download_links = client.fetch_episode_links(all_site_eps, ep_ranges)
            if not download_links or ep_num not in download_links:
                self.logger.error(f'No download links returned for episode {ep_num}')
                return False

            ep_links = download_links[ep_num]
            if 'error' in ep_links:
                self.logger.error(f'Site returned error for episode {ep_num}: {ep_links["error"]}')
                return False

            # Get available resolutions
            available_resolutions = [k for k in ep_links.keys() if k not in ('error', 'original')]
            if not available_resolutions:
                self.logger.error(f'No resolutions available for episode {ep_num}')
                return False

            # Select best resolution matching our quality preference(s).
            # Try each preferred quality in order (e.g. 1080 then 720);
            # fall back to the site client's selector strategy if none match.
            selected_res = None
            for q in self.qualities:
                if q in available_resolutions:
                    selected_res = q
                    break
            if not selected_res:
                selected_res = client._resolution_selector(available_resolutions, self.qualities[0],
                                                           client.selector_strategy)
            if not selected_res:
                selected_res = available_resolutions[0]

            res_data = ep_links.get(selected_res)
            if not res_data or 'downloadLink' not in res_data:
                self.logger.error(f'No download link for resolution {selected_res}')
                return False

            download_link = res_data['downloadLink']
            download_type = res_data.get('downloadType', 'hls')

            # Generate Sonarr-compatible filename
            extension = 'mp4'  # UDB outputs mp4 after muxing
            filename = self.sonarr.get_episode_filename(sonarr_series, sonarr_ep, extension)

            # Build output path with season folder
            season = sonarr_ep.get('seasonNumber', 1)
            season_folder = os.path.join(series_path, f'Season {season:02d}')
            os.makedirs(season_folder, exist_ok=True)
            os.chmod(season_folder, 0o775)
            try:
                os.chown(season_folder, self.puid, self.pgid)
            except (PermissionError, OSError) as e:
                self.logger.debug(f'Could not chown {season_folder}: {e}')
            output_path = os.path.join(season_folder, filename)

            # Skip if file already exists
            if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
                self.logger.info(f'File already exists: {output_path}')
                return True

            # Set up download config for this episode
            dl_config = dict(self.downloader_config)
            dl_config['download_dir'] = season_folder
            dl_config['use_season_folder'] = False  # daemon already built "Season 01/"
            dl_config['_controller'] = DownloadController()

            # Build episode details dict for UDB's downloader
            ep_details = {
                'episodeName': filename,
                'downloadLink': download_link,
                'downloadType': download_type,
                'season': season,
                'type': 'tv'
            }

            # Add audio link if available (for HLS with separate audio)
            if 'audioLink' in res_data and res_data['audioLink']:
                ep_details['audio'] = res_data['audioLink']

            # Add subtitles if available
            site_ep_data = client.udb_episode_dict.get(ep_num, {})
            if 'subtitles' in site_ep_data:
                ep_details['subtitles'] = site_ep_data['subtitles']
            if 'encrypted_subs_details' in site_ep_data:
                ep_details['encrypted_subs_details'] = site_ep_data['encrypted_subs_details']

            # Use UDB's downloader infrastructure
            from Utils.HLSDownloader import HLSDownloader
            from Utils.BaseDownloader import BaseDownloader

            # 'embed' download type (Asiaflix) needs yt-dlp: the links are
            # embed-page URLs (streamtape/mixdrop/vidmoly), not direct m3u8/mp4
            if self.downloader_type == 'yt-dlp' or download_type == 'embed':
                if download_type == 'embed' and self.downloader_type != 'yt-dlp':
                    self.logger.info('Embed download type requires yt-dlp backend — using yt-dlp for this episode')
                # yt-dlp backend (kisskh-dl style): robust HLS/MP4/embed download
                from Utils.YtDlpDownloader import YtDlpDownloader
                dl_config = dict(self.downloader_config)
                dl_config['download_dir'] = season_folder
                dl_config['quality'] = int(selected_res)
                dl_config['referer'] = getattr(client, 'base_url', '')
                dl_config['_aes_decrypt'] = getattr(client, '_aes_decrypt', None)
                dl_client = YtDlpDownloader(dl_config, ep_details)

                # Try the primary source, then any alternate sources
                # (e.g. streamtape mirror domains that may 404 from some IPs)
                links_to_try = [download_link] + list(res_data.get('alternateLinks', []))
                status, msg = 1, 'no sources'
                for i, link in enumerate(links_to_try):
                    if i > 0:
                        self.logger.info(f'Trying alternate source {i + 1}/{len(links_to_try)} for {filename}')
                    status, msg = dl_client.start_download(link)
                    if status == 0:
                        break
                    self.logger.warning(f'Source {i + 1} failed for {filename}: {msg}')

                # Last resort: sniff the raw m3u8 from an embed page with a
                # headless browser (works for hosts yt-dlp has no extractor
                # for, or blocks by policy), then download via ffmpeg.
                if status != 0 and download_type == 'embed':
                    self.logger.info(f'All embed sources failed — sniffing m3u8 directly for {filename}')
                    from Utils.M3u8Sniffer import M3u8Sniffer
                    sniffer = M3u8Sniffer(timeout=30)
                    m3u8_url = None
                    for link in links_to_try:
                        m3u8_url = sniffer.sniff(link, referer=getattr(client, 'base_url', ''))
                        if m3u8_url:
                            break
                    if m3u8_url:
                        self.logger.info(f'Sniffed m3u8 for {filename}: {m3u8_url}')
                        dl_config = dict(self.downloader_config)
                        dl_config['download_dir'] = season_folder
                        dl_config['_controller'] = DownloadController()
                        dl_client_hls = HLSDownloader(dl_config, ep_details)
                        status, msg = dl_client_hls.start_download(m3u8_url)
                        if status != 0:
                            self.logger.error(f'ffmpeg download of sniffed m3u8 failed: {msg}')

                if status == 0:
                    dl_client.download_subtitles()
                if status != 0:
                    self.logger.error(f'Download failed for {filename}: {msg}')
                    return False
                self.logger.info(f'Download completed: {filename}')
                return True

            if download_type == 'hls':
                dl_client = HLSDownloader(dl_config, ep_details)
            elif download_type == 'mp4':
                dl_client = BaseDownloader(dl_config, ep_details)
            else:
                self.logger.error(f'Unknown download type: {download_type}')
                return False

            self.logger.info(f'Starting download: {filename} ({selected_res}p, {download_type})')
            status, msg = dl_client.start_download(download_link)
            dl_client._cleanup_out_dirs()

            if status != 0:
                self.logger.error(f'Download failed: {msg}')
                return False

            self.logger.info(f'Download completed: {filename}')

            # Fix ownership/permissions so Sonarr (running as a different user,
            # e.g. 'nobody' on Unraid) can edit/move/delete the file.
            try:
                os.chmod(output_path, 0o664)
                os.chown(output_path, self.puid, self.pgid)
            except (PermissionError, OSError) as e:
                self.logger.debug(f'Could not chown/chmod {output_path}: {e}')

            return True

        except Exception as e:
            self.logger.error(f'Exception during download: {e}')
            self.logger.debug(f'Stacktrace: {traceback.format_exc()}')
            return False

    def run(self):
        '''Main daemon loop.'''
        # Initialize all components
        colprint_init(self.disable_colors)
        self.init_logging()
        self.check_ffmpeg()
        self.init_sonarr()
        self.init_matcher()
        self.init_site_client()

        colprint('header', f'\nUDB-Sonarr v{__version__} daemon started')
        colprint('results', f'  Sonarr: {self.config["SonarrConfig"]["url"]}')
        colprint('results', f'  Site clients: {", ".join(self.site_clients.keys())}')
        colprint('results', f'  Downloader: {self.downloader_type}')
        colprint('results', f'  Quality: {"/".join(self.qualities)}p')
        colprint('results', f'  Poll interval: {self.poll_interval // 60} minutes')
        colprint('results', f'  Mode: {"DRY-RUN" if self.dry_run else "ACTIVE"}')
        colprint('results', f'  Once: {self.once}')

        if self.dry_run:
            colprint('yellow', '\n  WARNING: Dry-run mode - no files will be downloaded')

        # Main loop
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                colprint('predefined', '\nInterrupted by user')
                self.logger.info('Daemon stopped by user (KeyboardInterrupt)')
                break
            except ExitException as ee:
                if int(str(ee)) == 0:
                    break
                self.logger.error(f'ExitException: {ee}')
            except Exception as e:
                self.logger.error(f'Cycle error: {e}')
                self.logger.debug(f'Stacktrace: {traceback.format_exc()}')

            if self.once:
                colprint('predefined', '\n--once mode: exiting after single cycle')
                break

            colprint('predefined', f'\nNext poll in {self.poll_interval // 60} minutes...')
            try:
                time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                colprint('predefined', '\nInterrupted during sleep')
                break

        # Cleanup
        for client in self.site_clients.values():
            try:
                client.cleanup()
            except Exception:
                pass

        # Close log handlers
        for handler in self.logger.handlers:
            handler.close()
            self.logger.removeHandler(handler)

        colprint('results', '\nUDB-Sonarr daemon stopped.')


def main():
    '''Entry point.'''
    parser = argparse.ArgumentParser(
        description='UDB-Sonarr: Auto-download missing Sonarr episodes via UDB site clients.'
    )
    parser.add_argument('-c', '--conf', default='config_sonarr.yaml',
                        help='configuration file (default: config_sonarr.yaml)')
    parser.add_argument('-D', '--debug', action='store_true', help='enable debug logging')
    parser.add_argument('-l', '--log-file', help='custom log file name')
    parser.add_argument('-v', '--version', action='store_true', help='show version')
    parser.add_argument('--once', action='store_true',
                        help='run a single poll cycle then exit (no loop)')
    parser.add_argument('--dry-run', action='store_true',
                        help='check for missing episodes but do not download')
    parser.add_argument('-dc', '--disable-colors', action='store_true', help='disable colored output')
    parser.add_argument('--skip-update-check', action='store_true', default=True,
                        help='skip UDB update checks (always skipped in daemon mode)')

    args = parser.parse_args()

    if args.version:
        print(f'UDB-Sonarr v{__version__}')
        sys.exit(0)

    # Load config
    config_file = args.conf
    if not os.path.isfile(config_file):
        # Try default UDB config as fallback
        config_file = 'config_udb.yaml'
        if not os.path.isfile(config_file):
            print(f'Error: Config file not found. Create config_sonarr.yaml with SonarrConfig.')
            print(f'See config_sonarr.yaml.example for a template.')
            sys.exit(1)

    config = load_yaml(config_file)

    # Run daemon
    daemon = UDBSonarrDaemon(config, args)
    daemon.run()


if __name__ == '__main__':
    main()
