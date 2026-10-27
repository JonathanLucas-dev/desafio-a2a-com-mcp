"""Testes de aceitacao da entrega, alem do validador. Biblioteca padrao apenas.

    python tests/test_entrega.py

Sobe servidor MCP e agente proprios (portas 7410-7413) com segredo descartavel.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PY = sys.executable
PORTA_MCP, PORTA_AGENTE = 7411, 7410
MCP = f"http://127.0.0.1:{PORTA_MCP}/mcp"
A2A = f"http://127.0.0.1:{PORTA_AGENTE}/a2a"
CARD = f"http://127.0.0.1:{PORTA_AGENTE}/.well-known/agent-card.json"
SEGREDO = secrets.token_hex(32)
PROTO = "2026-07-28"
CAPS = {"elicitation": {"form": {}}}

procs: dict[str, subprocess.Popen] = {}
tmp = Path(tempfile.mkdtemp(prefix="ponte-"))
LOG_MCP = tmp / "mcp.log"
LOG_MCP.touch()


def linhas_log() -> list[str]:
    return LOG_MCP.read_text(errors="replace").splitlines()


def esperar(porta: int) -> None:
    for _ in range(100):
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", porta)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"porta {porta} nao subiu")


def subir_mcp(env_extra: dict | None = None) -> None:
    env = {**os.environ, "REQUEST_STATE_SECRET": SEGREDO, "MCP_PORT": str(PORTA_MCP), **(env_extra or {})}
    procs["mcp"] = subprocess.Popen([PY, str(RAIZ / "servidor-mcp" / "servidor.py")], env=env, stderr=open(LOG_MCP, "ab"))
    esperar(PORTA_MCP)


def subir_agente(mcp_url: str = MCP, porta: int = PORTA_AGENTE, nome: str = "agente") -> None:
    env = {**os.environ, "AGENT_PORT": str(porta), "MCP_URL": mcp_url}
    env.pop("REQUEST_STATE_SECRET", None)
    procs[nome] = subprocess.Popen([PY, str(RAIZ / "agente" / "agente.py")], env=env, cwd=RAIZ / "agente",
                                   stderr=subprocess.DEVNULL)
    esperar(porta)


def parar(nome: str) -> None:
    p = procs.pop(nome, None)
    if p:
        p.terminate()
        p.wait(10)


def setUpModule() -> None:
    subir_mcp()
    subir_agente()


def tearDownModule() -> None:
    for n in list(procs):
        parar(n)


# --- HTTP ------------------------------------------------------------------

def post(url: str, corpo, cabecalhos: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(corpo).encode(), headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


_ids = iter(range(1000, 10**9))


def mcp(metodo: str, params: dict, nome: str | None = None, caps=CAPS, trace: str | None = None,
        mcp_name_header: str | None = None, id_=None) -> tuple[int, dict]:
    meta = {"io.modelcontextprotocol/protocolVersion": PROTO, "io.modelcontextprotocol/clientCapabilities": caps}
    if trace:
        meta["traceparent"] = f"00-{trace}-{secrets.token_hex(8)}-01"
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": PROTO, "Mcp-Method": metodo}
    if nome:
        h["Mcp-Name"] = mcp_name_header or nome
    return post(MCP, {"jsonrpc": "2.0", "id": id_ if id_ is not None else next(_ids), "method": metodo,
                      "params": {**params, "_meta": meta}}, h)


def tool(nome: str, args: dict, **kw) -> tuple[int, dict]:
    extra = {k: kw.pop(k) for k in ("inputResponses", "requestState") if k in kw}
    return mcp("tools/call", {"name": nome, "arguments": args, **extra}, nome, **kw)


def a2a(metodo: str, params: dict, trace: str | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    if trace:
        h["traceparent"] = f"00-{trace}-{secrets.token_hex(8)}-01"
    return post(A2A, {"jsonrpc": "2.0", "id": 1, "method": metodo, "params": params}, h)[1]


def enviar(texto: str, task_id: str | None = None, trace: str | None = None) -> dict:
    msg_ = {"messageId": f"m-{secrets.token_hex(4)}", "role": "ROLE_USER", "parts": [{"text": texto}]}
    if task_id:
        msg_["taskId"] = task_id
    return a2a("SendMessage", {"message": msg_}, trace)


def task(resp: dict) -> dict:
    return (resp.get("result") or {}).get("task") or {}


def estado(resp: dict) -> str:
    return task(resp).get("status", {}).get("state", "")


def msg(resp: dict) -> str:
    return " ".join(p["text"] for p in (task(resp).get("status", {}).get("message") or {}).get("parts", []))


def artifact(resp: dict) -> dict:
    arts = task(resp).get("artifacts") or []
    return json.loads(arts[0]["parts"][0]["text"]) if arts else {}


def pedido(sala, dia, ini, fim, quem="T"):
    return f"reservar sala={sala} inicio={dia}T{ini}:00-03:00 fim={dia}T{fim}:00-03:00 responsavel={quem}"


def args(sala, dia, ini, fim, quem="T"):
    return {"sala": sala, "inicio": f"{dia}T{ini}:00-03:00", "fim": f"{dia}T{fim}:00-03:00", "responsavel": quem}


def ocupar(sala, dia, ini, fim):
    _, r = tool("reservar_sala", args(sala, dia, ini, fim, "Ocupante"))
    assert r["result"]["resultType"] == "complete" and not r["result"].get("isError"), r


def livre(sala, dia, ini, fim) -> bool:
    _, r = tool("consultar_disponibilidade", args(sala, dia, ini, fim))
    return r["result"]["structuredContent"]["livre"]


def conflito_mcp(sala, dia, ini, fim):
    """Devolve (chave, requestState, args) de um input_required direto do servidor."""
    a = args(sala, dia, ini, fim, "Marty")
    _, r = tool("reservar_sala", a)
    res = r["result"]
    assert res["resultType"] == "input_required", r
    return next(iter(res["inputRequests"])), res["requestState"], a


def enum_de(r: dict) -> list:
    campo = next(iter(r["result"]["inputRequests"].values()))["params"]["requestedSchema"]["properties"]["sala"]
    return campo.get("enum") or [campo.get("const")]


def aceitar(chave: str, sala: str) -> dict:
    return {chave: {"action": "accept", "content": {"sala": sala}}}


# --- Servidor MCP ----------------------------------------------------------

class ServidorMCP(unittest.TestCase):
    def test_meta_obrigatorio_em_toda_tool_e_metodo(self):
        for metodo, params in (("tools/list", {}), ("resources/read", {"uri": "politica://uso"})):
            for omitido in ("io.modelcontextprotocol/protocolVersion", "io.modelcontextprotocol/clientCapabilities"):
                meta = {"io.modelcontextprotocol/protocolVersion": PROTO, "io.modelcontextprotocol/clientCapabilities": CAPS}
                meta.pop(omitido)
                h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                     "MCP-Protocol-Version": PROTO, "Mcp-Method": metodo}
                if metodo == "resources/read":
                    h["Mcp-Name"] = "politica://uso"
                st, r = post(MCP, {"jsonrpc": "2.0", "id": 1, "method": metodo, "params": {**params, "_meta": meta}}, h)
                self.assertEqual((st, r["error"]["code"]), (400, -32602), f"{metodo} sem {omitido}")

    def test_meta_totalmente_ausente(self):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTO, "Mcp-Method": "tools/list"}
        st, r = post(MCP, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, h)
        self.assertEqual((st, r["error"]["code"]), (400, -32602))

    def test_header_que_nao_bate_com_o_corpo_e_32020(self):
        st, r = tool("listar_salas", {}, mcp_name_header="outra_tool")
        self.assertEqual(r["error"]["code"], -32020, r)
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTO, "Mcp-Method": "tools/list"}
        meta = {"io.modelcontextprotocol/protocolVersion": PROTO, "io.modelcontextprotocol/clientCapabilities": CAPS}
        st, r = post(MCP, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "listar_salas", "arguments": {}, "_meta": meta}}, h)
        self.assertEqual((st, r["error"]["code"]), (400, -32020))

    def test_tres_tools_com_output_schema(self):
        _, r = mcp("tools/list", {})
        tools = {t["name"]: t for t in r["result"]["tools"]}
        self.assertEqual(set(tools), {"listar_salas", "consultar_disponibilidade", "reservar_sala"})
        self.assertIn("outputSchema", tools["listar_salas"])
        self.assertNotIn("escolha", tools["reservar_sala"]["inputSchema"]["properties"])
        self.assertEqual(set(tools["reservar_sala"]["inputSchema"]["required"]), {"sala", "inicio", "fim", "responsavel"})

    def test_listar_salas_traz_as_cinco_salas(self):
        _, r = tool("listar_salas", {})
        salas = r["result"]["structuredContent"]["salas"]
        self.assertEqual([s["id"] for s in salas], ["sala-aquario", "sala-porao", "sala-garagem", "sala-fusca", "sala-mirante"])

    def test_resource_markdown_e_igual_ao_arquivo(self):
        _, r = mcp("resources/read", {"uri": "politica://uso"}, "politica://uso")
        c = r["result"]["contents"][0]
        self.assertEqual(c["mimeType"], "text/markdown")
        self.assertEqual(c["text"], (RAIZ / "dados" / "politica-de-uso.md").read_text(encoding="utf-8"))
        _, r = mcp("resources/read", {"uri": "politica://nada"}, "politica://nada")
        self.assertEqual(r["error"]["code"], -32602)

    def test_consultar_disponibilidade_livre_e_ocupada(self):
        d = "2026-12-01"
        self.assertTrue(livre("sala-aquario", d, "09:00", "10:00"))
        ocupar("sala-aquario", d, "09:00", "10:00")
        _, r = tool("consultar_disponibilidade", args("sala-aquario", d, "09:30", "10:30"))
        sc = r["result"]["structuredContent"]
        self.assertFalse(sc["livre"])
        self.assertEqual(sc["conflitos"][0]["responsavel"], "Ocupante")
        self.assertTrue(livre("sala-aquario", d, "10:00", "11:00"), "intervalos encostados nao conflitam")

    def test_validacoes_nas_tres_regras_e_bordas(self):
        d = "2026-12-02"
        casos = {
            "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00": [("07:59", "09:00"), ("19:00", "20:01")],
            "Duracao acima do limite: a politica permite no maximo 2 horas": [("09:00", "11:01")],
            "Intervalo invalido: fim deve ser posterior a inicio": [("10:00", "10:00"), ("11:00", "10:00")],
        }
        for mensagem, intervalos in casos.items():
            for ini, fim in intervalos:
                for nome in ("consultar_disponibilidade", "reservar_sala"):
                    _, r = tool(nome, args("sala-porao", d, ini, fim))
                    self.assertTrue(r["result"]["isError"], (nome, ini, fim))
                    self.assertIn(mensagem, r["result"]["content"][0]["text"])
        for ini, fim in (("08:00", "10:00"), ("18:00", "20:00")):
            self.assertTrue(livre("sala-porao", d, ini, fim), f"{ini}-{fim} deve ser aceito")

    def test_janela_e_avaliada_em_sao_paulo_mesmo_com_outro_fuso(self):
        _, r = tool("consultar_disponibilidade", {"sala": "sala-porao", "inicio": "2026-12-02T12:00:00+00:00", "fim": "2026-12-02T13:00:00+00:00"})
        self.assertFalse(r["result"].get("isError"), r)
        _, r = tool("consultar_disponibilidade", {"sala": "sala-porao", "inicio": "2026-12-02T10:00:00+00:00", "fim": "2026-12-02T11:00:00+00:00"})
        self.assertTrue(r["result"]["isError"])

    def test_sufixo_z_e_aceito(self):
        # 12:00Z = 09:00-03:00
        _, r = tool("consultar_disponibilidade", {"sala": "sala-porao", "inicio": "2026-12-02T12:00:00Z", "fim": "2026-12-02T13:00:00Z"})
        self.assertFalse(r["result"].get("isError"), r)

    def test_corpo_grande_nao_quebra_o_log_nem_o_servidor(self):
        grande = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "listar_salas", "arguments": {"x": "a" * 2_000_000}}}
        try:
            post(MCP, grande, {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                               "MCP-Protocol-Version": PROTO, "Mcp-Method": "tools/call", "Mcp-Name": "listar_salas"})
        except Exception:
            pass  # recusar o corpo e legitimo; o que importa e o servidor continuar de pe
        _, r = tool("listar_salas", {})
        self.assertEqual(len(r["result"]["structuredContent"]["salas"]), 5)

    def test_datas_invalidas_viram_erro_de_execucao(self):
        _, r = tool("reservar_sala", {"sala": "sala-porao", "inicio": "ontem", "fim": "hoje", "responsavel": "X"})
        self.assertTrue(r["result"]["isError"], r)

    def test_validacao_vem_antes_da_capability(self):
        _, r = tool("reservar_sala", args("sala-nao-existe", "2026-12-02", "09:00", "10:00"), caps={})
        self.assertIn("Sala inexistente: sala-nao-existe", r["result"]["content"][0]["text"])

    def test_reserva_livre_traz_structured_content_do_contrato(self):
        _, r = tool("reservar_sala", args("sala-mirante", "2026-12-03", "09:00", "10:00", "Doc"))
        sc = r["result"]["structuredContent"]
        self.assertEqual(r["result"]["resultType"], "complete")
        for k in ("reserva", "reservado", "sala", "inicio", "fim", "responsavel", "politica"):
            self.assertIn(k, sc)
        self.assertTrue(sc["reservado"])
        self.assertEqual(sc["politica"], "2026-11-01")
        self.assertEqual(json.loads(r["result"]["content"][0]["text"]), sc)
        self.assertFalse(livre("sala-mirante", "2026-12-03", "09:00", "10:00"))

    def test_alternativas_ordem_e_limite_de_tres(self):
        d = "2026-12-04"
        ocupar("sala-aquario", d, "09:00", "10:00")
        _, r = tool("reservar_sala", args("sala-aquario", d, "09:00", "10:00"))
        # capacidade crescente, empate por id; mirante (20) fica de fora pelo limite de 3
        self.assertEqual(enum_de(r), ["sala-porao", "sala-fusca", "sala-garagem"])

    def test_alternativas_ignoram_salas_menores_e_ocupadas(self):
        d = "2026-12-05"
        ocupar("sala-fusca", d, "09:00", "10:00")
        ocupar("sala-garagem", d, "09:00", "10:00")
        _, r = tool("reservar_sala", args("sala-fusca", d, "09:00", "10:00"))
        self.assertEqual(enum_de(r), ["sala-mirante"], "aquario/porao sao menores; garagem esta ocupada")

    def test_elicitation_e_plana_e_so_pede_sala(self):
        d = "2026-12-06"
        ocupar("sala-porao", d, "09:00", "10:00")
        _, r = tool("reservar_sala", args("sala-porao", d, "09:00", "10:00"))
        p = next(iter(r["result"]["inputRequests"].values()))
        self.assertEqual(p["method"], "elicitation/create")
        self.assertEqual(p["params"]["mode"], "form")
        esquema = p["params"]["requestedSchema"]
        self.assertEqual(list(esquema["properties"]), ["sala"])
        self.assertEqual(esquema["required"], ["sala"])
        self.assertEqual(len(r["result"]["inputRequests"]), 1)

    def test_retry_accept_reserva_a_sala_escolhida_e_so_ela(self):
        d = "2026-12-08"
        ocupar("sala-garagem", d, "09:00", "10:00")
        chave, estado_, a = conflito_mcp("sala-garagem", d, "09:00", "10:00")
        _, r = tool("reservar_sala", a, inputResponses=aceitar(chave, "sala-mirante"), requestState=estado_)
        sc = r["result"]["structuredContent"]
        self.assertEqual((r["result"]["resultType"], sc["sala"], sc["reservado"]), ("complete", "sala-mirante", True))
        self.assertTrue(livre("sala-fusca", d, "09:00", "10:00"))

    def test_decline_e_cancel_concluem_sem_reservar(self):
        d = "2026-12-09"
        ocupar("sala-garagem", d, "09:00", "10:00")
        for acao in ("decline", "cancel"):
            chave, estado_, a = conflito_mcp("sala-garagem", d, "09:00", "10:00")
            _, r = tool("reservar_sala", a, inputResponses={chave: {"action": acao}}, requestState=estado_)
            res = r["result"]
            self.assertEqual(res["resultType"], "complete", acao)
            self.assertFalse(res.get("isError"), acao)
            self.assertFalse(res["structuredContent"]["reservado"], acao)
            self.assertTrue(res["structuredContent"]["motivo"], acao)
        for s in ("sala-fusca", "sala-mirante"):
            self.assertTrue(livre(s, d, "09:00", "10:00"))

    def test_escolha_fora_do_enum_nao_reserva(self):
        d = "2026-12-10"
        ocupar("sala-garagem", d, "09:00", "10:00")
        chave, estado_, a = conflito_mcp("sala-garagem", d, "09:00", "10:00")
        for intrusa in ("sala-aquario", "sala-inexistente"):
            _, r = tool("reservar_sala", a, inputResponses=aceitar(chave, intrusa), requestState=estado_)
            ok = bool(r.get("error")) or r["result"].get("isError") or r["result"].get("resultType") == "input_required"
            self.assertTrue(ok, r)
        self.assertTrue(livre("sala-aquario", d, "09:00", "10:00"))

    def test_chave_errada_no_input_responses_nao_conclui(self):
        d = "2026-12-11"
        ocupar("sala-garagem", d, "09:00", "10:00")
        _, estado_, a = conflito_mcp("sala-garagem", d, "09:00", "10:00")
        _, r = tool("reservar_sala", a, inputResponses=aceitar("outra-chave", "sala-fusca"), requestState=estado_)
        self.assertEqual(r.get("result", {}).get("resultType"), "input_required", r)
        self.assertTrue(livre("sala-fusca", d, "09:00", "10:00"))

    def test_request_state_adulterado_em_qualquer_posicao_e_lixo(self):
        d = "2026-12-12"
        ocupar("sala-garagem", d, "09:00", "10:00")
        chave, estado_, a = conflito_mcp("sala-garagem", d, "09:00", "10:00")
        resp = aceitar(chave, "sala-fusca")
        troca = lambda s, i: s[:i] + ("A" if s[i] != "A" else "B") + s[i + 1:]  # noqa: E731
        ruins = [troca(estado_, 10), troca(estado_, len(estado_) - 1), estado_[:-8], estado_ + "x",
                 "v1.", "lixo", "", "e30=", "eyJhIjoxfQ=="]
        for ruim in ruins:
            _, r = tool("reservar_sala", a, inputResponses=resp, requestState=ruim)
            self.assertEqual(r.get("error", {}).get("code"), -32602, (ruim[:30], r))
        self.assertTrue(livre("sala-fusca", d, "09:00", "10:00"))

    def test_request_state_de_um_pedido_nao_vale_para_outro(self):
        d = "2026-12-13"
        ocupar("sala-garagem", d, "09:00", "10:00")
        ocupar("sala-garagem", d, "11:00", "12:00")
        chave, estado_, _ = conflito_mcp("sala-garagem", d, "09:00", "10:00")
        outro = args("sala-garagem", d, "11:00", "12:00", "Marty")
        _, r = tool("reservar_sala", outro, inputResponses=aceitar(chave, "sala-fusca"), requestState=estado_)
        self.assertEqual(r["error"]["code"], -32602, "estado de 9h reaproveitado no pedido de 11h")
        self.assertTrue(livre("sala-fusca", d, "11:00", "12:00"))

    def test_request_state_nao_e_legivel(self):
        d = "2026-12-14"
        ocupar("sala-garagem", d, "09:00", "10:00")
        _, estado_, _ = conflito_mcp("sala-garagem", d, "09:00", "10:00")
        self.assertNotIn("sala-garagem", estado_)
        self.assertTrue(estado_.startswith("v1."))

    def test_request_state_sobrevive_a_restart_do_servidor(self):
        d = "2026-12-15"
        ocupar("sala-garagem", d, "09:00", "10:00")
        chave, estado_, a = conflito_mcp("sala-garagem", d, "09:00", "10:00")
        parar("mcp")
        subir_mcp()
        ocupar("sala-garagem", d, "09:00", "10:00")  # reservas em memoria sumiram no restart; reponho o conflito
        _, r = tool("reservar_sala", a, inputResponses=aceitar(chave, "sala-fusca"), requestState=estado_)
        self.assertEqual(r["result"]["structuredContent"]["sala"], "sala-fusca", r)

    def test_estado_selado_com_outra_chave_e_rejeitado(self):
        d = "2026-12-16"
        ocupar("sala-garagem", d, "09:00", "10:00")
        chave, estado_, a = conflito_mcp("sala-garagem", d, "09:00", "10:00")
        parar("mcp")
        subir_mcp({"REQUEST_STATE_SECRET": secrets.token_hex(32)})
        try:
            ocupar("sala-garagem", d, "09:00", "10:00")
            _, r = tool("reservar_sala", a, inputResponses=aceitar(chave, "sala-fusca"), requestState=estado_)
            self.assertEqual(r["error"]["code"], -32602)
        finally:
            parar("mcp")
            subir_mcp()

    def test_sem_capability_so_falha_quando_precisa_perguntar(self):
        d = "2026-12-17"
        _, r = tool("reservar_sala", args("sala-porao", d, "09:00", "10:00"), caps={})
        self.assertEqual(r["result"]["resultType"], "complete", "sem conflito nao precisa de elicitation")
        ocupar("sala-garagem", d, "09:00", "10:00")
        for caps in ({}, {"sampling": {}}, {"elicitation": {"url": {}}}):
            st, r = tool("reservar_sala", args("sala-garagem", d, "09:00", "10:00"), caps=caps)
            self.assertEqual((st, r["error"]["code"]), (400, -32021), caps)
            self.assertEqual(r["error"]["data"]["requiredCapabilities"], {"elicitation": {"form": {}}})
        # O SDK le `elicitation: {}` (formato anterior aos modos) como suporte a form mode.
        _, r = tool("reservar_sala", args("sala-garagem", d, "09:00", "10:00"), caps={"elicitation": {}})
        self.assertEqual(r["result"]["resultType"], "input_required")

    def test_tool_inexistente_e_recusada(self):
        _, r = tool("voar_delorean", {})
        self.assertTrue(r.get("error", {}).get("code") == -32602 or r["result"].get("isError"))

    def test_log_registra_metodo_id_e_traceparent(self):
        trace = secrets.token_hex(16)
        tool("listar_salas", {}, trace=trace, id_="id-de-teste-123")
        time.sleep(0.3)
        achadas = [ln for ln in linhas_log() if "id-de-teste-123" in ln]
        self.assertEqual(len(achadas), 1)
        self.assertIn("metodo=tools/call", achadas[0])
        self.assertIn(trace, achadas[0])

    def test_segredo_ausente_ou_curto_impede_a_subida(self):
        for valor in (None, "abc", secrets.token_hex(8)):
            env = {k: v for k, v in os.environ.items() if k != "REQUEST_STATE_SECRET"}
            env["MCP_PORT"] = "7499"
            if valor:
                env["REQUEST_STATE_SECRET"] = valor
            p = subprocess.run([PY, str(RAIZ / "servidor-mcp" / "servidor.py")], env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(p.returncode, 0, valor)


# --- Agente ----------------------------------------------------------------

class Agente(unittest.TestCase):
    def test_card_v1_igual_ao_exemplo(self):
        with urllib.request.urlopen(CARD) as r:
            card = json.loads(r.read())
        esperado = json.loads((RAIZ / "exemplos/wire/07-a2a-agent-card.json").read_text(encoding="utf-8"))["response"]["body"]
        for i in (card, esperado):
            for x in i["supportedInterfaces"]:
                x.pop("url")
        self.assertEqual(card, esperado)

    def test_fluxo_livre_completo(self):
        r = enviar(pedido("sala-porao", "2026-12-20", "09:00", "10:00", "Doc"))
        self.assertEqual(estado(r), "TASK_STATE_COMPLETED")
        t = task(r)
        self.assertTrue(t["id"] and t["contextId"])
        self.assertEqual(t["artifacts"][0]["name"], "reserva")
        a = artifact(r)
        self.assertEqual((a["sala"], a["responsavel"], a["politica"]), ("sala-porao", "Doc", "2026-11-01"))
        self.assertEqual(set(a), {"reserva", "sala", "inicio", "fim", "responsavel", "politica"})
        g = a2a("GetTask", {"id": t["id"]})
        self.assertEqual(task(g)["artifacts"], t["artifacts"])
        self.assertEqual(estado(g), "TASK_STATE_COMPLETED")

    def test_ponte_pausa_continua_e_usa_ids_diferentes_com_o_mesmo_trace(self):
        d, trace = "2026-12-21", secrets.token_hex(16)
        ocupar("sala-garagem", d, "14:00", "15:00")
        ocupar("sala-fusca", d, "14:00", "15:00")
        antes = len(linhas_log())
        r = enviar(pedido("sala-garagem", d, "14:00", "15:00", "Marty"), trace=trace)
        self.assertEqual(estado(r), "TASK_STATE_INPUT_REQUIRED")
        self.assertEqual(msg(r), "alternativas: sala-mirante")
        tid = task(r)["id"]
        self.assertEqual(estado(a2a("GetTask", {"id": tid})), "TASK_STATE_INPUT_REQUIRED")
        r = enviar("escolha=sala-mirante", tid, trace=trace)
        self.assertEqual(estado(r), "TASK_STATE_COMPLETED")
        self.assertEqual(artifact(r)["sala"], "sala-mirante")
        chamadas = [ln for ln in linhas_log()[antes:] if f"-{trace}-" in ln]
        self.assertTrue(chamadas, "nenhum request MCP com o trace-id da Task")
        calls = [ln for ln in chamadas if "metodo=tools/call" in ln and "reservar_sala" in ln]
        self.assertEqual(len(calls), 2)
        ids = [re.search(r"id=(\S+)", ln).group(1) for ln in calls]
        self.assertNotEqual(ids[0], ids[1], "o retry reaproveitou o id")
        self.assertIn("retry=nao", calls[0])
        self.assertIn("retry=sim", calls[1])
        self.assertTrue(any("metodo=resources/read" in ln for ln in chamadas))

    def test_tools_list_vem_antes_do_primeiro_tools_call_de_um_agente_novo(self):
        porta = 7412
        subir_agente(MCP, porta, "agente2")
        try:
            marca = len(linhas_log())
            req = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
                "messageId": "m1", "role": "ROLE_USER", "parts": [{"text": pedido("sala-aquario", "2026-12-22", "09:00", "10:00")}]}}}
            post(f"http://127.0.0.1:{porta}/a2a", req, {"Content-Type": "application/json"})
            novas = [ln for ln in linhas_log()[marca:] if ln.startswith("[mcp]")]
            metodos = [re.search(r"metodo=(\S+)", ln).group(1) for ln in novas]
            self.assertLess(metodos.index("tools/list"), metodos.index("tools/call"), metodos)
        finally:
            parar("agente2")

    def test_requests_do_agente_declaram_elicitation_form(self):
        d = "2026-12-23"  # sem a capability o conflito retornaria -32021 e a Task falharia em vez de pausar
        ocupar("sala-garagem", d, "09:00", "10:00")
        self.assertEqual(estado(enviar(pedido("sala-garagem", d, "09:00", "10:00"))), "TASK_STATE_INPUT_REQUIRED")

    def test_sem_traceparent_o_agente_gera_um_por_task(self):
        marca = len(linhas_log())
        enviar(pedido("sala-mirante", "2026-12-24", "09:00", "10:00"))
        novas = [ln for ln in linhas_log()[marca:] if "metodo=tools/call" in ln]
        self.assertTrue(novas and all("traceparent=00-" in ln for ln in novas), novas)

    def test_recusar_cancela_e_nao_reserva(self):
        d = "2026-12-25"
        ocupar("sala-garagem", d, "09:00", "10:00")
        tid = task(enviar(pedido("sala-garagem", d, "09:00", "10:00")))["id"]
        r = enviar("escolha=recusar", tid)
        self.assertEqual(estado(r), "TASK_STATE_CANCELED")
        self.assertEqual(task(r)["artifacts"], [])
        self.assertTrue(livre("sala-fusca", d, "09:00", "10:00") and livre("sala-mirante", d, "09:00", "10:00"))

    def test_escolha_invalida_repete_a_mesma_linha_e_nao_chama_o_mcp(self):
        d = "2026-12-26"
        ocupar("sala-garagem", d, "09:00", "10:00")
        r0 = enviar(pedido("sala-garagem", d, "09:00", "10:00"))
        tid, linha = task(r0)["id"], msg(r0)
        marca = len(linhas_log())
        for texto in ("escolha=sala-aquario", "escolha=", "qualquer coisa", "escolha=sala-fusca extra", "ESCOLHA=sala-fusca"):
            r = enviar(texto, tid)
            self.assertEqual(estado(r), "TASK_STATE_INPUT_REQUIRED", texto)
            self.assertEqual(msg(r), linha, texto)
        novas = [ln for ln in linhas_log()[marca:] if ln.startswith("[mcp]")]
        self.assertEqual(novas, [], "escolha invalida nao deve gerar trafego MCP")

    def test_estados_terminais_sao_definitivos(self):
        for texto in (pedido("sala-aquario", "2026-12-27", "09:00", "10:00"),
                      pedido("sala-nao-existe", "2026-12-27", "09:00", "10:00"), "lixo"):
            r = enviar(texto)
            tid, final = task(r)["id"], estado(r)
            self.assertIn(final, ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"))
            self.assertIn("error", enviar("escolha=sala-fusca", tid))
            self.assertEqual(estado(a2a("GetTask", {"id": tid})), final)

    def test_task_cancelada_tambem_recusa_novas_mensagens(self):
        d = "2026-12-28"
        ocupar("sala-garagem", d, "09:00", "10:00")
        tid = task(enviar(pedido("sala-garagem", d, "09:00", "10:00")))["id"]
        enviar("escolha=recusar", tid)
        self.assertIn("error", enviar("escolha=sala-fusca", tid))

    def test_parts_malformadas_nao_derrubam_o_agente(self):
        r = a2a("SendMessage", {"message": {"messageId": "m", "role": "ROLE_USER", "parts": ["texto solto", 7, None]}})
        self.assertEqual(estado(r), "TASK_STATE_FAILED", r)
        self.assertEqual(estado(enviar(pedido("sala-aquario", "2027-01-11", "09:00", "10:00"))), "TASK_STATE_COMPLETED")

    def test_task_inexistente(self):
        self.assertEqual(a2a("GetTask", {"id": "nao-existe"})["error"]["code"], -32001)
        self.assertEqual(enviar("escolha=sala-fusca", "nao-existe")["error"]["code"], -32001)

    def test_metodo_e_json_invalidos(self):
        self.assertEqual(a2a("Nada", {})["error"]["code"], -32601)
        req = urllib.request.Request(A2A, data=b"{nao json", headers={"Content-Type": "application/json"}, method="POST")
        self.assertEqual(json.loads(urllib.request.urlopen(req).read())["error"]["code"], -32700)
        self.assertEqual(a2a("SendMessage", {})["error"]["code"], -32602)

    def test_pedidos_malformados_falham_com_instrucao(self):
        for texto in ("oi", "reservar", "reservar sala=sala-porao", "reservar sala= inicio=x fim=y responsavel=z",
                      "escolha=sala-fusca", "reservar foo=bar sala=sala-porao inicio=a fim=b responsavel=c"):
            r = enviar(texto)
            self.assertEqual(estado(r), "TASK_STATE_FAILED", texto)
            self.assertIn("Pedido invalido", msg(r))

    def test_responsavel_com_espaco_e_campos_em_outra_ordem(self):
        r = enviar("reservar responsavel=Emmett Brown fim=2026-12-29T10:00:00-03:00 inicio=2026-12-29T09:00:00-03:00 sala=sala-aquario")
        self.assertEqual(estado(r), "TASK_STATE_COMPLETED", msg(r))
        self.assertEqual(artifact(r)["responsavel"], "Emmett Brown")

    def test_erros_de_regra_chegam_literais_no_historico(self):
        d = "2026-12-30"
        casos = {
            pedido("sala-delorean", d, "09:00", "10:00"): "Sala inexistente: sala-delorean",
            pedido("sala-aquario", d, "07:00", "08:00"): "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00",
            pedido("sala-aquario", d, "09:00", "12:00"): "Duracao acima do limite: a politica permite no maximo 2 horas",
            pedido("sala-aquario", d, "10:00", "09:00"): "Intervalo invalido: fim deve ser posterior a inicio",
        }
        for texto, esperado in casos.items():
            r = enviar(texto)
            self.assertEqual(estado(r), "TASK_STATE_FAILED", texto)
            self.assertIn(esperado, msg(r))
            historico = " ".join(p["text"] for m in task(r)["history"] for p in m["parts"])
            self.assertIn(esperado, historico)

    def test_sem_alternativas_falha_sem_pausar(self):
        d = "2026-12-31"
        ocupar("sala-mirante", d, "09:00", "10:00")
        r = enviar(pedido("sala-mirante", d, "09:00", "10:00"))
        self.assertEqual(estado(r), "TASK_STATE_FAILED")
        self.assertIn("Sem alternativas disponiveis no intervalo", msg(r))

    def test_historico_e_status_message_seguem_o_contrato(self):
        d = "2027-01-04"
        ocupar("sala-garagem", d, "09:00", "10:00")
        t = task(enviar(pedido("sala-garagem", d, "09:00", "10:00")))
        m = t["status"]["message"]
        self.assertEqual((m["role"], m["taskId"], m["contextId"]), ("ROLE_AGENT", t["id"], t["contextId"]))
        self.assertEqual(t["history"][0]["role"], "ROLE_USER")
        self.assertEqual(t["history"][-1]["messageId"], m["messageId"])
        self.assertEqual(m["parts"], [{"text": "alternativas: sala-fusca, sala-mirante"}])

    def test_request_state_nunca_aparece_em_resposta_a2a(self):
        d = "2027-01-05"
        ocupar("sala-garagem", d, "09:00", "10:00")
        r1 = enviar(pedido("sala-garagem", d, "09:00", "10:00"))
        tid = task(r1)["id"]
        r2 = a2a("GetTask", {"id": tid})
        r3 = enviar("escolha=sala-fusca", tid)
        r4 = a2a("GetTask", {"id": tid})
        with urllib.request.urlopen(CARD) as c:
            card = c.read().decode()
        for corpo in (r1, r2, r3, r4, card):
            texto = corpo if isinstance(corpo, str) else json.dumps(corpo)
            self.assertIsNone(re.search(r"v1\.[A-Za-z0-9_-]{40,}", texto))
            self.assertNotIn("requestState", texto)
            self.assertNotIn("inputRequests", texto)

    def test_duas_tasks_pausadas_em_paralelo_nao_trocam_estado(self):
        d = "2027-01-06"
        ocupar("sala-garagem", d, "09:00", "10:00")
        ocupar("sala-garagem", d, "11:00", "12:00")
        a = task(enviar(pedido("sala-garagem", d, "09:00", "10:00", "Lorraine")))["id"]
        b = task(enviar(pedido("sala-garagem", d, "11:00", "12:00", "George")))["id"]
        out: dict[str, dict] = {}
        ts = [threading.Thread(target=lambda k=k, t=t: out.__setitem__(k, enviar("escolha=sala-mirante", t)))
              for k, t in (("a", a), ("b", b))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ra, rb = artifact(out["a"]), artifact(out["b"])
        self.assertEqual((ra["responsavel"], ra["inicio"][11:16]), ("Lorraine", "09:00"))
        self.assertEqual((rb["responsavel"], rb["inicio"][11:16]), ("George", "11:00"))
        self.assertNotEqual(ra["reserva"], rb["reserva"])

    def test_dois_envios_simultaneos_da_mesma_escolha_nao_reservam_duas_vezes(self):
        d = "2027-01-07"
        ocupar("sala-garagem", d, "09:00", "10:00")
        tid = task(enviar(pedido("sala-garagem", d, "09:00", "10:00")))["id"]
        out = []
        ts = [threading.Thread(target=lambda: out.append(enviar("escolha=sala-fusca", tid))) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        sucessos = [o for o in out if estado(o) == "TASK_STATE_COMPLETED"]
        erros = [o for o in out if "error" in o]
        self.assertEqual((len(sucessos), len(erros)), (1, 1), out)
        _, r = tool("consultar_disponibilidade", args("sala-fusca", d, "09:00", "10:00"))
        self.assertEqual(len(r["result"]["structuredContent"]["conflitos"]), 1)

    def test_determinismo(self):
        d = "2027-01-08"
        ocupar("sala-garagem", d, "09:00", "10:00")
        textos = {msg(enviar(pedido("sala-garagem", d, "09:00", "10:00"))) for _ in range(3)}
        self.assertEqual(textos, {"alternativas: sala-fusca, sala-mirante"})

    def test_mcp_fora_do_ar_falha_a_task_sem_derrubar_o_agente(self):
        subir_agente("http://127.0.0.1:7498/mcp", 7413, "agente3")
        try:
            req = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
                "messageId": "m", "role": "ROLE_USER", "parts": [{"text": pedido("sala-aquario", "2027-01-09", "09:00", "10:00")}]}}}
            st, r = post("http://127.0.0.1:7413/a2a", req, {"Content-Type": "application/json"})
            self.assertEqual((st, estado(r)), (200, "TASK_STATE_FAILED"), r)
        finally:
            parar("agente3")

    def test_request_state_rejeitado_no_retry_falha_a_task(self):
        # reinicio do servidor com outra chave invalida o estado guardado no agente
        d = "2027-01-10"
        ocupar("sala-garagem", d, "09:00", "10:00")
        tid = task(enviar(pedido("sala-garagem", d, "09:00", "10:00")))["id"]
        parar("mcp")
        subir_mcp({"REQUEST_STATE_SECRET": secrets.token_hex(32)})
        try:
            ocupar("sala-garagem", d, "09:00", "10:00")
            r = enviar("escolha=sala-fusca", tid)
            self.assertEqual(estado(r), "TASK_STATE_FAILED")
            self.assertTrue(livre("sala-fusca", d, "09:00", "10:00"))
        finally:
            parar("mcp")
            subir_mcp()


# --- Higiene do repositorio -------------------------------------------------

class Higiene(unittest.TestCase):
    def test_dados_validador_exemplos_intactos(self):
        out = subprocess.run(["git", "diff", "--name-only", "263f17e", "--", "dados", "validador", "exemplos"],
                             cwd=RAIZ, capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "")

    def test_sem_sdk_de_llm_e_sem_segredo_no_codigo(self):
        deps = ((RAIZ / "requirements.txt").read_text() + (RAIZ / "pyproject.toml").read_text()).lower()
        for proibido in ("anthropic", "openai", "langchain", "google-generativeai", "cohere", "mistral", "litellm"):
            self.assertNotIn(proibido, deps)
        for arq in list((RAIZ / "servidor-mcp").glob("*.py")) + list((RAIZ / "agente").glob("*.py")):
            texto = arq.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"[0-9a-f]{64}", texto), arq.name)

    def test_agente_nao_importa_o_dominio_do_servidor(self):
        for arq in (RAIZ / "agente").glob("*.py"):
            self.assertNotRegex(arq.read_text(encoding="utf-8"), r"import dominio|from dominio|from servidor\b|import servidor\b")

    def test_agente_nao_abre_nem_decodifica_request_state(self):
        texto = (RAIZ / "agente" / "ponte.py").read_text(encoding="utf-8") + (RAIZ / "agente" / "cliente_mcp.py").read_text(encoding="utf-8")
        self.assertNotRegex(texto, r"b64|base64|hmac|decrypt|unseal")

    def test_versoes_travadas(self):
        for linha in (RAIZ / "requirements.txt").read_text().split():
            self.assertIn("==", linha)

    def test_readme_tem_as_quatro_secoes(self):
        r = (RAIZ / "README.md").read_text(encoding="utf-8")
        for secao in ("## Como rodar", "## Onde a ponte acontece", "## Decisões técnicas", "## Saída do validador"):
            self.assertIn(secao, r)
        self.assertNotIn(SEGREDO, r)
        self.assertEqual(len(re.findall(r"^PASS \d\d", r, re.M)), 36)


if __name__ == "__main__":
    unittest.main(verbosity=2)
