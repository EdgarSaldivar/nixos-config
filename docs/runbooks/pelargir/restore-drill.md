# pelargir — k3s control-plane restore drill

The tool is `hosts/nixos/pelargir/restore-drill-vm.nix`: a standalone QEMU VM that
restores the offsite backup set into a pristine data dir, starts k3s, and asserts the
recovered cluster is genuinely ours and that its Secrets decrypt.

This procedure was salvaged from K3S-PHASE1-PLAN.md on 2026-08-16 before that file
was deleted. Without it the drill VM has no documented way to run.
## How to run

Run it on **minas** — x86_64, has qemu and the capacity.

  1. Build a recovery disk from the backup set (state.db, token, node-password,
     PROVENANCE.txt), labelled RECOVERY:

       truncate -s 64M /var/tmp/k3s-drill-recovery.img
       mkfs.ext4 -L RECOVERY /var/tmp/k3s-drill-recovery.img
       mount -o loop ... && install the four files && umount

  2. Build and run the VM:

       nix build --impure --no-link --print-out-paths --expr '
         ((builtins.getFlake "/path/to/nixos-config").inputs.nixpkgs.lib.nixosSystem {
           system = "x86_64-linux"; modules = [ ./restore-drill-vm.nix ];
         }).config.system.build.vm'
       <result>/bin/run-nixos-vm

  3. ⛔ DELETE the recovery image, the VM disk and the console log afterwards.
     The image contains the CLUSTER TOKEN and the console log contains enough to
     matter. The drill is not finished until they are gone.

WHAT A PASS LOOKS LIKE
  - API READY
  - encryption-config.json PRESENT (it is NOT in the backup — it must be
    rehydrated from the token-encrypted bootstrap record inside the datastore)
  - CA fingerprint EQUAL to the live cluster's (else a fresh cluster is
    masquerading as a restore)
  - secret content hashes EQUAL to live (presence proves nothing; equality is
    what proves decryption actually worked)

Compare the printed hashes against live with:
  kubectl -n <ns> get secret <name> -o jsonpath='{.data}' | sha256sum


## What the drill asserts

It is not a boot test. The VM checks that the recovered cluster is genuinely ours
and that its Secrets decrypt — datastore identity, encrypted Secrets, CA identity
and workload recovery. A VM that boots and serves an empty cluster is a failed
drill, not a passed one.

⏳ **Last run against an encrypted-era snapshot; not re-run since.** Treat a stale
drill as an unproven restore.
