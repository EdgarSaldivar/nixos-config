# The Cloudflare source ranges must be declared in exactly ONE place.
#
# WHAT THIS USED TO CHECK, AND WHY IT CHANGED
#
# Until 2026-08-16 the ranges were declared twice -- in pelargir's nftables rules
# and again as a traefik `ipAllowList` Middleware in manifests/ingress.yaml -- and
# this check asserted the two lists agreed. That was the right check for as long as
# both copies existed, because Cloudflare adds ranges and both were refreshed by hand.
#
# The second copy turned out to be worse than duplication: the `cloudflare-only`
# Middleware **could never work here**. The UniFi router SNATs forwarded traffic, so
# Traefik sees the router's address rather than Cloudflare's edge IP and no range
# ever matches -- verified 2026-08-05, when legitimate Cloudflare requests were
# rejected 403 and the same request without the ACL returned 200. mTLS replaced it.
#
# It was therefore a live, referenced-by-nothing object named `cloudflare-only` that
# would silently 403 Home Assistant the moment anyone pointed a route at it believing
# it was the origin protection. It was deleted, which collapsed the list to one
# source and completed ROADMAP item 7.
#
# So this check inverts: instead of "the two copies agree", it now asserts "there is
# still only one copy". A second copy reappearing is the regression to catch, and it
# is the same failure this repository keeps meeting -- two hand-maintained lists that
# drift.
{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  # The single source of truth: what the host firewall actually enforces. Read the
  # RENDERED rules rather than re-importing a Nix list -- if the list ever stops
  # reaching the rules, that is itself the bug worth catching.
  #
  # ⚠️ extraForwardRules, not extraInputRules. The 80/443 Cloudflare gate is on the
  # forward chain because those ports are forwarded to the cluster rather than
  # terminated on the host. Reading the input chain finds nothing, and without the
  # vacuity guard below would make this check pass while comparing an empty set.
  allRules = nixosConfigurations.pelargir.config.networking.firewall.extraForwardRules;

  # ⚠️ Narrow to the CLOUDFLARE rules before extracting anything. `extraForwardRules`
  # holds every forward rule on the host, and a first version of this check happily
  # demanded that 10.0.1.0/24 and 10.42.0.0/16 -- the LAN and the k8s pod CIDR --
  # each appear in exactly one file. Those legitimately appear in several, so the
  # check failed on things it was never about.
  #
  # The Cloudflare rules are the ones with a brace-enclosed address SET:
  #
  #   iifname "eth0" ip  saddr { <15 v4 ranges> } tcp dport { 80, 443 } accept
  #   iifname "eth0" ip6 saddr { <7 v6 ranges>  } tcp dport { 80, 443 } accept
  #
  # while the LAN rule is a bare `saddr 10.0.0.0/24` with no braces, and the final
  # rule is an unconditional drop. Matching on "saddr {" therefore selects exactly
  # the two Cloudflare lines -- and the guard below fails if it ever selects a
  # different number of them.
  cfRuleLines = lib.filter (l: lib.hasInfix "saddr {" l) (lib.splitString "\n" allRules);
  rules = lib.concatStringsSep " " cfRuleLines;

  # ⚠️ Extract CIDRs by POSITIVE match on whole tokens, never by splitting on
  # "non-address characters". The latter is tempting and wrong: a-f are hex digits,
  # so a word like `accept` shreds into fragments that look like address parts. That
  # exact bug once reported `only in firewall: ://de, ://c, ://e`.
  tokensOf =
    text:
    lib.splitString " " (
      builtins.replaceStrings [ "," "{" "}" "\n" "\t" ] [ " " " " " " " " " " ] text
    );

  v4Re = "([0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+/[0-9]+)";
  v6Re = "([0-9a-fA-F]*:[0-9a-fA-F:]*/[0-9]+)";

  cidrsIn =
    text: re:
    lib.unique (
      lib.filter (t: t != null) (
        map (
          t:
          let
            m = builtins.match re t;
          in
          if m == null then null else lib.head m
        ) (tokensOf text)
      )
    );

  v4 = cidrsIn rules v4Re;
  v6 = cidrsIn rules v6Re;
  all = v4 ++ v6;

  # ⛔ Police the WHOLE repository, not just hosts/.
  #
  # Scanning only hosts/ was the first version, and cross-review immediately found a
  # fourth copy of the full v4 list sitting in experiments/traefik-canary/. "Declared
  # once" has to mean once in the repository, or the check merely relocates the
  # duplication somewhere it does not look.
  repoRoot = ../.;

  # The one file allowed to contain them. Everything else must DERIVE from it:
  # wireguard.nix imports it for the nftables gate, and manifests.nix substitutes it
  # into minas' traefik `forwardedHeaders.trustedIPs`.
  soleSource = "hosts/nixos/pelargir/cloudflare-ranges.nix";
in
pkgs.runCommand "cloudflare-ranges"
  {
    inherit repoRoot soleSource;
    canarySource = ../experiments/traefik-canary/traefik-canary.yaml;
    ranges = lib.concatStringsSep " " all;
    traefikSource = ../hosts/nixos/minas-tirith/manifests/traefik.yaml;
    # ⛔ Import the PRODUCTION generator, never re-implement it. An earlier version
    # of this check did its own replaceStrings, which meant breaking only the real
    # substitution in manifests.nix shipped the placeholder to the live ingress while
    # this check happily verified its own private rendering. Cross-review caught that;
    # the two are now one expression, so there is nothing to drift.
    renderedTraefik =
      (import ../hosts/nixos/pelargir/minas-traefik-manifest.nix { inherit lib pkgs; }).minasTraefik;
    renderedCanary =
      (import ../hosts/nixos/pelargir/minas-traefik-manifest.nix { inherit lib pkgs; }).canary;
    cfRuleCount = toString (lib.length cfRuleLines);
    v4count = toString (lib.length v4);
    v6count = toString (lib.length v6);
  }
  ''
    fail=0

    # ---- VACUITY GUARDS -----------------------------------------------------
    # An empty or near-empty range set would make every assertion below trivially
    # true. Cloudflare publishes 15 v4 and 7 v6 ranges; require both families.
    if [ "$cfRuleCount" -ne 2 ]; then
      echo "VACUITY: matched $cfRuleCount 'saddr {' rules, expected exactly 2 (v4 + v6)." >&2
      echo "  The rule shape changed; re-read extraForwardRules before trusting this check." >&2
      fail=1
    fi
    if [ "$v4count" -lt 10 ]; then
      echo "VACUITY: only $v4count IPv4 ranges parsed from the firewall rules." >&2
      fail=1
    fi
    if [ "$v6count" -lt 5 ]; then
      echo "VACUITY: only $v6count IPv6 ranges parsed from the firewall rules." >&2
      echo "  (a v4-only parse is the historical failure mode here)" >&2
      fail=1
    fi

    # ---- THE SUBSTITUTION MUST ACTUALLY HAPPEN ------------------------------
    # Generating the manifest introduces a failure the literal list did not have:
    # if the placeholder name and the substitution ever disagree, the rendered
    # manifest ships `trustedIPs=@cloudflareTrustedIPsV4@` to the LIVE public
    # ingress. Nothing else catches it -- manifest-objects parses YAML structure and
    # is perfectly happy with a garbage flag VALUE, which was verified by breaking
    # the substitution on purpose and watching it pass.
    if grep -q '@cloudflareTrustedIPsV4@' "$renderedTraefik"; then
      echo "FAIL: the rendered traefik manifest still contains the placeholder." >&2
      echo "  The substitution in pelargir/manifests.nix did not fire, and traefik" >&2
      echo "  would be handed a literal '@cloudflareTrustedIPsV4@' as a CIDR list." >&2
      fail=1
    fi
    if ! grep -q 'trustedIPs=173\.245\.48\.0/20,' "$renderedTraefik"; then
      echo "FAIL: the rendered traefik manifest has no substituted range list." >&2
      fail=1
    fi
    # And the SOURCE must still carry a placeholder -- if someone pastes the literal
    # list back into the manifest the substitution becomes a silent no-op.
    # The canary is rendered by the same expression and applied BY HAND, so it gets
    # the same treatment: it must substitute, and must not carry a literal list.
    if grep -q '@cloudflareTrustedIPsV4@' "$renderedCanary"; then
      echo "FAIL: the rendered traefik CANARY still contains the placeholder." >&2
      fail=1
    fi
    if ! grep -q 'trustedIPs=' "$renderedCanary"; then
      echo "FAIL: the rendered canary has no trustedIPs flag at all." >&2
      fail=1
    fi

    if ! grep -q '@cloudflareTrustedIPsV4@' "$traefikSource"; then
      echo "FAIL: manifests/traefik.yaml no longer contains the placeholder." >&2
      echo "  A literal list there would make the generation a no-op and reintroduce" >&2
      echo "  the duplication this check exists to prevent." >&2
      fail=1
    fi

    # ---- SINGLE SOURCE ------------------------------------------------------
    # Every range must appear in exactly one file, and it must be the expected one.
    for cidr in $ranges; do
      # -F: a CIDR contains '.' and '/', which are regex-significant.
      hits=$(grep -rlF -- "$cidr" "$repoRoot" 2>/dev/null | sed "s|^$repoRoot/||" | sort -u)
      n=$(printf '%s\n' "$hits" | grep -c . || true)

      if [ "$n" -eq 0 ]; then
        echo "FAIL: $cidr is enforced by the firewall but appears in no file in the repository." >&2
        fail=1
      elif [ "$n" -gt 1 ]; then
        echo "FAIL: $cidr is declared in $n files -- the duplication this check exists to prevent:" >&2
        printf '%s\n' "$hits" | sed 's/^/         /' >&2
        fail=1
      elif [ "$hits" != "$soleSource" ]; then
        echo "FAIL: $cidr is declared in '$hits', expected '$soleSource'." >&2
        fail=1
      fi
    done

    [ "$fail" -eq 0 ] || {
      echo "" >&2
      echo "The Cloudflare ranges must stay in one place. If a second consumer genuinely" >&2
      echo "needs them, generate that consumer FROM this list rather than restating it." >&2
      exit 1
    }

    echo "cloudflare ranges: $v4count v4 + $v6count v6, single-sourced in $soleSource"
    touch $out
  ''
