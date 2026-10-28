<img src="assets/logo.svg" width="600" alt="Cloudplow">


[![CI](https://img.shields.io/github/actions/workflow/status/newdave/cloudplow/ci.yml?branch=master&label=CI&style=flat-square)](https://github.com/newdave/cloudplow/actions/workflows/ci.yml)
[![made-with-python](https://img.shields.io/badge/Made%20with-Python%203.8%2B-blue.svg?style=flat-square)](https://www.python.org/)
[![License: GPL v3](https://img.shields.io/badge/License-GPL%203-blue.svg?style=flat-square)](LICENSE.md)
[![last commit (master)](https://img.shields.io/github/last-commit/newdave/cloudplow/master.svg?colorB=177DC1&label=Last%20Commit&style=flat-square)](https://github.com/newdave/cloudplow/commits/master)
[![Changelog](https://img.shields.io/badge/Changelog-gray.svg?style=flat-square)](CHANGELOG.md)
[![Contributing](https://img.shields.io/badge/Contributing-gray.svg?style=flat-square)](CONTRIBUTING.md)

> This is a maintained fork of [l3uddz/cloudplow](https://github.com/l3uddz/cloudplow). It adds safer uploads to
> local and network destinations (verified moves, mount guards, in-progress file handling), post-upload scripts,
> load-based bandwidth limiting and a test suite. It reads upstream `config.json` files unchanged and works as a
> drop-in replacement in [Saltbox](https://github.com/saltyorg/Saltbox) (see [Saltbox](#saltbox)). See the
> [changelog](CHANGELOG.md) for everything that changed.

---

<!-- TOC depthFrom:1 depthTo:2 withLinks:1 updateOnSave:1 orderedList:0 -->

- [Introduction](#introduction)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Sample](#sample)
  - [Core](#core)
  - [Hidden](#hidden)
  - [Notifications](#notifications)
  - [NZBGet](#nzbget)
  - [Sabnzbd](#sabnzbd)
  - [qBittorrent](#qbittorrent)
  - [Plex](#plex)
  - [Remotes](#remotes)
  - [Uploader](#uploader)
  - [Post Scripts and Rate Limiting](#post-scripts-and-rate-limiting)
  - [Syncer](#syncer)
- [Usage](#usage)
  - [Automatic (Scheduled)](#automatic-scheduled)
  - [Manual (CLI)](#manual-cli)
- [Saltbox](#saltbox)
- [Development](#development)
  - [mise tasks](#mise-tasks)
  - [Continuous integration](#continuous-integration)
- [Credits](#credits)

<!-- /TOC -->

---



# Introduction

Cloudplow has 3 main functions:

1. Automatic uploader: moves files off local storage to an Rclone remote or to another local path such as an NFS
   mount, with support for multiple uploaders (i.e. remote/folder pairings). Uploads can be:
   - **verified** (`verified_move`): local files are only removed once their copy is confirmed and they did not
     change during the upload, which makes uploading into a mergerfs branch safe;
   - **guarded** (`require_mounts`): skipped while a destination mount is missing, so nothing is written into an
     empty mount point;
   - **rate limited**: throttled while Plex streams are playing, and tuned to system load by a
     [rate script](#post-scripts-and-rate-limiting).

2. UnionFS Cleaner functionality: Deletion of UnionFS-Fuse whiteout files (`*_HIDDEN~`) and their corresponding "whited-out" files on Rclone remotes. With support for multiple remotes (useful if you have multiple Rclone remotes mounted).

3. Automatic remote syncer: Sync between two different Rclone remotes using 3rd party VM instances. With support for multiple remote/folder pairings. With support for multiple syncers (i.e. remote/remote pairings).

After every upload, an optional [post script](#post-scripts-and-rate-limiting) can act on the result.


# Requirements

1. Linux (developed on Ubuntu/Debian; other distributions should work).

2. Python 3.8 or higher, with `venv` (`sudo apt install python3 python3-venv`).

3. [Rclone](https://rclone.org/) (`rclone_binary_path` in the config).

4. `lsof`, for `exclude_open_files`. With mergerfs, passwordless `sudo` for `lsof` as well (see `opened_files_sudo`).

# Installation

1. Clone the Cloudplow repo.

   ```
   sudo git clone https://github.com/newdave/cloudplow /opt/cloudplow
   ```

2. Fix permissions of the Cloudplow folder (replace `user`/`group` with your info; run `id` to check).

   ```
   sudo chown -R user:group /opt/cloudplow
   ```

3. Go into the Cloudplow folder.

   ```
   cd /opt/cloudplow
   ```

4. Create a virtual environment and install the required Python modules into it.

   ```
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

5. Create a shortcut for Cloudplow that uses the virtual environment.

   ```
   printf '#!/bin/sh\nexec /opt/cloudplow/venv/bin/python3 /opt/cloudplow/cloudplow.py "$@"\n' | sudo tee /usr/local/bin/cloudplow >/dev/null
   sudo chmod +x /usr/local/bin/cloudplow
   ```

6. Generate a basic `config.json` file.

   ```
   cloudplow run
   ```

7. Configure the `config.json` file.

   ```
   nano config.json
   ```

   Cloudplow reads `config.json` once, at startup: restart it after every change.


# Configuration


## Sample

```json
{
    "core": {
        "dry_run": false,
        "rclone_binary_path": "/usr/bin/rclone",
        "rclone_config_path": "/home/seed/.config/rclone/rclone.conf"
    },
    "hidden": {
        "/mnt/local/.unionfs-fuse": {
            "hidden_remotes": [
                "google"
            ]
        }
    },
    "notifications": {
        "Pushover": {
            "app_token": "",
            "service": "pushover",
            "user_token": "",
            "priority": "0"
        },
        "Slack": {
            "webhook_url": "",
            "sender_name": "cloudplow",
            "sender_icon": ":heavy_exclamation_mark:",
            "channel": "",
            "service": "slack"
        }
    },
    "nzbget": {
        "enabled": false,
        "url": "https://user:pass@nzbget.domain.com"
    },
    "sabnzbd": {
        "enabled": false,
        "url": "https://sabnzbd.domain.com",
        "api-key": "1314234234"
    },
    "plex": {
        "enabled": true,
        "max_streams_before_throttle": 1,
        "ignore_local_streams": true,
        "poll_interval": 60,
        "notifications": false,
        "rclone": {
            "throttle_speeds": {
                "0": "100M",
                "1": "50M",
                "2": "40M",
                "3": "30M",
                "4": "20M",
                "5": "10M"
            },
            "url": "http://localhost:7949"
        },
        "token": "",
        "url": "https://plex.domain.com"
    },
    "remotes": {
        "google": {
            "hidden_remote": "google:",
            "rclone_excludes": [
                "**partial~",
                "**_HIDDEN~",
                ".unionfs/**",
                ".unionfs-fuse/**"
            ],
            "rclone_extras": {
                "--checkers": 16,
                "--drive-chunk-size": "64M",
                "--stats": "60s",
                "--transfers": 8,
                "--verbose": 1,
                "--skip-links": null,
                "--drive-stop-on-upload-limit": null,
                "--user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.131 Safari/537.36"
            },
            "rclone_sleeps": {
                "Failed to copy: googleapi: Error 403: User rate limit exceeded": {
                    "count": 5,
                    "sleep": 25,
                    "timeout": 3600
                },
                " 0/s,": {
                    "count": 15,
                    "sleep": 25,
                    "timeout": 140
                }
            },
            "rclone_command": "move",
            "remove_empty_dir_depth": 2,
            "require_mounts": [],
            "sync_remote": "google:/Backups",
            "upload_folder": "/mnt/local/Media",
            "upload_remote": "google:/Media",
            "verified_move": false
        },
        "google_downloads": {
            "hidden_remote": "",
            "rclone_excludes": [
              "**partial~",
              "**_HIDDEN~",
              ".unionfs/**",
              ".unionfs-fuse/**"
            ],
            "rclone_extras": {
              "--checkers": 32,
              "--stats": "60s",
              "--transfers": 16,
              "--verbose": 1,
              "--skip-links": null
            },
            "rclone_sleeps": {
            },
            "rclone_command": "copy",
            "remove_empty_dir_depth": 2,
            "sync_remote": "",
            "upload_folder": "/mnt/local/Downloads",
            "upload_remote": "google:/Downloads"
        },
        "box": {
            "hidden_remote": "box:",
            "rclone_excludes": [
              "**partial~",
              "**_HIDDEN~",
              ".unionfs/**",
              ".unionfs-fuse/**"
            ],
            "rclone_extras": {
              "--user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.131 Safari/537.36",
              "--checkers": 32,
              "--stats": "60s",
              "--transfers": 16,
              "--verbose": 1,
              "--skip-links": null
            },
            "rclone_sleeps": {
              "Failed to copy: googleapi: Error 403: User rate limit exceeded": {
                "count": 5,
                "sleep": 25,
                "timeout": 300
              },
              " 0/s,": {
                  "count": 15,
                  "sleep": 25,
                  "timeout": 140
              }
            },
            "rclone_command": "move",
            "remove_empty_dir_depth": 2,
            "sync_remote": "box:/Backups",
            "upload_folder": "/mnt/local/Media",
            "upload_remote": "box:/Media"
          },
          "google_with_mover": {
              "hidden_remote": "google:",
              "rclone_excludes": [
                  "**partial~",
                  "**_HIDDEN~",
                  ".unionfs/**",
                  ".unionfs-fuse/**"
              ],
              "rclone_extras": {
                  "--checkers": 16,
                  "--drive-chunk-size": "64M",
                  "--stats": "60s",
                  "--transfers": 8,
                  "--verbose": 1,
                  "--skip-links": null,
                  "--drive-stop-on-upload-limit": null, <-- this one
                  "--user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.131 Safari/537.36"
              },
              "rclone_sleeps": {
                  "Failed to copy: googleapi: Error 403: User rate limit exceeded": {
                      "count": 5,
                      "sleep": 25,
                      "timeout": 3600
                  },
                  " 0/s,": {
                      "count": 15,
                      "sleep": 25,
                      "timeout": 140
                  }
              },
              "rclone_command": "move",
              "remove_empty_dir_depth": 2,
              "sync_remote": "google:/Backups",
              "upload_folder": "/mnt/local/Media",
              "upload_remote": "google:/Media"
          }
    },
    "syncer": {
        "google2box": {
            "rclone_extras": {
                "--bwlimit": "80M",
                "--checkers": 32,
                "--drive-chunk-size": "64M",
                "--stats": "60s",
                "--transfers": 16,
                "--verbose": 1
            },
            "service": "scaleway",
            "sync_from": "google",
            "sync_interval": 24,
            "sync_to": "box",
            "tool_path": "/home/seed/go/bin/scw",
            "use_copy": true,
            "instance_destroy": false
          }
    },
    "uploader": {
        "google": {
            "can_be_throttled": true,
            "check_interval": 30,
            "exclude_open_files": true,
            "opened_excludes": [
                "/downloads/"
            ],
            "opened_files_sudo": false,
            "post_script": "",
            "rate_script": "",
            "rate_script_interval": 60,
            "schedule": {
                "allowed_from": "04:00",
                "allowed_until": "08:00",
                "enabled": false
            },
            "service_account_path":"/home/user/.config/cloudplow/service_accounts/"
        },
        "google_downloads": {
            "check_interval": 30,
            "exclude_open_files": true,
            "opened_excludes": [
            ],
            "schedule": {}
        },
        "google_with_mover": {
            "check_interval": 30,
            "exclude_open_files": true,
            "opened_excludes": [
                "/downloads/"
            ],
            "schedule": {},
            "service_account_path":"/home/user/.config/cloudplow/service_accounts/",
            "mover": {
                "enabled": false,
                "move_from_remote": "staging:Media",
                "move_to_remote": "gdrive:Media",
                "rclone_extras": {
                    "--delete-empty-src-dirs": null,
                    "--create-empty-src-dirs": null,
                    "--stats": "60s",
                    "--verbose": 1,
                    "--no-traverse": null,
                    "--drive-server-side-across-configs": null,
                    "--user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.131 Safari/537.36"
                }
            }
        }
    }
}
```


## Core

```
"core": {
    "dry_run": false,
    "rclone_binary_path": "/usr/bin/rclone",
    "rclone_config_path": "/home/seed/.config/rclone/rclone.conf"
},
```

`"dry_run": true` - prevent any files being uploaded or deleted - use this to test out your config.

`rclone_binary_path` - full path to Rclone binary file.

`rclone_config_path` - full path to Rclone config file.

## Hidden

UnionFS Hidden File Cleaner: Deletion of UnionFS whiteout files and their corresponding files on rclone remotes.

```
"hidden": {
    "/mnt/local/.unionfs-fuse": {
        "hidden_remotes": [
            "google"
        ]
    }
},
```

This is where you specify the location of the UnionFS `_HIDDEN~` files (i.e. whiteout files) and the Rclone remotes where the corresponding files will need to be deleted from. You may specify more than one remote here.

The specific remote path, where those corresponding files are, will be specified in the `remotes` section.

Note: If you plan on using this with any other file system, eg MergerFS, you can leave this section blank (`"hidden": {}`).

## Notifications

```json
"notifications": {
  "apprise": {
    "service": "apprise",
    "url": "",
    "title": ""
  }
},
```

Notifications alerts for both scheduled and manual Cloudplow tasks.

Supported `services`:
 - `apprise`
 - `pushover`
 - `slack`

_Note: The key name can be anything, but the `service` key must be must be the exact service name (e.g. `pushover`). See below for example._

```json
"notifications": {
  "anyname": {
    "service": "pushover",
  }
},
```

### Apprise

```json
"notifications": {
  "Apprise": {
    "service": "apprise",
    "url": "",
    "title": ""
  }
},
```

`url` - Apprise service URL (see [here](https://github.com/caronc/apprise)).

 - Required.

`title` - Notification Title.

 - Optional.

 - Default is `Cloudplow`.

### Pushover

```json
"notifications": {
    "Pushover": {
        "app_token": "",
        "service": "pushover",
        "user_token": "",
        "priority": 0
    }
},
```

`app_token`  - App Token from [Pushover.net](https://pushover.net).

 - Required.

`user_token` - User Token from [Pushover.net](https://pushover.net).

 - Required.

`priority` - [Priority](https://pushover.net/api#priority) of the notifications.

 - Optional.

 - Choices are: `-2`, `-1`, `0`, `1`, `2`.

 - Values are not quoted.

 - Default is `0`.

### Slack

```json
"notifications": {
    "Slack": {
        "service": "slack",
        "webhook_url": "",
        "channel": "",
        "sender_name": "Cloudplow",
        "sender_icon": ":heavy_exclamation_mark:"
    }
},
```

`webhook_url` - [Webhook URL](https://my.slack.com/services/new/incoming-webhook/).

 - Required.

`channel` - Slack channel to send the notifications to.

 - Optional.

 - Default is blank.

`sender_name` - Sender's name for the notifications.

 - Optional.

 - Default is `Cloudplow`.

`sender_icon` - Icon to use for the notifications.

 - Optional.

 - Default is `:heavy_exclamation_mark:`


## NZBGet

Cloudplow can pause the NZBGet download queue when an upload starts; and then resume it upon the upload finishing.

```
"nzbget": {
    "enabled": false,
    "url": "https://user:pass@nzbget.domain.com"
},
```

`enabled` - `true` to enable.

`url` - Your NZBGet URL. Can be either `http://user:pass@localhost:6789` or `https://user:pass@nzbget.domain.com`.

## Sabnzbd

Cloudplow can pause the Sabnzbd download queue when an upload starts; and then resume it upon the upload finishing.

```
"sabnzbd": {
    "enabled": false,
    "url": "https://sabnzbd.domain.com"
    "apikey": "1314234234"
},
```

`enabled` - `true` to enable.

## qBittorrent

Cloudplow can switch on qBittorrent's alternative speed limits ("turtle mode") when an upload starts, and switch them
off again when it finishes, so downloads and seeding slow down instead of stopping. Set the limits themselves in
qBittorrent under Options > Speed > Alternative Rate Limits.

```
"qbittorrent": {
    "enabled": false,
    "url": "http://qbittorrent:8080",
    "username": "admin",
    "password": "adminadmin"
},
```

`enabled` - `true` to enable.

`url` - The qBittorrent Web UI address.

`username` / `password` - Web UI login. Leave both empty if qBittorrent does not ask this host to log in
(Options > Web UI > "Bypass authentication for clients on localhost" or the whitelisted subnets).

If the alternative limits are already on when an upload starts, Cloudplow leaves them on afterwards. If Cloudplow is
stopped mid-upload, it switches the limits it turned on back off the next time it starts.

## Plex

Cloudplow can throttle Rclone uploads during active, playing Plex streams (paused streams are ignored).


```
"plex": {
    "enabled": true,
    "max_streams_before_throttle": 1,
    "ignore_local_streams": true,
    "poll_interval": 60,
    "notifications": false,
    "rclone": {
        "throttle_speeds": {
            "0": "1000M",
            "1": "50M",
            "2": "40M",
            "3": "30M",
            "4": "20M",
            "5": "10M"
        },
        "url": "http://localhost:7949"
    },
    "token": "",
    "url": "https://plex.domain.com"
},
```


`enabled` - `true` to enable.

`url` - Your Plex URL. Can be either `http://localhost:32400` or `https://plex.domain.com`.

`token` - Your Plex Access Token.

- Run the Plex Token script by [Werner Beroux](https://github.com/wernight): `/opt/cloudplow/scripts/plex_token.sh`.

  or

- Visit https://support.plex.tv/hc/en-us/articles/204059436-Finding-an-authentication-token-X-Plex-Token

`poll_interval` - How often (in seconds) Plex is checked for active streams.

`max_streams_before_throttle` - How many playing streams are allowed before enabling throttling.

`ignore_local_streams` - Whether streaming local files should count for throttling.

`notifications` - Send notifications when throttling is set, adjusted, or unset, depending on stream count.

`rclone`

- `url` - Leave as default.

- `throttle_speed` - Categorized option to configure upload speeds for various stream counts (where `5` represents 5 streams or more). Stream count `0` represents speeds when no active stream is playing. `M` is MB/s.

  - Format: `"STREAM COUNT": "THROTTLED UPLOAD SPEED",`


## Remotes

This is the heart of the configuration, most of the config references this section one way or another (e.g. hidden path references).

You can specify more than one remote here.

#### Basic

```
    "remotes": {
        "google": {
```

Under `"remote"`, you have the name of the remote as the key (in the example above, it is `"google"`). The remote name can be anything (e.g. google1, google2, google3, dropbox1, etc).



#### Hidden Cleaner

```
    "remotes": {
        "google": {
            "hidden_remote": "google:",
```


`"hidden_remote"`: is the remote path where the UnionFS hidden cleaner will remove files from (if the remote is listed under the `hidden` section).

#### Rclone Excludes


```
            "rclone_excludes": [
                "**partial~",
                "**_HIDDEN~",
                ".unionfs/**",
                ".unionfs-fuse/**"
            ],
```

These are the excludes to be used when uploading to this remote.


#### Rclone Extras


```
            "rclone_extras": {
                "--user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.131 Safari/537.36",
                "--checkers": 16,
                "--drive-chunk-size": "64M",
                "--drive-stop-on-upload-limit": null,
                "--stats": "60s",
                "--transfers": 8,
                "--verbose": 1
            },
```
These are Rclone parameters that will be used when uploading to this remote. You may use the given examples or add your own.

Note: An argument with no value (e.g. `--no-traverse`) will be be given the value `null` (e.g. `"no-traverse": null`).


#### Rclone Sleep (i.e. Ban Sleep)

Format:
```
            "rclone_sleeps": {
                "keyword or phrase to be monitored": {
                    "count": 5,
                    "sleep": 25,
                    "timeout": 300
                }
            },
```

Example:

```
            "rclone_sleeps": {
                "Failed to copy: googleapi: Error 403: User rate limit exceeded": {
                    "count": 5,
                    "sleep": 25,
                    "timeout": 300
                }
            },
```

`"rclone_sleeps"` are keywords or phrases that are monitored during Rclone tasks that will cause this remote's upload task to abort and go into a sleep for a specified amount of time. When a remote is asleep, it will not do its regularly scheduled uploads (as defined in `check_intervals`).

You may list multiple keywords or phrases here.

In the example above, the phrase `"Failed to copy: googleapi: Error 403: User rate limit exceeded"` is being monitored.

`"count"`: How many times this keyword/phrase has to occur within a specific time period (i.e. `timeout`), from the very first occurrence, to cause the remote to go to sleep.

`"timeout"`: The time period (in seconds) during which the the phrase is counted in after its first occurrence.

  - On its first occurrence, the time is logged and if the `count` is reached within this `timeout` period, the upload task will abort and the remote will go into `sleep`.

  - If the `timeout` period expires without reaching the `count`, the `count` will reset back to `0`.

  - The `timeout` period will restart again after the first new occurrence of the monitored phrase.

`"sleep"`: How many hours the remote goes to sleep for, when the monitored phrase is `count`-ed during the `timeout` period.

#### Rclone Command
```
            "rclone_command": "move",
```
This is the desired command to be used when running any Rclone uploads. Options are `move` or `copy`. Default is `move`.

#### Remove Empty Directories

```
            "remove_empty_dir_depth": 2,
```
This is the depth to min-depth to delete empty folders from relative to `upload_folder`  (1 = `/Media/ ` ; 2 = `/Media/Movies/`; 3 = `/Media/Movies/Movies-Kids/`)


```
            "upload_folder": "/mnt/local/Media/",
            "upload_remote": "google:/Media/"
```

#### Local/Remote Paths


`"upload_folder"`: is the local path that is uploaded by the `uploader` task every `check_interval` minutes.

`"upload_remote"`: is the remote path that the `uploader` task will uploaded to.

`"verified_move"`: When `true`, files are uploaded with `rclone copy` and each local file is only removed once the upload is confirmed and the local file is unchanged (same inode, size and modification time) since it was listed. Use this when the destination is also a mergerfs branch (e.g. an NFS mount) so that an app replacing a file mid-upload never loses its new version, and a file deleted or renamed mid-upload leaves no stale copy behind.

`"require_mounts"`: List of mount points that must be mounted before uploading (e.g. `["/mnt/nfs/Media/Movies"]`). If any is not mounted the upload is skipped, so files are never written into an empty mount point on the root filesystem.

#### Sync From/To Paths

`"sync_remote"`: Used by the `syncer` task. This specifies the from/to destinations used to build the Rclone command. See the [syncer](#syncer) section for more on this.

## Uploader

Each entry to `uploader` references a remote inside `remotes` (i.e. the names have to match). The remote can only be referenced ONCE.

If another folder needs to be uploaded, even to the same remote, then another uploader/remote combo must be created. The example at the top of this page shows 2 uploader/remote configs.

If multiple uploader tasks are specified, the tasks will run sequentially (vs in parallel).

```
"uploader": {
    "google": {
        "can_be_throttled": true,
        "check_interval": 30,
        "exclude_open_files": true,
        "opened_excludes": [
            "/downloads/"
        ],
        "opened_files_sudo": false,
        "post_script": "",
        "rate_script": "",
        "rate_script_interval": 60,
        "schedule": {
            "allowed_from": "04:00",
            "allowed_until": "08:00",
            "enabled": false
        },
        "service_account_path":"/home/user/config/cloudplow/service_accounts/"
      }
}
```

In the example above, the uploader references `"google"` from the `remotes` section.

`"can_be_throttled"`: When this attribute is missing or set to `true`, this uploader can be throttled if enabled in the Plex config section. When set to `false`, no throttling will be attempted on this uploader.

`"check_interval"`: How often (in minutes) to check this remote's `upload_folder` and upload it. Use rclone's `--min-age` in `rclone_extras` to hold back files that are still being written.

`"max_size_gb"` / `"size_excludes"` (optional): Only upload once `upload_folder` has reached `max_size_gb` GB, measured by `du` while skipping anything matching the `size_excludes` patterns (e.g. `"downloads/*"`). Without `max_size_gb`, every check uploads whatever is eligible. Manual `upload` runs ignore the threshold.

`"exclude_open_files"`: When set to `true`, open files will be excluded from the Rclone upload (i.e. upload will occur without them).

`"opened_files_sudo"`: Run the open file check (`lsof`) through passwordless `sudo`. Needed with mergerfs, where files written through the pool are held open by the mergerfs process (root) and are invisible to an unprivileged `lsof`.

`"opened_excludes"`: Paths the open file checker will check for when searching for open files. These are plain substring matches (not globs): in the example above, any open file with `/downloads/` in its path is still uploaded.

`"schedule"`: Allows you to specify a time period, in 24H (HH:MM) format, for when uploads are allowed to start. Uploads in progress will not stop when `allowed_until` is reached.

  - This setting will not affect manual uploads, only the automatic uploader in `run` mode.

`"service_account_path"`: Path that will be scanned for Google Drive service account keys (\*.json) to be used when performing upload operations.

  - This is currently not supported with sync operations.

`"post_script"`: Path to an executable run after each upload to this remote finishes. Leave empty (`""`) to disable. See [Post Scripts and Rate Limiting](#post-scripts-and-rate-limiting).

`"rate_script"` / `"rate_script_interval"`: An executable run every `rate_script_interval` seconds (default 60) while an upload to this remote is in progress. Leave empty (`""`) to disable. See [Post Scripts and Rate Limiting](#post-scripts-and-rate-limiting).

### Mover

Move operations occur at the end of an upload task (regardless if the task was successful or aborted).

Can be used to move uploads from one folder to another on the same remote (i.e. server side move) or moves between Google Team Drives and Google "My Drives" with the same ownership (for this we recommend Rclone 1.48+ with the `--drive-server-side-across-configs` argument).

```json
    "mover": {
        "enabled": true,
        "move_from_remote": "staging:Media",
        "move_to_remote": "gdrive:Media",
        "rclone_extras": {
            "--delete-empty-src-dirs": null,
            "--create-empty-src-dirs": null,
            "--stats": "60s",
            "--verbose": 1,
            "--no-traverse": null,
            "--drive-server-side-across-configs": null,
            "--user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.131 Safari/537.36"
        }
    }
```

`"enabled"` - Enable or disable mover function.

`"move_from_remote"` - Where to move the file/folders from.

`"move_to_remote"` - Where to move the file/folders to.

`"rclone_extras"` - Optional Rclone parameters.


## Post Scripts and Rate Limiting

### Post scripts

`post_script` is an executable (with a shebang line) that Cloudplow runs after each upload to a remote finishes, whether or not the upload succeeded. Its output is written to the Cloudplow log. It runs while the upload lock is held, so keep it short or background slow work.

The script receives these environment variables:

| Variable | Value |
|---|---|
| `CLOUDPLOW_REMOTE` | The remote name |
| `CLOUDPLOW_STATUS` | `success`, `failed` (includes a skip because a `require_mounts` path is not mounted), `aborted` (an rclone trigger suspended the remote) or `error` (an exception occurred); `running` for a `rate_script` |
| `CLOUDPLOW_REASON` | Why it failed or was aborted (empty on success) |
| `CLOUDPLOW_UPLOAD_FOLDER` / `CLOUDPLOW_UPLOAD_REMOTE` | The remote's source and destination |
| `CLOUDPLOW_OVERRIDES_FILE` | Where the script may write rclone overrides for this remote (see below) |
| `CLOUDPLOW_CONFIG_DIR` | The folder containing `config.json` |
| `CLOUDPLOW_DRY_RUN` | `1` in dry run mode, otherwise `0` |

### Rclone overrides

A script can tune a remote's uploads by writing `overrides/<remote>.json` next to `config.json` (the path is in `CLOUDPLOW_OVERRIDES_FILE`), for example:

```json
{"--transfers": 2, "--bwlimit": "100M"}
```

The file is merged over the remote's `rclone_extras` at the start of every upload. Only `--bwlimit`, `--transfers`, `--checkers`, `--tpslimit`, `--multi-thread-streams` and `--buffer-size` are accepted, so a script cannot change what is uploaded. A missing or invalid file is ignored.

### Rate scripts

`rate_script` is run every `rate_script_interval` seconds while an upload to the remote is in progress, with `CLOUDPLOW_STATUS=running`. When the `--bwlimit` in its overrides file changes, Cloudplow applies it to the running upload immediately through rclone's remote control, which is enabled by the [Plex](#plex) section. Other overrides, such as `--transfers`, take effect at the start of the next upload.

When Plex throttling is also active, the upload runs at the slower of the two limits, and when Plex streams stop it returns to the rate script's limit rather than to unlimited.

### Bundled scripts

`scripts/post_script/` contains ready-made scripts, configured by a `post_script.conf` next to `config.json` (copy `scripts/post_script/post_script.conf.sample`; the file is sourced as shell):

| Script | Use as | What it does |
|---|---|---|
| `post.sh` | `post_script` | Runs every executable in `post.d/` in name order and reports any that fail. |
| `post.d/10-tune-load` | `rate_script` (and part of `post.sh`) | Divides the local load average, and optionally a NAS's (read over SSH), by its CPU count. The busier machine selects a `TUNE_TIERS` entry (`max_load_per_cpu:transfers:bwlimit`), which is written as the remote's overrides. |
| `post.d/20-healthcheck` | part of `post.sh` | Pings a healthchecks.io-style URL or an Uptime Kuma push URL with the upload's outcome. |
| `post.d/30-permissions` | part of `post.sh` | Makes newly uploaded files on local destinations group-writable, and optionally re-owns them. |

For example, to tune every upload to load while it runs and tidy up afterwards:

```json
"uploader": {
    "nfs-movies": {
        "post_script": "/opt/cloudplow/scripts/post_script/post.sh",
        "rate_script": "/opt/cloudplow/scripts/post_script/post.d/10-tune-load",
        "rate_script_interval": 60
    }
}
```

To read a NAS's load, give `10-tune-load` a dedicated SSH key (`NAS_SSH_KEY`) and restrict it on the NAS to the one command it runs:

```
command="cat /proc/loadavg; grep -c ^processor /proc/cpuinfo",restrict ssh-ed25519 AAAA... cloudplow load probe
```

`mise run nas:install-key` does this for you: it creates `NAS_SSH_KEY` if it does not exist, logs in to `NAS_SSH` with
your usual credentials to add the restricted line (replacing any earlier line for the same key), then runs
`nas:check-key` (`--dry-run` shows what it would change). `mise run nas:check-key` tests the key the way the service uses it, and `mise run tune:show` prints the readings and
the tier the tuner would choose now.

## Syncer

Each entry to the `syncer` corresponds to a single sync task.

New `remotes` entries should be created for a single `syncer` task.

Further documentation refers to the example configurations below.

```json
    "remotes": {
        "local_torrents": {
            "hidden_remote": "",
            "rclone_excludes": [],
            "rclone_extras": {},
            "rclone_sleeps": {
                "Failed to copy: googleapi: Error 403: User rate limit exceeded": {
                    "count": 5,
                    "sleep": 25,
                    "timeout": 3600
                },
                " 0/s,": {
                    "count": 15,
                    "sleep": 25,
                    "timeout": 140
                }
            },
            "remove_empty_dir_depth": 2,
            "sync_remote": "/mnt/local/downloads/torrents",
            "upload_folder": "",
            "upload_remote": ""
        },
        "google_torrents": {
            "hidden_remote": "",
            "rclone_excludes": [],
            "rclone_extras": {},
            "rclone_sleeps": {},
            "remove_empty_dir_depth": 2,
            "sync_remote": "gdrive:/downloads/torrents",
            "upload_folder": "",
            "upload_remote": ""
        }
    },
    "syncer": {
        "torrents2google": {
            "rclone_extras": {
                "--checkers": 16,
                "--drive-chunk-size": "128M",
                "--drive-stop-on-upload-limit": null,
                "--stats": "60s",
                "--transfers": 8,
                "--verbose": 1,
                "--fast-list": null
            },
            "service": "local",
            "sync_from": "local_torrents",
            "sync_interval": 26,
            "sync_to": "google_torrents",
            "tool_path": "/usr/bin/rclone",
            "use_copy": false,
            "instance_destroy": false
          }
    },
```

### Remotes

```json
    "remotes": {
        "local_torrents": {
            "hidden_remote": "",
            "rclone_excludes": [],
            "rclone_extras": {},
            "rclone_sleeps": {
                "Failed to copy: googleapi: Error 403: User rate limit exceeded": {
                    "count": 5,
                    "sleep": 25,
                    "timeout": 3600
                },
                " 0/s,": {
                    "count": 15,
                    "sleep": 25,
                    "timeout": 140
                }
            },
            "remove_empty_dir_depth": 2,
            "sync_remote": "/mnt/local/downloads/torrents",
            "upload_folder": "",
            "upload_remote": ""
        },
        "google_torrents": {
            "hidden_remote": "",
            "rclone_excludes": [],
            "rclone_extras": {},
            "rclone_sleeps": {},
            "remove_empty_dir_depth": 2,
            "sync_remote": "gdrive:/downloads/torrents",
            "upload_folder": "",
            "upload_remote": ""
        }
    },
```

`sync_remote`: In the example above, there are two remote entries, both of which have  `sync_remote` filled-in. This is used by the syncer task to specify the sync source and destination (i.e. `sync_remote` of `sync_from` remote is the source and `sync_remote` of `sync_to` remote is the destination).

`rclone_sleeps`: Entries from both remotes are collated by the `syncer`, so there is only need for one `rclone_sleeps` to be filled in.

`rclone_extras`: Are not used by the syncer.

### Syncer

```json
    "syncer": {
        "torrents2google": {
            "rclone_extras": {
                "--checkers": 16,
                "--drive-chunk-size": "128M",
                "--drive-stop-on-upload-limit": null,
                "--stats": "60s",
                "--transfers": 8,
                "--verbose": 1,
                "--fast-list": null
            },
            "service": "local",
            "sync_from": "local_torrents",
            "sync_interval": 26,
            "sync_to": "google_torrents",
            "tool_path": "/usr/bin/rclone",
            "use_copy": false,
            "instance_destroy": false
          }
    },
```

`"rclone_extras"`: These are extra Rclone parameters that will be passed to the Rclone sync command (the `rclone_extras` in the remote entries are not used by the syncer).

`"service"`: Which syncer agent to use for the syncer task. Choices are `local` and `scaleway`. Other service providers can be added in the future.

  - `local`: a remote-to-remote sync that runs locally (i.e. on the same system as the one running Cloudplow).

  - `scaleway`: a remote-to-remote sync that runs on a Scaleway instance. Further documentation will be needed to describe the setup process.

`"sync_from"`: Where the sync is coming FROM.

  - In the example above, this is a local torrents folder.

`"sync_to"`: Where the sync is going TO.

  - In the example above, this is the `gdrive:/downloads/torrents` path.

`"sync_interval"`: How often to execute the sync, in hours. Only applies when Cloudplow is being ran as a service (see [here](#automatic-scheduled)).

`"tool_path"`: Which binary to use to execute the sync.

  - When using the `local` service, this will be the Rclone binary path.

  - When using `scaleway`, this will be the binary path of the `scw` tool.

`"use_copy"`: This tells the syncer to use the `rclone copy` command (vs the default `rclone sync` one). Default is `false`.

`"instance_destroy"`:

  - When this is `true`, the instance that is created for the sync task is destroyed after the task finishes.  This only applies to non-local sync services (e.g. `scaleway`).  

  - When this is set to `false`, it will re-use the existing instance that was previously created/shutdown after the last sync ran.

    - Note: It is able todo this because the instances being created are named after the `syncer` task (e.g. `torrents2google` in the example above). It uses this name to determine if an instance already exists, to start/stop it, rather than destroy it.


# Usage

## Automatic (Scheduled)

To have Cloudplow run automatically, do the following:

1. `sudo cp /opt/cloudplow/systemd/cloudplow.service /etc/systemd/system/`

   Edit `User`/`Group` in the copied file to match the owner of `/opt/cloudplow`.

2. `sudo systemctl daemon-reload`

3. `sudo systemctl enable cloudplow.service`

4. `sudo systemctl start cloudplow.service`

## Manual (CLI)

Command:
```
cloudplow
```

```
usage: cloudplow [-h] [--config [CONFIG]] [--logfile [LOGFILE]] [--cachefile [CACHEFILE]]
                 [--loglevel {DEBUG,INFO,WARNING,WARN,ERROR}]
                 {clean,upload,sync,run,update_config}

Script to assist cloud mount users.
Can remove UnionFS hidden files from Rclone remotes, upload local content to Rclone remotes, and keep Rclone remotes in sync.

positional arguments:
  {clean,upload,sync,run,update_config}
                        "clean": perform clean of UnionFS HIDDEN files from Rclone remotes
                        "upload": perform clean of UnionFS HIDDEN files and upload local content to Rclone remotes
                        "sync": perform sync between Rclone remotes
                        "run": starts the application in automatic mode
                        "update_config": perform simple update of config

optional arguments:
  -h, --help            show this help message and exit
  --config [CONFIG]     Config file location (default: /opt/cloudplow/config.json)
  --logfile [LOGFILE]   Log file location (default: /opt/cloudplow/cloudplow.log)
  --cachefile [CACHEFILE]
                        Cache file location (default: /opt/cloudplow/cache.db)
  --loglevel {DEBUG,INFO,WARNING,WARN,ERROR}
                        Log level (default: INFO)
```

***

# Saltbox

This fork is a drop-in replacement for upstream Cloudplow in [Saltbox](https://github.com/saltyorg/Saltbox). Point the
`cloudplow` role at it in `/srv/git/saltbox/inventories/host_vars/localhost.yml`:

```yaml
cloudplow_git_repo_url: "https://github.com/newdave/cloudplow.git"
cloudplow_git_repo_branch_primary: "master"
```

and run `sb install cloudplow`. Saltbox clones the repo, builds a Python 3.8 virtual environment, generates
`config.json` on first install (with `max_size_gb`/`size_excludes`, which this fork honours), and runs
`cloudplow.py update_config` on later installs; that keeps every setting this fork adds.

The role checks out `master` with force, so local changes in `/opt/cloudplow` are lost on every install. Keep
`config.json`, `post_script.conf` and `overrides/` there (they are not tracked), and commit and push anything else.
`mise run saltbox:install` refuses to run while there are uncommitted or unpushed changes, and `mise run config:check`
shows what the install's `update_config` would add to `config.json`.

# Development

Cloudplow has no build step. Set up a virtual environment as in [Installation](#installation), then run the tests from
the repository root:

```
venv/bin/python3 -m unittest discover -s tests
```

The verified-move tests run a real `rclone` and are skipped when it is not installed; run them as a regular user, since
one of them relies on a file being unreadable.

## mise tasks

With [mise](https://mise.jdx.dev/) installed, the common jobs are tasks (`mise tasks` lists them, `mise run <task>
--help` explains one). `mise.toml` pins the ShellCheck and actionlint versions, so local and CI results match.

| Task | What it does |
|---|---|
| `setup` | Create `venv/` and install the runtime and development requirements |
| `test [names]` | Run the unit tests, all or the named ones |
| `coverage` | Run the tests under coverage and print the report |
| `lint` | ShellCheck every shell script and actionlint the GitHub workflows |
| `check` | Everything CI checks: lint, tests, and no coverage drop against `origin/master` |
| `ci:local` | Run the CI test job in Docker (`python:3.8`, unprivileged user, rclone installed) |
| `service:status` | What cloudplow is doing: the service, an upload in progress, last results, rate limits |
| `service:restart` | Restart the service, only when no upload is running (`--wait` to wait for one to finish) |
| `logs` | Follow `cloudplow.log`, optionally only errors or one remote |
| `config:check` | Show what `update_config` (run by Saltbox on every install) would change in `config.json` |
| `saltbox:install` | Run `sb install cloudplow` after checking no work would be lost and the right repo is configured |
| `nas:install-key` | Create the NAS SSH key the load tuner uses and install it on the NAS, restricted to the load probe |
| `nas:check-key` | Test the NAS SSH key the load tuner uses, the way the service runs it (no SSH agent) |
| `tune:show` | Show the load readings and the `--transfers`/`--bwlimit` the load tuner would choose now |
| `upstream:check` | List upstream commits not in this branch, and whether they merge cleanly |
| `github:settings` | Enable, disable or show the GitHub settings CI relies on (see [CONTRIBUTING.md](CONTRIBUTING.md)) |

## Continuous integration

| Workflow | Runs | What it does |
|---|---|---|
| `ci.yml` | Every push to `master` and pull request | Tests under coverage on Python 3.8 (the version Saltbox installs), `mise run lint`, and the `CI gate` check that passes only when all of them do. A pull request also fails if it lowers total coverage. |
| `upstream-sync.yml` | Mondays, or by hand | Merges new [l3uddz/cloudplow](https://github.com/l3uddz/cloudplow) commits: a clean merge becomes a pull request, a conflict an issue. |
| `labeler.yml` | Pull requests | Labels pull requests by the files they change (`.github/labeler.yml`). |
| `labels.yml` | Changes to `.github/labels.json` | Creates or updates the repository's labels. |
| `stale.yml` | Daily | Marks issues and pull requests stale after 60 quiet days and closes them 14 days later. |

Dependabot keeps the pip requirements and the workflows' actions up to date. See [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a pull request.

| Path | Contents |
|---|---|
| `cloudplow.py` | Command line, scheduler and the `Cloudplow` class that runs uploads, syncs and Plex throttling |
| `utils/` | Config, rclone command building, uploader, syncers, notifications, locking, logging |
| `scripts/post_script/` | Bundled post and rate scripts |
| `tests/` | `unittest` suite |
| `.mise/` | mise tasks (`tasks/`) and their shared helpers (`lib.sh`) |
| `.github/` | Workflows, their scripts, labels and issue and pull request templates |
| `systemd/` | Example service unit |

# Credits

Cloudplow was created by [l3uddz](https://github.com/l3uddz) and maintained upstream at
[l3uddz/cloudplow](https://github.com/l3uddz/cloudplow) with [saltydk](https://github.com/saltydk) and other
contributors. This fork builds on their work and is released under the same [GPL-3.0 license](LICENSE.md).

To support the original authors:

  - [GitHub Sponsors: l3uddz](https://github.com/sponsors/l3uddz)

  - [GitHub Sponsors: saltydk](https://github.com/sponsors/saltydk)

  - [Paypal: l3uddz](https://www.paypal.me/l3uddz)
