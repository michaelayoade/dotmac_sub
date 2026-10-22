"""Bounded pinned-host SSH loopback transport to the existing Bao authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import IO


@dataclass(frozen=True)
class LoopbackPlan:
    ssh_host: str
    ssh_user: str
    known_hosts: Path
    known_hosts_sha256: str
    cluster_id: str
    port: int = 18200
    lifetime_seconds: int = 1800

    def validate(self) -> None:
        if (
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", self.ssh_host)
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.ssh_user)
            or not re.fullmatch(r"[0-9a-f]{64}", self.known_hosts_sha256)
            or not re.fullmatch(r"[0-9a-f-]{36}", self.cluster_id)
            or type(self.port) is not int
            or not 1024 <= self.port <= 65535
            or type(self.lifetime_seconds) is not int
            or not 1 <= self.lifetime_seconds <= 1800
        ):
            raise RuntimeError("transport_plan_invalid")
        if not self.known_hosts.is_absolute():
            raise RuntimeError("transport_host_pin_required")
        for path in (self.known_hosts, *self.known_hosts.parents):
            info = path.lstat()
            if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError("transport_host_pin_unprotected")
        if (
            hashlib.sha256(self.known_hosts.read_bytes()).hexdigest()
            != self.known_hosts_sha256
        ):
            raise RuntimeError("transport_host_pin_mismatch")


@contextmanager
def open_transport(plan: LoopbackPlan) -> Iterator[str]:
    """SSH server must expose canonical Bao on its own loopback:8200.

    SSH identity comes from the existing protected SSH configuration/agent;
    this adapter does not retrieve, create or print credentials. Remote command
    is the fixed sleep lifetime, so tunnel expires even if coordinator dies.
    """
    plan.validate()
    endpoint = f"http://127.0.0.1:{plan.port}"
    argv = (
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={plan.known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-L",
        f"127.0.0.1:{plan.port}:127.0.0.1:8200",
        f"{plan.ssh_user}@{plan.ssh_host}",
        f"printf 'DOTMAC_TUNNEL_READY\\n'; exec sleep {plan.lifetime_seconds}",
    )
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={
            "PATH": "/usr/bin:/bin",
            **(
                {"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]}
                if "SSH_AUTH_SOCK" in os.environ
                else {}
            ),
        },
    )
    try:
        # Remote stdout cannot arrive until SSH's forwarding setup succeeds.
        if process.stdout is None:
            raise RuntimeError("transport_forward_unproven")
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        ready = b""
        deadline = time.monotonic() + min(15, plan.lifetime_seconds)
        while not ready.endswith(b"\n") and len(ready) < 64:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
                raise RuntimeError("transport_forward_unproven")
            try:
                chunk = os.read(descriptor, 64 - len(ready))
            except BlockingIOError:
                continue
            if not chunk:
                raise RuntimeError("transport_forward_unproven")
            ready += chunk
        if ready != b"DOTMAC_TUNNEL_READY\n":
            raise RuntimeError("transport_forward_unproven")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        deadline = time.monotonic() + min(15, plan.lifetime_seconds)
        while True:
            if process.poll() is not None:
                raise RuntimeError("transport_ssh_failed")
            try:
                with opener.open(endpoint + "/v1/sys/health", timeout=2) as response:
                    raw = response.read(65537)
                health = json.loads(raw) if len(raw) <= 65536 else {}
                if (
                    health.get("cluster_id") != plan.cluster_id
                    or health.get("sealed") is not False
                    or health.get("initialized") is not True
                ):
                    raise RuntimeError("transport_authority_mismatch")
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("transport_authority_unavailable") from None
                time.sleep(0.1)
        yield endpoint
    finally:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            if process.stdout is not None:
                process.stdout.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        raise RuntimeError("transport_redirect_refused")
