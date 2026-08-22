__author__ = 'UDB-Sonarr Fork'

import logging
import requests
from typing import List, Dict, Optional, Any


class SonarrClient:
    '''
    Sonarr v3/v4 API client for polling monitored series, missing episodes,
    and triggering series rescans after downloads complete.

    Works with Sonarr v3 and v4 — both expose the API under /api/v3/
    (the API version has never been bumped to v4).
    '''

    def __init__(self, config: Dict[str, Any]):
        '''
        Args:
            config: dict with keys:
                - url: Sonarr base URL (e.g. http://localhost:8989)
                - api_key: Sonarr API key
                - api_version: always 'v3'. Sonarr v4 still uses /api/v3/
                  (a 'v4' value is normalized to 'v3' with a warning)
                - root_folder: override root folder path (optional, uses series.path by default)
                - request_timeout: HTTP timeout in seconds (default: 30)
        '''
        self.base_url = config['url'].rstrip('/')
        self.api_key = config['api_key']
        # Sonarr's API path is /api/v3/ on both Sonarr v3 and v4.
        api_version = config.get('api_version', 'v3').lower().lstrip('/')
        if api_version != 'v3':
            self.logger = logging.getLogger()
            self.logger.warning(
                f'Sonarr API version is always v3 (v3 and v4 both use /api/v3/). '
                f'Ignoring configured api_version "{api_version}"'
            )
            api_version = 'v3'
        self.api_version = api_version
        self.root_folder = config.get('root_folder')
        self.request_timeout = config.get('request_timeout', 30)
        self.logger = logging.getLogger()

        self.api_base = f'{self.base_url}/api/{self.api_version}'
        self.headers = {
            'X-Api-Key': self.api_key,
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.logger.debug(f'SonarrClient initialized: {self.base_url} (API {self.api_version})')

    def _request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                 json_body: Optional[Dict] = None) -> Any:
        '''Make an authenticated request to Sonarr API.'''
        url = f'{self.api_base}/{endpoint.lstrip("/")}'
        self.logger.debug(f'Sonarr API {method} {url} params={params} body={json_body}')
        response = None
        try:
            response = self.session.request(
                method, url, params=params, json=json_body,
                timeout=self.request_timeout
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return None
        except requests.exceptions.HTTPError as e:
            self.logger.error(f'Sonarr API error: {e} - {response.text if response else "no response"}')
            raise
        except requests.exceptions.RequestException as e:
            self.logger.error(f'Sonarr API connection error: {e}')
            raise

    def test_connection(self) -> bool:
        '''Test API connectivity. Returns True if Sonarr responds.'''
        try:
            self._request('GET', 'system/status')
            self.logger.info('Sonarr connection test: OK')
            return True
        except Exception as e:
            self.logger.error(f'Sonarr connection test failed: {e}')
            return False

    def get_monitored_series(self) -> List[Dict[str, Any]]:
        '''
        Get all monitored series from Sonarr.
        Returns list of series dicts, each containing:
            - id, title, titleSlug, year, monitored, path, seasonCount, seasons, etc.
        '''
        try:
            all_series = self._request('GET', 'series')
            monitored = [s for s in all_series if s.get('monitored', False)]
            self.logger.info(f'Sonarr: {len(monitored)} monitored series (of {len(all_series)} total)')
            return monitored
        except Exception as e:
            self.logger.error(f'Failed to fetch series from Sonarr: {e}')
            return []

    def get_missing_episodes(self, series_id: int) -> List[Dict[str, Any]]:
        '''
        Get monitored episodes without files for a given series.
        Returns list of episode dicts, each containing:
            - id, seriesId, seasonNumber, episodeNumber, title, hasFile, monitored, etc.
        '''
        try:
            episodes = self._request('GET', 'episode', params={'seriesId': series_id})
            missing = [
                ep for ep in episodes
                if ep.get('monitored', False) and not ep.get('hasFile', False)
            ]
            self.logger.info(f'Sonarr: series {series_id} has {len(missing)} missing monitored episodes')
            return missing
        except Exception as e:
            self.logger.error(f'Failed to fetch episodes for series {series_id}: {e}')
            return []

    def get_series(self, series_id: int) -> Optional[Dict[str, Any]]:
        '''Get a single series by ID.'''
        try:
            return self._request('GET', f'series/{series_id}')
        except Exception as e:
            self.logger.error(f'Failed to fetch series {series_id}: {e}')
            return None

    def trigger_rescan(self, series_id: int) -> Optional[Dict]:
        '''
        Trigger a RescanSeries command in Sonarr.
        This makes Sonarr scan the series folder for new files and import them.
        '''
        try:
            result = self._request('POST', 'command', json_body={
                'name': 'RescanSeries',
                'seriesId': series_id
            })
            self.logger.info(f'Sonarr: RescanSeries triggered for series {series_id} (job id: {result.get("id", "unknown")})')
            return result
        except Exception as e:
            self.logger.error(f'Failed to trigger rescan for series {series_id}: {e}')
            return None

    def trigger_refresh_series(self, series_id: int) -> Optional[Dict]:
        '''
        Trigger a RefreshSeries command in Sonarr.
        This updates series metadata from TVDB. Less aggressive than rescan.
        '''
        try:
            result = self._request('POST', 'command', json_body={
                'name': 'RefreshSeries',
                'seriesId': series_id
            })
            self.logger.info(f'Sonarr: RefreshSeries triggered for series {series_id}')
            return result
        except Exception as e:
            self.logger.error(f'Failed to trigger refresh for series {series_id}: {e}')
            return None

    def get_series_path(self, series: Dict[str, Any]) -> str:
        '''
        Get the target path for a series.
        Uses series.path from Sonarr, or overrides with root_folder from config.
        '''
        if self.root_folder:
            # Build path from root folder + series folder name
            title = series.get('title', 'Unknown')
            year = series.get('year', '')
            folder_name = f'{title} ({year})' if year else title
            # Sanitize for filesystem
            for char in ['/', '\\', '"', ':', '?', '|', '<', '>', '*']:
                folder_name = folder_name.replace(char, '')
            return f'{self.root_folder}/{folder_name}'
        return series.get('path', '')

    def get_episode_filename(self, series: Dict[str, Any], episode: Dict[str, Any],
                            extension: str = 'mp4') -> str:
        '''
        Generate a Sonarr-compatible filename for an episode.
        Format: Series Title - S01E05 - Episode Title.ext
        '''
        title = series.get('title', 'Unknown')
        season = episode.get('seasonNumber', 1)
        ep_num = episode.get('episodeNumber', 1)
        ep_title = episode.get('title', '')

        # Sanitize title
        for char in ['/', '\\', '"', ':', '?', '|', '<', '>', '*']:
            title = title.replace(char, '')

        if ep_title:
            for char in ['/', '\\', '"', ':', '?', '|', '<', '>', '*']:
                ep_title = ep_title.replace(char, '')
            filename = f'{title} - S{season:02d}E{ep_num:02d} - {ep_title}.{extension}'
        else:
            filename = f'{title} - S{season:02d}E{ep_num:02d}.{extension}'

        return filename
