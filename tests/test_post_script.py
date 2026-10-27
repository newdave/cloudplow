import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from cloudplow import Cloudplow
from utils import misc, overrides
from utils.ratelimit import RateArbiter
from utils.uploader import UploadResult


class FakeThrottler:
    def __init__(self, works=True):
        self.works = works
        self.calls = []

    def throttle(self, speed):
        self.calls.append(speed)
        return self.works


class RateTests(unittest.TestCase):
    def test_rate_to_bytes_uses_rclone_units(self):
        self.assertEqual(misc.rate_to_bytes('100M'), 100 * 1024 ** 2)
        self.assertEqual(misc.rate_to_bytes('512k'), 512 * 1024)
        self.assertEqual(misc.rate_to_bytes('1.5G'), 1.5 * 1024 ** 3)
        self.assertEqual(misc.rate_to_bytes('100Mi'), 100 * 1024 ** 2)
        self.assertEqual(misc.rate_to_bytes('64'), 64 * 1024)  # bare numbers are KiB/s
        for unparseable in ('off', '08:00,512k 12:00,10M', '10M:5M', None, ''):
            self.assertIsNone(misc.rate_to_bytes(unparseable))

    def test_lower_rate(self):
        self.assertEqual(misc.lower_rate('500M', '100M'), '100M')
        self.assertEqual(misc.lower_rate('50M', '100M'), '50M')
        self.assertEqual(misc.lower_rate('500M', None), '500M')
        self.assertEqual(misc.lower_rate('500M', 'off'), '500M')


class OverridesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = overrides.overrides_path(self.tmp.name, 'nfs-tv')
        os.makedirs(os.path.dirname(self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, content):
        with open(self.path, 'w', encoding='utf-8') as fp:
            fp.write(content if isinstance(content, str) else json.dumps(content))

    def test_missing_and_invalid_files_yield_nothing(self):
        self.assertEqual(overrides.load(self.path), {})
        self.write('{not json')
        self.assertEqual(overrides.load(self.path), {})
        self.write([1, 2])
        self.assertEqual(overrides.load(self.path), {})

    def test_only_tuning_flags_are_accepted(self):
        self.write({'--transfers': 4, '--bwlimit': '100M', '--exclude': '**', '--min-age': '0s',
                    '--checkers': True, '--tpslimit': None})
        self.assertEqual(overrides.load(self.path), {'--transfers': 4, '--bwlimit': '100M'})

    def test_apply_merges_without_mutating(self):
        remote = {'upload_folder': '/a', 'rclone_extras': {'--transfers': 2, '--min-age': '15m'}}
        merged = overrides.apply(remote, {'--transfers': 6})
        self.assertEqual(merged['rclone_extras'], {'--transfers': 6, '--min-age': '15m'})
        self.assertEqual(remote['rclone_extras']['--transfers'], 2)


class PostScriptEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plow = object.__new__(Cloudplow)
        self.plow.configs = {'remotes': {'nfs-tv': {'upload_folder': '/mnt/local/Media/TV',
                                                    'upload_remote': '/mnt/nfs/Media/TV'}}}
        self.plow.config_dir = self.tmp.name
        self.plow.dry_run = False

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_mapping(self):
        cases = [
            (UploadResult(0, '', True), 'success', ''),
            (UploadResult(0, 'Required mount(s) not mounted: /mnt/nfs', False), 'failed',
             'Required mount(s) not mounted: /mnt/nfs'),
            (UploadResult(25, "Rclone's 'Max Transfer Reached' signal", True), 'aborted',
             "Rclone's 'Max Transfer Reached' signal"),
            (None, 'error', 'Exception occurred while uploading'),
        ]
        for result, status, reason in cases:
            env = self.plow.post_script_env('nfs-tv', result)
            self.assertEqual((env['CLOUDPLOW_STATUS'], env['CLOUDPLOW_REASON']), (status, reason))

        env = self.plow.post_script_env('nfs-tv', UploadResult(0, '', True))
        self.assertEqual(env['CLOUDPLOW_UPLOAD_REMOTE'], '/mnt/nfs/Media/TV')
        self.assertEqual(env['CLOUDPLOW_OVERRIDES_FILE'], os.path.join(self.tmp.name, 'overrides', 'nfs-tv.json'))
        self.assertEqual(env['CLOUDPLOW_DRY_RUN'], '0')

    def test_script_receives_environment(self):
        script = os.path.join(self.tmp.name, 'post.sh')
        out = os.path.join(self.tmp.name, 'out')
        with open(script, 'w', encoding='utf-8') as fp:
            fp.write(f'#!/bin/sh\necho "$CLOUDPLOW_REMOTE $CLOUDPLOW_STATUS $PATH" > {out}\n')
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)

        self.plow._run_post_script('nfs-tv', script, UploadResult(0, '', True))
        with open(out, encoding='utf-8') as fp:
            remote, status, path_var = fp.read().split(' ', 2)
        self.assertEqual((remote, status), ('nfs-tv', 'success'))
        self.assertTrue(path_var.strip(), "the inherited environment (PATH) must be kept")


class RateArbiterTests(unittest.TestCase):
    def test_slower_limit_wins_and_plex_release_restores_load_limit(self):
        rates, rclone = RateArbiter(), FakeThrottler()
        rates.start_upload('100M')            # rclone started with --bwlimit=100M
        self.assertTrue(rates.set_plex('500M', rclone))
        self.assertEqual(rclone.calls, [])     # already slower than the Plex throttle
        rates.set_plex('50M', rclone)
        rates.set_plex(None, rclone)           # streams stopped: back to the load limit, not 'off'
        rates.set_load('off', rclone)
        self.assertEqual(rclone.calls, ['50M', '100M', 'off'])

    def test_unlimited_load_defers_to_plex(self):
        rates, rclone = RateArbiter(), FakeThrottler()
        rates.start_upload('off')
        rates.set_plex('400M', rclone)
        self.assertEqual(rclone.calls, ['400M'])

    def test_force_pushes_a_rate_that_looks_applied(self):
        rates, rclone = RateArbiter(), FakeThrottler()
        rates.start_upload(None)
        rates.set_plex('50M', rclone)
        rates.set_plex('50M', rclone)              # unchanged: nothing to send
        rates.set_plex('50M', rclone, force=True)  # a new rclone started without the limit
        self.assertEqual(rclone.calls, ['50M', '50M'])

    def test_failed_push_is_retried(self):
        rates, rclone = RateArbiter(), FakeThrottler(works=False)
        rates.start_upload(None)
        self.assertFalse(rates.set_load('20M', rclone))
        self.assertEqual(rates.applied, 'off')
        rclone.works = True
        self.assertTrue(rates.set_load('20M', rclone))
        self.assertEqual((rclone.calls, rates.applied), (['20M', '20M'], '20M'))


class RateGovernorTests(unittest.TestCase):
    class Ticks:
        """A stop event that lets the governor loop run `count` times."""
        def __init__(self, count):
            self.remaining = count

        def wait(self, _timeout):
            self.remaining -= 1
            return self.remaining < 0

        def is_set(self):
            return False

    def test_running_upload_follows_the_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            plow = object.__new__(Cloudplow)
            plow.configs = {'remotes': {'nfs-tv': {}}, 'plex': {'enabled': True, 'rclone': {'url': 'http://x'}}}
            plow.config_dir, plow.dry_run, plow.rates = tmp, False, RateArbiter()
            plow.rates.start_upload('off')

            # each run picks the next limit from a list, like load rising then falling again
            script = os.path.join(tmp, 'rate.sh')
            with open(script, 'w', encoding='utf-8') as fp:
                fp.write('#!/bin/sh\n'
                         '[ "$CLOUDPLOW_STATUS" = running ] || exit 9\n'
                         'n=$(cat "$0.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$0.n"\n'
                         'set -- 50M 50M 200M; shift "$n"\n'
                         'mkdir -p "$(dirname "$CLOUDPLOW_OVERRIDES_FILE")"\n'
                         'echo "{\\"--bwlimit\\": \\"$1\\"}" > "$CLOUDPLOW_OVERRIDES_FILE"\n')
            os.chmod(script, 0o755)

            rclone = FakeThrottler()
            with mock.patch('cloudplow.RcloneThrottler', return_value=rclone):
                plow._govern_rate('nfs-tv', script, 60, self.Ticks(3))
            self.assertEqual(rclone.calls, ['50M', '200M'])
            self.assertEqual(plow.rates.applied, '200M')


if __name__ == '__main__':
    unittest.main()
