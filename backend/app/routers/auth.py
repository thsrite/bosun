"""登录 / 登出 / 改口令。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str


class PasswordBody(BaseModel):
    new_password: str
    current_password: str = ""


@router.get("/status")
def status(request: Request):
    token = auth.token_from_request(request.headers)
    return {
        "enabled": auth.is_enabled(),
        "source": auth.password_source(),
        "authenticated": not auth.is_enabled() or auth.validate_token(token),
        "min_password_length": auth.MIN_PASSWORD_LENGTH,
    }


@router.post("/login")
def login(body: LoginBody, request: Request):
    if not auth.is_enabled():
        return {"token": "", "enabled": False}
    source = request.client.host if request.client else "unknown"
    waiting = auth.lockout_remaining(source)
    if waiting > 0:
        raise HTTPException(status_code=429, detail=f"尝试过于频繁，请 {int(waiting) + 1} 秒后再试")
    if not auth.verify_password(body.password):
        auth.record_login_failure(source)
        raise HTTPException(status_code=401, detail="口令不正确")
    auth.clear_login_failures(source)
    auth.purge_expired()
    return {"token": auth.create_session(), "enabled": True}


@router.post("/logout")
def logout(request: Request):
    auth.revoke(auth.token_from_request(request.headers))
    return {"ok": True}


@router.put("/password")
def set_password(body: PasswordBody, request: Request):
    """设置或修改访问口令。已启用登录时需先验证当前口令。"""
    if auth.password_source() == "env":
        raise HTTPException(status_code=400, detail="口令由环境变量 BOSUN_PASSWORD 提供，请改环境变量后重启后端")
    if auth.is_enabled() and not auth.verify_password(body.current_password):
        raise HTTPException(status_code=401, detail="当前口令不正确")
    new_password = body.new_password.strip()
    if len(new_password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"口令至少 {auth.MIN_PASSWORD_LENGTH} 位")
    auth.set_password(new_password)
    # 改密会踢掉所有会话（含当前这条），立刻发一条新会话，免得自己被挡在外面
    return {"token": auth.create_session(), "enabled": True}


@router.delete("/password")
def disable_password(request: Request):
    """关闭登录。"""
    if auth.password_source() == "env":
        raise HTTPException(status_code=400, detail="口令由环境变量 BOSUN_PASSWORD 提供，请改环境变量后重启后端")
    if auth.is_enabled() and not auth.validate_token(auth.token_from_request(request.headers)):
        raise HTTPException(status_code=401, detail="未登录")
    auth.clear_password()
    return {"ok": True}
