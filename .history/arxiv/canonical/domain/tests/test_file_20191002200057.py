"""Tests for :mod:`arxiv.canonical.domain`."""

from unittest import TestCase

from ..file import URI


class TestURIForFile(TestCase):
    """URI can refer to a local file."""

    def test_file_uri(self):
        """URI is initialized with an absolute path."""
        path = '/path/to/some/data'
        uri = URI(path)
        self.assertTrue(uri.is_file, 'Recognized as a file reference')
        self.assertFalse(uri.is_http_url, 'Not an HTTP URI')
        self.assertFalse(uri.is_canonical, 'Not a canonical URI')
        self.assertEqual(uri.path, path, 'Original path is preserved')

    def test_file_uri_with_relative_path(self):
        """URI is initialized with a relative path."""
        path = 'path/to/some/data'
        with self.assertRaises(ValueError):
            URI(path)

