import unittest
import sys
import types
from unittest.mock import Mock

if 'quickjs' not in sys.modules:
    sys.modules['quickjs'] = types.SimpleNamespace(Context=object)

from Clients.KissKhClient import KissKhClient


class KissKhDiagnosticsTests(unittest.TestCase):
    def test_error_summary_is_short_and_token_free(self):
        response = Mock(json=Mock(return_value={'message': 'episode unavailable'}), text='bad')
        summary = KissKhClient._api_error_summary(response)
        self.assertEqual(summary, 'message=episode unavailable')
        self.assertNotIn('kkey', summary)

    def test_empty_error_response_is_described(self):
        response = Mock(text='', json=Mock(side_effect=ValueError()))
        self.assertEqual(KissKhClient._api_error_summary(response), 'empty response body')


if __name__ == '__main__':
    unittest.main()
