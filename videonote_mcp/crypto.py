"""敏感信息机器级加密（docs/05 #29）：providers.api_key / app_config.hf_token 落盘加密。

密钥文件存 `VIDEONOTE_CONFIG_DIR/fernet.key`（0600），与 app_config.json 同目录。
卸载/升级不动 config/ → key 随配置保留，已加密数据升级后仍可解（设计红线：绝不丢配置；
只有显式 `cleanup(include_config=True)`（全局清理）才清掉 key）。

密文统一带 `enc:` 前缀：
- `decrypt_value` 对无前缀的值原样返回 —— 明文兼容迁移：历史明文照常工作，
  下次写入自然升级为密文，无需一次性迁移；
- key 丢失/损坏/跨机器时解密失败返回 None，调用方按「无 key」处理，不抛异常
  （换机器本来就该重新填 key）。

实现细节：`cryptography.fernet.Fernet`；key 惰性创建（首次写入时生成），
读取失败（无 key 文件）时返回 None 而不是自动新建 —— 避免「只读场景把脏 key 写出去」。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PREFIX = "enc:"


def _key_path() -> Path:
    config_dir = Path(os.environ.get("VIDEONOTE_CONFIG_DIR", "config"))
    return config_dir / "fernet.key"


def get_key() -> Optional[bytes]:
    """读取机器级密钥；不存在返回 None（调用方按明文兼容/未配置处理）。"""
    path = _key_path()
    try:
        data = path.read_bytes().strip()
        if len(data) != 44:
            logger.warning("fernet.key 长度异常（%d），忽略", len(data))
            return None
        return data
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("读取 fernet.key 失败: %s", exc)
        return None


def _ensure_key() -> Optional[bytes]:
    """读或创建密钥（0600）。创建失败（只读目录等）返回 None。

    并发安全：用 O_CREAT|O_EXCL 独占创建，两个进程/线程同时首写时只有一个赢，
    败者重读现有 key —— 避免「各自生成 K1/K2、后 replace 者赢，先加密的值永不可解」
    （配置绝不丢的红线，docs 审计 G1）。
    """
    key = get_key()
    if key:
        return key
    try:
        from cryptography.fernet import Fernet

        path = _key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        logger.info("已生成机器级加密密钥 %s", path)
        return key
    except FileExistsError:
        # 并发首写：另一个进程刚建好，用它的 key（已加密值用它加密）
        key = get_key()
        if key:
            return key
        logger.warning("fernet.key 并发创建冲突且重读失败")
        return None
    except Exception as exc:  # noqa: BLE001 —— 加密不可用不阻断，回退明文
        logger.warning("生成 fernet.key 失败，回退明文存储: %s", exc)
        return None


def encrypt_value(plaintext: Optional[str]) -> Optional[str]:
    """加密值（带 enc: 前缀）。空串/None 原样返回（空 key 无需加密）。"""
    if not plaintext:
        return plaintext
    key = _ensure_key()
    if key is None:
        return plaintext
    try:
        from cryptography.fernet import Fernet

        token = Fernet(key).encrypt(plaintext.encode("utf-8")).decode("utf-8")
        return _PREFIX + token
    except Exception as exc:  # noqa: BLE001 —— 加密失败回退明文，保证写入不丢
        logger.warning("加密失败，回退明文: %s", exc)
        return plaintext


def decrypt_value(stored: Optional[str]) -> Optional[str]:
    """解密 enc: 前缀的值；无前缀原样返回（明文兼容）；解密失败返回 None。"""
    if not stored or not stored.startswith(_PREFIX):
        return stored
    key = get_key()
    if key is None:
        logger.warning("存在加密数据但 fernet.key 缺失（跨机器/被清理？），无法解密")
        return None
    try:
        from cryptography.fernet import Fernet, InvalidToken

        return Fernet(key).decrypt(stored[len(_PREFIX):].encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        logger.warning("解密失败（key 不匹配或数据损坏），按无 key 处理: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("解密异常，按无 key 处理: %s", exc)
        return None
