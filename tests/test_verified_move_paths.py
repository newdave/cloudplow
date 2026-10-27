"""Verified move when something goes wrong: local files must only ever be removed once provably uploaded."""
import os
import tempfile
import unittest
from unittest import mock

from utils import uploader as uploader_module
from utils.uploader import EXCEPTION_EXIT, Uploader


class FakeRclone:
    """Stands in for RcloneUploader; each step's outcome is set per test."""

    def __init__(self, listed=None, upload=(True, 0), copied=None, on_upload=None):
        self.listed, self.upload_result, self.copied, self.on_upload = listed, upload, copied, on_upload
        self.uploaded_lists, self.deleted = [], []

    def list_source(self):
        return self.listed

    def upload(self, triggers, command=None, files_from=None):
        with open(files_from, encoding='utf-8') as fp:
            self.uploaded_lists.append(fp.read().split())
        if self.on_upload:
            self.on_upload()
        return self.upload_result

    def list_destination(self, files_from):
        return self.copied

    def delete_destination_file(self, relative_path):
        self.deleted.append(relative_path)
        return True


class VerifiedMovePathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src = tmp.name

    def write(self, name, content='data'):
        path = os.path.join(self.src, name)
        with open(path, 'w') as fp:
            fp.write(content)
        return path

    def run_upload(self, fake, dry_run=False):
        remote = {'upload_folder': self.src, 'upload_remote': '/nowhere', 'verified_move': True,
                  'rclone_sleeps': {}, 'remove_empty_dir_depth': 1}
        uploader = Uploader('nfs', {}, remote, 'rclone', '/dev/null', {}, dry_run=dry_run)
        with mock.patch.object(uploader_module, 'RcloneUploader', return_value=fake):
            return uploader.upload()

    def test_unlistable_source_uploads_nothing(self):
        kept = self.write('a.mkv')
        fake = FakeRclone(listed=None)
        with self.assertLogs('utils.uploader', 'ERROR'):
            result = self.run_upload(fake)
        self.assertEqual(result.trigger, 'Rclone exception occurred')
        self.assertFalse(result.success)
        self.assertEqual(fake.uploaded_lists, [])
        self.assertTrue(os.path.exists(kept))

    def test_nothing_eligible_is_a_successful_no_op(self):
        fake = FakeRclone(listed={})
        result = self.run_upload(fake)
        self.assertTrue(result.success)
        self.assertEqual(fake.uploaded_lists, [])

    def test_file_gone_before_snapshot_is_not_uploaded(self):
        self.write('a.mkv')
        fake = FakeRclone(listed={'a.mkv': 4, 'vanished.mkv': 4}, copied={'a.mkv': 4})
        self.run_upload(fake)
        self.assertEqual(fake.uploaded_lists, [['a.mkv']])

    def test_unverifiable_destination_keeps_every_source(self):
        paths = [self.write('a.mkv'), self.write('b.mkv')]
        for upload_result, expected_code in (((True, 0), EXCEPTION_EXIT), ((True, 3), 3)):
            with self.subTest(rclone=upload_result):
                fake = FakeRclone(listed={'a.mkv': 4, 'b.mkv': 4}, upload=upload_result, copied=None)
                with self.assertLogs('utils.uploader', 'ERROR'):
                    result = self.run_upload(fake)
                self.assertFalse(result.success)
                self.assertIn(str(expected_code) if expected_code != EXCEPTION_EXIT else 'exception',
                              result.trigger)
                self.assertTrue(all(os.path.exists(p) for p in paths))

    def test_file_deleted_mid_upload_that_never_landed_needs_no_remote_cleanup(self):
        path = self.write('a.mkv')
        fake = FakeRclone(listed={'a.mkv': 4}, copied={}, on_upload=lambda: os.remove(path))
        self.run_upload(fake)
        self.assertEqual(fake.deleted, [])

    def test_dry_run_reports_but_keeps_uploaded_sources(self):
        path = self.write('a.mkv')
        fake = FakeRclone(listed={'a.mkv': 4}, copied={'a.mkv': 4})
        with self.assertLogs('utils.uploader', 'INFO') as logs:
            self.run_upload(fake, dry_run=True)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(any('Dry run: would remove' in line for line in logs.output))

    def test_failed_source_removal_is_logged_and_the_rest_continue(self):
        a, b = self.write('a.mkv'), self.write('b.mkv')
        fake = FakeRclone(listed={'a.mkv': 4, 'b.mkv': 4}, copied={'a.mkv': 4, 'b.mkv': 4})
        real_remove = os.remove

        def remove(path):
            if path == a:
                raise PermissionError(path)
            real_remove(path)

        with mock.patch.object(uploader_module.os, 'remove', side_effect=remove), \
                self.assertLogs('utils.uploader', 'ERROR'):
            result = self.run_upload(fake)
        self.assertTrue(result.success)
        self.assertTrue(os.path.exists(a))
        self.assertFalse(os.path.exists(b))

    def test_partial_copy_size_mismatch_keeps_the_source(self):
        path = self.write('a.mkv', 'full content')
        fake = FakeRclone(listed={'a.mkv': 12}, copied={'a.mkv': 5})   # destination is truncated
        self.run_upload(fake)
        self.assertTrue(os.path.exists(path))


class UploaderSmallPathTests(unittest.TestCase):
    def test_service_account_is_handed_to_rclone(self):
        uploader = Uploader('gd', {}, {'upload_folder': '/x', 'rclone_sleeps': {}}, 'rclone', '/c', {}, False)
        with self.assertLogs('utils.uploader', 'INFO'):
            uploader.set_service_account('/sa/1.json')
        fake = mock.Mock()
        fake.upload.return_value = (True, 0)
        with mock.patch.object(uploader_module, 'RcloneUploader', return_value=fake) as cls:
            uploader.upload()
        self.assertEqual(cls.call_args[0][-1], '/sa/1.json')

    def test_remove_empty_dirs_uses_configured_depth(self):
        uploader = Uploader('gd', {}, {'upload_folder': '/x', 'remove_empty_dir_depth': 2}, 'rclone', '/c', {}, False)
        with mock.patch.object(uploader_module.path, 'remove_empty_dirs', return_value=True) as remove, \
                self.assertLogs('utils.uploader', 'INFO'):
            uploader.remove_empty_dirs()
        remove.assert_called_once_with('/x', 2)


if __name__ == '__main__':
    unittest.main()
