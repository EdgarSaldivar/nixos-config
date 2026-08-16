# Module placement

- `modules/nixos/fleet/` — option-bearing capabilities with multiple active consumers, defining `fleet.*` options. Currently `disk-health.nix` and `k3s-node.nix`.
- `modules/nixos/profiles/` — explicitly imported policy bundles applied broadly, with no option surface. Currently `base.nix`, applied to every host through `lib/mkHost.nix`.
- `modules/nixos/roles/` — coherent host-role bundles, honestly named for the role, validated on one host until a second consumer appears.
- Nothing OS-specific belongs directly under `modules/`; reserve `modules/nixos/`, and `modules/darwin/` or `modules/home/` if they are ever needed.

A module graduates from `roles/` to `fleet/` when a second host actually needs it, not in anticipation.
