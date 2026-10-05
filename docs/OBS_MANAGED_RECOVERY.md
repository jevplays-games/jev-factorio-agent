# Recover the managed OBS broadcast without restarting the game

The 2026-10-04 broadcast outage was independent of the Factorio controller. At
21:49 Eastern, repeated CEF `VizCompositorTh` failures ended with `GPU process
isn't usable`. The OBS owner restarted it, but OBS 32.2.0 waited at its crash
prompt. Its launcher still passed the removed `--disable-shutdown-check` option.
The video sender then failed because the OBS SRT listener was absent. Available
memory was about 6 GB; no out-of-memory evidence accompanied this failure.

A manual normal-mode selection restored OBS, but its hard-coded startup scene
selected the legacy dashboard. Recovery must verify Program, not Preview.

## Persistent repair

Use `scripts/obs_managed_launch.py` inside the existing desktop-user service,
after its display, audio, and keyring readiness checks. Preserve the established
profile, collection, stream credentials, and audio settings. For this campaign,
the command suffix is:

```sh
python3 /path/to/reviewed/obs_managed_launch.py \
  --config-dir "$HOME/.config/obs-studio" \
  --software-browser --recover-crash-markers -- \
  obs --profile STS2 --collection STS2 \
  --scene "JEV - Isolated Acceptance HUD" --startstreaming
```

The wrapper holds one inherited owner lock, rejects another OBS process, and
changes only the existing `General/BrowserHWAccel` setting to false, retaining
the original file. This removes browser hardware acceleration from the failing
rendering path; it is a mitigation, not proof that every CEF crash is eliminated.
Encoding and stream frame rate remain unchanged.

OBS 32 stores empty `run_UUID` files in `.sentinel`. Before a managed launch,
the wrapper preserves recognized markers and a recovery record in a durable
private archive, then removes only those original directory entries. It rejects
symlinks, unknown/nonempty markers, incomplete recovery records, and more than
three crash recoveries in 15 minutes. It does not delete failure history or
start a second controller. Do not use `--multi` or safe mode as a workaround;
safe mode disables WebSockets and scripting needed by this setup.

## Quick verification

1. Confirm the game/controller separately. Leave their owners and checkpoint alone.
2. Check the OBS user service, local WebSocket port, recent CEF errors, and available memory.
   Redact media URLs and credentials before retaining or printing logs.
3. If already at the crash prompt, inspect the desktop and choose normal mode for
   the authorized broadcast. Restore the exact Program scene and inspect its image.
4. After merge, install the reviewed wrapper and replace the obsolete launcher
   suffix. Back up the launcher first. Restart only the OBS user service at a
   controlled boundary; verify browser acceleration is false in its new log.
5. Verify streaming, media reception, unchanged audio, and the live Program image.
   Check that the service restart count stays stable. Retain crash and deployment
   evidence; a brief successful observation is not multi-day qualification.

Sources: [OBS removed launch flag](https://github.com/obsproject/obs-studio/issues/12650),
[OBS 32.2 crash sentinel implementation](https://github.com/obsproject/obs-studio/blob/32.2.0/frontend/utility/CrashHandler.cpp),
[browser acceleration setting](https://github.com/obsproject/obs-studio/blob/32.2.0/frontend/OBSApp.cpp).
