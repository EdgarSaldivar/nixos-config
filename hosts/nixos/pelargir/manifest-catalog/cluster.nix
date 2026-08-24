{ corednsCustom }:
# Cluster-scoped objects belong to no single host. They are grouped separately so
# the ownership map does not imply that e.g. a StorageClass is pelargir's, and so
# they are not mistakenly removed with a host's workloads.
[
  {
    name = "storage.yaml";
    path = ../manifests/storage.yaml;
  }
  # NOT "coredns.yaml" — that is k3s's own packaged filename. Reusing it would
  # collide in this directory, and if --disable=coredns is ever set the disable
  # matches by BASENAME permanently, turning the file into a delete-on-sight trap.
  {
    name = "coredns-ha.yaml";
    path = ../manifests/coredns-ha.yaml;
  }
  {
    name = "coredns-custom.yaml";
    path = corednsCustom;
  }
]
