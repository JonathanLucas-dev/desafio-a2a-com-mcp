"""Dominio das salas: dados, politica e regras. Em memoria, sem I/O alem da carga inicial."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

FUSO = timezone(timedelta(hours=-3))
ABERTURA, FECHAMENTO = 8, 20
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class ErroDeRegra(Exception):
    """Violacao de regra de negocio; vira isError no resultado da tool."""


@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


@dataclass
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str


def _instante(texto: str) -> datetime:
    try:
        # fromisoformat so aceita o sufixo Z a partir do Python 3.11
        valor = datetime.fromisoformat(texto[:-1] + "+00:00" if texto.endswith("Z") else texto)
    except (TypeError, ValueError):
        raise ErroDeRegra("Intervalo invalido: datas devem estar em ISO 8601 com fuso") from None
    if valor.tzinfo is None:
        raise ErroDeRegra("Intervalo invalido: datas devem estar em ISO 8601 com fuso")
    return valor


class Agenda:
    def __init__(self, dados: Path) -> None:
        self.salas = [Sala(**s) for s in json.loads((dados / "salas.json").read_text(encoding="utf-8"))]
        self.reservas = [Reserva(**r) for r in json.loads((dados / "reservas.json").read_text(encoding="utf-8"))]
        self.politica_texto = (dados / "politica-de-uso.md").read_text(encoding="utf-8")
        primeira = self.politica_texto.splitlines()[0]
        self.politica_versao = primeira.split(":", 1)[1].strip()

    def sala(self, sala_id: str) -> Sala | None:
        return next((s for s in self.salas if s.id == sala_id), None)

    def validar(self, sala_id: str, inicio: str, fim: str) -> Sala:
        sala = self.sala(sala_id)
        if sala is None:
            raise ErroDeRegra(f"Sala inexistente: {sala_id}")
        ini, f = _instante(inicio), _instante(fim)
        if f <= ini:
            raise ErroDeRegra(ERRO_INTERVALO)
        ini_local, fim_local = ini.astimezone(FUSO), f.astimezone(FUSO)
        abre = ini_local.replace(hour=ABERTURA, minute=0, second=0, microsecond=0)
        fecha = ini_local.replace(hour=FECHAMENTO, minute=0, second=0, microsecond=0)
        if ini_local < abre or fim_local > fecha:
            raise ErroDeRegra(ERRO_JANELA)
        if f - ini > DURACAO_MAXIMA:
            raise ErroDeRegra(ERRO_DURACAO)
        return sala

    def conflitos(self, sala_id: str, inicio: str, fim: str) -> list[Reserva]:
        ini, f = _instante(inicio), _instante(fim)
        return [
            r for r in self.reservas
            if r.sala == sala_id and _instante(r.inicio) < f and ini < _instante(r.fim)
        ]

    def alternativas(self, sala_id: str, inicio: str, fim: str) -> list[str]:
        pedida = self.sala(sala_id)
        livres = [
            s for s in self.salas
            if s.id != sala_id and s.capacidade >= pedida.capacidade and not self.conflitos(s.id, inicio, fim)
        ]
        livres.sort(key=lambda s: (s.capacidade, s.id))
        return [s.id for s in livres[:MAX_ALTERNATIVAS]]

    def criar(self, sala_id: str, inicio: str, fim: str, responsavel: str) -> Reserva:
        reserva = Reserva(f"res-{len(self.reservas) + 1:04d}", sala_id, inicio, fim, responsavel)
        self.reservas.append(reserva)
        return reserva


def carregar() -> Agenda:
    padrao = Path(__file__).resolve().parent.parent / "dados"
    return Agenda(Path(os.environ.get("DADOS_DIR", padrao)))
