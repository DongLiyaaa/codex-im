"""Permanent first-user initialization, serialized across API processes."""
from fastapi import HTTPException
from sqlalchemy import select, text
from .models import SetupState, User
from .security import hash_password


def initialized(db):
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 7814362901})
    state = db.get(SetupState, 'initial-admin')
    if state and state.initialized:
        return True
    if db.scalar(select(User.id).limit(1)):
        if state:
            state.initialized = True
        else:
            db.add(SetupState(id='initial-admin', initialized=True))
        db.flush()
        return True
    return False


def create_admin(db, body):
    if initialized(db):
        raise HTTPException(409, 'Setup already completed')
    user = User(email=body.email, name=body.name, password_hash=hash_password(body.password),
                role='super_admin', active=True, onboarding={'status': 'pending', 'step': 'model', 'auto_start': True})
    db.add(user)
    state = db.get(SetupState, 'initial-admin')
    if state:
        state.initialized = True
    else:
        db.add(SetupState(id='initial-admin', initialized=True))
    db.flush()
    return user
