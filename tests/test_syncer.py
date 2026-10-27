"""Behaviour tests for the syncer package (utils/syncer).

The Scaleway agent is driven entirely through a fake `scw` (utils.process.check_output) and a
fake process.stream, so no real scw/ssh/scp is ever executed. Local syncs use mocked
process.stream for argument checks and, when available, the real rclone binary against temp dirs
with an empty rclone config.
"""
import logging
import os
import shutil
import tempfile
import unittest
from unittest import mock

from utils import process
from utils.syncer import SERVICES, Syncer
from utils.syncer import scaleway as scaleway_mod
from utils.syncer.local import Local
from utils.syncer.scaleway import INSTALL_RCLONE, INSTALL_UNZIP, Scaleway

SCW = '/fake/bin/scw'
PS_HEADER = 'SERVER ID   IMAGE   ZONE   CREATED   STATUS   PORTS   NAME   COMMERCIAL TYPE'
TRIGGER = 'Failed to copy: googleapi: Error 403: User rate limit exceeded'


def remote(sync_remote, sleeps=None):
    return {
        'hidden_remote': '',
        'rclone_excludes': [],
        'rclone_extras': {'--ignored-for-sync': 1},
        'rclone_sleeps': sleeps or {},
        'remove_empty_dir_depth': 2,
        'sync_remote': sync_remote,
        'upload_folder': '',
        'upload_remote': '',
    }


class QuietLogs(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)


class FakeScw:
    """Stand-in for process.check_output that answers like the legacy scw CLI."""

    def __init__(self, region='par1', ps=PS_HEADER, run='new-instance-id', start=None,
                 uname='Linux host 4.4.0 x86_64 GNU/Linux', unzip='/usr/bin/unzip',
                 rclone='/usr/bin/rclone', cp='', cat='[gdrive]\ntype = drive\ntoken = rotated',
                 rm=None, stop=None):
        self.region = region
        self.calls = []
        self.responses = {'ps': ps, 'run': run, 'start': start, 'uname': uname, 'unzip': unzip,
                          'rclone': rclone, 'cp': cp, 'cat': cat, 'rm': rm, 'stop': stop}

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        assert args[0] == SCW, args
        rest = args[1:]
        if rest and rest[0].startswith('--region='):
            rest = rest[1:]
        sub = rest[0]
        if sub == 'ps':
            return self.responses['ps']
        if sub == 'run':
            return self.responses['run']
        if sub in ('start', 'stop'):
            resp = self.responses[sub]
            return rest[-1] if resp is None else resp
        if sub == 'rm':
            resp = self.responses['rm']
            return rest[-1] if resp is None else resp
        if sub == 'cp':
            return self.responses['cp']
        if sub == 'exec':
            command = rest[-1]
            if command == 'uname -a':
                return self.responses['uname']
            if command == INSTALL_UNZIP:
                return self.responses['unzip']
            if command == INSTALL_RCLONE:
                return self.responses['rclone']
            if command.startswith('cat '):
                return self.responses['cat']
        raise AssertionError(f'unexpected scw call: {args}')

    def subcommands(self):
        out = []
        for call in self.calls:
            rest = call[1:]
            if rest and rest[0].startswith('--region='):
                rest = rest[1:]
            out.append(rest[0])
        return out


# --------------------------------------------------------------------------- Syncer registry

class SyncerRegistryTests(QuietLogs):
    def setUp(self):
        super().setUp()
        self.config = {'remotes': {'src': remote('/tmp/does-not-matter-src'),
                                   'dst': remote('gdrive:/backup')}}
        self.syncer = Syncer(self.config)

    def test_services_registered(self):
        self.assertIs(SERVICES['local'], Local)
        self.assertIs(SERVICES['scaleway'], Scaleway)

    def test_load_local_agent_with_remote_configs(self):
        ok = self.syncer.load('job', service='local', tool_path='/usr/bin/rclone', sync_from='src', sync_to='dst',
                              use_copy=True, rclone_extras={'--checkers': 4})
        self.assertTrue(ok)
        agent = self.syncer.get('job')
        self.assertIsInstance(agent, Local)
        self.assertIs(agent.sync_from_config, self.config['remotes']['src'])
        self.assertIs(agent.sync_to_config, self.config['remotes']['dst'])
        self.assertEqual(agent.tool_path, '/usr/bin/rclone')
        self.assertEqual(agent.syncer_name, 'job')
        self.assertEqual(agent.kwargs['use_copy'], True)
        self.assertEqual(agent.kwargs['rclone_extras'], {'--checkers': 4})

    def test_load_scaleway_agent_passes_settings(self):
        ok = self.syncer.load('job', service='scaleway', tool_path=SCW, sync_from='src', sync_to='dst',
                              region='ams1', type='DEV1-S', image='ubuntu-focal', instance_destroy=False)
        self.assertTrue(ok)
        agent = self.syncer.get('job')
        self.assertIsInstance(agent, Scaleway)
        self.assertEqual((agent.region, agent.type, agent.image, agent.instance_destroy),
                         ('ams1', 'DEV1-S', 'ubuntu-focal', False))

    def test_invalid_service_rejected(self):
        logging.disable(logging.NOTSET)
        with self.assertLogs('utils.syncer', 'ERROR') as logs:
            self.assertFalse(self.syncer.load('job', service='aws', tool_path='x', sync_from='src', sync_to='dst'))
        self.assertIn('invalid sync service', logs.output[0])
        logging.disable(logging.CRITICAL)
        self.assertFalse(self.syncer.load('job', tool_path='x', sync_from='src', sync_to='dst'))
        self.assertEqual(self.syncer.agents, {})

    def test_service_name_is_case_sensitive(self):
        self.assertFalse(self.syncer.load('job', service='Local', tool_path='x', sync_from='src', sync_to='dst'))

    def test_missing_tool_path_rejected(self):
        self.assertFalse(self.syncer.load('job', service='local', sync_from='src', sync_to='dst'))
        self.assertFalse(self.syncer.load('job', service='local', tool_path='', sync_from='src', sync_to='dst'))
        self.assertEqual(self.syncer.agents, {})

    def test_missing_from_or_to_rejected(self):
        self.assertFalse(self.syncer.load('job', service='local', tool_path='x', sync_to='dst'))
        self.assertFalse(self.syncer.load('job', service='local', tool_path='x', sync_from='src'))
        self.assertFalse(self.syncer.load('job', service='local', tool_path='x', sync_from='', sync_to='dst'))
        self.assertEqual(self.syncer.agents, {})

    def test_unknown_remote_rejected(self):
        self.assertFalse(self.syncer.load('job', service='local', tool_path='x', sync_from='src', sync_to='nope'))
        self.assertFalse(self.syncer.load('job', service='local', tool_path='x', sync_from='nope', sync_to='dst'))
        self.assertEqual(self.syncer.agents, {})

    def test_missing_remotes_section_rejected(self):
        syncer = Syncer({})
        self.assertFalse(syncer.load('job', service='local', tool_path='x', sync_from='src', sync_to='dst'))

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.syncer.get('missing'))

    def test_failed_reload_keeps_previous_agent(self):
        self.assertTrue(self.syncer.load('job', service='local', tool_path='x', sync_from='src', sync_to='dst'))
        agent = self.syncer.get('job')
        self.assertFalse(self.syncer.load('job', service='local', tool_path='x', sync_from='src', sync_to='nope'))
        self.assertIs(self.syncer.get('job'), agent)

    def test_matches_cloudplow_init_syncers_shape(self):
        # cloudplow passes every syncer setting except sync_interval straight through
        settings = {'rclone_extras': {'--fast-list': None}, 'service': 'local', 'sync_from': 'src',
                    'sync_to': 'dst', 'tool_path': '/usr/bin/rclone', 'use_copy': False, 'instance_destroy': False}
        self.assertTrue(self.syncer.load(syncer_name='torrents2google', **settings))
        self.assertEqual(self.syncer.get('torrents2google').syncer_name, 'torrents2google')


# --------------------------------------------------------------------------- Local agent

class LocalAgentTests(QuietLogs):
    def setUp(self):
        super().setUp()
        self.from_cfg = remote('/src', sleeps={TRIGGER: {'count': 2, 'sleep': 25, 'timeout': 3600}})
        self.to_cfg = remote('gdrive:/dst', sleeps={' 0/s,': {'count': 3, 'sleep': 5, 'timeout': 140}})

    def agent(self, **kwargs):
        return Local('/usr/bin/rclone', self.from_cfg, self.to_cfg, syncer_name='job', **kwargs)

    def test_defaults(self):
        agent = Local('/usr/bin/rclone', self.from_cfg, self.to_cfg)
        self.assertEqual(agent.syncer_name, 'Unknown Syncer')
        self.assertFalse(agent.instance_destroy)
        self.assertIsNone(agent.instance_id)

    def test_local_never_destroys_even_if_configured(self):
        self.assertFalse(self.agent(instance_destroy=True).instance_destroy)

    def test_lifecycle_requires_startup(self):
        agent = self.agent()
        self.assertFalse(agent.setup('/tmp/rclone.conf'))
        self.assertFalse(agent.destroy())
        with mock.patch.object(process, 'stream') as stream:
            self.assertEqual(agent.sync(dry_run=False), (False, None, None))
        stream.assert_not_called()

    def test_startup_setup_destroy(self):
        agent = self.agent()
        ok, instance_id = agent.startup(name='job')
        self.assertTrue(ok)
        self.assertTrue(instance_id)
        self.assertEqual(agent.instance_id, instance_id)
        self.assertTrue(agent.setup('/cfg/rclone.conf'))
        self.assertEqual(agent.rclone_config_path, '/cfg/rclone.conf')
        self.assertTrue(agent.destroy())

    def started(self, **kwargs):
        agent = self.agent(**kwargs)
        agent.startup(name='job')
        agent.setup('/cfg/rclone.conf')
        return agent

    def test_sync_command_uses_tool_path_and_config(self):
        agent = self.started(rclone_extras={'--checkers': 16, '--fast-list': None})
        with mock.patch.object(process, 'stream', return_value=process.Result(0, '')) as stream:
            result = agent.sync(dry_run=False, rclone_config='/ignored/by/local.conf')
        self.assertEqual(result, (True, 0, None))
        args = stream.call_args[0][0]
        self.assertEqual(args, ['/usr/bin/rclone', 'sync', '/src', 'gdrive:/dst', '--checkers=16', '--fast-list',
                                '--config=/cfg/rclone.conf'])

    def test_remote_rclone_extras_are_not_used(self):
        # README: remotes' rclone_extras are not used by the syncer
        agent = self.started()
        with mock.patch.object(process, 'stream', return_value=process.Result(0, '')) as stream:
            agent.sync(dry_run=False)
        self.assertNotIn('--ignored-for-sync=1', stream.call_args[0][0])

    def test_use_copy_and_dry_run(self):
        agent = self.started(use_copy=True)
        with mock.patch.object(process, 'stream', return_value=process.Result(0, '')) as stream:
            agent.sync(dry_run=True)
        args = stream.call_args[0][0]
        self.assertEqual(args[1], 'copy')
        self.assertIn('--dry-run', args)
        self.assertEqual(args[-1], '--config=/cfg/rclone.conf')

    def test_sync_is_default_not_copy(self):
        agent = self.started()
        with mock.patch.object(process, 'stream', return_value=process.Result(0, '')) as stream:
            agent.sync(dry_run=False)
        self.assertEqual(stream.call_args[0][0][1], 'sync')
        self.assertNotIn('--dry-run', stream.call_args[0][0])

    def _stream_emitting(self, lines):
        seen = {}

        def fake_stream(args, callback=None, **kwargs):
            seen['args'] = args
            for line in lines:
                if callback(line):
                    seen['killed_at'] = line
                    break
            return process.Result(-9 if 'killed_at' in seen else 0, '\n'.join(lines))
        return fake_stream, seen

    def test_trigger_from_source_remote_suspends(self):
        agent = self.started()
        lines = ['Transferred: 1 GiB', f'ERROR : a.mkv: {TRIGGER}', f'ERROR : b.mkv: {TRIGGER}', 'never reached']
        fake, seen = self._stream_emitting(lines)
        with mock.patch.object(process, 'stream', side_effect=fake):
            ok, delay, trigger = agent.sync(dry_run=False)
        self.assertEqual((ok, delay, trigger), (False, 25, TRIGGER))
        self.assertEqual(seen['killed_at'], f'ERROR : b.mkv: {TRIGGER}')

    def test_trigger_from_destination_remote_also_tracked(self):
        agent = self.started()
        lines = ['Transferred: 0 B / 1 GiB, 0%, 0/s, ETA -'] * 3
        fake, _ = self._stream_emitting(lines)
        with mock.patch.object(process, 'stream', side_effect=fake):
            self.assertEqual(agent.sync(dry_run=False), (False, 5, ' 0/s,'))

    def test_below_trigger_count_succeeds(self):
        agent = self.started()
        fake, seen = self._stream_emitting([f'ERROR : a.mkv: {TRIGGER}', 'done'])
        with mock.patch.object(process, 'stream', side_effect=fake):
            self.assertEqual(agent.sync(dry_run=False), (True, 0, None))
        self.assertNotIn('killed_at', seen)

    def test_each_sync_gets_fresh_trigger_counts(self):
        agent = self.started()
        fake, _ = self._stream_emitting([f'ERROR : a.mkv: {TRIGGER}'])
        with mock.patch.object(process, 'stream', side_effect=fake):
            self.assertEqual(agent.sync(dry_run=False), (True, 0, None))
            self.assertEqual(agent.sync(dry_run=False), (True, 0, None))


@unittest.skipUnless(shutil.which('rclone'), 'rclone binary not installed')
class LocalRealRcloneTests(QuietLogs):
    """Runs the real rclone binary between two temp dirs with an empty rclone config."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        self.src = os.path.join(base, 'src')
        self.dst = os.path.join(base, 'dst')
        os.makedirs(os.path.join(self.src, 'show'))
        os.makedirs(self.dst)
        for rel, content in (('a.txt', 'alpha'), ('show/b.txt', 'bravo'), ('c.txt', 'charlie')):
            with open(os.path.join(self.src, rel), 'w') as fp:
                fp.write(content)
        with open(os.path.join(self.dst, 'extra.txt'), 'w') as fp:
            fp.write('only in destination')
        self.conf = os.path.join(base, 'rclone.conf')
        open(self.conf, 'w').close()

    def run_agent(self, sleeps=None, dry_run=False, **kwargs):
        agent = Local(shutil.which('rclone'), remote(self.src, sleeps), remote(self.dst), syncer_name='job', **kwargs)
        self.assertTrue(agent.startup(name='job')[0])
        self.assertTrue(agent.setup(self.conf))
        result = agent.sync(dry_run=dry_run, rclone_config=self.conf)
        self.assertTrue(agent.destroy())
        return result

    def listing(self, root):
        found = set()
        for dirpath, _, files in os.walk(root):
            for name in files:
                found.add(os.path.relpath(os.path.join(dirpath, name), root))
        return found

    def test_sync_mirrors_source_and_removes_extras(self):
        self.assertEqual(self.run_agent(), (True, 0, None))
        self.assertEqual(self.listing(self.dst), {'a.txt', 'show/b.txt', 'c.txt'})
        with open(os.path.join(self.dst, 'show', 'b.txt')) as fp:
            self.assertEqual(fp.read(), 'bravo')
        # the source is never modified by a sync
        self.assertEqual(self.listing(self.src), {'a.txt', 'show/b.txt', 'c.txt'})

    def test_use_copy_keeps_destination_only_files(self):
        self.assertEqual(self.run_agent(use_copy=True), (True, 0, None))
        self.assertEqual(self.listing(self.dst), {'a.txt', 'show/b.txt', 'c.txt', 'extra.txt'})

    def test_dry_run_changes_nothing(self):
        self.assertEqual(self.run_agent(dry_run=True), (True, 0, None))
        self.assertEqual(self.listing(self.dst), {'extra.txt'})

    def test_empty_config_file_is_untouched(self):
        self.run_agent()
        self.assertEqual(os.path.getsize(self.conf), 0)

    def test_trigger_in_real_output_aborts_and_suspends(self):
        sleeps = {'Copied (new)': {'count': 2, 'sleep': 7, 'timeout': 3600}}
        result = self.run_agent(sleeps=sleeps, rclone_extras={'-v': None, '--transfers': 1, '--checkers': 1})
        self.assertEqual(result, (False, 7, 'Copied (new)'))


# --------------------------------------------------------------------------- Scaleway agent

class ScalewayTestCase(QuietLogs):
    def setUp(self):
        super().setUp()
        sleep = mock.patch.object(scaleway_mod.time, 'sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.from_cfg = remote('gdrive:/src', sleeps={TRIGGER: {'count': 2, 'sleep': 25, 'timeout': 3600}})
        self.to_cfg = remote('gdrive2:/dst')

    def agent(self, **kwargs):
        kwargs.setdefault('syncer_name', 'torrents2google')
        return Scaleway(SCW, self.from_cfg, self.to_cfg, **kwargs)

    def patch_scw(self, fake):
        patcher = mock.patch.object(process, 'check_output', side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class ScalewayStartupTests(ScalewayTestCase):
    def test_defaults(self):
        agent = Scaleway(SCW, self.from_cfg, self.to_cfg)
        self.assertEqual((agent.region, agent.type, agent.image), ('par1', 'X64-2GB', 'ubuntu-xenial'))
        self.assertTrue(agent.instance_destroy)
        self.assertEqual(agent.syncer_name, 'Unknown Syncer')

    def test_destroy_mode_always_creates_new_instance(self):
        ps = PS_HEADER + '\nabc123  ubuntu  par1  1d  stopped  -  torrents2google  X64-2GB'
        fake = self.patch_scw(FakeScw(ps=ps, run='new-id-1'))
        agent = self.agent(instance_destroy=True, type='DEV1-S', image='ubuntu-focal')
        self.assertEqual(agent.startup(name='torrents2google'), (True, 'new-id-1'))
        self.assertEqual(fake.calls[0], [SCW, '--region=par1', 'ps', '-a'])
        self.assertEqual(fake.calls[1], [SCW, '--region=par1', 'run', '-d', '--name=torrents2google', '--ipv6',
                                         '--commercial-type=DEV1-S', 'ubuntu-focal'])
        self.assertEqual(fake.calls[2], [SCW, '--region=par1', 'exec', '-w', 'new-id-1', 'uname -a'])
        self.sleep.assert_called_once_with(scaleway_mod.BOOT_WAIT_SECONDS)
        self.assertNotIn('start', fake.subcommands())

    def test_reuse_mode_starts_existing_instance(self):
        ps = PS_HEADER + '\nabc123  ubuntu  par1  1d  stopped  -  Torrents2Google  X64-2GB'
        fake = self.patch_scw(FakeScw(ps=ps))
        agent = self.agent(instance_destroy=False)
        self.assertEqual(agent.startup(name='torrents2google'), (True, 'torrents2google'))
        self.assertEqual(fake.calls[1], [SCW, '--region=par1', 'start', 'torrents2google'])
        self.assertEqual(fake.calls[2], [SCW, '--region=par1', 'exec', '-w', 'torrents2google', 'uname -a'])
        self.assertNotIn('run', fake.subcommands())

    def test_reuse_mode_creates_when_missing(self):
        ps = PS_HEADER + '\nabc123  ubuntu  par1  1d  stopped  -  something-else  X64-2GB'
        fake = self.patch_scw(FakeScw(ps=ps, run='fresh'))
        agent = self.agent(instance_destroy=False)
        self.assertEqual(agent.startup(name='torrents2google'), (True, 'fresh'))
        self.assertIn('run', fake.subcommands())
        self.assertNotIn('start', fake.subcommands())

    def test_reuse_mode_does_not_match_instance_name_prefix(self):
        ps = PS_HEADER + '\nabc123  ubuntu  par1  1d  stopped  -  tv2google  X64-2GB'
        fake = self.patch_scw(FakeScw(ps=ps, run='fresh'))
        agent = self.agent(instance_destroy=False)
        agent.startup(name='tv')
        self.assertIn('run', fake.subcommands())

    def test_instance_lookup_uses_configured_region(self):
        fake = self.patch_scw(FakeScw())
        agent = self.agent(instance_destroy=False, region='ams1')
        agent.startup(name='torrents2google')
        self.assertEqual(fake.calls[0], [SCW, '--region=ams1', 'ps', '-a'])

    def test_region_is_passed_to_lifecycle_commands(self):
        fake = self.patch_scw(FakeScw())
        agent = self.agent(region='ams1')
        self.assertTrue(agent.startup(name='torrents2google')[0])
        for call in fake.calls[1:]:
            self.assertEqual(call[1], '--region=ams1')

    def test_ps_failure_aborts_without_creating(self):
        for ps in (None, '', 'permission denied'):
            with self.subTest(ps=ps):
                fake = FakeScw(ps=ps)
                with mock.patch.object(process, 'check_output', side_effect=fake):
                    agent = self.agent()
                    self.assertEqual(agent.startup(name='torrents2google'), (False, None))
                self.assertEqual(fake.subcommands(), ['ps'])
        self.sleep.assert_not_called()

    def test_create_failure_aborts(self):
        for run in (None, '', 'Failed to create server'):
            with self.subTest(run=run):
                fake = FakeScw(run=run)
                with mock.patch.object(process, 'check_output', side_effect=fake):
                    agent = self.agent()
                    self.assertEqual(agent.startup(name='torrents2google'), (False, None))
                self.assertEqual(fake.subcommands(), ['ps', 'run'])
                self.assertIsNone(agent.instance_id)

    def test_start_failure_aborts(self):
        ps = PS_HEADER + '\nabc123  ubuntu  par1  1d  stopped  -  torrents2google  X64-2GB'
        for start in ('', 'FAILED: server is locked'):
            with self.subTest(start=start):
                fake = FakeScw(ps=ps, start=start)
                with mock.patch.object(process, 'check_output', side_effect=fake):
                    agent = self.agent(instance_destroy=False)
                    self.assertEqual(agent.startup(name='torrents2google'), (False, 'torrents2google'))
                self.assertEqual(fake.subcommands(), ['ps', 'start'])
                self.assertIsNone(agent.instance_id)

    def test_boot_failure_destroys_new_instance(self):
        fake = self.patch_scw(FakeScw(run='new-id', uname=None))
        agent = self.agent(instance_destroy=True)
        self.assertEqual(agent.startup(name='torrents2google'), (False, 'new-id'))
        self.assertEqual(fake.calls[-1], [SCW, '--region=par1', 'rm', '-f', 'new-id'])

    def test_boot_failure_stops_reused_instance(self):
        ps = PS_HEADER + '\nabc123  ubuntu  par1  1d  stopped  -  torrents2google  X64-2GB'
        fake = self.patch_scw(FakeScw(ps=ps, uname='timeout waiting for ssh'))
        agent = self.agent(instance_destroy=False)
        self.assertEqual(agent.startup(name='torrents2google'), (False, 'torrents2google'))
        self.assertEqual(fake.calls[-1], [SCW, '--region=par1', 'stop', 'torrents2google'])
        self.assertNotIn('rm', fake.subcommands())


class ScalewaySetupTests(ScalewayTestCase):
    def started(self, fake, **kwargs):
        self.patch_scw(fake)
        agent = self.agent(**kwargs)
        agent.instance_id = 'inst-1'
        return agent

    def test_setup_requires_instance(self):
        fake = self.patch_scw(FakeScw())
        self.assertFalse(self.agent().setup('/cfg/rclone.conf'))
        self.assertEqual(fake.calls, [])

    def test_setup_installs_tools_and_copies_config(self):
        fake = FakeScw()
        agent = self.started(fake)
        self.assertTrue(agent.setup('/cfg/rclone.conf'))
        self.assertEqual(fake.calls, [
            [SCW, '--region=par1', 'exec', 'inst-1', INSTALL_UNZIP],
            [SCW, '--region=par1', 'exec', 'inst-1', INSTALL_RCLONE],
            [SCW, '--region=par1', 'cp', '/cfg/rclone.conf', 'inst-1:/root/.config/rclone/rclone.conf'],
        ])

    def test_setup_failures_tear_down_instance(self):
        cases = {
            'unzip missing': dict(unzip='E: Unable to locate package unzip'),
            'unzip error': dict(unzip=None),
            'rclone missing': dict(rclone='curl: (6) Could not resolve host'),
            'cp error': dict(cp=None),
            'cp output': dict(cp='scp: /root/.config/rclone/: No such file or directory'),
        }
        for label, overrides in cases.items():
            for destroy in (True, False):
                with self.subTest(label, instance_destroy=destroy):
                    fake = FakeScw(**overrides)
                    with mock.patch.object(process, 'check_output', side_effect=fake):
                        agent = self.agent(instance_destroy=destroy)
                        agent.instance_id = 'inst-1'
                        self.assertFalse(agent.setup('/cfg/rclone.conf'))
                    expected = ([SCW, '--region=par1', 'rm', '-f', 'inst-1'] if destroy
                                else [SCW, '--region=par1', 'stop', 'inst-1'])
                    self.assertEqual(fake.calls[-1], expected)

    def test_rclone_not_installed_when_unzip_fails(self):
        fake = FakeScw(unzip=None)
        agent = self.started(fake)
        self.assertFalse(agent.setup('/cfg/rclone.conf'))
        self.assertNotIn([SCW, '--region=par1', 'exec', 'inst-1', INSTALL_RCLONE], fake.calls)
        self.assertNotIn('cp', fake.subcommands())

    def test_config_lands_where_remote_rclone_reads_it(self):
        fake = FakeScw()
        agent = self.started(fake)
        self.assertTrue(agent.setup('/cfg/cloudplow.conf'))
        self.assertEqual(fake.calls[-1][-1], 'inst-1:/root/.config/rclone/rclone.conf')


class ScalewayDestroyTests(ScalewayTestCase):
    def test_destroy_requires_instance(self):
        fake = self.patch_scw(FakeScw())
        self.assertFalse(self.agent().destroy())
        self.assertEqual(fake.calls, [])

    def test_destroy_removes_when_configured(self):
        fake = self.patch_scw(FakeScw())
        agent = self.agent(instance_destroy=True)
        agent.instance_id = 'Inst-1'
        self.assertTrue(agent.destroy())
        self.assertEqual(fake.calls, [[SCW, '--region=par1', 'rm', '-f', 'Inst-1']])

    def test_destroy_stops_when_reusing(self):
        fake = self.patch_scw(FakeScw())
        agent = self.agent(instance_destroy=False)
        agent.instance_id = 'inst-1'
        self.assertTrue(agent.destroy())
        self.assertEqual(fake.calls, [[SCW, '--region=par1', 'stop', 'inst-1']])

    def test_destroy_reports_failure(self):
        for destroy in (True, False):
            for resp in ('', 'server not found', None):
                with self.subTest(instance_destroy=destroy, resp=resp):
                    fake = FakeScw(rm=resp, stop=resp) if resp is not None else (lambda args: None)
                    with mock.patch.object(process, 'check_output', side_effect=fake):
                        agent = self.agent(instance_destroy=destroy)
                        agent.instance_id = 'inst-1'
                        self.assertFalse(agent.destroy())

    def test_destroy_response_match_is_case_insensitive(self):
        self.patch_scw(FakeScw(rm='INST-1'))
        agent = self.agent()
        agent.instance_id = 'inst-1'
        self.assertTrue(agent.destroy())


class ScalewaySyncTests(ScalewayTestCase):
    ORIGINAL = '[gdrive]\ntype = drive\ntoken = original\n'

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = os.path.join(self.tmp.name, 'rclone.conf')
        with open(self.conf, 'w') as fp:
            fp.write(self.ORIGINAL)

    def read_conf(self):
        with open(self.conf) as fp:
            return fp.read()

    def run_sync(self, fake, lines=(), **kwargs):
        self.patch_scw(fake)
        agent = self.agent(**kwargs)
        agent.instance_id = 'inst-1'
        seen = {}

        def fake_stream(args, callback=None, **kw):
            seen['args'] = list(args)
            for line in lines:
                if callback(line):
                    seen['killed'] = True
                    break
            return process.Result(0, '')

        with mock.patch.object(process, 'stream', side_effect=fake_stream) as stream:
            result = agent.sync(rclone_config=self.conf, dry_run=False)
        return result, seen, stream

    def test_sync_requires_instance(self):
        fake = self.patch_scw(FakeScw())
        with mock.patch.object(process, 'stream') as stream:
            self.assertEqual(self.agent().sync(rclone_config=self.conf), (False, None, None))
        stream.assert_not_called()
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.read_conf(), self.ORIGINAL)

    def test_remote_rclone_command_is_quoted_and_run_via_scw_exec(self):
        result, seen, _ = self.run_sync(FakeScw(), region='ams1',
                                        rclone_extras={'--drive-chunk-size': '128M', '--exclude': '* (copy).mkv'})
        self.assertEqual(result, (True, 0, None))
        self.assertEqual(seen['args'], [
            SCW, '--region=ams1', 'exec', 'inst-1',
            "rclone sync gdrive:/src gdrive2:/dst --drive-chunk-size=128M '--exclude=* (copy).mkv'",
        ])

    def test_remote_command_does_not_embed_local_config_path(self):
        _, seen, _ = self.run_sync(FakeScw())
        self.assertNotIn(self.conf, seen['args'][-1])
        self.assertNotIn('--config', seen['args'][-1])

    def test_use_copy_and_dry_run_remote(self):
        self.patch_scw(FakeScw())
        agent = self.agent(use_copy=True)
        agent.instance_id = 'inst-1'
        with mock.patch.object(process, 'stream', return_value=process.Result(0, '')) as stream:
            agent.sync(rclone_config=self.conf, dry_run=True)
        self.assertEqual(stream.call_args[0][0][-1], 'rclone copy gdrive:/src gdrive2:/dst --dry-run')

    def test_rotated_config_is_copied_back(self):
        fake = FakeScw(cat='[gdrive]\ntype = drive\ntoken = rotated')
        result, _, _ = self.run_sync(fake)
        self.assertEqual(result, (True, 0, None))
        self.assertEqual(self.read_conf(), '[gdrive]\ntype = drive\ntoken = rotated\n')
        self.assertEqual(fake.calls[-1], [SCW, '--region=par1', 'exec', 'inst-1',
                                          'cat /root/.config/rclone/rclone.conf'])

    def test_failed_copy_back_keeps_local_config(self):
        for cat in (None, ''):
            with self.subTest(cat=cat):
                with mock.patch.object(process, 'check_output', side_effect=FakeScw(cat=cat)):
                    agent = self.agent()
                    agent.instance_id = 'inst-1'
                    with mock.patch.object(process, 'stream', return_value=process.Result(0, '')):
                        self.assertEqual(agent.sync(rclone_config=self.conf, dry_run=False), (True, 0, None))
                self.assertEqual(self.read_conf(), self.ORIGINAL)

    def test_real_check_output_failure_keeps_local_config(self):
        # exercise the real process.check_output with a command that exits non-zero ("scw" is `false`)
        false_bin = shutil.which('false')
        if not false_bin:
            self.skipTest('false binary not available')
        agent = Scaleway(false_bin, self.from_cfg, self.to_cfg)
        agent.instance_id = 'inst-1'
        with mock.patch.object(process, 'stream', return_value=process.Result(0, '')) as stream:
            self.assertEqual(agent.sync(rclone_config=self.conf, dry_run=False), (True, 0, None))
        self.assertEqual(stream.call_args[0][0][0], false_bin)
        self.assertEqual(self.read_conf(), self.ORIGINAL)

    def test_trigger_suspends_and_still_copies_config_back(self):
        lines = [f'ERROR : a: {TRIGGER}', f'ERROR : b: {TRIGGER}', 'never']
        fake = FakeScw()
        result, seen, _ = self.run_sync(fake, lines=lines)
        self.assertEqual(result, (False, 25, TRIGGER))
        self.assertTrue(seen['killed'])
        self.assertIn('token = rotated', self.read_conf())


if __name__ == '__main__':
    unittest.main()
