"""Cliente MCP do agente: HTTP cru, sem callback de elicitation.

Usar o ClientSession do SDK com um handler de elicitation responderia a pergunta
sozinho e a Task nunca pausaria. Aqui o `input_required` chega cru ao chamador.
Nenhum estado de protocolo e inferido de request anterior: todo request leva
protocolVersion, clientCapabilities e (quando ha) o traceparent da Task.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets

import httpx2 as httpx

PROTOCOLO = "2026-07-28"
CAPABILITIES = {"elicitation": {"form": {}}}
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}


class ErroMCP(Exception):
    """Erro JSON-RPC devolvido pelo servidor MCP (ou falha de transporte)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def novo_traceparent(trace_id: str) -> str:
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._http = httpx.AsyncClient(timeout=30)
        self._ids = itertools.count(1)
        self._descoberta = asyncio.Lock()
        self.tools: dict[str, dict] | None = None

    async def fechar(self) -> None:
        await self._http.aclose()

    async def requisitar(self, metodo: str, params: dict, trace_id: str, nome: str | None = None) -> dict:
        """Um request MCP. O id e sempre novo, inclusive no retry de um tools/call."""
        meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
            "traceparent": novo_traceparent(trace_id),
        }
        cabecalhos = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome:
            cabecalhos["Mcp-Name"] = nome
        corpo = {"jsonrpc": "2.0", "id": next(self._ids), "method": metodo, "params": {**params, "_meta": meta}}
        try:
            resp = await self._http.post(self.url, json=corpo, headers=cabecalhos)
        except httpx.HTTPError as e:
            raise ErroMCP(-32603, f"servidor MCP inacessivel: {e}") from e
        dados = _decodificar(resp)
        if "error" in dados:
            erro = dados["error"]
            raise ErroMCP(erro.get("code", -32603), erro.get("message", "erro"))
        return dados["result"]

    async def descobrir(self, trace_id: str) -> dict[str, dict]:
        """tools/list em runtime, antes da primeira chamada. Feito uma vez, sob lock."""
        async with self._descoberta:
            if self.tools is None:
                resultado = await self.requisitar("tools/list", {}, trace_id)
                self.tools = {t["name"]: t for t in resultado.get("tools", [])}
            return self.tools

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict,
        trace_id: str,
        input_responses: dict | None = None,
        request_state: str | None = None,
    ) -> dict:
        tools = await self.descobrir(trace_id)
        if nome not in tools:
            raise ErroMCP(-32602, f"o servidor MCP nao oferece a tool {nome}")
        params: dict = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state  # ecoado sem abrir nem modificar
        return await self.requisitar("tools/call", params, trace_id, nome=nome)

    async def versao_da_politica(self, trace_id: str) -> str:
        """Le o resource politica://uso e extrai a versao declarada na primeira linha."""
        uri = "politica://uso"
        resultado = await self.requisitar("resources/read", {"uri": uri}, trace_id, nome=uri)
        texto = resultado["contents"][0]["text"]
        primeira = texto.splitlines()[0]
        return primeira.split(":", 1)[1].strip()


def _decodificar(resp: httpx.Response) -> dict:
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for linha in resp.text.splitlines():
            if linha.startswith("data:"):
                return json.loads(linha[5:].strip())
        raise ErroMCP(-32603, "resposta SSE sem dados")
    try:
        return resp.json()
    except ValueError:
        raise ErroMCP(-32603, f"resposta nao JSON do servidor MCP (HTTP {resp.status_code})") from None
