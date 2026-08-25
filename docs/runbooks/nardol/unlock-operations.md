# Nardol unlock operations

Use this runbook after completing Nardol's installation and manual-fallback
proof in the [install runbook](install.md#8-prove-both-manual-fallbacks). It
covers recurring Tang recovery, portable USB enrollment and use, credential
revocation and rotation, header custody, and unlock drills.

## 9. Tang loss or rotation

`/var/lib/tang` is a recovery set. Back up the whole directory, including
retired keys, and restore it as a unit. After a Pelargir rebuild, restore the
tree before serving production requests, verify `tang-show-keys 7654` matches
the recorded thumbprint, then prove a Nardol cold boot.

If Tang state is irretrievably lost, neither volume is lost:

1. Unlock both Nardol volumes with slot 0 at the console or through initrd SSH.
2. Bring up a new Tang key set and record its thumbprint.
3. Prove slot 0 again with `cryptsetup open --test-passphrase` on both
   `/dev/disk/by-partlabel/nardol-root-luks` and `nardol-fast-luks`.
4. Remove only slot 1 from each device with `clevis luks unbind -d DEVICE -s
   1`.
5. Rebind slot 1 on each device to the verified new thumbprint and create both
   fresh header backups.

Never remove slot 0. Never rotate or delete retired Tang keys until every bound
client has been revalidated and an off-host backup has been restored in a drill.

## 10. Portable unlock: the USB key

Nardol travels to LAN parties. Clevis binds Tang at `10.0.0.165`, which is
reachable only from home, and that is the point: network-bound encryption means
a machine that leaves the house does not unlock itself. TPM binding was
considered and rejected for exactly that reason — it would make a stolen machine
self-decrypting.

The portable path is a dedicated USB stick carrying a 4096-byte key at a fixed
raw offset, enrolled in **keyslot 2** on both volumes. Slot 0 (passphrase) and
slot 1 (Clevis) are unchanged.

### What this is, and is not

This is **possession separation, not two-factor authentication**. Nardol stolen
alone stays protected. Nardol and the stick taken from the same bag open both
volumes with no Tang and no human secret. Therefore, as operational policy:
transport and store the stick separately from the machine, insert it only to
boot, remove it after unlock, and keep the sealed offline copy in a third
location.

The stick **is a key**. Never use it for file transfer, never lend it, and never
initialize, partition, format or "repair" it — any of those destroys the key.

### Unlock order is not a sequence

Once `keyFileTimeout` expires, systemd opens **one** password request, and
Clevis answers that same request concurrently. Do not wait for Clevis to finish
before typing a passphrase; both are racing to satisfy one prompt.

A stick inserted *after* the timeout expires is **not** retried.

`keyFileTimeout` bounds waiting for the device to *appear*. It does not bound
reads from a stick that enumerates and then stalls — a dying stick can still
wedge cryptsetup, and the recovery is to physically remove it.

### Qualify the stick before enrolling

1. Confirm a unique serial and that the `by-id` path survives replug, reboot and
   a different physical port.
2. Run a **destructive full-capacity integrity test** (`f3write`/`f3read` or
   equivalent). Stable enumeration is not enough: counterfeit or failing flash
   that misreports capacity corrupts the key extent silently.
3. Confirm it enumerates through `usb_storage`. `uas` is **not** in
   `boot.initrd.availableKernelModules`; a UAS-only device will not be readable
   in the initrd.

### Provision and enrol

Values are fixed and asserted by `nix flake check`: offset **4194304** (4 MiB),
size **4096**, and a non-zero `keyFileTimeout`.

```bash
# On nardol, with the qualified stick attached.
ID=/dev/disk/by-id/usb-<model>_<serial>-0:0
test "$(lsblk -ndo TRAN "$(readlink -f "$ID")")" = usb   # refuse anything else

dd if=/dev/urandom of=/root/nardol.key bs=4096 count=1
dd if=/root/nardol.key of="$ID" seek=1 bs=4096 count=1 conv=fsync
dd if="$ID" skip=1 bs=4096 count=1 2>/dev/null | sha256sum   # must match the file

for d in /dev/disk/by-partlabel/nardol-root-luks \
         /dev/disk/by-partlabel/nardol-fast-luks; do
  cryptsetup luksAddKey --key-slot 2 "$d" /root/nardol.key
  cryptsetup luksDump "$d" | grep -E '^  [0-9]+: luks2'   # expect 0, 1, 2
done
```

Seal an offline copy of `/root/nardol.key`, then remove it from the host.

**Refresh the header backups immediately.** The stored headers contain slots 0
and 1 only; restoring one later would silently delete the USB slot. Take and
verify fresh off-host backups now, and record them per §12.

Re-running **disko destroys both the Clevis and USB enrolments**. Installer
verification must require slots 0/1/2 and token 0 → slot 1.

## 11. Unlocking away from home

Stage 2 already uses DHCP, so the running system adapts to any network. The
initrd now does too: it takes a **DHCP address only** — no gateway, no DNS, no
router advertisements, no classless routes — and identifies itself as
`nardol-initrd`.

**With the stick:** insert it before powering on. Both volumes unlock
unattended.

**Without the stick, with a laptop on the same LAN:**

1. Find the address. There is no initrd mDNS, and a DHCP hostname is a lease
   label rather than a discovery protocol, so: read the party router's DHCP
   lease table for `nardol-initrd`. If that is unavailable, scan the subnet for
   an open port 2222.
2. `ssh -p 2222 root@<address>` from any RFC1918 client and enter the
   passphrase. The key is forced to the password agent; no shell is possible.

**Without the stick and with no DHCP server at all** (laptop plugged directly
into nardol): there is no address and initrd SSH is unreachable. IPv4 link-local
is deliberately disabled — it would be false comfort, since discovery still
fails. **The console passphrase is the guaranteed path.** Bring a keyboard and a
display, or do not travel without the stick.

ProxyJump via pelargir is **home-LAN recovery only**. It cannot reach a machine
physically at a party.

## 12. Revocation, rotation, and header custody

### Revocation — retire the USB path entirely

Never destroy a credential before proving the survivors.

1. On **both** volumes, prove what will remain:
   `cryptsetup open --test-passphrase --key-slot 0 <dev>`, and confirm Clevis
   slot 1 by an actual Tang-supplied unlock — not by assuming Tang is up.
2. Create and verify rollback headers.
3. Revoke **one volume at a time**: `cryptsetup luksKillSlot <dev> 2`, confirm
   slot 2 is absent in that dump, re-test the surviving paths on that volume
   before touching the next.
4. Cold boot and confirm the machine unlocks without the stick.
5. **Only then** destroy the stick and the sealed copy, and take final verified
   header backups.

### Rotation — replace the key, keep the path

State first which transaction applies: **(a)** rewriting the same device, or
**(b)** replacing it with different media.

0. **Preflight.** Confirm slot 3 is free on both volumes and slots 0/1/2 are
   present. An occupied slot 3 means a **previous rotation was interrupted** —
   identify what it holds, test it, and complete or unwind that transaction
   before starting a new one. Verify the target `by-id` path resolves to the
   expected serial before any write.
1. Stage the new key in **slot 3** on both volumes.
2. Test slot 3 on both (`--test-passphrase --key-slot 3`).
3. Write the new stick; read back the 4096-byte extent at offset 4194304 and
   compare hashes.
4. Transaction (b) only: update the `by-id` path and run **`nixos-rebuild
   boot` — install the generation, do not merely build it.** A build leaves the
   booted initrd carrying the old path, so the test below would pass for the
   wrong reason. Verify the selected boot entry and its initrd belong to the new
   generation.
5. **Cold boot isolated from Tang** on slot 3: Tang unreachable, no passphrase
   entered. Prove both volumes opened from USB using the §13 evidence rules. A
   boot with Tang reachable proves nothing.
6. Remove **old slot 2** on each volume. This must precede recreating it —
   `luksAddKey --key-slot 2` fails while the slot is occupied.
7. Add the new key explicitly to slot 2 and test it on both volumes.
8. Retain slot 3 until step 7 passes on both — it is the only rollback in this
   window. Then remove slot 3.
9. Final cold boot on slot 2. Refresh and verify headers; retire predecessors.

**Same-stick rotation** is permitted only as *routine* logical rotation, with the
remanence risk accepted explicitly: overwriting the extent cannot erase the old
bytes, because flash translation layers relocate writes. When rotation is
motivated by **compromise**, use different media and physically destroy the old
stick.

### Header and ciphertext custody

Fresh backups do **not** invalidate old ones. Restoring a post-enrolment header
resurrects a revoked key; restoring a pre-USB header deletes a live one. So
"revocation without re-encryption" holds only while every older copy stays under
control.

Maintain **two** artifact registers, because a header and a raw snapshot are not
the same object:

*Header copies* — SHA-256, volume UUID, creation date, slot/key generation,
every location, retention system and its own expiry, custodian, destruction
evidence.

*Ciphertext copies* — artifact type (snapshot / replica / image / dedup
reference), immutable ID or snapshot ID, volume UUID, **volume-key epoch**,
matching header IDs, locations and derivatives, retention, custodian,
destruction evidence.

The volume-key epoch and matching-header link are what let you decide whether an
escaped *pair* exists. Without both registers that decision cannot be made.

**Re-encryption does not "complete" revocation.** Separate the two exposures:

- *Live and future ciphertext*: changing the LUKS volume key neutralises an
  unaccounted old header against what is on the disks now.
- *Historical ciphertext*: an escaped snapshot, together with its matching
  header, stays decryptable by the revoked key **forever**. Re-encrypting the
  live volume cannot reach a copy that already left.

If an old ciphertext copy and the retired credential may both have escaped, do
not declare revocation complete. Record the residual exposure — which volume,
which key generation, which copies are unaccounted — as dated accepted risk.

### What loss actually requires

Recovery of a volume requires **all three**: intact ciphertext on a working
device, **and** a compatible header, **and** at least one credential matching
*that header generation* — slot 0, slot 2 (stick or sealed copy), or slot 1 with
Tang reachable.

A header is a precondition, not an alternative: losing every usable header loses
the volume even with every credential intact, and a restored header is useless
if no credential matching it survives. Media failure or ciphertext damage is
unrecoverable regardless of credentials.

**LUKS recovery is not a data backup.** Off-host data backups are a separate
control and must not be conflated with header custody.

## 13. Unlock drills

Each row is a **separate cold boot** with its own isolation. Never infer the
mechanism from "it booted" — attribute it from the journal.

| # | Drill | Isolation | Expected evidence, per volume |
|---|---|---|---|
| 1 | USB-only | Tang unreachable, no passphrase, stick present | cryptsetup unit succeeds; **no** Clevis success message; no password consumed |
| 2 | Tang-only | stick absent, no passphrase, home LAN | Clevis `Unlocked ... successfully` per device, after the timeout elapses |
| 3 | Console-only | stick absent, Tang unreachable, answer **only** at the console | mapping opens; SSH untouched |
| 4 | SSH-only | stick absent, Tang unreachable, answer **only** over port 2222 | mapping opens; console untouched |
| 5 | No-DHCP console | laptop direct, no DHCP server | no lease; wait-online fails after 20s; console prompt still answerable |
| 6 | Foreign DHCP | party-like subnet | lease obtained; `nardol-initrd` in the lease table; SSH reachable from RFC1918; unlock completes |
| 7 | NEG wrong key | corrupt the extent | key rejected, falls through to prompt, no hang |
| 8 | NEG late insert | insert after the timeout | not retried; falls through |
| 9 | NEG stick pulled | remove mid-unlock | record behaviour and recovery action |

Drills 3 and 4 must be separate boots: both interfaces answer the *same* shared
password request, so satisfying one proves nothing about the other.

Record the exact journal command used for each row. Drill 5 and 6 exist because
those two capabilities are claimed by §11 and are otherwise untested.

`keyFileTimeout` starts at 10s. Measure by-id appearance across every port and
set the final value above the worst observed enumeration time with margin,
normally 10–15s. Then assert the measured value in the contract.

### What the contract can and cannot prove

`nix flake check` proves **declarative intent only**. It cannot assert any live
header state — not slot 0, not slot 2, not a temporary slot 3. Someone can
`luksKillSlot 0` on the live disks with the check still green.

Runtime verification lives here: periodic `luksDump` confirming slots 0/1/2, and
periodic cold-boot `cryptsetup open --test-passphrase` drills. A temporary slot 3
during rotation is expected; final slot numbering is a runbook invariant, not a
contractable one.
