# k3s control-plane RESTORE DRILL — standalone QEMU VM.
#
# NOT imported by any host. This is a tool: it boots a throwaway VM, restores the
# offsite backup set into a pristine data dir, starts k3s, and asserts that the
# recovered cluster is genuinely ours and that its Secrets decrypt.
#
# It lives in the repo because the drill MUST be repeatable and the previous copy
# survived only in a scratch directory, so it had to be reconstructed from scratch.
#
# ---------------------------------------------------------------------------
# HOW TO RUN (on minas — x86_64, has qemu and the capacity)
# ---------------------------------------------------------------------------
#   1. Build a recovery disk from the backup set (state.db, token, node-password,
#      PROVENANCE.txt), labelled RECOVERY:
#
#        truncate -s 64M /var/tmp/k3s-drill-recovery.img
#        mkfs.ext4 -L RECOVERY /var/tmp/k3s-drill-recovery.img
#        mount -o loop ... && install the four files && umount
#
#   2. Build and run the VM:
#
#        nix build --impure --no-link --print-out-paths --expr '
#          ((builtins.getFlake "/path/to/nixos-config").inputs.nixpkgs.lib.nixosSystem {
#            system = "x86_64-linux"; modules = [ ./restore-drill-vm.nix ];
#          }).config.system.build.vm'
#        <result>/bin/run-nixos-vm
#
#   3. ⛔ DELETE the recovery image, the VM disk and the console log afterwards.
#      The image contains the CLUSTER TOKEN and the console log contains enough to
#      matter. The drill is not finished until they are gone.
#
# ---------------------------------------------------------------------------
# WHAT A PASS LOOKS LIKE
# ---------------------------------------------------------------------------
#   - API READY
#   - encryption-config.json PRESENT (it is NOT in the backup — it must be
#     rehydrated from the token-encrypted bootstrap record inside the datastore)
#   - CA fingerprint EQUAL to the live cluster's (else a fresh cluster is
#     masquerading as a restore)
#   - secret content hashes EQUAL to live (presence proves nothing; equality is
#     what proves decryption actually worked)
#
# Compare the printed hashes against live with:
#   kubectl -n <ns> get secret <name> -o jsonpath='{.data}' | sha256sum
#
{
  config,
  pkgs,
  lib,
  ...
}:
{
  system.stateVersion = "26.05";
  boot.loader.grub.enable = false;
  users.users.root.password = "";
  services.getty.autologinUser = "root";

  # k3s REFUSES to start without a default route ("no default routes found in
  # /proc/net/route"), so the VM cannot be fully networkless. It gets QEMU's
  # standard user-mode address STATICALLY (no DHCP dependency) while the qemu
  # side keeps `restrict=on`, which blocks guest->host and all outbound traffic.
  # So there is a route table but nothing reachable: k3s is satisfied and the VM
  # still cannot touch the live cluster, the real tailnet, or minas.
  networking.useDHCP = false;
  networking.interfaces.eth0.ipv4.addresses = [
    {
      address = "10.0.2.15";
      prefixLength = 24;
    }
  ];
  networking.defaultGateway = {
    address = "10.0.2.2";
    interface = "eth0";
  };
  networking.firewall.enable = false;

  environment.systemPackages = [
    pkgs.k3s
    pkgs.sqlite
    pkgs.util-linux
  ];

  virtualisation.vmVariant.virtualisation = {
    memorySize = 6144;
    cores = 4;
    graphics = false;
    # `restrict=on` blocks guest->host and outbound. No forwards, no shares.
    qemu.networkingOptions = lib.mkForce [
      "-net nic,model=virtio"
      "-net user,restrict=on"
    ];
    qemu.options = [ "-drive file=/var/tmp/k3s-drill-recovery.img,format=raw,if=virtio,readonly=on" ];
  };

  systemd.services.restore-drill = {
    description = "k3s control-plane restore drill";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    serviceConfig = {
      Type = "oneshot";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
    path = with pkgs; [
      k3s
      sqlite
      util-linux
      coreutils
      gnugrep
      gnused
      openssl
      systemd
    ];
    script = ''
      say() { echo "DRILL| $*"; }
      say "=================== RESTORE DRILL START ==================="
      mkdir -p /mnt/recovery
      mount -o ro /dev/disk/by-label/RECOVERY /mnt/recovery || { say "FAIL: cannot mount recovery disk"; poweroff -f; }
      say "recovery set: $(ls /mnt/recovery | tr '\n' ' ')"
      say "provenance:"; sed 's/^/DRILL|   /' /mnt/recovery/PROVENANCE.txt | head -6

      # Snapshot identity BEFORE k3s touches anything, so a fresh DB cannot masquerade.
      # The recovery disk is mounted READ-ONLY and state.db is in WAL mode, so
      # sqlite cannot even read it in place -- it needs to create a -shm file.
      # ("Error: in prepare, unable to open database file (14)".) Query a
      # writable copy; hash the ORIGINAL so identity is still proven.
      PRE_HASH=$(sha256sum /mnt/recovery/state.db | cut -c1-16)
      cp /mnt/recovery/state.db /tmp/pre.db
      PRE_ROWS=$(sqlite3 /tmp/pre.db "SELECT COUNT(*) FROM kine;")
      PRE_SEC=$(sqlite3 /tmp/pre.db "SELECT COUNT(DISTINCT name) FROM kine WHERE name LIKE '/registry/secrets/%';")
      rm -f /tmp/pre.db
      say "pre-boot: kine=$PRE_ROWS secrets=$PRE_SEC sha256=$PRE_HASH"

      # Pristine data dir + pre-placed db/token. NOT --cluster-reset (etcd-only).
      D=/var/lib/rancher/k3s
      rm -rf "$D"; mkdir -p "$D/server/db"
      install -m 0600 /mnt/recovery/state.db "$D/server/db/state.db"
      install -m 0600 /mnt/recovery/token    "$D/server/token"
      [ -f /mnt/recovery/node-password ] && { mkdir -p /etc/rancher/node; install -m 0600 /mnt/recovery/node-password /etc/rancher/node/password; say "node-password placed"; }

      # Server args are DERIVED FROM PROVENANCE.txt, not hardcoded.
      #
      # The first encrypted-era run of this drill hardcoded the pre-P1B arguments and
      # therefore omitted --secrets-encryption. k3s came up with the IDENTITY
      # transformer, could not read a single encrypted Secret
      # ("identity transformer tried to read encrypted data"), and the API never went
      # ready. The backup was fine; the drill was reproducing the wrong server.
      #
      # That is precisely the failure docs/runbooks/pelargir/rollback.md warns about, so hardcoding here would
      # make the drill silently drift from the cluster it is supposed to prove
      # recoverable. PROVENANCE.txt exists to answer "what was this written by" -- so
      # read it.
      #
      # Three flags are dropped deliberately: --token-file and --agent-token-file point
      # at sops paths that do not exist here (the token is placed into the data-dir
      # instead), and --vpn-auth-file MUST be omitted so the VM cannot reach the real
      # tailnet.
      ARGS=""
      while read -r a; do
        case "$a" in
          --token-file*|--agent-token-file*|--vpn-auth-file*) continue ;;
          --*) ARGS="$ARGS $a" ;;
        esac
      done < <(sed -n 's/^  \(--.*\)$/\1/p' /mnt/recovery/PROVENANCE.txt)
      say "server args from PROVENANCE:$ARGS"
      case "$ARGS" in
        *--secrets-encryption*) say "  --secrets-encryption PRESENT (required to read encrypted Secrets)" ;;
        *) say "  NOTE: no --secrets-encryption in provenance (pre-P1B snapshot)" ;;
      esac

      say "starting k3s server (vpn-auth OMITTED so it cannot reach the real tailnet)"
      # shellcheck disable=SC2086
      k3s server $ARGS > /var/log/k3s-drill.log 2>&1 &
      K=$!

      for i in $(seq 1 90); do
        k3s kubectl get --raw /readyz >/dev/null 2>&1 && break
        sleep 4
      done
      if ! k3s kubectl get --raw /readyz >/dev/null 2>&1; then
        say "FAIL: API never became ready"; tail -25 /var/log/k3s-drill.log | sed 's/^/DRILL| k3s: /'
        poweroff -f
      fi
      say "API READY — the restored datastore booted"

      say "--- objects recovered from the restored datastore ---"
      for k in namespaces secrets configmaps deployments; do
        say "  $k: $(k3s kubectl get $k -A --no-headers 2>/dev/null | wc -l)"
      done
      # ================= ENCRYPTED-ERA CHECKS (added 2026-08-06) =================
      # The backup contains state.db + token ONLY. It does NOT contain
      # encryption-config.json. The claim under test is that k3s rehydrates that
      # config from the EncryptionConfig embedded in the token-encrypted bootstrap
      # record inside the datastore. If that claim is false, every backup taken
      # after P1B is unrestorable -- so this is the single most important assertion
      # in the drill.
      say "--- ENCRYPTION: was encryption-config.json REGENERATED? (it is not in the backup) ---"
      EC="$D/server/cred/encryption-config.json"
      if [ -f "$EC" ]; then
        say "  PRESENT -> rehydrated from the bootstrap record. Backups ARE restorable."
        say "  providers: $(grep -oE '\"(aescbc|identity|secretbox|kms)\"' "$EC" | tr -d '\"' | tr '\n' ' ')"
      else
        say "  *** ABSENT -> POST-P1B BACKUPS ARE NOT RESTORABLE. DRILL FAILED. ***"
      fi
      say "  secrets-encrypt status: $(k3s secrets-encrypt status 2>&1 | head -1)"

      say "--- are the restored rows actually ciphertext on disk? ---"
      cp "$D/server/db/state.db" /tmp/chk.db 2>/dev/null
      say "  $(sqlite3 /tmp/chk.db "SELECT SUM(CASE WHEN substr(CAST(value AS TEXT),1,8)='k8s:enc:' THEN 1 ELSE 0 END)||' encrypted / '||COUNT(*)||' total' FROM kine k WHERE k.name LIKE '/registry/secrets/%' AND k.deleted=0 AND k.id=(SELECT MAX(id) FROM kine k2 WHERE k2.name=k.name);" 2>/dev/null)"
      rm -f /tmp/chk.db

      say "--- CA identity (proves the restored CA, not a fresh one) ---"
      say "  $(openssl x509 -in $D/server/tls/server-ca.crt -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 | cut -c1-32)"
      # This is the DECRYPTION proof. Reading a matching content hash back through
      # the API means the restored server decrypted ciphertext with a key it
      # recovered from the bootstrap record -- presence alone would prove nothing.
      say "--- preselected secret CONTENT, not just presence (= decryption proof) ---"
      for S in cert-manager/cloudflare-api-token home/mosquitto-auth home/zigbee2mqtt-config; do
        NS=''${S%%/*}; NM=''${S##*/}
        V=$(k3s kubectl -n "$NS" get secret "$NM" -o jsonpath='{.data}' 2>/dev/null | sha256sum | cut -c1-32)
        say "  $S data sha256[0:32]: ''${V:-UNREADABLE}"
      done
      say "--- a real workload object ---"
      k3s kubectl -n home get deploy -o name 2>/dev/null | sed 's/^/DRILL|   /'
      say "=================== RESTORE DRILL END ==================="
      kill $K 2>/dev/null || true
      sleep 3
      poweroff -f
    '';
  };
}
