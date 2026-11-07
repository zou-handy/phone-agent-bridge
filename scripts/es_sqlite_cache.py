#!/usr/bin/env python3
"""
es_sqlite_cache.py — Zero-RTT Directory Snapshot Indexer for Cloud Agent Sandboxes

Exports Windows NTFS MFT file entries via Voidtools Everything CLI (es.exe) into an
in-memory or local SQLite FTS5 database, eliminating thousands of serial POSIX
LOOKUP/GETATTR round-trips over remote WebSocket FUSE (go-fuse) mounts.
"""

import argparse
import os
import sqlite3
import subprocess
import time


def build_snapshot(root_path: str, db_path: str, max_count: int = 50000) -> int:
    t0 = time.perf_counter()
    cmd = ["es.exe", "-n", str(max_count), "-path", root_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"es.exe exited with code {proc.returncode}: {proc.stderr.strip()}")

    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode = WAL;")
    cur.execute("PRAGMA synchronous = NORMAL;")
    cur.execute("DROP TABLE IF EXISTS vfs_index;")
    cur.execute(
        "CREATE VIRTUAL TABLE vfs_index USING fts5(full_path, basename, ext, tokenize='unicode61');"
    )

    rows = []
    for p in lines:
        base = os.path.basename(p)
        _, ext = os.path.splitext(base)
        rows.append((p, base, ext.lstrip(".").lower()))

    cur.executemany("INSERT INTO vfs_index (full_path, basename, ext) VALUES (?, ?, ?);", rows)
    conn.commit()
    conn.close()

    elapsed_ms = (time.perf_counter() - t0) * 1000
    print(f"[es_sqlite_cache] Indexed {len(rows)} entries from {root_path} into {db_path} in {elapsed_ms:.1f} ms")
    return len(rows)


def query_snapshot(db_path: str, keyword: str, limit: int = 20):
    t0 = time.perf_counter()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT full_path FROM vfs_index WHERE vfs_index MATCH ? LIMIT ?;",
        (f'"{keyword}"', limit),
    )
    results = [r[0] for r in cur.fetchall()]
    conn.close()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    print(f"[es_sqlite_cache] Found {len(results)} matches in {elapsed_ms:.2f} ms:")
    for item in results:
        print(" ", item)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Everything (es.exe) to SQLite FTS5 Snapshot Cache")
    parser.add_argument("--root", default=".", help="Root directory to snapshot via es.exe")
    parser.add_argument("--db", default="vfs_snapshot.db", help="SQLite database file path")
    parser.add_argument("--max", type=int, default=50000, help="Max entries to export")
    parser.add_argument("--query", default="", help="Optional keyword to query immediately")
    args = parser.parse_args()

    if not os.path.exists(args.db) or args.root != ".":
        build_snapshot(os.path.abspath(args.root), args.db, args.max)
    if args.query:
        query_snapshot(args.db, args.query)
