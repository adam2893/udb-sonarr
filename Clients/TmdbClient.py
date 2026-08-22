__author__ = 'UDB-Sonarr Fork'

import logging
import requests
from typing import Dict, List, Optional


class TmdbClient:
    '''
    Thin TMDB API client used to resolve alternate/original titles for a
    series, so the matcher can recognize shows whose site title differs
    from Sonarr's title (common for Thai BL / localized dramas).

    Optional — only active when an api_key is configured.
    '''

    BASE_URL = 'https://api.themoviedb.org/3'

    def __init__(self, api_key: str, timeout: int = 10):
        self.api_key = api_key
        self.timeout = timeout
        self.logger = logging.getLogger()
        self.session = requests.Session()
        # resolved_tmdb_id -> list of alias titles
        self._alias_cache: Dict[int, List[str]] = {}
        # resolved_tmdb_id -> English overview string
        self._overview_cache: Dict[int, str] = {}

    def _get(self, path: str, params: Optional[Dict] = None):
        params = dict(params or {})
        params['api_key'] = self.api_key
        resp = self.session.get(f'{self.BASE_URL}{path}', params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _find_tmdb_id(self, tvdb_id: int) -> Optional[int]:
        '''Resolve a TVDB id to a TMDB id via TMDB's /find endpoint.'''
        try:
            data = self._get(f'/find/{tvdb_id}', params={'external_source': 'tvdb_id'})
            results = data.get('tv_results', [])
            if results:
                return results[0].get('id')
        except Exception as e:
            self.logger.warning(f'TMDB /find failed for tvdb {tvdb_id}: {e}')
        return None

    def get_series_aliases(self, tmdb_id: Optional[int] = None,
                           tvdb_id: Optional[int] = None) -> List[str]:
        '''
        Return alternate + original titles for a series.

        Args:
            tmdb_id: TMDB id from Sonarr (v4 series objects carry this)
            tvdb_id: TVDB id (v3 fallback; resolved via TMDB /find)

        Returns list of title strings (may be empty if TMDB unreachable
        or the series is unknown there).
        '''
        resolved = tmdb_id
        if not resolved and tvdb_id:
            resolved = self._find_tmdb_id(tvdb_id)
        if not resolved:
            return []

        if resolved in self._alias_cache:
            return self._alias_cache[resolved]

        aliases: List[str] = []
        try:
            data = self._get(f'/tv/{resolved}/alternative_titles')
            for entry in data.get('titles', []) or []:
                title = entry.get('title')
                if title and title not in aliases:
                    aliases.append(title)
        except Exception as e:
            self.logger.warning(f'TMDB alternative_titles failed for id {resolved}: {e}')

        try:
            detail = self._get(f'/tv/{resolved}')
            original = detail.get('original_name')
            if original and original not in aliases:
                aliases.append(original)
        except Exception as e:
            self.logger.warning(f'TMDB series detail failed for id {resolved}: {e}')

        self._alias_cache[resolved] = aliases
        if aliases:
            self.logger.info(f'TMDB: {len(aliases)} alternate title(s) for tmdb id {resolved}')
            self.logger.debug(f'TMDB aliases: {aliases}')
        return aliases

    def get_series_overview(self, tmdb_id: Optional[int] = None,
                            tvdb_id: Optional[int] = None) -> str:
        '''Return the English overview/synopsis for a series from TMDB.'''
        resolved = tmdb_id
        if not resolved and tvdb_id:
            resolved = self._find_tmdb_id(tvdb_id)
        if not resolved:
            return ''
        if resolved in self._overview_cache:
            return self._overview_cache[resolved]
        try:
            detail = self._get(f'/tv/{resolved}')
            overview = detail.get('overview', '')
        except Exception as e:
            self.logger.warning(f'TMDB overview fetch failed for id {resolved}: {e}')
            overview = ''
        self._overview_cache[resolved] = overview
        return overview
