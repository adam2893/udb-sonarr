import sys
import types
import unittest
from unittest.mock import patch


if 'tqdm' not in sys.modules:
    tqdm = types.ModuleType('tqdm.auto')
    tqdm.tqdm = lambda iterable=None, **kwargs: iterable
    sys.modules['tqdm'] = types.ModuleType('tqdm')
    sys.modules['tqdm.auto'] = tqdm

from Utils.BaseDownloader import BaseDownloader


class DownloaderConcurrencyTests(unittest.TestCase):
    def make_downloader(self, value='auto'):
        downloader = BaseDownloader.__new__(BaseDownloader)
        with patch.object(BaseDownloader, '_create_out_dirs'):
            BaseDownloader.__init__(downloader, {
                'download_dir': '/tmp/udb-test',
                'concurrency_per_file': value,
            }, {'episodeName': 'test.mp4'})
        return downloader

    def test_auto_concurrency_is_bounded(self):
        self.assertEqual(self.make_downloader().concurrency, 8)

    def test_explicit_concurrency_is_preserved(self):
        self.assertEqual(self.make_downloader(3).concurrency, 3)

    def test_zero_concurrency_is_clamped(self):
        self.assertEqual(self.make_downloader(0).concurrency, 1)


if __name__ == '__main__':
    unittest.main()
