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

    # ISO-3166 alpha-2 -> common English country name (lowercase)
    COUNTRY_ISO_TO_NAME = {
        'TH': 'thailand', 'KR': 'south korea', 'CN': 'china', 'JP': 'japan',
        'PH': 'philippines', 'TW': 'taiwan', 'HK': 'hong kong', 'VN': 'vietnam',
        'ID': 'indonesia', 'MY': 'malaysia', 'SG': 'singapore', 'IN': 'india',
        'US': 'united states', 'GB': 'united kingdom', 'UK': 'united kingdom',
        'AU': 'australia', 'CA': 'canada', 'DE': 'germany', 'FR': 'france',
    }

    # Generic words that appear in many titles but don't distinguish shows.
    # Excluded from the word-overlap check so "Player" vs "ABO Desire" can't
    # match just because both contain "series".
    COMMON_WORDS = frozenset({
        'the', 'a', 'an', 'series', 'show', 'drama', 'part', 'season',
        'movie', 'film', 'episode', 'ep', 'vol', 'volume', 'chapter',
        'arc', 'special', 'ova', 'oad', 'web',
    })

    def __init__(self, config: Optional[Dict] = None):
        self.logger = logging.getLogger()
        # Per-series override mapping: { sonarr_series_id: { season: kisskh_ep_offset } }
        # e.g. { 123: { 1: 0, 2: 16 } } means season 2 starts at KissKh ep 17
        self.season_mappings = config.get('season_mappings', {}) if config else {}
        # Minimum similarity ratio for title matching (0.0 to 1.0)
        self.match_threshold = config.get('match_threshold', 0.6) if config else 0.6
        # Above this raw title similarity the title alone is conclusive and
        # year/country are not required to confirm the match.
        self.high_conf_threshold = config.get('high_conf_threshold', 0.8) if config else 0.8
        # Whether to verify year when matching
        self.verify_year = config.get('verify_year', True) if config else True
        # Whether to verify country when matching (Sonarr countryCode vs site country)
        self.verify_country = config.get('verify_country', True) if config else True

    def is_qualified(self, sonarr_series: Dict[str, Any],
                     result: Dict[str, Any], raw_title_score: float) -> bool:
        '''
        Decide whether a search result is a confident-enough match to act on.

        Two tiers:
        - raw title >= high_conf_threshold (0.8): title alone is conclusive.
        - raw title >= match_threshold but below high confidence: MARGINAL —
          word overlap, year, and country must CONFIRM the match. This blocks
          cases like "Player: The Series" -> "ABO Desire the Series" (raw 0.67)
          where a Thai show matches a Chinese show because both share "series".
        - below match_threshold: not a match.
        '''
        if raw_title_score >= self.high_conf_threshold:
            return True
        if raw_title_score < self.match_threshold:
            return False

        # Marginal: require meaningful word overlap. SequenceMatcher is
        # character-level, so titles that share only generic words ("series",
        # "the", "drama") can score above the threshold while being completely
        # different shows. Jaccard similarity of meaningful words must be >= 0.2.
        sonarr_title = self._normalize_title(sonarr_series.get('title', ''))
        result_title = self._normalize_title(result.get('title', ''))
        overlap = self._word_overlap(sonarr_title, result_title)
        if overlap < 0.2:
            self.logger.debug(
                f'Marginal match [{result.get("title")}] rejected: word overlap {overlap:.2f} < 0.2 '
                f'(sonarr="{sonarr_title}" vs result="{result_title}")'
            )
            return False

        # Marginal: require year confirmation when both years are known.
        sonarr_year = str(sonarr_series.get('year', '')).strip()
        result_year = str(result.get('year', 'XXXX')).strip()
        if sonarr_year and self.verify_year:
            if result_year == 'XXXX' or result_year == '':
                self.logger.debug(
                    f'Marginal match [{result.get("title")}] rejected: site has no year to confirm'
                )
                return False
            if sonarr_year != result_year:
                self.logger.debug(
                    f'Marginal match [{result.get("title")}] rejected: year {result_year} != {sonarr_year}'
                )
                return False

        # Marginal: country must not conflict. If Sonarr has a country but the
        # site doesn't report one, reject — an unknown country can't confirm
        # the match (e.g. Thai show matching a Chinese show because KissKh
        # didn't return a country).
        if self.verify_country:
            sonarr_country = sonarr_series.get('countryCode') or sonarr_series.get('country') or ''
            result_country = result.get('country') or ''
            if sonarr_country:
                if not result_country:
                    self.logger.debug(
                        f'Marginal match [{result.get("title")}] rejected: site has no country to confirm'
                    )
                    return False
                if not self._countries_match(sonarr_country, result_country):
                    self.logger.debug(
                        f'Marginal match [{result.get("title")}] rejected: country {result_country} conflicts with {sonarr_country}'
                    )
                    return False

        return True

    @staticmethod
    def _normalize_country(country: str) -> str:
        '''
        Normalize a country value to a canonical lowercase name.
        Handles both ISO-3166 codes (from Sonarr countryCode) and
        English names (from site search results).
        '''
        if not country:
            return ''
        c = str(country).strip().lower()
        # ISO code -> name
        upper = c.upper()
        if upper in SeriesMatcher.COUNTRY_ISO_TO_NAME:
            return SeriesMatcher.COUNTRY_ISO_TO_NAME[upper]
        # Common aliases
        aliases = {
            'usa': 'united states', 'us': 'united states', 'america': 'united states',
            'korea': 'south korea', 'southkorea': 'south korea',
            'uk': 'united kingdom', 'england': 'united kingdom', 'britain': 'united kingdom',
            'prc': 'china', 'mainland china': 'china',
        }
        compact = c.replace(' ', '')
        if compact in aliases:
            return aliases[compact]
        if c in aliases:
            return aliases[c]
        return c

    @classmethod
    def _countries_match(cls, a: str, b: str) -> bool:
        '''True if two normalized country values refer to the same country.'''
        na, nb = cls._normalize_country(a), cls._normalize_country(b)
        if not na or not nb:
            return False
        return na == nb

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

    @staticmethod
    def _word_overlap(a: str, b: str) -> float:
        '''
        Jaccard similarity of meaningful words in two normalized titles.
        Generic words (series, the, drama, etc.) are excluded so titles
        that share only common suffixes don't appear to match.

        Returns 0.0 if either title has no meaningful words after filtering.
        '''
        words_a = set(a.split()) - SeriesMatcher.COMMON_WORDS
        words_b = set(b.split()) - SeriesMatcher.COMMON_WORDS
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

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
        scored = self.score_all_results(sonarr_series, kisskh_results, extra_titles)
        # Qualification is tiered: raw title alone above high_conf_threshold,
        # otherwise marginal matches must be confirmed by year/country.
        if scored and self.is_qualified(sonarr_series, scored[0][2], scored[0][3]):
            _, idx, result, raw = scored[0]
            self.logger.info(f'Series matched: Sonarr [{sonarr_series.get("title")}] -> [{result.get("title")}] (raw title: {raw:.2f})')
            return (idx, result)
        else:
            best = scored[0][3] if scored else 0.0
            self.logger.warning(f'No match found for [{sonarr_series.get("title")}] (best raw title score: {best:.2f}, threshold: {self.match_threshold})')
            return None

    def score_all_results(self, sonarr_series: Dict[str, Any],
                          results: Dict[int, Dict],
                          extra_titles: Optional[List[str]] = None) -> List[Tuple[float, int, Dict, float]]:
        '''
        Score every search result against the Sonarr series and return them
        sorted best-first: [(final_score, result_index, result_dict, raw_title_score), ...].

        final_score includes year/country bonuses (for ranking); raw_title_score
        is the unmodified title similarity and is what QUALIFIES a match.

        Used by match_series (single best) and by the daemon to detect
        season-split entries (a site listing "X" and "X Season 2" as
        separate series, e.g. Asiaflix).
        '''
        # Build the set of candidate sonarr titles: primary + TMDB aliases
        sonarr_titles = [self._normalize_title(sonarr_series.get('title', ''))]
        for alt in (extra_titles or []):
            alt_norm = self._normalize_title(alt)
            if alt_norm and alt_norm not in sonarr_titles:
                sonarr_titles.append(alt_norm)
        sonarr_year = str(sonarr_series.get('year', ''))
        sonarr_country = sonarr_series.get('countryCode') or sonarr_series.get('country') or ''

        self.logger.debug(
            f'Scoring Sonarr series [{sonarr_titles}] ({sonarr_year}, country={sonarr_country}) against '
            f'{len(results)} results'
        )

        scored = []
        for idx, result in results.items():
            result_title = self._normalize_title(result.get('title', ''))
            result_year = str(result.get('year', 'XXXX'))

            # Best similarity across all candidate sonarr titles.
            # This RAW title score is what qualifies a match — bonuses below
            # only rank among qualifiers, they must not turn a different show
            # (e.g. "Temporary Mom" vs "Mother and Mom", raw 0.59) into a match.
            raw_title_score = max(
                (self._similarity(t, result_title) for t in sonarr_titles),
                default=0.0
            )
            title_score = raw_title_score

            # Year bonus: if years match, boost the score
            year_match = sonarr_year == result_year and sonarr_year != ''
            if self.verify_year and year_match:
                title_score += 0.15  # year match bonus
            elif self.verify_year and sonarr_year != '' and result_year != 'XXXX' and not year_match:
                title_score -= 0.2  # year mismatch penalty

            # Country check: a conflicting country (e.g. Philippines vs
            # Thailand) means it is almost certainly a different show,
            # even if the title is similar.
            result_country = result.get('country') or ''
            if self.verify_country and sonarr_country and result_country:
                if self._countries_match(sonarr_country, result_country):
                    title_score += 0.15  # country match bonus
                else:
                    title_score -= 0.4  # strong penalty for wrong country

            self.logger.debug(
                f'  Result [{result.get("title")}] ({result_year}, country={result_country}): '
                f'raw_title={raw_title_score:.2f} final={title_score:.2f}'
            )
            # (final_score, idx, result, raw_title_score)
            scored.append((title_score, idx, result, raw_title_score))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    def map_episode(self, sonarr_episode: Dict[str, Any],
                    kisskh_episodes: List[Dict[str, Any]],
                    sonarr_series_id: Optional[int] = None,
                    variant_episodes: Optional[List[Tuple[Dict, List[Dict]]]] = None) -> Optional[Dict[str, Any]]:
        '''
        Map a Sonarr episode (seasonNumber, episodeNumber) to a KissKh episode.

        For single-season shows: KissKh ep N = Sonarr S01E0N (direct map)
        For multi-season shows: uses season_mappings config or a heuristic
        that counts episodes across seasons.

        variant_episodes: list of (entry_dict, episode_list) for season-split
        entries (e.g. Asiaflix listing "X" and "X Season 2" separately). Only
        consulted when the flat mapping fails. Each variant's title is checked
        for a season marker ("Season 2", "Part 2", "S2", etc.); if it matches
        the Sonarr season, that entry's episodes are used.

        Args:
            sonarr_episode: dict with seasonNumber, episodeNumber
            kisskh_episodes: list from KissKhClient.fetch_episodes_list()
            sonarr_series_id: optional, for looking up per-series season mappings
            variant_episodes: optional list of (entry, episodes) for season splits

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
            # Season 1 with no direct match: try season-split variant entries
            # (e.g. site lists "X" separately from "X Season 1")
            matched = self._map_from_variants(season, ep_num, variant_episodes)
            if matched:
                return matched
            self.logger.warning(f'Episode {ep_num} not found in KissKh episode list (single-season direct map)')
            return None

        # Multi-season heuristic: try to find the episode by absolute number
        absolute_ep = ep_num
        for ep in kisskh_episodes:
            if float(ep.get('episode', 0)) == float(ep_num):
                self.logger.debug(f'Multi-season: direct match S{season}E{ep_num} -> KissKh ep {ep_num}')
                return ep

        # Fall back to season-split variant entries (Asiaflix-style splits)
        matched = self._map_from_variants(season, ep_num, variant_episodes)
        if matched:
            return matched

        self.logger.warning(
            f'Could not map S{season}E{ep_num} to KissKh episode. '
            f'Consider adding a season_mapping override for series ID {sonarr_series_id}.'
        )
        return None

    def _map_from_variants(self, season: int, ep_num: int,
                           variant_episodes: Optional[List[Tuple[Dict, List[Dict]]]]) -> Optional[Dict]:
        '''
        Try to map a Sonarr episode to a season-split variant entry.
        A variant matches if its title carries a season marker equal to the
        Sonarr season (e.g. "Season 2", "Part 2", "S2", " 2"). The episode
        number is then used directly against that entry's episode list
        (season-relative numbering: "X Season 2" lists its own eps 1..N).
        '''
        if not variant_episodes:
            return None

        for entry, episodes in variant_episodes:
            entry_title = entry.get('title', '')
            if not self._title_matches_season(entry_title, season):
                continue
            for ep in episodes:
                if float(ep.get('episode', 0)) == float(ep_num):
                    self.logger.info(
                        f'Mapped S{season}E{ep_num} -> variant [{entry_title}] ep {ep_num}'
                    )
                    return ep
        return None

    @staticmethod
    def _title_matches_season(title: str, season: int) -> bool:
        '''Check whether a title carries a marker for the given season,
        e.g. "X Season 2", "X Part 2", "X S2", "X 2nd", "X: The Second".'''
        lowered = title.lower()
        markers = [
            f'season {season}', f'season-{season}', f's{season}',
            f'part {season}', f'part-{season}', f'part {season}',
            f'series {season}',
        ]
        for marker in markers:
            if marker in lowered:
                return True
        # Word-ending number, e.g. "BLANK 2" or "BLANK: The Second Season"
        import re
        if re.search(rf'(^|\s){season}(\s|$|:)', lowered):
            return True
        return False

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
