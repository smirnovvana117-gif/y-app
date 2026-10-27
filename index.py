# -*- coding: utf-8 -*-
"""Точка входа Vercel (FastAPI). Маршруты работают и с префиксом /api, и без него."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import _core as core

logging.basicConfig(level=logging.INFO)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

dp = Dispatcher(storage=core.TursoStorage())
dp.update.outer_middleware(core.BanMiddleware())
dp.include_router(core.admin)
dp.include_router(core.user)


def make_bot() -> Bot:
    return Bot(core.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))


def route(method: str, path: str):
    def deco(fn):
        for p in (path, "/api" + path):
            app.add_api_route(p, fn, methods=[method])
        return fn
    return deco


def secret_ok(request: Request) -> bool:
    """Секрет в ?s=... / ?secret=... или заголовок Authorization: Bearer <CRON_SECRET> (так ходит Vercel Cron)."""
    q = request.query_params.get("s") or request.query_params.get("secret")
    if core.WEBHOOK_SECRET and q == core.WEBHOOK_SECRET:
        return True
    auth = request.headers.get("authorization", "")
    return bool(core.CRON_SECRET) and auth == f"Bearer {core.CRON_SECRET}"


@app.get("/")
async def root():
    return {"ok": True}


@route("POST", "/webhook")
async def webhook(request: Request):
    if core.WEBHOOK_SECRET and request.headers.get("x-telegram-bot-api-secret-token") != core.WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    data = await request.json()
    bot = make_bot()
    try:
        await core.ensure_ready(bot)
        await dp.feed_raw_update(bot, data)
    except Exception:
        logging.exception("webhook")  # всегда отвечаем 200, чтобы Telegram не слал повторы
    finally:
        await bot.session.close()
    return {"ok": True}


@route("GET", "/cron")
async def cron(request: Request):
    """Проверка отписок (Flyer/TGrass/каналы админа/сервисы). Дёргается Vercel Cron или внешним таймером."""
    if not secret_ok(request):
        return JSONResponse({"ok": False}, status_code=403)
    bot = make_bot()
    try:
        await core.ensure_ready(bot)
        report = await core.check_unsubs(bot)
        return {"ok": True, "report": report}
    except Exception as ex:
        logging.exception("cron")
        return JSONResponse({"ok": False, "error": repr(ex)}, status_code=500)
    finally:
        await bot.session.close()


@route("POST", "/hook/{provider}")
async def provider_hook(provider: str, request: Request):
    """Вебхуки об отписках: PiarFlow, Trafsly, Traffy. URL в кабинете сервиса: https://ДОМЕН/api/hook/<сервис>?s=СЕКРЕТ"""
    if not secret_ok(request):
        return JSONResponse({"ok": False}, status_code=403)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    bot = make_bot()
    try:
        await core.ensure_ready(bot)
        n = await core.handle_provider_hook(bot, provider, payload)
        logging.info("hook %s -> штрафов: %s", provider, n)
    except Exception:
        logging.exception("hook %s", provider)
    finally:
        await bot.session.close()
    return {"ok": True}


@route("GET", "/setup")
async def setup(request: Request):
    """Один раз после деплоя: открой https://ДОМЕН/api/setup?secret=СЕКРЕТ — создаст таблицы и подключит вебхук."""
    if not secret_ok(request):
        return JSONResponse({"ok": False, "error": "неверный secret"}, status_code=403)
    if not core.BOT_TOKEN:
        return JSONResponse({"ok": False, "error": "BOT_TOKEN не задан"}, status_code=500)
    bot = make_bot()
    try:
        await core.ensure_ready(bot)  # проверяет Turso и создаёт таблицы
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        url = f"https://{host}/api/webhook"
        await bot.set_webhook(url, secret_token=core.WEBHOOK_SECRET or None,
                              allowed_updates=dp.resolve_used_update_types())
        info = await bot.get_webhook_info()
        keys = {k: bool(getattr(core, k)) for k in
                ("FLYER_KEY", "TGRASS_KEY", "PIARFLOW_KEY", "TRAFSLY_KEY", "TRAFFY_KEY")}
        return {"ok": True, "webhook": info.url, "pending": info.pending_update_count,
                "last_error": info.last_error_message, "keys": keys, "admins": len(core.ADMIN_IDS)}
    except Exception as ex:
        logging.exception("setup")
        return JSONResponse({"ok": False, "error": repr(ex)}, status_code=500)
    finally:
        await bot.session.close()
