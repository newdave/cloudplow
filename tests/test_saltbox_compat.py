"""Behaviour Saltbox's generated config.json relies on, so this fork can replace upstream cloudplow."""
import os
import tempfile
import unittest
from unittest import mock

from cloudplow import Cloudplow
from utils import path


class GetSizeTests(unittest.TestCase):
    def test_du_is_called_with_an_argument_list(self):
        with mock.patch('utils.process.run') as run:
            run.return_value.returncode = 0
            run.return_value.output = '201\t/mnt/local/Media'
            size = path.get_size('/mnt/local/Media', ['downloads/*', 'it\'s [odd]'])
        self.assertEqual(size, 201)
        self.assertEqual(run.call_args[0][0], ['du', '-s', '--block-size=1G', '--exclude=downloads/*',
                                               "--exclude=it's [odd]", '/mnt/local/Media'])

    def test_failure_counts_as_empty(self):
        with mock.patch('utils.process.run') as run:
            run.return_value.returncode = 1
            run.return_value.output = 'du: cannot access'
            self.assertEqual(path.get_size('/missing'), 0)

    def test_real_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'f'), 'wb') as fp:
                fp.write(b'x')
            self.assertEqual(path.get_size(tmp), 1)  # du rounds up to whole GiB blocks


class SizeThresholdTests(unittest.TestCase):
    """Saltbox's template sets max_size_gb/size_excludes: upload only once the folder reaches that size."""

    def make(self, uploader_config):
        plow = object.__new__(Cloudplow)
        plow.configs = {'remotes': {'google': {'upload_folder': '/mnt/local/Media'}}, 'hidden': {}}
        plow.uploader_suspended = mock.Mock(return_value=False)
        plow.release_expired_service_accounts = mock.Mock()
        plow.clean_hidden = mock.Mock()
        plow.upload = mock.Mock()
        plow.scheduled_uploader('google', uploader_config)
        return plow

    def test_below_threshold_waits(self):
        with mock.patch('utils.path.get_size', return_value=150) as get_size:
            plow = self.make({'max_size_gb': 200, 'size_excludes': ['downloads/*']})
        get_size.assert_called_once_with('/mnt/local/Media', ['downloads/*'])
        plow.upload.assert_not_called()

    def test_at_threshold_uploads(self):
        with mock.patch('utils.path.get_size', return_value=200):
            plow = self.make({'max_size_gb': 200, 'size_excludes': ['downloads/*']})
        plow.upload.assert_called_once_with('google')

    def test_without_threshold_uploads_every_interval(self):
        with mock.patch('utils.path.get_size') as get_size:
            plow = self.make({'check_interval': 10})
        get_size.assert_not_called()
        plow.upload.assert_called_once_with('google')


if __name__ == '__main__':
    unittest.main()
