import io
import logging
import logging.handlers
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import warnings
from unittest import mock

from git import GitCommandError, InvalidGitRepositoryError

from utils import cache, decorators, lock, logs, version


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)


# ---------------------------------------------------------------------------------------------- version

class VersionTests(unittest.TestCase):
    def repo(self, current='a' * 40, latest='a' * 40, branch='master'):
        repo = mock.Mock()
        repo.active_branch.name = branch
        repo.active_branch.commit = current
        fetched = mock.Mock()
        fetched.commit = latest
        repo.remotes.origin.fetch.return_value = [fetched]
        return repo

    def check(self, repo=None, side_effect=None):
        with mock.patch.object(version, 'Repo', return_value=repo, side_effect=side_effect) as repo_cls:
            with self.assertLogs('utils.version', 'DEBUG') as cm:
                self.assertIsNone(version.check_version())
        return repo_cls, cm.output

    def test_up_to_date(self):
        repo_cls, output = self.check(self.repo())
        self.assertIn('You are using the latest master: ' + 'a' * 40, output[0])
        self.assertEqual(repo_cls.call_args[0][0], os.path.dirname(os.path.realpath(sys.argv[0])))

    def test_behind_origin(self):
        _, output = self.check(self.repo(current='a' * 40, latest='b' * 40, branch='develop'))
        self.assertIn('You are NOT using the latest develop: ' + 'b' * 40, output[0])

    def test_failures_are_swallowed(self):
        detached = self.repo()
        type(detached).active_branch = mock.PropertyMock(side_effect=TypeError('HEAD is a detached symbolic reference'))
        no_origin = self.repo()
        del no_origin.remotes.origin
        fetch_fails = self.repo()
        fetch_fails.remotes.origin.fetch.side_effect = GitCommandError('git fetch', 128)
        nothing_fetched = self.repo()
        nothing_fetched.remotes.origin.fetch.return_value = []
        cases = {'not a repo': dict(side_effect=InvalidGitRepositoryError('/opt/cloudplow')),
                 'detached': dict(repo=detached), 'no origin': dict(repo=no_origin),
                 'offline': dict(repo=fetch_fails), 'empty fetch': dict(repo=nothing_fetched)}
        for name, kwargs in cases.items():
            with self.subTest(name):
                _, output = self.check(**kwargs)
                self.assertEqual(len(output), 1)
                self.assertIn('Unable to check version', output[0])


# ---------------------------------------------------------------------------------------------- lock

class LockTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.folder = os.path.join(self.tmp, 'locks')
        patcher = mock.patch.object(lock, 'lock_folder', self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_named_locks_live_in_lock_folder(self):
        lock.ensure_lock_folder()
        lock.ensure_lock_folder()   # existing folder is fine
        self.assertTrue(os.path.isdir(self.folder))
        for factory, name in ((lock.upload, 'upload'), (lock.sync, 'sync'), (lock.hidden, 'hidden')):
            self.assertEqual(factory().path, os.path.join(self.folder, f'{name}.flock'))

    def test_uncreatable_lock_folder_exits(self):
        blocker = os.path.join(self.tmp, 'file')
        open(blocker, 'w').close()
        with mock.patch.object(lock, 'lock_folder', os.path.join(blocker, 'locks')):
            with self.assertLogs('utils.lock', 'ERROR'), self.assertRaises(SystemExit) as cm:
                lock.ensure_lock_folder()
        self.assertEqual(cm.exception.code, 1)

    def test_second_holder_waits_for_release(self):
        lock.ensure_lock_folder()
        first, second = lock.upload(), lock.upload()
        acquired = threading.Event()

        def contender():
            with second:
                acquired.set()

        first.acquire()
        self.assertTrue(first.is_locked())
        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        self.assertFalse(acquired.wait(0.2))
        first.release()
        self.assertTrue(acquired.wait(5))
        thread.join(5)
        self.assertFalse(first.is_locked())
        self.assertFalse(second.is_locked())

    def test_different_locks_do_not_contend(self):
        lock.ensure_lock_folder()
        with lock.upload():
            self.assertFalse(lock.sync().is_locked())
            self.assertFalse(lock.hidden().is_locked())
            self.assertTrue(lock.upload().is_locked())

    def test_release_is_idempotent(self):
        lock.ensure_lock_folder()
        held = lock.sync()
        held.release()   # never acquired
        with held as entered:
            self.assertIs(entered, held)
        held.release()
        self.assertFalse(lock.sync().is_locked())

    def test_released_when_block_raises(self):
        lock.ensure_lock_folder()
        with self.assertRaises(RuntimeError):
            with lock.hidden():
                raise RuntimeError
        self.assertFalse(lock.hidden().is_locked())


# ---------------------------------------------------------------------------------------------- logs

class LogSetupTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        root = logging.getLogger()
        saved = (list(root.handlers), root.level, logs._console_handler, logs._file_handler, logs._startup_buffer,
                 {name: logging.getLogger(name).level for name in logs.QUIET_LOGGERS},
                 logging._warnings_showwarning is not None)
        logs._console_handler = logs._file_handler = logs._startup_buffer = None
        root.handlers[:] = []
        self.stdout = io.StringIO()
        patcher = mock.patch.object(sys, 'stdout', self.stdout)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.restore, saved)
        self.logfile = os.path.join(self.tmp, 'cloudplow.log')
        self.log = logging.getLogger('tests.support')

    def restore(self, saved):
        handlers, level, console, file_handler, buffer, quiet, captured = saved
        root = logging.getLogger()
        for handler in root.handlers:
            if handler not in handlers:
                handler.close()
        if logs._file_handler is not None:
            logs._file_handler.close()
        root.handlers[:] = handlers
        root.setLevel(level)
        logs._console_handler, logs._file_handler, logs._startup_buffer = console, file_handler, buffer
        for name, quiet_level in quiet.items():
            logging.getLogger(name).setLevel(quiet_level)
        logging.captureWarnings(captured)

    def read_log(self):
        for handler in logging.getLogger().handlers:
            handler.flush()
        with open(self.logfile, encoding='utf-8') as fp:
            return fp.read()

    def test_startup_records_are_replayed_into_the_log_file(self):
        logs.setup_console()
        self.log.info('before the log file is known')
        self.log.debug('below the level, dropped')
        logs.setup_file(self.logfile)
        self.log.warning('after setup_file')
        content = self.read_log()
        self.assertIn('before the log file is known', content)
        self.assertIn('after setup_file', content)
        self.assertNotIn('dropped', content)
        self.assertLess(content.index('before the log file'), content.index('after setup_file'))
        self.assertIsNone(logs._startup_buffer)
        self.assertNotIn(logging.handlers.MemoryHandler, [type(h) for h in logging.getLogger().handlers])
        # both are also on the console, once each
        self.assertEqual(self.stdout.getvalue().count('before the log file is known'), 1)

    def test_setup_file_is_idempotent(self):
        logs.setup_console()
        logs.setup_file(self.logfile)
        handler = logs._file_handler
        logs.setup_file(os.path.join(self.tmp, 'other.log'))
        self.assertIs(logs._file_handler, handler)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'other.log')))
        self.log.error('once')
        self.assertEqual(self.read_log().count('once'), 1)

    def test_setup_file_without_console(self):
        logging.getLogger().setLevel(logging.INFO)
        logs.setup_file(self.logfile)
        self.log.info('direct')
        self.assertIn('direct', self.read_log())

    def test_levels(self):
        logs.setup_console('DEBUG')
        self.assertEqual(logging.getLogger().level, logging.DEBUG)
        logs.setup_console('ERROR')   # re-setup only changes the level
        self.assertEqual(logging.getLogger().level, logging.ERROR)
        self.assertEqual(sum(isinstance(h, logging.StreamHandler) and h.stream is self.stdout
                             for h in logging.getLogger().handlers), 1)
        for level, expected in (('WARN', logging.WARNING), ('INFO', logging.INFO), ('DEBUG', logging.DEBUG)):
            logs.set_level(level)
            self.assertEqual(logging.getLogger().level, expected)
        for name, quiet_level in logs.QUIET_LOGGERS.items():
            self.assertEqual(logging.getLogger(name).level, quiet_level)

    def test_level_filters_console_and_file(self):
        logs.setup_console()
        logs.setup_file(self.logfile)
        logs.set_level('WARN')
        self.log.info('quiet info')
        self.log.warning('loud warning')
        logging.getLogger('urllib3.connectionpool').info('third party chatter')
        self.assertNotIn('quiet info', self.read_log())
        self.assertIn('loud warning', self.read_log())
        self.assertNotIn('quiet info', self.stdout.getvalue())
        logs.set_level('DEBUG')
        logging.getLogger('urllib3.connectionpool').info('third party chatter')
        self.assertNotIn('third party chatter', self.read_log())

    def test_secrets_redacted_on_console_and_file(self):
        logs.setup_console()
        self.log.info('fetching http://user:hunter22@nzbget:6789/xmlrpc')
        logs.setup_file(self.logfile)
        self.log.info('GET /api?apikey=abcdef0123456789 failed')
        for output in (self.stdout.getvalue(), self.read_log()):
            self.assertNotIn('hunter22', output)
            self.assertNotIn('abcdef0123456789', output)
            self.assertIn('http://***:***@nzbget', output)

    def test_format_and_warnings_captured(self):
        logs.setup_console()
        logs.setup_file(self.logfile)
        with warnings.catch_warnings():
            warnings.simplefilter('always')
            warnings.warn('deprecated thing')
        self.log.info('formatted')
        line = [l for l in self.read_log().splitlines() if 'formatted' in l][0]
        self.assertRegex(line, r'^\d{4}-\d\d-\d\d [\d:,]+ - INFO     - tests.support\s+ - '
                               r'test_format_and_warnings_captured - formatted$')
        self.assertIn('deprecated thing', self.read_log())


# ---------------------------------------------------------------------------------------------- cache

class CacheTests(TempDirTestCase):
    def open(self):
        store = cache.Cache(os.path.join(self.tmp, 'cache.db'))
        self.addCleanup(lambda: [table.close() for table in store.caches.values()])
        return store

    def test_tables_persist_independently(self):
        store = self.open()
        self.assertEqual(set(store.caches), set(cache.TABLES))
        store.get_cache('uploader_bans')['google'] = 1700000000.5
        store.get_cache('download_clients')['state'] = {'alt_limits_enabled': True}
        self.assertNotIn('google', store.get_cache('syncer_bans'))

        reopened = self.open()
        self.assertEqual(reopened.get_cache('uploader_bans')['google'], 1700000000.5)
        self.assertEqual(reopened.get_cache('download_clients')['state'], {'alt_limits_enabled': True})
        del reopened.get_cache('uploader_bans')['google']
        self.assertNotIn('google', self.open().get_cache('uploader_bans'))

    def test_values_stored_as_json(self):
        store = self.open()
        store.get_cache('sa_bans')['sa1.json'] = {'until': 5}
        for table in store.caches.values():
            table.close()
        with sqlite3.connect(os.path.join(self.tmp, 'cache.db')) as conn:
            rows = conn.execute('SELECT key, value FROM sa_bans').fetchall()
        self.assertEqual(rows, [('sa1.json', '{"until": 5}')])

    def test_unknown_table(self):
        with self.assertRaises(KeyError):
            self.open().get_cache('missing')


# ---------------------------------------------------------------------------------------------- decorators

class TimedTests(unittest.TestCase):
    def test_logs_duration_and_returns_result(self):
        class Job:
            @decorators.timed
            def run(self, value, factor=2):
                """Doc kept."""
                return value * factor

        with mock.patch.object(decorators.timeit, 'default_timer', side_effect=[100.0, 3825.0]):
            with self.assertLogs('utils.decorators', 'INFO') as cm:
                self.assertEqual(Job().run(3, factor=4), 12)
        self.assertEqual(cm.output, ['INFO:utils.decorators:TimedTests.test_logs_duration_and_returns_result.'
                                     '<locals>.Job.run finished in 1 hours, 2 minutes and 5 seconds'])
        self.assertEqual((Job.run.__name__, Job.run.__doc__), ('run', 'Doc kept.'))

    def test_logs_even_when_raising(self):
        @decorators.timed
        def explode():
            raise ValueError('boom')

        with self.assertLogs('utils.decorators', 'INFO') as cm, self.assertRaises(ValueError):
            explode()
        self.assertIn('explode finished in', cm.output[0])


if __name__ == '__main__':
    unittest.main()
