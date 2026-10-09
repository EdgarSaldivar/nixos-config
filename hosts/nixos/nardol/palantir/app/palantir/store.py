"""Palantír's index: what has been learned about each video, computed once.

Every expensive pass (scenes, faces, transcript, search embeddings) writes here
and is reused by every later question about the same video. Keyed by a content
hash, so the same file under two names is one video.
"""
import os
import sqlite3
import threading
import time

import numpy as np

DATA = os.environ.get("PALANTIR_DATA", "/data")
DB = os.path.join(DATA, "palantir.sqlite")

_local = threading.local()

SCHEMA = """
create table if not exists videos(
  id text primary key, path text not null, name text, duration real,
  width int, height int, fps real, added real);
create table if not exists stages(
  video text, stage text, done real, primary key(video, stage));
create table if not exists scenes(video text, idx int, start real, end real);
create table if not exists faces(
  video text, t real, x1 real, y1 real, x2 real, y2 real, score real, emb blob);
create table if not exists transcript(video text, start real, end real, text text);
create table if not exists frames(video text, t real, emb blob);
create table if not exists people(name text, emb blob, source text, added real);
create index if not exists faces_v on faces(video);
create index if not exists frames_v on frames(video);
"""


def db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DATA, exist_ok=True)
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("pragma journal_mode=wal")
        conn.executescript(SCHEMA)
        _local.conn = conn
    return conn


def vec(blob):
    return np.frombuffer(blob, dtype=np.float32)


def blob(v):
    return np.asarray(v, dtype=np.float32).tobytes()


def stage_done(video, stage):
    return db().execute("select 1 from stages where video=? and stage=?", (video, stage)).fetchone() is not None


def mark_done(video, stage):
    with db() as c:
        c.execute("insert or replace into stages values(?,?,?)", (video, stage, time.time()))


def video(vid):
    row = db().execute(
        "select id, path, name, duration, width, height, fps from videos where id=?", (vid,)
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "path", "name", "duration", "width", "height", "fps")
    return dict(zip(keys, row))


def videos():
    rows = db().execute("select id, name, duration from videos order by added desc").fetchall()
    return [{"id": r[0], "name": r[1], "duration": r[2]} for r in rows]
