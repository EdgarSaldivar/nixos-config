{
  authentikCandidateRoutes,
  authentikGateEnabled,
  authentikGateFile,
  authentikRollout,
  legacyBasicAuthFallbackRoutes,
  lib,
  managedNames,
  rendered,
  routes,
}:
let
  # The traefik Pod bind-mounts this mutable file-provider directory. It is not a
  # store path: activation installs generated files here on every rebuild and the
  # store is read-only. Moving it requires the consumer manifest to change in the
  # same commit. The leaf is owned by edgar:users; its system parents remain root.
  routeDir = "/usr/local/etc/traefik";
in
{
  assertions = [
    {
      assertion = lib.all (
        name: builtins.elem name authentikCandidateRoutes
      ) authentikRollout.protectedRoutes;
      message = "authentikRollout.protectedRoutes contains a non-admin or unknown route";
    }
    {
      assertion = lib.all (name: builtins.hasAttr name routes) authentikRollout.protectedRoutes;
      message = "authentikRollout.protectedRoutes names a route that does not exist";
    }
    {
      assertion = lib.all (
        name: builtins.elem name authentikCandidateRoutes && builtins.hasAttr name routes
      ) legacyBasicAuthFallbackRoutes;
      message = "legacyBasicAuthFallbackRoutes contains a non-admin or unknown route";
    }
    {
      assertion = !authentikGateEnabled || authentikRollout.publish;
      message = "Authentik ForwardAuth cannot be enabled before auth.saldivar.io is published";
    }
  ];

  system.activationScripts.minas-traefik-routes = lib.stringAfter [ "users" ] ''
    # ⛔ CREATE IT. Do not require it to pre-exist.
    #
    # This used to FATAL when the directory was absent, and that was right while
    # routeDir pointed into an external git checkout a replacement host would not
    # have: creating it there would have produced an EMPTY provider directory, and
    # traefik would have served nothing while activation reported success.
    #
    # That reasoning did not survive the move to /usr/local/etc/traefik. This path is
    # minas' own and every file in it is GENERATED from this module, so a freshly
    # created directory is fully populated by the very next lines. There is no
    # empty-and-wrong state left to protect against.
    #
    # Keeping the FATAL instead made the configuration NOT SELF-SUFFICIENT: nothing
    # in this repository created the directory, so a rebuild-from-scratch of minas
    # aborted. Cross-review caught it on 2026-08-18; the live host only worked
    # because the directory had been created by hand during the relocation.
    # ⛔ Create the PARENTS root-owned, and only the leaf as edgar.
    #
    # `install -d -m 0755 -o edgar -g users ${routeDir}` applies that mode AND
    # ownership to every component it has to create. On a rebuilt-from-scratch minas
    # that means /usr/local and /usr/local/etc end up owned by edgar:users, which is
    # not what those directories should be and is not something anyone would notice
    # until it mattered.
    install -d -m 0755 -o root -g root /usr/local /usr/local/etc
    install -d -m 0755 -o edgar -g users ${routeDir}

    # The one case still worth refusing: something exists here that is NOT a
    # directory. The Pod mounts it `type: Directory`, so a stray file fails the mount
    # at runtime instead of here -- the worse place to discover it.
    if [ -e ${routeDir} ] && [ ! -d ${routeDir} ]; then
      echo "FATAL: ${routeDir} exists and is not a directory." >&2
      echo "       traefik's Pod mounts it with type: Directory and will not start." >&2
      exit 1
    fi

    if [ -d ${routeDir} ]; then
      ${lib.concatMapStringsSep "\n" (e: ''
        install -m 0644 -o edgar -g users ${e.file} ${routeDir}/k8s-${e.name}.yml
      '') rendered}
      install -m 0644 -o edgar -g users ${authentikGateFile} ${routeDir}/k8s-authentik-gate.yml

      # Report k8s-*.yml files that are no longer declared. Deliberately REPORT and
      # not delete: traefik would drop the route instantly, taking a live service
      # offline mid-activation, and a rebuild is not where an outage should originate.
      # Removing a service means removing it here AND deleting the file by hand, in
      # that order, having checked nothing still needs it.
      expected="${lib.concatStringsSep " " managedNames}"
      for f in ${routeDir}/k8s-*.yml; do
        [ -e "$f" ] || continue
        b=$(basename "$f")
        case " $expected " in
          *" $b "*) ;;
          *) echo "WARNING: stale traefik route $b is no longer declared in traefik-routes.nix." >&2
             echo "         traefik is STILL SERVING it. Delete it by hand once confirmed unused." >&2 ;;
        esac
      done
    else
      # Unreachable in practice: `install -d` above creates it, and the
      # not-a-directory case has already exited. Kept as a last belt, because a
      # silently-empty provider directory is the failure this whole block exists to
      # prevent -- a green rebuild that removes public ingress for 26 hostnames.
      echo "FATAL: ${routeDir} is still absent after install -d." >&2
      exit 1
    fi
  '';
}
