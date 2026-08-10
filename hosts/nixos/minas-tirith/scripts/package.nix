{
  lib,
  python3,
  writeTextFile,
}:

writeTextFile {
  name = "ingress-acceptance";
  destination = "/bin/ingress-acceptance";
  executable = true;
  text =
    "#!${python3}/bin/python3\n"
    + lib.removePrefix "#!/usr/bin/env python3\n" (builtins.readFile ./ingress-acceptance.py);
  checkPhase = ''
    $out/bin/ingress-acceptance --selftest
  '';
}
