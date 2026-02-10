"""聊天API路由 - OpenAI兼容的聊天接口"""

import time
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Optional, Dict, Any
from fastapi.responses import StreamingResponse

from app.core.auth import auth_manager
from app.core.exception import GrokApiException
from app.core.logger import logger
from app.services.grok.client import GrokClient
from app.models.openai_schema import OpenAIChatRequest
from app.services.request_stats import request_stats
from app.services.request_logger import request_logger


router = APIRouter(prefix="/chat", tags=["聊天"])


@router.post("/completions", response_model=None)
async def chat_completions(
    request: Request,
    body: OpenAIChatRequest, 
    auth_info: Dict[str, Any] = Depends(auth_manager.verify)
):
    """创建聊天补全（支持流式和非流式）"""
    start_time = time.time()
    model = body.model
    ip = request.client.host if request.client else "unknown"
    key_name = auth_info.get("name", "Unknown")
    
    status_code = 200
    error_msg = ""
    
    try:
        logger.info(f"[Chat] 收到聊天请求: {key_name} @ {ip}")

        # 调用Grok客户端
        result = await GrokClient.openai_to_grok(body.model_dump())
        
        # 记录成功统计
        await request_stats.record_request(model, success=True)
        
        # 流式响应
        if body.stream:
            async def stream_wrapper():
                try:
                    async for chunk in result:
                        yield chunk
                finally:
                    # 流式结束记录日志
                    duration = time.time() - start_time
                    await request_logger.add_log(ip, model, duration, 200, key_name)

            return StreamingResponse(
                content=stream_wrapper(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no"
                }
            )
        
        # 非流式响应 - 记录日志
        duration = time.time() - start_time
        await request_logger.add_log(ip, model, duration, 200, key_name)
        return result
        
    except GrokApiException as e:
        status_code = e.status_code or 500
        error_msg = str(e)
        await request_stats.record_request(model, success=False)
        logger.error(f"[Chat] Grok API错误: {e} - 详情: {e.details}")
        
        duration = time.time() - start_time
        await request_logger.add_log(ip, model, duration, status_code, key_name, error=error_msg)
        
        raise HTTPException(
            status_code=status_code,
            detail={
                "error": {
                    "message": error_msg,
                    "type": e.error_code or "grok_api_error",
                    "code": e.error_code or "unknown"
                }
            }
        )
    except Exception as e:
        status_code = 500
        error_msg = str(e)
        await request_stats.record_request(model, success=False)
        logger.error(f"[Chat] 处理失败: {e}")
        
        duration = time.time() - start_time
        await request_logger.add_log(ip, model, duration, status_code, key_name, error=error_msg)
        
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "message": "服务器内部错误",
                    "type": "internal_error",
                    "code": "internal_server_error"
                }
            }
        )
