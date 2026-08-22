__author__ = 'Prudhvi PLN'

import re
import json
from urllib.parse import quote_plus

from Clients.BaseClient import BaseClient


class AsiaflixClient(BaseClient):
    '''
    All-in-one Client for asiaflix.net site (sister site of KissKh)
    '''
    # step-0
    def __init__(self, config, session=None):
        self.base_url = config.get('base_url', 'https://asiaflix.net/')
        if not self.base_url.endswith('/'):
            self.base_url += '/'
        self.search_url = self.base_url + config.get('search_url', 'search?q=')
        self.drama_url = self.base_url + config.get('drama_url', 'drama/')
        # embed hosts ordered by preference. yt-dlp has native extractors
        # for streamtape, mixdrop and vidmoly
        self.source_priority = config.get('source_priority', [
            'streamtape', 'mixdrop', 'vidmoly', 'vidbasic', 'asianctv'
        ])
        self.search_limit = config.get('search_limit', 10)
        self.selector_strategy = config.get(
            'alternate_resolution_selector', 'highest'
        )
        super().__init__(config.get('request_timeout', 30), session)
        self.logger.debug(f'Asiaflix client initialized with {config = }')

    # step-1
    def search(self, keyword):
        '''
        search for drama based on a keyword
        '''
        search_results = {}
        search_url = self.search_url + quote_plus(keyword)
        self.logger.debug(f'Searching for {keyword} on {search_url}')
        soup = self._get_bsoup(search_url, referer=self.base_url)
        if soup is None:
            self.logger.warning('Failed to fetch search page')
            return search_results

        # each result card has 3 anchors: thumbnail (has img), the title
        # anchor (long text) and a 'Watch now' anchor (short text).
        # Ignore UI-button labels that are not real titles.
        UI_LABELS = {'watch now', 'watch', 'play', 'play now', 'download', 'more'}
        drama_cards = {}
        for anchor in soup.select('a[href^="/drama/"]'):
            href = anchor['href']
            text = anchor.get_text(strip=True)
            has_img = bool(anchor.find('img'))
            if href not in drama_cards:
                drama_cards[href] = {'title': '', 'anchor': anchor}
            if text.lower() in UI_LABELS:
                continue
            if not has_img and len(text) > 3 and len(text) > len(drama_cards[href]['title']):
                drama_cards[href]['title'] = text

        idx = 1
        for href, card in drama_cards.items():
            if len(search_results) >= self.search_limit:
                break
            title = card['title']
            if not title:
                continue

            # extract year & status from the card text
            year = 'XXXX'
            status = 'Unknown'
            node = card['anchor']
            for _ in range(4):
                card_text = node.get_text(' ', strip=True)
                year_match = re.search(r'\((\d{4})', card_text)
                if not year_match:
                    year_match = re.search(r'Year:\s*(\d{4})', card_text)
                if year_match:
                    year = year_match.group(1)
                status_match = re.search(
                    r'(Ongoing|Completed|Upcoming)', card_text
                )
                if status_match:
                    status = status_match.group(1)
                if year != 'XXXX' and status != 'Unknown':
                    break
                node = node.parent

            slug = href.split('/drama/')[-1].strip('/')
            item = {
                'title': title,
                'series_id': slug,
                'year': year,
                'status': status,
                'country': ''
            }
            search_results[idx] = item
            self._colprint(
                'results',
                f"{idx}: {title} | Year: {year} | Status: {status}"
            )
            idx += 1

        return search_results

    # step-2.1
    def _extract_episodes_json(self, soup):
        '''
        extract embedded episodes json array from the drama page html.
        balanced-bracket scan respecting quoted strings and escapes
        '''
        html = str(soup)
        match = re.search(r'"episodes":', html)
        if match is None:
            return None
        pos = match.end()
        depth = 0
        in_str = False
        esc = False
        i = pos
        while i < len(html):
            char = html[i]
            if in_str:
                if esc:
                    esc = False
                elif char == '\\':
                    esc = True
                elif char == '"':
                    in_str = False
            else:
                if char == '"':
                    in_str = True
                elif char == '[':
                    depth += 1
                elif char == ']':
                    depth -= 1
                    if depth == 0:
                        break
            i += 1
        try:
            return json.loads(html[pos:i+1])
        except json.JSONDecodeError as jde:
            self.logger.warning(
                f'Failed to parse episodes json. Error: {jde}'
            )
            return None

    # step-2
    def fetch_episodes_list(self, target):
        '''
        fetch episode list from the drama page
        '''
        all_episodes_list = []
        slug = target['series_id']
        page_url = self.drama_url + slug
        self.logger.debug(f'Fetching drama page for {target.get("title", slug)}')
        soup = self._get_bsoup(page_url, referer=self.base_url)
        if soup is None:
            self.logger.warning(f'Failed to fetch drama page for {slug}')
            return all_episodes_list

        # extract the embedded json array containing episode data
        episodes_data = self._extract_episodes_json(soup)
        if episodes_data is None:
            self.logger.warning(f'Failed to parse episodes json for {slug}')
            return all_episodes_list

        # extract drama title from the page title (eg: "Ashes to Crown (2026)")
        drama_title = target.get('title', slug)
        title_match = re.search(r'<title>([^(<]+)', str(soup))
        if title_match:
            drama_title = title_match.group(1).strip()
        self.logger.debug(f'Drama title: {drama_title}')

        # the json array is already ascending (1..24). do not reverse
        for episode in episodes_data:
            ep_no = int(episode['number']) if 'number' in episode else 0
            ep_name = f"{drama_title} Episode {self._fmted_ep_no(ep_no)}"
            all_episodes_list.append({
                'episode': ep_no,
                'episodeName': ep_name,
                'streamUrls': episode.get('streamUrls', []),
                'type': episode.get('type', 'SUB')
            })

        return all_episodes_list

    # step-3.1
    def _pick_stream_url(self, episode):
        '''
        pick the best embed url for the episode based on source priority
        '''
        stream_urls = episode.get('streamUrls', [])
        if len(stream_urls) == 0:
            return None

        # iterate the priority order and pick the first matching source
        for source in self.source_priority:
            for stream in stream_urls:
                if stream.get('source') == source and stream.get('url'):
                    return stream['url']

        # fallback: use the first available source
        for stream in stream_urls:
            if stream.get('url'):
                return stream['url']

        return None

    # step-3
    def fetch_episode_links(self, episodes, ep_ranges):
        '''
        fetch embed links for required episodes based on episode range
        '''
        download_links = {}
        ep_start = ep_ranges['start']
        ep_end = ep_ranges['end']
        specific_eps = ep_ranges.get('specific_no', [])

        for episode in episodes:
            ep_no = episode.get('episode')
            if ep_no is None:
                continue
            if not (ep_start <= ep_no <= ep_end or ep_no in specific_eps):
                continue
            self.logger.debug(f'Processing {episode = }')

            url = self._pick_stream_url(episode)
            if url is None:
                self.logger.warning(f'No stream url found for episode: {ep_no}')
                continue

            # single '1080' numeric key. the yt-dlp backend resolves
            # the actual quality itself, so keep it resolution-agnostic
            download_links[ep_no] = {
                '1080': {
                    'downloadLink': url,
                    'downloadType': 'embed',
                    'resolution_size': 'embed',
                    'duration': 'NA'
                }
            }

            # add episode details & stream link to udb dict
            self._update_udb_dict(ep_no, {
                'episodeName': episode['episodeName']
            })
            self._update_udb_dict(ep_no, {
                'streamLink': url, 'refererLink': self.base_url
            })

            self._colprint(
                'results',
                f'Episode: {self._safe_type_cast(ep_no)} | Link found [{url}]'
            )

        return download_links

    # step-4
    def set_out_names(self, target_series):
        drama_title = self._windows_safe_string(target_series['title'])
        # set target output dir
        target_dir = drama_title if drama_title.endswith(')') else f"{drama_title} ({target_series['year']})"

        return target_dir, None
