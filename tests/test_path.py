import os
import shutil
import tempfile
import unittest
from unittest import mock

from utils import path, process


def touch(file_path, content=''):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'w') as fp:
        fp.write(content)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def p(self, *parts):
        return os.path.join(self.root, *parts)


class FindItemsTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.p('A', 'B', 'C'))
        os.makedirs(self.p('A', 'X_hidden~'))
        touch(self.p('A', 'B', 'f.mkv'))
        touch(self.p('top.txt'))

    def test_without_suffix_returns_every_directory_deepest_first(self):
        found = path.find_items(self.root)
        self.assertEqual(set(found), {self.p('A'), self.p('A', 'B'), self.p('A', 'B', 'C'), self.p('A', 'X_hidden~')})
        depths = [item.count(os.sep) for item in found]
        self.assertEqual(depths, sorted(depths, reverse=True))   # children are removed before their parents
        self.assertEqual(found[0], self.p('A', 'B', 'C'))

    def test_suffix_is_case_insensitive(self):
        self.assertEqual(path.find_items(self.root, '_HIDDEN~'), [self.p('A', 'X_hidden~')])

    def test_files_mode_ignores_directories(self):
        self.assertEqual(path.find_items(self.root, files=True), [self.p('A', 'B', 'f.mkv'), self.p('top.txt')])
        self.assertEqual(path.find_items(self.root, '.mkv', files=True), [self.p('A', 'B', 'f.mkv')])

    def test_depth_limits_how_far_down_items_are_collected(self):
        self.assertEqual(path.find_items(self.root, depth=1), [self.p('A')])
        self.assertEqual(set(path.find_items(self.root, depth=2)),
                         {self.p('A'), self.p('A', 'B'), self.p('A', 'X_hidden~')})
        self.assertEqual(path.find_items(self.root, files=True, depth=1), [self.p('top.txt')])

    def test_depth_with_trailing_slash(self):
        self.assertEqual(path.find_items(self.root + os.sep, depth=1), [self.p('A')])

    def test_missing_folder_yields_nothing(self):
        self.assertEqual(path.find_items(self.p('missing')), [])


class OpenedFilesTests(TempDirTestCase):
    def test_parses_lsof_name_records_keeping_existing_files_only(self):
        touch(self.p('b.mkv'))
        touch(self.p('a.mkv'))
        output = '\n'.join(['p123', 'fcwd', f'n{self.root}', 'f3', f'n{self.p("b.mkv")}', 'p456',
                            f'n{self.p("a.mkv")}', f'n{self.p("b.mkv")}', f'n{self.p("gone.mkv")}'])
        with mock.patch('utils.process.run', return_value=process.Result(0, output)) as run:
            found = path.opened_files(self.root)
        self.assertEqual(found, [self.p('a.mkv'), self.p('b.mkv')])   # deduplicated, sorted, dirs dropped
        self.assertEqual(run.call_args[0][0], ['lsof', '-w', '-F', 'n', '+D', self.root])

    def test_lsof_failure_without_output_means_no_open_files(self):
        with mock.patch('utils.process.run', return_value=process.Result(1, '')):
            self.assertEqual(path.opened_files(self.root), [])

    @unittest.skipUnless(shutil.which('lsof'), 'lsof is not installed')
    def test_real_lsof_sees_a_file_held_open_by_this_process(self):
        touch(self.p('sub', 'writing.mkv'))
        touch(self.p('sub', 'closed.mkv'))
        with open(self.p('sub', 'writing.mkv'), 'a'):
            found = path.opened_files(self.root)
        self.assertIn(self.p('sub', 'writing.mkv'), found)
        self.assertNotIn(self.p('sub', 'closed.mkv'), found)


class DeleteTests(TempDirTestCase):
    def test_single_string_path(self):
        touch(self.p('a.mkv'))
        path.delete(self.p('a.mkv'))
        self.assertFalse(os.path.exists(self.p('a.mkv')))

    def test_files_and_empty_dirs_removed_missing_skipped(self):
        touch(self.p('a.mkv'))
        os.makedirs(self.p('empty'))
        touch(self.p('keep.mkv'))
        path.delete([self.p('a.mkv'), self.p('missing'), self.p('empty')])
        self.assertEqual(os.listdir(self.root), ['keep.mkv'])

    def test_non_empty_directory_is_never_removed_recursively(self):
        touch(self.p('dir', 'precious.mkv'))
        touch(self.p('after.mkv'))
        with self.assertLogs('utils.path', 'ERROR'):
            path.delete([self.p('dir'), self.p('after.mkv')])   # failure must not stop the remaining items
        self.assertTrue(os.path.exists(self.p('dir', 'precious.mkv')))
        self.assertFalse(os.path.exists(self.p('after.mkv')))

    @unittest.skipIf(os.geteuid() == 0, 'root ignores directory permissions')
    def test_permission_error_is_logged_not_raised(self):
        touch(self.p('ro', 'a.mkv'))
        os.chmod(self.p('ro'), 0o500)
        self.addCleanup(os.chmod, self.p('ro'), 0o700)
        with self.assertLogs('utils.path', 'ERROR'):
            path.delete(self.p('ro', 'a.mkv'))
        self.assertTrue(os.path.exists(self.p('ro', 'a.mkv')))


class RemoveEmptyDirsTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.p('Movies', 'Empty', 'Nested'))
        os.makedirs(self.p('TV'))
        touch(self.p('Movies', 'Full', 'film.mkv'))

    def test_removes_empty_trees_but_keeps_root_and_content(self):
        self.assertTrue(path.remove_empty_dirs(self.root, 1))
        self.assertTrue(os.path.isdir(self.root))
        self.assertEqual(sorted(os.listdir(self.root)), ['Movies'])
        self.assertEqual(os.listdir(self.p('Movies')), ['Full'])
        self.assertTrue(os.path.exists(self.p('Movies', 'Full', 'film.mkv')))

    def test_min_depth_protects_shallower_directories(self):
        self.assertTrue(path.remove_empty_dirs(self.root, 2))
        self.assertTrue(os.path.isdir(self.p('TV')))                 # depth 1: kept even though empty
        self.assertFalse(os.path.exists(self.p('Movies', 'Empty')))  # depth 2+: removed

    def test_missing_folder(self):
        with mock.patch('utils.process.run') as run, self.assertLogs('utils.path', 'ERROR'):
            self.assertFalse(path.remove_empty_dirs(self.p('missing'), 1))
        run.assert_not_called()

    def test_find_failure_reported(self):
        with mock.patch('utils.process.run', return_value=process.Result(1, 'find: boom')), \
                self.assertLogs('utils.path', 'ERROR'):
            self.assertFalse(path.remove_empty_dirs(self.root, 1))


class GetSizeTests(TempDirTestCase):
    def test_non_numeric_du_output_is_zero(self):
        with mock.patch('utils.process.run', return_value=process.Result(0, 'garbage\t/x')), \
                self.assertLogs('utils.path', 'ERROR'):
            self.assertEqual(path.get_size(self.root), 0)


if __name__ == '__main__':
    unittest.main()
