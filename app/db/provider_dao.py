import json
import os
import sys
from app.db.models.providers import Provider
from app.utils.logger import get_logger
from app.db.engine import get_db

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
        except Exception as e:
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
    except Exception as e:
        logger.error(f"Failed to seed default providers: {e}")
    finally:
        db.close()


def insert_provider(id: str, name: str, api_key: str, base_url: str, logo: str, type_: str, enabled: int = 1):
    db = next(get_db())
    try:
        # 落盘加密（docs/05 #29）；明文兼容：加密失败自动回退明文写入。
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
        logger.error(f"Failed to insert provider: {e}")
    finally:
        db.close()


def get_enabled_providers():
    db = next(get_db())
    try:
        return db.query(Provider).filter_by(enabled=1).all()
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

        for key, value in kwargs.items():
            if hasattr(provider, key):
                if key == "api_key":
                    # 落盘加密（docs/05 #29）。update 通常先读后写（已解密），
                    # 但 enc: 前缀值会二次加密——先解密再加密保持幂等
                    from videonote_mcp.crypto import decrypt_value, encrypt_value

                    if value and str(value).startswith("enc:"):
                        value = decrypt_value(value) or value
                    value = encrypt_value(value)
                setattr(provider, key, value)

        db.commit()
        logger.info(f"Provider updated successfully. id: {id}, updated_fields: {list(kwargs.keys())}")
    except Exception as e:
        logger.error(f"Failed to update provider: {e}")
    finally:
        db.close()


def delete_provider(id: str):
    db = next(get_db())
    try:
        provider = db.query(Provider).filter_by(id=id).first()
        if provider:
            db.delete(provider)
            db.commit()
            logger.info(f"Provider deleted successfully. id: {id}")
    except Exception as e:
        logger.error(f"Failed to delete provider: {e}")
    finally:
        db.close()