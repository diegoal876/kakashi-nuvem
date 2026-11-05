"""The "Roleta de Páginas" rule, ported from its artifact (gerarPlano) so the panel knows which Page each avatar
posts on each day. The data (pages, avatars, fixed days) is a copy of the artifact's database in roleta.json;
a day fixed there is used as is, any other day is computed exactly like the artifact's preview."""
import re
from datetime import date
from difflib import SequenceMatcher


def _ordem(o: dict):
    # porOrdem: by "ordem", then by name with numbers compared as numbers
    nome = str(o.get("nome", ""))
    return (o.get("ordem") or 0, [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", nome)])


def paginas(S: dict) -> list[dict]:
    return sorted(({**p, "id": k} for k, p in S.get("paginas", {}).items()), key=_ordem)


def avatares(S: dict) -> list[dict]:
    return sorted(({**a, "id": k} for k, a in S.get("avatares", {}).items()), key=_ordem)


def _oficiais(S: dict) -> dict:
    # a paused Page (restricted by Meta) lends its official avatar back to the wheel until it comes back
    return {p["avatarOficial"]: p["id"] for p in paginas(S)
            if p.get("fase") == 3 and not p.get("pausada") and p.get("avatarOficial") in S.get("avatares", {})}


AVATARES_POR_PAGINA = 3  # phases 1-2, one video of each a day (since 02/11/2026; it was 2)


def _num_dia(data: str) -> int:  # numDia: days since 1970-01-01
    return (date.fromisoformat(data) - date(1970, 1, 1)).days


def plano(S: dict, data: str) -> dict[str, list]:
    """Roleta page id -> its avatar ids on that day (YYYY-MM-DD)."""
    dia = S.get("dias", {}).get(data)
    if dia:
        return {pid: e.get("avatares") or [] for pid, e in (dia.get("paginas") or {}).items()}
    k, pags, of = _num_dia(data), [p for p in paginas(S) if not p.get("pausada")], _oficiais(S)  # a paused Page gets nothing
    pool = [a for a in avatares(S) if a.get("ativo") is not False and a["id"] not in of]
    n, res, usados = len(pool), {}, set()
    ultimo: dict[str, dict[str, str]] = {}
    for d in S.get("dias", {}).values():
        if d.get("data", "") >= data:
            continue
        for pid, e in (d.get("paginas") or {}).items():
            for a in e.get("avatares") or []:
                if a:
                    m = ultimo.setdefault(pid, {})
                    if a not in m or m[a] < d["data"]:
                        m[a] = d["data"]
    # each Page of phases 1-2 gets AVATARES_POR_PAGINA avatars: first the one that never (or longest ago) posted there,
    # ties broken by the position on the wheel, which turns that many places a day. The Pages are served in the radar's order
    # ("prioridade"), so when avatars run short the weakest Pages of phase 1 are the ones left without
    for pi, p in enumerate(sorted((x for x in pags if x.get("fase") != 3), key=lambda x: x.get("prioridade") or 999)):
        escolhidos = []
        for s in range(AVATARES_POR_PAGINA):
            alvo = (AVATARES_POR_PAGINA * (pi + k) + s) % n if n else 0
            melhor, m_visto, m_dist = None, "", None
            for ai, a in enumerate(pool):
                if a["id"] in usados:
                    continue
                visto, dist = ultimo.get(p["id"], {}).get(a["id"], ""), (ai - alvo) % n
                if melhor is None or visto < m_visto or (visto == m_visto and dist < m_dist):
                    melhor, m_visto, m_dist = a, visto, dist
            if melhor:
                usados.add(melhor["id"])
            escolhidos.append(melhor["id"] if melhor else None)
        res[p["id"]] = escolhidos
    for p in pags:
        if p.get("fase") == 3:
            o = p.get("avatarOficial")
            res[p["id"]] = [o if o in S.get("avatares", {}) else None]
    return res


def numero_avatar(S: dict, avid: str) -> str | None:
    """"Avatar 03" -> "3" (the number in the video names)."""
    m = re.search(r"(\d+)\s*$", str(S.get("avatares", {}).get(avid, {}).get("nome", "")))
    return str(int(m.group(1))) if m else None


def avatar_para_pagina(S: dict, data: str) -> dict[str, str]:
    """Avatar number -> roleta page id on that day."""
    out = {}
    for pid, avs in plano(S, data).items():
        for a in avs:
            n = a and numero_avatar(S, a)
            if n:
                out[n] = pid
    return out


def _chave(nome: str) -> str:
    return re.sub(r"[^a-z0-9]", "", nome.lower())


def casar_paginas(S: dict, conectadas: list[dict]) -> dict[str, str]:
    """Roleta page id -> connected Facebook Page id, by name ("Mina Choi" ~ "Mina Cho", "Amara" ~ "Amara Wells")."""
    out = {}
    for p in paginas(S):
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
