# The Cloudflare source ranges are declared TWICE, and must agree.
#
#   hosts/nixos/pelargir/wireguard.nix        nftables, gates 80/443 on eth0
#   hosts/nixos/pelargir/manifests/ingress.yaml  traefik ipAllowList middleware
#
# They enforce the same policy at two layers — the host firewall and the ingress
# controller — so a range present in one and missing from the other is a silent
# asymmetry: traffic that the firewall admits and traefik rejects, or worse, the
# reverse. Cloudflare adds ranges, and the two lists are refreshed by hand.
#
# ⛔ THE DUPLICATION IS NOT REMOVED HERE, deliberately. Rendering the manifest
# from the Nix list would change how that manifest is delivered, and it is
# delivered into a k3s auto-deploy directory where the installed basename is
# frozen and a botched change reaches the live ingress. Making the two provably
# agree removes the actual risk — divergence — without touching the delivery path.
# Collapsing them to one source stays ROADMAP work, to be done with a window.
{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  # From the firewall side: read the rendered nftables FORWARD rules, which is
  # what the host actually enforces, rather than re-importing the Nix list. If the
  # list ever stops reaching the rules, that is itself the bug this should catch.
  #
  # ⚠️ extraForwardRules, not extraInputRules. The 80/443 Cloudflare gate is on the
  # forward chain because these ports are forwarded to the cluster, not terminated
  # on the host. Reading the input chain finds nothing and — without the vacuity
  # guard below — would have made this check pass while comparing an empty set.
  rules = nixosConfigurations.pelargir.config.networking.firewall.extraForwardRules;

  ingressYaml = builtins.readFile ../hosts/nixos/pelargir/manifests/ingress.yaml;

  # Extract CIDRs by POSITIVE match on tokens, not by splitting on "non-address
  # characters". The latter is tempting and wrong: a-f are hex digits, so a word
  # like `accept` shreds into fragments that look like address parts. That bug
  # produced "only in firewall: ://de, ://c, ://e" before this was rewritten.
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

  # v4 and v6 handled separately: an IPv6 range accidentally landing in the v4
  # allow-list is a real mistake and lumping them together would hide it.
  ingV4 = cidrsIn ingressYaml v4Re;
  ingV6 = cidrsIn ingressYaml v6Re;

  # Take the Cloudflare set from the ONE line that declares it, so unrelated
  # addresses elsewhere in the ruleset (the LAN accept, the site-A subnet path)
  # cannot contaminate the comparison.
  cfLine =
    pred:
    let
      matching = lib.filter pred (lib.splitString "\n" rules);
    in
    if matching == [ ] then "" else lib.head matching;

  cf4Line = cfLine (l: lib.hasInfix "ip saddr {" l && lib.hasInfix "80, 443" l);
  cf6Line = cfLine (l: lib.hasInfix "ip6 saddr {" l && lib.hasInfix "80, 443" l);

  cf4 = cidrsIn cf4Line v4Re;
  cf6 = cidrsIn cf6Line v6Re;

  missingFromIngress = lib.subtractLists ingV4 cf4;
  extraInIngress = lib.subtractLists cf4 ingV4;
  missingV6 = lib.subtractLists ingV6 cf6;
  extraV6 = lib.subtractLists cf6 ingV6;

  fmt = xs: lib.concatStringsSep ", " xs;
in
# Guard the vacuous pass: if either extraction stops finding ranges, every
# comparison below is trivially satisfied.
if builtins.length cf4 < 10 || builtins.length ingV4 < 10 then
  throw ''
    cloudflare-ranges extracted ${toString (builtins.length cf4)} firewall and
    ${toString (builtins.length ingV4)} manifest IPv4 ranges. Extraction is broken
    and this check would pass vacuously.
  ''
else if missingFromIngress != [ ] then
  throw ''
    Cloudflare ranges in pelargir's firewall are MISSING from the traefik
    ipAllowList in manifests/ingress.yaml:
      ${fmt missingFromIngress}
    The firewall would admit this traffic and traefik would reject it.
  ''
else if extraInIngress != [ ] then
  throw ''
    The traefik ipAllowList contains ranges the firewall does not admit:
      ${fmt extraInIngress}
    Those ranges cannot reach traefik, so the allow-list entry is misleading.
  ''
else if missingV6 != [ ] || extraV6 != [ ] then
  throw ''
    The IPv6 Cloudflare ranges disagree between the firewall and the ingress
    allow-list.
      only in firewall: ${fmt extraV6}
      only in manifest: ${fmt missingV6}
  ''
else
  pkgs.runCommand "cloudflare-ranges-ok" { } "touch $out"
