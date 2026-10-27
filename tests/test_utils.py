import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from utils import lock, misc, path, process
from utils.rclone import RcloneUploader, drive_upstreams, excludes_to_args, extras_to_args, rc_args
from utils.triggers import TriggerTracker
from utils.unionfs import UnionfsHiddenFolder
from utils.uploader import Uploader, rclone_glob_escape


class MiscTests(unittest.TestCase):
    SPEEDS = {'0': 'A', '5': 'B', '7': 'C', '11': 'D', '20': 'E'}

    def test_nearest_less_picks_largest_key_not_above(self):
        self.assertEqual(misc.get_nearest_less_element(self.SPEEDS, 6), 'B')
        self.assertEqual(misc.get_nearest_less_element(self.SPEEDS, 16), 'D')
        self.assertEqual(misc.get_nearest_less_element(self.SPEEDS, 20), 'E')
        self.assertEqual(misc.get_nearest_less_element({'3': 'X', '9': 'Y'}, 1), 'X')

    def test_time_window_wraps_midnight(self):
        self.assertTrue(misc.is_time_between(('22:00', '04:00'), '23:30'))
        self.assertTrue(misc.is_time_between(('22:00', '04:00'), '03:59'))
        self.assertFalse(misc.is_time_between(('22:00', '04:00'), '12:00'))
        self.assertTrue(misc.is_time_between(('04:00', '08:00'), '05:00'))

    def test_seconds_to_string(self):
        self.assertEqual(misc.seconds_to_string(90061), '1 days, 1 hours, 1 minutes and 1 seconds')
        self.assertEqual(misc.seconds_to_string(3600), '1 hours')
        self.assertEqual(misc.seconds_to_string(0), '0 seconds')

    def test_lowest_remaining_time(self):
        self.assertEqual(misc.get_lowest_remaining_time({'a': 5.0, 'b': 3.0}), 3.0)
        self.assertIsNone(misc.get_lowest_remaining_time({'a': 5.0, 'b': None}))

    def test_sorted_naturally(self):
        self.assertEqual(misc.sorted_naturally(['sa10.json', 'sa2.json', 'sa1.json']),
                         ['sa1.json', 'sa2.json', 'sa10.json'])


class RcloneArgTests(unittest.TestCase):
    def test_extras_and_excludes(self):
        self.assertEqual(extras_to_args({'--transfers': 2, '--skip-links': None, '--min-age': '15m'}),
                         ['--transfers=2', '--skip-links', '--min-age=15m'])
        self.assertEqual(excludes_to_args(['**.mkv', '/a b/\\[x\\].mp4']),
                         ['--exclude=**.mkv', '--exclude=/a b/\\[x\\].mp4'])

    def test_rc_args(self):
        self.assertEqual(rc_args({'enabled': False}), [])
        self.assertEqual(rc_args({'enabled': True, 'rclone': {'url': 'http://localhost:7949/'}}),
                         ['--rc', '--rc-no-auth', '--rc-addr=localhost:7949'])

    def test_drive_upstreams_through_layers(self):
        remotes = {
            'crypt': {'type': 'crypt', 'remote': 'union:'},
            'union': {'type': 'union', 'upstreams': 'gd1:/a gd2:/b chunk:'},
            'chunk': {'type': 'chunker', 'remote': 'gd3:'},
            'gd1': {'type': 'drive'}, 'gd2': {'type': 'drive'}, 'gd3': {'type': 'drive'},
            'web': {'type': 'webdav'},
        }
        self.assertEqual(drive_upstreams(remotes, 'crypt'), ['gd1', 'gd2', 'gd3'])
        self.assertEqual(drive_upstreams(remotes, 'web'), [])
        with self.assertRaises(ValueError):
            drive_upstreams(remotes, 'missing')

    def test_upload_never_runs_sync(self):
        config = {'upload_folder': '/src', 'upload_remote': 'r:/dst', 'rclone_command': 'sync'}
        with mock.patch('utils.process.stream', return_value=process.Result(0, '')) as stream:
            RcloneUploader('r', config, 'rclone', '/rc.conf').upload(lambda line: False)
        self.assertEqual(stream.call_args[0][0][:4], ['rclone', 'move', '/src', 'r:/dst'])


class UploaderTests(unittest.TestCase):
    def make_uploader(self, open_files=()):
        rclone_config = {'upload_folder': '/mnt/local/Media', 'upload_remote': 'r:/', 'rclone_excludes': ['x'],
                         'rclone_sleeps': {'429': {'count': 2, 'timeout': 60, 'sleep': 3}}}
        uploader = Uploader('r', {'exclude_open_files': True, 'opened_excludes': ['/downloads/']},
                            rclone_config, 'rclone', '/rc.conf', {}, dry_run=True)
        patcher = mock.patch('utils.path.opened_files', return_value=list(open_files))
        patcher.start()
        self.addCleanup(patcher.stop)
        return uploader

    def test_glob_escape(self):
        self.assertEqual(rclone_glob_escape('/A {tmdb-1}/B [x]*?.mp4'), '/A \\{tmdb-1\\}/B \\[x\\]\\*\\?.mp4')

    def test_open_files_become_anchored_excludes_without_mutating_config(self):
        uploader = self.make_uploader(['/mnt/local/Media/M/a [1].mp4', '/mnt/local/Media/downloads/b.mp4'])
        with mock.patch('utils.uploader.RcloneUploader') as rclone_cls:
            rclone_cls.return_value.upload.return_value = (True, 0)
            result = uploader.upload()
        excludes = rclone_cls.call_args[0][1]['rclone_excludes']
        self.assertEqual(excludes, ['x', '/M/a \\[1\\].mp4'])
        self.assertEqual(uploader.rclone_config['rclone_excludes'], ['x'])
        self.assertEqual(result, (0, '', True))

    def test_result_codes(self):
        uploader = self.make_uploader()

        def run(code, trip=None):
            def fake_upload(callback):
                if trip:
                    callback(trip)
                    callback(trip)
                return True, code
            with mock.patch('utils.uploader.RcloneUploader') as rclone_cls:
                rclone_cls.return_value.upload.side_effect = fake_upload
                return uploader.upload()

        self.assertEqual(run(7), (25, "Rclone's 'Max Transfer Reached' signal", True))
        self.assertEqual(run(-9, trip='HTTP error 429'), (3, '429', True))
        self.assertFalse(run(1).success)


class TriggerTests(unittest.TestCase):
    def test_trips_after_count_and_resets_after_timeout(self):
        tracker = TriggerTracker({'rate limit': {'count': 3, 'timeout': 60, 'sleep': 2}})
        with mock.patch('time.time', return_value=1000):
            self.assertFalse(tracker('ERROR: Rate Limit hit'))
            self.assertFalse(tracker('ERROR: rate limit hit'))
        with mock.patch('time.time', return_value=1100):
            self.assertFalse(tracker('rate limit'))  # earlier hits expired, count restarts at 1
            self.assertFalse(tracker('rate limit'))
            self.assertTrue(tracker('rate limit'))
        self.assertEqual((tracker.delay_hours, tracker.tripped), (2, 'rate limit'))


class PathAndHiddenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        os.makedirs(os.path.join(root, 'TV', 'Show_HIDDEN~'))
        os.makedirs(os.path.join(root, 'TV', 'Keep'))
        open(os.path.join(root, 'TV', 'Keep', 'EPISODE.mkv_HIDDEN~'), 'w').close()
        open(os.path.join(root, 'TV', 'Keep', 'other.mkv'), 'w').close()

    def test_find_items_files_and_dirs(self):
        root = self.tmp.name
        self.assertEqual(path.find_items(root, '_HIDDEN~', files=True),
                         [os.path.join(root, 'TV', 'Keep', 'EPISODE.mkv_HIDDEN~')])
        self.assertEqual(path.find_items(root, '_HIDDEN~'), [os.path.join(root, 'TV', 'Show_HIDDEN~')])

    def test_hidden_maps_to_remote_without_overstripping(self):
        hidden = UnionfsHiddenFolder(self.tmp.name, True, 'rclone', '/rc.conf')
        self.assertEqual(len(hidden.hidden_files), 1)
        mapped = hidden._hidden2remote({'hidden_remote': 'gd:'}, hidden.hidden_files[0])
        self.assertEqual(mapped, 'gd:/TV/Keep/EPISODE.mkv')


class ProcessTests(unittest.TestCase):
    def test_stream_keeps_every_line(self):
        result = process.stream([sys.executable, '-c', 'print("a"); print("b"); print("c")'], logs=False)
        self.assertEqual(result, (0, 'a\nb\nc'))

    def test_callback_kills_process(self):
        script = 'import time\nprint("stop", flush=True)\ntime.sleep(30)'
        result = process.stream([sys.executable, '-c', script], callback=lambda line: line == 'stop', logs=False)
        self.assertEqual(result.returncode, -9)

    def test_invalid_utf8_is_replaced(self):
        result = process.stream([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"bad\\xff\\n")'],
                                logs=False)
        self.assertEqual(result.output, 'bad\ufffd')


class LockTests(unittest.TestCase):
    def test_lock_visible_to_other_instances_and_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, 'upload.flock')
            holder, observer = lock.FileLock(lock_path), lock.FileLock(lock_path)
            self.assertFalse(observer.is_locked())
            with holder:
                self.assertTrue(observer.is_locked())
                probe = ('import fcntl,sys; f=open(sys.argv[1],"a")\n'
                         'try: fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
                         'except BlockingIOError: sys.exit(3)')
                self.assertEqual(subprocess.run([sys.executable, '-c', probe, lock_path]).returncode, 3)
            self.assertFalse(observer.is_locked())


if __name__ == '__main__':
    unittest.main()
