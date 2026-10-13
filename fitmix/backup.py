"""Offline wardrobe backups. This module never imports or starts the application."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path


IMAGE_DIRECTORIES = ("originals", "garment-sources", "garments", "person-images", "tryons")
BACKUP_FORMAT = 1


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _check_database(path: Path) -> None:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise ValueError("数据库完整性检查失败，备份没有完成")


def create_backup(data_dir: Path, backup_root: Path | None = None) -> Path:
    """Copy SQLite (including committed WAL data) and images; stop FitMix first."""
    data_dir = Path(data_dir).resolve()
    database = data_dir / "wardrobe.db"
    if not database.is_file() or database.is_symlink():
        raise ValueError("没有找到衣橱数据库")
    backup_root = Path(backup_root or data_dir.parent / "backups").resolve()
    if backup_root.is_relative_to(data_dir):
        raise ValueError("备份目录必须位于衣橱数据目录之外")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    destination = backup_root / f"fitmix-{token}"
    temporary = backup_root / f".pending-{token}"
    temporary.mkdir(mode=0o700)
    try:
        # A separate writer lock prevents database changes while copying. Files
        # may be written before their DB transaction, so full backups are offline.
        with closing(sqlite3.connect(database, timeout=5)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(temporary / "wardrobe.db")) as target:
                    source.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE")
            for category in IMAGE_DIRECTORIES:
                folder = data_dir / category
                if not folder.exists():
                    continue
                if folder.is_symlink():
                    raise ValueError("备份不接受链接到其他目录的图片文件夹")
                for path in folder.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("备份不接受链接到其他位置的图片文件")
                    if path.is_file():
                        copied = temporary / path.relative_to(data_dir)
                        copied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        shutil.copy2(path, copied)
            lock.rollback()
        _check_database(temporary / "wardrobe.db")
        files = {}
        for path in sorted(temporary.rglob("*")):
            if path.is_file():
                path.chmod(0o600)
                files[path.relative_to(temporary).as_posix()] = {
                    "size": path.stat().st_size, "sha256": _digest(path),
                }
        manifest = {"format": BACKUP_FORMAT, "files": files}
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (temporary / "manifest.json").chmod(0o600)
        os.rename(temporary, destination)
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_backup(backup_dir: Path) -> dict:
    backup_dir = Path(backup_dir).resolve()
    try:
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != BACKUP_FORMAT or not isinstance(manifest.get("files"), dict):
            raise ValueError("不支持的备份格式")
        if "wardrobe.db" not in manifest["files"]:
            raise ValueError("备份缺少数据库")
        for filename, expected in manifest["files"].items():
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError("备份包含无效文件路径")
            if filename != "wardrobe.db" and relative.parts[0] not in IMAGE_DIRECTORIES:
                raise ValueError("备份包含非衣橱文件")
            path = backup_dir / relative
            if not path.resolve().is_relative_to(backup_dir) or path.is_symlink() or not path.is_file():
                raise ValueError(f"备份文件丢失或路径无效：{filename}")
            if path.stat().st_size != expected["size"] or _digest(path) != expected["sha256"]:
                raise ValueError(f"备份文件校验失败：{filename}")
        _check_database(backup_dir / "wardrobe.db")
        return manifest
    except (OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError, sqlite3.Error) as exc:
        raise ValueError("备份不完整或格式不正确") from exc


def restore_backup(backup_dir: Path, destination: Path) -> Path:
    """Restore into a new directory, leaving both current data and backup intact."""
    backup_dir = Path(backup_dir).resolve()
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("恢复目标必须是尚不存在的新目录，避免覆盖现有衣橱")
    if destination.resolve().is_relative_to(backup_dir):
        raise ValueError("恢复目录不能位于备份内部")
    manifest = verify_backup(backup_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.restore-{uuid.uuid4().hex}")
    temporary.mkdir(mode=0o700)
    try:
        for filename in manifest["files"]:
            target = temporary / filename
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(backup_dir / filename, target)
        for category in (*IMAGE_DIRECTORIES, "thumbnails"):
            (temporary / category).mkdir(exist_ok=True, mode=0o700)
        # Recheck copied files as well, detecting a changed backup during restore.
        for filename, expected in manifest["files"].items():
            if _digest(temporary / filename) != expected["sha256"]:
                raise ValueError("恢复过程中备份发生变化，恢复已停止")
        _check_database(temporary / "wardrobe.db")
        if destination.exists():
            raise ValueError("恢复目标已存在，恢复已停止")
        os.rename(temporary, destination)
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="FitMix 备份工具；创建和恢复前请先退出 FitMix")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="备份数据库和图片，不包含 API Key")
    create.add_argument("--data-dir", type=Path, required=True)
    create.add_argument("--backup-root", type=Path)
    verify = commands.add_parser("verify", help="检查备份是否完整")
    verify.add_argument("backup_dir", type=Path)
    restore = commands.add_parser("restore", help="恢复到新目录，不覆盖原数据")
    restore.add_argument("backup_dir", type=Path)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            print(f"备份完成：{create_backup(args.data_dir, args.backup_root)}")
        elif args.command == "verify":
            print(f"备份完整，共 {len(verify_backup(args.backup_dir)['files'])} 个文件")
        else:
            print(f"恢复完成：{restore_backup(args.backup_dir, args.destination)}")
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"操作未完成：{exc}\n")


if __name__ == "__main__":
    main()
