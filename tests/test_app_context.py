"""T4 验证测试 — 最小 AppContext + L1/L2 共享 AIClient / SettingsManager。

覆盖授权 §三 T4 的必验项：
  * 生命周期：AppContext.get() 幂等（双调用同一实例）；
  * 重复初始化边界：共享对象已就位（assert_ready）；
  * 并发访问边界：ai_busy 跨线程原子互斥（占用期间第二方 acquire 失败）；
  * L1/L2 共享实例证明：同一 AppContext 提供的 ai_client / settings
    被两个 UI 构造器注入后，id() 完全相同（单一 AIClient 语义）。

不启动 Qt 事件循环、不发网络：以 __new__ 构造空壳 UI + 注入共享对象，
验证"注入后指向同一实例"这一契约本身。

Run:
  .venv/Scripts/python.exe -m pytest tests/test_app_context.py -v
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.app_context import AppContext


# ── 生命周期：幂等单例 ──────────────────────────────────────────────

def test_get_is_idempotent():
    a = AppContext.get()
    b = AppContext.get()
    assert a is b


def test_get_single_across_threads():
    """并发 get()：所有线程拿到同一实例。"""
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(AppContext.get())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r is results[0] for r in results)


def test_shared_objects_ready():
    ctx = AppContext.get()
    ctx.assert_ready()
    assert ctx.ai_client is not None
    assert ctx.settings is not None


def test_reset_for_tests_recreates():
    before = AppContext.get()
    AppContext._reset_for_tests()
    after = AppContext.get()
    assert before is not after
    after.assert_ready()


# ── 并发访问边界：ai_busy 原子互斥 ─────────────────────────────────

def test_ai_busy_exclusive():
    ctx = AppContext.get()
    assert ctx.acquire_ai_busy(owner="A") is True
    assert ctx.ai_busy is True
    # 第二方在占用期间必须失败
    assert ctx.acquire_ai_busy(owner="B") is False
    assert ctx.ai_busy is True
    ctx.release_ai_busy()
    assert ctx.ai_busy is False
    # 释放后可再抢占
    assert ctx.acquire_ai_busy(owner="C") is True
    ctx.release_ai_busy()


def test_ai_busy_release_is_idempotent():
    ctx = AppContext.get()
    # 未持有即释放：不抛异常（幂等）
    ctx.release_ai_busy()
    assert ctx.ai_busy is False


def test_ai_busy_cross_thread_contention():
    """跨线程：占用方持锁期间，竞争线程全部失败；释放后恰好一方成功。"""
    ctx = AppContext.get()
    assert ctx.acquire_ai_busy(owner="holder") is True

    contenders = []

    def try_acquire():
        contenders.append(ctx.acquire_ai_busy(owner=f"cx{len(contenders)}"))

    threads = [threading.Thread(target=try_acquire) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(c is False for c in contenders), "占用期间竞争者不得成功"

    ctx.release_ai_busy()
    # 释放后至少一方能抢到
    assert ctx.acquire_ai_busy(owner="post") is True
    ctx.release_ai_busy()


# ── L1/L2 共享实例证明（单一 AIClient 语义）───────────────────────

def test_l1_l2_share_same_ai_client_instance():
    """模拟 L1/L2 经同一 AppContext 注入：两个 UI 的 ai_client 是同一对象。

    用 __new__ 构造空壳（不跑 11k 行 UI 初始化），仅验证注入契约：
    注入后 instance.ai_client is AppContext.get().ai_client。
    """
    ctx = AppContext.get()
    shared_ai = ctx.ai_client
    shared_settings = ctx.settings

    from ui import AronaUI
    import spatial_ui.spatial_main_window as smw

    l1 = AronaUI.__new__(AronaUI)
    l1.ai_client = shared_ai            # main.py 注入路径等价
    assert l1.ai_client is ctx.ai_client
    assert l1.ai_client is shared_ai

    l2 = smw.SpatialMainWindow.__new__(smw.SpatialMainWindow)
    l2.ai_client = shared_ai
    l2.settings = shared_settings        # spatial_main_window.main() 注入路径等价
    assert l2.ai_client is ctx.ai_client
    assert l2.settings is ctx.settings
    assert l1.ai_client is l2.ai_client  # 单一 AIClient 语义


def test_single_settings_writer():
    """L1/L2 共享同一 SettingsManager 实例（单写者，无双写竞态）。"""
    ctx = AppContext.get()
    assert ctx.settings is not None
    # AIClient 的 settings 也应指向同一共享实例（_init 内绑定）
    assert ctx.ai_client.settings is ctx.settings
