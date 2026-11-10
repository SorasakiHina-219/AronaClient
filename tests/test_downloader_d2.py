"""T3 (D2) 行为锁定测试 — downloader DNS 校验纯重命名回归防护。

D2 修复仅把 `is_safe` 重命名为 `is_unsafe`（原名与语义相反：为真时 IP 实为
不安全）。本测试锁定 `_resolve_and_validate_dns` 的**当前正确行为**，防止
后续任何人按变量"名字"误改逻辑（那会反转 SSRF 防护）。

行为契约（monkeypatch socket.getaddrinfo，不触真实网络）：
  - 纯公网解析        → success=True，ip = 第一个公网 IP（TOCTOU 安全直连）
  - 纯私网/保留各 5 类 → success=False，"内网地址"
  - 混合 公网+私网     → success=False，"混合地址"（DNS 重绑嫌疑）
  - DNS 失败/空结果/无效 IP → fail-closed success=False

Run:
  .venv/Scripts/python.exe -m pytest tests/test_downloader_d2.py -v
"""
from __future__ import annotations

import ipaddress
import socket
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from computer import downloader as dl_mod
from computer.downloader import Downloader


@pytest.fixture
def d(tmp_path):
    # 不触发任何网络 / keyring：用 tmp_path 作下载根目录（_resolve_and_validate_dns
    # 只读 self._download_root，不做 IO），避免真实 Downloads 目录
    return Downloader(download_root=tmp_path)


def _addrinfo(ips):
    """把 IP 列表包装成 socket.getaddrinfo 返回结构。"""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for ip in ips
    ]


def _patch_dns(monkeypatch, ips):
    monkeypatch.setattr(
        dl_mod.socket, "getaddrinfo",
        lambda *a, **k: _addrinfo(ips),
    )


# ── 纯公网放行 ────────────────────────────────────────────────────────

def test_pure_public_allowed(d, monkeypatch):
    _patch_dns(monkeypatch, ["93.184.216.34", "93.184.215.34"])
    r = d._resolve_and_validate_dns("example.com")
    assert r["success"] is True
    assert r["ip"] == "93.184.216.34"  # 第一个公网 IP


# ── 纯私网 / 保留 5 类全部拒绝 ────────────────────────────────────────

@pytest.mark.parametrize("ip,note", [
    ("127.0.0.1", "loopback"),
    ("10.0.0.5", "private-10"),
    ("192.168.1.10", "private-192.168"),
    ("::1", "ipv6-loopback"),
    ("169.254.10.10", "link-local"),
])
def test_private_rejected(d, monkeypatch, ip, note):
    _patch_dns(monkeypatch, [ip])
    r = d._resolve_and_validate_dns(f"host-{note}.local")
    assert r["success"] is False
    assert "内网地址" in r["message"] or "私有" in r["error"]


# ── 混合地址拒绝（DNS 重绑嫌疑）────────────────────────────────────

def test_mixed_rejected(d, monkeypatch):
    _patch_dns(monkeypatch, ["93.184.216.34", "127.0.0.1"])
    r = d._resolve_and_validate_dns("mixed.example")
    assert r["success"] is False
    assert "混合" in r["error"]


# ── fail-closed 路径 ─────────────────────────────────────────────────

def test_dns_failure_rejected(d, monkeypatch):
    def _boom(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(dl_mod.socket, "getaddrinfo", _boom)
    r = d._resolve_and_validate_dns("missing.example")
    assert r["success"] is False


def test_empty_result_rejected(d, monkeypatch):
    _patch_dns(monkeypatch, [])
    r = d._resolve_and_validate_dns("empty.example")
    assert r["success"] is False


def test_invalid_ip_rejected(d, monkeypatch):
    _patch_dns(monkeypatch, ["not-an-ip"])
    r = d._resolve_and_validate_dns("bad.example")
    assert r["success"] is False
    assert "无效" in r["error"]
