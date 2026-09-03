import json
import os
import sys

from app.db.engine import get_db
from app.db.models.models import Model
from app.db.models.providers import Provider
from app.utils.logger import get_logger

logger = get_logger(__name__)


def get_builtin_providers_path():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(__file__)
    return os.path.join(base_path, 'builtin_providers.json')


def seed_default_providers():
    db = next(get_db())
    try:
        if db.query(Provider).count() > 0:
            logger.info("Providers already exist, skipping seed.")
            return

        json_path = get_builtin_providers_path()
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                providers = json.load(f)
        except Exception as e:  # noqa: BLE001 - seed data is treated as an optional bootstrap
            logger.error(f"Failed to read builtin_providers.json: {e}")
            return

        for p in providers:
            db.add(Provider(
                id=p['id'],
                name=p['name'],
                api_key=p['api_key'],
                base_url=p['base_url'],
                logo=p['logo'],
                type=p['type'],
                enabled=p.get('enabled', 1)
            ))
        db.commit()
        logger.info("Default providers seeded successfully.")
    except Exception as e:  # noqa: BLE001 - preserve the DAO's existing error boundary
        db.rollback()
        logger.error(f"Failed to seed default providers: {e}")
    finally:
        db.close()


def insert_provider(id: str, name: str, api_key: str, base_url: str, logo: str, type_: str, enabled: int = 1):
    db = next(get_db())
    try:
        # 落盘加密（docs/05 #29）；加密失败必须在 add/commit 前中止，绝不明文写入。
        # 惰性 import：app/ 是 vendored 层，不强制依赖 videonote_mcp（上游可独立跑）
        from videonote_mcp.crypto import encrypt_value

        provider = Provider(
            id=id, name=name, api_key=encrypt_value(api_key), base_url=base_url,
            logo=logo, type=type_, enabled=enabled,
        )
        db.add(provider)
        db.commit()
        logger.info(f"Provider inserted successfully. id: {id}, name: {name}, type: {type_}")
        return id
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to insert provider: {e}")
        raise
    finally:
        db.close()


def get_provider_by_name(name: str):
    db = next(get_db())
    try:
        return db.query(Provider).filter_by(name=name).first()
    finally:
        db.close()


def get_provider_by_id(id: str):
    db = next(get_db())
    try:
        return db.query(Provider).filter_by(id=id).first()
    finally:
        db.close()


def get_all_providers():
    db = next(get_db())
    try:
        return db.query(Provider).all()
    finally:
        db.close()


def update_provider(id: str, **kwargs):
    db = next(get_db())
    try:
        provider = db.query(Provider).filter_by(id=id).first()
        if not provider:
            logger.warning(f"Provider {id} not found for update.")
            return

        # 先完成所有敏感字段转换，再触碰 ORM 对象。这样后续字段加密失败时，
        # 不会留下「前面的普通字段已改、但本次调用仍继续提交」的半更新。
        updates = {}
        for key, value in kwargs.items():
            if hasattr(provider, key):
                if key == "api_key":
                    # 落盘加密（docs/05 第 29）：update 通常先读后写（已解密），
                    # 但 enc: 前缀值会二次加密——先解密再加密保持幂等。
                    # 解密失败（key 缺失/不匹配）时中止整个更新而非二次加密：
                    # 二次加密会把 enc: 串再包一层，产生永远解不出的数据（docs 审计 G1）
                    from videonote_mcp.crypto import (
                        EncryptionError,
                        decrypt_value,
                        encrypt_value,
                    )

                    if isinstance(value, str) and value.startswith("enc:"):
                        decrypted = decrypt_value(value)
                        if decrypted is None:
                            raise EncryptionError(
                                f"Provider {id} 的 api_key 无法解密（fernet.key 缺失/不匹配）"
                            )
                        value = encrypt_value(decrypted)
                    else:
                        value = encrypt_value(value)
                updates[key] = value

        for key, value in updates.items():
            setattr(provider, key, value)

        db.commit()
        logger.info(f"Provider updated successfully. id: {id}, updated_fields: {list(kwargs.keys())}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update provider: {e}")
        raise
    finally:
        db.close()


def delete_provider(id: str):
    db = next(get_db())
    try:
        provider = db.query(Provider).filter_by(id=id).first()
        if provider:
            # 显式删除关联模型，避免不同数据库后端对外键级联行为不一致。
            db.query(Model).filter_by(provider_id=id).delete(
                synchronize_session=False
            )
            db.delete(provider)
            db.commit()
            logger.info(f"Provider deleted successfully. id: {id}")
    except Exception as e:
        logger.error(f"Failed to delete provider: {e}")
        db.rollback()
        raise
    finally:
        db.close()
