import os
import shutil
import tempfile
import unittest
from unittest import mock

from utils import path
from utils.rclone import RcloneUploader, join_remote
from utils.uploader import Uploader


def write(file_path, content):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'w') as fp:
        fp.write(content)


def read(file_path):
    with open(file_path) as fp:
        return fp.read()


@unittest.skipUnless(shutil.which('rclone'), 'rclone is not installed')
class VerifiedMoveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src = os.path.join(tmp.name, 'local')
        self.dst = os.path.join(tmp.name, 'nfs')
        os.makedirs(self.dst)
        self.rclone_conf = os.path.join(tmp.name, 'rclone.conf')
        open(self.rclone_conf, 'w').close()

        write(os.path.join(self.src, 'M [x] {1}', 'normal.mp4'), 'normal')
        write(os.path.join(self.src, 'M [x] {1}', 'replaced.mp4'), 'old version')
        write(os.path.join(self.src, 'M [x] {1}', 'deleted.mp4'), 'deleted mid-copy')
        write(os.path.join(self.src, 'M [x] {1}', 'unreadable.mp4'), 'cannot copy')
        os.chmod(os.path.join(self.src, 'M [x] {1}', 'unreadable.mp4'), 0)

    def make_uploader(self, **remote_overrides):
        # real filters on purpose: rclone rejects them alongside --files-from-raw
        remote = {'upload_folder': self.src, 'upload_remote': self.dst, 'verified_move': True,
                  'rclone_excludes': ['**.repairing.*'],
                  'rclone_extras': {'--retries': 1, '--low-level-retries': 1, '--min-age': '0s', '--transfers': 2},
                  'rclone_sleeps': {}, **remote_overrides}
        return Uploader('nfs', {}, remote, 'rclone', self.rclone_conf, {}, dry_run=False)

    def src_path(self, name):
        return os.path.join(self.src, 'M [x] {1}', name)

    def dst_path(self, name):
        return os.path.join(self.dst, 'M [x] {1}', name)

    def test_races_are_resolved_safely(self):
        real_upload = RcloneUploader.upload

        def upload_then_race(rclone_self, *args, **kwargs):
            outcome = real_upload(rclone_self, *args, **kwargs)
            # an app replaces one file at the same path, and deletes another, after rclone listed them
            os.remove(self.src_path('replaced.mp4'))
            write(self.src_path('replaced.mp4'), 'new version')
            os.remove(self.src_path('deleted.mp4'))
            return outcome

        with mock.patch.object(RcloneUploader, 'upload', upload_then_race):
            result = self.make_uploader().upload()

        self.assertFalse(os.path.exists(self.src_path('normal.mp4')))
        self.assertEqual(read(self.dst_path('normal.mp4')), 'normal')

        self.assertEqual(read(self.src_path('replaced.mp4')), 'new version')   # new version never deleted
        self.assertEqual(read(self.dst_path('replaced.mp4')), 'old version')   # refreshed on the next run

        self.assertFalse(os.path.exists(self.dst_path('deleted.mp4')))         # no stale copy left behind

        if os.geteuid() == 0:
            return  # root can read a mode-0 file, so the failed-copy case cannot be staged
        self.assertTrue(os.path.exists(self.src_path('unreadable.mp4')))       # failed copy keeps the source
        self.assertFalse(os.path.exists(self.dst_path('unreadable.mp4')))
        self.assertFalse(result.success)                                       # rclone reported the failure

    def test_dry_run_changes_nothing(self):
        uploader = self.make_uploader()
        uploader.dry_run = True
        uploader.upload()
        self.assertTrue(os.path.exists(self.src_path('normal.mp4')))
        self.assertEqual(os.listdir(self.dst), [])

    def test_unmounted_destination_is_never_written(self):
        result = self.make_uploader(require_mounts=[self.dst]).upload()   # a plain directory, not a mount
        self.assertFalse(result.success)
        self.assertIn('not mounted', result.trigger)
        self.assertEqual(os.listdir(self.dst), [])
        self.assertTrue(os.path.exists(self.src_path('normal.mp4')))


class HelperTests(unittest.TestCase):
    def test_join_remote(self):
        self.assertEqual(join_remote('webdav:', 'a/b.mp4'), 'webdav:a/b.mp4')
        self.assertEqual(join_remote('webdav:/', 'a/b.mp4'), 'webdav:/a/b.mp4')
        self.assertEqual(join_remote('/mnt/nfs/Media/Movies/', 'a/b.mp4'), '/mnt/nfs/Media/Movies/a/b.mp4')

    def test_opened_files_can_use_sudo(self):
        with mock.patch('utils.process.run') as run:
            run.return_value.output = ''
            path.opened_files('/mnt/local/Media', sudo=True)
        self.assertEqual(run.call_args[0][0][:3], ['sudo', '-n', 'lsof'])


if __name__ == '__main__':
    unittest.main()
