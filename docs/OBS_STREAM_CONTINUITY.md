# Distinguish a stream reconnect from a stopped campaign

A reset in OBS stream duration or transmitted bytes can happen while the same
OBS process, local video source, controller, and game continue running. Verify
these separately before taking recovery action:

| Evidence | What it establishes |
| --- | --- |
| Fresh native receipts or advancing craft, furnace, or research counters | Useful gameplay progress |
| Actual process identity and start time | Process continuity |
| Current OBS Program scene and rendered screenshot | The local broadcast composition |
| `GetStreamStatus` active/reconnecting state, duration, bytes, and frames | Current outbound stream session state |
| Dated encoder connection history | Observed stream reconnects and interruptions |

A live process or Program screenshot alone does not establish uninterrupted
delivery to the streaming service. A brief stream reconnect alone does not
establish that the campaign stopped.

## Read long-running OBS logs with their dates

OBS log lines can contain only a time of day while a single log file spans
several days. Start with the file's creation date and the guest's timezone, then
inspect midnight rollovers across the complete log or corroborate dates with
the system journal. A filtered tail can put unrelated incidents next to one
another and make them appear to belong to the same day.

Exclude routine `obs-websocket` client disconnects when examining the outbound
RTMP connection. A status client closing its local WebSocket is a different
connection from the streaming output. Account for small out-of-order timestamps
from concurrent log writers; those are not midnight rollovers. If a capture
omits earlier log sections or spans rotation, retain that uncertainty instead
of assigning dates from the remaining lines alone.

Collect `outputActive`, `outputReconnecting`, `outputDuration`, `outputBytes`,
`outputTotalFrames`, and `outputSkippedFrames`. Retain the before/after evidence
and original campaign history. Sanitize endpoint paths and credentials before
sharing logs; diagnosing continuity does not require a stream key.

## Observed October 7, 2026 reconnect

The V43 native campaign continued making useful progress when the OBS stream
byte counter reset. Reconstructing the entire OBS log distinguished two events
(America/New_York local time):

- **October 5, 00:46:04:** an RTMP send timeout was followed by repeated socket
  connection failures. OBS connected again at **00:52:18.381**.
- **October 7, 00:52:18.210:** the existing connection closed almost exactly
  48 hours later. OBS's existing reconnect behavior established a new connection
  at **00:52:20.843**, about **2.6 seconds** later, without manual intervention.

Twitch documents a maximum broadcast length of 48 hours, with reconnection
behavior dependent on the encoder. The second event's timing is consistent with
that limit; the local log does not itself provide a server-side reason code
proving the limit caused the disconnect. See the official
[Twitch Broadcasting Guidelines](https://help.twitch.tv/s/article/broadcasting-guidelines?langage=en_US&language=en_US).

Readback verified the same OBS process, active outbound streaming, increasing
bytes and frame counts, zero reported skipped frames in the new stream session,
and the actual gameplay Program output. Native campaign receipts continued and
the game/controller identities did not change. The earlier six-minute network
incident was not part of the October 7 gameplay acceptance interval.

The automatic reconnect was already working. A reset stream counter was not
authority to restart OBS, the controller, or the game. Preserve the gameplay
acceptance interval and separately retain the broadcast interruption; do not
claim an uninterrupted broadcast from evidence that proves only uninterrupted
gameplay. Neither a recovered connection nor one hour of native progress
satisfies the campaign's full 24-hour acceptance requirement.
