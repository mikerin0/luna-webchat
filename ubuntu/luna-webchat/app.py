from fastapi import FastAPI

from routers import chat, esp32, led, memory, pages, pi, system

app = FastAPI(title="Luna Local Chat")

app.include_router(system.router)
app.include_router(esp32.router)
app.include_router(pi.router)
app.include_router(pages.router)
app.include_router(chat.router)
app.include_router(led.router)
app.include_router(memory.router)
