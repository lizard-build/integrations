"""Guest command and filesystem operations. Receives no host API credentials."""

import base64
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

ENV_FILE = "/tmp/lizard-tanstack-env.json"


def run(job):
    request = json.loads((job / "request.json").read_text())
    process = None

    def interrupted(signum, frame):
        raise InterruptedError("Command cancelled")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with (job / "stdout").open("wb") as stdout, (job / "stderr").open("wb") as stderr:
            inherited = json.loads(Path(ENV_FILE).read_text()) if Path(ENV_FILE).exists() else {}
            try:
                process = subprocess.Popen(
                    ["/bin/sh", "-c", request["command"]],
                    cwd=request["cwd"],
                    env={**os.environ, **inherited, **request["env"]},
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                code = process.wait(timeout=request["timeout"])
                return {"exitCode": code if code >= 0 else 128 - code}
            except (subprocess.TimeoutExpired, InterruptedError) as exc:
                return {"error": "timeout"} if isinstance(exc, subprocess.TimeoutExpired) else {"exitCode": 137}
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def dispatch(action, value):
    if action == "prepare":
        Path(value).mkdir(parents=True, exist_ok=True)
        return os.path.realpath(value)
    if action == "start":
        process = subprocess.Popen(
            [sys.executable, __file__, "run", value],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        return {"pid": process.pid}
    if action == "run":
        job = Path(value)
        try:
            result = run(job)
        except Exception:
            result = {"error": "worker"}
        temporary = job / "result.tmp"
        temporary.write_text(json.dumps(result))
        temporary.replace(job / "result.json")
        return None
    if action == "poll":
        req = json.loads(value)
        job = Path(req["job"])
        result_file = job / "result.json"
        result = json.loads(result_file.read_text()) if result_file.exists() else None
        response = {"result": result, "done": result is not None}
        for stream in ("stdout", "stderr"):
            path = job / stream
            data = b""
            if path.exists():
                with path.open("rb") as source:
                    source.seek(req[stream])
                    data = source.read(64 * 1024)
                if path.stat().st_size > req[stream] + len(data):
                    response["done"] = False
            response[stream] = base64.b64encode(data).decode()
        return response
    if action == "stop":
        try:
            os.kill(int(value), signal.SIGTERM)
        except ProcessLookupError:
            pass
        return None
    if action == "env":
        import fcntl
        with open(ENV_FILE + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = Path(ENV_FILE)
            env = json.loads(path.read_text()) if path.exists() else {}
            env.update(json.loads(value))
            path.write_text(json.dumps(env))
        return None
    req = json.loads(value)
    path = Path(req["path"])
    if action == "read_check":
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            raise IsADirectoryError(21, "Is a directory", str(path))
        if not stat.S_ISREG(info.st_mode):
            raise OSError(22, "Only regular files can be read", str(path))
        if info.st_size > 4 * 1024 * 1024:
            raise OSError(27, "File exceeds the 4 MiB read limit", str(path))
        return None
    if action == "list":
        return [{"name": p.name, "path": str(p), "type": "dir" if p.is_dir() else "file"}
                for p in path.iterdir()]
    if action == "mkdir":
        path.mkdir(parents=True, exist_ok=True)
    elif action == "remove":
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)
    elif action == "rename":
        path.rename(req["to"])
    elif action == "lstat":
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        kind = ("symlink" if stat.S_ISLNK(info.st_mode) else "dir" if stat.S_ISDIR(info.st_mode)
                else "file" if stat.S_ISREG(info.st_mode) else "other")
        result = {"type": kind, "mode": info.st_mode}
        if kind == "file":
            result["size"] = info.st_size
        return result
    return None


if __name__ == "__main__":
    try:
        print(json.dumps({"value": dispatch(*sys.argv[1:3])}))
    except OSError as exc:
        print(json.dumps({"error": {"errno": exc.errno, "message": str(exc)}}))
