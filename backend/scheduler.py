"""后台调度器：周期扫描 → 自动更新候选执行（PRD 5.3 触发时机层①）。

回答"自动更新到底什么时候执行"：
1. 用户在设置页配置 scan_interval_sec（秒），存 settings 表；
   调度线程在每段等待内重读该值 → 配置改动秒级热生效（无需重启）。
2. interval=0（默认）→ 线程挂起（纯手动模式）；改成 >0 即自动恢复。
3. 每轮到期：detect.scan()（自带扫描互斥）→ auto_update_pending() 为真才
   engine.run_update(manual=False)——发布延迟 / 合并等待 / freeze / ignored /
   protected 全部在计划层过滤，自动更新永不触碰版本号 tag。
4. 与手动操作互斥：扫描走 detect._SCAN_LOCK，更新走 engine._UPDATE_LOCK；
   忙碌时对应步骤本轮跳过（扫描返回 already-running，更新不排队）。

单轮失败只记录 last_error，绝不终止调度线程。
"""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend import db, detect, engine
from backend.config import CONFIG

logger = logging.getLogger("scheduler")

_POLL_SEC = 0.5  # 等待期唤醒粒度（检查停止事件 / 配置变化）
_IDLE_POLL_SEC = 1.0  # 挂起态（interval=0）的配置复查周期

_STOP = threading.Event()
_THREAD: Optional[threading.Thread] = None
_GET_DOCKER: Optional[Callable[[], Any]] = None
_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "interval_sec": 0,
    "busy": False,
    "cycles": 0,
    "last_started": None,
    "last_finished": None,
    "next_due_epoch": None,
    "last_result": None,
    "last_error": None,
}


def _read_interval() -> int:
    """当前生效间隔：settings 表优先，其次环境变量基线；负值归零。"""
    raw = db.setting_get("scan_interval_sec")
    try:
        v = int(raw) if raw else CONFIG.SCAN_INTERVAL_SEC
    except ValueError:
        v = CONFIG.SCAN_INTERVAL_SEC
    return max(0, v)


def _iso(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


def _patch(**kw: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(kw)


def status() -> dict[str, Any]:
    """调度器状态快照（API /api/scheduler → 仪表盘）。"""
    with _STATE_LOCK:
        st = dict(_STATE)
    st["enabled"] = st["interval_sec"] > 0
    st["next_due"] = _iso(st.pop("next_due_epoch"))
    st["running"] = _THREAD is not None and _THREAD.is_alive()
    return st


def _run_cycle() -> None:
    get = _GET_DOCKER
    if get is None:
        return
    _patch(busy=True, last_started=db.now_iso(), last_error=None)
    result: dict[str, Any] = {}
    try:
        scan_out = detect.scan(get(), force=False)
        result["scan"] = scan_out
        # 审计日志：自动轮次扫描结果（有发现时记详细，无发现不刷屏）
        if scan_out.get("events"):
            db.log_event(
                "info",
                f"自动调度扫描：发现 {scan_out['events']} 项更新，检查 {scan_out.get('checked', 0)} 个容器",
            )
        # 仅当"自动模式候选就绪"才执行更新：避免无候选周期产生空任务
        if engine.auto_update_pending(get()):
            db.log_event("info", "自动调度：存在就绪候选，即将自动执行更新")
            out = engine.run_update(get(), manual=False)
            if out.get("status") == "update already running":
                result["update"] = "busy"  # 手动更新进行中 → 本轮让行
            else:
                result["update"] = "executed"
                result["job_id"] = out.get("job_id")
                result["containers"] = out.get("containers", [])
        else:
            result["update"] = "no-ready-candidates"
        with _STATE_LOCK:
            _STATE["cycles"] += 1
        _patch(last_result=result, last_finished=db.now_iso())
    except Exception as e:  # 单轮失败不终止调度线程
        logger.exception("scheduler cycle failed")
        _patch(last_error=str(e), last_finished=db.now_iso())
    finally:
        _patch(busy=False)


def _loop() -> None:
    logger.info("scheduler loop started")
    while not _STOP.is_set():
        try:
            interval = _read_interval()
            _patch(interval_sec=interval)
            if interval <= 0:
                # 挂起：纯手动模式，只复查配置是否被改开
                _patch(next_due_epoch=None)
                _STOP.wait(_IDLE_POLL_SEC)
                continue
            # 进入自动模式（激活/配置变更）：立即执行一轮（建基线/刷新），
            # 随后按间隔周期执行
            _run_cycle()
            deadline = time.monotonic() + interval
            _patch(next_due_epoch=time.time() + interval)
            # 分段等待：停止 / 配置变化立即响应（热生效）
            while not _STOP.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if _read_interval() != interval:
                    break  # 间隔被改 → 立即按新配置进入下一轮
                _STOP.wait(min(_POLL_SEC, remaining))
            if _STOP.is_set():
                break
        except Exception:  # 任何循环级异常都不允许杀死调度线程
            logger.exception("scheduler loop iteration failed")
            _STOP.wait(_IDLE_POLL_SEC)
    logger.info("scheduler loop stopped")


def start(get_docker: Callable[[], Any]) -> None:
    """启动调度线程（幂等）。get_docker 返回当前 Docker 客户端（惰性解析，
    兼容测试/运行期对 app.DOCKER 的替换）。"""
    global _THREAD, _GET_DOCKER
    if _THREAD and _THREAD.is_alive():
        return
    _GET_DOCKER = get_docker
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="vt-scheduler", daemon=True)
    _THREAD.start()
    logger.info("scheduler started")


def stop(timeout: float = 5.0) -> None:
    """请求停止并等待线程退出（进行中的一轮会自然结束后退出）。"""
    _STOP.set()
    t = _THREAD
    if t and t.is_alive():
        t.join(timeout=timeout)
