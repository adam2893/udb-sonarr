import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


# Load only this module: Clients package imports unrelated optional dependencies.
# A scoped requests stub also allows running without installed HTTP dependencies.
requests_stub = types.ModuleType('requests')
requests_stub.Session = Mock
spec = importlib.util.spec_from_file_location(
    'tmdb_origin_client', Path(__file__).resolve().parents[1] / 'Clients' / 'TMDBClient.py')
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'requests': requests_stub}):
    spec.loader.exec_module(module)
TmdbClient = module.TmdbClient


class TmdbOriginTests(unittest.TestCase):
    def setUp(self):
        self.client = TmdbClient('secret-key')
        self.client.session = Mock()

    def respond(self, *payloads):
        responses = []
        for payload in payloads:
            response = Mock()
            response.json.return_value = payload
            responses.append(response)
        self.client.session.get.side_effect = responses

    def test_tmdb_id_multiple_countries_and_cache(self):
        self.respond({'origin_country': ['TH', 'JP', 'TH', 'us']})
        countries = self.client.get_series_origin_countries(tmdb_id=42, tvdb_id=99)
        self.assertEqual(countries, ['TH', 'JP', 'US'])
        countries.append('GB')
        self.assertEqual(self.client.get_series_origin_countries(42), ['TH', 'JP', 'US'])
        self.client.session.get.assert_called_once_with(
            TmdbClient.BASE_URL + '/tv/42', params={'api_key': 'secret-key'}, timeout=10)

    def test_tvdb_fallback(self):
        self.respond({'tv_results': [{'id': 42}]}, {'origin_country': ['TH']})
        self.assertEqual(self.client.get_series_origin_countries(tvdb_id=99), ['TH'])
        self.assertEqual(self.client.session.get.call_args_list[0].kwargs['params'],
                         {'external_source': 'tvdb_id', 'api_key': 'secret-key'})
        self.assertTrue(self.client.session.get.call_args_list[0].args[0].endswith('/find/99'))

    def test_empty_absent_and_malformed_metadata_cached(self):
        for metadata in ({}, {'origin_country': []}, {'origin_country': None},
                         {'origin_country': 'TH'}, {'origin_country': {}},
                         {'origin_country': [None, 1, {}, '', 'Thailand', '12']}):
            with self.subTest(metadata=metadata):
                self.setUp()
                self.respond(metadata)
                self.assertEqual(self.client.get_series_origin_countries(42), [])
                self.assertEqual(self.client.get_series_origin_countries(42), [])
                self.assertEqual(self.client.session.get.call_count, 1)

    def test_detail_failure_retried_without_key_logging(self):
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError('url?api_key=secret-key')
        success = Mock()
        success.json.return_value = {'origin_country': ['TH']}
        self.client.session.get.side_effect = [response, success]
        with self.assertLogs(level='WARNING') as logs:
            self.assertEqual(self.client.get_series_origin_countries(42), [])
        self.assertNotIn('secret-key', ''.join(logs.output))
        self.assertEqual(self.client.get_series_origin_countries(42), ['TH'])

    def test_find_failure_retried_without_key_logging(self):
        self.client.session.get.side_effect = RuntimeError('api_key=secret-key')
        with self.assertLogs(level='WARNING') as logs:
            self.assertEqual(self.client.get_series_origin_countries(tvdb_id=99), [])
        self.assertNotIn('secret-key', ''.join(logs.output))
        self.respond({'tv_results': [{'id': 42}]}, {'origin_country': ['TH']})
        self.assertEqual(self.client.get_series_origin_countries(tvdb_id=99), ['TH'])

    def test_invalid_detail_response_retried(self):
        self.respond([], {'origin_country': ['TH']})
        with self.assertLogs(level='WARNING'):
            self.assertEqual(self.client.get_series_origin_countries(42), [])
        self.assertEqual(self.client.get_series_origin_countries(42), ['TH'])

    def test_details_shared_in_both_lookup_orders(self):
        detail = {'origin_country': ['TH'], 'overview': 'Synopsis', 'original_name': 'Original'}
        for origin_first in (True, False):
            with self.subTest(origin_first=origin_first):
                self.setUp()
                if origin_first:
                    self.respond(detail, {'titles': [{'title': 'Alternate'}]})
                    self.assertEqual(self.client.get_series_origin_countries(42), ['TH'])
                else:
                    self.respond({'titles': [{'title': 'Alternate'}]}, detail)
                self.assertEqual(self.client.get_series_aliases(42), ['Alternate', 'Original'])
                self.assertEqual(self.client.get_series_overview(42), 'Synopsis')
                self.assertEqual(self.client.get_series_origin_countries(42), ['TH'])
                self.assertEqual(self.client.session.get.call_count, 2)

    def test_overview_failure_does_not_poison_detail_cache(self):
        self.client.session.get.side_effect = RuntimeError('temporary')
        with self.assertLogs(level='WARNING'):
            self.assertEqual(self.client.get_series_overview(42), '')
        self.respond({'origin_country': ['TH'], 'overview': 'Synopsis'})
        self.assertEqual(self.client.get_series_origin_countries(42), ['TH'])
        self.assertEqual(self.client.get_series_overview(42), 'Synopsis')

    def test_missing_ids_and_unknown_tvdb(self):
        self.assertEqual(self.client.get_series_origin_countries(), [])
        self.client.session.get.assert_not_called()
        self.respond({'tv_results': []})
        self.assertEqual(self.client.get_series_origin_countries(tvdb_id=99), [])


if __name__ == '__main__':
    unittest.main()
