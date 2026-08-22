__author__ = 'UDB-Sonarr Fork'

'''M3u8Sniffer: extract the raw m3u8 manifest URL from an embed page by
loading it in a headless browser and sniffing network traffic.

Why this exists: some embed hosts (streamtape, mixdrop, vidmoly, and
mirror/unsupported hosts) load the actual m3u8 via page JavaScript — the
manifest URL is never in the static HTML (the page is an empty shell).
yt-dlp handles hosts it has extractors for, but has no extractor (or
deliberately blocks, e.g. piracy-flagged domains) for others.

This is the server-side equivalent of a browser m3u8-extractor userscript:
drive headless Chromium, watch the network, capture the .m3u8 request,
and return it so the caller can hand it to ffmpeg / UDB's HLSDownloader.

Requires: a Chrome/Chromium browser (the Docker image ships chromium).
'''

import json
import logging
import time
from typing import List, Optional


class M3u8Sniffer:
    '''Sniff m3u8 manifest URLs from an embed page via headless Chrome.'''

    def __init__(self, headless: bool = True, timeout: int = 30):
        self.headless = headless
        self.timeout = timeout
        self.logger = logging.getLogger()

    def _launch_driver(self, uc):
        '''Launch undetected_chromedriver, matching the installed browser's
        major version so chromedriver is not refused.'''
        options = uc.ChromeOptions()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--mute-audio')
        if self.headless:
            options.add_argument('--headless=new')
        # Capture network performance logs so we can read response URLs
        options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

        version_main = None
        try:
            chrome_path = uc.find_chrome_executable()
            if chrome_path:
                import re
                out = self._exec_version(chrome_path)
                m = re.search(r'(\d+)\.', out or '')
                if m:
                    version_main = int(m.group(1))
        except Exception:
            pass

        if version_main:
            return uc.Chrome(options=options, version_main=version_main)
        return uc.Chrome(options=options)

    @staticmethod
    def _exec_version(chrome_path: str) -> str:
        '''Run `<chrome> --version` and return its output.'''
        import subprocess
        try:
            return subprocess.run(
                [chrome_path, '--version'], capture_output=True, text=True, timeout=10
            ).stdout or ''
        except Exception:
            return ''

    def sniff(self, embed_url: str, referer: str = '', wait_seconds: int = 12) -> Optional[str]:
        '''
        Load embed_url in headless Chrome and return the first .m3u8 URL
        seen in network traffic, or None if nothing was captured.
        '''
        try:
            import undetected_chromedriver as uc
        except ImportError:
            self.logger.error('M3u8Sniffer: undetected_chromedriver not installed')
            return None

        driver = None
        try:
            self.logger.debug(f'M3u8Sniffer: launching Chrome for {embed_url}')
            driver = self._launch_driver(uc)
            driver.set_page_load_timeout(self.timeout)

            if referer:
                # Seed cookies/headers by visiting the referer origin first
                try:
                    driver.get(referer)
                except Exception:
                    pass

            try:
                driver.get(embed_url)
            except Exception as e:
                self.logger.debug(f'M3u8Sniffer: page load raised (ok, waiting for network): {e}')

            # Let the page JS fire its media requests
            time.sleep(wait_seconds)

            m3u8_urls = self._extract_m3u8_from_logs(driver)
            if m3u8_urls:
                self.logger.info(f'M3u8Sniffer: captured {len(m3u8_urls)} m3u8 candidate(s) from {embed_url}')
                self.logger.debug(f'M3u8Sniffer: candidates: {m3u8_urls}')
                return m3u8_urls[0]
            self.logger.warning(f'M3u8Sniffer: no m3u8 captured from {embed_url}')
            return None

        except Exception as e:
            self.logger.warning(f'M3u8Sniffer: failed for {embed_url}: {e}')
            return None

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    def _extract_m3u8_from_logs(self, driver) -> List[str]:
        '''Parse Chrome performance logs for network responses whose URL
        points to an m3u8 manifest.'''
        found: List[str] = []
        try:
            logs = driver.get_log('performance')
        except Exception as e:
            self.logger.debug(f'M3u8Sniffer: could not read performance logs: {e}')
            return found

        for entry in logs:
            try:
                message = json.loads(entry.get('message', '{}'))
                params = message.get('message', {}).get('params', {})
                response = params.get('response', {})
                url = response.get('url', '')
                if '.m3u8' in url.lower() and url not in found:
                    found.append(url)
            except Exception:
                continue

        # Prefer master playlists (they are shorter and list variant streams);
        # else return any captured manifest.
        found.sort(key=lambda u: ('m3u8?device' not in u.lower(), len(u)))
        return found
