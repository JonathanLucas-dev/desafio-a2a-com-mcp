"""Agente da Central de Salas: servidor A2A v1.0 (JSON-RPC) por fora, host MCP por dentro."""

from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cliente_mcp import ClienteMCP
from ponte import OperacaoNaoSuportada, Ponte, TarefaNaoEncontrada

PORTA = int(os.environ.get("AGENT_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENT_PUBLIC_URL", f"http://localhost:{PORTA}").rstrip("/")
URL_MCP = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")

mcp = ClienteMCP(URL_MCP)
ponte = Ponte(mcp)


def cartao() -> dict:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [{"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


def erro(id_, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


async def card(_: Request) -> JSONResponse:
    return JSONResponse(cartao())


async def rpc(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except ValueError:
        return erro(None, -32700, "JSON invalido")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or "method" not in corpo:
        return erro(corpo.get("id") if isinstance(corpo, dict) else None, -32600, "Request invalido")
    id_, metodo, params = corpo.get("id"), corpo["method"], corpo.get("params") or {}
    try:
        if metodo == "SendMessage":
            mensagem = params.get("message")
            if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
                return erro(id_, -32602, "message.parts obrigatorio")
            m = TRACEPARENT.match(request.headers.get("traceparent", ""))
            task = await ponte.enviar(mensagem, m.group(1) if m else None)
            return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {"task": task.para_a2a()}})
        if metodo == "GetTask":
            task = ponte.obter(str(params.get("id", "")))
            return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {"task": task.para_a2a()}})
    except TarefaNaoEncontrada:
        return erro(id_, -32001, "Task nao encontrada")
    except OperacaoNaoSuportada as e:
        return erro(id_, -32004, str(e))
    return erro(id_, -32601, f"Metodo nao suportado: {metodo}")


@asynccontextmanager
async def ciclo_de_vida(_: Starlette):
    yield
    await mcp.fechar()


app = Starlette(
    lifespan=ciclo_de_vida,
    routes=[
        Route("/.well-known/agent-card.json", card, methods=["GET"]),
        Route("/a2a", rpc, methods=["POST"]),
    ],
)

if __name__ == "__main__":
    print(f"agente em {URL_PUBLICA}, MCP em {URL_MCP}", file=sys.stderr, flush=True)
    uvicorn.run(app, host=os.environ.get("AGENT_HOST", "127.0.0.1"), port=PORTA, log_level="warning")
