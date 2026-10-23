"""The routes for importing history in Settings › Cars and sources (see importjob.py).

Every write answers with a redirect back to the section, which shows the job's state; the
upload and the status poll answer in JSON. The page itself is rendered by settings_render."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

BACK = "/settings/cars#import"


def cars_of(url: str, tm_connect) -> tuple[list[dict], str | None]:
    """A TeslaMate database's cars (with their ids, which the import reads by), or the reason it cannot be read."""
    from sources import scrub
    from teslamate_live import list_cars

    url = (url or "").strip()
    if not url.startswith(("postgresql://", "postgres://")):
        return [], "A TeslaMate database address starts with postgresql://"
    try:
        with tm_connect(url) as tm:
            return list_cars(tm), None
    except Exception as err:  # noqa: BLE001 - say what failed, never the password
        first = str(err).strip().splitlines()[0] if str(err).strip() else type(err).__name__
        return [], scrub(f"Could not read that database: {first}", url)[:200]


def build(store, to_site, tm_connect, saved_url) -> APIRouter:
    r = APIRouter()

    def back(request: Request):
        return to_site(request, BACK)

    @r.post("/settings/import/upload")
    async def upload(request: Request, name: str = ""):
        """One file as the raw body, written to disk as it arrives (the page sends it with fetch, so there is no multipart to buffer)."""
        length = request.headers.get("content-length")
        up, why = store.begin(name, int(length) if (length or "").isdigit() else None)
        if up is None:
            return JSONResponse({"ok": False, "error": why}, status_code=413 if "limit" in why else 400)
        try:
            async for chunk in request.stream():
                why = await asyncio.to_thread(up.write, chunk)
                if why:
                    break
            if not why:
                why = await asyncio.to_thread(up.finish)
            else:
                up.abort()
        except BaseException:
            up.abort()
            raise
        if why:
            return JSONResponse({"ok": False, "error": why}, status_code=413 if "limit" in why else 400)
        return {"ok": True, "name": up.name}

    @r.post("/settings/import/start")
    async def start(request: Request):
        why = await asyncio.to_thread(store.start_teslafi)
        if why:
            store.say("err", why)
        return back(request)

    @r.post("/settings/import/remove")
    async def remove(request: Request):
        form = await request.form()
        store.unstage(form.get("name") or None)
        return back(request)

    @r.post("/settings/import/teslamate")
    async def teslamate(request: Request):
        form = await request.form()
        url = (form.get("url") or "").strip()
        if not url and form.get("saved"):
            url = await asyncio.to_thread(saved_url) or ""
        if not url:
            store.say("err", "A TeslaMate database address is needed.")
            return back(request)
        cars, why = await asyncio.to_thread(cars_of, url, tm_connect)
        why = why or await asyncio.to_thread(store.start_teslamate, url, cars)
        if why:
            store.say("err", why)
            store.typed = None if form.get("saved") and not (form.get("url") or "").strip() else url
        return back(request)

    @r.post("/settings/import/resume")
    async def resume(request: Request):
        form = await request.form()
        why = await asyncio.to_thread(store.resume, form.get("restart") == "1")
        if why:
            store.say("err", why)
        return back(request)

    @r.post("/settings/import/dismiss")
    async def dismiss(request: Request):
        await asyncio.to_thread(store.dismiss)
        return back(request)

    @r.get("/settings/import/status")
    async def status():
        return JSONResponse(store.view(), headers={"Cache-Control": "no-store"})

    return r
