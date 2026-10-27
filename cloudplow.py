#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from multiprocessing import Process
from typing import Any, Dict, Iterator, List, Mapping, Optional

import schedule

from utils import config, decorators, lock, logs, misc, version
from utils.cache import Cache
from utils.notifications import Notifications
from utils.nzbget import Nzbget
from utils.plex import Plex
from utils.rclone import RcloneMover, RcloneThrottler
from utils.sabnzbd import Sabnzbd
from utils.syncer import Syncer
from utils.unionfs import UnionfsHiddenFolder
from utils.uploader import Uploader, UploadResult

log = logging.getLogger('cloudplow')

MOVER_REQUIRED_SETTINGS = ('move_from_remote', 'move_to_remote', 'rclone_extras')
RCLONE_STARTUP_GRACE_SECONDS = 15
SECONDS_PER_HOUR = 60 * 60


def _format_time(timestamp: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))


class Cloudplow:
    def __init__(self, conf: config.Config):
        self.configs: Dict[str, Any] = conf.configs
        self.core = self.configs['core']
        self.dry_run: bool = self.core['dry_run']

        cache = Cache(conf.settings['cachefile'])
        self.uploader_bans = cache.get_cache('uploader_bans')
        self.syncer_bans = cache.get_cache('syncer_bans')
        self.sa_bans = cache.get_cache('sa_bans')

        self.notify = Notifications()
        self.syncer = Syncer(self.configs)
        self._plex_thread: Optional[threading.Thread] = None
        self._plex_thread_lock = threading.Lock()

        lock.ensure_lock_folder()

    # ------------------------------------------------------------------ init

    def init_notifications(self) -> None:
        for name, settings in self.configs['notifications'].items():
            if not self.notify.load(**settings):
                log.error("Failed to load notification agent: %s", name)

    def init_syncers(self) -> None:
        for name, settings in self.configs['syncer'].items():
            agent_settings = {key: value for key, value in settings.items() if key != 'sync_interval'}
            self.syncer.load(syncer_name=name, **agent_settings)

    def init_service_accounts(self) -> None:
        """Reconcile cached service-account bans with the account files currently on disk."""
        log.debug("Start initializing of service accounts.")
        for remote, uploader_config in self.configs['uploader'].items():
            if remote not in self.sa_bans:
                self.sa_bans[remote] = None

            sa_path = uploader_config.get('service_account_path')
            if not sa_path or not os.path.exists(sa_path):
                continue

            sa_path = os.path.normpath(sa_path)
            on_disk = [os.path.join(sa_path, name) for name in os.listdir(sa_path) if name.endswith('.json')]
            cached = self.sa_bans[remote]
            if cached is None:
                log.debug("The following accounts are defined: %s and are about to be added to remote %r",
                          on_disk, remote)
                self.sa_bans[remote] = dict.fromkeys(on_disk)
                continue

            # drop accounts that moved or were deleted, keep bans for the rest, add new ones unbanned
            kept = {account: expiry for account, expiry in cached.items()
                    if account.startswith(sa_path) and os.path.exists(account)}
            new_accounts = [account for account in on_disk if account not in kept]
            for account in new_accounts:
                log.debug("New service account %r has been added for remote %r", account, remote)
            self.sa_bans[remote] = {**kept, **dict.fromkeys(new_accounts)}

            if new_accounts:
                log.debug("Additional service accounts were added. Lifting any current bans for remote %r", remote)
                self.uploader_bans.pop(remote, None)
        log.debug("Finished initializing of service accounts.")

    # ----------------------------------------------------------- suspensions

    def _is_suspended(self, bans: Any, name: str, activity: str, kind: str) -> bool:
        """Expire finished suspensions in `bans`; return True if `name` is still suspended."""
        suspended = False
        for banned, expiry in dict(bans.items()).items():
            if time.time() < expiry:
                log_fn = log.info if banned == name else log.debug
                log_fn("%s is still suspended due to a previously aborted %s. Normal operation in %s at %s",
                       banned, activity, misc.seconds_to_string(expiry - time.time()), _format_time(expiry))
                suspended = suspended or banned == name
            else:
                log.warning("%s is no longer suspended due to a previous aborted %s!", banned, activity)
                bans.pop(banned, None)
                self.notify.send(f"{activity.capitalize()} suspension has expired for {kind}: {banned}")
        return suspended

    def uploader_suspended(self, name: str) -> bool:
        return self._is_suspended(self.uploader_bans, name, 'upload', 'remote')

    def syncer_suspended(self, name: str) -> bool:
        return self._is_suspended(self.syncer_bans, name, 'sync', 'syncer')

    def release_expired_service_accounts(self, remote: str) -> None:
        accounts = self.sa_bans.get(remote)
        if not accounts:
            return
        now = time.time()
        expired = [account for account, expiry in accounts.items() if expiry is not None and now > expiry]
        for account in expired:
            log.debug("Setting ban status for service_account %s to None since timeout has passed", account)
        if expired:
            # sqlitedict only persists on assignment, so write back a modified copy
            self.sa_bans[remote] = {**accounts, **dict.fromkeys(expired)}

    def _set_sa_ban(self, remote: str, account: str, expiry: Optional[float]) -> None:
        self.sa_bans[remote] = {**self.sa_bans[remote], account: expiry}

    # ---------------------------------------------------------------- upload

    @decorators.timed
    def upload(self, remote: Optional[str] = None) -> None:
        lock_file = lock.upload()
        if lock_file.is_locked():
            log.info("Waiting for running upload to finish before proceeding...")

        with lock_file:
            log.info("Starting upload")
            for name, uploader_config in self.configs['uploader'].items():
                if remote and name != remote:
                    continue
                try:
                    self._upload_remote(name, uploader_config)
                except Exception:
                    log.exception("Exception occurred while uploading to remote: %s", name)
                    self.notify.send(f"Exception occurred while uploading to remote: {name}")
        log.info("Finished upload")

    def _upload_remote(self, name: str, uploader_config: Mapping[str, Any]) -> None:
        self.notify.send(f"Upload has begun for remote: {name}")
        if uploader_config.get('can_be_throttled', True):
            self._ensure_plex_monitor()
        else:
            log.debug("Skipping check for Plex stream due to throttling disabled in remote: %s", name)

        uploader = Uploader(name, uploader_config, self.configs['remotes'][name], self.core['rclone_binary_path'],
                            self.core['rclone_config_path'], self.configs['plex'], self.dry_run)

        with self._download_clients_paused():
            if self.sa_bans.get(name) is not None:
                self._upload_with_service_accounts(name, uploader)
            else:
                self._handle_upload_result(name, uploader.upload())

            if not self.dry_run:
                uploader.remove_empty_dirs()

        mover_config = uploader_config.get('mover')
        if mover_config and mover_config.get('enabled'):
            self._move(name, mover_config)

    def _upload_with_service_accounts(self, name: str, uploader: Uploader) -> None:
        available = misc.sorted_naturally(
            account for account, expiry in self.sa_bans[name].items() if expiry is None)
        log.info("There is %d available service accounts", len(available))
        log.debug("Available service accounts: %s", available)

        if not available:
            log.info("Upload aborted due to the fact that no service accounts are currently unbanned and "
                     "available to use for remote %s", name)
            self.uploader_bans[name] = misc.get_lowest_remaining_time(self.sa_bans[name])
            return

        for index, account in enumerate(available):
            uploader.set_service_account(account)
            result = uploader.upload()
            if not result.delay_hours:
                self._notify_upload_outcome(name, result.success)
                self._set_sa_ban(name, account, None)
                return

            self._set_sa_ban(name, account, time.time() + SECONDS_PER_HOUR * result.delay_hours)
            log.debug("Setting service account %s as banned for remote: %s", account, name)
            if index < len(available) - 1:
                log.info("Upload aborted due to trigger: %s being met, %s is cycling to service_account file: %s",
                         result.trigger, name, available[index + 1])
                continue

            # every account is exhausted; suspend the remote until the first one is unbanned
            self.release_expired_service_accounts(name)
            unban_time = misc.get_lowest_remaining_time(self.sa_bans[name])
            if unban_time is not None:
                log.info("Upload aborted due to trigger: %s being met, %s will continue automatic uploading "
                         "normally in %s hours", result.trigger, name, result.delay_hours)
                self.uploader_bans[name] = unban_time
                self.notify.send(f"Upload was aborted for remote: {name} due to trigger {result.trigger}. "
                                 f"Uploads suspended for {result.delay_hours} hours")

    def _handle_upload_result(self, name: str, result: UploadResult) -> None:
        if not result.delay_hours:
            self._notify_upload_outcome(name, result.success)
            if self.uploader_bans.pop(name, None) is not None:
                log.info("%s is no longer suspended due to a previous aborted upload!", name)
            return

        if name in self.uploader_bans:
            log.info("Upload aborted due to trigger: %s being met for %s uploader", result.trigger, name)
            self.notify.send(f"Upload was aborted for remote: {name} due to trigger {result.trigger}.")
            return

        log.info("Upload aborted due to trigger: %s being met, %s will continue automatic uploading normally in "
                 "%s hours", result.trigger, name, result.delay_hours)
        self.uploader_bans[name] = time.time() + SECONDS_PER_HOUR * result.delay_hours
        self.notify.send(f"Upload was aborted for remote: {name} due to trigger {result.trigger}. "
                         f"Uploads suspended for {result.delay_hours} hours")

    def _notify_upload_outcome(self, name: str, success: bool) -> None:
        if success:
            log.info("Upload completed successfully for uploader: %s", name)
            self.notify.send(f"Upload was completed successfully for remote: {name}")
        else:
            log.info("Upload not completed successfully for uploader: %s", name)
            self.notify.send(f"Upload was not completed successfully for remote: {name}")

    @contextlib.contextmanager
    def _download_clients_paused(self) -> Iterator[None]:
        """Pause enabled download clients for the duration of the block, resuming only those we paused."""
        clients: List[Any] = []
        if self.configs['nzbget']['enabled']:
            clients.append(('Nzbget', Nzbget(self.configs['nzbget']['url'])))
        if self.configs['sabnzbd']['enabled']:
            clients.append(('Sabnzbd', Sabnzbd(self.configs['sabnzbd']['url'], self.configs['sabnzbd']['apikey'])))

        paused = []
        for label, client in clients:
            if client.pause_queue():
                paused.append((label, client))
                self._log_and_notify(logging.INFO, f"Paused the {label} download queue, upload commencing!")
            else:
                self._log_and_notify(logging.ERROR,
                                     f"Failed to pause the {label} download queue, upload commencing anyway...")
        try:
            yield
        finally:
            for label, client in paused:
                if client.resume_queue():
                    self._log_and_notify(logging.INFO, f"Resumed the {label} download queue!")
                else:
                    self._log_and_notify(logging.ERROR, f"Failed to resume the {label} download queue??")

    def _move(self, name: str, mover_config: Mapping[str, Any]) -> None:
        missing = [setting for setting in MOVER_REQUIRED_SETTINGS if setting not in mover_config]
        if missing:
            log.error("Unable to act on %r mover because there was no %r setting in the mover configuration",
                      name, missing[0])
            return

        route = f"{mover_config['move_from_remote']} -> {mover_config['move_to_remote']}"
        mover = RcloneMover(mover_config, self.core['rclone_binary_path'], self.core['rclone_config_path'],
                            self.configs['plex'], self.dry_run)
        log.info("Move starting from %s", route)
        self.notify.send(f"Move has started for {route}")
        if mover.move():
            log.info("Move completed successfully from %s", route)
            self.notify.send(f"Move finished successfully for {route}")
        else:
            log.error("Move failed from %s", route)
            self.notify.send(f"Move failed for {route}")

    def _log_and_notify(self, level: int, message: str) -> None:
        log.log(level, message)
        self.notify.send(message)

    # ------------------------------------------------------------------ sync

    @decorators.timed
    def sync(self, only: Optional[str] = None) -> None:
        lock_file = lock.sync()
        if lock_file.is_locked():
            log.info("Waiting for running sync to finish before proceeding...")

        with lock_file:
            log.info("Starting sync")
            for name, sync_config in self.configs['syncer'].items():
                if only and name != only:
                    continue
                try:
                    self._sync_one(name, sync_config)
                except Exception:
                    log.exception("Exception occurred while syncing: %s", name)
        log.info("Finished sync")

    def _sync_one(self, name: str, sync_config: Mapping[str, Any]) -> None:
        agent = self.syncer.get(name)
        if agent is None:
            return
        service = sync_config['service']
        is_remote = service.lower() != 'local'
        instance_word = 'new' if agent.instance_destroy else 'existing'

        if is_remote:
            self.notify.send(f"Sync initiated for syncer: {name}. "
                             f"{'Creating' if agent.instance_destroy else 'Starting'} {service} instance...")

        started, instance_id = agent.startup(name=name)
        if not started:
            self.notify.send(f"Syncer: {name} failed to startup a {instance_word} instance. "
                             "Manually check no instances are still running!")
            return
        if not agent.setup(rclone_config=self.core['rclone_config_path']):
            self.notify.send(f"Syncer: {name} failed to setup a {instance_word} instance. "
                             "Manually check no instances are still running!")
            return

        self.notify.send(f"Sync has begun for syncer: {name}")
        ok, delay_hours, trigger = agent.sync(dry_run=self.dry_run, rclone_config=self.core['rclone_config_path'])
        if not ok and not delay_hours:
            log.error("Sync unexpectedly failed for syncer: %s", name)
            self.notify.send(f"Sync failed unexpectedly for syncer: {name}. "
                             "Manually check no instances are still running!")
        elif not ok and trigger:
            if name in self.syncer_bans:
                log.info("Sync aborted due to trigger: %s being met for %s syncer", trigger, name)
                self.notify.send(f"Sync was aborted for syncer: {name} due to trigger {trigger}.")
            else:
                log.info("Sync aborted due to trigger: %s being met, %s will continue automatic syncing normally "
                         "in %s hours", trigger, name, delay_hours)
                self.syncer_bans[name] = time.time() + SECONDS_PER_HOUR * delay_hours
                self.notify.send(f"Sync was aborted for syncer: {name} due to trigger {trigger}. "
                                 f"Syncs suspended for {delay_hours} hours")
        else:
            log.info("Syncing completed successfully for syncer: %s", name)
            self.notify.send(f"Sync was completed successfully for syncer: {name}")
            if self.syncer_bans.pop(name, None) is not None:
                log.info("%s is no longer suspended due to a previous aborted sync!", name)

        destroyed = agent.destroy()
        if is_remote:
            if destroyed:
                self.notify.send(f"Syncer: {name} has {'destroyed' if agent.instance_destroy else 'stopped'} "
                                 f"its {service} instance")
            else:
                self.notify.send(f"Syncer: {name} failed to {'destroy' if agent.instance_destroy else 'stop'} its "
                                 f"instance: {instance_id}. Manually check no instances are still running!")

    # ---------------------------------------------------------------- hidden

    @decorators.timed
    def clean_hidden(self) -> None:
        lock_file = lock.hidden()
        if lock_file.is_locked():
            log.info("Waiting for running hidden cleaner to finish before proceeding...")

        with lock_file:
            log.info("Starting hidden cleaning")
            for folder, hidden_config in self.configs['hidden'].items():
                try:
                    hidden = UnionfsHiddenFolder(folder, self.dry_run, self.core['rclone_binary_path'],
                                                 self.core['rclone_config_path'])
                    for remote in hidden_config['hidden_remotes']:
                        _, deleted, failed = hidden.clean_remote(remote, self.configs['remotes'][remote])
                        if deleted or failed:
                            self.notify.send(f"Cleaned {deleted} hidden(s) with {failed} failure(s) "
                                             f"from remote: {remote}")
                    if not self.dry_run:
                        hidden.remove_local_hidden()
                        hidden.remove_empty_dirs()
                except Exception:
                    log.exception("Exception occurred while cleaning hiddens in %s: ", folder)
        log.info("Finished hidden cleaning")

    # ------------------------------------------------------------------ plex

    def _ensure_plex_monitor(self) -> None:
        if not self.configs['plex']['enabled']:
            return
        with self._plex_thread_lock:
            if self._plex_thread is None or not self._plex_thread.is_alive():
                self._plex_thread = threading.Thread(target=self.plex_monitor, name='plex-monitor', daemon=True)
                self._plex_thread.start()

    @decorators.timed
    def plex_monitor(self) -> None:
        """Throttle the running upload while enough Plex streams are playing; exits when the upload ends."""
        plex_config = self.configs['plex']
        plex = Plex(plex_config['url'], plex_config['token'])
        if not plex.validate():
            log.error("Aborting Plex Media Server stream monitor due to failure to validate supplied server URL "
                      "and/or Token.")
            return

        log.info("Plex Media Server URL + Token were validated. Sleeping for %d seconds before checking Rclone "
                 "RC URL.", RCLONE_STARTUP_GRACE_SECONDS)
        time.sleep(RCLONE_STARTUP_GRACE_SECONDS)

        rclone = RcloneThrottler(plex_config['rclone']['url'])
        if not rclone.validate():
            log.error("Aborting Plex Media Server stream monitor due to failure to validate supplied Rclone RC URL.")
            return
        log.info("Rclone RC URL was validated. Stream monitoring for Plex Media Server will now begin.")

        max_streams = plex_config['max_streams_before_throttle']
        speeds = plex_config['rclone']['throttle_speeds']
        throttled = False
        throttle_speed: Optional[str] = None
        upload_lock = lock.upload()

        while upload_lock.is_locked():
            streams = plex.get_streams()
            if streams is None:
                log.error("Failed to check Plex Media Server stream(s). Trying again in %s seconds...",
                          plex_config['poll_interval'])
                time.sleep(plex_config['poll_interval'])
                continue

            stream_count = sum(1 for stream in streams if stream.active and
                               (not stream.local or not plex_config['ignore_local_streams']))
            wanted_speed = misc.get_nearest_less_element(speeds, stream_count)

            if stream_count >= max_streams and (not throttled or not rclone.throttle_active(throttle_speed)):
                log.info("There was %d playing stream(s) on Plex Media Server while it was currently un-throttled.",
                         stream_count)
                for stream in streams:
                    log.info(stream)
                log.info("Upload throttling will now commence.")
                throttle_speed = wanted_speed
                throttled = rclone.throttle(throttle_speed)
                if throttled and plex_config['notifications']:
                    self.notify.send(f"Throttled current upload to {throttle_speed} because there was "
                                     f"{stream_count} playing stream(s) on Plex")

            elif throttled and stream_count < max_streams:
                log.info("There was less than %d playing stream(s) on Plex Media Server while it was currently "
                         "throttled. Removing throttle ...", max_streams)
                throttled = not rclone.no_throttle()
                throttle_speed = None
                if not throttled and plex_config['notifications']:
                    self.notify.send(f"Un-throttled current upload because there was less than {max_streams} "
                                     "playing stream(s) on Plex Media Server")

            elif throttled and wanted_speed != throttle_speed:
                throttle_speed = wanted_speed
                log.info("Adjusting throttle speed for current upload to %s because there was now %d playing "
                         "stream(s) on Plex Media Server", throttle_speed, stream_count)
                throttled = rclone.throttle(throttle_speed)
                if throttled and plex_config['notifications']:
                    self.notify.send(f"Throttle for current upload was adjusted to {throttle_speed} due to "
                                     f"{stream_count} playing stream(s) on Plex Media Server")

            elif throttled:
                log.info("There was %d playing stream(s) on Plex Media Server it was already throttled to %s. "
                         "Throttling will continue.", stream_count, throttle_speed)

            time.sleep(plex_config['poll_interval'])

        log.info("Finished monitoring Plex stream(s)!")

    # ------------------------------------------------------------- scheduled

    def scheduled_uploader(self, name: str, uploader_config: Mapping[str, Any]) -> None:
        log.debug("Scheduled upload triggered for uploader: %s", name)
        try:
            if self.uploader_suspended(name):
                return
            self.release_expired_service_accounts(name)

            upload_window = uploader_config.get('schedule', {})
            if upload_window.get('enabled'):
                allowed = (upload_window['allowed_from'], upload_window['allowed_until'])
                if not misc.is_time_between(allowed):
                    log.info("Uploader: %s. The current time %s is not within the allowed upload time periods "
                             "%s -> %s", name, time.strftime('%H:%M'), *allowed)
                    return

            self.clean_hidden()
            self.upload(name)
        except Exception:
            log.exception("Unexpected exception occurred while processing uploader %s: ", name)

    def scheduled_syncer(self, syncer_name: str) -> None:
        log.info("Scheduled sync triggered for syncer: %s", syncer_name)
        try:
            if not self.syncer_suspended(syncer_name):
                self.sync(syncer_name)
        except Exception:
            log.exception("Unexpected exception occurred while processing syncer: %s", syncer_name)

    def run_in_process(self, syncer_name: str) -> None:
        Process(target=self.scheduled_syncer, kwargs={'syncer_name': syncer_name}).start()

    def run_forever(self) -> None:
        for name, uploader_config in self.configs['uploader'].items():
            schedule.every(uploader_config['check_interval']).minutes.do(self.scheduled_uploader, name,
                                                                        uploader_config)
            log.info("Added %s uploader to schedule, uploading every %d minutes", name,
                     uploader_config['check_interval'])

        for name, sync_config in self.configs['syncer'].items():
            # remote syncers can run for hours, keep them off the scheduler thread
            job = self.scheduled_syncer if sync_config['service'].lower() == 'local' else self.run_in_process
            schedule.every(sync_config['sync_interval']).hours.do(job, syncer_name=name)
            log.info("Added %s syncer to schedule, syncing every %d hours", name, sync_config['sync_interval'])

        while True:
            try:
                schedule.run_pending()
            except Exception:
                log.exception("Unhandled exception occurred while processing scheduled tasks: ")
            time.sleep(1)


def main() -> int:
    logs.setup_console()
    conf = config.Config()
    logs.setup_file(conf.settings['logfile'])
    logs.set_level(conf.settings['loglevel'])
    conf.load()
    version.check_version()

    command = conf.args['cmd']
    if command == 'update_config':
        return 0

    app = Cloudplow(conf)
    app.init_notifications()
    try:
        if command == 'clean':
            log.info("Started in clean mode")
            app.clean_hidden()
        elif command == 'upload':
            log.info("Started in upload mode")
            app.init_service_accounts()
            app.clean_hidden()
            app.upload()
        elif command == 'sync':
            log.info("Starting in sync mode")
            app.init_syncers()
            app.sync()
        elif command == 'run':
            log.info("Started in run mode")
            app.init_service_accounts()
            app.init_syncers()
            app.run_forever()
    except KeyboardInterrupt:
        log.info("cloudplow was interrupted by Ctrl + C")
    except Exception:
        log.exception("Unexpected fatal exception occurred: ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
