# Progress monitoring across checkpoint history eviction

V39 delivered 20 logistic science packs and native research advanced. After
enough blocked foreground observations, the monitor incorrectly reported
`verified progress tick regressed` and hid the research-progress label.

The checkpoint has a bounded 64-entry history. It deliberately retains the
latest background craft, but a later `step_verified` transfer can expire from
that window. In this incident the retained craft was tick 17709882 and the
expired lab transfer was tick 17711589. Computing a lifetime progress watermark
solely from this short history made the apparent completion tick move backward.

The read-only adapter can now combine the retained craft/history evidence with
the service owner's monotonic verified-step watermark. The adapter must first
bind that owner to the reviewed script hash, original session, exact admitted
execution and live launched child. The projector rejects a watermark outside
the checkpoint's tick bounds or from another session. It preserves the original
completion time; repeated ticks cannot become fresh progress. The classifier's
existing regression checks remain in force.

To diagnose this efficiently, compare the monitor's last progress tick with the
checkpoint history maximum and owner `useful_tick`. Inspect the history-retention
rule before assuming a game rollback. Verify current research from a paired,
accepted native observation. Deploy a signed read-only monitor release while
preserving its state and the existing controller execution and acceptance
baseline. Verify the actual OBS Program after the new observer has sampled.

The regression uses the real `retain_latest_craft` implementation to evict a
transfer after a retained craft. It also covers unqualified owners, wrong
sessions, future/invalid ticks and timestamp-refresh attempts. A retained
completion alone never counts as current research or crafting progress.
