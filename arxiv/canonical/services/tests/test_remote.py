"""Tests for :mod:`arxiv.canonical.services.remote`."""

import io
import os
import tempfile
from unittest import TestCase, mock

from ...domain import URI
from .. import remote


class TestCanResolve(TestCase):
    """Remote service can resolve HTTP URIs."""

    def setUp(self):
        """Given a remote service instance."""
        self.trusted_domain = 'arxiv.org'
        self.remote = remote.RemoteSource(self.trusted_domain, 'https')

    def test_with_http_uri(self):
        """CAN resolve HTTP URIs in the trusted domain."""
        self.assertTrue(
            self.remote.can_resolve(URI('https://arxiv.org/stats/today'))
        )

    def test_with_http_uri_outside_trusted_domain(self):
        """Cannot resolve HTTP URIs outside of the trusted domain."""
        self.assertFalse(self.remote.can_resolve(URI('https://asdf.com')))

    def test_with_http_uri_with_nontrusted_scheme(self):
        """Cannot resolve HTTP URIs with a non-trusted scheme."""
        self.assertFalse(
            self.remote.can_resolve(URI('http://arxiv.org/stats/today'))
        )

    def test_with_canonical_uri(self):
        """Cannot resolve canonical URIs."""
        self.assertFalse(self.remote.can_resolve(URI('arxiv:///foo/key')))

    def test_with_file_uri(self):
        """Cannot resolve file URIs."""
        self.assertFalse(self.remote.can_resolve(URI('/foo/bar/baz')))


class TestLoadDeferred(TestCase):
    """Remote service can load HTTP URIs."""

    @mock.patch(f'{remote.__name__}.requests.Session')
    def setUp(self, mock_Session):
        """Given a remote service instance."""
        self.mock_session = mock.MagicMock()
        mock_Session.return_value = self.mock_session

        self.trusted_domain = 'arxiv.org'
        self.remote = remote.RemoteSource(self.trusted_domain, 'https')

    def test_load(self):
        """Can load content from the HTTP URI."""
        mock_response = mock.MagicMock(status_code=200)
        mock_response.iter_content.return_value = \
            iter([b'foo', b'con' b'ten', b't'])
        self.mock_session.get.return_value = mock_response
        res = self.remote.load(URI('https://arxiv.org/stats/today'))
        self.assertEqual(self.mock_session.get.call_count, 0,
                         'No request is yet performed')
        self.assertEqual(res.read(4), b'fooc')
        self.assertEqual(self.mock_session.get.call_count, 1,
                         'Until an attempt to read() is made')

        mock_response.iter_content.return_value = \
            iter([b'foo', b'con' b'ten', b't'])
        res = self.remote.load(URI('https://arxiv.org/stats/today'))
        self.assertEqual(res.read(), b'foocontent')

    def test_load_outside_base_path(self):
        """Cannot load an HTTP URI outside trusted domain."""
        with self.assertRaises(RuntimeError):
            self.remote.load(URI('https://asdf.com'))

    def test_load_without_training_wheels(self):
        """This will issue a live call to arxiv.org."""
        r = remote.RemoteSource(self.trusted_domain, 'https')
        reader = r.load(URI('https://arxiv.org/pdf/0801.1021v2.pdf'))
        self.assertIsInstance(reader, io.BytesIO)
        self.assertEqual(len(reader.read()), 237187)
        reader.seek(0)
        self.assertEqual(len(reader.read()), 237187)

        reader.seek(0)
        self.assertEqual(len(reader.read(4096)), 4096)


    def test_load_streaming_without_training_wheels(self):
        """This will issue a live call to arxiv.org."""
        r = remote.RemoteSource(self.trusted_domain, 'https')
        reader = r.load(URI('https://arxiv.org/pdf/0801.1021v2.pdf'),
                                 stream=True)
        self.assertIsInstance(reader, io.BytesIO)
        self.assertEqual(len(reader.read()), 237187)
        reader.seek(0)
        self.assertEqual(len(reader.read()), 237187)

        reader.seek(0)
        self.assertEqual(len(reader.read(4096)), 4096)
