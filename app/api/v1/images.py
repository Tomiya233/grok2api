"""图片服务API - 提供缓存的图片和视频文件"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.logger import logger
from app.services.grok.cache import image_cache_service, video_cache_service


router = APIRouter()


def _safe_cache_name(img_path: str) -> str:
    """将任意输入路径归一为缓存文件名，避免路径回溯并兼容历史链接。"""
    normalized = img_path.replace("\\", "/")
    return Path(normalized).name


@router.get("/images/{img_path:path}")
async def get_image(img_path: str):
    """获取缓存的图片或视频

    Args:
        img_path: 缓存文件名（通常由原始资源路径中的 / 替换为 - 得到）
    """
    try:
        safe_name = _safe_cache_name(img_path)
        if not safe_name:
            raise HTTPException(status_code=404, detail="File not found")

        # 优先按缓存文件名直接定位，避免历史实现中 '-' -> '/' 反解导致的误判
        is_video = any(safe_name.lower().endswith(ext) for ext in [".mp4", ".webm", ".mov", ".avi"])
        cache_dir = video_cache_service.cache_dir if is_video else image_cache_service.cache_dir
        cache_path = cache_dir / safe_name

        if cache_path.exists():
            logger.debug(f"[MediaAPI] 返回缓存: {cache_path}")
            return FileResponse(
                path=str(cache_path),
                media_type="video/mp4" if is_video else "image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                },
            )

        logger.warning(f"[MediaAPI] 未找到: {safe_name}")
        raise HTTPException(status_code=404, detail="File not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MediaAPI] 获取失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
