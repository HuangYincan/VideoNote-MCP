import uuid
from typing import Optional

from app.db.models.providers import Provider
from app.db.provider_dao import (
    insert_provider,
    get_all_providers,
    get_provider_by_name,
    get_provider_by_id,
    update_provider,
    delete_provider,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ProviderService:

    @staticmethod
    def serialize_provider(row: Provider) -> dict:
        if not row:
            return None
        row = ProviderService.provider_to_dict(row)
        return {
            "id": row.get("id"),
            "name": row.get("name"),
            "logo": row.get("logo"),
            "type":row.get("type"),
            "enabled": row.get("enabled"),
            "base_url": row.get("base_url"),
            "api_key": row.get("api_key"),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            # "name": row[1],
            # "logo": row[2],
            # "type": row[3],
            # "api_key": row[4],
            # "base_url": row[5],
            # "enabled": row[6],
            # "created_at": row[7],
        }
    @staticmethod
    def serialize_provider_safe(row: Provider) -> dict:
        if not row:
            return None
        row = ProviderService.provider_to_dict(row)

        return {
            "id": row.get("id"),
            "name": row.get("name"),
            "logo": row.get("logo"),
            "type":row.get("type"),
            "enabled": row.get("enabled"),
            "base_url": row.get("base_url"),
            "api_key":  ProviderService.mask_key(row.get("api_key")),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,

            # "id": row[0],
            # "name": row[1],
            # "logo": row[2],
            # "type": row[3],
            # "api_key": ProviderService.mask_key(row[4]),
            # "base_url": row[5],
            # "enabled": row[6],
            # "created_at": row[7],
        }
    @staticmethod
    def mask_key(key: str) -> str:
        # 固定首尾显式位数、中间全掩码，避免 len==8 时 `'*'*0` 整条泄露
        if not key:
            return ''
        n = len(key)
        if n <= 4:
            return '*' * n
        head, tail = (2, 2) if n < 16 else (4, 4)
        return key[:head] + '*' * (n - head - tail) + key[-tail:]
    @staticmethod
    def add_provider( name: str, api_key: str, base_url: str, logo: str, type_: str, enabled: int = 1):
        try:
            # 内置供应商（type='built-in'）只能由 seed 流程写入；API 创建一律落到 'custom'，
            # 否则历史上出现过批量伪内置脏数据
            if type_ != 'custom':
                type_ = 'custom'
            existing = get_provider_by_name(name)
            if existing is not None:
                raise ValueError(f'供应商名称已存在: {name}')
            id = uuid.uuid4().hex
            logo = 'custom'
            return insert_provider(id, name, api_key, base_url, logo, type_, enabled)
        except Exception as  e:
            print('创建模式失败',e)
            raise
    @staticmethod
    def provider_to_dict(p: Provider):
        from videonote_mcp.crypto import decrypt_value  # 惰性：vendored 层不强制依赖 videonote_mcp

        return {
            "id": p.id,
            "name": p.name,
            "logo": p.logo,
            "type": p.type,
            "api_key": decrypt_value(p.api_key),
            "base_url": p.base_url,
            "enabled": p.enabled,
            "created_at": p.created_at,
        }
    @staticmethod
    def get_all_providers():
        rows = get_all_providers()
        if rows is None:
            return []

        return [ProviderService.serialize_provider(row) for row in rows] if rows else []
    @staticmethod
    def get_all_providers_safe():
        rows = get_all_providers()

        # 注意：必须用 serialize_provider_safe（掩码 api_key）。上游此处在用
        # serialize_provider 的 bug 已在本仓库修复，否则 list_providers 会泄完整 key。
        return [ProviderService.serialize_provider_safe(row) for row in rows] if (rows) else []
    @staticmethod
    def get_provider_by_name(name: str):
        row = get_provider_by_name(name)
        return ProviderService.serialize_provider(row)

    @staticmethod
    def get_provider_by_id(id: str):  # 已改为 str 类型
        row = get_provider_by_id(id)
        return ProviderService.serialize_provider(row)

    @staticmethod
    def get_provider_by_id_safe(id: str):  # 已改为 str 类型
        row = get_provider_by_id(id)
        return ProviderService.serialize_provider_safe(row)
            # all_models.extend(provider['models'])

    @staticmethod
    def update_provider(id: str, data: dict) -> Optional[dict]:
        try:
            # 过滤掉空值
            filtered_data = {k: v for k, v in data.items() if v is not None and k != 'id'}
            # 防御掩码污染：前端展示时 api_key 被 mask_key() 处理过（如 a92f****...2d3a），
            # 如果用户未重新输入直接保存，带星号的值不应覆盖原 key。
            if 'api_key' in filtered_data and '*' in str(filtered_data.get('api_key', '')):
                filtered_data.pop('api_key')
            # 打码 api_key，避免 key 泄进日志
            _log_data = {
                k: (str(v)[:4] + '****' if k == 'api_key' and v else v)
                for k, v in filtered_data.items()
            }
            logger.info('更新模型供应商 %s', _log_data)
            update_provider(id, **filtered_data)
            # 获取更新后的供应商信息：get_provider_by_id 是模块级导入的 DAO，
            # 返回 ORM 对象，用属性访问（.get 反而会 AttributeError）。
            # 供应商不存在时必须返回 None（此前返回 {'id':…, 'enabled': None} 恒真，
            # CLI/MCP 的 `if not updated` 判空永不生效，会把不存在报成「已更新」）。
            updated_provider = get_provider_by_id(id)
            if updated_provider is None:
                return None
            return {
                'id': id,
                'enabled': updated_provider.enabled,
            }

        except Exception as e:
            # 真实异常（DB 锁/连接等）必须透传：此前 print + return None 把原因吞掉，
            # 调用方拿到 None 只能报「供应商不存在」——DB 问题被误报成不存在（#123 B8）。
            logger.error('更新模型供应商失败 %s: %s', id, e, exc_info=True)
            raise ValueError(f"更新供应商 {id} 失败: {e}") from e

    @staticmethod
    def delete_provider(id: str):
        return delete_provider(id)
