__author__ = 'UDB-Sonarr Fork'

import logging
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Any, Tuple


class SeriesMatcher:
    '''
    Match Sonarr series and episodes to KissKh search results.

    Handles the core mapping problem: Sonarr uses season/episode numbering
    (S01E05) while KissKh often uses flat episode numbering (Episode 5).
    For single-season shows (most Thai BL/drama), this is a 1:1 map.
    For multi-season shows, a mapping config or heuristic is needed.
    '''

    def __init__(self, config: Optional[Dict] = None):
        self.logger = logging.getLogger()
        # Per-series override mapping: { sonarr_series_id: { season: kisskh_ep_offset } }
        # e.g. { 123: { 1: 0, 2: 16 } } means season 2 starts at KissKh ep 17
        self.season_mappings = config.get('season_mappings', {}) if config else {}
        # Minimum similarity ratio for title matching (0.0 to 1.0)
        self.match_threshold = config.get('match_threshold', 0.6) if config else 0.6
        # Whether to verify year when matching
        self.verify_year = config.get('verify_year', True) if config else True

    @staticmethod
    def _normalize_title(title: str) -> str:
        '''
        Normalize a title for comparison:
        - lowercase
        - remove punctuation
        - remove common suffixes/prefixes
        - collapse whitespace
        '''
        title = title.lower().strip()
        # Remove common variations
        title = re.sub(r'\b(the|a|an)\b', '', title)
        # Remove punctuation and special chars
        title = re.sub(r'[^\w\s]', ' ', title)
        # Remove trailing year in parentheses
        title = re.sub(r'\(\d{4}\)', '', title)
        # Collapse whitespace
        title = re.sub(r'\s+', ' ', title).strip()
        return title

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        '''Calculate string similarity ratio between two normalized titles.'''
        return SequenceMatcher(None, a, b).ratio()

    def match_series(self, sonarr_series: Dict[str, Any],
                     kisskh_results: Dict[int, Dict],
                     extra_titles: Optional[List[str]] = None) -> Optional[Tuple[int, Dict]]:
        '''
        Match a Sonarr series to the best KissKh search result.

        Args:
            sonarr_series: Sonarr series dict with title, year, etc.
            kisskh_results: dict from KissKhClient.search() { index: { title, year, series_id, ... } }
            extra_titles: optional list of alternate titles (e.g. TMDB
                original/alternate titles) to match against too. Useful when
                the site's title differs from Sonarr's (localized dramas).

        Returns:
            Tuple of (kisskh_index, kisskh_result_dict) or None if no match found.
        '''
        # Build the set of candidate sonarr titles: primary + TMDB aliases
        sonarr_titles = [self._normalize_title(sonarr_series.get('title', ''))]
        for alt in (extra_titles or []):
            alt_norm = self._normalize_title(alt)
            if alt_norm and alt_norm not in sonarr_titles:
                sonarr_titles.append(alt_norm)
        sonarr_year = str(sonarr_series.get('year', ''))

        self.logger.debug(
            f'Matching Sonarr series [{sonarr_titles}] ({sonarr_year}) against '
            f'{len(kisskh_results)} results'
        )

        best_match = None
        best_score = 0.0

        for idx, result in kisskh_results.items():
            kisskh_title = self._normalize_title(result.get('title', ''))
            kisskh_year = str(result.get('year', 'XXXX'))

            # Best similarity across all candidate sonarr titles
            title_score = max(
                (self._similarity(t, kisskh_title) for t in sonarr_titles),
                default=0.0
            )

            # Year bonus: if years match, boost the score
            year_match = sonarr_year == kisskh_year and sonarr_year != ''
            if self.verify_year and year_match:
                title_score += 0.15  # year match bonus
            elif self.verify_year and sonarr_year != '' and kisskh_year != 'XXXX' and not year_match:
                title_score -= 0.2  # year mismatch penalty

            self.logger.debug(f'  Result [{result.get("title")}] ({kisskh_year}): title_score={title_score:.2f}')

            if title_score > best_score:
                best_score = title_score
                best_match = (idx, result)

        if best_match and best_score >= self.match_threshold:
            self.logger.info(f'Series matched: Sonarr [{sonarr_series.get("title")}] -> [{best_match[1].get("title")}] (score: {best_score:.2f})')
            return best_match
        else:
            self.logger.warning(f'No match found for [{sonarr_series.get("title")}] (best score: {best_score:.2f}, threshold: {self.match_threshold})')
            return None

    def map_episode(self, sonarr_episode: Dict[str, Any],
                    kisskh_episodes: List[Dict[str, Any]],
                    sonarr_series_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        '''
        Map a Sonarr episode (seasonNumber, episodeNumber) to a KissKh episode.

        For single-season shows: KissKh ep N = Sonarr S01E0N (direct map)
        For multi-season shows: uses season_mappings config or a heuristic
        that counts episodes across seasons.

        Args:
            sonarr_episode: dict with seasonNumber, episodeNumber
            kisskh_episodes: list from KissKhClient.fetch_episodes_list()
            sonarr_series_id: optional, for looking up per-series season mappings

        Returns:
            Matching KissKh episode dict or None.
        '''
        season = sonarr_episode.get('seasonNumber', 1)
        ep_num = sonarr_episode.get('episodeNumber', 1)

        # Check for explicit season mapping override
        if sonarr_series_id and sonarr_series_id in self.season_mappings:
            mapping = self.season_mappings[sonarr_series_id]
            if str(season) in mapping:
                offset = mapping[str(season)]
                kisskh_ep_num = ep_num + offset
                self.logger.debug(f'Using explicit mapping: S{season}E{ep_num} -> KissKh ep {kisskh_ep_num}')
                # Find the episode with this number
                for ep in kisskh_episodes:
                    if float(ep.get('episode', 0)) == float(kisskh_ep_num):
                        return ep
                self.logger.warning(f'Mapped to KissKh ep {kisskh_ep_num} but not found in episode list')
                return None

        # Single season: direct mapping (most common for Thai BL/drama)
        if season == 1:
            for ep in kisskh_episodes:
                if float(ep.get('episode', 0)) == float(ep_num):
                    return ep
            self.logger.warning(f'Episode {ep_num} not found in KissKh episode list (single-season direct map)')
            return None

        # Multi-season heuristic: try to find the episode by absolute number
        # This assumes KissKh lists episodes sequentially across seasons
        # Calculate absolute episode number by summing previous seasons' episode counts
        # This is a best-effort heuristic that may need per-series overrides
        absolute_ep = ep_num
        # Without knowing previous season counts, we try the direct number first
        # then try offsetting by common season lengths
        for ep in kisskh_episodes:
            if float(ep.get('episode', 0)) == float(ep_num):
                self.logger.debug(f'Multi-season: direct match S{season}E{ep_num} -> KissKh ep {ep_num}')
                return ep

        # Try treating KissKh episode number as absolute (season * typical_eps + ep)
        # This is fragile but better than nothing for shows without explicit mappings
        self.logger.warning(
            f'Could not map S{season}E{ep_num} to KissKh episode. '
            f'Consider adding a season_mapping override for series ID {sonarr_series_id}.'
        )
        return None

    def build_episode_ranges(self, missing_episodes: List[Dict[str, Any]]) -> Dict[str, Dict]:
        '''
        Group missing Sonarr episodes into ranges for efficient KissKh fetching.
        Returns dict keyed by season number with start/end/specific episodes.

        e.g. { '1': {'start': 1, 'end': 5, 'specific_no': []} }
        '''
        by_season = {}
        for ep in missing_episodes:
            season = ep.get('seasonNumber', 1)
            ep_num = ep.get('episodeNumber', 1)
            if season not in by_season:
                by_season[season] = []
            by_season[season].append(ep_num)

        ranges = {}
        for season, eps in sorted(by_season.items()):
            eps_sorted = sorted(eps)
            ranges[str(season)] = {
                'start': float(eps_sorted[0]),
                'end': float(eps_sorted[-1]),
                'specific_no': [float(e) for e in eps_sorted if e != eps_sorted[0] and e != eps_sorted[-1]]
            }

        return ranges
