"""Idiomas da operação do Kakashi: uma tabela só para EN, ES e DE.

Cada idioma é uma estrutura isolada (avatares com numeração própria, _Rodízio próprio, Roleta própria).
Para um idioma novo, acrescente uma linha em cada dicionário abaixo.
"""
from __future__ import annotations

import re

IDIOMAS = ("en", "es", "de")
PADRAO = "en"

SIGLA = {"en": "EN", "es": "ES", "de": "DE"}
BANDEIRA = {"en": "🇺🇸", "es": "🇪🇸", "de": "🇩🇪"}
NOME = {"en": "Inglês", "es": "Espanhol", "de": "Alemão"}
NOME_EN = {"en": "English", "es": "Spanish", "de": "German"}          # para prompts do Flow/Gemini
WHISPER = {"en": "en", "es": "es", "de": "de"}
CTA = {"en": "COMMENT YES", "es": "COMENTA SÍ", "de": "KOMMENTIERE JA"}  # selo da última cena
RODIZIO = {"en": "_Rodízio", "es": "_Rodízio ES", "de": "_Rodízio DE"}
AVATAR_SUB = {"en": "", "es": "es", "de": "de"}                       # subpasta de AVATAR_DIR
AVATAR_DRIVE = {"en": "Avatares", "es": "Avatares ES", "de": "Avatares DE"}
FALAS = {"en": "falas.json", "es": "falas_es.json", "de": "falas_de.json"}
NOME_AVATAR = {"en": "avatar", "es": "avatar_es", "de": "avatar_de"}  # prefixo das pastas de saída
PREF_CHAVE = {"en": "av", "es": "es_av", "de": "de_av"}               # ids dos avatares na Roleta
CHAVE_RADAR = {"en": "", "es": "es:", "de": "de:"}
SUFIXO = {"en": "", "es": "_es", "de": "_de"}                         # sufixo de arquivo (lipsync etc.)


def norm(valor, padrao: str = PADRAO) -> str:
    """'ES', ' de ', 'Espanhol'... -> 'es' / 'de'; qualquer outra coisa -> padrao."""
    s = str(valor or "").strip().lower()
    if s in IDIOMAS:
        return s
    for lang, nome in NOME.items():
        if s and s in (nome.lower(), NOME_EN[lang].lower()):
            return lang
    return padrao


def valido(valor) -> bool:
    return str(valor or "").strip().lower() in IDIOMAS


def ou_none(valor) -> str | None:
    """Idioma válido ou None (para filtros opcionais)."""
    s = str(valor or "").strip().lower()
    return s if s in IDIOMAS else None


def tag(lang) -> str:
    """'🇩🇪 DE'."""
    lang = norm(lang)
    return f"{BANDEIRA[lang]} {SIGLA[lang]}"


def rotulo(lang) -> str:
    """'🇩🇪 Alemão (DE)'."""
    lang = norm(lang)
    return f"{BANDEIRA[lang]} {NOME[lang]} ({SIGLA[lang]})"


def de_nome(nome) -> str | None:
    """Idioma pelo nome da pasta/arquivo ('avatar_de_3', 'Avatar 5 copy2_es.mp4'); None se não der para saber."""
    n = str(nome or "").lower()
    for lang in IDIOMAS:
        if lang != PADRAO and NOME_AVATAR[lang] in n:
            return lang
    return None


def de_sufixo(nome) -> str | None:
    """Arquivo terminado em _es / _de (antes da extensão). O 'es' aceita espaço, ponto ou hífen antes (era assim);
    o 'de' só com _, porque 'de' no fim do nome é comum em português e espanhol ('receita de.mp4')."""
    nome = str(nome or "")
    if re.search(r"(?:^|[_\s.-])es(?:\.[a-z0-9]+)?$", nome, re.I):
        return "es"
    for lang in IDIOMAS:
        if lang not in (PADRAO, "es") and re.search(rf"_{lang}(?:\.[a-z0-9]+)?$", nome, re.I):
            return lang
    return None


def de_pasta_avatar(nome_pasta) -> str | None:
    """'es', 'Avatares ES', 'de', 'Avatares DE' -> idioma."""
    n = str(nome_pasta or "").strip().lower()
    for lang in IDIOMAS:
        if lang != PADRAO and n in (AVATAR_SUB[lang], AVATAR_DRIVE[lang].lower()):
            return lang
    return None


def rodizios() -> dict[str, str]:
    """Nome da pasta de rodízio (com variações sem acento e com _) -> idioma."""
    out = {}
    for lang, pasta in RODIZIO.items():
        for v in {pasta, pasta.replace("í", "i")}:
            out[v] = lang
            out[v.replace(" ", "_")] = lang
    return out
