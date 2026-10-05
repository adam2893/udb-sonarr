"""Optional exact-TMDB TV provider using the site's public playback flow.

No title matching, token persistence, browser bypass or DRM decryption.
"""
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup as BS

from Clients.BaseClient import BaseClient


class SevenMoviesClient(BaseClient):
    def __init__(self, config=None, session=None):
        config = config or {}
        super().__init__(config.get('request_timeout', 30), session,
                         daemon_mode=config.get('daemon_mode', False))
        self.base_url = config.get('base_url', 'https://7movies.ac').rstrip('/')
        self.embed_origin = 'https://embed.vidrift.net'
        self.selector_strategy = config.get('alternate_resolution_selector', 'lowest')

    def _request(self, method, url, **kwargs):
        # Do not use BaseClient's URL/dictionary logging: URLs contain tokens.
        headers = dict(self.header)
        headers.update(kwargs.pop('headers', {}))
        response = getattr(self.req_session, method)(
            url, timeout=self.request_timeout, headers=headers, **kwargs)
        if response.status_code != 200:
            raise ValueError('SevenMovies request failed')
        return response

    @staticmethod
    def _id(value):
        try:
            number = int(value)
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    def lookup_series(self, sonarr_series, tmdb_client=None):
        tmdb_id = self._id(sonarr_series.get('tmdbId'))
        if not tmdb_id and tmdb_client and sonarr_series.get('tvdbId'):
            tmdb_id = self._id(tmdb_client._find_tmdb_id(sonarr_series['tvdbId']))
        if not tmdb_id:
            return None
        try:
            soup = BS(self._request('get', f'{self.base_url}/tv/{tmdb_id}').text,
                      'html.parser')
            title_node = soup.select_one('h1')
            title_meta = soup.select_one('meta[property="og:title"]')
            title = (title_node.get_text(' ', strip=True) if title_node else
                      title_meta.get('content', '') if title_meta else '')
            if not title and title_node:
                image = title_node.select_one('img[alt]')
                title = image.get('alt', '') if image else ''
            if not title and title_meta:
                title = title_meta.get('content', '').split(' — ')[0]
            if not title or re.search(r'not found|404', title, re.I):
                return None
            year = re.search(r'\b(?:19|20)\d{2}\b', soup.get_text(' ', strip=True))
            target = {'title': title, 'series_id': tmdb_id, 'tmdb_id': tmdb_id,
                      'year': int(year.group()) if year else None, 'episodes': [],
                      'sonarr_seasons': sonarr_series.get('sonarr_seasons',
                                                         sonarr_series.get('seasons', []))}
            target['episodes'] = self._episodes(soup, tmdb_id)
            return target
        except (requests.RequestException, ValueError):
            return None

    def _episodes(self, soup, tmdb_id):
        found = {}
        for anchor in soup.select('a[href], [data-navigation-href]'):
            url = urljoin(self.base_url + '/', anchor.get('data-navigation-href') or anchor['href'])
            parsed = urlparse(url)
            if parsed.netloc != urlparse(self.base_url).netloc:
                continue
            if not re.search(rf'/(?:tv|watch)/{tmdb_id}(?:/|$)', parsed.path):
                continue
            query = parse_qs(parsed.query)
            season = self._id(query.get('season', [None])[0])
            episode = self._id(query.get('episode', [None])[0])
            if season and episode:
                found[(season, episode)] = {
                    'season': season, 'seasonNumber': season, 'episode': episode,
                    'tmdb_id': tmdb_id, 'series_id': tmdb_id, 'episodeLink': url,
                    'episodeName': anchor.get_text(' ', strip=True) or f'S{season:02}E{episode:02}'}
        return list(found.values())

    def fetch_episodes_list(self, target):
        tmdb_id = self._id(target.get('tmdb_id') or target.get('series_id'))
        if not tmdb_id:
            return []
        found = {(e['seasonNumber'], e['episode']): e for e in target.get('episodes', [])}
        seasons = target.get('sonarr_seasons') or sorted({key[0] for key in found}) or [1]
        for entry in seasons:
            season = self._id(entry.get('seasonNumber') if isinstance(entry, dict) else entry)
            if not season:
                continue
            soup = BS(self._request('get', f'{self.base_url}/tv/{tmdb_id}/watch?season={season}&episode=1').text,
                      'html.parser')
            for ep in self._episodes(soup, tmdb_id):
                found[(ep['seasonNumber'], ep['episode'])] = ep
        return [found[key] for key in sorted(found)]

    def _manifest(self, url, source):
        text = self._request('get', url, headers={'Referer': self.embed_origin + '/'}).text
        if not text.lstrip().startswith('#EXTM3U'):
            return {}
        if re.search(r'METHOD=(?:SAMPLE-AES[^,\s]*|[^,\s]*DRM)', text, re.I):
            raise ValueError('Unsupported DRM playlist')
        links = {}
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not line.startswith('#EXT-X-STREAM-INF:'):
                continue
            resolution = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
            child = next((s.strip() for s in lines[index + 1:] if s.strip() and not s.startswith('#')), None)
            if resolution and child:
                links[resolution.group(2)] = {
                    'resolution_size': f'{resolution.group(1)}x{resolution.group(2)}',
                    'downloadLink': urljoin(url, child), 'downloadType': 'hls'}
        if not links and '#EXTINF:' in text:
            resolution = re.fullmatch(r'(\d+)x(\d+)', str(source.get('resolution', '')))
            height = self._id(source.get('height'))
            key = resolution.group(2) if resolution else str(height) if height else 'unknown'
            links[key] = {'downloadLink': url, 'downloadType': 'hls',
                          'resolution_size': resolution.group() if resolution else None}
        return links

    def fetch_episode_links(self, episodes, ep_ranges=None):
        result = {}
        for episode in episodes:
            number = int(episode['episode'])
            if ep_ranges and not (ep_ranges['start'] <= number <= ep_ranges['end'] or
                                  number in ep_ranges.get('specific_no', [])):
                continue
            try:
                tmdb_id = self._id(episode.get('tmdb_id') or episode.get('series_id'))
                season = self._id(episode.get('seasonNumber') or episode.get('season'))
                if not tmdb_id or not season or number < 1:
                    continue
                watch = f'{self.base_url}/tv/{tmdb_id}/watch?season={season}&episode={number}'
                token = self._request('post', self.base_url + '/api/playback-token',
                                      json={'tmdbId': tmdb_id, 'type': 'tv', 'season': season,
                                            'episode': number},
                                      headers={'Origin': self.base_url, 'Referer': watch}).json()['token']
                path = f'tv/{tmdb_id}/{season}/{number}'
                boot = self._request('get', self.embed_origin + '/api/boot/' + path,
                                     params={'token': token, 'shell': 1},
                                     headers={'x-embed-parent': self.base_url}).json()
                token = (boot.get('meta') or {}).get('playbackToken') or token
                data = self._request('get', self.embed_origin + '/api/source/' + path,
                                     params={'token': token, 'provider': 'vaplayer', 'hevc': 0},
                                     headers={'Referer': self.embed_origin + '/embed2/play',
                                              'x-embed-parent': self.base_url}).json()
                if not data.get('success'):
                    continue
                links = {}
                for source in data.get('streams', []):
                    raw = source.get('proxyUrl') or source.get('url')
                    if not raw:
                        continue
                    url = urljoin(self.embed_origin + '/', raw)
                    if source.get('type') in ('hls', 'm3u8', 'application/vnd.apple.mpegurl') or '.m3u8' in url:
                        for quality, link in self._manifest(url, source).items():
                            links.setdefault(quality, link)
                if links:
                    subtitles = {s.get('label') or s.get('language') or 'Unknown':
                                 urljoin(self.embed_origin + '/', s.get('url') or s.get('src'))
                                 for s in data.get('subtitles', [])
                                 if isinstance(s, dict) and (s.get('url') or s.get('src'))}
                    # Direct assignment avoids BaseClient logging signed URLs.
                    self.udb_episode_dict[number] = dict(episode, refererLink=self.embed_origin + '/',
                                                         subtitles=subtitles)
                    result[number] = links
            except (requests.RequestException, ValueError, KeyError, TypeError):
                self.logger.warning('SevenMovies episode unavailable or unsupported')
        return result
