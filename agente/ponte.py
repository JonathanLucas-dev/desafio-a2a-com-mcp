"""A ponte: onde o estado do MCP (requestState) e o estado do A2A (Task) se encontram.

- `Ponte._aplicar_resultado` transforma `resultType: input_required` em
  TASK_STATE_INPUT_REQUIRED e guarda o requestState na `Pausa` ligada a Task.
- `Ponte.continuar` devolve esse requestState ao servidor MCP, num tools/call novo (id novo).
O agente traduz protocolo; conflito, politica e alternativas sao decisao do servidor.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from dataclasses import dataclass, field

from cliente_mcp import ClienteMCP, ErroMCP

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = {COMPLETED, CANCELED, FAILED}

USO = "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
CAMPOS = ("sala", "inicio", "fim", "responsavel")
TOOL = "reservar_sala"


class TarefaNaoEncontrada(Exception):
    pass


class OperacaoNaoSuportada(Exception):
    pass


@dataclass
class Pausa:
    """Estado MCP pendente de uma Task. Nunca sai pelo A2A."""

    chave: str
    alternativas: list[str]
    request_state: str


@dataclass
class Task:
    id: str
    context_id: str
    trace_id: str
    estado: str = SUBMITTED
    status_msg: dict | None = None
    historico: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    argumentos: dict = field(default_factory=dict)
    pausa: Pausa | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    def para_a2a(self) -> dict:
        status: dict = {"state": self.estado}
        if self.status_msg:
            status["message"] = self.status_msg
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": list(self.historico),
            "artifacts": list(self.artifacts),
        }

    def responder(self, estado: str, texto: str) -> None:
        mensagem = {
            "messageId": f"msg-{secrets.token_hex(6)}",
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        self.estado = estado
        self.status_msg = mensagem
        self.historico.append(mensagem)


def interpretar_pedido(texto: str) -> dict | None:
    m = re.fullmatch(r"\s*reservar\s+(.*?)\s*", texto, re.DOTALL)
    if not m:
        return None
    partes = re.split(r"(?:^|\s)(sala|inicio|fim|responsavel)=", m.group(1))
    # partes: ['', chave, valor, chave, valor, ...]
    if partes[0].strip():
        return None
    campos = {partes[i]: partes[i + 1].strip() for i in range(1, len(partes) - 1, 2)}
    if set(campos) != set(CAMPOS) or not all(campos.values()):
        return None
    return campos


def interpretar_escolha(texto: str) -> str | None:
    m = re.fullmatch(r"\s*escolha=(\S+)\s*", texto)
    return m.group(1) if m else None


def linha_de_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


def _texto_da_tool(resultado: dict) -> str:
    textos = [p.get("text", "") for p in resultado.get("content", []) if p.get("type") == "text"]
    return " ".join(textos) or "erro na tool"


class Ponte:
    def __init__(self, mcp: ClienteMCP) -> None:
        self.mcp = mcp
        self.tasks: dict[str, Task] = {}

    def obter(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise TarefaNaoEncontrada(task_id)
        return task

    # --- A2A: SendMessage -------------------------------------------------

    async def enviar(self, mensagem: dict, trace_id: str | None) -> Task:
        texto = " ".join(p.get("text", "") for p in mensagem.get("parts", []) if isinstance(p, dict))
        task_id = mensagem.get("taskId")
        if task_id:
            task = self.obter(task_id)
            async with task.trava:
                if task.estado in TERMINAIS:
                    raise OperacaoNaoSuportada(f"Task {task.id} ja esta em estado terminal ({task.estado})")
                if task.estado != INPUT_REQUIRED or task.pausa is None:
                    raise OperacaoNaoSuportada(f"Task {task.id} nao esta aguardando entrada ({task.estado})")
                if trace_id:
                    task.trace_id = trace_id
                task.historico.append({**mensagem, "taskId": task.id})
                await self.continuar(task, texto)
            return task

        task = Task(
            id=f"task-{secrets.token_hex(6)}",
            context_id=f"ctx-{secrets.token_hex(6)}",
            trace_id=trace_id or secrets.token_hex(16),
        )
        task.historico.append(mensagem)
        self.tasks[task.id] = task
        async with task.trava:
            await self.iniciar(task, texto)
        return task

    # --- Pedido novo ------------------------------------------------------

    async def iniciar(self, task: Task, texto: str) -> None:
        argumentos = interpretar_pedido(texto)
        if argumentos is None:
            task.responder(FAILED, USO)
            return
        task.argumentos = argumentos
        task.estado = WORKING
        try:
            resultado = await self.mcp.chamar_tool(TOOL, argumentos, task.trace_id)
        except ErroMCP as e:
            task.responder(FAILED, e.message)
            return
        await self._aplicar_resultado(task, resultado)

    # --- Continuacao: o requestState volta para o servidor MCP --------------

    async def continuar(self, task: Task, texto: str) -> None:
        pausa = task.pausa
        assert pausa is not None
        escolha = interpretar_escolha(texto)
        if escolha is None or (escolha != "recusar" and escolha not in pausa.alternativas):
            task.responder(INPUT_REQUIRED, linha_de_alternativas(pausa.alternativas))
            return
        if escolha == "recusar":
            resposta = {"action": "decline"}
        else:
            resposta = {"action": "accept", "content": {"sala": escolha}}
        task.estado = WORKING
        task.pausa = None
        try:
            # Mesmo tools/call, id novo (ClienteMCP.requisitar), mesma chave e requestState ecoado.
            resultado = await self.mcp.chamar_tool(
                TOOL,
                task.argumentos,
                task.trace_id,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except ErroMCP as e:
            task.responder(FAILED, e.message)
            return
        await self._aplicar_resultado(task, resultado)

    # --- Traducao MCP -> A2A -------------------------------------------------

    async def _aplicar_resultado(self, task: Task, resultado: dict) -> None:
        if resultado.get("resultType") == "input_required":
            # O input_required do MCP vira a pausa da Task.
            try:
                chave, pedido = next(iter(resultado["inputRequests"].items()))
                campo = pedido["params"]["requestedSchema"]["properties"]["sala"]
                alternativas = campo.get("enum") or [campo["const"]]
                request_state = resultado["requestState"]
            except (KeyError, TypeError, StopIteration, AttributeError):
                task.responder(FAILED, "Resposta input_required inesperada do servidor MCP")
                return
            task.pausa = Pausa(chave, alternativas, request_state)
            task.responder(INPUT_REQUIRED, linha_de_alternativas(alternativas))
            return
        if resultado.get("isError"):
            task.responder(FAILED, _texto_da_tool(resultado))
            return
        dados = resultado.get("structuredContent") or {}
        if dados.get("reservado") is False:
            task.responder(CANCELED, "Reserva recusada: nada foi reservado.")
            return
        try:
            politica = await self.mcp.versao_da_politica(task.trace_id)
        except ErroMCP as e:
            task.responder(FAILED, e.message)
            return
        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,
        }
        task.artifacts.append(
            {"artifactId": f"art-{secrets.token_hex(6)}", "name": "reserva", "parts": [{"text": json.dumps(reserva)}]}
        )
        task.responder(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")
