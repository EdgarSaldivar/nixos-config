# Cloudflare's published edge ranges — the ONE place they are declared.
#
# Source: https://www.cloudflare.com/ips/ — refreshed 2026-08-03.
#
# WHY THIS FILE EXISTS
#
# These ranges had drifted into three copies: pelargir's nftables forward rules, a
# traefik `ipAllowList` Middleware in pelargir/manifests/ingress.yaml, and minas'
# `forwardedHeaders.trustedIPs` in manifests/traefik.yaml. Only the first two were
# ever compared, so the check that claimed "divergence is now impossible" was
# comparing two of three copies and could not see the third.
#
# The Middleware copy was deleted on 2026-08-16 — it could never work behind a
# SNATing router — and the traefik copy is now generated from this list, so the
# ranges are declared exactly once. `checks/cloudflare-ranges.nix` asserts that.
#
# ⛔ REFRESHING THEM: edit this file only. Both consumers derive from it, and the
# check fails the build if a literal range reappears anywhere else under hosts/.
#
# ⚠️ v6 IS DELIBERATELY NOT IN `trustedIPs` TODAY. minas' traefik trusts only the
# v4 ranges for forwarded headers, which is the behaviour that existed before this
# was single-sourced and is preserved verbatim. Whether it SHOULD trust the v6
# ranges is a real question — see ROADMAP item 7 — but it is a behaviour change,
# not part of collapsing the duplication, so it is not made here.
{
  v4 = [
    "173.245.48.0/20"
    "103.21.244.0/22"
    "103.22.200.0/22"
    "103.31.4.0/22"
    "141.101.64.0/18"
    "108.162.192.0/18"
    "190.93.240.0/20"
    "188.114.96.0/20"
    "197.234.240.0/22"
    "198.41.128.0/17"
    "162.158.0.0/15"
    "104.16.0.0/13"
    "104.24.0.0/14"
    "172.64.0.0/13"
    "131.0.72.0/22"
  ];
  v6 = [
    "2400:cb00::/32"
    "2606:4700::/32"
    "2803:f800::/32"
    "2405:b500::/32"
    "2405:8100::/32"
    "2a06:98c0::/29"
    "2c0f:f248::/32"
  ];
}
