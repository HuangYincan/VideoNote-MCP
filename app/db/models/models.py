from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func

from app.db.engine import Base


class Model(Base):
    __tablename__ = "models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Provider.id 是字符串（内置 provider id / 自定义 uuid），模型表必须使用同一
    # 类型并建立外键；此前 Integer 只在 SQLite 的宽松类型系统里「看起来能用」，
    # 在严格数据库或 provider 删除时会产生漂移/孤儿模型。
    provider_id = Column(String, ForeignKey("providers.id"), nullable=False)
    model_name = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
