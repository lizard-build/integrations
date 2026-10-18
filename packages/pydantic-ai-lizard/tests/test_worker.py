"""Exercise guest process behavior in a temporary local test directory."""

import json
import os
import subprocess
import sys
from importlib.resources import files

import pytest

from pydantic_ai_lizard._worker import file_operation


def run_worker(tmp_path, argv, *, timeout=None, output_limit=1024):
    request = {
        "argv": argv,
        "cwd": str(tmp_path),
        "env": {"SAMPLE": "42"},
        "timeout": timeout,
        "output_limit": output_limit,
    }
    (tmp_path / "request.json").write_text(json.dumps(request))
    worker = str(files("pydantic_ai_lizard").joinpath("_worker.py"))
    subprocess.run([sys.executable, worker, "run", str(tmp_path)], check=True, timeout=10)
    result = json.loads((tmp_path / "result.json").read_text())
    return result, (tmp_path / "stdout").read_bytes(), (tmp_path / "stderr").read_bytes()


def test_exit_code_bytes_and_environment(tmp_path):
    result, out, err = run_worker(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import os,sys; os.write(1,os.environ['SAMPLE'].encode()+b'\\xff'); "
            "os.write(2,b'err'); sys.exit(7)",
        ],
    )
    assert result == {"exit_code": 7}
    assert (out, err) == (b"42\xff", b"err")


def test_stdin_eof_and_literal_arguments(tmp_path):
    payload = "$(touch BAD); `touch BAD`"
    result, out, _ = run_worker(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read() == ''); print(sys.argv[1])",
            payload,
        ],
    )
    assert result["exit_code"] == 0
    assert out == ("True\n" + payload + "\n").encode()
    assert not (tmp_path / "BAD").exists()


def test_missing_program(tmp_path):
    result, _, _ = run_worker(tmp_path, ["not-a-real-lizard-test-program"])
    assert result["exit_code"] == 127


def test_timeout_keeps_partial_output(tmp_path):
    result, out, _ = run_worker(
        tmp_path,
        [sys.executable, "-c", "import time; print('before', flush=True); time.sleep(10)"],
        timeout=0.1,
    )
    assert result["error"] == "timeout"
    assert out == b"before\n"


def test_output_limit_is_explicit(tmp_path):
    result, out, _ = run_worker(tmp_path, [sys.executable, "-c", "print('x'*10000)"])
    assert result["error"] == "output_limit"
    assert len(out) == 1024


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest worker")
def test_background_child_does_not_hold_command_open(tmp_path):
    result, out, _ = run_worker(tmp_path, ["sh", "-c", "sleep 0.2 & printf done"])
    assert result == {"exit_code": 0}
    assert out == b"done"


def test_file_as_parent_raises_not_a_directory(tmp_path):
    parent = tmp_path / "file"
    parent.write_bytes(b"old")
    with pytest.raises(NotADirectoryError):
        file_operation(
            {"action": "write", "path": str(parent / "child"), "source": str(parent), "offset": 0}
        )
    assert parent.read_bytes() == b"old"


def test_chunk_writes_follow_links_and_truncate(tmp_path):
    target, link, chunk = (tmp_path / name for name in ("target", "link", "chunk"))
    target.write_bytes(b"long old contents")
    link.symlink_to(target)
    chunk.write_bytes(b"abc")
    file_operation({"action": "write", "path": str(link), "source": str(chunk), "offset": 0})
    chunk.write_bytes(b"\x00\xff")
    file_operation({"action": "write", "path": str(link), "source": str(chunk), "offset": 3})
    assert link.is_symlink()
    assert target.read_bytes() == b"abc\x00\xff"
    file_operation(
        {"action": "read_chunk", "path": str(link), "target": str(chunk), "offset": 2, "length": 2}
    )
    assert chunk.read_bytes() == b"c\x00"


def test_special_files_fail_before_open(tmp_path):
    fifo, source = tmp_path / "fifo", tmp_path / "source"
    os.mkfifo(fifo)
    source.write_bytes(b"hello")
    with pytest.raises(OSError, match="regular files"):
        file_operation({"action": "read_check", "path": str(fifo)})
    with pytest.raises(OSError, match="regular files"):
        file_operation({"action": "write", "path": str(fifo), "source": str(source), "offset": 0})


def test_listing_keeps_broken_and_looping_links(tmp_path):
    (tmp_path / "broken").symlink_to(tmp_path / "missing")
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    entries = file_operation({"action": "list", "path": str(tmp_path)})["entries"]
    assert {item["name"] for item in entries} == {"broken", "loop"}
    assert all(item["size"] is None for item in entries)
