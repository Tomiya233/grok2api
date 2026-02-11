"""Grok Token 管理器 - 单例模式的Token负载均衡和状态管理"""

import orjson
import time
import asyncio
import portalocker
from pathlib import Path
from curl_cffi.requests import AsyncSession
from typing import Dict, Any, Optional, Tuple

from app.models.grok_models import TokenType, Models
from app.core.exception import GrokApiException
from app.core.logger import logger
from app.core.config import setting
from app.core.proxy_pool import proxy_pool
from app.services.grok.statsig import get_dynamic_headers


# 常量
RATE_LIMIT_API = "https://grok.com/rest/rate-limits"
TIMEOUT = 30
BROWSER = "chrome133a"
MAX_FAILURES = 3
TOKEN_INVALID = 401
STATSIG_INVALID = 403

# 冷却常量
COOLDOWN_REQUESTS = 5  # 普通失败冷却请求数
COOLDOWN_429_WITH_QUOTA = 3600  # 429+有额度冷却1小时（秒）
COOLDOWN_429_NO_QUOTA = 36000  # 429+无额度冷却10小时（秒）


class GrokTokenManager:
    """Token管理器（单例）"""

    _instance: Optional["GrokTokenManager"] = None

    def __new__(cls) -> "GrokTokenManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return

        self.token_file = Path(__file__).parents[3] / "data" / "token.json"
        self._file_lock = asyncio.Lock()
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self._storage = None
        self.token_data = None  # 延迟加载

        # 批量保存队列
        self._save_pending = False  # 标记是否有待保存的数据
        self._save_task = None  # 后台保存任务
        self._shutdown = False  # 关闭标志

        # 冷却状态
        self._cooldown_counts: Dict[str, int] = {}  # Token -> 剩余冷却次数
        self._request_counter = 0  # 全局请求计数器

        # 刷新状态
        self._refresh_lock = False  # 刷新锁
        self._refresh_progress: Dict[str, Any] = {
            "running": False,
            "current": 0,
            "total": 0,
            "success": 0,
            "failed": 0,
        }

        self._initialized = True
        logger.debug(f"[Token] 初始化完成: {self.token_file}")

    def set_storage(self, storage) -> None:
        """设置存储实例"""
        self._storage = storage

    async def _load_data(self) -> None:
        """异步加载Token数据（支持多进程）"""
        default = {TokenType.NORMAL.value: {}, TokenType.SUPER.value: {}}

        def load_sync():
            with open(self.token_file, "r", encoding="utf-8") as f:
                portalocker.lock(f, portalocker.LOCK_SH)
                try:
                    return orjson.loads(f.read())
                finally:
                    portalocker.unlock(f)

        try:
            if self.token_file.exists():
                # 使用进程锁读取文件
                async with self._file_lock:
                    loaded = await asyncio.to_thread(load_sync)

                # 兼容旧键名：sso -> ssoNormal
                if isinstance(loaded, dict):
                    if "ssoNormal" not in loaded and "sso" in loaded:
                        loaded[TokenType.NORMAL.value] = loaded.get("sso", {})
                    loaded.setdefault(TokenType.NORMAL.value, {})
                    loaded.setdefault(TokenType.SUPER.value, {})
                else:
                    loaded = default

                self.token_data = loaded
            else:
                self.token_data = default
                logger.debug("[Token] 创建新数据文件")
        except Exception as e:
            logger.error(f"[Token] 加载失败: {e}")
            self.token_data = default

    async def _save_data(self) -> None:
        """保存Token数据（支持多进程）"""

        def save_sync(data):
            with open(self.token_file, "w", encoding="utf-8") as f:
                portalocker.lock(f, portalocker.LOCK_EX)
                try:
                    content = orjson.dumps(data, option=orjson.OPT_INDENT_2).decode()
                    f.write(content)
                    f.flush()
                finally:
                    portalocker.unlock(f)

        try:
            if not self._storage:
                async with self._file_lock:
                    await asyncio.to_thread(save_sync, self.token_data)
            else:
                await self._storage.save_tokens(self.token_data)
        except Exception as e:
            logger.error(f"[Token] 保存失败: {e}")
            raise GrokApiException(f"保存失败: {e}", "TOKEN_SAVE_ERROR")

    def _mark_dirty(self) -> None:
        """标记有待保存的数据"""
        self._save_pending = True

    async def _batch_save_worker(self) -> None:
        """批量保存后台任务"""
        interval = setting.global_config.get("batch_save_interval", 1.0)
        logger.info(f"[Token] 存储任务已启动，间隔: {interval}s")

        while not self._shutdown:
            await asyncio.sleep(interval)

            if self._save_pending and not self._shutdown:
                try:
                    await self._save_data()
                    self._save_pending = False
                    logger.debug("[Token] 存储完成")
                except Exception as e:
                    logger.error(f"[Token] 存储失败: {e}")

    async def start_batch_save(self) -> None:
        """启动批量保存任务"""
        if self._save_task is None:
            self._save_task = asyncio.create_task(self._batch_save_worker())
            logger.info("[Token] 存储任务已创建")

    async def shutdown(self) -> None:
        """关闭并刷新所有待保存数据"""
        self._shutdown = True

        if self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass

        # 最终刷新
        if self._save_pending:
            await self._save_data()
            logger.info("[Token] 关闭时刷新完成")

    @staticmethod
    def _extract_sso(auth_token: str) -> Optional[str]:
        """提取SSO值"""
        if "sso=" in auth_token:
            return auth_token.split("sso=")[1].split(";")[0]
        logger.warning("[Token] 无法提取SSO值")
        return None

    def _find_token(self, sso: str) -> Tuple[Optional[str], Optional[Dict]]:
        """查找Token"""
        for token_type in [TokenType.NORMAL.value, TokenType.SUPER.value]:
            if sso in self.token_data[token_type]:
                return token_type, self.token_data[token_type][sso]
        return None, None

    async def add_token(self, tokens: list[str], token_type: TokenType) -> None:
        """添加Token"""
        if not tokens:
            return

        count = 0
        for token in tokens:
            if not token or not token.strip():
                continue

            self.token_data[token_type.value][token] = {
                "createdTime": int(time.time() * 1000),
                "remainingQueries": -1,
                "heavyremainingQueries": -1,
                "status": "active",
                "failedCount": 0,
                "lastFailureTime": None,
                "lastFailureReason": None,
                "tags": [],
                "note": "",
            }
            count += 1

        self._mark_dirty()  # 批量保存
        logger.info(f"[Token] 添加 {count} 个 {token_type.value} Token")

    async def delete_token(self, tokens: list[str], token_type: TokenType) -> None:
        """删除Token"""
        if not tokens:
            return

        count = 0
        for token in tokens:
            if token in self.token_data[token_type.value]:
                del self.token_data[token_type.value][token]
                count += 1

        self._mark_dirty()  # 批量保存
        logger.info(f"[Token] 删除 {count} 个 {token_type.value} Token")

    async def update_token_tags(
        self, token: str, token_type: TokenType, tags: list[str]
    ) -> None:
        """更新Token标签"""
        if token not in self.token_data[token_type.value]:
            raise GrokApiException(
                "Token不存在", "TOKEN_NOT_FOUND", {"token": token[:10]}
            )

        cleaned = [t.strip() for t in tags if t and t.strip()]
        self.token_data[token_type.value][token]["tags"] = cleaned
        self._mark_dirty()  # 批量保存
        logger.info(f"[Token] 更新标签: {token[:10]}... -> {cleaned}")

    async def update_token_note(
        self, token: str, token_type: TokenType, note: str
    ) -> None:
        """更新Token备注"""
        if token not in self.token_data[token_type.value]:
            raise GrokApiException(
                "Token不存在", "TOKEN_NOT_FOUND", {"token": token[:10]}
            )

        self.token_data[token_type.value][token]["note"] = note.strip()
        self._mark_dirty()  # 批量保存
        logger.info(f"[Token] 更新备注: {token[:10]}...")

    def get_tokens(self) -> Dict[str, Any]:
        """获取所有Token"""
        return self.token_data.copy()

    async def _reload_if_needed(self) -> None:
        """在多进程模式下重新加载数据"""
        # 只在文件模式且多进程环境下才重新加载
        if self._storage:
            return

        def reload_sync():
            with open(self.token_file, "r", encoding="utf-8") as f:
                portalocker.lock(f, portalocker.LOCK_SH)
                try:
                    return orjson.loads(f.read())
                finally:
                    portalocker.unlock(f)

        try:
            if self.token_file.exists():
                self.token_data = await asyncio.to_thread(reload_sync)
        except Exception as e:
            logger.warning(f"[Token] 重新加载失败: {e}")

    async def get_token(self, model: str) -> str:
        """获取Token"""
        jwt = await self.select_token(model)
        return f"sso-rw={jwt};sso={jwt}"

    async def select_token(self, model: str) -> str:
        """选择最优Token（多进程安全，支持冷却）"""
        # 重新加载最新数据（多进程模式）
        await self._reload_if_needed()

        # 递减所有次数冷却计数
        self._request_counter += 1
        for token in list(self._cooldown_counts.keys()):
            self._cooldown_counts[token] -= 1
            if self._cooldown_counts[token] <= 0:
                del self._cooldown_counts[token]
                logger.debug(f"[Token] 冷却结束: {token[:10]}...")

        current_time = time.time() * 1000  # 毫秒

        def select_best(
            tokens: Dict[str, Any], field: str
        ) -> Tuple[Optional[str], Optional[int]]:
            """选择最佳Token"""
            unused, used = [], []

            for key, data in tokens.items():
                # 跳过已失效的token
                if data.get("status") == "expired":
                    continue

                # 跳过失败次数过多的token（任何错误状态码）
                if data.get("failedCount", 0) >= MAX_FAILURES:
                    continue

                # 跳过次数冷却中的token
                if key in self._cooldown_counts:
                    continue

                # 跳过时间冷却中的token（429）
                cooldown_until = data.get("cooldownUntil", 0)
                if cooldown_until and cooldown_until > current_time:
                    continue

                remaining = int(data.get(field, -1))
                if remaining == 0:
                    continue

                if remaining == -1:
                    unused.append(key)
                elif remaining > 0:
                    used.append((key, remaining))

            if unused:
                return unused[0], -1
            if used:
                used.sort(key=lambda x: x[1], reverse=True)
                return used[0][0], used[0][1]
            return None, None

        # 直接引用（单线程 asyncio 无需快照，避免大量 Token 时的复制开销）
        normal_tokens = self.token_data[TokenType.NORMAL.value]
        super_tokens = self.token_data[TokenType.SUPER.value]

        # 选择策略
        if model == "grok-4-heavy":
            field = "heavyremainingQueries"
            token_key, remaining = select_best(super_tokens, field)
        else:
            field = "remainingQueries"
            token_key, remaining = select_best(normal_tokens, field)
            if token_key is None:
                token_key, remaining = select_best(super_tokens, field)

        if token_key is None:
            raise GrokApiException(
                f"没有可用Token: {model}",
                "NO_AVAILABLE_TOKEN",
                {
                    "model": model,
                    "normal": len(normal_tokens),
                    "super": len(super_tokens),
                    "cooldown_count": len(self._cooldown_counts),
                },
            )

        status = "未使用" if remaining == -1 else f"剩余{remaining}次"
        logger.debug(f"[Token] 分配Token: {model} ({status})")
        return token_key

    async def check_limits(
        self, auth_token: str, model: str
    ) -> Optional[Dict[str, Any]]:
        """检查速率限制"""
        try:
            rate_model = Models.to_rate_limit(model)
            payload = {"requestKind": "DEFAULT", "modelName": rate_model}

            cf = setting.grok_config.get("cf_clearance", "")
            headers = get_dynamic_headers("/rest/rate-limits")
            headers["Cookie"] = f"{auth_token};{cf}" if cf else auth_token

            # 外层重试：可配置状态码（401/429等）
            retry_codes = setting.grok_config.get("retry_status_codes", [401, 429])
            MAX_OUTER_RETRY = 3

            for outer_retry in range(MAX_OUTER_RETRY + 1):  # +1 确保实际重试3次
                # 内层重试：403代理池重试
                max_403_retries = 5
                retry_403_count = 0

                while retry_403_count <= max_403_retries:
                    # 如果是403重试且使用代理池，强制刷新代理
                    if retry_403_count > 0 and proxy_pool.enabled:
                        logger.info(
                            f"[Token] 403重试 {retry_403_count}/{max_403_retries}，刷新代理..."
                        )
                        proxy = await proxy_pool.force_refresh()
                    else:
                        proxy = await setting.get_proxy_async("service")

                    proxies = {"http": proxy, "https": proxy} if proxy else None

                    async with AsyncSession() as session:
                        response = await session.post(
                            RATE_LIMIT_API,
                            headers=headers,
                            json=payload,
                            impersonate=BROWSER,
                            timeout=TIMEOUT,
                            proxies=proxies,
                        )

                        # 内层403重试：仅当有代理池时触发
                        if response.status_code == 403 and proxy_pool.enabled:
                            retry_403_count += 1

                            if retry_403_count <= max_403_retries:
                                logger.warning(
                                    f"[Token] 遇到403错误，正在重试 ({retry_403_count}/{max_403_retries})..."
                                )
                                await asyncio.sleep(0.5)
                                continue

                            # 内层重试全部失败
                            logger.error(
                                f"[Token] 403错误，已重试{retry_403_count - 1}次，放弃"
                            )
                            sso = self._extract_sso(auth_token)
                            if sso:
                                await self.record_failure(
                                    auth_token, 403, "服务器被Block"
                                )

                        # 检查可配置状态码错误 - 外层重试
                        if response.status_code in retry_codes:
                            if outer_retry < MAX_OUTER_RETRY:
                                delay = (
                                    outer_retry + 1
                                ) * 0.1  # 渐进延迟：0.1s, 0.2s, 0.3s
                                logger.warning(
                                    f"[Token] 遇到{response.status_code}错误，外层重试 ({outer_retry + 1}/{MAX_OUTER_RETRY})，等待{delay}s..."
                                )
                                await asyncio.sleep(delay)
                                break  # 跳出内层循环，进入外层重试
                            else:
                                logger.error(
                                    f"[Token] {response.status_code}错误，已重试{outer_retry}次，放弃"
                                )
                                sso = self._extract_sso(auth_token)
                                if sso:
                                    if response.status_code == 401:
                                        await self.record_failure(
                                            auth_token, 401, "Token失效"
                                        )
                                    else:
                                        await self.record_failure(
                                            auth_token,
                                            response.status_code,
                                            f"错误: {response.status_code}",
                                        )
                                return None

                        if response.status_code == 200:
                            data = response.json()
                            sso = self._extract_sso(auth_token)

                            if outer_retry > 0 or retry_403_count > 0:
                                logger.info(f"[Token] 重试成功！")

                            if sso:
                                if model == "grok-4-heavy":
                                    await self.update_limits(
                                        sso,
                                        normal=None,
                                        heavy=data.get("remainingQueries", -1),
                                    )
                                    logger.info(
                                        f"[Token] 更新限制: {sso[:10]}..., heavy={data.get('remainingQueries', -1)}"
                                    )
                                else:
                                    await self.update_limits(
                                        sso,
                                        normal=data.get("remainingTokens", -1),
                                        heavy=None,
                                    )
                                    logger.info(
                                        f"[Token] 更新限制: {sso[:10]}..., basic={data.get('remainingTokens', -1)}"
                                    )

                            return data
                        else:
                            # 其他错误
                            logger.warning(
                                f"[Token] 获取限制失败: {response.status_code}"
                            )
                            sso = self._extract_sso(auth_token)
                            if sso:
                                await self.record_failure(
                                    auth_token,
                                    response.status_code,
                                    f"错误: {response.status_code}",
                                )
                            return None

        except Exception as e:
            logger.error(f"[Token] 检查限制错误: {e}")
            return None

    async def update_limits(
        self, sso: str, normal: Optional[int] = None, heavy: Optional[int] = None
    ) -> None:
        """更新限制"""
        try:
            for token_type in [TokenType.NORMAL.value, TokenType.SUPER.value]:
                if sso in self.token_data[token_type]:
                    if normal is not None:
                        self.token_data[token_type][sso]["remainingQueries"] = normal
                    if heavy is not None:
                        self.token_data[token_type][sso]["heavyremainingQueries"] = (
                            heavy
                        )
                    self._mark_dirty()  # 批量保存
                    logger.info(f"[Token] 更新限制: {sso[:10]}...")
                    return
            logger.warning(f"[Token] 未找到: {sso[:10]}...")
        except Exception as e:
            logger.error(f"[Token] 更新限制错误: {e}")

    async def record_failure(self, auth_token: str, status: int, msg: str) -> None:
        """记录失败"""
        try:
            if status == STATSIG_INVALID:
                logger.warning("[Token] IP被Block，请: 1.更换IP 2.使用代理 3.配置CF值")
                return

            sso = self._extract_sso(auth_token)
            if not sso:
                return

            _, data = self._find_token(sso)
            if not data:
                logger.warning(f"[Token] 未找到: {sso[:10]}...")
                return

            data["failedCount"] = data.get("failedCount", 0) + 1
            data["lastFailureTime"] = int(time.time() * 1000)
            data["lastFailureReason"] = f"{status}: {msg}"

            logger.warning(
                f"[Token] 失败: {sso[:10]}... (状态:{status}), "
                f"次数: {data['failedCount']}/{MAX_FAILURES}, 原因: {msg}"
            )

            if 400 <= status < 500 and data["failedCount"] >= MAX_FAILURES:
                data["status"] = "expired"
                logger.error(
                    f"[Token] 标记失效: {sso[:10]}... (连续{status}错误{data['failedCount']}次)"
                )

            self._mark_dirty()  # 批量保存

        except Exception as e:
            logger.error(f"[Token] 记录失败错误: {e}")

    async def reset_failure(self, auth_token: str) -> None:
        """重置失败计数"""
        try:
            sso = self._extract_sso(auth_token)
            if not sso:
                return

            _, data = self._find_token(sso)
            if not data:
                return

            if data.get("failedCount", 0) > 0:
                data["failedCount"] = 0
                data["lastFailureTime"] = None
                data["lastFailureReason"] = None
                self._mark_dirty()  # 批量保存
                logger.info(f"[Token] 重置失败计数: {sso[:10]}...")

        except Exception as e:
            logger.error(f"[Token] 重置失败错误: {e}")

    async def apply_cooldown(self, auth_token: str, status_code: int) -> None:
        """应用冷却策略
        - 429 错误：使用时间冷却（有额度1小时，无额度10小时）
        - 其他错误：使用次数冷却（5次请求）
        """
        try:
            sso = self._extract_sso(auth_token)
            if not sso:
                return

            _, data = self._find_token(sso)
            if not data:
                return

            remaining = data.get("remainingQueries", -1)

            if status_code == 429:
                # 429 使用时间冷却
                if remaining > 0 or remaining == -1:
                    # 有额度：冷却1小时
                    cooldown_until = time.time() + COOLDOWN_429_WITH_QUOTA
                    logger.info(f"[Token] 429冷却(有额度): {sso[:10]}... 冷却1小时")
                else:
                    # 无额度：冷却10小时
                    cooldown_until = time.time() + COOLDOWN_429_NO_QUOTA
                    logger.info(f"[Token] 429冷却(无额度): {sso[:10]}... 冷却10小时")
                data["cooldownUntil"] = int(cooldown_until * 1000)
                self._mark_dirty()
            else:
                # 其他错误使用次数冷却（有额度时才冷却）
                if remaining != 0:
                    self._cooldown_counts[sso] = COOLDOWN_REQUESTS
                    logger.info(
                        f"[Token] 次数冷却: {sso[:10]}... 冷却{COOLDOWN_REQUESTS}次请求"
                    )

        except Exception as e:
            logger.error(f"[Token] 应用冷却错误: {e}")

    async def refresh_all_limits(self) -> Dict[str, Any]:
        """刷新所有 Token 的剩余次数"""
        # 检查是否已在刷新
        if self._refresh_lock:
            return {
                "error": "refresh_in_progress",
                "message": "已有刷新任务在进行中",
                "progress": self._refresh_progress,
            }

        # 获取锁
        self._refresh_lock = True

        try:
            # 计算总数
            all_tokens = []
            for token_type in [TokenType.NORMAL.value, TokenType.SUPER.value]:
                for sso in list(self.token_data[token_type].keys()):
                    all_tokens.append((token_type, sso))

            total = len(all_tokens)
            self._refresh_progress = {
                "running": True,
                "current": 0,
                "total": total,
                "success": 0,
                "failed": 0,
            }

            success_count = 0
            fail_count = 0

            for i, (token_type, sso) in enumerate(all_tokens):
                auth_token = f"sso-rw={sso};sso={sso}"
                try:
                    result = await self.check_limits(auth_token, "grok-4-fast")
                    if result:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    logger.warning(f"[Token] 刷新失败: {sso[:10]}... - {e}")
                    fail_count += 1

                # 更新进度
                self._refresh_progress = {
                    "running": True,
                    "current": i + 1,
                    "total": total,
                    "success": success_count,
                    "failed": fail_count,
                }
                await asyncio.sleep(0.1)  # 避免请求过快

            logger.info(f"[Token] 批量刷新完成: 成功{success_count}, 失败{fail_count}")
            self._refresh_progress = {
                "running": False,
                "current": total,
                "total": total,
                "success": success_count,
                "failed": fail_count,
            }
            return {"success": success_count, "failed": fail_count, "total": total}

        finally:
            self._refresh_lock = False

    def get_refresh_progress(self) -> Dict[str, Any]:
        """获取刷新进度"""
        return self._refresh_progress.copy()


# 全局实例
token_manager = GrokTokenManager()
