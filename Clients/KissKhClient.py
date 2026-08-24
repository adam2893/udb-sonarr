__author__ = 'Prudhvi PLN'

import re
from quickjs import Context as quickjsContext
from urllib.parse import quote_plus

from Clients.BaseClient import BaseClient


class KissKhClient(BaseClient):
    '''
    All-in-one Client for kisskh site
    '''
    # step-0
    def __init__(self, config, session=None):
        # Known working mirrors, checked in order. kisskh.nl was retired;
        # current live domains verified 2026-08: .ovh, .id, .is
        self.mirror_domains = config.get('kisskh_domains') or [
            'kisskh.ovh', 'kisskh.id', 'kisskh.is', 'kisskh.nl'
        ]
        configured = config.get('base_url', 'https://kisskh.ovh/')
        self.base_url = self._first_alive(configured)
        self._build_urls(self.base_url)
        self.preferred_urls = config['preferred_urls'] if config.get('preferred_urls') else []
        self.blacklist_urls = config['blacklist_urls'] if config.get('blacklist_urls') else []
        self.selector_strategy = config.get('alternate_resolution_selector', 'lowest')
        self.hls_size_accuracy = config.get('hls_size_accuracy', 0)
        self.search_limit = config.get('search_limit', 20)
        super().__init__(config.get('request_timeout', 30), session,
                         daemon_mode=config.get('daemon_mode', False))
        self.logger.debug(f'KissKh Drama client initialized with {config = }')
        self.token_generation_js_code = None
        self.quickjs_context = None
        # site specific details required to create token. Check dev-notes for more details.
        self.subGuid = "VgV52sWhwvBSf8BsM3BRY9weWiiCbtGp"
        self.viGuid = "62f176f3bb1b5b8e70e39932ad34a0c7"
        self.appVer = "2.8.10"
        self.platformVer = 4830201
        self.appName = "kisskh"
        # key and iv for decrypting subtitles for txt. Source: https://github.com/debakarr/kisskh-dl/issues/14#issuecomment-1862055123
        self.DECRYPT_SUBS_KEY = b'8056483646328763'
        self.DECRYPT_SUBS_IV = b'6852612370185273'
        # new key & iv for decrypting subtitles for txt1, as on Feb-13, 2025. Check your dev-notes for more details.
        self.DECRYPT_SUBS_KEY2 = b'AmSmZVcH93UQUezi'
        self.DECRYPT_SUBS_IV2 = b'ReBKWW8cqdjPEnF6'
        # key & iv for decrypting subtitles, default encryption.
        self.DECRYPT_SUBS_KEY3 = b'sWODXX04QRTkHdlZ'
        self.DECRYPT_SUBS_IV3 = b'8pwhapJeC4hrS9hO'
        # kkey token cache + resilience (kisskh-dl / KissKH-Api merge)
        self.token_cache = {}
        self.use_kkey_cache = config.get('kkey_cache', True)
        self.kisskh_api_fallback_url = config.get('kisskh_api_fallback_url', '').rstrip('/')

    def _build_urls(self, base_url):
        '''Rebuild all endpoint URLs from the given base (e.g. after a domain switch).'''
        base_url = base_url.rstrip('/') + '/'
        self.base_url = base_url
        self.search_url = base_url + 'api/DramaList/Search?q='
        self.series_url = base_url + 'api/DramaList/Drama/'
        self.episode_url = base_url + 'api/DramaList/Episode/{id}.png?kkey='
        self.subtitles_url = base_url + 'api/Sub/{id}?kkey='

    def _first_alive(self, configured):
        '''
        Pick the first reachable KissKh domain: the configured base first,
        then each known mirror. Falls back to the configured value if all
        checks fail (requests will just fail naturally afterwards).
        '''
        import requests as _requests
        candidates = [configured.rstrip('/') + '/']
        from urllib.parse import urlparse
        configured_host = urlparse(configured).netloc.lower()
        for domain in self.mirror_domains:
            if domain.lower() != configured_host:
                candidates.append(f'https://{domain}/')

        for base in candidates:
            try:
                r = _requests.get(base, timeout=8,
                                  headers={'User-Agent': 'Mozilla/5.0'})
                if r.status_code < 500:
                    return base
            except Exception:
                continue
        self.logger.warning(f'No reachable KissKh domain found among {candidates}')
        return candidates[0]

    # step-1.1
    def _show_search_results(self, key, details):
        '''
        pretty print drama results based on your search
        '''
        line = f"{key}: {details.get('title')} | Country: {details.get('country')}" + \
                f"\n   | Episodes: {details.get('episodesCount', 'NA')} | Released: {details.get('year')} | Status: {details.get('status')}"
        self._colprint('results', line)

    # step-4.1
    def _get_token(self, episode_id, uid):
        '''
        create token required to fetch stream & subtitle links
        '''
        # use cached token if available
        cache_key = (episode_id, uid)
        if self.use_kkey_cache and cache_key in self.token_cache:
            self.logger.debug(f'Using cached token for {cache_key}')
            return self.token_cache[cache_key]

        # js code to generate token from kisskh site
        if self.token_generation_js_code is None:
            self.logger.debug('Fetching token generation js code...')
            soup = self._get_bsoup(self.base_url + 'index.html')
            common_js_url = self.base_url + [ i['src'] for i in soup.select('script') if i.get('src') and 'common' in i['src'] ][0]
            self.token_generation_js_code = self._send_request(common_js_url)

        # quickjs context for evaluating js code
        if self.quickjs_context is None:
            self.logger.debug('Creating quickjs context...')
            self.quickjs_context = quickjsContext()

        # evaluate js code to generate token
        self.logger.debug(f'Evaluating js code to generate token using {episode_id = } and {uid = }')
        token = self.quickjs_context.eval(self.token_generation_js_code + f'_0x54b991({episode_id}, null, "2.8.10", "{uid}", 4830201,  "kisskh", "kisskh", "kisskh", "kisskh", "kisskh", "kisskh")')
        self.token_cache[cache_key] = token
        return token

    # step-4.1.1
    def _fetch_via_kisskh_api(self, episode):
        '''
        fallback to local KissKH-Api microservice (beorgsh/KissKH-Api) which
        bypasses Cloudflare using a real browser and returns stream + subtitles
        Returns (stream_link, subtitles_dict) or None on failure
        '''
        if not self.kisskh_api_fallback_url:
            return None

        episode_id = episode.get('episodeId')
        try:
            self.logger.debug(f'Fetching episode {episode_id} via KissKH-Api fallback...')
            response = self.req_session.get(
                f'{self.kisskh_api_fallback_url}/resolve/{episode_id}',
                timeout=30
            )
            if response.status_code != 200:
                self.logger.debug(f'KissKH-Api fallback failed with code: {response.status_code}')
                return None
            data = response.json()
        except Exception as e:
            self.logger.debug(f'KissKH-Api fallback request failed. Error: {e}')
            return None

        # guard: response may not be a dict, keys may be missing
        if not isinstance(data, dict):
            self.logger.debug(f'KissKH-Api fallback returned unexpected response: {data}')
            return None

        stream = data.get('stream') or {}
        stream_link = stream.get('Video') or stream.get('BackupVideo')
        if not stream_link:
            self.logger.debug(f'No stream link found in KissKH-Api fallback response for episode {episode_id}')
            return None

        subtitles_dict = {
            s.get('label'): s.get('src')
            for s in (data.get('subtitles') or [])
            if isinstance(s, dict) and s.get('src')
        }
        self.logger.info(f'Obtained stream link via KissKH-Api fallback for episode {episode_id}')
        return stream_link, subtitles_dict

    # step-1
    def search(self, keyword):
        '''
        search for drama based on a keyword
        '''
        # search type codes
        search_types = {
            # '0': 'all',
            '1': 'Asian Drama',
            '2': 'Asian Movies',
            '3': 'Anime',
            '4': 'Hollywood'
        }
        idx = 1
        search_results = {}
        search_type = None

        # check if search type is provided
        try:
            if '>' in keyword:
                search_type = [ k for k,v in search_types.items() if keyword.split('>')[0].strip().lower() in v.lower() ][0]
                keyword = keyword.split('>')[1].strip()
                search_limit = self.search_limit * 2
            else:
                search_limit = self.search_limit
        except:
            pass

        # url encode search keyword
        search_key = quote_plus(keyword)

        for code, type in search_types.items():
            if search_type and search_type != code:
                continue
            self._colprint('blurred', f"-------------- {type} --------------")
            self.logger.debug(f'Searching for {type} with keyword: {keyword}')
            search_url = self.search_url + search_key + '&type=' + str(code)
            search_data = self._send_request(search_url, return_type='json')[:search_limit]
            # if len(search_data) == 0:
            #     self.logger.error('Nothing here')

            # Get basic details available from the site
            for result in search_data:
                series_id = result['id']
                self.logger.debug(f'Fetching additional details for series_id: {series_id}')
                series_data = self._send_request(self.series_url + str(series_id), return_type='json')
                item = {
                    'title': series_data['title'],
                    'series_id': series_id,
                    'country': series_data['country'],
                    'episodesCount': series_data['episodesCount'],
                    'series_type': series_data['type'],
                    'status': series_data['status'],
                    'episodes': series_data['episodes'],
                    'description': series_data.get('description', ''),
                }
                try:
                    item['year'] = series_data['releaseDate'].split('-')[0]
                except:
                    item['year'] = 'XXXX'

                # Add index to every search result
                search_results[idx] = item
                self._show_search_results(idx, item)
                idx += 1

        return search_results

    # step-2
    def fetch_episodes_list(self, target):
        '''
        fetch episode links as dict containing link, name
        '''
        all_episodes_list = []
        episodes = target['episodes']

        self.logger.debug(f'Extracting episode details for {target["title"]}')
        for episode in episodes:
            ep_no = int(episode['number']) if str(episode['number']).endswith('.0') else episode['number']
            if target['series_type'].lower() == 'movie' and len(episodes) > 1:
                ep_name = f"{target['title']} Movie Part-{self._fmted_ep_no(ep_no)}"
            elif target['series_type'].lower() == 'movie':
                ep_name = f"{target['title']} Movie"
            else:
                ep_name = f"{target['title']} Episode {self._fmted_ep_no(ep_no)}"
            all_episodes_list.append({
                'episode': ep_no,
                'episodeName': self._windows_safe_string(ep_name),
                'episodeId': episode['id'],
                'episodeSubs': episode['sub']
            })

        return all_episodes_list[::-1]   # return episodes in ascending

    # step-3
    def show_episode_results(self, items, *predefined_range):
        '''
        pretty print episodes list from fetch_episodes_list
        '''
        start, end = self._get_episode_range_to_show(items[0].get('episode'), items[-1].get('episode'), predefined_range[1], threshold=24)
        display_prefix = 'Movie' if items[0].get('episodeName').endswith('Movie') else 'Episode'

        for item in items:
            if item.get('episode') >= start and item.get('episode') <= end:
                fmted_name = re.sub(r'\b(\d$)', r'0\1', item.get('episodeName'))
                self._colprint('results', f"{display_prefix}: {fmted_name}")

    # step-4
    def fetch_episode_links(self, episodes, ep_ranges):
        '''
        fetch only required episodes based on episode range provided
        '''
        download_links = {}
        ep_start, ep_end, specific_eps = ep_ranges['start'], ep_ranges['end'], ep_ranges.get('specific_no', [])
        display_prefix = 'Movie' if episodes[0].get('episodeName').endswith('Movie') else 'Episode'

        for episode in episodes:
            # self.logger.debug(f'Current {episode = }')

            if (float(episode.get('episode')) >= ep_start and float(episode.get('episode')) <= ep_end) or (float(episode.get('episode')) in specific_eps):
                self.logger.debug(f'Processing {episode = }')

                # try direct path to fetch stream link, with one token-refresh retry
                fallback_subtitles = None
                episode_id = episode.get('episodeId')
                ep = episode.get('episode')
                self.logger.debug('Fetching stream token')
                token = self._get_token(episode_id, self.viGuid)
                self.logger.debug(f'Fetching stream link')
                try:
                    dl_links = self._send_request(self.episode_url.format(id=str(episode_id)) + token, return_type='json')
                except Exception:
                    dl_links = None
                if dl_links is None:
                    # one retry with a freshly generated token
                    self.logger.debug(f'Token refresh retry for episode {ep}')
                    self.token_cache.pop((episode_id, self.viGuid), None)
                    token = self._get_token(episode_id, self.viGuid)
                    try:
                        dl_links = self._send_request(self.episode_url.format(id=str(episode_id)) + token, return_type='json')
                    except Exception:
                        dl_links = None

                if dl_links is None:
                    # fallback to KissKH-Api microservice if configured
                    if self.kisskh_api_fallback_url:
                        self.logger.debug(f'Falling back to KissKH-Api for episode {ep}')
                        fallback = self._fetch_via_kisskh_api(episode)
                        if fallback is not None:
                            link, fallback_subtitles = fallback
                        else:
                            self.logger.warning(f'Failed to fetch stream link for episode: {ep}')
                            continue
                    else:
                        self.logger.warning(f'Failed to fetch stream link for episode: {ep}')
                        continue
                else:
                    link = dl_links.get('Video')
                    self.logger.debug(f'Extracted stream link: {link = }')

                # skip if no stream link found
                if link is None:
                    continue

                # check if link has countdown timer for upcoming releases
                if 'tickcounter.com' in link:
                    self.logger.debug(f'Episode {ep} is not released yet')
                    self._show_episode_links(ep, {'error': 'Not Released Yet'}, display_prefix)
                    continue

                # add episode details & stream link to udb dict
                self._update_udb_dict(ep, episode)
                self._update_udb_dict(ep, {'streamLink': link, 'refererLink': self.base_url})

                # get subtitles dictionary (key:value = language:link) and add to udb dict
                if episode.get('episodeSubs', 0) > 0:
                    if fallback_subtitles is not None:
                        # fallback already returned subtitles
                        self.logger.debug('Using subtitles from KissKH-Api fallback')
                        subtitles = fallback_subtitles
                    else:
                        self.logger.debug('Subtitles found. Fetching subtitles token')
                        token = self._get_token(episode_id, self.subGuid)
                        self.logger.debug('Fetching subtitles for the episode...')
                        subtitles = self._send_request(self.subtitles_url.format(id=str(episode_id)) + token, return_type='json')
                        subtitles = { sub['label']: sub['src'] for sub in subtitles }
                    self._update_udb_dict(ep, {'subtitles': subtitles})
                    # check if subtitles are encrypted and add decryption details to udb dict
                    # every subtitle can have it's own encryption type. So, check all subtitles for encryption and add decryption details to udb dict
                    encrypted_subs_details = {}
                    for k, v in subtitles.items():
                        self.logger.debug(f'Checking encryption type for {k} language...')
                        encryption_type = v.split('?')[0].split('.')[-1]
                        if encryption_type == 'txt':
                            encrypted_subs_details[k] = {'key': self.DECRYPT_SUBS_KEY, 'iv': self.DECRYPT_SUBS_IV, 'decrypter': self._aes_decrypt}
                        elif encryption_type == 'txt1':
                            encrypted_subs_details[k] = {'key': self.DECRYPT_SUBS_KEY2, 'iv': self.DECRYPT_SUBS_IV2, 'decrypter': self._aes_decrypt}
                        elif encryption_type == 'srt':
                            continue    # no encryption
                        else:
                            encrypted_subs_details[k] = {'key': self.DECRYPT_SUBS_KEY3, 'iv': self.DECRYPT_SUBS_IV3, 'decrypter': self._aes_decrypt}  # use default encryption

                    if encrypted_subs_details:
                        self.logger.debug(f'Encrypted subtitles found. Adding decryption details to udb dict...')
                        self._update_udb_dict(ep, {'encrypted_subs_details': encrypted_subs_details})

                # get actual download links
                m3u8_links = [{'file': link, 'type': 'hls'}] if link.split('?')[0].endswith('.m3u8') else [{'file': link, 'type': 'mp4'}]
                self.logger.debug(f'Fetching resolution streams from the stream link...')
                try:
                    m3u8_links = self._get_download_links(m3u8_links, self.base_url, self.preferred_urls, self.blacklist_urls)
                    # self.logger.debug(f'Extracted {m3u8_links = }')
                except Exception as e:
                    self.logger.error(f'Failed to extract download links for episode: {episode.get("episode")}. Error: {e}')
                    continue

                download_links[episode.get('episode')] = m3u8_links
                self._show_episode_links(episode.get('episode'), m3u8_links, display_prefix)

        return download_links

    # step-5
    def set_out_names(self, target_series):
        drama_title = self._windows_safe_string(target_series['title'])
        # set target output dir
        target_dir = drama_title if drama_title.endswith(')') else f"{drama_title} ({target_series['year']})"

        return target_dir, None
