{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  service = nixosConfigurations.minas-tirith.config.systemd.services.media-optimizer;
  settings = builtins.fromJSON (
    builtins.unsafeDiscardStringContext service.environment.MEDIA_OPTIMIZER_POLICY_JSON
  );
  source = ../pkgs/media-optimizer;
  contract =
    settings.concurrency == 5
    && settings.verification_concurrency == 1
    && settings.original_retention_days == 0
    && settings.deluge_label == "media-optimizer"
    && !(settings ? daily_bytes)
    && !(settings ? speed_limit)
    && !(settings ? free_floor_bytes)
    && service.serviceConfig.User == "edgar"
    && service.serviceConfig.Restart == "on-failure"
    && lib.elem "multi-user.target" service.wantedBy;
in
if !contract then
  throw "media optimizer unit contract changed"
else
  pkgs.runCommand "media-optimizer-tests" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    cp -r ${source} "$TMPDIR/media-optimizer"
    chmod -R u+w "$TMPDIR/media-optimizer"
    cd "$TMPDIR/media-optimizer"
    export PYTHONPATH="$PWD/src"
    export PYTHONDONTWRITEBYTECODE=1
    python -m unittest discover -s tests -v
    touch "$out"
  ''
