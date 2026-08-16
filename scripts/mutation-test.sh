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
TREE="$(cd "$1" && pwd)"
pass=0; fail=0

# $1 human name, $2 check file basename, $3 nix expr producing doctored `configs`
mutate() {
  local name="$1" check="$2" doctor="$3"
  local out
  out=$(nix eval --impure --expr "
    let
      f    = builtins.getFlake \"path:$TREE\";
      lib  = f.inputs.nixpkgs.lib;
      pkgs = f.inputs.nixpkgs.legacyPackages.aarch64-darwin;
      configs = $doctor;
      chk = import $TREE/checks/$check.nix {
        inherit lib pkgs;
        nixosConfigurations = configs;
        darwinConfigurations = f.darwinConfigurations;
      };
    in (builtins.tryEval (builtins.seq chk.drvPath chk)).success
  " 2>/dev/null)
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

mutate "a rotated secret stops restarting the applier" secret-applier-contract \
 'f.nixosConfigurations // { pelargir = f.nixosConfigurations.pelargir.extendModules {
    modules = [ ({ lib, ... }: { sops.secrets.k3s_agent_token.restartUnits = lib.mkForce []; }) ];
  }; }'

mutate "nardol Wolf becomes privileged" nardol-gaming-contract \
 'f.nixosConfigurations // { nardol = f.nixosConfigurations.nardol.extendModules {
    modules = [ ({ lib, ... }: { virtualisation.oci-containers.containers.wolf.privileged = lib.mkForce true; }) ];
  }; }'

echo
echo "mutations rejected: $pass    contracts dead: $fail"
[ "$fail" -eq 0 ]
