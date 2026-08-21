#!/usr/bin/env bash
# Negative mutation tests for the extracted checks.
#
# A pure move can silently turn a contract into a tautology: the check keeps its
# name, `nix flake check` stays green, and it no longer guards anything. Name
# equality and a green suite cannot detect that. This can.
#
# For each case: deliberately violate the invariant, then assert the check THROWS.
# A case that still evaluates successfully means the contract is dead.
#
# Usage: mutation-test.sh <worktree>
set -uo pipefail
# ⛔ `pwd -P`, not `pwd`. Nix refuses a flake path that traverses a symlink
# ("error: path '/tmp' is a symlink"), and on macOS /tmp IS a symlink to /private/tmp
# -- so the obvious invocation, a scratch worktree under /tmp, failed every mutation
# with an eval error. Combined with the swallowed stderr below, that presented as
# "contracts dead: 9": a broken harness reporting that nine real checks were vacuous.
TREE="$(cd "$1" && pwd -P)"
pass=0; fail=0

# $1 human name, $2 check file basename, $3 nix expr producing doctored `configs`
mutate() {
  local name="$1" check="$2" doctor="$3"
  local out
  out=$(nix eval --impure --expr "
    let
      f    = builtins.getFlake \"path:$TREE\";
      lib  = f.inputs.nixpkgs.lib;
      pkgs = f.inputs.nixpkgs.legacyPackages.\${builtins.currentSystem};
      configs = $doctor;
      chk = import $TREE/checks/$check.nix {
        inherit lib pkgs;
        nixosConfigurations = configs;
        darwinConfigurations = f.darwinConfigurations;
      };
    in (builtins.tryEval (builtins.seq chk.drvPath chk)).success
  " 2>/tmp/mutation-eval-err)
  # ⛔ Do NOT swallow stderr. A typo in the doctoring expression yields an EMPTY
  # $out, which the branch below reports as "check ACCEPTED it (result=)" -- a
  # broken harness presented as a dead contract, the most misleading possible
  # outcome for a tool whose whole job is telling you whether a check is real.
  if [ -z "$out" ]; then
    printf '  ⚠️  %-38s HARNESS ERROR (not a verdict):\n' "$name"
    sed 's/^/       /' /tmp/mutation-eval-err | head -4
    return 1
  fi
  if [ "$out" = "false" ]; then
    printf '  ✅ %-38s check REJECTED the mutation\n' "$name"; pass=$((pass+1))
  else
    printf '  ❌ %-38s check ACCEPTED it (result=%s) — CONTRACT IS DEAD\n' "$name" "${out:-eval-error}"; fail=$((fail+1))
  fi
}

echo "Negative mutation tests against $TREE"
echo

mutate "minas disko gains a second disk" minas-tirith-disko-targets \
 'f.nixosConfigurations // { minas-tirith = f.nixosConfigurations.minas-tirith.extendModules {
    modules = [ ({ lib, ... }: { disko.devices.disk.root.device = lib.mkForce "/dev/sdz"; }) ];
  }; }'

mutate "osgiliath disko target changes" osgiliath-disko-targets \
 'f.nixosConfigurations // { osgiliath = f.nixosConfigurations.osgiliath.extendModules {
    modules = [ ({ lib, ... }: { disko.devices.disk = lib.mkForce {}; }) ];
  }; }'

mutate "pelargir disko target changes" pelargir-disko-targets \
 'f.nixosConfigurations // { pelargir = f.nixosConfigurations.pelargir.extendModules {
    modules = [ ({ lib, ... }: { disko.devices.disk = lib.mkForce {}; }) ];
  }; }'

mutate "a hostname stops matching its output" hostnames \
 'f.nixosConfigurations // { pelargir = f.nixosConfigurations.pelargir.extendModules {
    modules = [ ({ lib, ... }: { networking.hostName = lib.mkForce "not-pelargir"; }) ];
  }; }'

mutate "a disk collector loses its endpoint" fleet-disk-health \
 'f.nixosConfigurations // { nardol = f.nixosConfigurations.nardol.extendModules {
    modules = [ ({ lib, ... }: { services.scrutiny.collector.settings.api.endpoint = lib.mkForce "http://evil:9080"; }) ];
  }; }'

mutate "nardol loses its Tang unlock" nardol-unlock-contract \
 'f.nixosConfigurations // { nardol = f.nixosConfigurations.nardol.extendModules {
    modules = [ ({ lib, ... }: { boot.initrd.clevisLuksAskpass.useTang = lib.mkForce false; }) ];
  }; }'

mutate "pelargir Tang opens to the world" pelargir-tang-contract \
 'f.nixosConfigurations // { pelargir = f.nixosConfigurations.pelargir.extendModules {
    modules = [ ({ lib, ... }: { services.tang.listenStream = lib.mkForce [ "0.0.0.0:1234" ]; }) ];
  }; }'

# ⛔ Mutate a secret the applier ACTUALLY READS, or this proves nothing.
#
# This used to strip restartUnits from `k3s_agent_token`, which the applier script
# never references -- the contract derives its scope from the paths the script
# interpolates, so a secret outside that scope is correctly ignored and the check
# passed. That read as "CONTRACT IS DEAD" when the contract was fine and the TEST was
# aimed at the wrong secret. Found 2026-08-21, once the harness stopped swallowing
# its own errors and could run at all.
mutate "a rotated secret stops restarting the applier" secret-applier-contract \
 'f.nixosConfigurations // { pelargir = f.nixosConfigurations.pelargir.extendModules {
    modules = [ ({ lib, ... }: { sops.secrets.pin_collector_database_url.restartUnits = lib.mkForce []; }) ];
  }; }'

mutate "nardol Wolf becomes privileged" nardol-gaming-contract \
 'f.nixosConfigurations // { nardol = f.nixosConfigurations.nardol.extendModules {
    modules = [ ({ lib, ... }: { virtualisation.oci-containers.containers.wolf.privileged = lib.mkForce true; }) ];
  }; }'

echo
echo "mutations rejected: $pass    contracts dead: $fail"
[ "$fail" -eq 0 ]
