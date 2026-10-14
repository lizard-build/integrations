"""Standard-library worker copied into the sandbox, never executed on the host."""

import base64
import json
import os
import pwd
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


def run(job):
    request = json.loads((job / "request.json").read_text())
    process = None

    def interrupted(signum, frame):
        raise InterruptedError("Command cancelled")

    signal.signal(signal.SIGTERM, interrupted)
    result: dict[str, Any] = {}
    try:
        user = request.get("user")
        credentials: dict[str, Any] = {}
        if user is not None:
            try:
                account = pwd.getpwuid(int(user)) if user.isdigit() else pwd.getpwnam(user)
            except KeyError:
                result = {"error": "user", "message": f"Unknown sandbox user: {user}"}
                return result
            credentials = dict(
                user=account.pw_uid,
                group=account.pw_gid,
                extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
            )
        with (job / "stdin").open("wb") as f:
            f.write(base64.b64decode(request.get("input", "")))
        with (
            (job / "stdin").open("rb") as stdin,
            (job / "stdout").open("wb") as stdout,
            (job / "stderr").open("wb") as stderr,
        ):
            try:
                process = subprocess.Popen(
                    request["cmd"],
                    cwd=request["cwd"],
                    env={**os.environ, **request.get("env", {})},
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    **credentials,
                )
                code = process.wait(timeout=request.get("timeout"))
                result = {"returncode": code if code >= 0 else 128 - code}
            except FileNotFoundError as exc:
                stderr.write(str(exc).encode())
                result = {"returncode": 127}
            except PermissionError as exc:
                result = {"error": "permission", "message": str(exc)}
            except (subprocess.TimeoutExpired, InterruptedError) as exc:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                result = {
                    "error": "timeout"
                    if isinstance(exc, subprocess.TimeoutExpired)
                    else "cancelled"
                }
        limit = request["output_limit"]
        for name in ("stdout", "stderr"):
            path = job / name
            with path.open("rb") as f:
                f.seek(0, 2)
                length = f.tell()
                f.seek(max(0, length - limit) if limit is not None else 0)
                tail = f.read()
            path.write_bytes(tail)
            result[name + "_size"] = len(tail)
            result[name + "_truncated"] = limit is not None and length > limit
            result[name + "_dropped_bytes"] = max(0, length - len(tail))
        return result
    except Exception as exc:
        return {"error": "worker", "message": str(exc)}
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def main():
    action, value = sys.argv[1:3]
    if action == "start":
        process = subprocess.Popen(
            [sys.executable, __file__, "run", value],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        print(json.dumps({"pid": process.pid}))
    elif action == "run":
        job = Path(value)
        result = run(job)
        temporary = job / "result.tmp"
        temporary.write_text(json.dumps(result))
        temporary.replace(job / "result.json")
    elif action == "stat":
        try:
            path = Path(value)
            stat = path.stat()
            print(json.dumps({"size": stat.st_size, "directory": path.is_dir()}))
        except OSError as exc:
            print(json.dumps({"errno": exc.errno, "message": str(exc)}))
    elif action == "stop":
        try:
            os.kill(int(value), signal.SIGTERM)
        except ProcessLookupError:
            pass
        print("{}")
    elif action == "write_chunk":
        request = json.loads(value)
        target = Path(request["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        data = Path(request["source"]).read_bytes()
        with target.open("r+b" if target.exists() else "wb") as destination:
            destination.seek(request["offset"])
            destination.write(data)
            destination.truncate()
        print("{}")
    elif action == "chunk":
        request = json.loads(value)
        with open(request["path"], "rb") as source:
            source.seek(request["offset"])
            data = source.read(request["length"])
        Path(request["target"]).write_bytes(data)
        print(json.dumps({"length": len(data)}))


if __name__ == "__main__":
    main()
