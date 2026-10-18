"""Command worker uploaded to a sandbox; no Lizard credentials enter the guest."""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


def file_operation(request):
    action, path = request["action"], Path(request["path"])

    def entry(target):
        try:
            info = target.stat()
            directory = stat.S_ISDIR(info.st_mode)
            size = None if directory else info.st_size
        except OSError:
            directory, size = False, None
        return {"name": target.name, "path": str(target), "is_dir": directory, "size": size}

    if action == "stat":
        path.stat()
        return entry(path)
    if action == "list":
        return {"entries": [entry(child) for child in path.iterdir()]}
    if action == "mkdir":
        path.mkdir(parents=True, exist_ok=True)
    elif action == "exists":
        return {"exists": path.exists()}
    elif action == "realpath":
        return {"path": os.path.realpath(path)}
    elif action == "remove":
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)
    elif action == "write":
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            raise NotADirectoryError(20, "Not a directory", str(path.parent)) from None
        if path.exists() and not path.is_file() and not path.is_dir():
            raise OSError(22, "Only regular files can be written", str(path))
        with path.open("r+b" if path.exists() else "wb") as target:
            target.seek(request["offset"])
            target.write(Path(request["source"]).read_bytes())
            target.truncate()
    elif action in {"read_check", "read_chunk"}:
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            raise IsADirectoryError(21, "Is a directory", str(path))
        if not stat.S_ISREG(info.st_mode):
            raise OSError(22, "Only regular files can be read", str(path))
        if action == "read_chunk":
            with path.open("rb") as source:
                source.seek(request["offset"])
                Path(request["target"]).write_bytes(source.read(request["length"]))
        return {"size": info.st_size}
    return {}


def run(job):
    request = json.loads((job / "request.json").read_text())
    process = None

    def interrupted(signum, frame):
        raise InterruptedError("Command cancelled")

    signal.signal(signal.SIGTERM, interrupted)
    result: dict[str, Any] = {}
    try:
        with (job / "stdout").open("wb") as stdout, (job / "stderr").open("wb") as stderr:
            try:
                process = subprocess.Popen(
                    request["argv"],
                    cwd=request["cwd"],
                    env={**os.environ, **request["env"]},
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                code = process.wait(timeout=request["timeout"])
                result["exit_code"] = code if code >= 0 else 128 - code
            except FileNotFoundError as exc:
                stderr.write(str(exc).encode())
                result["exit_code"] = 127
            except PermissionError as exc:
                stderr.write(str(exc).encode())
                result["exit_code"] = 126
            except (subprocess.TimeoutExpired, InterruptedError) as exc:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                result["error"] = (
                    "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "cancelled"
                )
        # Regular files let us observe the direct child's exit even when a background
        # child keeps stdout open. Never return a truncated successful result.
        for name in ("stdout", "stderr"):
            path = job / name
            if path.stat().st_size > request["output_limit"]:
                if "error" not in result:
                    result["error"] = "output_limit"
                with path.open("r+b") as stream:
                    stream.truncate(request["output_limit"])
        return result
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
        try:
            result = run(job)
        except Exception as exc:
            result = {"error": "worker", "message": str(exc)}
        temporary = job / "result.tmp"
        temporary.write_text(json.dumps(result))
        temporary.replace(job / "result.json")
    elif action == "poll":
        path = Path(value) / "result.json"
        print(path.read_text() if path.exists() else "null")
    elif action == "stop":
        try:
            os.kill(int(value), signal.SIGTERM)
        except ProcessLookupError:
            pass
        print("null")

    elif action == "fs":
        try:
            print(json.dumps(file_operation(json.loads(value))))
        except OSError as exc:
            print(json.dumps({"errno": exc.errno, "message": str(exc)}))


if __name__ == "__main__":
    main()
