from sqlalchemy import Column, String, Integer, DateTime, func

from app.db.engine import Base


class Provider(Base):
    __tablename__ = "providers"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    logo = Column(String, nullable=False)
    type = Column(String, nullable=False)
    api_key = Column(String, nullable=False)
    base_url = Column(String, nullable=False)
    enabled = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())