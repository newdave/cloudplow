"""Behaviour tests for the Cloudplow orchestrator in cloudplow.py."""
import os
import tempfile
import unittest
from unittest import mock

import cloudplow as cloudplow_module
from cloudplow import SECONDS_PER_HOUR, Cloudplow
from utils.ratelimit import RateArbiter
from utils.uploader import UploadResult

NOW = 1_000_000.0


class StopLoop(Exception):
    """Raised from a patched time.sleep to break out of an infinite loop."""


def fake_lock(locked_polls=0):
    """A lock whose is_locked() is True `locked_polls` times, then False; usable as a context manager."""
    lock_file = mock.MagicMock()
    lock_file.is_locked.side_effect = [True] * locked_polls + [False] * 100
    return lock_file


def make_plow(**configs):
    plow = object.__new__(Cloudplow)
    plow.configs = {'remotes': {}, 'uploader': {}, 'syncer': {}, 'hidden': {}, 'plex': {'enabled': False},
                    'nzbget': {'enabled': False}, 'sabnzbd': {'enabled': False}}
    plow.configs.update(configs)
    plow.core = {'rclone_binary_path': '/usr/bin/rclone', 'rclone_config_path': '/rclone.conf'}
    plow.dry_run = False
    plow.config_dir = tempfile.gettempdir()
    plow.notify = mock.Mock()
    plow.uploader_bans = {}
    plow.syncer_bans = {}
    plow.sa_bans = {}
    plow.download_client_state = {}
    plow.rates = RateArbiter()
    return plow


def sent(plow):
    return [c.args[0] for c in plow.notify.send.call_args_list]


class ConstructionTests(unittest.TestCase):
    def test_init_opens_caches_and_lock_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = mock.Mock()
            conf.configs = {'core': {'dry_run': True}}
            conf.settings = {'config': os.path.join(tmp, 'config.json'), 'cachefile': os.path.join(tmp, 'c.db')}
            with mock.patch.object(cloudplow_module, 'Syncer'), \
                    mock.patch.object(cloudplow_module.lock, 'ensure_lock_folder') as ensure:
                plow = Cloudplow(conf)
            ensure.assert_called_once_with()
            self.assertTrue(plow.dry_run)
            self.assertEqual(plow.config_dir, tmp)
            plow.uploader_bans['gd'] = 5.0
            self.assertEqual(plow.uploader_bans['gd'], 5.0)
            for cache in (plow.uploader_bans, plow.syncer_bans, plow.sa_bans, plow.download_client_state):
                cache.close()

    def test_init_notifications_logs_failed_agents(self):
        plow = make_plow(notifications={'good': {'service': 'a'}, 'bad': {'service': 'b'}})
        plow.notify.load.side_effect = lambda **kw: kw['service'] == 'a'
        with self.assertLogs('cloudplow', 'ERROR') as logs:
            plow.init_notifications()
        self.assertEqual(plow.notify.load.call_count, 2)
        self.assertIn('bad', logs.output[0])

    def test_init_syncers_strips_sync_interval(self):
        plow = make_plow(syncer={'s1': {'service': 'local', 'sync_interval': 24, 'sync_from': 'a'}})
        plow.syncer = mock.Mock()
        plow.init_syncers()
        plow.syncer.load.assert_called_once_with(syncer_name='s1', service='local', sync_from='a')


class ServiceAccountInitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sa_dir = self.tmp.name
        for name in ('sa1.json', 'sa2.json', 'notes.txt'):
            open(os.path.join(self.sa_dir, name), 'w').close()
        self.plow = make_plow(uploader={'gd': {'service_account_path': self.sa_dir}, 'plain': {}})

    def tearDown(self):
        self.tmp.cleanup()

    def acc(self, name):
        return os.path.join(self.sa_dir, name)

    def test_first_run_registers_json_accounts_unbanned(self):
        self.plow.init_service_accounts()
        self.assertEqual(self.plow.sa_bans['gd'], {self.acc('sa1.json'): None, self.acc('sa2.json'): None})
        self.assertIsNone(self.plow.sa_bans['plain'])

    def test_missing_path_leaves_remote_without_accounts(self):
        self.plow.configs['uploader']['gd']['service_account_path'] = os.path.join(self.sa_dir, 'nope')
        self.plow.init_service_accounts()
        self.assertIsNone(self.plow.sa_bans['gd'])

    def test_existing_bans_kept_deleted_dropped_and_new_lift_remote_ban(self):
        self.plow.sa_bans['gd'] = {self.acc('sa1.json'): NOW + 50, self.acc('gone.json'): NOW + 10}
        self.plow.uploader_bans['gd'] = NOW + 10
        self.plow.init_service_accounts()
        self.assertEqual(self.plow.sa_bans['gd'], {self.acc('sa1.json'): NOW + 50, self.acc('sa2.json'): None})
        self.assertNotIn('gd', self.plow.uploader_bans)

    def test_no_new_accounts_keeps_remote_ban(self):
        self.plow.sa_bans['gd'] = {self.acc('sa1.json'): NOW + 50, self.acc('sa2.json'): NOW + 60}
        self.plow.uploader_bans['gd'] = NOW + 50
        self.plow.init_service_accounts()
        self.assertEqual(self.plow.uploader_bans['gd'], NOW + 50)
        self.assertEqual(self.plow.sa_bans['gd'][self.acc('sa2.json')], NOW + 60)


@mock.patch('time.time', return_value=NOW)
class SuspensionTests(unittest.TestCase):
    def test_active_ban_suspends_only_that_name(self, _):
        plow = make_plow()
        plow.uploader_bans.update({'gd': NOW + 60, 'other': NOW + 60})
        self.assertTrue(plow.uploader_suspended('gd'))
        self.assertFalse(plow.uploader_suspended('free'))
        self.assertEqual(set(plow.uploader_bans), {'gd', 'other'})
        plow.notify.send.assert_not_called()

    def test_expired_ban_is_removed_and_notified(self, _):
        plow = make_plow()
        plow.uploader_bans.update({'gd': NOW - 1, 'other': NOW + 60})
        self.assertFalse(plow.uploader_suspended('gd'))
        self.assertEqual(plow.uploader_bans, {'other': NOW + 60})
        self.assertEqual(sent(plow), ['Upload suspension has expired for remote: gd'])

    def test_syncer_suspension_uses_syncer_bans(self, _):
        plow = make_plow()
        plow.syncer_bans['s1'] = NOW + 60
        plow.uploader_bans['s1'] = NOW - 1
        self.assertTrue(plow.syncer_suspended('s1'))
        self.assertIn('s1', plow.uploader_bans)
        plow.syncer_bans['s1'] = NOW - 1
        self.assertFalse(plow.syncer_suspended('s1'))
        self.assertEqual(sent(plow), ['Sync suspension has expired for syncer: s1'])

    def test_release_expired_service_accounts_persists_by_assignment(self, _):
        plow = make_plow()
        original = {'a': NOW - 1, 'b': NOW + 60, 'c': None}
        plow.sa_bans['gd'] = original
        plow.release_expired_service_accounts('gd')
        self.assertEqual(plow.sa_bans['gd'], {'a': None, 'b': NOW + 60, 'c': None})
        # a new dict was assigned (sqlitedict only persists on assignment)
        self.assertIsNot(plow.sa_bans['gd'], original)
        self.assertEqual(original['a'], NOW - 1)

    def test_release_without_accounts_is_noop(self, _):
        plow = make_plow()
        plow.sa_bans['gd'] = None
        plow.release_expired_service_accounts('gd')
        plow.release_expired_service_accounts('missing')
        self.assertEqual(plow.sa_bans, {'gd': None})


@mock.patch('time.time', return_value=NOW)
class ServiceAccountUploadTests(unittest.TestCase):
    def setUp(self):
        self.plow = make_plow()
        self.uploader = mock.Mock()

    def test_no_unbanned_accounts_suspends_until_first_unban(self, _):
        self.plow.sa_bans['gd'] = {'sa1': NOW + 300, 'sa2': NOW + 100}
        result = self.plow._upload_with_service_accounts('gd', self.uploader)
        self.assertFalse(result.success)
        self.assertEqual(self.plow.uploader_bans['gd'], NOW + 100)
        self.uploader.upload.assert_not_called()

    def test_success_uses_first_available_in_natural_order(self, _):
        self.plow.sa_bans['gd'] = {'sa10': None, 'sa2': None, 'sa1': NOW + 50}
        self.uploader.upload.return_value = UploadResult(0, '', True)
        result = self.plow._upload_with_service_accounts('gd', self.uploader)
        self.assertTrue(result.success)
        self.uploader.set_service_account.assert_called_once_with('sa2')
        self.assertEqual(sent(self.plow), ['Upload was completed successfully for remote: gd'])
        self.assertNotIn('gd', self.plow.uploader_bans)

    def test_cycles_to_next_account_after_trigger(self, _):
        self.plow.sa_bans['gd'] = {'sa1': None, 'sa2': None}
        self.uploader.upload.side_effect = [UploadResult(25, '750GB', False), UploadResult(0, '', False)]
        result = self.plow._upload_with_service_accounts('gd', self.uploader)
        self.assertEqual(result, UploadResult(0, '', False))
        self.assertEqual([c.args[0] for c in self.uploader.set_service_account.call_args_list], ['sa1', 'sa2'])
        self.assertEqual(self.plow.sa_bans['gd'], {'sa1': NOW + 25 * SECONDS_PER_HOUR, 'sa2': None})
        self.assertEqual(sent(self.plow), ['Upload was not completed successfully for remote: gd'])
        self.assertNotIn('gd', self.plow.uploader_bans)

    def test_all_accounts_exhausted_suspends_remote(self, _):
        self.plow.sa_bans['gd'] = {'sa1': None, 'sa2': None}
        self.uploader.upload.side_effect = [UploadResult(25, '750GB', False), UploadResult(24, '750GB', False)]
        result = self.plow._upload_with_service_accounts('gd', self.uploader)
        self.assertEqual(result.delay_hours, 24)
        self.assertEqual(self.plow.sa_bans['gd'], {'sa1': NOW + 25 * SECONDS_PER_HOUR,
                                                   'sa2': NOW + 24 * SECONDS_PER_HOUR})
        self.assertEqual(self.plow.uploader_bans['gd'], NOW + 24 * SECONDS_PER_HOUR)
        self.assertEqual(sent(self.plow), ['Upload was aborted for remote: gd due to trigger 750GB. '
                                           'Uploads suspended for 24 hours'])

    def test_exhausting_available_accounts_while_one_was_released_does_not_suspend(self, _):
        # sa0 was banned at the start, but its ban expires by the time the others are exhausted
        self.plow.sa_bans['gd'] = {'sa0': NOW + 10, 'sa1': None}

        clock = {'now': NOW}

        def upload():
            clock['now'] = NOW + 20  # the upload takes long enough for sa0's ban to expire
            return UploadResult(25, '750GB', False)

        self.uploader.upload.side_effect = upload
        with mock.patch('time.time', side_effect=lambda: clock['now']):
            self.plow._upload_with_service_accounts('gd', self.uploader)
        self.assertIsNone(self.plow.sa_bans['gd']['sa0'])
        self.assertNotIn('gd', self.plow.uploader_bans)
        self.plow.notify.send.assert_not_called()


@mock.patch('time.time', return_value=NOW)
class HandleUploadResultTests(unittest.TestCase):
    def test_success_lifts_existing_ban(self, _):
        plow = make_plow()
        plow.uploader_bans['gd'] = NOW - 5
        plow._handle_upload_result('gd', UploadResult(0, '', True))
        self.assertNotIn('gd', plow.uploader_bans)
        self.assertEqual(sent(plow), ['Upload was completed successfully for remote: gd'])

    def test_failure_without_delay_notifies_failure(self, _):
        plow = make_plow()
        plow._handle_upload_result('gd', UploadResult(0, '', False))
        self.assertEqual(sent(plow), ['Upload was not completed successfully for remote: gd'])
        self.assertEqual(plow.uploader_bans, {})

    def test_delay_suspends_remote(self, _):
        plow = make_plow()
        plow._handle_upload_result('gd', UploadResult(25, '750GB', False))
        self.assertEqual(plow.uploader_bans['gd'], NOW + 25 * SECONDS_PER_HOUR)
        self.assertEqual(sent(plow), ['Upload was aborted for remote: gd due to trigger 750GB. '
                                      'Uploads suspended for 25 hours'])

    def test_delay_while_already_banned_keeps_existing_expiry(self, _):
        plow = make_plow()
        plow.uploader_bans['gd'] = NOW + 7
        plow._handle_upload_result('gd', UploadResult(25, '750GB', False))
        self.assertEqual(plow.uploader_bans['gd'], NOW + 7)
        self.assertEqual(sent(plow), ['Upload was aborted for remote: gd due to trigger 750GB.'])


class MoverTests(unittest.TestCase):
    CONFIG = {'enabled': True, 'move_from_remote': 'a:', 'move_to_remote': 'b:', 'rclone_extras': {}}

    def test_missing_setting_skips_move(self):
        plow = make_plow()
        with mock.patch.object(cloudplow_module, 'RcloneMover') as mover, \
                self.assertLogs('cloudplow', 'ERROR') as logs:
            plow._move('gd', {'move_from_remote': 'a:', 'rclone_extras': {}})
        mover.assert_not_called()
        plow.notify.send.assert_not_called()
        self.assertIn("'move_to_remote'", logs.output[0])

    def test_success_and_failure_are_notified(self):
        for ok, final in ((True, 'Move finished successfully for a: -> b:'), (False, 'Move failed for a: -> b:')):
            plow = make_plow()
            with mock.patch.object(cloudplow_module, 'RcloneMover') as mover:
                mover.return_value.move.return_value = ok
                plow._move('gd', self.CONFIG)
            mover.assert_called_once_with(self.CONFIG, '/usr/bin/rclone', '/rclone.conf', plow.configs['plex'],
                                          False)
            self.assertEqual(sent(plow), ['Move has started for a: -> b:', final])


class UploadRemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plow = make_plow(remotes={'gd': {'upload_folder': '/x', 'rclone_extras': {'--bwlimit': '50M'}}})
        self.plow.config_dir = self.tmp.name
        self.plow._ensure_plex_monitor = mock.Mock()
        self.plow._move = mock.Mock()
        patcher = mock.patch.object(cloudplow_module, 'Uploader')
        self.uploader_cls = patcher.start()
        self.addCleanup(patcher.stop)
        self.uploader = self.uploader_cls.return_value
        self.uploader.upload.return_value = UploadResult(0, '', True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plain_upload_handles_result_and_cleans_up(self):
        result = self.plow._upload_remote('gd', {'mover': {'enabled': False}})
        self.assertTrue(result.success)
        self.assertEqual(self.plow.rates.load_rate, '50M')
        self.plow._ensure_plex_monitor.assert_called_once_with()
        self.uploader.remove_empty_dirs.assert_called_once_with()
        self.plow._move.assert_not_called()
        self.assertEqual(sent(self.plow), ['Upload has begun for remote: gd',
                                           'Upload was completed successfully for remote: gd'])

    def test_dry_run_unthrottleable_with_mover(self):
        self.plow.dry_run = True
        mover = {'enabled': True}
        self.plow._upload_remote('gd', {'can_be_throttled': False, 'mover': mover})
        self.plow._ensure_plex_monitor.assert_not_called()
        self.uploader.remove_empty_dirs.assert_not_called()
        self.plow._move.assert_called_once_with('gd', mover)

    def test_service_accounts_path_and_overrides(self):
        os.makedirs(os.path.join(self.tmp.name, 'overrides'), exist_ok=True)
        self.plow.sa_bans['gd'] = {'sa1': None}
        self.plow._upload_with_service_accounts = mock.Mock(return_value=UploadResult(0, '', True))
        with mock.patch.object(cloudplow_module.overrides, 'load', return_value={'--bwlimit': '10M'}):
            self.plow._upload_remote('gd', {})
        self.plow._upload_with_service_accounts.assert_called_once_with('gd', self.uploader)
        self.assertEqual(self.plow.rates.load_rate, '10M')
        remote_config = self.uploader_cls.call_args.args[2]
        self.assertEqual(remote_config['rclone_extras']['--bwlimit'], '10M')

    def test_download_clients_paused_and_resumed(self):
        self.plow.configs['nzbget'] = {'enabled': True, 'url': 'http://127.0.0.1:1'}
        self.plow.configs['sabnzbd'] = {'enabled': True, 'url': 'http://127.0.0.1:2', 'apikey': 'k'}
        with mock.patch.object(cloudplow_module, 'Nzbget') as nzb, mock.patch.object(cloudplow_module, 'Sabnzbd') as sab:
            nzb.return_value.pause_queue.return_value = True
            nzb.return_value.resume_queue.return_value = False
            sab.return_value.pause_queue.return_value = False
            self.plow._upload_remote('gd', {})
        nzb.return_value.resume_queue.assert_called_once_with()
        sab.return_value.resume_queue.assert_not_called()  # we never paused it, so we don't resume it
        messages = sent(self.plow)
        self.assertIn('Paused the Nzbget download queue, upload commencing!', messages)
        self.assertIn('Failed to pause the Sabnzbd download queue, upload commencing anyway...', messages)
        self.assertIn('Failed to resume the Nzbget download queue??', messages)


class UploadTests(unittest.TestCase):
    def test_upload_runs_selected_remote_and_post_script_on_exception(self):
        plow = make_plow(uploader={'gd': {'post_script': '/s.sh'}, 'other': {}})
        plow._upload_remote = mock.Mock(side_effect=RuntimeError('boom'))
        plow._run_post_script = mock.Mock()
        with mock.patch.object(cloudplow_module.lock, 'upload', return_value=fake_lock(1)) as upload_lock, \
                self.assertLogs('cloudplow', 'ERROR'):
            plow.upload('gd')
        plow._upload_remote.assert_called_once_with('gd', {'post_script': '/s.sh'})
        plow._run_post_script.assert_called_once_with('gd', '/s.sh', None)
        self.assertEqual(sent(plow), ['Exception occurred while uploading to remote: gd'])
        upload_lock.return_value.__enter__.assert_called_once()
        upload_lock.return_value.__exit__.assert_called_once()

    def test_upload_all_passes_result_to_post_script(self):
        plow = make_plow(uploader={'a': {}, 'b': {'post_script': ''}})
        result = UploadResult(0, '', True)
        plow._upload_remote = mock.Mock(return_value=result)
        plow._run_post_script = mock.Mock()
        with mock.patch.object(cloudplow_module.lock, 'upload', return_value=fake_lock()):
            plow.upload()
        self.assertEqual(plow._upload_remote.call_count, 2)
        plow._run_post_script.assert_any_call('b', '', result)


class EnsurePlexMonitorTests(unittest.TestCase):
    def test_starts_monitor_only_when_enabled_and_not_running(self):
        import threading
        plow = make_plow()
        plow._plex_thread = None
        plow._plex_thread_lock = threading.Lock()
        started = threading.Event()
        plow.plex_monitor = started.set
        plow._ensure_plex_monitor()
        self.assertIsNone(plow._plex_thread)
        plow.configs['plex']['enabled'] = True
        plow._ensure_plex_monitor()
        self.assertTrue(started.wait(2))
        first = plow._plex_thread
        first.join(2)
        plow._ensure_plex_monitor()  # previous thread finished, so a new one is started
        self.assertIsNot(plow._plex_thread, first)
        plow._plex_thread.join(2)


class Stream:
    def __init__(self, active=True, local=False):
        self.active, self.local = active, local

    def __str__(self):
        return 'stream'


class FakeRclone:
    def __init__(self, valid=True, active=True):
        self.valid = valid
        self.active = active
        self.calls = []

    def validate(self):
        return self.valid

    def throttle_active(self, speed):
        return self.active

    def throttle(self, speed):
        self.calls.append(speed)
        return True


class PlexMonitorTests(unittest.TestCase):
    def setUp(self):
        self.plow = make_plow(plex={
            'enabled': True, 'url': 'http://127.0.0.1:32400', 'token': 't', 'poll_interval': 30,
            'max_streams_before_throttle': 1, 'ignore_local_streams': True, 'notifications': True,
            'rclone': {'url': 'http://127.0.0.1:5572', 'throttle_speeds': {'1': '50M', '2': '40M', '3': '30M'}},
        })
        self.plex = mock.Mock()
        self.plex.validate.return_value = True
        self.rclone = FakeRclone()
        for target, value in (('Plex', mock.Mock(return_value=self.plex)),
                              ('RcloneThrottler', mock.Mock(return_value=self.rclone))):
            patcher = mock.patch.object(cloudplow_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = mock.patch('time.sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def run_monitor(self, polls):
        self.plex.get_streams.side_effect = polls
        with mock.patch.object(cloudplow_module.lock, 'upload', return_value=fake_lock(len(polls))):
            self.plow.plex_monitor()

    def test_invalid_plex_aborts_before_sleeping(self):
        self.plex.validate.return_value = False
        with self.assertLogs('cloudplow', 'ERROR'):
            self.plow.plex_monitor()
        self.sleep.assert_not_called()

    def test_invalid_rclone_aborts(self):
        self.rclone.valid = False
        with mock.patch.object(cloudplow_module.lock, 'upload') as upload_lock, \
                self.assertLogs('cloudplow', 'ERROR'):
            self.plow.plex_monitor()
        upload_lock.assert_not_called()

    def test_throttle_adjust_and_restore_load_limit(self):
        self.plow.rates.start_upload('45M')
        self.run_monitor([
            [Stream()],                              # 1 stream: throttle to 50M, but load limit 45M is lower
            [Stream(), Stream(), Stream()],          # 3 streams: adjust to 30M
            [Stream(), Stream(), Stream()],          # unchanged: keep throttling
            [Stream(local=True), Stream(active=False)],  # local + paused don't count: un-throttle
            [],
        ])
        self.assertEqual(self.rclone.calls, ['30M', '45M'])
        self.assertEqual(self.plow.rates.applied, '45M')
        self.assertIsNone(self.plow.rates.plex_rate)
        self.assertEqual(sent(self.plow), [
            'Throttled current upload to 50M because there was 1 playing stream(s) on Plex',
            'Throttle for current upload was adjusted to 30M due to 3 playing stream(s) on Plex Media Server',
            'Un-throttled current upload because there was less than 1 playing stream(s) on Plex Media Server',
        ])
        # grace period + one sleep per poll
        self.assertEqual(self.sleep.call_count, 1 + 5)

    def test_failed_stream_check_retries_and_exits_when_upload_lock_released(self):
        self.run_monitor([None, []])
        self.assertEqual(self.plex.get_streams.call_count, 2)
        self.assertEqual(self.rclone.calls, [])
        self.plow.notify.send.assert_not_called()

    def test_local_streams_count_when_not_ignored(self):
        self.plow.configs['plex']['ignore_local_streams'] = False
        self.plow.configs['plex']['notifications'] = False
        self.run_monitor([[Stream(local=True), Stream(local=True)]])
        self.assertEqual(self.rclone.calls, ['40M'])
        self.plow.notify.send.assert_not_called()

    # a fresh rclone (e.g. for the next service account) starts without the Plex limit: it must be sent again
    def test_rethrottles_when_rclone_is_no_longer_throttled(self):
        # poll 1 throttles; by poll 2 rclone runs unthrottled again (fresh rclone for the next account)
        self.rclone.throttle_active = lambda speed: False
        self.run_monitor([[Stream()], [Stream()]])
        self.assertEqual(self.rclone.calls, ['50M', '50M'])


class RateGovernorGuardTests(unittest.TestCase):
    def test_no_script_or_no_rc_runs_upload_without_governor(self):
        plow = make_plow()
        plow._govern_rate = mock.Mock()
        with plow._rate_governor('gd', {}):
            pass
        with self.assertLogs('cloudplow', 'WARNING'):
            with plow._rate_governor('gd', {'rate_script': '/s.sh'}):
                pass
        plow._govern_rate.assert_not_called()

    def test_governor_thread_runs_for_the_upload_and_is_stopped(self):
        plow = make_plow(plex={'enabled': True})
        seen = {}

        def govern(name, script, interval, stop):
            seen.update(name=name, script=script, interval=interval)
            stop.wait(2)
            seen['stopped'] = stop.is_set()

        plow._govern_rate = govern
        with plow._rate_governor('gd', {'rate_script': '/s.sh', 'rate_script_interval': 1}):
            pass
        self.assertEqual(seen, {'name': 'gd', 'script': '/s.sh', 'interval': 10, 'stopped': True})

    def test_post_script_skipped_when_unset_and_missing_script_fails(self):
        plow = make_plow()
        plow._run_script = mock.Mock()
        plow._run_post_script('gd', '', None)
        plow._run_script.assert_not_called()
        with self.assertLogs('cloudplow', 'ERROR'):
            self.assertFalse(Cloudplow._run_script('post_script', '/nonexistent/script.sh', {}))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.plow = make_plow(syncer={'remote': {'service': 'scaleway'}, 'local': {'service': 'local'}})
        self.plow.syncer = mock.Mock()
        self.agent = self.plow.syncer.get.return_value
        self.agent.instance_destroy = True
        self.agent.startup.return_value = (True, 'id-1')
        self.agent.setup.return_value = True
        self.agent.destroy.return_value = True

    def sync(self, name='remote'):
        self.plow._sync_one(name, self.plow.configs['syncer'][name])
        return sent(self.plow)

    def test_unknown_syncer_is_skipped(self):
        self.plow.syncer.get.return_value = None
        self.assertEqual(self.sync(), [])

    def test_successful_remote_sync_lifts_ban_and_destroys_instance(self):
        self.plow.syncer_bans['remote'] = NOW
        self.agent.sync.return_value = (True, 0, None)
        self.assertEqual(self.sync(), [
            'Sync initiated for syncer: remote. Creating scaleway instance...',
            'Sync has begun for syncer: remote',
            'Sync was completed successfully for syncer: remote',
            'Syncer: remote has destroyed its scaleway instance',
        ])
        self.assertNotIn('remote', self.plow.syncer_bans)

    def test_local_sync_sends_no_instance_messages(self):
        self.agent.instance_destroy = False
        self.agent.sync.return_value = (True, 0, None)
        self.agent.destroy.return_value = False
        self.assertEqual(self.sync('local'), ['Sync has begun for syncer: local',
                                              'Sync was completed successfully for syncer: local'])

    def test_startup_and_setup_failures_stop_early(self):
        self.agent.instance_destroy = False
        self.agent.startup.return_value = (False, None)
        self.assertIn('Syncer: remote failed to startup a existing instance. '
                      'Manually check no instances are still running!', self.sync())
        self.agent.startup.return_value = (True, 'id')
        self.agent.setup.return_value = False
        self.plow.notify.reset_mock()
        self.assertIn('Syncer: remote failed to setup a existing instance. '
                      'Manually check no instances are still running!', self.sync())
        self.agent.sync.assert_not_called()

    def test_trigger_suspends_syncer(self):
        self.agent.sync.return_value = (False, 2, 'limit')
        self.agent.destroy.return_value = False
        with mock.patch('time.time', return_value=NOW):
            messages = self.sync()
        self.assertEqual(self.plow.syncer_bans['remote'], NOW + 2 * SECONDS_PER_HOUR)
        self.assertIn('Sync was aborted for syncer: remote due to trigger limit. Syncs suspended for 2 hours',
                      messages)
        self.assertIn('Syncer: remote failed to destroy its instance: id-1. '
                      'Manually check no instances are still running!', messages)

    def test_trigger_while_banned_keeps_ban_and_unexpected_failure(self):
        self.plow.syncer_bans['remote'] = NOW
        self.agent.sync.return_value = (False, 2, 'limit')
        self.assertIn('Sync was aborted for syncer: remote due to trigger limit.', self.sync())
        self.assertEqual(self.plow.syncer_bans['remote'], NOW)
        self.agent.sync.return_value = (False, 0, None)
        self.assertIn('Sync failed unexpectedly for syncer: remote. Manually check no instances are still running!',
                      self.sync())

    def test_sync_filters_and_survives_exceptions(self):
        self.plow._sync_one = mock.Mock(side_effect=RuntimeError('x'))
        with mock.patch.object(cloudplow_module.lock, 'sync', return_value=fake_lock(1)), \
                self.assertLogs('cloudplow', 'ERROR'):
            self.plow.sync()
        self.assertEqual(self.plow._sync_one.call_count, 2)
        self.plow._sync_one.reset_mock()
        with mock.patch.object(cloudplow_module.lock, 'sync', return_value=fake_lock()):
            self.plow.sync('local')
        self.plow._sync_one.assert_called_once_with('local', {'service': 'local'})


class CleanHiddenTests(unittest.TestCase):
    def test_cleans_remotes_and_local_hiddens(self):
        plow = make_plow(hidden={'/mnt/local/.unionfs': {'hidden_remotes': ['gd', 'td']},
                                 '/broken': {'hidden_remotes': ['gd']}},
                         remotes={'gd': {'r': 1}, 'td': {'r': 2}})

        def folder(path, *args):
            if path == '/broken':
                raise RuntimeError('broken')
            hidden = mock.Mock()
            hidden.clean_remote.side_effect = lambda remote, cfg: (None, 3, 1) if remote == 'gd' else (None, 0, 0)
            folder.hidden = hidden
            return hidden

        with mock.patch.object(cloudplow_module, 'UnionfsHiddenFolder', side_effect=folder), \
                mock.patch.object(cloudplow_module.lock, 'hidden', return_value=fake_lock(1)), \
                self.assertLogs('cloudplow', 'ERROR'):
            plow.clean_hidden()
        self.assertEqual(sent(plow), ['Cleaned 3 hidden(s) with 1 failure(s) from remote: gd'])
        folder.hidden.remove_local_hidden.assert_called_once_with()
        folder.hidden.remove_empty_dirs.assert_called_once_with()


class ScheduledTests(unittest.TestCase):
    def make(self):
        plow = make_plow(remotes={'gd': {'upload_folder': '/x'}})
        plow.clean_hidden = mock.Mock()
        plow.upload = mock.Mock()
        plow.sync = mock.Mock()
        return plow

    def test_suspended_uploader_does_nothing(self):
        plow = self.make()
        plow.uploader_bans['gd'] = 9e12
        plow.scheduled_uploader('gd', {})
        plow.upload.assert_not_called()
        plow.clean_hidden.assert_not_called()

    def test_upload_window(self):
        plow = self.make()
        window = {'schedule': {'enabled': True, 'allowed_from': '01:00', 'allowed_until': '02:00'}}
        with mock.patch.object(cloudplow_module.misc, 'is_time_between', return_value=False) as between:
            plow.scheduled_uploader('gd', window)
        between.assert_called_once_with(('01:00', '02:00'))
        plow.upload.assert_not_called()
        with mock.patch.object(cloudplow_module.misc, 'is_time_between', return_value=True):
            plow.scheduled_uploader('gd', window)
        plow.clean_hidden.assert_called_once_with()
        plow.upload.assert_called_once_with('gd')

    def test_uploader_exceptions_are_contained(self):
        plow = self.make()
        plow.upload.side_effect = RuntimeError('x')
        with self.assertLogs('cloudplow', 'ERROR'):
            plow.scheduled_uploader('gd', {})

    def test_releases_expired_service_accounts_before_upload(self):
        plow = self.make()
        plow.sa_bans['gd'] = {'sa1': 1.0}
        plow.scheduled_uploader('gd', {})
        self.assertEqual(plow.sa_bans['gd'], {'sa1': None})

    def test_scheduled_syncer(self):
        plow = self.make()
        plow.syncer_bans['s'] = 9e12
        plow.scheduled_syncer('s')
        plow.sync.assert_not_called()
        plow.scheduled_syncer('t')
        plow.sync.assert_called_once_with('t')
        plow.sync.side_effect = RuntimeError('x')
        with self.assertLogs('cloudplow', 'ERROR'):
            plow.scheduled_syncer('t')

    def test_run_in_process_starts_a_process(self):
        plow = self.make()
        with mock.patch.object(cloudplow_module, 'Process') as process:
            plow.run_in_process('s')
        process.assert_called_once_with(target=plow.scheduled_syncer, kwargs={'syncer_name': 's'})
        process.return_value.start.assert_called_once_with()


class RunForeverTests(unittest.TestCase):
    def test_schedules_jobs_and_survives_task_errors(self):
        plow = make_plow(uploader={'gd': {'check_interval': 30}},
                         syncer={'loc': {'service': 'local', 'sync_interval': 2},
                                 'rem': {'service': 'Scaleway', 'sync_interval': 6}},
                         qbittorrent={'enabled': True, 'url': 'http://127.0.0.1:1'})
        sched = mock.MagicMock()
        sched.run_pending.side_effect = [RuntimeError('task'), None]
        with mock.patch.object(cloudplow_module, 'schedule', sched), \
                mock.patch.object(cloudplow_module, 'Qbittorrent') as qbit, \
                mock.patch('time.sleep', side_effect=[None, StopLoop]), \
                self.assertLogs('cloudplow', 'ERROR') as logs:
            qbit.return_value.restore_leftovers.return_value = False
            with self.assertRaises(StopLoop):
                plow.run_forever()
        sched.every.assert_any_call(30)
        sched.every.return_value.minutes.do.assert_called_once_with(plow.scheduled_uploader, 'gd',
                                                                    {'check_interval': 30})
        sched.every.assert_any_call(2)
        sched.every.assert_any_call(6)
        hours_do = sched.every.return_value.hours.do
        hours_do.assert_any_call(plow.scheduled_syncer, syncer_name='loc')
        hours_do.assert_any_call(plow.run_in_process, syncer_name='rem')
        self.assertEqual(sched.run_pending.call_count, 2)
        self.assertTrue(any('qBittorrent' in line for line in logs.output))


class MainTests(unittest.TestCase):
    def run_main(self, cmd, app_error=None):
        conf = mock.Mock()
        conf.args = {'cmd': cmd}
        conf.settings = {'logfile': '/dev/null', 'loglevel': 'INFO'}
        with mock.patch.object(cloudplow_module, 'logs'), mock.patch.object(cloudplow_module, 'version'), \
                mock.patch.object(cloudplow_module.config, 'Config', return_value=conf), \
                mock.patch.object(cloudplow_module, 'Cloudplow') as app_cls:
            app = app_cls.return_value
            for method in ('clean_hidden', 'upload', 'sync', 'run_forever'):
                getattr(app, method).side_effect = app_error
            code = cloudplow_module.main()
        conf.load.assert_called_once_with()
        return code, app_cls, app

    def called(self, app):
        return [name for name, _, _ in app.method_calls]

    def test_update_config_exits_without_starting(self):
        code, app_cls, _ = self.run_main('update_config')
        self.assertEqual(code, 0)
        app_cls.assert_not_called()

    def test_command_dispatch(self):
        expected = {
            'clean': ['init_notifications', 'clean_hidden'],
            'upload': ['init_notifications', 'init_service_accounts', 'clean_hidden', 'upload'],
            'sync': ['init_notifications', 'init_syncers', 'sync'],
            'run': ['init_notifications', 'init_service_accounts', 'init_syncers', 'run_forever'],
        }
        for cmd, calls in expected.items():
            with self.subTest(cmd=cmd):
                code, _, app = self.run_main(cmd)
                self.assertEqual(code, 0)
                self.assertEqual(self.called(app), calls)

    def test_ctrl_c_exits_cleanly_and_errors_return_1(self):
        code, _, _ = self.run_main('run', KeyboardInterrupt())
        self.assertEqual(code, 0)
        with self.assertLogs('cloudplow', 'ERROR'):
            code, _, _ = self.run_main('upload', RuntimeError('fatal'))
        self.assertEqual(code, 1)


if __name__ == '__main__':
    unittest.main()
