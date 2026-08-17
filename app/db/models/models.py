from sqlalchemy import Column, DateTime, Integer, String, func

from app.db.engine import Base


class Model(Base):
    __tablename__ = "models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, nullable=False)
    model_name = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())