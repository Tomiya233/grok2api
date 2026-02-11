"""请求统计模块 - 按小时/天统计请求数据"""

import asyncio
import orjson
from datetime import datetime, timedelta
from typing import Dict, Any
from pathlib import Path
from collections import defaultdict

from app.core.logger import logger


class RequestStats:
    """请求统计管理器（单例）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return

        self.file_path = Path(__file__).parents[2] / "data" / "stats.json"

        # 统计数据
        self._hourly: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"total": 0, "success": 0, "failed": 0}
        )
        self._daily: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"total": 0, "success": 0, "failed": 0}
        )
        self._models: Dict[str, int] = defaultdict(int)

        # 保留策略
        self._hourly_keep = 48  # 保留48小时
        self._daily_keep = 30  # 保留30天

        # 清理间隔：每100次请求清理一次
        self._cleanup_interval = 100
        self._cleanup_counter = 0

        self._lock = asyncio.Lock()
        self._loaded = False
        self._save_task = None
        self._initialized = True

    async def init(self):
        """初始化加载数据"""
        if not self._loaded:
            await self._load_data()

    async def _load_data(self):
        """从磁盘加载统计数据"""
        if self._loaded:
            return

        if not self.file_path.exists():
            self._loaded = True
            return

        try:
            async with self._lock:
                content = await asyncio.to_thread(self.file_path.read_bytes)
                if content:
                    data = orjson.loads(content)

                    # 恢复 defaultdict 结构
                    self._hourly = defaultdict(
                        lambda: {"total": 0, "success": 0, "failed": 0}
                    )
                    self._hourly.update(data.get("hourly", {}))

                    self._daily = defaultdict(
                        lambda: {"total": 0, "success": 0, "failed": 0}
                    )
                    self._daily.update(data.get("daily", {}))

                    self._models = defaultdict(int)
                    self._models.update(data.get("models", {}))

                    logger.debug(f"[Stats] 加载统计数据成功")

                # 即使文件为空也视为已完成初始化，避免重复读取
                self._loaded = True
        except Exception as e:
            logger.error(f"[Stats] 加载数据失败: {e}")
            self._loaded = True  # 防止覆盖

    async def _save_data(self):
        """保存统计数据到磁盘"""
        if not self._loaded:
            return

        try:
            # 确保目录存在
            self.file_path.parent.mkdir(parents=True, exist_ok=True)

            async with self._lock:
                data = {
                    "hourly": dict(self._hourly),
                    "daily": dict(self._daily),
                    "models": dict(self._models),
                }
                content = orjson.dumps(data)
                await asyncio.to_thread(self.file_path.write_bytes, content)
        except Exception as e:
            logger.error(f"[Stats] 保存数据失败: {e}")

    async def record_request(self, model: str, success: bool) -> None:
        """记录一次请求"""
        if not self._loaded:
            await self.init()

        now = datetime.now()
        hour_key = now.strftime("%Y-%m-%dT%H")
        day_key = now.strftime("%Y-%m-%d")

        # 小时统计
        self._hourly[hour_key]["total"] += 1
        if success:
            self._hourly[hour_key]["success"] += 1
        else:
            self._hourly[hour_key]["failed"] += 1

        # 天统计
        self._daily[day_key]["total"] += 1
        if success:
            self._daily[day_key]["success"] += 1
        else:
            self._daily[day_key]["failed"] += 1

        # 模型统计
        self._models[model] += 1

        # 定期清理旧数据（每 _cleanup_interval 次请求执行一次）
        self._cleanup_counter += 1
        if self._cleanup_counter >= self._cleanup_interval:
            self._cleanup_counter = 0
            self._cleanup()

        # 合并保存任务，避免高并发下创建过多后台任务
        if self._save_task and not self._save_task.done():
            return
        self._save_task = asyncio.create_task(self._save_data())

    def _cleanup(self) -> None:
        """清理过期数据"""
        # 清理小时数据
        hour_keys = list(self._hourly.keys())
        if len(hour_keys) > self._hourly_keep:
            for key in sorted(hour_keys)[: -self._hourly_keep]:
                del self._hourly[key]

        # 清理天数据
        day_keys = list(self._daily.keys())
        if len(day_keys) > self._daily_keep:
            for key in sorted(day_keys)[: -self._daily_keep]:
                del self._daily[key]

    def get_stats(self, hours: int = 24, days: int = 7) -> Dict[str, Any]:
        """获取统计数据"""
        now = datetime.now()

        # 获取最近N小时数据
        hourly_data = []
        for i in range(hours - 1, -1, -1):
            dt = now - timedelta(hours=i)
            key = dt.strftime("%Y-%m-%dT%H")
            data = self._hourly.get(key, {"total": 0, "success": 0, "failed": 0})
            hourly_data.append(
                {"hour": dt.strftime("%H:00"), "date": dt.strftime("%m-%d"), **data}
            )

        # 获取最近N天数据
        daily_data = []
        for i in range(days - 1, -1, -1):
            dt = now - timedelta(days=i)
            key = dt.strftime("%Y-%m-%d")
            data = self._daily.get(key, {"total": 0, "success": 0, "failed": 0})
            daily_data.append({"date": dt.strftime("%m-%d"), **data})

        # 模型统计（取 Top 10）
        model_data = sorted(self._models.items(), key=lambda x: x[1], reverse=True)[:10]

        # 总计
        total_requests = sum(d["total"] for d in self._hourly.values())
        total_success = sum(d["success"] for d in self._hourly.values())
        total_failed = sum(d["failed"] for d in self._hourly.values())

        return {
            "hourly": hourly_data,
            "daily": daily_data,
            "models": [{"model": m, "count": c} for m, c in model_data],
            "summary": {
                "total": total_requests,
                "success": total_success,
                "failed": total_failed,
                "success_rate": round(total_success / total_requests * 100, 1)
                if total_requests > 0
                else 0,
            },
        }

    async def flush(self) -> None:
        """等待未完成的保存任务并确保落盘"""
        task = self._save_task
        if task and not task.done():
            try:
                await task
            except Exception as e:
                logger.warning(f"[Stats] 等待保存任务失败: {e}")

        await self._save_data()

    async def reset(self) -> None:
        """重置所有统计"""
        self._hourly.clear()
        self._daily.clear()
        self._models.clear()
        await self._save_data()


# 全局实例
request_stats = RequestStats()
