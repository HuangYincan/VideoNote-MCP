from app.db.engine import get_db
from app.db.models.models import Model


def get_model_by_provider_and_name(provider_id: int, model_name: str):
    db = next(get_db())
    try:
        model = db.query(Model).filter_by(provider_id=provider_id, model_name=model_name).first()
        if model:
            return {
                "id": model.id,
                "provider_id": model.provider_id,
                "model_name": model.model_name,
                "created_at": model.created_at,
            }
        return None
    finally:
        db.close()


def insert_model(provider_id: int, model_name: str):
    db = next(get_db())
    try:
        model = Model(provider_id=provider_id, model_name=model_name)
        db.add(model)
        db.commit()
        db.refresh(model)
        return {
            "id": model.id,
            "provider_id": model.provider_id,
            "model_name": model.model_name,
            "created_at": model.created_at,
        }
    finally:
        db.close()


def get_models_by_provider(provider_id: int):
    db = next(get_db())
    try:
        models = db.query(Model).filter_by(provider_id=provider_id).all()
        return [{"id": m.id, "model_name": m.model_name} for m in models]
    finally:
        db.close()