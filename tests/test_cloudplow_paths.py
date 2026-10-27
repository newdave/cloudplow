"""Failure and dry-run paths of Cloudplow's script hooks and hidden cleaner."""
import os
import stat
import tempfile
import unittest
from unittest import mock

import cloudplow as cloudplow_module
from cloudplow import Cloudplow
from utils.ratelimit import RateArbiter


def executable(directory, name, content):
    path = os.path.join(directory, name)
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class ScriptFailureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.plow = object.__new__(Cloudplow)
        self.plow.configs = {'remotes': {'nfs': {}}, 'plex': {'enabled': True, 'rclone': {'url': 'http://x'}}}
        self.plow.config_dir, self.plow.dry_run, self.plow.rates = self.tmp, False, RateArbiter()

    def test_script_without_shebang_is_reported_not_raised(self):
        script = executable(self.tmp, 'noshebang', 'echo hi\n')
        with self.assertLogs('cloudplow', 'ERROR') as logs:
            self.assertFalse(Cloudplow._run_script('post_script', script, {}))
        self.assertIn('could not be executed', logs.output[0])

    def test_failing_rate_script_leaves_the_limit_alone(self):
        # the script writes an override, then fails: a failed run must not be trusted
        script = executable(self.tmp, 'rate.sh', '#!/bin/sh\nmkdir -p "$(dirname "$CLOUDPLOW_OVERRIDES_FILE")"\n'
                                                 'echo \'{"--bwlimit": "1M"}\' > "$CLOUDPLOW_OVERRIDES_FILE"\nexit 3\n')
        self.plow.rates.start_upload('100M')
        stop = mock.Mock()
        stop.wait.side_effect = [False, True]
        stop.is_set.return_value = False
        throttler = mock.Mock()
        with mock.patch.object(cloudplow_module, 'RcloneThrottler', return_value=throttler), \
                self.assertLogs('cloudplow', 'ERROR'):
            self.plow._govern_rate('nfs', script, 60, stop)
        throttler.throttle.assert_not_called()
        self.assertEqual(self.plow.rates.applied, '100M')


class HiddenCleanerTests(unittest.TestCase):
    def make(self, dry_run):
        plow = object.__new__(Cloudplow)
        plow.configs = {'hidden': {'/mnt/local/.unionfs-fuse': {'hidden_remotes': ['gd']}},
                        'remotes': {'gd': {'hidden_remote': 'gd:'}}}
        plow.core = {'rclone_binary_path': 'rclone', 'rclone_config_path': '/c'}
        plow.dry_run = dry_run
        plow.notify = mock.Mock()
        return plow

    def run_clean(self, plow, cleaned=(True, 2, 0)):
        hidden = mock.Mock()
        hidden.clean_remote.return_value = cleaned
        lock_file = mock.MagicMock()
        lock_file.is_locked.return_value = False
        with mock.patch.object(cloudplow_module, 'UnionfsHiddenFolder', return_value=hidden), \
                mock.patch.object(cloudplow_module.lock, 'hidden', return_value=lock_file):
            plow.clean_hidden()
        return hidden

    def test_dry_run_never_touches_local_whiteouts(self):
        hidden = self.run_clean(self.make(dry_run=True))
        hidden.clean_remote.assert_called_once()
        hidden.remove_local_hidden.assert_not_called()
        hidden.remove_empty_dirs.assert_not_called()

    def test_real_run_removes_local_whiteouts_after_the_remote(self):
        plow = self.make(dry_run=False)
        hidden = self.run_clean(plow)
        hidden.remove_local_hidden.assert_called_once()
        plow.notify.send.assert_called_once()

    def test_nothing_cleaned_sends_no_notification(self):
        plow = self.make(dry_run=False)
        self.run_clean(plow, cleaned=(True, 0, 0))
        plow.notify.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
