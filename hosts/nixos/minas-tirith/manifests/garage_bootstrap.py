"""Bring PinCollector's single Garage node to its declared state. Idempotent.

Runs in the API image (stdlib + boto3) as the garage-bootstrap Job:
  1. wait for the admin API (reachable before the node is healthy);
  2. give the node its layout role if it has none, then wait for /health;
  3. import the app and backup keys, or prove existing ones match the declared secrets;
  4. create the bucket if absent; app = read+write, backup = read only, neither owner;
  5. prove it through S3: app can write and delete, backup can list and read but not write.

Credentials are read from mounted files and never printed.
"""

import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

ADMIN = os.environ.get("GARAGE_ADMIN_URL", "http://garage-admin.pin-collector.svc:3903")
S3 = os.environ.get("GARAGE_S3_URL", "http://garage.pin-collector.svc:3900")
BUCKET = "pin-collector-uploads"
ZONE = "minas"
CAPACITY = int(os.environ["GARAGE_CAPACITY_BYTES"])
SECRETS = os.environ.get("GARAGE_BOOTSTRAP_SECRETS", "/run/secrets/pin-collector")
# A fresh 128-bit name per run, checked absent first: the probe never touches a real object.
PROBE = f".garage-bootstrap/probe-{secrets.token_hex(16)}"


def secret(name: str) -> str:
    with open(os.path.join(SECRETS, name), encoding="utf-8") as handle:
        return handle.read().strip()


TOKEN = secret("garage-admin-token")


def fail(message: str) -> None:
    print(f"garage bootstrap: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def say(message: str) -> None:
    print(f"garage bootstrap: {message}", file=sys.stderr, flush=True)


def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        ADMIN + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        return error.code, payload


def error_code(payload: object) -> str:
    return str(payload.get("code", "")) if isinstance(payload, dict) else ""


def must(result: tuple[int, object], what: str) -> dict:
    status, payload = result
    if status != 200:
        # Only the status and Garage's error code: a body could echo request fields.
        fail(f"{what}: HTTP {status} {error_code(payload)}")
    return payload  # type: ignore[return-value]


def wait(what: str, ready, attempts: int = 90, delay: float = 2.0) -> None:
    for _ in range(attempts):
        try:
            if ready():
                return
        except (OSError, BotoCoreError, ClientError):
            pass
        time.sleep(delay)
    fail(f"timed out waiting for {what}")


def health() -> bool:
    try:
        with urllib.request.urlopen(ADMIN + "/health", timeout=5) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        return False


def s3(key_id: str, key_secret: str):
    return boto3.client(
        "s3",
        endpoint_url=S3,
        aws_access_key_id=key_id,
        aws_secret_access_key=key_secret,
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            retries={"total_max_attempts": 3, "mode": "standard"},
        ),
    )


def denied(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in {"AccessDenied", "Forbidden", "403"}


def ensure_layout() -> None:
    wait("the admin API", lambda: call("GET", "/v2/GetClusterStatus")[0] == 200)
    status = must(call("GET", "/v2/GetClusterStatus"), "GetClusterStatus")
    nodes = status["nodes"]
    if len(nodes) != 1:
        fail(f"expected exactly one Garage node, found {len(nodes)}")
    node = nodes[0]
    if node.get("role"):
        say(f"layout already assigned (version {status['layoutVersion']})")
    else:
        layout = must(call("GET", "/v2/GetClusterLayout"), "GetClusterLayout")
        must(
            call(
                "POST",
                "/v2/UpdateClusterLayout",
                {"roles": [{"id": node["id"], "zone": ZONE, "capacity": CAPACITY, "tags": []}]},
            ),
            "UpdateClusterLayout",
        )
        must(
            call("POST", "/v2/ApplyClusterLayout", {"version": layout["version"] + 1}),
            "ApplyClusterLayout",
        )
        say(f"layout applied (version {layout['version'] + 1})")
    wait("/health", health)


def ensure_key(name: str, id_file: str, secret_file: str) -> tuple[str, str]:
    key_id, key_secret = secret(id_file), secret(secret_file)
    query = urllib.parse.urlencode({"id": key_id})
    status, payload = call("GET", f"/v2/GetKeyInfo?{query}&showSecretKey=true")
    if status == 200:
        # Before any grant: an existing key must hold exactly the declared secret.
        stored = payload.get("secretAccessKey") if isinstance(payload, dict) else None
        if not (isinstance(stored, str) and hmac.compare_digest(stored, key_secret)):
            fail(f"key {name} exists with a different secret than declared; not granting it anything")
        say(f"key {name} exists and matches its declared secret")
    elif status == 404 or error_code(payload) == "NoSuchAccessKey":
        must(
            call(
                "POST",
                "/v2/ImportKey",
                {"accessKeyId": key_id, "secretAccessKey": key_secret, "name": name},
            ),
            f"ImportKey {name}",
        )
        say(f"key {name} imported")
    else:
        fail(f"GetKeyInfo {name}: HTTP {status} {error_code(payload)}")
    return key_id, key_secret


def ensure_bucket() -> str:
    query = urllib.parse.urlencode({"globalAlias": BUCKET})
    status, payload = call("GET", f"/v2/GetBucketInfo?{query}")
    if status == 200:
        return payload["id"]  # type: ignore[index]
    if status == 404 or error_code(payload) == "NoSuchBucket":
        created = must(call("POST", "/v2/CreateBucket", {"globalAlias": BUCKET}), "CreateBucket")
        say(f"bucket {BUCKET} created")
        return created["id"]
    fail(f"GetBucketInfo: HTTP {status} {error_code(payload)}")
    raise AssertionError


def set_permissions(bucket_id: str, key_id: str, name: str, *, write: bool) -> None:
    allow = {"read": True, "write": write, "owner": False}
    deny = {"read": False, "write": not write, "owner": True}
    must(
        call("POST", "/v2/AllowBucketKey", {"bucketId": bucket_id, "accessKeyId": key_id, "permissions": allow}),
        f"AllowBucketKey {name}",
    )
    must(
        call("POST", "/v2/DenyBucketKey", {"bucketId": bucket_id, "accessKeyId": key_id, "permissions": deny}),
        f"DenyBucketKey {name}",
    )
    info = must(call("GET", f"/v2/GetKeyInfo?{urllib.parse.urlencode({'id': key_id})}"), f"GetKeyInfo {name}")
    granted = next((b["permissions"] for b in info["buckets"] if b["id"] == bucket_id), None)
    expected = {"read": True, "write": write, "owner": False}
    if granted is None or {k: bool(granted.get(k)) for k in expected} != expected:
        fail(f"key {name} has {granted} on {BUCKET}, expected {expected}")


def prove(app: tuple[str, str], backup: tuple[str, str]) -> None:
    app_s3, backup_s3 = s3(*app), s3(*backup)
    def reachable() -> bool:
        # Retry only while S3 is unreachable; a refusal is an answer, and a final one.
        try:
            app_s3.head_bucket(Bucket=BUCKET)
        except ClientError as error:
            fail(f"app key refused by S3 (declared secret mismatch?): {error.response['Error'].get('Code')}")
        return True

    wait("the S3 API", reachable)
    try:
        app_s3.head_object(Bucket=BUCKET, Key=PROBE)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
            fail(f"probe key check failed: {error.response['Error'].get('Code')}")
    else:
        fail("probe key unexpectedly exists; refusing to overwrite it")
    body = secrets.token_bytes(32)
    try:
        app_s3.put_object(Bucket=BUCKET, Key=PROBE, Body=body)
    except ClientError as error:
        fail(f"app key cannot write (declared secret mismatch or permissions): {error.response['Error'].get('Code')}")
    try:
        for name, client in (("app", app_s3), ("backup", backup_s3)):
            try:
                client.list_objects_v2(Bucket=BUCKET, Prefix=PROBE, MaxKeys=1)
                read = client.get_object(Bucket=BUCKET, Key=PROBE)["Body"].read()
            except ClientError as error:
                fail(f"{name} key cannot list/read: {error.response['Error'].get('Code')}")
            if read != body:
                fail(f"{name} key read back different bytes")
        try:
            backup_s3.put_object(Bucket=BUCKET, Key=PROBE, Body=b"overwrite")
        except ClientError as error:
            if not denied(error):
                fail(f"backup key write probe failed unexpectedly: {error.response['Error'].get('Code')}")
        else:
            fail("backup key could write; it must be read only")
        try:
            backup_s3.delete_object(Bucket=BUCKET, Key=PROBE)
        except ClientError as error:
            if not denied(error):
                fail(f"backup key delete probe failed unexpectedly: {error.response['Error'].get('Code')}")
        else:
            fail("backup key could delete; it must be read only")
    finally:
        app_s3.delete_object(Bucket=BUCKET, Key=PROBE)
    say("app key read/write/delete, backup key list/read only: proven through S3")


def main() -> None:
    ensure_layout()
    app = ensure_key("pin-collector-api", "garage-app-key-id", "garage-app-secret")
    backup = ensure_key("pin-collector-backup", "garage-backup-key-id", "garage-backup-secret")
    bucket_id = ensure_bucket()
    set_permissions(bucket_id, app[0], "pin-collector-api", write=True)
    set_permissions(bucket_id, backup[0], "pin-collector-backup", write=False)
    prove(app, backup)


if __name__ == "__main__":
    main()
