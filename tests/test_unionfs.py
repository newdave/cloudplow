import os
import shutil
import tempfile
import unittest
from unittest import mock

from utils import process
from utils.unionfs import UnionfsHiddenFolder


def touch(file_path, content='x'):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'w') as fp:
        fp.write(content)


class HiddenTestCase(unittest.TestCase):
    """A unionfs-fuse hidden folder and a local directory standing in for the rclone remote."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hidden = os.path.join(tmp.name, '.unionfs-fuse')
        self.remote = os.path.join(tmp.name, 'remote')
        self.rclone_conf = os.path.join(tmp.name, 'rclone.conf')
        open(self.rclone_conf, 'w').close()

        # whiteouts: a deleted episode, a deleted (empty on the remote) folder, and a file whose remote is gone
        touch(self.h('TV', 'Show', 'S01E01.mkv_HIDDEN~'))
        touch(self.h('Movies', 'Gone_HIDDEN~', 'unused'))
        os.makedirs(self.h('Movies', 'Gone_HIDDEN~'), exist_ok=True)
        touch(self.h('TV', 'Show', 'never-uploaded.mkv_HIDDEN~'))
        touch(self.h('TV', 'Show', 'not-hidden.txt'))

        touch(self.r('TV', 'Show', 'S01E01.mkv'), 'episode 1')
        touch(self.r('TV', 'Show', 'S01E02.mkv'), 'episode 2')         # sibling: must survive
        touch(self.r('TV', 'Show', 'S01E01.mkv.srt'), 'subtitle')      # name prefix: must survive
        touch(self.r('TV', 'Show', 'S01E01.mkv_HIDDEN~'), 'literal')   # must survive (suffix is stripped)
        os.makedirs(self.r('Movies', 'Gone'))
        touch(self.r('Movies', 'Kept', 'film.mkv'))

    def h(self, *parts):
        return os.path.join(self.hidden, *parts)

    def r(self, *parts):
        return os.path.join(self.remote, *parts)

    def make(self, dry_run=False, binary='rclone'):
        return UnionfsHiddenFolder(self.hidden, dry_run, binary, self.rclone_conf)

    def remote_files(self):
        found = set()
        for root, dirs, files in os.walk(self.remote):
            for name in dirs + files:
                found.add(os.path.relpath(os.path.join(root, name), self.remote))
        return found


class DiscoveryAndMappingTests(HiddenTestCase):
    def test_finds_hidden_files_and_folders_only(self):
        hidden = self.make()
        self.assertEqual(sorted(hidden.hidden_files),
                         [self.h('TV', 'Show', 'S01E01.mkv_HIDDEN~'), self.h('TV', 'Show', 'never-uploaded.mkv_HIDDEN~')])
        self.assertEqual(hidden.hidden_folders, [self.h('Movies', 'Gone_HIDDEN~')])

    def test_missing_hidden_folder_finds_nothing(self):
        hidden = UnionfsHiddenFolder(self.h('nope'), False, 'rclone', self.rclone_conf)
        self.assertEqual((hidden.hidden_files, hidden.hidden_folders), ([], []))

    def test_find_error_is_contained(self):
        with mock.patch('utils.path.find_items', side_effect=PermissionError('denied')), \
                self.assertLogs('utils.unionfs', 'ERROR'):
            hidden = self.make()
        self.assertEqual((hidden.hidden_files, hidden.hidden_folders), ([], []))

    def test_mapping_onto_remote_sub_path(self):
        hidden = self.make()
        self.assertEqual(hidden._hidden2remote({'hidden_remote': 'gd:/Media'}, self.h('TV', 'a.mkv_HIDDEN~')),
                         'gd:/Media/TV/a.mkv')
        self.assertEqual(hidden._hidden2remote({'hidden_remote': 'gd:'}, self.h('Movies', 'Gone_HIDDEN~')),
                         'gd:/Movies/Gone')

    def test_only_the_trailing_suffix_is_stripped(self):
        hidden = self.make()
        mapped = hidden._hidden2remote({'hidden_remote': 'gd:'}, self.h('a_HIDDEN~dir', 'b_HIDDEN~_HIDDEN~'))
        self.assertEqual(mapped, 'gd:/a_HIDDEN~dir/b_HIDDEN~')

    def test_unmappable_paths_return_none(self):
        hidden = self.make()
        with self.assertLogs('utils.unionfs', 'ERROR'):
            self.assertIsNone(hidden._hidden2remote({}, self.h('TV', 'a_HIDDEN~')))
            self.assertIsNone(hidden._hidden2remote({'hidden_remote': ''}, self.h('TV', 'a_HIDDEN~')))
            self.assertIsNone(hidden._hidden2remote({'hidden_remote': 'gd:'}, '/elsewhere/a_HIDDEN~'))

    def test_trailing_slash_on_hidden_folder(self):
        hidden = UnionfsHiddenFolder(self.hidden + '/', False, 'rclone', self.rclone_conf)
        self.assertEqual(hidden._hidden2remote({'hidden_remote': 'gd:/Media'}, self.h('TV', 'a.mkv_HIDDEN~')),
                         'gd:/Media/TV/a.mkv')


@unittest.skipUnless(shutil.which('rclone'), 'rclone is not installed')
class CleanRemoteWithRcloneTests(HiddenTestCase):
    def test_deletes_exactly_the_whited_out_items(self):
        before = self.remote_files()
        with self.assertLogs('utils.unionfs', 'INFO'):
            completed, deleted, failed = self.make().clean_remote('local', {'hidden_remote': self.remote})
        self.assertEqual((completed, deleted, failed), (True, 2, 1))   # never-uploaded.mkv is missing remotely
        self.assertEqual(before - self.remote_files(), {os.path.join('TV', 'Show', 'S01E01.mkv'),
                                                        os.path.join('Movies', 'Gone')})
        self.assertEqual(self.remote_files() - before, set())
        # local whiteouts are untouched by the remote clean
        self.assertTrue(os.path.exists(self.h('TV', 'Show', 'S01E01.mkv_HIDDEN~')))

    def test_dry_run_deletes_nothing(self):
        before = self.remote_files()
        with self.assertLogs('utils.unionfs', 'INFO'):
            completed, _, _ = self.make(dry_run=True).clean_remote('local', {'hidden_remote': self.remote})
        self.assertTrue(completed)
        self.assertEqual(self.remote_files(), before)

    def test_non_empty_remote_folder_is_not_purged(self):
        touch(self.r('Movies', 'Gone', 'still-here.mkv'))
        with self.assertLogs('utils.unionfs', 'ERROR'):
            _, deleted, failed = self.make().clean_remote('local', {'hidden_remote': self.remote})
        self.assertTrue(os.path.exists(self.r('Movies', 'Gone', 'still-here.mkv')))
        self.assertEqual((deleted, failed), (1, 2))


class CleanRemoteTests(HiddenTestCase):
    def test_remote_without_hidden_remote_deletes_nothing(self):
        with mock.patch('utils.process.run') as run, self.assertLogs('utils.unionfs', 'ERROR'):
            completed, deleted, failed = self.make().clean_remote('gd', {})
        run.assert_not_called()
        self.assertEqual((completed, deleted, failed), (True, 0, 3))

    def test_rclone_commands_and_failure_text(self):
        calls = []

        def fake_run(args, logs=False):
            calls.append(args)
            # rclone may exit 0 but still report the failure in its output
            return process.Result(0, 'NOTICE: Failed to delete: x' if 'never-uploaded' in args[2] else '')

        with mock.patch('utils.process.run', side_effect=fake_run), self.assertLogs('utils.unionfs', 'INFO'):
            completed, deleted, failed = self.make(binary='/bin/rclone').clean_remote('gd', {'hidden_remote': 'gd:'})
        self.assertEqual((completed, deleted, failed), (True, 2, 1))
        self.assertIn(['/bin/rclone', 'delete', 'gd:/TV/Show/S01E01.mkv', f'--config={self.rclone_conf}'], calls)
        self.assertIn(['/bin/rclone', 'rmdir', 'gd:/Movies/Gone', f'--config={self.rclone_conf}'], calls)
        self.assertEqual(len(calls), 3)

    def test_dry_run_passes_dry_run_to_every_rclone_call(self):
        with mock.patch('utils.process.run', return_value=process.Result(0, '')) as run:
            self.make(dry_run=True).clean_remote('gd', {'hidden_remote': 'gd:'})
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            self.assertEqual(call[0][0][-1], '--dry-run')

    def test_unexpected_exception_reports_incomplete(self):
        with mock.patch('utils.process.run', return_value=process.Result(0, '')), \
                mock.patch('utils.unionfs.RcloneUploader.delete_folder', side_effect=RuntimeError('boom')), \
                self.assertLogs('utils.unionfs', 'ERROR'):
            completed, deleted, failed = self.make().clean_remote('gd', {'hidden_remote': 'gd:'})
        self.assertEqual((completed, deleted, failed), (False, 2, 0))

    def test_nothing_hidden_runs_nothing(self):
        hidden = UnionfsHiddenFolder(self.r('Movies'), False, 'rclone', self.rclone_conf)
        with mock.patch('utils.process.run') as run:
            self.assertEqual(hidden.clean_remote('gd', {'hidden_remote': 'gd:'}), (True, 0, 0))
        run.assert_not_called()


class LocalCleanupTests(HiddenTestCase):
    def test_remove_local_hidden_keeps_other_files_and_non_empty_folders(self):
        hidden = self.make()
        with self.assertLogs('utils.path', 'ERROR'):   # Gone_HIDDEN~ still contains 'unused'
            hidden.remove_local_hidden()
        self.assertFalse(os.path.exists(self.h('TV', 'Show', 'S01E01.mkv_HIDDEN~')))
        self.assertFalse(os.path.exists(self.h('TV', 'Show', 'never-uploaded.mkv_HIDDEN~')))
        self.assertTrue(os.path.exists(self.h('TV', 'Show', 'not-hidden.txt')))
        self.assertTrue(os.path.exists(self.h('Movies', 'Gone_HIDDEN~', 'unused')))

    def test_remove_local_hidden_removes_empty_hidden_folders(self):
        os.remove(self.h('Movies', 'Gone_HIDDEN~', 'unused'))
        self.make().remove_local_hidden()
        self.assertFalse(os.path.exists(self.h('Movies', 'Gone_HIDDEN~')))
        self.assertTrue(os.path.isdir(self.h('Movies')))

    def test_nothing_hidden_deletes_nothing_locally(self):
        hidden = UnionfsHiddenFolder(self.r('Movies'), False, 'rclone', self.rclone_conf)
        with mock.patch('utils.path.delete') as delete:
            hidden.remove_local_hidden()
        delete.assert_not_called()

    def test_remove_empty_dirs_on_vanished_folder(self):
        hidden = self.make()
        shutil.rmtree(self.hidden)
        with self.assertLogs('utils.path', 'ERROR'):
            hidden.remove_empty_dirs()
        self.assertFalse(os.path.exists(self.hidden))

    def test_remove_empty_dirs_keeps_the_hidden_root(self):
        shutil.rmtree(self.h('Movies', 'Gone_HIDDEN~'))
        hidden = self.make()
        hidden.remove_empty_dirs()
        self.assertFalse(os.path.exists(self.h('Movies')))
        self.assertTrue(os.path.exists(self.h('TV', 'Show', 'not-hidden.txt')))
        shutil.rmtree(self.h('TV'))
        hidden.remove_empty_dirs()
        self.assertTrue(os.path.isdir(self.hidden))


if __name__ == '__main__':
    unittest.main()
