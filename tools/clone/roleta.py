"""The "Roleta de Páginas" rule, ported from its artifact (gerarPlano) so the panel knows which Page each avatar
posts on each day. Supports independent language pools ("en" and "es") so English and Spanish operations never mix."""
import re
from datetime import date
from difflib import SequenceMatcher


def idioma_de(o: dict | None) -> str:
    return "es" if str((o or {}).get("idioma") or "").strip().lower() == "es" else "en"


def _ordem(o: dict):
    # porOrdem: by "ordem", then by name with numbers compared as numbers
    nome = str(o.get("nome", ""))
    return (o.get("ordem") or 0, [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", nome)])


def paginas(S: dict, idioma: str | None = None) -> list[dict]:
    itens = ({**p, "id": k} for k, p in S.get("paginas", {}).items())
    if idioma in ("en", "es"):
        itens = (p for p in itens if idioma_de(p) == idioma)
    return sorted(itens, key=_ordem)


def avatares(S: dict, idioma: str | None = None) -> list[dict]:
    itens = ({**a, "id": k} for k, a in S.get("avatares", {}).items())
    if idioma in ("en", "es"):
        itens = (a for a in itens if idioma_de(a) == idioma)
    return sorted(itens, key=_ordem)


def _oficiais(S: dict, idioma: str | None = None) -> dict:
    # a paused Page (restricted by Meta) lends its official avatar back to the wheel until it comes back
    return {p["avatarOficial"]: p["id"] for p in paginas(S, idioma)
            if p.get("fase") == 3 and not p.get("pausada") and p.get("avatarOficial") in S.get("avatares", {})}


AVATARES_POR_PAGINA = 3  # phases 1-2, one video of each a day (since 02/11/2026; it was 2)


def _num_dia(data: str) -> int:  # numDia: days since 1970-01-01
    return (date.fromisoformat(data) - date(1970, 1, 1)).days


def _plano_idioma(S: dict, data: str, lang: str) -> dict[str, list]:
    pags = [p for p in paginas(S, lang) if not p.get("pausada")]  # a paused Page gets nothing
    ids_lang = {pid for pid, p in S.get("paginas", {}).items() if idioma_de(p) == lang}
    dia = S.get("dias", {}).get(data)
    if dia:
        dia_pags = dia.get("paginas") or {}
        fixadas = {pid: e.get("avatares") or [] for pid, e in dia_pags.items() if pid in ids_lang}
        if fixadas:
            return fixadas
    k, of = _num_dia(data), _oficiais(S, lang)
    pool = [a for a in avatares(S, lang) if a.get("ativo") is not False and a["id"] not in of]
    n, res, usados = len(pool), {}, set()
    ultimo: dict[str, dict[str, str]] = {}
    for d in S.get("dias", {}).values():
        if d.get("data", "") >= data:
            continue
        for pid, e in (d.get("paginas") or {}).items():
            if pid not in ids_lang:
                continue
            for a in e.get("avatares") or []:
                if a:
                    m = ultimo.setdefault(pid, {})
                    if a not in m or m[a] < d["data"]:
                        m[a] = d["data"]
    for pi, p in enumerate(sorted((x for x in pags if x.get("fase") != 3), key=lambda x: x.get("prioridade") or 999)):
        escolhidos = []
        eh_fase2 = p.get("fase") == 2
        idades_boas = set(p.get("idades_boas") or []) if eh_fase2 else set()
        for s in range(AVATARES_POR_PAGINA):
            alvo = (AVATARES_POR_PAGINA * (pi + k) + s) % n if n else 0
            melhor, m_chave = None, None
            for ai, a in enumerate(pool):
                if a["id"] in usados:
                    continue
                visto, dist = ultimo.get(p["id"], {}).get(a["id"], ""), (ai - alvo) % n if n else 0
                sin_ok = 1 if (eh_fase2 and idades_boas and a.get("idade") in idades_boas) else 0
                merito = (a.get("merito") if a.get("merito") is not None else 1) if eh_fase2 else 1
                chave = (visto, -sin_ok, -merito, dist)
                if melhor is None or chave < m_chave:
                    melhor, m_chave = a, chave
            if melhor:
                usados.add(melhor["id"])
            escolhidos.append(melhor["id"] if melhor else None)
        res[p["id"]] = escolhidos
    for p in pags:
        if p.get("fase") == 3:
            o = p.get("avatarOficial")
            res[p["id"]] = [o if o in S.get("avatares", {}) else None]
    return res


def plano(S: dict, data: str, idioma: str | None = None) -> dict[str, list]:
    """Roleta page id -> its avatar ids on that day (YYYY-MM-DD), isolated per language pool ('en' vs 'es')."""
    if idioma in ("en", "es"):
        return _plano_idioma(S, data, idioma)
    return {**_plano_idioma(S, data, "en"), **_plano_idioma(S, data, "es")}


def numero_avatar(S: dict, avid: str) -> str | None:
    """"Avatar 03" -> "3" (the number in the video names)."""
    m = re.search(r"(\d+)\s*$", str(S.get("avatares", {}).get(avid, {}).get("nome", "")))
    return str(int(m.group(1))) if m else None


def avatar_para_pagina(S: dict, data: str, idioma: str | None = None) -> dict[str, str]:
    """Avatar number -> roleta page id on that day (optionally filtered to 'en' or 'es')."""
    out = {}
    for pid, avs in plano(S, data, idioma=idioma).items():
        for a in avs:
            n = a and numero_avatar(S, a)
            if n:
                out[n] = pid
    return out


def _chave(nome: str) -> str:
    return re.sub(r"[^a-z0-9]", "", nome.lower())


def _casar_grupo(pags_roleta: list[dict], conectadas: list[dict]) -> dict[str, str]:
    out = {}
    for p in pags_roleta:
        a = _chave(p.get("nome", ""))
        if not a:
            continue
        iguais = [c for c in conectadas if _chave(c["nome"]) == a]
        if not iguais:  # one name starts the other; the shorter part has to be specific (4+ letters)
            iguais = [c for c in conectadas if min(len(a), len(_chave(c["nome"]))) >= 4
                      and (_chave(c["nome"]).startswith(a) or a.startswith(_chave(c["nome"])))]
        if not iguais:  # a typo ("Test trick" ~ "Teste Trick"): the one clearly closest name
            notas = sorted(((SequenceMatcher(None, a, _chave(c["nome"])).ratio(), i) for i, c in enumerate(conectadas)),
                           reverse=True)
            if notas and notas[0][0] >= 0.85 and (len(notas) == 1 or notas[1][0] < notas[0][0] - 0.1):
                iguais = [conectadas[notas[0][1]]]
        if len(iguais) == 1:
            out[p["id"]] = iguais[0]["id"]
    return out


def casar_paginas(S: dict, conectadas: list[dict], idioma: str | None = None) -> dict[str, str]:
    """Roleta page id -> connected Facebook Page id, matched strictly within the same language ('en' vs 'es')."""
    langs = (idioma,) if idioma in ("en", "es") else ("en", "es")
    out = {}
    for lang in langs:
        pags_r = paginas(S, lang)
        con_l = [c for c in conectadas if idioma_de(c) == lang]
        out.update(_casar_grupo(pags_r, con_l))
    return out
