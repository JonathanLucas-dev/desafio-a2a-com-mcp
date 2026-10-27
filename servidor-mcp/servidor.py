"""Servidor MCP da Central de Salas: Streamable HTTP stateless em /mcp.

O MRTR da reserva mora no resolver `escolha_de_sala`: quando o intervalo esta ocupado
ele devolve um `Elicit`, e o SDK termina a resposta com `resultType: input_required`
mais um `requestState` selado (AES-GCM, expiracao, amarrado ao tool e aos argumentos).
O servidor nao guarda nada entre as duas rodadas.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError

import dominio

NOME = "central-de-salas"
TTL_REQUEST_STATE = 600.0  # 10 minutos, dentro da faixa de 5 a 30 exigida

agenda = dominio.carregar()


def segredo() -> bytes:
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not bruto:
        sys.exit("REQUEST_STATE_SECRET ausente. Gere: python -c \"import secrets; print(secrets.token_hex(32))\"")
    try:
        chave = bytes.fromhex(bruto)
    except ValueError:
        chave = bruto.encode()
    if len(chave) < 32:
        sys.exit("REQUEST_STATE_SECRET precisa ter no minimo 32 bytes de aleatoriedade (64 caracteres hex)")
    return chave


mcp = MCPServer(
    NOME,
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[segredo()], ttl=TTL_REQUEST_STATE, audience=NOME),
)


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class Escolha(BaseModel):
    sala: str


def _validar(sala: str, inicio: str, fim: str) -> None:
    try:
        agenda.validar(sala, inicio, fim)
    except dominio.ErroDeRegra as e:
        raise ToolError(str(e)) from None


def escolha_de_sala(sala: str, inicio: str, fim: str):
    """Pergunta qual sala usar quando a pedida esta ocupada; sem conflito, devolve a propria sala."""
    _validar(sala, inicio, fim)
    if not agenda.conflitos(sala, inicio, fim):
        return Escolha(sala=sala)
    alternativas = agenda.alternativas(sala, inicio, fim)
    if not alternativas:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    esquema = create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(alternativas)], Field(description="Sala alternativa escolhida", title="Sala")),
    )
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", esquema)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**vars(s)) for s in agenda.salas])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    _validar(sala, inicio, fim)
    conflitos = agenda.conflitos(sala, inicio, fim)
    return Disponibilidade(sala=sala, livre=not conflitos, conflitos=[ConflitoOut(**vars(c)) for c in conflitos])


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Escolha], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        return ReservaOut(reservado=False, motivo="recusado")
    destino = escolha.data.sala
    _validar(destino, inicio, fim)
    if agenda.conflitos(destino, inicio, fim):
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    r = agenda.criar(destino, inicio, fim, responsavel)
    return ReservaOut(
        reserva=r.id, sala=r.sala, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel, politica=agenda.politica_versao
    )


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    return agenda.politica_texto


LIMITE_LOG = 1_048_576


def _repetir(lidas: list, receive):
    """Devolve ao app as mensagens ASGI ja lidas e depois segue lendo do receive original."""
    pendentes = list(lidas)

    async def replay():
        return pendentes.pop(0) if pendentes else await receive()

    return replay


class RegistroDeRequests:
    """Middleware ASGI: uma linha no stderr por request, com metodo, id, nome e traceparent."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        # Le so o necessario para registrar; o que passar do limite segue direto para o SDK, que aplica o seu.
        lidas, corpo, completo = [], b"", False
        while True:
            msg = await receive()
            lidas.append(msg)
            if msg["type"] != "http.request":
                break
            corpo += msg.get("body", b"")
            if not msg.get("more_body", False):
                completo = True
                break
            if len(corpo) > LIMITE_LOG:
                break
        if not completo:
            print("[mcp] request grande ou interrompido, sem detalhes", file=sys.stderr, flush=True)
            return await self.app(scope, _repetir(lidas, receive), send)
        try:
            req = json.loads(corpo)
            params = req.get("params") or {}
            meta = params.get("_meta") or {}
            linha = (
                f"[mcp] metodo={req.get('method')} id={req.get('id')} nome={params.get('name') or params.get('uri') or '-'} "
                f"retry={'sim' if 'requestState' in params else 'nao'} traceparent={meta.get('traceparent', '-')}"
            )
        except (ValueError, AttributeError):
            linha = "[mcp] request ilegivel"
        print(linha, file=sys.stderr, flush=True)

        await self.app(scope, _repetir(lidas, receive), send)


def main() -> None:
    app = RegistroDeRequests(mcp.streamable_http_app(stateless_http=True, json_response=True))
    uvicorn.run(app, host=os.environ.get("MCP_HOST", "127.0.0.1"), port=int(os.environ.get("MCP_PORT", "7301")), log_level="warning")


if __name__ == "__main__":
    main()
