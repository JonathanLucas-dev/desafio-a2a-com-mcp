# A Ponte: um agente A2A com MCP por dentro

A Central de Salas da Hill Valley Tech em dois processos: um **servidor MCP** (porta 7301) que expõe as
salas, e um **agente** (porta 7300) que é host MCP por dentro e servidor A2A v1.0 por fora. Python 3.10+,
SDK `mcp` v2 (revisão 2026-07-28), sem LLM no caminho de execução.

## Como rodar

A partir de um clone limpo, na raiz do repositório.

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv/Scripts/Activate.ps1
pip install -r requirements.txt

# A chave de integridade do requestState. Gere a sua (nunca commite o valor):
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
# PowerShell: $env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
```

Terminal 1, servidor MCP (o stderr mostra método, id e traceparent de cada request):

```bash
python3 servidor-mcp/servidor.py
```

Terminal 2, agente (não precisa do segredo):

```bash
cd agente && python3 agente.py
```

Terminal 3, validador (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Testes extras (60 casos além do validador; sobem os próprios processos nas portas 7410-7413 e não precisam de
nada rodando antes):

```bash
python3 tests/test_entrega.py
```

Variáveis opcionais: `MCP_PORT` (7301), `MCP_HOST`, `DADOS_DIR`, `AGENT_PORT` (7300), `AGENT_PUBLIC_URL`
(`http://localhost:7300`, usada na URL do card), `MCP_URL` (`http://127.0.0.1:7301/mcp`).
Para testar o restart do servidor, suba-o de novo **com o mesmo** `REQUEST_STATE_SECRET`.

## Onde a ponte acontece

Tudo está em [agente/ponte.py](agente/ponte.py). O `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`
em `Ponte._aplicar_resultado`: ali o agente lê a única entrada de `inputRequests`, extrai o enum de
`requestedSchema.properties.sala`, guarda `{chave, alternativas, requestState}` numa `Pausa` ligada **àquela
Task** e responde com a linha `alternativas: a, b`. O `requestState` volta para o servidor em
`Ponte.continuar`: a escolha vira `inputResponses` (`accept` com a sala, ou `decline` para `escolha=recusar`) e
o mesmo `tools/call` é repetido por [agente/cliente_mcp.py](agente/cliente_mcp.py) com **id JSON-RPC novo**, a
mesma chave e o `requestState` ecoado sem ser aberto. O agente usa HTTP cru em vez do `ClientSession` do SDK
porque um callback de elicitation responderia sozinho e a Task nunca pausaria. Do lado do servidor, o MRTR
nasce no resolver `escolha_de_sala` em [servidor-mcp/servidor.py](servidor-mcp/servidor.py).

## Decisões técnicas

- **requestState**: o `RequestStateBoundary` do SDK sela cada valor com AES-256-GCM (chave derivada por HKDF de
  `REQUEST_STATE_SECRET`, que deve ter no mínimo 32 bytes; o servidor não sobe sem ela). O envelope carrega
  expiração, método, tool e um digest dos argumentos. Adulterar um caractere, expirar ou reenviar com
  argumentos diferentes dos selados resulta em `-32602 Invalid or expired requestState`. Cifrar não era
  obrigatório, mas é o que o utilitário do SDK faz.
- **Validade**: 10 minutos (`TTL_REQUEST_STATE = 600`, dentro da faixa de 5 a 30).
- **Sem estado no servidor MCP**: nada fica em memória entre o `input_required` e o retry; a pergunta é
  recalculada a cada rodada e o estado viaja no token. Como a chave vem do ambiente e o `audience` é fixo, um
  retry sobrevive a restart do processo (verificado: gerar o estado, reiniciar o servidor, reenviar).
- **Tasks**: ficam em memória no agente (`Ponte.tasks`), cada uma com seu `asyncio.Lock`. O `requestState` fica
  num objeto `Pausa` separado, que `Task.para_a2a()` nunca serializa, então não aparece em card, artifact nem
  mensagem. Tasks não sobrevivem a restart do agente (fora de escopo).
- **traceparent**: o trace-id do header A2A é guardado na Task e reaproveitado em todo request MCP dela (span-id
  novo a cada request). Sem header, o agente gera um trace-id por Task.
- **Divisão de responsabilidade**: o agente só interpreta o formato fixo e traduz estados. Janela, duração,
  conflito e alternativas são decididos pelo servidor. Reservas ficam em memória.
- **Erros A2A**: Task terminal ou não pausada recebendo `SendMessage` responde `-32004`; Task desconhecida, `-32001`.
- **Capability vazia**: o SDK lê `elicitation: {}` (formato anterior aos modos) como suporte a form mode; só
  `{}`, `sampling` ou `elicitation.url` sozinhos recebem `-32021`. O agente sempre declara `{"elicitation":{"form":{}}}`.
- **Limites conhecidos**: Tasks ficam só em memória e nunca são removidas; a checagem de conflito e a criação
  da reserva não são atômicas (concorrência de escrita está fora de escopo); a expiração do `requestState` é
  aplicada pelo SDK e não tem teste próprio, porque o TTL de 10 minutos não é configurável.
- **SDK**: o servidor roda stateless com `json_response=True`. O SDK já valida `_meta` (`-32602`/HTTP 400),
  headers espelhados (`-32020`) e capability de elicitation (`-32021`/HTTP 400). Nenhuma limitação do SDK
  impediu algum item.

## Saída do validador

```
trace-id desta execucao: ef9705ce11e72f3c94348ec997159efa
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
