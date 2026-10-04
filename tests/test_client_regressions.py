import unittest
import sys
import types
from unittest.mock import Mock

# Keep these regressions runnable in the lightweight/offline test environment.
if 'Cryptodome' not in sys.modules:
    cipher = types.ModuleType('Cryptodome.Cipher')
    cipher.AES = types.SimpleNamespace(block_size=16)
    crypto = types.ModuleType('Cryptodome')
    crypto.Cipher = cipher
    sys.modules['Cryptodome'] = crypto
    sys.modules['Cryptodome.Cipher'] = cipher
if 'undetected_chromedriver' not in sys.modules:
    sys.modules['undetected_chromedriver'] = types.SimpleNamespace(Chrome=type('Chrome', (), {}))
if 'selenium' not in sys.modules:
    exc = types.ModuleType('selenium.common.exceptions')
    exc.NoSuchElementException = type('NoSuchElementException', (Exception,), {})
    by = types.ModuleType('selenium.webdriver.common.by')
    by.By = types.SimpleNamespace(XPATH='xpath')
    sys.modules['selenium'] = types.ModuleType('selenium')
    sys.modules['selenium.common'] = types.ModuleType('selenium.common')
    sys.modules['selenium.common.exceptions'] = exc
    sys.modules['selenium.webdriver'] = types.ModuleType('selenium.webdriver')
    sys.modules['selenium.webdriver.common'] = types.ModuleType('selenium.webdriver.common')
    sys.modules['selenium.webdriver.common.by'] = by
if 'tqdm' not in sys.modules:
    tqdm = types.ModuleType('tqdm.auto')
    tqdm.tqdm = lambda iterable=None, **kwargs: iterable
    sys.modules['tqdm'] = types.ModuleType('tqdm')
    sys.modules['tqdm.auto'] = tqdm

from Clients.BaseClient import BaseClient
from Clients.AnimePaheClient import AnimePaheClient
from Utils.HLSDownloader import HLSDownloader


class ClientRegressionTests(unittest.TestCase):
    def test_normalize_url_handles_malformed_cdn_host_reference(self):
        client = BaseClient(request_timeout=1)
        self.assertEqual(client._normalize_url('//cdn.example/seg.ts', 'https://site.example/path'),
                         'https://cdn.example/seg.ts')
        self.assertEqual(client._normalize_url('/seg.ts', 'https://site.example/path'),
                         'https://site.example/seg.ts')

    def test_hls_uri_resolution_handles_scheme_relative_and_queries(self):
        downloader = HLSDownloader.__new__(HLSDownloader)
        self.assertEqual(set(downloader._collect_ts_urls(
            'https://site.example/a/master.m3u8',
            '//cdn.example/a.ts\n/absolute.ts\npart.ts?x=1\n')),
            {'https://cdn.example/a.ts', 'https://site.example/absolute.ts',
             'https://site.example/a/part.ts?x=1'})

    def test_browser_navigation_failure_quits_driver(self):
        client = AnimePaheClient.__new__(AnimePaheClient)
        client.logger = Mock()
        driver = Mock()
        driver.get.side_effect = RuntimeError('navigation failed')
        client._get_undetected_chrome_driver = Mock(return_value=driver)
        with self.assertRaises(RuntimeError):
            client._get_new_cookies('https://example.test', '//ready', wait_time_in_secs=0)
        driver.quit.assert_called_once_with()

    def test_browser_cookie_failure_quits_driver(self):
        client = AnimePaheClient.__new__(AnimePaheClient)
        client.logger = Mock()
        driver = Mock()
        driver.get_cookies.side_effect = RuntimeError('cookie failed')
        client._get_undetected_chrome_driver = Mock(return_value=driver)
        with self.assertRaises(RuntimeError):
            client._get_new_cookies('https://example.test', '//ready', wait_time_in_secs=0)
        driver.quit.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
