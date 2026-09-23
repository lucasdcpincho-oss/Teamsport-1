import os
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr

from db import db
from security import (
    hash_password, verify_password, create_access_token, create_refresh_token,
    set_auth_cookies, clear_auth_cookies, decode_token, get_current_user, public_user,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


class RegisterInput(BaseModel):
    name: str
    email: EmailStr
    password: str
    phone: str = ""


class LoginInput(BaseModel):
    identifier: str
    password: str


async def check_lockout(identifier: str):
    rec = await db.login_attempts.find_one({"identifier": identifier})
    if rec and rec.get("count", 0) >= MAX_ATTEMPTS:
        last = rec.get("last_attempt")
        if last:
            if isinstance(last, str):
                last = datetime.fromisoformat(last)
            if datetime.now(timezone.utc) - last < timedelta(minutes=LOCKOUT_MINUTES):
                raise HTTPException(status_code=429, detail="Demasiadas tentativas. Tenta novamente dentro de 15 minutos.")


async def record_failure(identifier: str):
    await db.login_attempts.update_one(
        {"identifier": identifier},
        {"$inc": {"count": 1}, "$set": {"last_attempt": datetime.now(timezone.utc)}},
        upsert=True,
    )


@router.post("/register")
async def register(data: RegisterInput, response: Response):
    email = data.email.lower()
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="A password deve ter pelo menos 6 caracteres")
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Este email jÃ¡ estÃ¡ registado")
    user = {
        "id": str(uuid.uuid4()),
        "name": data.name.strip(),
        "email": email,
        "phone": data.phone.strip(),
        "password_hash": hash_password(data.password),
        "role": "customer",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(user)
    set_auth_cookies(response, create_access_token(user["id"], email, "customer"), create_refresh_token(user["id"]))
    return public_user(user)


@router.post("/login")
async def login(data: LoginInput, request: Request, response: Response):
    ident = data.identifier.strip()
    ip = request.client.host if request.client else "unknown"
    lock_id = f"{ip}:{ident.lower()}"
    await check_lockout(lock_id)
    user = await db.users.find_one(
        {"$or": [{"email": ident.lower()}, {"username": ident}]}, {"_id": 0}
    )
    if not user or not verify_password(data.password, user.get("password_hash", "")):
        await record_failure(lock_id)
        raise HTTPException(status_code=401, detail="Utilizador/email ou password incorretos")
    await db.login_attempts.delete_one({"identifier": lock_id})
    set_auth_cookies(response, create_access_token(user["id"], user["email"], user.get("role", "customer")), create_refresh_token(user["id"]))
    return public_user(user)


@router.post("/logout")
async def logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = await get_current_user(request)
    return public_user(user)


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        import jwt as _jwt
        payload = decode_token(token)
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except _jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    access = create_access_token(user["id"], user["email"], user.get("role", "customer"))
    response.set_cookie(key="access_token", value=access, httponly=True, secure=True, samesite="none", max_age=43200, path="/")
    return {"ok": True}


async def seed_admin():
    admin_username = os.environ.get("ADMIN_USERNAME")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    admin_email = os.environ.get("ADMIN_EMAIL")
    if not admin_username or not admin_password or not admin_email:
        return
    admin_email = admin_email.lower()
    existing = await db.users.find_one({"$or": [{"username": admin_username}, {"email": admin_email}, {"role": {"$in": ["admin", "worker"]}}]})
    if existing is None:
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": admin_email,
            "username": admin_username,
            "password_hash": hash_password(admin_password),
            "name": "Team Sport â€” GestÃ£o",
            "phone": "",
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    else:
        update = {"username": admin_username, "email": admin_email, "role": "admin"}
        if not verify_password(admin_password, existing.get("password_hash", "")):
            update["password_hash"] = hash_password(admin_password)
        await db.users.update_one({"id": existing["id"]}, {"$set": update})
    # apenas esta conta pode gerir a loja
    await db.users.update_many(
        {"role": {"$in": ["admin", "worker"]}, "username": {"$ne": admin_username}},
        {"$set": {"role": "customer"}},
    )
