from unittest.mock import Mock

import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest.mock import patch


class OfflineBaseClient:
    def __init__(self, request_timeout=30, session=None, daemon_mode=False):
        self.req_session = session
        self.request_timeout = request_timeout
        self.header = {'User-Agent': 'Mozilla/5.0'}
        self.udb_episode_dict = {}
        self.logger = logging.getLogger(__name__)


base = types.ModuleType('Clients.BaseClient')
base.BaseClient = OfflineBaseClient
spec = importlib.util.spec_from_file_location(
    '_sevenmovies_test', Path(__file__).resolve().parents[1] / 'Clients/SevenMoviesClient.py')
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'Clients.BaseClient': base}):
    spec.loader.exec_module(module)
SevenMoviesClient = module.SevenMoviesClient


def response(data=None, text='', status=200):
    return Mock(status_code=status, text=text, json=Mock(return_value=data))


def test_unknown_id_does_not_search():
    session = Mock()
    client = SevenMoviesClient(session=session)
    assert client.lookup_series({'title': 'Anything'}) is None
    session.get.assert_not_called()


def test_exact_tvdb_mapping_and_requested_season():
    session = Mock()
    session.get.side_effect = [response(text='<h1>Example</h1> 2024'),
                              response(text='<a href="/watch/12?season=2&episode=3">Three</a>')]
    tmdb = Mock()
    tmdb._find_tmdb_id.return_value = 12
    client = SevenMoviesClient(session=session)
    target = client.lookup_series({'tvdbId': 99, 'seasons': [{'seasonNumber': 2}]}, tmdb)
    assert target['tmdb_id'] == 12
    episodes = client.fetch_episodes_list(target)
    assert [(e['season'], e['episode']) for e in episodes] == [(2, 3)]
    assert session.get.call_args.args[0].endswith('/watch?season=2&episode=1')


def test_token_boot_source_relative_stream_and_manifest():
    session = Mock()
    session.post.return_value = response({'token': 'secret'})
    session.get.side_effect = [response({'meta': {'playbackToken': 'refreshed'}}),
                              response({'success': True, 'provider': 'vidlove', 'fellBack': True,
                                        'streams': [{'proxyUrl': '/relay/master.m3u8', 'type': 'hls'}],
                                        'subtitles': [{'label': 'English', 'url': '/sub.vtt'}]}),
                              response(text='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720\n720/index.m3u8\n#EXT-X-STREAM-INF:RESOLUTION=640x360\n/360.m3u8\n')]
    client = SevenMoviesClient(session=session)
    links = client.fetch_episode_links([{'tmdb_id': 12, 'season': 2, 'episode': 3}])
    assert set(links[3]) == {'720', '360'}
    assert links[3]['720']['downloadLink'] == 'https://embed.vidrift.net/relay/720/index.m3u8'
    assert session.post.call_args.kwargs['json'] == {'tmdbId': 12, 'type': 'tv', 'season': 2, 'episode': 3}
    assert session.get.call_args_list[0].kwargs['headers']['x-embed-parent'] == 'https://7movies.ac'
    assert session.get.call_args_list[1].kwargs['params']['token'] == 'refreshed'
    assert session.get.call_args_list[1].kwargs['params']['provider'] == 'vaplayer'
    assert client.udb_episode_dict[3]['refererLink'] == 'https://embed.vidrift.net/'
    assert client.udb_episode_dict[3]['subtitles']['English'].endswith('/sub.vtt')


def test_media_playlist_has_no_invented_resolution():
    session = Mock()
    session.get.return_value = response(text='#EXTM3U\n#EXTINF:4,\na.ts\n')
    client = SevenMoviesClient(session=session)
    assert set(client._manifest('https://example.com/a.m3u8', {})) == {'unknown'}
    assert set(client._manifest('https://example.com/a.m3u8', {'resolution': '1920x1080'})) == {'1080'}


def test_sample_aes_is_rejected():
    session = Mock()
    session.get.return_value = response(text='#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n#EXTINF:4,\na.ts')
    import unittest
    with unittest.TestCase().assertRaisesRegex(ValueError, 'Unsupported DRM'):
        SevenMoviesClient(session=session)._manifest('https://example.com/a.m3u8', {})


def test_real_html_episode_controls():
    session = Mock()
    session.get.return_value = response(text='<h1><img alt="Game of Thrones"></h1><article data-navigation-href="/tv/1399/watch?season=2&amp;episode=1">Episode one</article>')
    target = SevenMoviesClient(session=session).lookup_series({'tmdbId': 1399})
    assert target['title'] == 'Game of Thrones'
    assert target['episodes'][0]['seasonNumber'] == 2


def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in globals().items()
                              if name.startswith('test_') and callable(value))
