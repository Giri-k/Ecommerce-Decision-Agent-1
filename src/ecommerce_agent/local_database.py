"""Project-local PostgreSQL process management. Never prints credentials."""

import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo


def private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    path.chmod(0o600)


def binaries():
    from embedded_postgres._commands import POSTGRES_BIN_PATH

    return Path(POSTGRES_BIN_PATH)


def start_database(root=Path("var/local-db"), port=54329):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    settings_path = root / "bootstrap.json"
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
    else:
        settings = {"port": port, "password": secrets.token_urlsafe(32)}
        private_json(settings_path, settings)
    port = settings["port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid local PostgreSQL port")
    data = root / "pgdata"
    bin_dir = binaries()
    if not (data / "PG_VERSION").exists():
        password_file = root / "init-password"
        password_file.write_text(settings["password"] + "\n")
        password_file.chmod(0o600)
        try:
            subprocess.run(
                [
                    str(bin_dir / "initdb"),
                    "-D",
                    str(data),
                    "-U",
                    "postgres",
                    "--encoding=UTF8",
                    "--locale=C",
                    "--auth-host=scram-sha-256",
                    "--auth-local=trust",
                    "--pwfile",
                    str(password_file),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=45,
            )
        finally:
            password_file.unlink(missing_ok=True)
        (data / "pg_hba.conf").write_text(
            "local all postgres trust\nlocal all all scram-sha-256\n"
            "host all all 127.0.0.1/32 scram-sha-256\n"
        )
    status = subprocess.run(
        [str(bin_dir / "pg_ctl"), "-D", str(data), "status"], capture_output=True
    )
    if status.returncode != 0:
        socket_dir = Path("/tmp") / ("ec-pg-" + hashlib.sha256(str(root).encode()).hexdigest()[:16])
        socket_dir.mkdir(mode=0o700, exist_ok=True)
        socket_dir.chmod(0o700)
        subprocess.run(
            [
                str(bin_dir / "pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(root / "postgres.log"),
                "-o",
                f"-h 127.0.0.1 -p {port} -k {socket_dir}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
    dsn = make_conninfo(
        host="127.0.0.1",
        port=port,
        dbname="postgres",
        user="postgres",
        password=settings["password"],
        connect_timeout=3,
    )
    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT 1").fetchone()
    return dsn


def stop_database(root=Path("var/local-db")):
    data = root.resolve() / "pgdata"
    if (data / "postmaster.pid").exists():
        subprocess.run(
            [str(binaries() / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
