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
                if self.tmdb_client:
                    try:
                        extra_titles = self.tmdb_client.get_series_aliases(
                            tmdb_id=sonarr_series.get('tmdbId'),
                            tvdb_id=sonarr_series.get('tvdbId'),
                        )
                    except Exception as e:
                        self.logger.debug(f'TMDB lookup failed for [{series_title}]: {e}')

                # Score all results; collect every above-threshold match.
                # The best becomes the primary; the rest are "variants"
                # (season-split entries) if they belong to the same show.
                scored = self.matcher.score_all_results(sonarr_series, search_results, extra_titles=extra_titles)
                above = [(score, idx, res) for score, idx, res in scored if score >= self.matcher.match_threshold]
                if not above:
                    self.logger.debug(f'No match on {client_name} for query [{query}], trying next query')
                    continue

                score, idx, primary = above[0]
                self.logger.info(f'Found [{series_title}] on {client_name} -> [{primary.get("title")}] (score: {score:.2f})')

                # Detect season-split variants ("X" + "X Season 2"). Variants
                # are scored against the PRIMARY's title (not Sonarr + year),
                # because a "Season 2" entry often has a different year and
                # would otherwise fall below the strict match threshold.
                variants = []
                for v_score, v_idx, v_res in scored:
                    if (v_score, v_idx) == (score, idx):
                        continue
                    v_sim = self.matcher._similarity(
                        self.matcher._normalize_title(primary.get('title', '')),
                        self.matcher._normalize_title(v_res.get('title', ''))
                    )
                    if v_sim >= self.matcher.match_threshold:
                        variants.append(v_res)

                if variants:
                    self.logger.info(
                        f'  {len(variants)} additional match(es) on {client_name}: '
                        f'{[v.get("title") for v in variants]} (treating as season variants)'
                    )
                return (client_name, client, primary, variants)

        return None

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

            client_name, client, matched_series, variant_series = found

            # Fetch episode list from site
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
            self.logger.info(
                f'Download target for [{series_title}]: {series_path} '
                f'(Sonarr reports: {series.get("path", "?")})'
            )

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
                self.sonarr.trigger_rescan(series_id)

                # Verify Sonarr actually detected the downloaded files.
                # If it reports 0 detected, the daemon's download path does not
                # match the path Sonarr scans (common container path mismatch).
                time.sleep(5)  # let the rescan command run
                expected = {}
                for ep in missing_eps:
                    season = ep.get('seasonNumber', 1)
                    ep_num = ep.get('episodeNumber', 1)
                    if f'{series_id}-S{season:02d}E{ep_num:02d}' in self.completed_downloads:
                        expected.setdefault(season, []).append(ep_num)
                if expected:
                    detected = self.sonarr.check_files_detected(series_id, expected)
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
            output_path = os.path.join(season_folder, filename)

            # Skip if file already exists
            if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
                self.logger.info(f'File already exists: {output_path}')
                return True

            # Set up download config for this episode
            dl_config = dict(self.downloader_config)
            dl_config['download_dir'] = season_folder
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
