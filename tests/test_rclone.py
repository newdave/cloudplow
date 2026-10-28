import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from utils import process
from utils.rclone import (RcloneMover, RcloneSyncer, RcloneThrottler, RcloneUploader, drive_upstreams, rc_args)

HAS_RCLONE = shutil.which('rclone') is not None


def write(file_path, content):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'w') as fp:
        fp.write(content)


def tree(root):
    """{relative path: content} of every file below root."""
    found = {}
    for folder, _, files in os.walk(root):
        for name in files:
            full = os.path.join(folder, name)
            with open(full) as fp:
                found[os.path.relpath(full, root)] = fp.read()
    return found


class LocalRemoteTestCase(unittest.TestCase):
    """Temp source/destination directories and an empty rclone config, so rclone never sees a real remote."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.src = os.path.join(tmp.name, 'local')
        self.dst = os.path.join(tmp.name, 'remote')
        os.makedirs(self.src)
        os.makedirs(self.dst)
        self.conf = os.path.join(tmp.name, 'rclone.conf')
        open(self.conf, 'w').close()
        write(os.path.join(self.src, 'Movies', 'A [1]', 'a.mkv'), 'aaaa')
        write(os.path.join(self.src, 'Movies', 'B', 'b.mkv'), 'bb')
        write(os.path.join(self.src, 'Movies', 'B', 'b.mkv.partial~'), 'p')
        write(os.path.join(self.src, 'downloads', 'c.mkv'), 'c')

    def uploader(self, dry_run=False, **config):
        remote = {'upload_folder': self.src, 'upload_remote': self.dst,
                  'rclone_excludes': ['**.partial~', 'downloads/**'],
                  'rclone_extras': {'--retries': 1, '--low-level-retries': 1}, **config}
        return RcloneUploader('local', remote, 'rclone', self.conf, dry_run=dry_run)

    def files_from(self, *paths):
        list_path = os.path.join(self.tmp, 'files-from.txt')
        write(list_path, ''.join(f'{item}\n' for item in paths))
        return list_path


class HelperTests(unittest.TestCase):
    def test_rc_args_none_and_https_www(self):
        self.assertEqual(rc_args(None), [])
        self.assertEqual(rc_args({}), [])
        self.assertEqual(rc_args({'enabled': True, 'rclone': {'url': 'https://www.example.com:5572/'}}),
                         ['--rc', '--rc-no-auth', '--rc-addr=example.com:5572'])

    def test_drive_upstreams_through_alias_and_union_with_paths(self):
        remotes = {'al': {'type': 'alias', 'remote': 'un:/sub/dir'},
                   'un': {'type': 'union', 'upstreams': 'gd1:/x gd1:/y'},
                   'gd1': {'type': 'drive'}}
        self.assertEqual(drive_upstreams(remotes, 'al'), ['gd1', 'gd1'])


class UploadArgumentTests(unittest.TestCase):
    CONFIG = {'upload_folder': '/src', 'upload_remote': 'gd:/Media',
              'rclone_excludes': ['**.partial~'],
              'rclone_extras': {'--transfers': 8, '--min-age': '15m', '--exclude-if-present': '.ignore',
                                '--include': '*.mkv', '--max-size': '10G', '--filter': '- x', '--skip-links': None,
                                '--max-depth': 3, '--drive-chunk-size': '64M'}}

    def run_upload(self, config=None, returncode=0, **kwargs):
        rclone = RcloneUploader('gd', config or self.CONFIG, '/usr/bin/rclone', '/rc.conf', **{
            key: kwargs.pop(key) for key in ('plex', 'dry_run', 'service_account') if key in kwargs})
        with mock.patch('utils.process.stream', return_value=process.Result(returncode, '')) as stream:
            result = rclone.upload(lambda line: False, **kwargs)
        return result, stream

    def test_full_scan_applies_every_filter_and_exclude(self):
        result, stream = self.run_upload()
        self.assertEqual(result, (True, 0))
        args = stream.call_args[0][0]
        self.assertEqual(args[:5], ['/usr/bin/rclone', 'move', '/src', 'gd:/Media', '--config=/rc.conf'])
        for expected in ('--min-age=15m', '--include=*.mkv', '--max-size=10G', '--exclude=**.partial~',
                         '--transfers=8', '--skip-links'):
            self.assertIn(expected, args)
        self.assertNotIn('--dry-run', args)
        self.assertFalse(any(arg.startswith('--files-from') for arg in args))

    def test_files_from_drops_filters_but_keeps_transfer_flags(self):
        _, stream = self.run_upload(files_from='/tmp/list.txt')
        args = stream.call_args[0][0]
        self.assertEqual(args[-2:], ['--files-from-raw=/tmp/list.txt', '--no-traverse'])
        self.assertIn('--transfers=8', args)
        self.assertIn('--skip-links', args)
        self.assertIn('--drive-chunk-size=64M', args)
        for flag in ('--min-age', '--include', '--max-size', '--filter', '--max-depth', '--exclude'):
            self.assertFalse([arg for arg in args if arg.startswith(flag + '=')], flag)
        # --exclude-if-present also starts with '--exclude' and is classed as a filter flag
        self.assertNotIn('--exclude-if-present=.ignore', args)
        self.assertEqual(self.CONFIG['rclone_extras']['--min-age'], '15m')   # config not mutated

    def test_command_override_dry_run_and_rc(self):
        plex = {'enabled': True, 'rclone': {'url': 'http://localhost:7949'}}
        _, stream = self.run_upload(command='copy', dry_run=True, plex=plex)
        args = stream.call_args[0][0]
        self.assertEqual(args[1], 'copy')
        self.assertEqual(args[-1], '--dry-run')
        self.assertIn('--rc-addr=localhost:7949', args)

    def test_sync_is_downgraded_in_any_case(self):
        _, stream = self.run_upload(command='SYNC')
        self.assertEqual(stream.call_args[0][0][1], 'move')

    def test_callback_forwarded_and_exit_code_returned(self):
        callback = mock.Mock()
        rclone = RcloneUploader('gd', self.CONFIG, 'rclone', '/rc.conf')
        with mock.patch('utils.process.stream', return_value=process.Result(7, '')) as stream:
            self.assertEqual(rclone.upload(callback), (True, 7))
        self.assertIs(stream.call_args[0][1], callback)
        self.assertIsNone(stream.call_args[0][2])   # no service account: inherit the environment

    def test_os_error_reports_not_run(self):
        rclone = RcloneUploader('gd', self.CONFIG, '/missing/rclone', '/rc.conf')
        with mock.patch('utils.process.stream', side_effect=FileNotFoundError), \
                self.assertLogs('utils.rclone', 'ERROR'):
            self.assertEqual(rclone.upload(lambda line: False), (False, 9999))


class ServiceAccountTests(unittest.TestCase):
    DUMP = json.dumps({'crypt': {'type': 'crypt', 'remote': 'un:'},
                       'un': {'type': 'union', 'upstreams': 'gd1: gd2:/x gd1:/y'},
                       'gd1': {'type': 'drive'}, 'gd2': {'type': 'drive'}})

    def make(self, remote='crypt:/Media'):
        return RcloneUploader('gd', {'upload_folder': '/src', 'upload_remote': remote}, 'rclone', '/rc.conf',
                              service_account='/sa/sa1.json')

    def test_service_account_set_for_every_drive_upstream(self):
        with mock.patch('utils.process.check_output', return_value=self.DUMP) as dump, \
                mock.patch('utils.process.stream', return_value=process.Result(0, '')) as stream:
            self.assertEqual(self.make().upload(lambda line: False), (True, 0))
        self.assertEqual(dump.call_args[0][0], ['rclone', 'config', 'dump', '--config=/rc.conf'])
        env = stream.call_args[0][2]
        self.assertEqual(env['RCLONE_CONFIG_GD1_SERVICE_ACCOUNT_FILE'], '/sa/sa1.json')
        self.assertEqual(env['RCLONE_CONFIG_GD2_SERVICE_ACCOUNT_FILE'], '/sa/sa1.json')
        self.assertEqual(env.get('PATH'), os.environ.get('PATH'))   # rest of the environment kept

    def test_unreadable_config_aborts_upload(self):
        with mock.patch('utils.process.check_output', return_value=None), \
                mock.patch('utils.process.stream') as stream, self.assertLogs('utils.rclone', 'ERROR'):
            self.assertEqual(self.make().upload(lambda line: False), (False, 9999))
        stream.assert_not_called()

    def test_unknown_remote_aborts_upload(self):
        with mock.patch('utils.process.check_output', return_value=self.DUMP), \
                mock.patch('utils.process.stream') as stream, self.assertLogs('utils.rclone', 'ERROR'):
            self.assertEqual(self.make('nope:').upload(lambda line: False), (False, 9999))
        stream.assert_not_called()

    def test_non_drive_remote_runs_without_sa_vars(self):
        dump = json.dumps({'web': {'type': 'webdav'}})
        with mock.patch('utils.process.check_output', return_value=dump), \
                mock.patch('utils.process.stream', return_value=process.Result(0, '')) as stream, \
                self.assertLogs('utils.rclone', 'WARNING'):
            self.make('web:').upload(lambda line: False)
        env = stream.call_args[0][2]
        self.assertFalse([key for key in env if key.endswith('_SERVICE_ACCOUNT_FILE') and key.startswith('RCLONE_CONFIG_WEB')])


@unittest.skipUnless(HAS_RCLONE, 'rclone is not installed')
class RealUploadTests(LocalRemoteTestCase):
    def test_move_honours_excludes(self):
        ran, code = self.uploader().upload(lambda line: False)
        self.assertEqual((ran, code), (True, 0))
        self.assertEqual(tree(self.dst), {os.path.join('Movies', 'A [1]', 'a.mkv'): 'aaaa',
                                          os.path.join('Movies', 'B', 'b.mkv'): 'bb'})
        self.assertEqual(set(tree(self.src)), {os.path.join('Movies', 'B', 'b.mkv.partial~'),
                                               os.path.join('downloads', 'c.mkv')})

    def test_copy_keeps_source(self):
        self.uploader().upload(lambda line: False, command='copy')
        self.assertEqual(len(tree(self.dst)), 2)
        self.assertEqual(len(tree(self.src)), 4)

    def test_dry_run_changes_nothing(self):
        before = tree(self.src)
        self.assertEqual(self.uploader(dry_run=True).upload(lambda line: False), (True, 0))
        self.assertEqual(tree(self.src), before)
        self.assertEqual(tree(self.dst), {})

    def test_files_from_moves_only_listed_files_even_with_filter_extras(self):
        # --min-age would make rclone refuse --files-from-raw; it must be dropped, and the list wins
        rclone = self.uploader(rclone_extras={'--min-age': '1h', '--retries': 1})
        list_path = self.files_from(os.path.join('Movies', 'A [1]', 'a.mkv'))
        self.assertEqual(rclone.upload(lambda line: False, files_from=list_path), (True, 0))
        self.assertEqual(tree(self.dst), {os.path.join('Movies', 'A [1]', 'a.mkv'): 'aaaa'})
        self.assertNotIn(os.path.join('Movies', 'A [1]', 'a.mkv'), tree(self.src))
        self.assertIn(os.path.join('Movies', 'B', 'b.mkv'), tree(self.src))

    def test_failure_exit_code_is_returned(self):
        rclone = RcloneUploader('local', {'upload_folder': os.path.join(self.tmp, 'missing'),
                                          'upload_remote': self.dst,
                                          'rclone_extras': {'--retries': 1}}, 'rclone', self.conf)
        ran, code = rclone.upload(lambda line: False)
        self.assertTrue(ran)
        self.assertNotEqual(code, 0)


@unittest.skipUnless(HAS_RCLONE, 'rclone is not installed')
class RealListingTests(LocalRemoteTestCase):
    def test_list_source_applies_excludes_and_reports_sizes(self):
        self.assertEqual(self.uploader().list_source(), {'Movies/A [1]/a.mkv': 4, 'Movies/B/b.mkv': 2})

    def test_list_source_applies_filter_extras(self):
        rclone = self.uploader(rclone_extras={'--min-size': '3B'})
        self.assertEqual(rclone.list_source(), {'Movies/A [1]/a.mkv': 4})

    def test_list_destination_only_listed_existing_paths(self):
        write(os.path.join(self.dst, 'Movies', 'A [1]', 'a.mkv'), 'aaa')
        write(os.path.join(self.dst, 'Movies', 'other.mkv'), 'unlisted')
        list_path = self.files_from('Movies/A [1]/a.mkv', 'Movies/B/b.mkv')
        self.assertEqual(self.uploader().list_destination(list_path), {'Movies/A [1]/a.mkv': 3})

    def test_empty_destination(self):
        self.assertEqual(self.uploader().list_destination(self.files_from('x.mkv')), {})

    def test_listing_failure_is_none(self):
        rclone = self.uploader(upload_folder=os.path.join(self.tmp, 'missing'))
        with self.assertLogs('utils.process', 'ERROR'):
            self.assertIsNone(rclone.list_source())

    def test_delete_destination_file_removes_only_that_file(self):
        write(os.path.join(self.dst, 'Movies', 'B', 'b.mkv'), 'bb')
        write(os.path.join(self.dst, 'Movies', 'B', 'b.mkv.srt'), 'sub')
        self.assertTrue(self.uploader().delete_destination_file('Movies/B/b.mkv'))
        self.assertEqual(tree(self.dst), {os.path.join('Movies', 'B', 'b.mkv.srt'): 'sub'})

    def test_delete_destination_file_refuses_directories(self):
        write(os.path.join(self.dst, 'Movies', 'B', 'b.mkv'), 'bb')
        with self.assertLogs('utils.rclone', 'ERROR'):
            self.assertFalse(self.uploader().delete_destination_file('Movies/B'))
        self.assertIn(os.path.join('Movies', 'B', 'b.mkv'), tree(self.dst))

    def test_delete_destination_file_dry_run(self):
        write(os.path.join(self.dst, 'a.mkv'), 'a')
        with mock.patch('utils.process.run') as run:
            self.assertTrue(self.uploader(dry_run=True).delete_destination_file('a.mkv'))
        run.assert_not_called()
        self.assertEqual(tree(self.dst), {'a.mkv': 'a'})

    def test_delete_file_and_folder(self):
        write(os.path.join(self.dst, 'D', 'x.mkv'), 'x')
        os.makedirs(os.path.join(self.dst, 'E'))
        rclone = self.uploader()
        self.assertFalse(rclone.delete_folder(os.path.join(self.dst, 'D')))   # rmdir never removes content
        self.assertTrue(rclone.delete_file(os.path.join(self.dst, 'D', 'x.mkv')))
        self.assertTrue(rclone.delete_folder(os.path.join(self.dst, 'D')))
        self.assertTrue(rclone.delete_folder(os.path.join(self.dst, 'E')))
        self.assertEqual(os.listdir(self.dst), [])

    def test_delete_file_dry_run_keeps_file(self):
        write(os.path.join(self.dst, 'x.mkv'), 'x')
        self.assertTrue(self.uploader(dry_run=True).delete_file(os.path.join(self.dst, 'x.mkv')))
        self.assertEqual(tree(self.dst), {'x.mkv': 'x'})


class MockedListingTests(unittest.TestCase):
    def make(self, dry_run=False):
        return RcloneUploader('gd', {'upload_folder': '/src', 'upload_remote': 'gd:',
                                     'rclone_extras': {'--min-age': '1m'}, 'rclone_excludes': ['*.tmp']},
                              'rclone', '/rc.conf', dry_run=dry_run)

    def test_list_arguments(self):
        with mock.patch('utils.process.check_output', return_value='') as out:
            self.assertEqual(self.make().list_source(), {})
        self.assertEqual(out.call_args[0][0], ['rclone', 'lsjson', '-R', '--files-only', '--no-mimetype',
                                               '--no-modtime', '/src', '--config=/rc.conf', '--min-age=1m',
                                               '--exclude=*.tmp'])
        with mock.patch('utils.process.check_output', return_value='[]') as out:
            self.assertEqual(self.make().list_destination('/l.txt'), {})
        self.assertEqual(out.call_args[0][0][-2:], ['--config=/rc.conf', '--files-from-raw=/l.txt'])
        self.assertIn('gd:', out.call_args[0][0])

    def test_unparseable_listing_raises(self):
        # callers must not mistake garbage output for "nothing to upload"/"nothing at destination"
        with mock.patch('utils.process.check_output', return_value='not json'):
            with self.assertRaises(ValueError):
                self.make().list_source()

    def test_delete_destination_file_target_and_failure(self):
        with mock.patch('utils.process.run', return_value=process.Result(4, 'object not found')) as run, \
                self.assertLogs('utils.rclone', 'ERROR'):
            self.assertFalse(self.make().delete_destination_file('a/b.mkv'))
        self.assertEqual(run.call_args[0][0], ['rclone', 'deletefile', 'gd:a/b.mkv', '--config=/rc.conf'])

    def test_delete_reports_failure_text_and_exceptions(self):
        rclone = self.make()
        with mock.patch('utils.process.run', return_value=process.Result(0, 'NOTICE: Failed to rmdir: x')):
            self.assertFalse(rclone.delete_folder('gd:/x'))
        with mock.patch('utils.process.run', return_value=process.Result(3, '')):
            self.assertFalse(rclone.delete_file('gd:/x'))
        with mock.patch('utils.process.run', side_effect=OSError), self.assertLogs('utils.rclone', 'ERROR'):
            self.assertFalse(rclone.delete_file('gd:/x'))
        with mock.patch('utils.process.run', return_value=process.Result(0, '')) as run:
            self.assertTrue(self.make(dry_run=True).delete_file('gd:/x'))
        self.assertEqual(run.call_args[0][0], ['rclone', 'delete', 'gd:/x', '--config=/rc.conf', '--dry-run'])


class MoverTests(unittest.TestCase):
    CONFIG = {'move_from_remote': 'a:/x', 'move_to_remote': 'b:/y', 'rclone_extras': {'--transfers': 2},
              'rclone_excludes': ['*.tmp']}

    def test_arguments(self):
        plex = {'enabled': True, 'rclone': {'url': 'http://localhost:7949'}}
        with mock.patch('utils.process.stream', return_value=process.Result(0, '')) as stream:
            self.assertTrue(RcloneMover(self.CONFIG, 'rclone', '/rc.conf', plex, dry_run=True).move())
        self.assertEqual(stream.call_args[0][0],
                         ['rclone', 'move', 'a:/x', 'b:/y', '--config=/rc.conf', '--transfers=2', '--exclude=*.tmp',
                          '--rc', '--rc-no-auth', '--rc-addr=localhost:7949', '--dry-run'])

    def test_failures(self):
        mover = RcloneMover(self.CONFIG, 'rclone', '/rc.conf', None)
        with mock.patch('utils.process.stream', return_value=process.Result(1, '')):
            self.assertFalse(mover.move())
        with mock.patch('utils.process.stream', side_effect=OSError), self.assertLogs('utils.rclone', 'ERROR'):
            self.assertFalse(mover.move())

    @unittest.skipUnless(HAS_RCLONE, 'rclone is not installed')
    def test_real_move_between_local_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, conf = os.path.join(tmp, 's'), os.path.join(tmp, 'd'), os.path.join(tmp, 'c.conf')
            open(conf, 'w').close()
            write(os.path.join(src, 'keep.tmp'), 't')
            write(os.path.join(src, 'sub', 'go.mkv'), 'g')
            config = {'move_from_remote': src, 'move_to_remote': dst, 'rclone_excludes': ['*.tmp']}
            self.assertTrue(RcloneMover(config, 'rclone', conf, None).move())
            self.assertEqual(tree(dst), {os.path.join('sub', 'go.mkv'): 'g'})
            self.assertEqual(tree(src), {'keep.tmp': 't'})


class SyncerTests(unittest.TestCase):
    FROM = {'sync_remote': 'a:/', 'rclone_sleeps': {'rate limit': {'count': 2, 'timeout': 60, 'sleep': 5}}}
    TO = {'sync_remote': 'b:/', 'rclone_sleeps': {'quota': {'count': 3, 'timeout': 60, 'sleep': 25}}}

    def test_arguments_go_through_wrapper(self):
        syncer = RcloneSyncer(self.FROM, self.TO, rclone_extras={'--fast-list': None}, dry_run=True)
        wrapper = mock.Mock(side_effect=lambda args: ['docker', 'exec', *args])
        with mock.patch('utils.process.stream', return_value=process.Result(0, '')) as stream:
            self.assertEqual(syncer.sync(wrapper), (True, 0, None))
        wrapper.assert_called_once_with(['rclone', 'sync', 'a:/', 'b:/', '--fast-list', '--dry-run'])
        self.assertEqual(stream.call_args[0][0][:3], ['docker', 'exec', 'rclone'])

    def test_use_copy(self):
        syncer = RcloneSyncer(self.FROM, self.TO, use_copy=True)
        with mock.patch('utils.process.stream', return_value=process.Result(0, '')):
            wrapper = mock.Mock(side_effect=lambda args: args)
            syncer.sync(wrapper)
        self.assertEqual(wrapper.call_args[0][0], ['rclone', 'copy', 'a:/', 'b:/'])

    def test_triggers_from_both_remotes_are_tracked_and_suspend(self):
        self.assertEqual(set(RcloneSyncer(self.FROM, self.TO).triggers.triggers), {'rate limit', 'quota'})

        seen = []

        def fake_stream(args, callback, logger=None):
            for line in ['ok', 'ERROR: quota exceeded', 'ERROR: User rate limit exceeded', 'Rate limit again',
                         'never reached']:
                seen.append(line)
                if callback(line):
                    break
            return process.Result(-9, '')

        with mock.patch('utils.process.stream', side_effect=fake_stream), self.assertLogs('utils.triggers', 'WARNING'):
            self.assertEqual(RcloneSyncer(self.FROM, self.TO).sync(lambda args: args), (False, 5, 'rate limit'))
        self.assertNotIn('never reached', seen)


class FakeRcloneRc:
    """rclone's remote-control API (rc/noop, core/bwlimit, core/stats) over real HTTP on 127.0.0.1."""

    def __init__(self, rate='off', transferring=None):
        self.rate = rate
        self.transferring = transferring
        self.requests = []
        self.override = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(length) or b'{}')
                endpoint = self.path.lstrip('/')
                fake.requests.append((endpoint, payload))
                body = fake.override.get(endpoint)
                if body is None:
                    body = json.dumps(fake.respond(endpoint, payload))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body.encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/'
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, endpoint, payload):
        if endpoint == 'rc/noop':
            return payload
        if endpoint == 'core/bwlimit':
            if payload['rate'] == 'bogus':
                return {'error': 'bad bwlimit', 'status': 500}
            self.rate = payload['rate']
            if self.rate == 'off':
                return {'bytesPerSecond': -1, 'rate': 'off'}
            # rclone echoes the rate normalised, e.g. '10M' -> '10Mi'
            return {'bytesPerSecond': 10 * 1024 * 1024, 'rate': '10Mi'}
        if endpoint == 'core/stats':
            stats = {'bytes': 0, 'speed': 0}
            if self.transferring is not None:
                stats['transferring'] = self.transferring
            return stats
        return {'error': 'couldn\'t find method', 'status': 404}


class ThrottlerTests(unittest.TestCase):
    def make(self, **kwargs):
        fake = FakeRcloneRc(**kwargs)
        self.addCleanup(fake.close)
        return fake, RcloneThrottler(fake.url)

    def test_validate(self):
        fake, throttler = self.make()
        self.assertTrue(throttler.validate())
        self.assertEqual(fake.requests, [('rc/noop', {'validated': True})])
        fake.override['rc/noop'] = '{}'
        self.assertFalse(throttler.validate())

    def test_throttle_and_unthrottle(self):
        fake, throttler = self.make()
        with self.assertLogs('utils.rclone', 'WARNING'):
            self.assertTrue(throttler.throttle('10M'))
        self.assertEqual(fake.rate, '10M')
        self.assertEqual(fake.requests[-1], ('core/bwlimit', {'rate': '10M'}))
        with self.assertLogs('utils.rclone', 'WARNING'):
            self.assertTrue(throttler.no_throttle())
        self.assertEqual(fake.rate, 'off')

    def test_throttle_confirmed_by_bytes_per_second(self):
        fake, throttler = self.make()
        fake.override['core/bwlimit'] = json.dumps({'bytesPerSecond': 10485760, 'rate': '10.000Mi'})
        with self.assertLogs('utils.rclone', 'WARNING'):
            self.assertTrue(throttler.throttle('10240'))   # bare number = KiB/s

    def test_throttle_failures(self):
        fake, throttler = self.make()
        with self.assertLogs('utils.rclone', 'ERROR'):
            self.assertFalse(throttler.throttle('bogus'))              # rc error
        fake.override['core/bwlimit'] = json.dumps({'bytesPerSecond': 1, 'rate': '1'})
        with self.assertLogs('utils.rclone', 'ERROR'):
            self.assertFalse(throttler.throttle('10M'))                # rclone applied something else
            self.assertFalse(throttler.no_throttle())

    def test_non_json_response(self):
        fake, throttler = self.make()
        fake.override['rc/noop'] = '<html>nope</html>'
        with self.assertLogs('utils.rclone', 'ERROR'):
            self.assertFalse(throttler.validate())

    def test_unreachable_rclone(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        throttler = RcloneThrottler(f'http://127.0.0.1:{port}/')
        # rclone not running is expected between uploads: a debug line, no traceback
        with self.assertLogs('utils.rclone', 'DEBUG') as logs:
            self.assertFalse(throttler.validate())
            self.assertFalse(throttler.throttle_active('10M'))
        self.assertTrue(all(record.levelname == 'DEBUG' and not record.exc_info for record in logs.records))
        with self.assertLogs('utils.rclone', 'ERROR'):
            self.assertFalse(throttler.no_throttle())          # a failed un-throttle is still an error

    def test_throttle_active(self):
        # "active" means rclone is honouring the limit: total speed no more than 10 MB/s above it
        mib = 1024 * 1024
        fake, throttler = self.make(transferring=[{'speed': 12 * mib}, {'speed': '3000000.5'}])   # ~15.6 MB/s
        self.assertFalse(throttler.throttle_active(None))
        self.assertEqual(fake.requests, [])                    # no speed configured: no API call
        self.assertTrue(throttler.throttle_active('10M'))      # 10.49 MB/s limit + 10 MB/s slack
        self.assertEqual(fake.requests[-1][0], 'core/stats')
        self.assertTrue(throttler.throttle_active('100M'))
        self.assertFalse(throttler.throttle_active('5M'))      # 5.24 MB/s + 10 < 15.6: limit not applied
        self.assertFalse(throttler.throttle_active('off'))     # not a comparable rate

    def test_throttle_active_without_transfers(self):
        _, throttler = self.make()
        self.assertFalse(throttler.throttle_active('10M'))     # key absent
        _, throttler = self.make(transferring=[])
        self.assertFalse(throttler.throttle_active('10M'))


if __name__ == '__main__':
    unittest.main()
