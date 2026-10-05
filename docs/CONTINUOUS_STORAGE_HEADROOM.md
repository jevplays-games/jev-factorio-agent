# Storage headroom for the continuous campaign

At 2026-10-05 18:12Z, the acceptance guest had 11.591 GiB free on its
43.084 GiB root filesystem. A half-hour sample after the V22 deployment consumed
0.167 GiB, approximately 0.332 GiB/hour across the guest. If that short-term rate
continued, the guest would reach 2 GiB free in about 29 hours. This is a capacity
warning, not a forecast or evidence that the current incident was caused by disk
exhaustion.

The largest campaign files at 18:11Z were the dashboard stream (566.5 MB), gameplay
stream (297.2 MB), and active V22 research journal (47.8 MB). These files retain
active or historical evidence. Deleting or truncating them is not this repair.

## Prepared expansion

Grow the active acceptance guest disk from 45 to 96 GiB, then expand its existing
final ext4 partition online. The host filesystem backing this exact image had
404 GiB free during preflight. Sparse virtual growth does not preallocate the
entire 51 GiB increase; monitor host space as the guest subsequently uses it.

| Boundary | Expected identity before expansion |
| --- | --- |
| Domain | `factorio-native-acceptance-20260927` |
| Domain UUID | `a6ddc89e-10ba-4cf0-b549-ca89956bb711` |
| Active disk | `vda` |
| Image | `/srv/virtual-machines/factorio-native-acceptance-20260927.qcow2` |
| Capacity | `48318382080` bytes |
| Mounted root | `/dev/vda2`, ext4, read-write |
| GPT disk ID | `8DBBBC53-344E-48F7-8C34-699B403DAED7` |
| Root partition UUID | `7F734A28-61B6-4579-8D77-F289137C0D31` |
| Root partition start | `2203648` sectors of 512 bytes |

This is a prepared procedure. Merge and preflight alone do not establish that
the expansion has been applied. Retain the actual operation records and readback
with the incident evidence.

## Apply and verify

1. Revalidate the exact domain, active disk path, original capacity, host free
   space, guest UUID, mounted root, GPT identity and partition layout. Check fresh
   valid autosaves and availability of `growpart`, `resize2fs`, `sfdisk` and
   `blockdev`. Preserve a private domain-definition copy and partition-table dump.
2. Write an exclusive operation intent containing the identities, original sizes
   and intended size. Use the original campaign session and process identities
   for the final continuity check. Do not restart any VM or controller.
3. On Train, expand only the named active disk:

   ```sh
   virsh -c qemu:///system blockresize factorio-native-acceptance-20260927 vda --size 100663296
   ```

   The installed command's default unit is KiB, making this exactly 96 GiB.
   Read back `103079215104` bytes on both host and guest before continuing.
4. In that acceptance guest, run `growpart /dev/vda 2`. Revalidate the unchanged
   EFI partition, root start and root UUID, and the larger final partition.
   Then run `resize2fs /dev/vda2` to grow the mounted ext4 filesystem.
5. Retain each command's outcome. Verify larger filesystem capacity and free
   space, the same campaign session and processes, fresh useful native progress,
   autosave freshness, and sufficient host space. A larger block device alone
   does not prove that the partition and filesystem grew.

If a later step fails, retain the larger device and diagnose that step. Do not
shrink the live filesystem or blindly restore the old partition table after
filesystem growth. No controller policy, source checkout, world identity, receipt
or ownership record is part of this storage operation.

Capacity remains finite and write rates vary. Continue measuring free space and
autosave freshness during the soak. Increased headroom does not prove multi-day
reliability and does not replace a separately reviewed evidence-archival policy.
