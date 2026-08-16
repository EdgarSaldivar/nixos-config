# minas-tirith — in-cluster Jobs

Salvaged from K3S-HANDOFF.md on 2026-08-16. Cited from `pelargir/secrets.nix`.

## ⛔ `--request-timeout` SILENTLY DISABLES kubectl's in-cluster config

Cost an hour on the jellyfin quiesce job, and it fails in a way that points nowhere near
the cause:

```
The connection to the server localhost:8080 was refused - did you specify the right host or port?
```

…with the ServiceAccount token mounted, `KUBERNETES_SERVICE_HOST=10.43.0.1` set, and
`kubectl auth can-i` confirming the RBAC. kubectl only falls back to **in-cluster**
configuration when the merged kubeconfig **equals the defaults**. Any global flag that
shapes the client config — `--request-timeout` among them — makes the merged config
differ, so kubectl returns *that* config, pointing at `localhost:8080`, and never reads
the token at all.

Bisected: the identical command without the flag prints `Using in-cluster configuration`
and succeeds. Bound an in-cluster job with the **Job's `activeDeadlineSeconds`** instead
— it is also the bound that matters, since it terminates the pod and releases
`concurrencyPolicy: Forbid`.
