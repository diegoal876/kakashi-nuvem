"""Posting queue of the panel (tab Postagem): videos go to Facebook Pages and to the Instagram accounts
linked to them, through Meta's Graph API.

Videos come in four ways: sent from the panel, dropped in output/postagem/entrada (moved in), copied from
the Google Drive folder (one subfolder per Page: the Page is the destination and <video>.txt or _legenda.txt
the caption; the file stays in Drive), or picked up when the panel makes a new one (clonagem, tela
dividida, clickbait). Each item has a caption, destinations
("fb:<page id>" / "ig:<page id>") and a time. Facebook more than NATIVO_MIN_S ahead is scheduled at Meta
right away (it goes out even with this PC off); everything else is posted by the worker here when the
time comes, so the panel has to be open then.

Files in output/postagem/: fila.json (queue + settings), contas.json (page tokens; never sent to the page),
videos/ (the queue's own copies), entrada/ (watched folder), _lixeira/ (removed items).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlencode

import roleta

ROOT = Path(__file__).resolve().parents[2]  # flowkit/
OUTPUT = ROOT / "output"
DIR = Path(os.environ.get("POSTAGEM_DIR") or OUTPUT / "postagem")
VIDEOS, ENTRADA, LIXEIRA = DIR / "videos", DIR / "entrada", DIR / "_lixeira"
FILA_FILE, CONTAS_FILE = DIR / "fila.json", DIR / "contas.json"
ROLETA_FILE = DIR / "roleta.json"  # copy of the "Roleta de Paginas" artifact database
# overridable so a fake Graph server can stand in during tests
GRAPH = os.environ.get("POSTAGEM_GRAPH", "https://graph.facebook.com").rstrip("/")
GRAPH_VIDEO = os.environ.get("POSTAGEM_GRAPH_VIDEO", "https://graph-video.facebook.com").rstrip("/")
VERSAO = os.environ.get("GRAPH_VERSION", "v24.0")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
NATIVO_MIN_S = 20 * 60      # Meta wants a scheduled post at least 10 min ahead; below this the worker posts it
MAX_FUTURO_S = 29 * 86400   # the shortest limit Meta has for scheduled videos
ESTAVEL_S = 30              # a Kakashi video this fresh may still be being written
ENVIO_INTERVALO_S = 150     # between two sends to Meta: ~24 an hour, never a burst (a burst got the app blocked)
TOKENS_CHECAR_S = 1800      # between two tests of the Page tokens
IG_ESPERA_S = 10            # between two checks of Instagram's processing
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

lock = threading.RLock()
wake = threading.Event()


class Recusa(ValueError):
    """A request the user has to fix (shown as is)."""


class GraphError(RuntimeError):
    def __init__(self, msg: str, code=None):
        super().__init__(msg)
        self.code = code

    @property
    def bloqueio(self) -> bool:
        """Meta cut the app off as a whole (not one Page or one video): nothing can be posted until it lifts."""
        return self.code == 200 and any(s in str(self) for s in ("API access blocked", "Cannot call API for app"))


# ─── Storage ────────────────────────────────────────────────


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


fila = _load(FILA_FILE, {})
fila.setdefault("seq", 0)
fila.setdefault("itens", [])
fila.setdefault("log", [])
fila.setdefault("config", {}).setdefault("auto_kakashi", True)
fila["config"].setdefault("destinos_padrao", [])
fila["config"].setdefault("pasta_drive", os.environ.get("POSTAGEM_DRIVE", r"G:\Meu Drive\Postagens"))
fila["config"].setdefault("drive_ig", False)
fila.setdefault("drive_vistos", {})  # path inside the Drive folder -> [size, mtime] already copied
fila["config"].setdefault("horarios_padrao", ["10:00", "15:00", "20:00"])  # posting times of every Page...
fila["config"].setdefault("horarios_paginas", {})  # ...unless it has its own: page id -> times
fila["config"].setdefault("rodizio", [])  # page ids the _Rodízio folder shares its videos among
fila["config"].setdefault("avatar_paginas", None)  # avatar number -> page id; None until learned from the history
fila["config"].setdefault("rodizio_cont", {})  # page id -> videos it got from the rotation (breaks ties)
fila["config"].setdefault("limpar_dias", 3)  # posted videos leave the list and this PC after these days
fila["config"].setdefault("sem_agendamento_meta", [])  # page ids where Meta holds SCHEDULED Reels: sent as PUBLISHED at their time


def _salvar() -> None:
    with lock:
        _write(FILA_FILE, fila)


def _log(msg: str) -> None:
    with lock:
        fila["log"] = (fila["log"] + [{"em": int(time.time()), "msg": msg}])[-40:]
        _salvar()


def _item(iid) -> dict | None:
    return next((i for i in fila["itens"] if i["id"] == iid), None)


def contas() -> dict:
    return _load(CONTAS_FILE, {"paginas": []})


def _pagina(pid: str) -> dict | None:
    return next((p for p in contas().get("paginas", []) if p["id"] == pid), None)


def _quando_txt(t) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(t))


def _dentro(base: Path, rel: str) -> Path | None:
    p = (base / rel).resolve()
    return p if base.resolve() in p.parents else None


# ─── Graph API ──────────────────────────────────────────────


def _http(method: str, url: str, data=None, headers=None, timeout=120) -> dict:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body)["error"]
        except (ValueError, KeyError, TypeError):
            raise GraphError(f"HTTP {e.code}: {body[:300]}") from None
        if err.get("code") == 190:
            raise GraphError("token invalido, incompleto ou vencido: gere outro no Graph API Explorer e conecte "
                             "de novo (1. Contas)", 190) from None
        msg = err.get("error_user_msg") or err.get("message") or body[:300]
        raise GraphError(f"{msg} (codigo {err.get('code')})", err.get("code")) from None
    except (urllib.error.URLError, TimeoutError) as e:
        # no network at all (DNS, refused): nothing reached Meta, so it can simply be tried again; a timeout may
        # have delivered the video already, so that one stays an error for the user to look at
        rede = isinstance(e, urllib.error.URLError) and not isinstance(e.reason, TimeoutError)
        raise GraphError(f"sem conexao com a Meta: {getattr(e, 'reason', e)}", "rede" if rede else None) from None


def graph(method: str, path: str, token: str, **params) -> dict:
    url, auth = f"{GRAPH}/{VERSAO}/{path}", {"Authorization": f"OAuth {token}"}
    if method in ("GET", "DELETE"):
        return _http(method, url + ("?" + urlencode(params) if params else ""), headers=auth)
    return _http("POST", url, urlencode(params).encode(), {**auth, "Content-Type": "application/x-www-form-urlencoded"})


def _pedacos(path: Path, antes=b"", depois=b""):
    yield antes
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            yield chunk
    yield depois


def _rupload(url: str, token: str, path: Path) -> dict:
    """The binary upload Reels (Facebook) and resumable Instagram uploads use."""
    size = path.stat().st_size
    return _http("POST", url, _pedacos(path), {"Authorization": f"OAuth {token}", "offset": "0",
                                               "file_size": str(size), "Content-Length": str(size),
                                               "Content-Type": "application/octet-stream"}, timeout=1800)


def e_reels(info: dict) -> bool:
    return info.get("h", 0) > info.get("w", 0) and 3 <= info.get("dur", 0) <= 90


def fb_reels(pagina: dict, path: Path, legenda: str, quando=None) -> dict:
    tok, pid = pagina["token"], pagina["id"]
    r = graph("POST", f"{pid}/video_reels", tok, upload_phase="start")
    vid = r["video_id"]
    up = _rupload(r.get("upload_url") or f"https://rupload.facebook.com/video-upload/{VERSAO}/{vid}", tok, path)
    if up.get("success") is False:
        raise GraphError(f"o envio do arquivo falhou: {up}")
    fim = {"upload_phase": "finish", "video_id": vid, "description": legenda}
    fim.update({"video_state": "SCHEDULED", "scheduled_publish_time": int(quando)} if quando
               else {"video_state": "PUBLISHED"})
    r = graph("POST", f"{pid}/video_reels", tok, **fim)
    if not r.get("success"):
        raise GraphError(f"a Meta nao confirmou a publicacao: {r}")
    return {"id": vid, "tipo": "reels", "link": f"https://www.facebook.com/reel/{vid}"}


def fb_video(pagina: dict, path: Path, legenda: str, quando=None) -> dict:
    """Plain Page video (not vertical, or longer than a Reel): one multipart upload."""
    campos = {"description": legenda}
    if quando:
        campos.update(published="false", scheduled_publish_time=str(int(quando)))
    sep = f"----kakashi{time.time_ns()}"
    antes = "".join(f'--{sep}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in campos.items())
    antes += f'--{sep}\r\nContent-Disposition: form-data; name="source"; filename="{path.name}"\r\n'
    antes += "Content-Type: video/mp4\r\n\r\n"
    depois = f"\r\n--{sep}--\r\n".encode()
    antes = antes.encode()
    size = len(antes) + path.stat().st_size + len(depois)
    r = _http("POST", f"{GRAPH_VIDEO}/{VERSAO}/{pagina['id']}/videos", _pedacos(path, antes, depois),
              {"Authorization": f"OAuth {pagina['token']}", "Content-Type": f"multipart/form-data; boundary={sep}",
               "Content-Length": str(size)}, timeout=1800)
    return {"id": r["id"], "tipo": "video", "link": f"https://www.facebook.com/watch/?v={r['id']}"}


def ig_reels(pagina: dict, path: Path, legenda: str) -> dict:
    tok, ig = pagina["token"], pagina.get("ig_id")
    if not ig:
        raise GraphError("essa Pagina nao tem Instagram ligado")
    r = graph("POST", f"{ig}/media", tok, media_type="REELS", upload_type="resumable", caption=legenda,
              share_to_feed="true")
    cid = r["id"]
    _rupload(r.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{VERSAO}/{cid}", tok, path)
    for _ in range(90):  # Instagram processes the video before it can be published (up to ~15 min)
        st = graph("GET", cid, tok, fields="status_code,status")
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise GraphError(f"o Instagram recusou o video: {st.get('status') or st.get('status_code')}")
        time.sleep(IG_ESPERA_S)
    else:
        raise GraphError("o Instagram demorou demais para processar o video")
    mid = graph("POST", f"{ig}/media_publish", tok, creation_id=cid)["id"]
    try:
        link = graph("GET", mid, tok, fields="permalink").get("permalink", "")
    except GraphError:
        link = ""
    return {"id": mid, "tipo": "reels", "link": link}


def _enviar(dest: str, pagina: dict, path: Path, legenda: str, info: dict, quando=None) -> dict:
    if dest.startswith("ig:"):
        return ig_reels(pagina, path, legenda)
    if e_reels(info):
        return fb_reels(pagina, path, legenda, quando)
    return fb_video(pagina, path, legenda, quando)


# ─── Accounts & AdsPower + API Bulk Setup ───────────────────

FOTOS_PAGINAS = DIR / "fotos_paginas"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".jfif")
AVATAR_DIR_PADRAO = Path(os.environ.get("AVATAR_DIR", r"C:\Users\diego\Downloads\Avatares -20261018T145532Z-1-001\Avatares"))
AVATAR_DRIVE_PADRAO = Path(os.environ.get("AVATAR_DRIVE_DIR", r"G:\Meu Drive\Non Shop\001\Avatares"))


def _mapa_fotos_avatares() -> dict[int, Path]:
    """Avatar number -> photo file path (from local avatar folder and Google Drive Avatares folder)."""
    fotos: dict[int, Path] = {}
    for pasta in (AVATAR_DIR_PADRAO, AVATAR_DRIVE_PADRAO):
        if not pasta.is_dir():
            continue
        for f in pasta.iterdir():
            if f.is_file() and f.name.lower().endswith(IMAGE_EXTS):
                m = re.search(r"\d+", f.name)
                if m:
                    fotos[int(m.group())] = f
    return dict(sorted(fotos.items()))


def _perfis_lista(c: dict) -> list[dict]:
    """Connected Facebook profiles (AdsPower accounts). Migrates legacy single token_usuario on the fly."""
    perfis = [dict(pf) for pf in (c.get("perfis") or []) if isinstance(pf, dict) and pf.get("token_usuario")]
    if not perfis and c.get("token_usuario"):
        perfis.append({
            "id": c.get("usuario_id") or "principal",
            "nome": c.get("usuario_nome") or "Conta Principal",
            "rotulo": c.get("usuario_rotulo") or "Conta de postagem dos videos",
            "token_usuario": c["token_usuario"],
            "token_vence": c.get("token_vence", 0),
            "longo": bool(c.get("longo", False)),
            "conectado_em": c.get("conectado_em", 0),
        })
    return perfis


def _buscar_paginas(token: str) -> list[dict]:
    try:
        r = graph("GET", "me/accounts", token,
                  fields="id,name,access_token,about,picture{url,is_silhouette},instagram_business_account{id,username}",
                  limit=100)
    except GraphError:
        r = graph("GET", "me/accounts", token, fields="id,name,access_token,instagram_business_account{id,username}",
                  limit=100)
    paginas = []
    for p in r.get("data", []):
        ig = p.get("instagram_business_account") or {}
        pic = (p.get("picture") or {}).get("data") or {}
        item = {"id": p["id"], "nome": p["name"].strip(), "token": p["access_token"],
                "ig_id": ig.get("id", ""), "ig_nome": ig.get("username", "")}
        if "about" in p or pic:
            item["bio"] = (p.get("about") or "").strip()
            item["foto_url"] = pic.get("url", "")
            item["sem_foto"] = bool(pic.get("is_silhouette", False))
        paginas.append(item)
    if not paginas:
        raise Recusa("Esse token nao ve nenhuma Pagina. Gere de novo marcando pages_show_list e autorizando as Paginas.")
    return paginas


def _mesclar_pagina(antiga: dict | None, nova: dict, perfil_id: str = "", rotulo: str = "") -> dict:
    """Keep local metadata (configurada, avatar_foto, criada_em) when refreshing a Page from Meta."""
    base = dict(antiga or {})
    base.update(nova)
    if perfil_id:
        base["perfil_id"] = perfil_id
    if rotulo:
        base["perfil_rotulo"] = rotulo
    if base.get("bio") and not base.get("sem_foto", False):
        base.setdefault("configurada", True)
    return base


def _registrar_novas_sem_sched(novos_ids: list[str]) -> None:
    """New Pages default to direct publishing at the scheduled time so Meta's native scheduler never holds them."""
    if not novos_ids:
        return
    with lock:
        sem_sched = fila["config"].setdefault("sem_agendamento_meta", [])
        mudou = False
        for pid in novos_ids:
            if pid not in sem_sched:
                sem_sched.append(pid)
                mudou = True
        if mudou:
            _salvar()


def _roleta_entrar(paginas: list[dict], antes: set[str]) -> list[str]:
    """A Page that just showed up on Facebook joins the Roleta in Phase 1, as "Adicionar pagina do dia" does there,
    unless the Roleta already has one by that name. The posting queue follows the new Roleta by itself.
    The names are matched against every connected Page, so a new "Vida in natura" is not taken for the
    Roleta's "Vida natural", which already belongs to the connected "Vida natural"."""
    entram = []
    novas = [p for p in paginas if p["id"] not in antes]
    with roleta_lock:
        S = roleta_dados()
        if not S or not novas:
            return []
        casadas = set(roleta.casar_paginas(S, paginas).values())
        ordem = max([p.get("ordem") or 0 for p in S["paginas"].values()] + [0])
        for p in novas:
            if p["id"] in casadas:
                continue
            ordem += 1
            S["paginas"][f"fb{p['id']}"] = {
                "nome": p["nome"], "fase": 1, "ordem": ordem, "criadaEm": datetime.now().date().isoformat(),
                "formatoFixo": "", "avatarOficial": None, "formatoViral": "", "novoViral": "", "historico": []}
            entram.append(p["nome"])
        if entram:
            S["alterada_em"] = int(time.time())
            _write(ROLETA_FILE, S)
    return entram


def atualizar_paginas(_body: dict | None = None) -> str:
    """Fetch the Page list again across all connected Facebook profiles (AdsPower): new Pages (and newly linked
    Instagrams) come in, with their Drive folder and Phase 1 Roleta entry."""
    c = contas()
    perfis = _perfis_lista(c)
    if not perfis:
        raise Recusa("Conecte uma vez pelo Graph API Explorer (Atualizar conexao) para liberar este botao.")
    agora = time.time()
    ativos = [pf for pf in perfis if pf.get("token_vence", 0) > agora]
    if not ativos:
        raise Recusa("O token de usuario venceu (dura cerca de 60 dias). Gere outro no Graph API Explorer e use "
                     "Atualizar conexao; a postagem continua funcionando enquanto isso.")
    antes = {p["id"]: p for p in c.get("paginas", [])}
    por_id: dict[str, dict] = {}
    erros_pf = []
    perfis_lidos = set()
    for pf in ativos:
        try:
            lote = _buscar_paginas(pf["token_usuario"])
            perfis_lidos.add(pf.get("id") or "")
            for p in lote:
                por_id[p["id"]] = _mesclar_pagina(por_id.get(p["id"]) or antes.get(p["id"]), p,
                                                  pf.get("id", ""), pf.get("rotulo") or pf.get("nome", ""))
        except (GraphError, Recusa) as e:
            erros_pf.append(f"{pf.get('rotulo') or pf.get('nome') or 'Perfil'}: {e}")
    if not por_id and erros_pf:
        raise Recusa(f"A Meta nao deixou atualizar: {'; '.join(erros_pf)}")
    if len(perfis) > 1:
        for pid, p in antes.items():
            if pid not in por_id and (p.get("perfil_id") or "") not in perfis_lidos:
                por_id[pid] = p
    paginas = list(por_id.values())
    novos_ids = [p["id"] for p in paginas if p["id"] not in antes]
    novas = [p["nome"] for p in paginas if p["id"] not in antes]
    na_roleta = _roleta_entrar(paginas, set(antes))
    _registrar_novas_sem_sched(novos_ids)
    sairam = [p["nome"] for pid, p in antes.items() if pid not in por_id]
    ig_novos = [p["nome"] for p in paginas if p["ig_id"] and p["id"] in antes and not antes[p["id"]].get("ig_id")]
    c.update(paginas=paginas, perfis=perfis, atualizado_em=int(time.time()))
    _write(CONTAS_FILE, c)
    try:
        criar_pastas_drive()
    except OSError:
        pass
    partes = ([f"Pagina(s) nova(s): {', '.join(novas)}"] if novas else []) + \
        ([f"entrou na Roleta na Fase 1: {', '.join(na_roleta)}"] if na_roleta else []) + \
        ([f"Instagram ligado agora: {', '.join(ig_novos)}"] if ig_novos else []) + \
        ([f"sairam (sem acesso): {', '.join(sairam)}"] if sairam else []) + \
        ([f"aviso: {'; '.join(erros_pf)}"] if erros_pf else [])
    return ". ".join(partes) + "." if partes else f"Nada novo: as mesmas {len(paginas)} Paginas."


def conectar(body: dict) -> str:
    """User token (Graph API Explorer) -> the Pages it manages, each with its own token and linked Instagram.
    Supports Multi-Perfil (AdsPower): by default ('somar') merges the new profile's Pages with existing profiles."""
    token = str(body.get("token") or "").strip()
    if not token:
        raise Recusa("Cole o token de usuario.")
    atual = contas()
    app_id = str(body.get("app_id") or "").strip() or atual.get("app_id", "")
    secret = str(body.get("app_secret") or "").strip() or atual.get("app_secret", "")
    rotulo = str(body.get("rotulo") or "").strip()
    modo = str(body.get("modo") or "somar").strip().lower()
    longo, vence = False, int(time.time()) + 3600
    if app_id and secret:
        r = _http("GET", f"{GRAPH}/{VERSAO}/oauth/access_token?" + urlencode(
            {"grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret, "fb_exchange_token": token}))
        token, longo = r["access_token"], True
        vence = int(time.time()) + int(r.get("expires_in") or 60 * 86400)  # long-lived user tokens last ~60 days
    lote = _buscar_paginas(token)
    try:
        me = graph("GET", "me", token, fields="id,name")
        me_id = str(me.get("id") or "").strip()
        me_nome = str(me.get("name") or "").strip()
    except GraphError:
        me_id, me_nome = "", ""
    if not me_id:
        me_id = "pf_" + hashlib.md5((rotulo or lote[0]["id"]).encode()).hexdigest()[:10]
    if not me_nome:
        me_nome = rotulo or f"Perfil ({len(lote)} Paginas)"
    rotulo_final = rotulo or me_nome
    agora = int(time.time())
    antes_map = {p["id"]: p for p in atual.get("paginas", [])}
    ja = set(antes_map)
    perfis_atuais = _perfis_lista(atual)
    if len(perfis_atuais) == 1:
        pf0 = perfis_atuais[0]
        for p in antes_map.values():
            p.setdefault("perfil_id", pf0.get("id", "principal"))
            p.setdefault("perfil_rotulo", pf0.get("rotulo") or pf0.get("nome", "Conta principal"))
    novo_pf = {"id": me_id, "nome": me_nome, "rotulo": rotulo_final, "token_usuario": token,
               "token_vence": vence, "longo": longo, "conectado_em": agora}
    if modo == "substituir" or not perfis_atuais:
        perfis_finais = [novo_pf]
        paginas = [_mesclar_pagina(antes_map.get(p["id"]), p, me_id, rotulo_final) for p in lote]
    else:
        perfis_finais = []
        trocou = False
        for pf in perfis_atuais:
            if pf.get("id") == me_id or (rotulo and pf.get("rotulo", "").lower() == rotulo.lower()) or (
                    pf.get("id") == "principal" and len(perfis_atuais) == 1 and any(p["id"] in ja for p in lote)):
                novo_pf["rotulo"] = rotulo or pf.get("rotulo") or me_nome
                rotulo_final = novo_pf["rotulo"]
                perfis_finais.append(novo_pf)
                trocou = True
            else:
                perfis_finais.append(pf)
        if not trocou:
            perfis_finais.append(novo_pf)
        por_id = dict(antes_map)
        for p in lote:
            por_id[p["id"]] = _mesclar_pagina(por_id.get(p["id"]), p, me_id, rotulo_final)
        paginas = list(por_id.values())
    melhor_pf = max(perfis_finais, key=lambda x: x.get("token_vence", 0))
    _write(CONTAS_FILE, {"app_id": app_id, "app_secret": secret, "longo": bool(melhor_pf.get("longo")),
                         "conectado_em": agora, "token_usuario": melhor_pf["token_usuario"],
                         "token_vence": melhor_pf["token_vence"], "perfis": perfis_finais, "paginas": paginas})
    with lock:  # Meta answered, so whatever block there was is over
        if [fila.pop(k, None) for k in ("bloqueio", "tokens_caidos")] != [None, None]:
            _salvar()
    n_ig = sum(1 for p in paginas if p["ig_id"])
    try:
        novas = criar_pastas_drive()
    except OSError:
        novas = None
    novos_ids = [p["id"] for p in paginas if p["id"] not in ja]
    if ja and novos_ids:
        _registrar_novas_sem_sched(novos_ids)
    na_roleta = _roleta_entrar(paginas, ja) if ja else []
    msg_pf = f" ({len(perfis_finais)} perfis somados)" if len(perfis_finais) > 1 else ""
    return f"{len(paginas)} Pagina(s) conectada(s){msg_pf}, {n_ig} com Instagram." + (
        "" if longo else " Sem o App Secret o token vence em cerca de 1 hora.") + (
        f" {novas} pasta(s) nova(s) no Google Drive." if novas else "") + (
        f" Entrou na Roleta na Fase 1: {', '.join(na_roleta)}." if na_roleta else "")


def remover_perfil(body: dict) -> str:
    """Remove a specific Facebook profile (AdsPower account) from contas.json."""
    pid = str(body.get("id") or "").strip()
    tirar_pags = bool(body.get("remover_paginas", False))
    c = contas()
    perfis = _perfis_lista(c)
    alvo = next((pf for pf in perfis if pf.get("id") == pid), None)
    if not alvo:
        raise Recusa("Perfil nao encontrado.")
    restantes = [pf for pf in perfis if pf.get("id") != pid]
    paginas = c.get("paginas", [])
    if tirar_pags:
        paginas = [p for p in paginas if p.get("perfil_id") != pid]
    if not restantes and not paginas:
        CONTAS_FILE.unlink(missing_ok=True)
        return f"Perfil '{alvo.get('rotulo') or alvo.get('nome')}' removido e contas desconectadas."
    melhor = max(restantes, key=lambda x: x.get("token_vence", 0)) if restantes else {}
    c.update(perfis=restantes, paginas=paginas,
             token_usuario=melhor.get("token_usuario", ""), token_vence=melhor.get("token_vence", 0))
    _write(CONTAS_FILE, c)
    return f"Perfil '{alvo.get('rotulo') or alvo.get('nome')}' removido. {len(paginas)} Pagina(s) continuam ativas."


def _enviar_foto_pagina(pagina: dict, img_path: Path) -> dict:
    """Upload a profile picture to a Facebook Page via Graph API POST /{page-id}/picture."""
    if not img_path or not img_path.is_file():
        raise Recusa("Foto nao encontrada para enviar.")
    tmp_jpg = None
    alvo = img_path
    if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        DIR.mkdir(parents=True, exist_ok=True)
        tmp_jpg = DIR / f"_tmp_pic_{time.time_ns()}.jpg"
        res = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(img_path),
                              "-frames:v", "1", "-q:v", "2", str(tmp_jpg)],
                             capture_output=True, creationflags=NO_WINDOW)
        if res.returncode == 0 and tmp_jpg.is_file():
            alvo = tmp_jpg
    try:
        mime = "image/png" if alvo.suffix.lower() == ".png" else "image/jpeg"
        nome_arq = "avatar.png" if mime == "image/png" else "avatar.jpg"
        sep = f"----kakashipic{time.time_ns()}"
        antes = (
            f'--{sep}\r\nContent-Disposition: form-data; name="redirect"\r\n\r\nfalse\r\n'
            f'--{sep}\r\nContent-Disposition: form-data; name="source"; filename="{nome_arq}"\r\n'
            f'Content-Type: {mime}\r\n\r\n'
        ).encode()
        depois = f"\r\n--{sep}--\r\n".encode()
        body = antes + alvo.read_bytes() + depois
        req = urllib.request.Request(
            f"{GRAPH}/{VERSAO}/{pagina['id']}/picture?redirect=false",
            data=body,
            method="POST",
            headers={"Authorization": f"OAuth {pagina['token']}",
                     "Content-Type": f"multipart/form-data; boundary={sep}",
                     "Content-Length": str(len(body))},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {"success": True}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(err_body)["error"]
                msg = err.get("error_user_msg") or err.get("message") or err_body[:300]
                raise GraphError(f"{msg} (codigo {err.get('code')})", err.get("code")) from None
            except (ValueError, KeyError, TypeError):
                raise GraphError(f"HTTP {e.code}: {err_body[:300]}") from None
    finally:
        if tmp_jpg:
            tmp_jpg.unlink(missing_ok=True)


def configurar_paginas_massa(body: dict) -> str:
    """Configure multiple Pages in bulk via Meta's Official Graph API (profile picture, bio/about, website)
    plus automatic Kakashi setup (Google Drive folder, Roleta Phase 1, Rodizio, and direct publishing)."""
    itens = body.get("itens") or []
    if not itens:
        raise Recusa("Selecione pelo menos uma Pagina para configurar.")
    criar_drv = bool(body.get("criar_drive", True))
    por_roleta = bool(body.get("entrar_roleta", True))
    por_rodizio = bool(body.get("entrar_rodizio", True))
    envio_dir = bool(body.get("envio_direto", True))
    c = contas()
    paginas = c.get("paginas", [])
    por_id = {p["id"]: p for p in paginas}
    fotos_av = _mapa_fotos_avatares()
    n_foto, n_bio, n_cfg = 0, 0, 0
    erros_api = []
    alvos_pags = []
    fez_chamada_meta = False
    for it in itens:
        pid = str(it.get("id") or "").strip()
        p = por_id.get(pid)
        if not p:
            continue
        alvos_pags.append(p)
        if fez_chamada_meta:
            time.sleep(0.8)
            fez_chamada_meta = False
        bio = str(it.get("bio") or "").strip()
        site = str(it.get("site") or "").strip()
        av_num = it.get("avatar_num")
        usar_up = bool(it.get("usar_upload"))
        if bio or site:
            if bio != (p.get("bio") or "") or site:
                params = {}
                if bio:
                    params["about"] = bio[:255]
                if site:
                    params["website"] = site
                try:
                    fez_chamada_meta = True
                    graph("POST", pid, p["token"], **params)
                    if bio:
                        p["bio"] = bio[:255]
                    p["configurada"] = True
                    n_bio += 1
                except GraphError as e:
                    erros_api.append(f"{p['nome']} (bio): {e}")
        img_path = None
        if usar_up:
            cand = FOTOS_PAGINAS / f"{pid}.jpg"
            if cand.is_file():
                img_path = cand
        elif av_num is not None and str(av_num).strip().isdigit():
            img_path = fotos_av.get(int(av_num))
        if img_path:
            try:
                fez_chamada_meta = True
                _enviar_foto_pagina(p, img_path)
                p["sem_foto"] = False
                p["configurada"] = True
                p["avatar_foto"] = f"upload:{pid}" if usar_up else int(av_num)
                try:
                    pic_r = graph("GET", f"{pid}/picture", p["token"], redirect="false", fields="url,is_silhouette")
                    pic_d = pic_r.get("data") or pic_r
                    if pic_d.get("url"):
                        p["foto_url"] = pic_d["url"]
                except GraphError:
                    pass
                n_foto += 1
            except (GraphError, Recusa) as e:
                erros_api.append(f"{p['nome']} (foto): {e}")
        if not bio and not img_path:
            p["configurada"] = True
        n_cfg += 1
    if not alvos_pags:
        raise Recusa("Nenhuma das Paginas selecionadas esta conectada.")
    _write(CONTAS_FILE, c)
    na_roleta = []
    if por_roleta:
        with roleta_lock:
            S = roleta_dados()
            casadas = set(roleta.casar_paginas(S, paginas).values()) if S else set()
        na_roleta = _roleta_entrar(paginas, casadas - {p["id"] for p in alvos_pags})
    with lock:
        if por_rodizio:
            rod = fila["config"].setdefault("rodizio", [])
            for p in alvos_pags:
                if p["id"] not in rod:
                    rod.append(p["id"])
        if envio_dir:
            sem_sched = fila["config"].setdefault("sem_agendamento_meta", [])
            for p in alvos_pags:
                if p["id"] not in sem_sched:
                    sem_sched.append(p["id"])
        _salvar()
    novas_drv = None
    if criar_drv:
        try:
            novas_drv = criar_pastas_drive()
        except OSError:
            novas_drv = None
    partes = [f"{n_cfg} Pagina(s) configurada(s)"]
    if n_foto:
        partes.append(f"{n_foto} foto(s) de perfil enviada(s) na Meta")
    if n_bio:
        partes.append(f"{n_bio} bio(s) atualizada(s) na Meta")
    if na_roleta:
        partes.append(f"entrou na Fase 1 da Roleta: {', '.join(na_roleta)}")
    if novas_drv:
        partes.append(f"{novas_drv} pasta(s) criada(s) no Drive")
    resumo = ", ".join(partes) + "."
    if erros_api:
        falta_perm = any("permission" in e.lower() or "200" in e or "100" in e for e in erros_api)
        dica = " (Para alterar foto/bio via API, inclua a permissao pages_manage_metadata ao gerar o token no Graph API Explorer.)" if falta_perm else ""
        resumo += f" Avisos da Meta: {'; '.join(erros_api[:4])}{dica}"
    _log(f"Configuracao em massa (AdsPower + API): {resumo}")
    return resumo


def contas_publico() -> dict:
    c = contas()
    pags = c.get("paginas", [])
    perfis = _perfis_lista(c)
    agora = time.time()
    vence = max([pf.get("token_vence", 0) for pf in perfis] + [c.get("token_vence", 0)])
    pode_at = any(pf.get("token_vence", 0) > agora for pf in perfis) or bool(c.get("token_usuario"))
    sem_sched = set(fila["config"].get("sem_agendamento_meta") or [])
    return {
        "app_id": c.get("app_id", ""),
        "tem_secret": bool(c.get("app_secret")),
        "longo": c.get("longo", False) or any(pf.get("longo") for pf in perfis),
        "conectado_em": c.get("conectado_em", 0),
        "pode_atualizar": pode_at,
        "token_vence": vence,
        "perfis": [{
            "id": pf.get("id", ""),
            "nome": pf.get("nome", ""),
            "rotulo": pf.get("rotulo") or pf.get("nome") or "Perfil Facebook",
            "conectado_em": pf.get("conectado_em", 0),
            "token_vence": pf.get("token_vence", 0),
            "longo": bool(pf.get("longo", False)),
            "paginas": sum(1 for p in pags if p.get("perfil_id") == pf.get("id")) or (len(pags) if len(perfis) == 1 else 0),
        } for pf in perfis],
        "paginas": [{
            "id": p["id"],
            "nome": p["nome"],
            "ig_id": p.get("ig_id", ""),
            "ig_nome": p.get("ig_nome", ""),
            "perfil_id": p.get("perfil_id", ""),
            "perfil_rotulo": p.get("perfil_rotulo", ""),
            "bio": p.get("bio", ""),
            "foto_url": p.get("foto_url", ""),
            "sem_foto": bool(p.get("sem_foto", False)),
            "configurada": bool(p.get("configurada", False)),
            "avatar_foto": p.get("avatar_foto"),
            "tem_upload_foto": (FOTOS_PAGINAS / f"{p['id']}.jpg").is_file(),
            "envio_direto": p["id"] in sem_sched,
        } for p in pags],
    }


def _validar_destinos(destinos) -> list[str]:
    ok = []
    for d in destinos or []:
        rede, _, pid = str(d).partition(":")
        p = _pagina(pid)
        if rede not in ("fb", "ig") or not p:
            raise Recusa("Um dos destinos nao esta mais conectado. Atualize a pagina.")
        if rede == "ig" and not p.get("ig_id"):
            raise Recusa(f"A Pagina {p['nome']} nao tem Instagram ligado.")
        if d not in ok:
            ok.append(d)
    return ok


# ─── Videos coming in ───────────────────────────────────────


def probe(path: Path) -> dict:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,pix_fmt:stream_tags=rotate"
                          ":stream_side_data=rotation:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    try:
        d = json.loads(res.stdout or "{}")
    except ValueError:
        d = {}
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    w, h = v.get("width", 0), v.get("height", 0)
    rot = v.get("tags", {}).get("rotate") or next((x.get("rotation") for x in v.get("side_data_list", [])
                                                     if "rotation" in x), 0)
    if abs(int(float(rot or 0))) % 180 == 90:  # phone videos: stored sideways, shown upright
        w, h = h, w
    try:
        dur = round(float(d.get("format", {}).get("duration") or 0), 1)
    except ValueError:
        dur = 0
    return {"w": w, "h": h, "dur": dur, "vcodec": v.get("codec_name", ""), "pix": v.get("pix_fmt", ""),
            "acodec": a.get("codec_name", "")}


def avisos(info: dict) -> list[str]:
    if not info.get("w"):
        return []
    out, w, h, dur = [], info["w"], info["h"], info["dur"]
    if h <= w:
        out.append("Nao e vertical: no Facebook vai como video comum e no Instagram o Reels fica com faixas.")
    elif abs(w / h - 9 / 16) > 0.03:
        out.append("Vertical, mas nao e 9:16: pode ser cortado nas bordas.")
    if dur < 3:
        out.append("Curto demais: Reels pede pelo menos 3 s.")
    if h > w and dur > 90:
        out.append("Mais de 90 s: no Facebook vai como video comum (Reels vai ate 90 s).")
    if dur > 15 * 60:
        out.append("Mais de 15 min: o Instagram nao aceita.")
    if not info.get("acodec"):
        out.append("Video sem audio.")
    return out


def _compativel(path: Path, info: dict) -> bool:
    return (path.suffix.lower() == ".mp4" and info["vcodec"] == "h264" and info["pix"] in ("yuv420p", "")
            and info["acodec"] in ("aac", ""))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:50] or "video"


def adicionar(src: Path, origem: str, nome: str, mover=False, destinos=None, legenda="", quando=None) -> dict:
    """Copy (or move) a video into the queue as a draft; checking and converting run in the background."""
    VIDEOS.mkdir(parents=True, exist_ok=True)
    with lock:
        seq = fila["seq"] = fila["seq"] + 1
    dest = VIDEOS / f"{seq:04d}_{_slug(Path(nome).stem)}{src.suffix.lower()}"
    (shutil.move if mover else shutil.copy2)(str(src), str(dest))
    item = {"id": seq, "origem": origem, "nome": nome, "arquivo": dest.name, "fonte": "" if mover else str(src),
            "legenda": legenda,
            "destinos": list(destinos or []), "quando": quando,  # no Page until the user picks one (or its Drive folder says)
            "status": "preparando", "andamento": "Conferindo o video...", "resultados": {}, "info": {},
            "avisos": [], "erro": "", "criado": int(time.time())}
    with lock:
        fila["itens"].append(item)
        _salvar()
    threading.Thread(target=_preparar, args=(seq,), daemon=True).start()
    return item


def _preparar(iid: int) -> None:
    with lock:
        item = _item(iid)
        if not item:
            return
        path = VIDEOS / item["arquivo"]
    info, erro = probe(path), ""
    if info["w"] and not _compativel(path, info):
        with lock:
            item["andamento"] = "Convertendo para MP4 (H.264 + AAC)..."
        conv = path.with_name(path.stem + "_mp4.mp4")
        res = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                              "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(conv)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        if res.returncode == 0:
            path.unlink(missing_ok=True)
            path, info = conv, probe(conv)
        else:
            conv.unlink(missing_ok=True)
            erro = "Nao consegui converter para MP4: " + (res.stderr.strip().splitlines() or ["ffmpeg falhou"])[-1][:200]
    if not info["w"]:
        erro = "Nao consegui ler este video (arquivo corrompido ou formato estranho)."
    with lock:
        item = _item(iid)
        if not item:
            return
        item.update(arquivo=path.name, info=info, avisos=avisos(info) + aviso_nome(item), andamento="", erro=erro,
                    status="erro" if erro else "rascunho")
        _salvar()


def _fontes_kakashi() -> list[tuple[str, Path]]:
    """Finished videos the other tabs make: (origin, file)."""
    out = []
    clones = OUTPUT / "clone"
    if clones.is_dir():
        for c in clones.iterdir():
            if c.is_dir() and not c.name.startswith("_"):
                out += [("kakashi", v) for v in (c / "final").glob("final_*.mp4") if v.name != "final_sem_legenda.mp4"]
    out += [("tela_dividida", v) for v in (OUTPUT / "tela_dividida" / "saidas").glob("*.mp4")]
    out += [("clickbait", v) for v in (OUTPUT / "clickbait" / "saidas").glob("*.mp4")]
    out += [("lipsync", v) for v in (OUTPUT / "lipsync" / "saidas").glob("*.mp4") if not v.stem.endswith("_sem_legenda")]
    return out


def _nome_fonte(origem: str, p: Path) -> str:
    return f"{p.parents[1].name} · {p.stem.removeprefix('final_')}.mp4" if origem == "kakashi" else p.name


def _marcar_vistos() -> None:
    """Everything that exists now counts as seen: only videos made from here on enter by themselves."""
    vistos = {}
    for _, p in _fontes_kakashi():
        try:
            vistos[p.relative_to(OUTPUT).as_posix()] = int(p.stat().st_mtime)
        except OSError:
            pass
    with lock:
        fila["vistos"] = vistos
        _salvar()


def _scan_kakashi() -> None:
    if not fila["config"]["auto_kakashi"]:
        return
    agora, novos = time.time(), []
    with lock:
        vistos = fila["vistos"]
        for origem, p in _fontes_kakashi():
            try:
                mt = int(p.stat().st_mtime)
            except OSError:
                continue
            rel = p.relative_to(OUTPUT).as_posix()
            if vistos.get(rel) == mt or agora - mt < ESTAVEL_S:
                continue
            vistos[rel] = mt  # a video made again (new file) comes in again
            novos.append((origem, p))
        if novos:
            _salvar()
    for origem, p in novos:
        try:
            adicionar(p, origem, _nome_fonte(origem, p))
            _log(f"Entrou na fila: {_nome_fonte(origem, p)}")
        except OSError as e:
            _log(f"Nao consegui copiar {p.name}: {e}")


_tamanhos: dict[str, int] = {}


def _scan_entrada() -> None:
    """A file dropped in the folder comes in once its size stops changing (the copy finished)."""
    ENTRADA.mkdir(parents=True, exist_ok=True)
    vivos = {}
    for f in ENTRADA.iterdir():
        if not (f.is_file() and f.suffix.lower() in VIDEO_EXTS):
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if size and _tamanhos.get(f.name) == size:
            try:
                adicionar(f, "pasta", f.name, mover=True)
                _log(f"Entrou na fila (pasta de entrada): {f.name}")
                continue
            except OSError:  # still locked by whatever is copying it
                pass
        vivos[f.name] = size
    _tamanhos.clear()
    _tamanhos.update(vivos)


INVALIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _drive() -> Path:
    return Path(fila["config"]["pasta_drive"])


def _pastas_paginas() -> dict[str, dict]:
    """Drive subfolder name -> Page. Two Pages with the same name get the id after it."""
    paginas = contas().get("paginas", [])
    nomes = [INVALIDOS.sub("", p["nome"]).strip().rstrip(".") or "Pagina" for p in paginas]
    return {(n if nomes.count(n) == 1 else f"{n} ({p['id']})"): p for n, p in zip(nomes, paginas)}


RODIZIO = "_Rodízio"  # Drive subfolder whose videos are shared among the Pages in fila["config"]["rodizio"]
LEIA_ME = """COMO ORGANIZAR OS VÍDEOS PARA POSTAGEM
======================================

Cada pasta aqui dentro tem o nome de uma Página. O vídeo colocado na pasta
de uma Página vai ser postado nessa Página.

  Nome da Página\\
      receita bolo.mp4
      receita bolo.txt      <- legenda deste vídeo (mesmo nome do vídeo)
      _legenda.txt          <- legenda dos vídeos da pasta que não têm .txt próprio

  _Rodízio\\
      Avatar 27 copy 25.mp4 <- sem Página definida: o painel olha o número do avatar
                               no nome e manda para a Página desse avatar (mapa em
                               "Rodízio e horários" no painel). Avatar fora do mapa
                               vai para a Página do rodízio com menos vídeos na fila.

Regras:
- Vídeo vertical (9:16), MP4, de 3 a 90 segundos.
- Nome do arquivo sem caracteres estranhos; o .txt da legenda com o mesmo nome do vídeo.
- Não renomeie nem apague as pastas das Páginas.
- Não tire o vídeo da pasta depois de colocado: o painel copia e lembra o que já pegou.
- Para o painel achar a Página pelo avatar, o nome tem que ter "Avatar" e o número
  (ex.: "Avatar 27 copy 25.mp4"). Duas copys do mesmo avatar: mude só o "copy N".

Cada vídeo entra na fila do painel como rascunho, já com a Página e o próximo
horário livre dela. Nada é postado sem revisão.
"""


def criar_pastas_drive() -> int | None:
    """One subfolder per connected Page, the rotation folder and the guide for whoever fills them.
    None when the Drive is not there (app closed, other letter)."""
    base = _drive()
    if not base.is_dir():
        if not base.parent.is_dir():
            return None
        base.mkdir()
    novas = 0
    for nome in [*_pastas_paginas(), RODIZIO]:
        if not (base / nome).is_dir():
            (base / nome).mkdir()
            novas += 1
    guia = base / "LEIA-ME.txt"
    try:  # the panel's own guide is kept current; one somebody rewrote is left alone
        atual = guia.read_text(encoding="utf-8-sig") if guia.exists() else None
    except (OSError, UnicodeDecodeError):
        atual = ""
    if atual is None or atual != LEIA_ME and atual.startswith(LEIA_ME.splitlines()[0]):
        guia.write_text(LEIA_ME, encoding="utf-8-sig")
    return novas


def _horarios(pid: str) -> list[tuple[int, int]]:
    cfg = fila["config"]
    lista = cfg["horarios_paginas"].get(pid) or cfg["horarios_padrao"]
    return sorted(tuple(map(int, h.split(":"))) for h in lista)


def _horario(pid: str, dia: datetime, h: int, m: int = 0) -> int:
    """A Page's posting time on a day, a few broken minutes past it (h:02, h:17...): the Pages do not all post on
    the hour. The same Page, day and time always get the same minutes, so a taken slot is recognized as taken."""
    quebra = int(hashlib.md5(f"{pid}|{dia:%Y-%m-%d}|{h}:{m}".encode()).hexdigest(), 16) % 27 + 2  # 2-28 min
    return int(dia.replace(hour=h, minute=m).timestamp()) + quebra * 60


def _livre_no_dia(pid: str, dia: datetime, agora: float) -> int | None:
    """The Page's first posting time on that day no other video of it holds yet (from 15 min from now)."""
    ocupados = {i["quando"] for i in fila["itens"] if i.get("quando") and i["quando"] > agora
                and any(d.partition(":")[2] == pid for d in i["destinos"])}
    for h, m in _horarios(pid):
        t = _horario(pid, dia, h, m)
        if t > agora + 15 * 60 and t not in ocupados:
            return t
    return None


def _dias(n=29) -> list[datetime]:
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return [hoje + timedelta(days=d) for d in range(n)]


def proximo_horario(pid: str) -> int | None:
    """The Page's next posting time no other video of it holds yet (from 15 min from now, up to 29 days)."""
    agora = time.time()
    return next((t for dia in _dias() if (t := _livre_no_dia(pid, dia, agora))), None)


def roleta_dados() -> dict | None:
    return _load(ROLETA_FILE, None) if ROLETA_FILE.exists() else None


ROLETA_COLECOES = ("paginas", "avatares", "dias", "config")
ROLETA_ID = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
roleta_lock = threading.Lock()


def _roleta_versao() -> str:
    try:
        return str(ROLETA_FILE.stat().st_mtime_ns)
    except OSError:
        return "0"


def roleta_db() -> dict:
    """Everything the Roleta page (/postagem-roleta) shows; versao changes with every save."""
    with roleta_lock:
        S = roleta_dados() or {}
        return {"versao": _roleta_versao(), "dados": {c: S.get(c, {}) for c in ROLETA_COLECOES}}


def _mesclar(a: dict, b: dict) -> dict:
    """update() as the page expects it on claude.ai: nested objects merge, anything else is replaced."""
    out = dict(a)
    for k, v in b.items():
        out[k] = _mesclar(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def roleta_gravar(body: dict) -> dict:
    """One write from the Roleta page: {op: set|update|delete, caminho: "paginas/pg01", dados}. The panel's worker
    sees the new file and places its avatar drafts again by the changed Roleta."""
    op, caminho, dados = body.get("op"), str(body.get("caminho") or ""), body.get("dados")
    col, _, doc = caminho.partition("/")
    if col not in ROLETA_COLECOES or not ROLETA_ID.match(doc) or op not in ("set", "update", "delete"):
        raise Recusa("Gravacao invalida da Roleta.")
    if op != "delete" and not isinstance(dados, dict):
        raise Recusa("Gravacao invalida da Roleta.")
    with roleta_lock:
        S = roleta_dados() or {}
        docs = S.setdefault(col, {})
        if op == "set":
            docs[doc] = dados
        elif op == "update":
            if doc not in docs:
                raise Recusa("Esse item da Roleta nao existe mais. Recarregue a pagina.")
            docs[doc] = _mesclar(docs[doc], dados)
        else:
            docs.pop(doc, None)
        S["alterada_em"] = int(time.time())
        _write(ROLETA_FILE, S)
        versao = _roleta_versao()
    wake.set()  # the queue follows now, not at the next pass
    return {"versao": versao}


FIM_DO_DIA_H = 6  # a Roleta day's posts may run until 6h the next morning
INTERVALO_PAGINA_S = 3 * 3600  # between two posts of the same Page


def _janela(dia: datetime) -> tuple[float, float]:
    """A Roleta day as posting time: from FIM_DO_DIA_H that morning until FIM_DO_DIA_H the next morning."""
    return dia.replace(hour=FIM_DO_DIA_H).timestamp(), (dia + timedelta(days=1)).replace(hour=FIM_DO_DIA_H).timestamp()


def _mesmo_dia_roleta(a: float, b: float) -> bool:
    """Both times in the same Roleta day (a post until FIM_DO_DIA_H belongs to the day before)."""
    dia = lambda t: time.strftime("%Y-%m-%d", time.localtime(t - FIM_DO_DIA_H * 3600))
    cal = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))
    return dia(a) == dia(b) or cal(a) == cal(b)


def _da_pagina(pid: str, de: float, ate: float) -> list[dict]:
    return [i for i in fila["itens"] if i.get("quando") and de <= i["quando"] < ate
            and any(d.partition(":")[2] == pid for d in i["destinos"])]


def _vaga_no_dia(pid: str, dia: datetime, agora: float) -> int | None:
    """The Page's earliest time in that Roleta day at least INTERVALO_PAGINA_S away from its other posts: from its
    first posting time (with broken minutes), or 15 min from now on the current day, until 6h the next morning."""
    de, ate = _janela(dia)
    h, m = _horarios(pid)[0]
    # on the current day: 15 min from now plus the Page's own broken minutes, so the Pages do not all start together
    quebra = _horario(pid, dia, 0) - dia.timestamp()
    ini = -(-int(max(_horario(pid, dia, h, m), agora + 15 * 60 + quebra)) // 60) * 60  # a whole minute
    if time.localtime(ini).tm_min == 0:
        ini += 2 * 60  # never on the hour
    perto = [i["quando"] for i in _da_pagina(pid, de - INTERVALO_PAGINA_S, ate + INTERVALO_PAGINA_S)] + [
        r["em"] for i in fila["itens"] for d, r in i.get("resultados", {}).items()
        if d.partition(":")[2] == pid and r.get("estado") == "ok" and de - INTERVALO_PAGINA_S <= r.get("em", 0) < ate + INTERVALO_PAGINA_S
    ]
    for t in sorted({int(ini), *(int(o + INTERVALO_PAGINA_S) for o in perto)}):
        if ini <= t <= ate and all(abs(t - o) >= INTERVALO_PAGINA_S for o in perto):
            return t
    return None


# A Roleta day per phase: (videos of one avatar, videos of the Page). Phases 1-2: 1 video of each of the 3 avatars;
# phase 3: 3 videos of the official avatar, variations of the video that went viral
COTA_FASE = {3: (3, 3)}
COTA_PADRAO = (1, roleta.AVATARES_POR_PAGINA)


def _no_dia(pid: str, dia: datetime) -> list[dict]:
    """The Page's videos in that Roleta day; one that failed did not go out, so it holds no place."""
    return [i for i in _da_pagina(pid, *_janela(dia)) if i["status"] != "erro"]


def _cabe(S: dict, rid: str, pid: str, dia: datetime, n: str | None = None) -> bool:
    """With n, one of the Page's avatars of that day: it has room for its own video (1 in phases 1-2, 3 for the official
    one), whatever else went out on the Page that day (a video left over from the day before, a split screen), so the
    day's batch always gets its places. Without n, a video of an avatar the Roleta left without a Page: it only takes a
    place the Page still has that day."""
    por_avatar, por_pagina = COTA_FASE.get(S["paginas"].get(rid, {}).get("fase"), COTA_PADRAO)
    itens = _no_dia(pid, dia)
    if n is not None:
        return sum(1 for i in itens if _avatar(i["nome"]) == n) < por_avatar
    return len(itens) < por_pagina


def _vaga_sem_pagina(S: dict, casadas: dict, dia: datetime, agora: float) -> tuple[dict, int] | None:
    """An avatar the Roleta leaves without a Page that day: the Page of phases 1-2 with the fewest posts in that
    Roleta day that still has room and a free time (3h from its other posts, until 6h the next morning)."""
    livres = [(rid, p) for rid, pid in casadas.items() if S["paginas"].get(rid, {}).get("fase") != 3
              and not S["paginas"].get(rid, {}).get("pausada") and (p := _pagina(pid))]
    for rid, p in sorted(livres, key=lambda x: len(_no_dia(x[1]["id"], dia))):
        if _cabe(S, rid, p["id"], dia) and (t := _vaga_no_dia(p["id"], dia, agora)):
            return p, t
    return None


def _vaga_roleta(nome: str) -> tuple[dict, int] | None:
    """For an "Avatar N" video: the Page that avatar has on the Roleta, on the first day (from today) that Page still
    has room (see COTA_FASE), 3 hours from the Page's other posts and no later than 6h the next morning. A day the
    Roleta leaves the avatar without a Page, the video goes to a Page of phases 1-2 with room (_vaga_sem_pagina)."""
    S, n = roleta_dados(), _avatar(nome)
    if not S or not n:
        return None
    casadas, agora = roleta.casar_paginas(S, contas().get("paginas", [])), time.time()
    for dia in _dias():
        rid = roleta.avatar_para_pagina(S, dia.date().isoformat()).get(n)
        pagina = _pagina(casadas.get(rid, ""))
        if not pagina:
            if lugar := _vaga_sem_pagina(S, casadas, dia, agora):
                return lugar
            continue
        if _cabe(S, rid, pagina["id"], dia, n) and (t := _vaga_no_dia(pagina["id"], dia, agora)):
            return pagina, t
    return None


def _escolher_rodizio() -> dict | None:
    """The rotation Page with the fewest videos waiting in the queue (then the fewest received so far)."""
    paginas = [p for p in (_pagina(pid) for pid in fila["config"]["rodizio"]) if p]
    if not paginas:
        return None
    vivos = ("preparando", "rascunho", "agendado")
    espera = {p["id"]: sum(1 for i in fila["itens"] if i["status"] in vivos
                           and any(d.partition(":")[2] == p["id"] for d in i["destinos"])) for p in paginas}
    cont = fila["config"]["rodizio_cont"]
    p = min(paginas, key=lambda p: (espera[p["id"]], cont.get(p["id"], 0)))
    cont[p["id"]] = cont.get(p["id"], 0) + 1
    return p


AVATAR_RE = re.compile(r"avatar[\s_-]*0*(\d+)", re.I)
COPIA_RE = re.compile(r"\bcopy\s*(\d+)", re.I)  # the Drive has both "copy 28" and "copy29"
NUMERO_RE = re.compile(r"^(?:final_download_)?0*(\d+)(?: \(\d+\))?$")  # "12.mp4", "final_download_12", "4 (2)"


def _avatar(nome: str, solto=False) -> str | None:
    """Avatar number in a video name ("_Rodízio/Avatar 27 copy 25.mp4" -> "27"); solto also takes a bare
    number, as the first batches were named."""
    m = AVATAR_RE.search(nome)
    if m:
        return m.group(1)
    m = solto and NUMERO_RE.match(Path(nome.split(" · ")[-1].replace(" (rodízio)", "")).stem)
    return m.group(1) if m else None


def aviso_nome(item: dict) -> list[str]:
    """The name is the only record of a video's avatar and copy, and a posted video leaves the queue in days."""
    nome, arquivo = item["nome"], item["nome"].rsplit("/", 1)[-1]
    if item.get("origem") in ("clickbait", "tela_dividida") or arquivo.startswith(("isca_clickbait", "saida_tela_dividida")) or " · " in nome:
        return []  # those have no avatar of their own, or the name says the base they came from
    falta = [t for t, achou in (("avatar", _avatar(nome, solto=True)), ("copy", COPIA_RE.search(nome))) if not achou]
    return [f'Nome sem {" nem ".join(falta)}: o radar não vai saber o que é este vídeo. Use o padrão "Avatar 4 copy 25" no arquivo.'] if falta else []


def _avatares_do_historico() -> dict:
    """Avatar -> Page as it was done by hand so far (the latest post of each avatar wins)."""
    mapa = {}
    for i in sorted(fila["itens"], key=lambda i: i["id"]):
        fb = [d.partition(":")[2] for d in i["destinos"] if d.startswith("fb:")]
        n = len(fb) == 1 and i["resultados"] and _avatar(i["nome"], solto=True)
        if n and _pagina(fb[0]):
            mapa[n] = fb[0]
    return mapa


def _pagina_avatar(nome: str) -> dict | None:
    pid = fila["config"]["avatar_paginas"].get(_avatar(nome) or "")
    return _pagina(pid) if pid else None


def _destinos_drive(pagina: dict) -> list[str]:
    return [f"fb:{pagina['id']}"] + ([f"ig:{pagina['id']}"] if pagina["ig_id"] and fila["config"]["drive_ig"] else [])


def roleta_publica() -> dict | None:
    """Today's Roleta for the page: each of its Pages, the connected Page it matched, and its avatar numbers."""
    S = roleta_dados()
    if not S:
        return None
    casadas, plano = roleta.casar_paginas(S, contas().get("paginas", [])), roleta.plano(S, datetime.now().date().isoformat())
    return {"alterada_em": S.get("alterada_em") or S.get("copiada_em", 0), "paginas": [
        {"nome": p.get("nome", ""), "fase": p.get("fase") or 1, "pagina": casadas.get(p["id"], ""), "pausada": bool(p.get("pausada")),
         "avatares": [roleta.numero_avatar(S, a) for a in plano.get(p["id"], []) if a]} for p in roleta.paginas(S)]}


def _lugar_auto(nome: str) -> tuple[dict, int | None] | None:
    """Where the panel puts an avatar video by itself: the Roleta of the day when there is one, else the fixed map."""
    if roleta_dados():
        return _vaga_roleta(nome)
    p = _pagina_avatar(nome)
    return (p, proximo_horario(p["id"])) if p else None


def _aplicar_avatares(refazer=False) -> int:
    """Drafts without a Page get their avatar's one (Roleta of the day, or the fixed map). With refazer, the drafts
    the panel placed itself whose time is gone (or all of them, refazer="todos") are placed again."""
    n, agora = 0, time.time()
    rascunhos = sorted((i for i in fila["itens"] if i["status"] in ("preparando", "rascunho") and not i["resultados"]),
                       key=lambda i: i["id"])
    for i in rascunhos:  # all freed first, so an older video is not kept out of a slot a newer one still holds
        if i.get("auto") and (refazer == "todos" or refazer and (i.get("quando") or 0) < agora + 15 * 60):
            i["destinos"], i["quando"] = [], None
    for i in rascunhos:
        if i["destinos"]:
            continue
        lugar = _lugar_auto(i["nome"])
        if lugar:
            i["destinos"], i["quando"], i["auto"] = _destinos_drive(lugar[0]), lugar[1], True
            n += 1
    return n


_reposicao = {"ultima": 0.0, "roleta": None}


def _repor_auto() -> None:
    """A new copy of the Roleta (roleta_sync.py) places every draft the panel placed again at once; besides that,
    every 10 minutes a draft whose suggested time went by moves to the avatar's next slot (on the Roleta, the
    avatar may be on another Page that day)."""
    try:
        marca = ROLETA_FILE.stat().st_mtime
    except OSError:
        marca = None
    if _reposicao["roleta"] is None:
        _reposicao["roleta"] = marca  # the copy the panel opened with was already applied by iniciar()
    elif marca != _reposicao["roleta"]:
        _reposicao["roleta"] = marca
        with lock:
            antes = {i["id"]: (i["destinos"], i["quando"]) for i in fila["itens"]}
            _aplicar_avatares(refazer="todos")
            mudaram = sum(1 for i in fila["itens"] if antes.get(i["id"]) != (i["destinos"], i["quando"]))
            _salvar()
        if mudaram:  # views typed in the Roleta change nothing here: only a new Page or time is worth a line
            _log(f"Roleta de Paginas mudou: {mudaram} rascunho(s) foram para a Pagina do avatar no dia.")
        return
    if time.time() - _reposicao["ultima"] < 600:
        return
    _reposicao["ultima"] = time.time()
    with lock:
        if _aplicar_avatares(refazer=True):
            _salvar()


def _tam_fonte(item: dict) -> int | None:
    if "fonte_tam" in item:
        return item["fonte_tam"]
    try:  # older items: the copy here, unless it was converted
        return (VIDEOS / item["arquivo"]).stat().st_size
    except OSError:
        return None


def _ja_na_fila(nome_arquivo: str, tam: int) -> dict | None:
    """The same Drive video already in the queue: moved to another folder, or touched again by Drive while syncing."""
    return next((i for i in fila["itens"] if i["origem"] == "drive" and i.get("fonte")
                 and Path(i["fonte"]).name == nome_arquivo and _tam_fonte(i) == tam), None)


def _tirar_duplicados() -> None:
    """Drafts that are the same Drive video twice (two panels open at once, or a video moved between folders)
    keep one: the one whose Drive file is still where it was."""
    grupos: dict[tuple, list] = {}
    for i in fila["itens"]:
        if i["origem"] == "drive" and i.get("fonte") and i["status"] in ("preparando", "rascunho") and not i["resultados"]:
            grupos.setdefault((Path(i["fonte"]).name, _tam_fonte(i)), []).append(i)
    sobra = [i["id"] for g in grupos.values() if len(g) > 1
             for i in sorted(g, key=lambda i: (Path(i["fonte"]).is_file(), i["status"] == "rascunho", i["id"]))[:-1]]
    if sobra:
        remover({"ids": sobra})
        _log(f"{len(sobra)} video(s) repetido(s) do Drive tirados da fila (ficam na _lixeira por "
             f"{fila['config']['limpar_dias']} dias).")


def _legenda_drive(video: Path) -> str:
    for txt in (video.with_suffix(".txt"), video.parent / "_legenda.txt"):
        if txt.is_file():
            raw = txt.read_bytes()
            for enc in ("utf-8-sig", "cp1252"):  # Notepad saves either
                try:
                    return raw.decode(enc).strip()[:2200]
                except UnicodeDecodeError:
                    pass
    return ""


_drive_marcas: dict[str, list] = {}


def _scan_drive() -> None:
    """Copy new videos from the Drive folder; one comes in once its size and date stop changing (synced)."""
    base = _drive()
    if not base.is_dir():
        return
    pastas, vivos = _pastas_paginas(), {}
    for f in [*base.glob("*"), *base.glob("*/*")]:
        if not (f.is_file() and f.suffix.lower() in VIDEO_EXTS):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        rel, marca = f.relative_to(base).as_posix(), [st.st_size, int(st.st_mtime)]
        if fila["drive_vistos"].get(rel) == marca:
            continue
        if not st.st_size or _drive_marcas.get(rel) != marca:
            vivos[rel] = marca
            continue
        rodizio = f.parent.name == RODIZIO
        na_pasta = f.parent != base and not rodizio  # a Page's folder decides, whatever the name says
        with lock:
            repetido = _ja_na_fila(f.name, st.st_size)
            lugar = None if na_pasta else _lugar_auto(f.name)
            pagina = pastas.get(f.parent.name) if na_pasta else lugar and lugar[0]
            por_avatar = bool(pagina) and not na_pasta
            if repetido:  # moved to another folder: no second copy, but a Page it now has fills an empty draft
                fila["drive_vistos"][rel] = marca
                if pagina and repetido["status"] in ("preparando", "rascunho") and not repetido["destinos"]:
                    repetido["destinos"] = _destinos_drive(pagina)
                    repetido["quando"] = lugar[1] if lugar else proximo_horario(pagina["id"])
                    repetido["auto"] = not na_pasta
                _salvar()
                continue
            if not pagina and rodizio:
                pagina = _escolher_rodizio()
            destinos, quando = [], None
            if pagina:
                destinos = _destinos_drive(pagina)  # the time is a suggestion: the video still comes in as a draft
                quando = lugar[1] if por_avatar else proximo_horario(pagina["id"])
            nome = rel  # the Page shows on the card and can change with the Roleta, so it stays out of the name
        try:  # outside the lock: a copy from Drive can take a while
            novo = adicionar(f, "drive", nome, destinos=destinos, legenda=_legenda_drive(f), quando=quando)
        except OSError as e:  # still downloading from Drive, or locked
            _log(f"Nao consegui copiar do Drive {rel}: {e}")
            continue
        with lock:
            novo["fonte_tam"] = st.st_size
            if pagina and not na_pasta:
                novo["auto"] = True  # placed by the panel: placed again if its time goes by while still a draft
            fila["drive_vistos"][rel] = marca
            _salvar()
        aviso = ("" if pagina else " · pasta sem Pagina conectada, marque o destino" if na_pasta
                 else " · sem Pagina (avatar fora do mapa e rodizio vazio), escolha o destino")
        _log(f"Entrou na fila (Google Drive): {nome}" + (f" · sugerido {_quando_txt(quando)}" if quando else "") + aviso)
    _drive_marcas.clear()
    _drive_marcas.update(vivos)


def lista_kakashi() -> list[dict]:
    na_fila = {i["fonte"] for i in fila["itens"] if i.get("fonte")}
    out = []
    for origem, p in _fontes_kakashi():
        try:
            mt = int(p.stat().st_mtime)
        except OSError:
            continue
        out.append({"fonte": p.relative_to(OUTPUT).as_posix(), "origem": origem, "nome": _nome_fonte(origem, p),
                    "mtime": mt, "na_fila": str(p) in na_fila})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)[:60]


def _fonte(rel: str) -> tuple[str, Path] | None:
    f = _dentro(OUTPUT, rel)
    return next(((o, p) for o, p in _fontes_kakashi() if f and p.resolve() == f), None)


# ─── Posting ────────────────────────────────────────────────


def _restantes(item: dict) -> list[str]:
    # a destination with a result is done; an "erro" one waits for "Tentar de novo"
    return [d for d in item["destinos"] if d not in item["resultados"]]


def _devidos(item: dict, agora: float) -> list[tuple[str, bool]]:
    """(destination, scheduled at Meta?) that should be sent now."""
    out, q = [], item.get("quando")
    sem_sched = set(fila["config"].get("sem_agendamento_meta") or [])
    for d in _restantes(item):
        pid = d.partition(":")[2]
        if not q or q <= agora:
            out.append((d, False))
        elif d.startswith("fb:") and pid not in sem_sched and q > agora + 120:
            out.append((d, True))
    return out


def _status_final(item: dict, agora: float) -> str:
    """Once every destination has its result: an error wins; a post Meta is still holding for later stays
    "agendado" (and can still be called back to draft) until its time comes, then it counts as posted."""
    res = item["resultados"].values()
    if any(r["estado"] == "erro" for r in res):
        return "erro"
    if (item.get("quando") or 0) > agora and any(r["estado"] == "agendado_meta" for r in res):
        return "agendado"
    return "postado"


def _acompanhar_meta() -> None:
    """Move posts scheduled at Meta to "Postados" when their time comes (and back, for ones marked too early)."""
    agora, mudou = time.time(), False
    with lock:
        for item in fila["itens"]:
            if (item["status"] in ("agendado", "postado") and item["resultados"] and not _restantes(item)
                    and not item.get("andamento")):
                novo = _status_final(item, agora)
                if novo != "erro" and novo != item["status"]:
                    item["status"] = novo
                    mudou = True
        if mudou:
            _salvar()


def _nome_destino(dest: str) -> str:
    rede, _, pid = dest.partition(":")
    p = _pagina(pid) or {"nome": pid, "ig_nome": ""}
    return f"Instagram @{p.get('ig_nome') or p['nome']}" if rede == "ig" else f"Facebook {p['nome']}"


def _bloquear(e: GraphError) -> None:
    """Meta blocked the app: stop sending, and give back what has not gone out yet as drafts (keeping its time), so
    nothing bursts out late on its own when access returns."""
    with lock:
        if fila.get("bloqueio"):
            return
        fila["bloqueio"] = {"msg": str(e), "em": int(time.time())}
        voltaram = 0
        for item in fila["itens"]:
            if item["status"] == "agendado" and _restantes(item) and not item.get("andamento"):
                item["status"] = "rascunho" if not item["resultados"] else "erro"
                voltaram += 1
        _salvar()
    _log(f"A Meta bloqueou o acesso do app ({e}). Nada sera postado ate liberar"
         + (f"; {voltaram} video(s) agendado(s) voltaram para Rascunhos." if voltaram else "."))


_bloq = {"checado": 0.0}


def _ainda_bloqueado() -> bool:
    """Every 30 minutes, ask Meta again with one Page token; clear the block once it answers."""
    if not fila.get("bloqueio"):
        return False
    agora = time.time()
    if agora - _bloq["checado"] < 1800:
        return True
    _bloq["checado"] = agora
    paginas = contas().get("paginas", [])
    if not paginas:
        return True
    try:
        graph("GET", "me", paginas[0]["token"], fields="id")
    except GraphError:
        return True
    with lock:
        fila.pop("bloqueio", None)
        _salvar()
    _log("A Meta liberou o acesso do app de novo. Agende os rascunhos quando quiser.")
    return False


_tokens = {"checado": 0.0}


def _checar_tokens() -> None:
    """Every TOKENS_CHECAR_S each Page's token is tried. Meta ends them all at once when the password changes or for
    security, and until a post or a reading fails nothing says so. No answer from Meta says nothing about the tokens."""
    agora = time.time()
    if agora - _tokens["checado"] < TOKENS_CHECAR_S:
        return
    _tokens["checado"] = agora
    paginas, caidas, ids = contas().get("paginas", []), [], []
    for p in paginas:
        try:
            graph("GET", "me", p["token"], fields="id")
        except GraphError as e:
            if e.code in ("rede", None):
                return
            if e.code == 190:
                caidas.append(p["nome"].strip())
                ids.append(p["id"])
        time.sleep(0.2)
    with lock:
        antes = fila.get("tokens_caidos")
        if caidas:
            fila["tokens_caidos"] = {"em": antes["em"] if antes else int(agora), "paginas": caidas, "ids": ids, "total": len(paginas)}
        else:
            fila.pop("tokens_caidos", None)
        if caidas != (antes or {}).get("paginas", []):
            _salvar()
    if caidas and not antes:
        _log(f"A Meta derrubou a conexao de {len(caidas)} de {len(paginas)} Pagina(s): reconecte em 1. Contas.")
        _alerta_discord("tokens_caidos", f"⚠️ **Conexão da Meta caiu em {len(caidas)} de {len(paginas)} Página(s):** {', '.join(caidas[:10])}. Reconecte em `1. Contas`.")
    elif antes and not caidas:
        _log("A conexao com a Meta voltou em todas as Paginas.")


CONFERIR_DEPOIS_S = 180       # Meta needs a moment after the time to publish a scheduled Reel
CONFERIR_DESISTE_S = 6 * 3600  # not out after this long: count it as failed
CONFERIR_SEGURADO_S = 2 * 3600  # still "scheduled" at Meta this long after its time: Meta is holding the Page's posts


def _conferir_meta() -> None:
    """A post only counts once Meta shows it: after its time, each Facebook video is looked up again. One Meta
    refused or removed becomes an error (with "Tentar de novo"); while the app is blocked, nothing is checked."""
    if _ainda_bloqueado():
        return
    agora, alvos = time.time(), []
    with lock:
        for item in fila["itens"]:
            for dest, r in item["resultados"].items():
                if not dest.startswith("fb:") or r["estado"] not in ("ok", "agendado_meta") or r.get("conf"):
                    continue
                desde = (item.get("quando") or r["em"]) if r["estado"] == "agendado_meta" else r["em"]
                if desde + CONFERIR_DEPOIS_S <= agora and r.get("conf_prox", 0) <= agora:
                    alvos.append((item["id"], dest, r["id"], desde))
    for iid, dest, vid, desde in alvos[:10]:
        pagina = _pagina(dest.partition(":")[2])
        if not pagina:
            continue
        novo = {}
        try:
            v = graph("GET", vid, pagina["token"], fields="status,published,permalink_url")
            st = v.get("status") or {}
            falhas = [f.get("message") for fase in ("uploading_phase", "processing_phase", "publishing_phase")
                      for f in ((st.get(fase) or {}).get("errors") or [])]
            fase = (st.get("publishing_phase") or {}).get("publish_status")
            # Meta answers published=True for a Reel it still holds as scheduled: only the publishing phase tells
            if v.get("published") and fase in (None, "published"):
                link = v.get("permalink_url") or ""
                novo = {"conf": "ok", **({"link": link if link.startswith("http") else "https://www.facebook.com" + link}
                                          if link else {})}
            elif st.get("video_status") == "error" or falhas:
                novo = {"estado": "erro", "erro": "a Meta recusou o video depois do envio"
                        + (f": {'; '.join(m for m in falhas if m)}" if any(falhas) else "")}
            elif fase == "scheduled" and agora - desde > CONFERIR_SEGURADO_S:
                novo = {"estado": "erro", "segurado": True,
                        "erro": "a Meta segurou o video: ficou agendado e nao publicou no horario"}
            elif agora - desde > CONFERIR_DESISTE_S:
                novo = {"estado": "erro", "erro": f"a Meta nao publicou o video (ficou como "
                                                  f"{st.get('video_status') or 'nao publicado'})"}
            else:
                novo = {"conf_prox": agora + 600}
        except GraphError as e:
            if e.bloqueio:
                _bloquear(e)
                return
            if e.code == 100:
                novo = {"estado": "erro", "erro": "o video nao existe mais na Pagina (apagado pela Meta ou por alguem)"}
            else:
                novo = {"conf_prox": agora + 600}
        with lock:
            item = _item(iid)
            r = item and item["resultados"].get(dest)
            if not r:
                continue
            r.update(novo)
            if novo.get("estado") == "erro":
                item["status"] = "erro"
            _salvar()
        if novo.get("segurado"):
            _destravar_agendados_meta(dest.partition(":")[2])
        elif novo.get("estado") == "erro":
            _log(f"ERRO · {_nome_destino(dest)} · {item['nome']} · {novo['erro']}")


_envio = {"ultimo": 0.0}


def _na_fila_de_envio() -> int:
    agora = time.time()
    return sum(len(_devidos(i, agora)) for i in fila["itens"] if i["status"] == "agendado")


def _ritmo_txt() -> str:
    n = _na_fila_de_envio()
    if n <= 1:
        return ""
    num = lambda x: f"{x:.1f}".rstrip("0").rstrip(".").replace(".", ",")  # 2,5 / 3
    return (f" Os envios para a Meta saem um a cada {num(ENVIO_INTERVALO_S / 60)} min, o mais cedo primeiro "
            f"({n} na fila, cerca de {num(total / 3600) + ' h' if (total := n * ENVIO_INTERVALO_S) >= 3600 else f'{total // 60:.0f} min'}): "
            "deixe o Kakashi aberto ate terminar.")


def _postar_devidos() -> None:
    """One send to Meta per pass, at most one every ENVIO_INTERVALO_S, the earliest time first. A video scheduled
    far ahead is handed to Meta in its turn (Meta then holds it); one whose time is less than NATIVO_MIN_S away
    waits for its time and goes out directly."""
    if fila.get("bloqueio") or time.time() - _envio["ultimo"] < ENVIO_INTERVALO_S:
        return
    agora = time.time()
    with lock:
        caidas = (fila.get("tokens_caidos") or {}).get("ids", [])  # those wait for the reconnection, with no error
        alvos = sorted((i.get("quando") or 0, i["id"], d, meta) for i in fila["itens"] if i["status"] == "agendado"
                       for d, meta in _devidos(i, agora) if d.partition(":")[2] not in caidas)
    for _, iid, dest, meta in alvos[:1]:
        with lock:
            item = _item(iid)
            if not item or item["status"] != "agendado" or dest in item["resultados"]:
                continue
            if novo := _respiro(item, dest, agora):
                item["quando"] = novo
                _salvar()
                _log(f"Adiado para {_quando_txt(novo)} · {_nome_destino(dest)} · {item['nome']} · 3h depois do ultimo video da Pagina")
                continue
            if meta and item.get("quando") and item["quando"] - time.time() < NATIVO_MIN_S:
                item["quando"] = int(time.time() + NATIVO_MIN_S + 60)
                _salvar()
            item["andamento"] = f"Enviando para {_nome_destino(dest)}..."
            path, legenda, info = VIDEOS / item["arquivo"], item["legenda"], item["info"]
            quando = item["quando"] if meta else None
            pagina = _pagina(dest.partition(":")[2])
        bloqueio, restrita = None, False
        try:
            if not pagina:
                raise GraphError("essa Pagina nao esta mais conectada")
            res = _enviar(dest, pagina, path, legenda, info, quando)
            res["estado"] = "agendado_meta" if quando else "ok"
            if dest.startswith("ig:"):
                res["conf"] = "ok"  # Instagram only answers once the Reel is out, with its link
        except (GraphError, OSError, KeyError) as e:
            if isinstance(e, GraphError) and e.code == "rede":  # internet down for a moment: same video next turn
                _envio["ultimo"] = time.time()
                with lock:
                    if item := _item(iid):
                        item["andamento"] = ""
                _log(f"Sem internet agora; tento de novo em {f'{ENVIO_INTERVALO_S / 60:g}'.replace('.', ',')} min · {_nome_destino(dest)}")
                return
            if isinstance(e, GraphError) and e.code == 190:  # the token fell before the check saw it: test them now
                _envio["ultimo"] = time.time()
                _tokens["checado"] = 0
                with lock:
                    if item := _item(iid):
                        item["andamento"] = ""
                _log(f"Token recusado; o video espera a reconexao · {_nome_destino(dest)}")
                return
            res = {"estado": "erro", "erro": str(e)}
            if isinstance(e, GraphError) and e.bloqueio:
                bloqueio = e
            restrita = isinstance(e, GraphError) and e.code == RESTRICAO_COD
        _envio["ultimo"] = time.time()
        if res.get("estado") == "erro" and not bloqueio and not restrita:
            with lock:
                item = _item(iid)
                tent = int((item or {}).get("tentativas") or 0) + 1
                if item and tent < 3:
                    item["tentativas"] = tent
                    item["andamento"] = ""
                    item["quando"] = int(max(item.get("quando") or 0, time.time()) + 600)
                    _salvar()
                    _log(f"Falha temporaria ({res['erro']}); tentativa automatica {tent}/3 em 10 min · {_nome_destino(dest)} · {item['nome']}")
                    continue
        res["em"] = int(time.time())
        with lock:
            item = _item(iid)
            if item:
                if res.get("estado") != "erro":
                    item.pop("tentativas", None)
                item["resultados"][dest] = res
                item["andamento"] = ""
                if not _restantes(item):
                    item["status"] = _status_final(item, time.time())
                _salvar()
        verbo = {"ok": "Postado", "agendado_meta": f"Agendado na Meta para {_quando_txt(quando or 0)}"}.get(
            res["estado"], "ERRO")
        _log(f"{verbo} · {_nome_destino(dest)} · {item['nome'] if item else iid}" +
             (f" · {res['erro']}" if res.get("erro") else ""))
        if res.get("estado") == "erro" and not bloqueio:
            _alerta_discord(f"erro:{iid}:{dest}", f"❌ **Falha ao postar vídeo (após 3 tentativas)**\nPágina: **{_nome_destino(dest)}**\nVídeo: `{item['nome'] if item else iid}`\nMotivo: {res.get('erro', '')}")
        if bloqueio:
            _bloquear(bloqueio)
        if restrita:
            _pagina_restrita(dest.partition(":")[2], res["erro"])


def _respiro(item: dict, dest: str, agora: float) -> int | None:
    """A video whose time came late (the PC slept, a retry, a time typed by hand) must still be INTERVALO_PAGINA_S away
    from the Page's other videos already with Meta: posting several at once is what got a Page limited. Returns the
    new time, or None when this one can go. Caller holds `lock`."""
    q = max(item.get("quando") or agora, agora)  # a late one goes out now, not at its old time

    def saiu(o: dict) -> float | None:  # when the other video goes (or went) out on the Page
        r = o["resultados"].get(dest) or {}
        return r.get("em") if r.get("estado") == "ok" else o.get("quando") if r.get("estado") == "agendado_meta" else None

    perto = [t for o in fila["itens"] if o is not item and (t := saiu(o)) and abs(t - q) < INTERVALO_PAGINA_S]
    if not perto:
        return None
    return int(max(max(perto) + INTERVALO_PAGINA_S, agora + 15 * 60)) // 60 * 60


RESTRICAO_COD = 368  # Meta: "for security reasons your account has limited access for a few days"


def _destravar_agendados_meta(pid: str) -> None:
    """Meta's native Reel scheduler held a SCHEDULED video past its time without publishing it. Instead of pausing the
    Page and throwing its videos back to draft, switch this Page to direct publishing at the scheduled time
    (video_state='PUBLISHED', meta=False), delete the stuck scheduled uploads at Meta, and keep the videos scheduled
    in the queue (3 hours apart). If the Page is truly restricted (e.g. code 368), direct publishing will report it."""
    with lock:
        sem_sched = fila["config"].setdefault("sem_agendamento_meta", [])
        if pid not in sem_sched:
            sem_sched.append(pid)
        na_meta = [(i["id"], d, r["id"]) for i in fila["itens"] for d, r in i["resultados"].items()
                   if d.partition(":")[2] == pid and r.get("id") and not i["andamento"]
                   and (r.get("segurado") or (r.get("estado") == "agendado_meta" and r.get("conf") != "ok"))]
    tirados = {(iid, d) for iid, d, vid in na_meta if _tirar_da_meta(pid, vid)}
    reagendados = []
    with lock:
        for i in fila["itens"]:
            if i["andamento"]:
                continue
            for d in [d for d, r in i["resultados"].items() if d.partition(":")[2] == pid
                      and ((i["id"], d) in tirados or r.get("segurado"))]:
                i["resultados"].pop(d, None)
                i["status"] = "agendado"
                if i not in reagendados:
                    reagendados.append(i)
        _horarios_na_pagina(pid, reagendados)
        _salvar()
    nome = (_pagina(pid) or {}).get("nome", pid)
    _log(f"{nome}: agendador da Meta segurou o Reel; {len(reagendados)} video(s) passaram para envio direto (PUBLISHED) pelo Kakashi.")


def _pagina_restrita(pid: str, erro: str, motivo: str = "a Meta limitou a Página (código 368)") -> None:
    """Meta refused a post because the Page is limited (code 368), or is holding its scheduled Reels. Trying its next
    videos would only collect the same answer, so the Page is paused on the Roleta at once (Reativar brings it back,
    phase kept). What Meta holds for it and has not published is deleted there, or Meta could publish them all at once
    when it lets the Page go; those, and the ones it refused, come back as drafts on that same Page, kept off the
    Roleta's moves, with suggested times 3 hours apart (a draft with no time would go out at once on "Agendar"), for the
    user to schedule once the Page is fine. Its videos not handed to Meta yet go to the
    Pages their avatars have that day, still scheduled."""
    with roleta_lock:
        S = roleta_dados() or {}
    rid = next((r for r, p in roleta.casar_paginas(S, contas().get("paginas", [])).items() if p == pid), None) if S else None
    pausou = bool(rid) and not S["paginas"][rid].get("pausada")
    if pausou:
        p = S["paginas"][rid]
        hist = (p.get("historico") or []) + [{"data": datetime.now().date().isoformat(),
                                              "texto": f"Pausada sozinha: {motivo}"}]
        roleta_gravar({"op": "update", "caminho": f"paginas/{rid}", "dados": {"pausada": True, "historico": hist[-20:]}})
    with lock:
        na_meta = [(i["id"], d, r["id"]) for i in fila["itens"] for d, r in i["resultados"].items()
                   if d.partition(":")[2] == pid and r.get("id") and not i["andamento"]
                   and (r.get("segurado") or (r.get("estado") == "agendado_meta" and r.get("conf") != "ok"))]
    tirados = {(iid, d) for iid, d, vid in na_meta if _tirar_da_meta(pid, vid)}
    mudaram, parados, voltaram = 0, 0, []
    with lock:
        for i in fila["itens"]:
            if i["andamento"]:
                continue
            for d in [d for d, r in i["resultados"].items() if d.partition(":")[2] == pid
                      and ((i["id"], d) in tirados or (r.get("estado") == "erro" and not r.get("id")))]:
                i["resultados"].pop(d)  # nothing of it went out on this Page
                if not i["resultados"]:
                    i.update(status="rascunho", quando=None, auto=False)  # same Page, off the Roleta's moves
                    voltaram.append(i)
        _horarios_na_pagina(pid, voltaram)
        presos = [i for i in fila["itens"] if i["status"] == "agendado" and i.get("auto") and not i["resultados"]
                  and not i["andamento"] and any(d.partition(":")[2] == pid for d in i["destinos"])]
        for i in presos:
            i["destinos"], i["quando"] = [], None
        for i in presos:
            lugar = _lugar_auto(i["nome"])
            if lugar and lugar[1]:
                i["destinos"], i["quando"] = _destinos_drive(lugar[0]), lugar[1]
                mudaram += 1
            else:
                i["status"] = "rascunho"  # nowhere to go yet: it waits as a draft
                parados += 1
        _salvar()
    nome = (_pagina(pid) or {}).get("nome", pid)
    texto = (f"{nome}: {motivo}." + (" Pausada na Roleta." if pausou else "")
             + (f" {len(tirados)} agendamento(s) cancelado(s) na Meta." if tirados else "")
             + (f" {len(voltaram)} video(s) voltaram para rascunho nela." if voltaram else "")
             + (f" {mudaram} video(s) foram para outras Paginas." if mudaram else "")
             + (f" {parados} voltaram para rascunho." if parados else ""))
    _log(texto)
    radar = sys.modules.get("radar")  # loaded by the panel; read here only, so no import cycle
    if pausou and radar and hasattr(radar, "_enviar_aviso"):
        radar._enviar_aviso(f"⛔ **{nome}: {motivo}**. Ela foi pausada na Roleta"
                            + (f", {len(tirados)} agendamento(s) foram cancelados na Meta" if tirados else "")
                            + (f" e {len(voltaram)} vídeo(s) voltaram para rascunho nela" if voltaram else "")
                            + ". Confira a Página no Meta Business Suite; quando estiver normal, use Reativar na aba Páginas da Roleta.")


def _horarios_na_pagina(pid: str, itens: list[dict]) -> None:
    """Suggested times on the Page for these drafts, in order, each 3 hours from the Page's other videos. Caller holds
    `lock`."""
    agora = time.time()
    for i in itens:
        i["quando"] = next((t for dia in _dias() if (t := _vaga_no_dia(pid, dia, agora))), None)


def _tirar_da_meta(pid: str, vid: str) -> bool:
    """Deletes at Meta a video it holds and has not published. False when it is out, or Meta did not answer (the next
    detection tries again)."""
    p = _pagina(pid)
    if not p:
        return False
    try:
        v = graph("GET", vid, p["token"], fields="status")
        if ((v.get("status") or {}).get("publishing_phase") or {}).get("publish_status") == "published":
            return False
        graph("DELETE", vid, p["token"])
    except GraphError as e:
        return e.code == 100  # already gone at Meta
    return True


_auto = {"paginas": 0.0}


def _auto_paginas() -> None:
    """On opening and every hour: new Pages show up by themselves (and join the Roleta) while any saved user
    token lasts."""
    agora = time.time()
    c = contas()
    perfis = _perfis_lista(c)
    if agora - _auto["paginas"] < 3600 or not any(pf.get("token_vence", 0) > agora for pf in perfis):
        return
    _auto["paginas"] = agora
    try:
        msg = atualizar_paginas()
    except Recusa as e:
        _log(f"Atualizacao automatica das Paginas: {e}")
        return
    if not msg.startswith("Nada novo"):
        _log(f"Paginas atualizadas sozinhas. {msg}")


_limpeza = {"ultima": 0.0}


def _esvaziar_lixeira(agora: float, dias: int) -> None:
    """Files taken off the queue are deleted for good limpar_dias after they went to the trash."""
    if not LIXEIRA.is_dir():
        return
    apagados, total = 0, 0
    with lock:
        quando = fila.setdefault("lixeira_em", {})
        presentes = {f.name: f for f in LIXEIRA.iterdir() if f.is_file()}
        for nome in list(quando):
            if nome not in presentes:
                del quando[nome]
        for nome, f in presentes.items():
            # a file already there without a date (from before this rule) gets the full wait from now
            if agora - quando.setdefault(nome, int(agora)) < dias * 86400:
                continue
            try:
                total += f.stat().st_size
                f.unlink()
                del quando[nome]
                apagados += 1
            except OSError:
                pass
        _salvar()
    if apagados:
        _log(f"Limpeza: {apagados} arquivo(s) da _lixeira com mais de {dias} dias apagados do PC ({total / 1e6:.0f} MB).")


def _limpar_postados() -> None:
    """Once an hour: posted videos older than limpar_dias leave the list and their copy here is deleted (the post
    stays on Facebook/Instagram), and the trash is emptied by the same rule. They do not come back: Drive and
    Kakashi remember what they already handed over."""
    agora = time.time()
    if agora - _limpeza["ultima"] < 3600:
        return
    _limpeza["ultima"] = agora
    dias = fila["config"]["limpar_dias"]
    _esvaziar_lixeira(agora, dias)

    def saiu(i: dict) -> float:  # when it went out: its time, or the last answer from Meta
        return max([i.get("quando") or 0] + [r.get("em", 0) for r in i["resultados"].values()])
    with lock:
        # only what Meta confirmed: a post that never showed up keeps its video here to send again
        velhos = [i for i in fila["itens"] if i["status"] == "postado" and saiu(i) < agora - dias * 86400
                  and all(r.get("conf") == "ok" for r in i["resultados"].values())]
        for i in velhos:
            fila["itens"].remove(i)
        if velhos:
            _salvar()
    if not velhos:
        return
    total = 0
    for i in velhos:
        f = VIDEOS / i["arquivo"]
        try:
            total += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    _log(f"Limpeza: {len(velhos)} video(s) postado(s) ha mais de {dias} dias sairam da lista e foram apagados "
         f"do PC ({total / 1e6:.0f} MB).")


def _sincronizar_nuvem(forcar: bool = False) -> None:
    try:
        import nuvem_sync
        nuvem_sync.exportar_para_nuvem(forcar=forcar)
    except Exception:
        pass


def _alerta_discord(chave: str, texto: str) -> None:
    """Send an operational alert to the Discord webhook configured in radar.json (at most once every 6h per key)."""
    try:
        agora = int(time.time())
        with lock:
            alertas = fila.setdefault("alertas_discord", {})
            if agora - int(alertas.get(chave) or 0) < 6 * 3600:
                return
            alertas[chave] = agora
            _salvar()
        r_cfg = _load(DIR / "radar.json", {}).get("config", {})
        url = r_cfg.get("webhook") or r_cfg.get("webhook_relatorio") or ""
        if not url:
            return
        corpo = json.dumps({"content": texto[:1900], "allowed_mentions": {"parse": []}}).encode()
        req = urllib.request.Request(url, corpo, {"Content-Type": "application/json", "User-Agent": "Kakashi-Postagem/1.0"})
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception:
        pass


def _checar_atrasos() -> None:
    """Warn on Discord if scheduled videos are overdue by >45 min and no upload happened in the last 15 min."""
    if fila.get("bloqueio"):
        return
    agora = time.time()
    if _envio["ultimo"] and agora - _envio["ultimo"] < 15 * 60:
        return
    with lock:
        caidas = set((fila.get("tokens_caidos") or {}).get("ids", []))
        atrasados = [
            i for i in fila.get("itens", [])
            if i.get("status") == "agendado" and i.get("quando") and agora - i["quando"] > 45 * 60
            and any(d.partition(":")[2] not in caidas for d, _ in _devidos(i, agora))
        ]
    if atrasados and (_envio["ultimo"] == 0 or agora - _envio["ultimo"] >= 15 * 60):
        mais_antigo = min(i["quando"] for i in atrasados)
        _alerta_discord(
            f"atraso:{int(mais_antigo // 3600)}",
            f"⚠️ **Alerta de Fila Atrasada no Kakashi:** {len(atrasados)} vídeo(s) estão agendados desde {_quando_txt(mais_antigo)} aguardando envio."
        )


def _manter_acordado() -> None:
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        with lock:
            tem_pendente = any(i['status'] == 'agendado' and _restantes(i) for i in fila['itens'])
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if tem_pendente else 0x80000000)
    except Exception:
        pass


def _worker() -> None:
    while True:
        for passo in (_manter_acordado, _sincronizar_nuvem, _checar_tokens, _auto_paginas, _scan_entrada, _scan_drive, _scan_kakashi,
                      _repor_auto, _postar_devidos, _acompanhar_meta, _conferir_meta, _checar_atrasos, _limpar_postados):
            try:
                passo()
            except Exception as e:  # keep the loop alive whatever one pass hits
                _log(f"Erro interno ({passo.__name__}): {e}")
        wake.wait(10)
        wake.clear()


def iniciar() -> None:
    _sincronizar_nuvem(forcar=True)
    with lock:
        if "vistos" not in fila:
            _marcar_vistos()
        for item in fila["itens"]:
            item["andamento"] = ""  # whatever was running when the panel closed starts over
        if fila["config"]["avatar_paginas"] is None:
            fila["config"]["avatar_paginas"] = _avatares_do_historico()
            if fila["config"]["avatar_paginas"]:
                _log(f"Mapa Avatar -> Pagina montado pelo que ja foi postado: {len(fila['config']['avatar_paginas'])} "
                     "avatar(es). Confira em Rodizio e horarios.")
        _salvar()
    _tirar_duplicados()
    with lock:
        if roleta_dados() and not fila["config"].get("roleta_migrada"):
            # drafts the fixed map placed before the Roleta was here count as the panel's own: placed again by it
            base = _drive()
            for i in fila["itens"]:
                if (i["origem"] == "drive" and i.get("fonte") and i["status"] in ("preparando", "rascunho")
                        and not i["resultados"] and Path(i["fonte"]).parent in (base, base / RODIZIO)):
                    i["auto"] = True
            fila["config"]["roleta_migrada"] = True
        if roleta_dados():  # on opening, the panel's drafts are placed by today's Roleta and the current rules
            antes = {i["id"]: (i["destinos"], i["quando"]) for i in fila["itens"]}
            _aplicar_avatares(refazer="todos")
            if n := sum(1 for i in fila["itens"] if antes.get(i["id"]) != (i["destinos"], i["quando"])):
                _log(f"Roleta de Paginas: {n} rascunho(s) foram para a Pagina do avatar no dia da Roleta.")
        if n := _aplicar_avatares():
            _log(f"{n} rascunho(s) sem Pagina receberam a Pagina do seu avatar.")
        _salvar()
    for item in list(fila["itens"]):
        if item["status"] == "preparando":
            threading.Thread(target=_preparar, args=(item["id"],), daemon=True).start()
    try:
        criar_pastas_drive()
    except OSError:
        pass
    threading.Thread(target=_worker, daemon=True).start()


# ─── Actions from the page ──────────────────────────────────


def _editavel(body: dict) -> dict:
    item = _item(body.get("id"))
    if not item:
        raise Recusa("Video nao encontrado na fila.")
    if item["status"] not in ("rascunho", "erro", "preparando"):
        raise Recusa("Volte o video para rascunho antes de mudar.")
    return item


def _aplicar(item: dict, body: dict) -> None:
    legenda = str(body.get("legenda") or "").strip()
    if len(legenda) > 2200:
        raise Recusa("Legenda grande demais: o Instagram aceita ate 2200 caracteres.")
    if legenda.count("#") > 30:
        raise Recusa("O Instagram aceita no maximo 30 hashtags.")
    quando = body.get("quando")
    quando = int(quando) if quando else None
    if quando and quando < time.time() - 60:
        raise Recusa("Esse horario ja passou.")
    if quando and quando > time.time() + MAX_FUTURO_S:
        raise Recusa("Agende para no maximo 29 dias a frente.")
    destinos = _validar_destinos(body.get("destinos"))
    if destinos != item["destinos"] or quando != item["quando"]:
        item.pop("auto", None)  # the user chose: the panel no longer moves it
    item.update(legenda=legenda, destinos=destinos, quando=quando)


def salvar(body: dict) -> str:
    with lock:
        _aplicar(_editavel(body), body)
        _salvar()
    return "Salvo."


def _agendar(item: dict) -> None:
    if item["status"] == "preparando":
        raise Recusa("Espere o video terminar de ser conferido.")
    if item.get("erro") and not item["info"].get("w"):
        raise Recusa(item["erro"])
    if not item["destinos"]:
        raise Recusa("Marque pelo menos um destino.")
    item["resultados"] = {d: r for d, r in item["resultados"].items() if r["estado"] != "erro"}  # retry the failed
    if not _restantes(item):
        raise Recusa("Esse video ja foi para todos os destinos marcados.")
    item["status"] = "agendado"
    fila["config"]["destinos_padrao"] = list(item["destinos"])


def _sem_bloqueio() -> None:
    if fila.get("bloqueio"):
        raise Recusa("A Meta bloqueou o acesso do app: nada pode ser agendado ate liberar (veja o aviso no topo "
                     "da Postagem).")


def agendar(body: dict) -> str:
    _sem_bloqueio()
    with lock:
        item = _editavel(body)
        _aplicar(item, body)
        _agendar(item)
        _salvar()
        q, dests = item["quando"], _restantes(item)
    threading.Thread(target=_sincronizar_nuvem, kwargs={"forcar": True}, daemon=True).start()
    wake.set()
    if not q:
        return "Postando agora..."
    msg = f"Agendado para {_quando_txt(q)}."
    sem_sched = set(fila["config"].get("sem_agendamento_meta") or [])
    fb_meta = any(d.startswith("fb:") and d.partition(":")[2] not in sem_sched for d in dests) and q - time.time() >= NATIVO_MIN_S
    fb_direto = any(d.startswith("fb:") and d.partition(":")[2] in sem_sched for d in dests) and q - time.time() >= NATIVO_MIN_S
    if fb_meta:
        msg += " Facebook: vai agendado direto na Meta."
    if fb_direto:
        msg += " Facebook: o Kakashi publica direto no horario (sem usar o agendador da Meta)."
    if any(d.startswith("ig:") for d in dests) or fb_direto or q - time.time() < NATIVO_MIN_S:
        msg += " Deixe o painel aberto nesse horario."
    return msg + _ritmo_txt()


def destinos_lote(body: dict) -> str:
    """The same destinations (one Page, or its Facebook + Instagram) for many videos at once."""
    destinos = _validar_destinos(body.get("destinos"))
    if not destinos:
        raise Recusa("Escolha uma Pagina.")
    feitos, pulados = 0, 0
    with lock:
        for iid in body.get("ids") or []:
            item = _item(iid)
            if not item:
                continue
            if item["status"] not in ("rascunho", "erro", "preparando"):
                pulados += 1
                continue
            item["destinos"] = list(destinos)
            item.pop("auto", None)
            feitos += 1
        _salvar()
    if not feitos:
        raise Recusa("Nenhum dos selecionados pode mudar: so rascunhos e videos com erro.")
    return (f"{feitos} video(s) agora vao para {' e '.join(map(_nome_destino, destinos))}." +
            (f" {pulados} ja postado(s) ou agendado(s) ficaram como estavam." if pulados else ""))


def lote(body: dict) -> str:
    """Schedule drafts from inicio, each Page's videos spaced by intervalo_h (the Pages a few minutes apart, so they
    do not all post at once): the ones in "ids" (ticked on the page), else every draft (only the ones going to
    "pagina" when given)."""
    _sem_bloqueio()
    inicio, passo = int(body.get("inicio") or 0), float(body.get("intervalo_h") or 0) * 3600
    pid = str(body.get("pagina") or "")
    ids = body.get("ids")

    def da_pagina(i: dict) -> bool:
        if ids is not None:
            return i["id"] in ids
        return not pid or any(d.partition(":")[2] == pid for d in i["destinos"])
    if inicio < time.time() - 60:
        raise Recusa("Escolha um horario no futuro para o primeiro.")
    if passo < 15 * 60:
        raise Recusa("Deixe pelo menos 15 minutos entre um video e outro.")
    feitos, pulados, vez = [], 0, {}
    with lock:
        for item in sorted((i for i in fila["itens"] if i["status"] == "rascunho" and da_pagina(i)), key=lambda i: i["id"]):
            pg = item["destinos"][0].partition(":")[2] if item["destinos"] else ""
            desloc = 0 if not vez and pg not in vez else int(hashlib.md5(pg.encode()).hexdigest(), 16) % 27 * 60
            vez.setdefault(pg, [0, desloc])
            quando = inicio + vez[pg][1] + vez[pg][0] * passo
            if (item.get("auto") and (item["quando"] or 0) > time.time() + 60 and roleta_dados()
                    and not _mesmo_dia_roleta(item["quando"], quando)):
                quando = item["quando"]  # a Roleta video keeps its own day: the avatar is on that Page only then
            else:
                vez[pg][0] += 1
            if not item["destinos"] or quando > time.time() + MAX_FUTURO_S:
                pulados += 1
                continue
            item["quando"] = int(quando)
            try:
                _agendar(item)
            except Recusa:
                pulados += 1
                continue
            feitos.append(item["quando"])
        _salvar()
    threading.Thread(target=_sincronizar_nuvem, kwargs={"forcar": True}, daemon=True).start()
    wake.set()
    if not feitos:
        raise Recusa("Nenhum video agendado: escolha a Pagina deles primeiro.")
    feitos.sort()
    return (f"{len(feitos)} video(s) agendado(s), um a cada {passo / 3600:g} h em cada Pagina, de "
            f"{_quando_txt(feitos[0])} a {_quando_txt(feitos[-1])}." +
            (f" {pulados} ficaram de fora (sem Pagina ou alem de 29 dias)." if pulados else "") + _ritmo_txt())


def _ler_horarios(texto: str) -> list[str]:
    """ "10, 15h, 18:30, 20h45" -> ["10:00", "15:00", "18:30", "20:45"]"""
    out = []
    for parte in re.split(r"[,;\s]+", str(texto or "").strip()):
        if not parte:
            continue
        m = re.fullmatch(r"(\d{1,2})(?:[:h](\d{2}))?h?", parte.lower())
        if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
            raise Recusa(f"Horario que nao entendi: '{parte}'. Use por exemplo 10, 15h ou 18:30.")
        h = f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"
        if h not in out:
            out.append(h)
    return sorted(out)


def avatares(body: dict) -> str:
    """Save the Avatar -> Page map ({"mapa": {"27": page id or ""}}) and give drafts without a Page their avatar's."""
    mapa = {}
    for n, pid in (body.get("mapa") or {}).items():
        n = str(n).strip().lstrip("0") or "0"
        if not n.isdigit():
            raise Recusa(f"Avatar invalido: {n}")
        if pid and _pagina(str(pid)):
            mapa[n] = str(pid)
    with lock:
        fila["config"]["avatar_paginas"] = mapa
        aplicados = _aplicar_avatares()
        _salvar()
    return f"Mapa salvo: {len(mapa)} avatar(es) com Pagina." + (
        f" {aplicados} rascunho(s) sem Pagina receberam a do seu avatar." if aplicados else "")


def paginas_config(body: dict) -> str:
    """Rotation Pages and posting times (the default and each Page's own)."""
    padrao = _ler_horarios(body.get("horarios_padrao"))
    if not padrao:
        raise Recusa("Deixe pelo menos um horario padrao.")
    proprios = {}
    for pid, texto in (body.get("horarios") or {}).items():
        if not _pagina(pid):
            continue
        h = _ler_horarios(texto)
        if h:
            proprios[pid] = h
    rodizio = [pid for pid in body.get("rodizio") or [] if _pagina(pid)]
    with lock:
        fila["config"].update(horarios_padrao=padrao, horarios_paginas=proprios, rodizio=rodizio)
        _salvar()
    return f"Salvo: {len(rodizio)} Pagina(s) no rodizio, horario padrao {', '.join(padrao)}."


def agendar_sugeridos(body: dict) -> str:
    """Schedule drafts at their own time; one without a time (or with one already gone) gets the next free
    time of its Page. Takes the ticked ones ("ids"), else every draft (of "pagina" when given)."""
    _sem_bloqueio()
    ids, pid, agora = body.get("ids"), str(body.get("pagina") or ""), time.time()
    feitos, sem_pagina = [], 0
    with lock:
        for item in sorted((i for i in fila["itens"] if i["status"] == "rascunho"), key=lambda i: i["id"]):
            if ids is not None and item["id"] not in ids or ids is None and pid and \
                    not any(d.partition(":")[2] == pid for d in item["destinos"]):
                continue
            if not item["destinos"]:
                sem_pagina += 1
                continue
            if not item["quando"] or item["quando"] < agora + 60:
                lugar = item.get("auto") and _lugar_auto(item["nome"])
                if lugar:
                    item["destinos"], item["quando"] = _destinos_drive(lugar[0]), lugar[1]
                else:
                    item["quando"] = proximo_horario(item["destinos"][0].partition(":")[2])
            try:
                if not item["quando"]:
                    raise Recusa("sem horario livre")
                _agendar(item)
            except Recusa:
                sem_pagina += 1
                continue
            feitos.append(item["quando"])
        _salvar()
    threading.Thread(target=_sincronizar_nuvem, kwargs={"forcar": True}, daemon=True).start()
    wake.set()
    if not feitos:
        raise Recusa("Nenhum video agendado: escolha a Pagina deles primeiro.")
    feitos.sort()
    return (f"{len(feitos)} video(s) agendado(s) nos horarios de cada Pagina, de {_quando_txt(feitos[0])} "
            f"a {_quando_txt(feitos[-1])}." + (f" {sem_pagina} ficaram de fora (sem Pagina)." if sem_pagina else "") +
            _ritmo_txt())


def cancelar(body: dict) -> str:
    """Back to draft. What Meta already holds as scheduled is deleted there; what was posted stays posted."""
    with lock:
        item = _item(body.get("id"))
        if not item or item["status"] != "agendado":
            raise Recusa("Esse video nao esta agendado.")
        if item["andamento"]:
            raise Recusa("Esse video esta sendo enviado agora. Espere terminar.")
        na_meta = [(d, r) for d, r in item["resultados"].items() if r["estado"] == "agendado_meta"]
        item["status"] = "rascunho"
        _salvar()
    falhas = []
    for d, r in na_meta:
        p = _pagina(d.partition(":")[2])
        try:
            if not p:
                raise GraphError("Pagina desconectada")
            graph("DELETE", r["id"], p["token"])
            with lock:
                item["resultados"].pop(d, None)
        except GraphError as e:
            falhas.append(f"{_nome_destino(d)}: {e}")
    with lock:
        _salvar()
    if falhas:
        return "Voltou para rascunho, mas nao consegui tirar o agendamento da Meta em " + "; ".join(falhas)
    return "Voltou para rascunho." + (" O agendamento na Meta foi apagado." if na_meta else "")


def reagendar(body: dict) -> str:
    """New time for a scheduled video. What Meta already holds is taken back there; the worker sends it again
    for the new time (scheduled at Meta again when it is far enough ahead). Destinations already posted stay posted."""
    _sem_bloqueio()
    with lock:
        item = _item(body.get("id"))
        if not item or item["status"] != "agendado":
            raise Recusa("So da para mudar o horario de videos agendados.")
        if item["andamento"]:
            raise Recusa("Esse video esta sendo enviado agora. Espere terminar.")
        try:
            novo = int(body.get("quando") or 0)
        except (TypeError, ValueError):
            novo = 0
        if not novo:
            raise Recusa("Escolha o novo horario.")
        if novo < time.time() + 60:
            raise Recusa("Escolha um horario daqui a pelo menos 1 minuto.")
        if novo > time.time() + MAX_FUTURO_S:
            raise Recusa("Agende para no maximo 29 dias a frente.")
        if item.get("quando") == novo:
            raise Recusa("Esse ja e o horario do video.")
        if not [d for d in item["destinos"] if item["resultados"].get(d, {}).get("estado") != "ok"]:
            raise Recusa("Esse video ja foi postado em todos os destinos.")
        na_meta = [(d, r) for d, r in item["resultados"].items() if r["estado"] == "agendado_meta"]
        item["andamento"] = "Mudando o horario..."  # keeps the worker off it while Meta is updated
    falhas = []
    for d, r in na_meta:
        p = _pagina(d.partition(":")[2])
        try:
            if not p:
                raise GraphError("Pagina desconectada")
            graph("DELETE", r["id"], p["token"])
            with lock:
                item["resultados"].pop(d, None)
        except GraphError as e:
            falhas.append(f"{_nome_destino(d)}: {e}")
    with lock:
        item["andamento"] = ""
        if falhas:  # the old post is still scheduled at Meta: keep the old time so the panel tells the truth
            _salvar()
            raise Recusa("Nao consegui tirar o agendamento antigo da Meta em " + "; ".join(falhas) +
                         ". O horario continua o mesmo.")
        item["quando"] = novo
        _salvar()
    wake.set()
    return f"Novo horario: {_quando_txt(novo)}." + (" O agendamento antigo foi apagado na Meta e o video vai ser"
                                                     " reagendado em instantes." if na_meta else "")


def remover(body: dict) -> str:
    """One video ({"id"}) or several ({"ids"}); scheduled ones stay until they go back to draft."""
    tirados, presos = [], 0
    with lock:
        for iid in body.get("ids") or [body.get("id")]:
            item = _item(iid)
            if not item:
                continue
            if item["status"] == "agendado" or item["andamento"]:
                presos += 1
                continue
            fila["itens"].remove(item)
            tirados.append(item)
        _salvar()
    if not tirados:
        raise Recusa("Volte o video para rascunho antes de remover." if presos else "Video nao encontrado na fila.")
    LIXEIRA.mkdir(parents=True, exist_ok=True)
    for item in tirados:
        f = VIDEOS / item["arquivo"]
        if f.is_file():
            shutil.move(str(f), str(LIXEIRA / f.name))
            with lock:  # the trash is emptied counting from now (the moved file keeps its old date)
                fila.setdefault("lixeira_em", {})[f.name] = int(time.time())
    _salvar()
    msg = ("Tirado da fila" if len(tirados) == 1 else f"{len(tirados)} videos tirados da fila") + \
        f" (ficam em output/postagem/_lixeira por {fila['config']['limpar_dias']} dias)."
    return msg + (f" {presos} agendado(s) ficaram: volte para rascunho antes de tirar." if presos else "")


def importar(body: dict) -> str:
    achado = _fonte(str(body.get("fonte") or ""))
    if not achado:
        raise Recusa("Video nao encontrado.")
    origem, p = achado
    if any(i.get("fonte") == str(p) for i in fila["itens"]):
        raise Recusa("Esse video ja esta na fila.")
    adicionar(p, origem, _nome_fonte(origem, p))
    return f"'{_nome_fonte(origem, p)}' entrou na fila."


def configurar(body: dict) -> str:
    ligar = bool(body.get("auto_kakashi"))
    if ligar and not fila["config"]["auto_kakashi"]:
        _marcar_vistos()
    with lock:
        fila["config"]["auto_kakashi"] = ligar
        _salvar()
    return ("Os videos novos do Kakashi vao entrar na fila como rascunho." if ligar
            else "Os videos do Kakashi nao entram mais sozinhos.")


def configurar_drive(body: dict) -> str:
    pasta = str(body.get("pasta") or "").strip().strip('"')
    if not pasta:
        raise Recusa("Escreva o caminho da pasta do Google Drive.")
    with lock:
        if pasta != fila["config"]["pasta_drive"]:
            fila["drive_vistos"] = {}  # another folder: everything in it is new
        fila["config"].update(pasta_drive=pasta, drive_ig=bool(body.get("ig")))
        _salvar()
    try:
        novas = criar_pastas_drive()
    except OSError as e:
        raise Recusa(f"Nao consegui criar as pastas: {e}")
    if novas is None:
        raise Recusa(f"Nao achei {Path(pasta).parent}. O Google Drive para computador esta aberto?")
    return f"Pasta salva. {novas} pasta(s) de Pagina criada(s)." if novas else "Pasta salva."


def desconectar(_body: dict) -> str:
    CONTAS_FILE.unlink(missing_ok=True)
    return "Contas desconectadas (tokens apagados deste PC)."


def abrir_pasta(body: dict) -> str:
    alvo = {"entrada": ENTRADA, "videos": VIDEOS, "drive": _drive()}.get(body.get("what"), ENTRADA)
    if body.get("what") == "drive" and not alvo.is_dir():
        raise Recusa("A pasta do Google Drive nao foi encontrada.")
    alvo.mkdir(parents=True, exist_ok=True)
    os.startfile(alvo)
    return "ok"


CRIATIVO_MAX_PCT = 70  # on Phase 1 Pages, no creative above this share of a day's videos, summing every Page: the rest
# tests new creatives, so the next winner shows up while the current one still works...
CRIATIVO_MIN_VIDEOS = 4  # ...once that day has this many: 1 of 1 says nothing


def _avisos_dia() -> dict[int, list[str]]:
    """Item id -> warnings about its day: its Page already has another copy that day (one copy per Page a day), its
    creative passes CRIATIVO_MAX_PCT of the day's videos on Phase 1 Pages, or the radar found its copy did not work.
    Copies the radar knows as the same creative count as one. Nothing is held back."""
    radar = sys.modules.get("radar")  # loaded by the panel; read here only, so no import cycle
    dados = getattr(radar, "estado", None) or {}
    criativos, fracas = dados.get("config", {}).get("criativos", {}), dados.get("fracas", {})
    av_fracos, av_mantidos = dados.get("avatares_fracos", {}), set(dados.get("avatares_fracos_ignorados", []))
    S = roleta_dados() or {}
    fase1 = {pid for rid, pid in roleta.casar_paginas(S, contas().get("paginas", [])).items()
             if (S["paginas"][rid].get("fase") or 1) == 1 and not S["paginas"][rid].get("pausada")} if S else set()
    vistos, out, do_dia = {}, {}, {}
    ativos = ("preparando", "rascunho", "agendado")
    for i in sorted(fila["itens"], key=lambda i: (i.get("quando") or 0, i["id"])):
        m = i.get("quando") and i["status"] != "erro" and COPIA_RE.search(i["nome"])
        if not m:
            continue
        copia = f"copy {int(m.group(1))}"
        criativo = (criativos.get(copia) or {}).get("nome") or copia
        dia = time.localtime(i["quando"] - FIM_DO_DIA_H * 3600)
        data = time.strftime("%Y-%m-%d", dia)
        n = _avatar(i["nome"])
        if n in av_fracos and n not in av_mantidos and i["status"] in ativos:
            f = av_fracos[n]
            out.setdefault(i["id"], []).append(f"O Avatar {n} não funcionou: {f['videos']} vídeos em {f['paginas']} Páginas, nenhum bom, "
                                               f"{str(f['mediana']).replace('.', ',')}× o normal.")
        if copia in fracas and i["status"] in ativos:
            f = fracas[copia]
            out.setdefault(i["id"], []).append(f"A {copia} não funcionou: {f['videos']} vídeos somando as Páginas e nenhum bom "
                                               f"(o melhor fez {f['melhor']:,} views).".replace(",", "."))
        for pid in {d.partition(":")[2] for d in i["destinos"]}:
            antes = vistos.setdefault((pid, data), (criativo, copia))
            if antes[0] != criativo and i["status"] in ativos:
                nome = (_pagina(pid) or {}).get("nome", "A Pagina")
                out.setdefault(i["id"], []).append(f"Uma copy por Página por dia: {nome} já tem a {antes[1]} em "
                                                   f"{time.strftime('%d/%m', dia)} e este vídeo é a {copia}.")
            if pid in fase1:
                do_dia.setdefault(data, []).append((i, criativo))
    for data, lista in do_dia.items():
        if len(lista) < CRIATIVO_MIN_VIDEOS:
            continue
        for criativo, n in Counter(c for _, c in lista).items():
            if 100 * n <= CRIATIVO_MAX_PCT * len(lista):
                continue
            texto = (f"{criativo} está em {n} de {len(lista)} vídeos da Fase 1 em {data[8:]}/{data[5:7]}: o limite é "
                     f"{CRIATIVO_MAX_PCT}%, deixe pelo menos {100 - CRIATIVO_MAX_PCT}% para testar criativos novos.")
            for i, c in lista:
                if c == criativo and i["status"] in ativos and texto not in out.get(i["id"], []):
                    out.setdefault(i["id"], []).append(texto)
    return out


def estado() -> dict:
    with lock:
        extra = _avisos_dia()
        base = _drive()
        drive = {"pasta": str(base), "existe": base.is_dir(), "ig": fila["config"]["drive_ig"],
                 "paginas": sum(1 for n in _pastas_paginas() if (base / n).is_dir()) if base.is_dir() else 0}
        itens = [{**i, "avisos": [*i.get("avisos", []), *extra[i["id"]]]} if i["id"] in extra else i for i in fila["itens"]]
        return {"contas": contas_publico(), "itens": itens, "config": fila["config"], "bloqueio": fila.get("bloqueio"),
                "roleta": roleta_publica(), "avatares_fotos": list(_mapa_fotos_avatares()),
                "entrada": str(ENTRADA), "drive": drive, "log": fila["log"][-30:]}


ACOES = {"conectar": conectar, "desconectar": desconectar, "remover-perfil": remover_perfil,
         "configurar-massa": configurar_paginas_massa, "salvar": salvar, "agendar": agendar, "lote": lote,
         "destinos-lote": destinos_lote, "paginas-config": paginas_config, "agendar-sugeridos": agendar_sugeridos,
         "atualizar-paginas": atualizar_paginas, "avatares": avatares,
         "cancelar": cancelar, "reagendar": reagendar, "remover": remover, "importar": importar, "config": configurar, "drive": configurar_drive,
         "abrir-pasta": abrir_pasta}


# ─── HTTP (called by painel.py's Handler) ───────────────────


def get(h, path: str) -> None:
    if path == "/api/postagem/estado":
        return h.send_json(estado())
    if path == "/api/postagem/kakashi":
        return h.send_json(lista_kakashi())
    if path == "/api/postagem/roleta-db":
        return h.send_json(roleta_db())
    if path == "/postagem-roleta":
        return h.send_file(Path(__file__).with_name("roleta.html"), "text/html; charset=utf-8")
    if path.startswith("/postagem-foto-pagina/"):
        pid = re.sub(r"[^a-zA-Z0-9_-]", "", path[len("/postagem-foto-pagina/"):])
        return h.send_file(FOTOS_PAGINAS / f"{pid}.jpg", "image/jpeg")
    if path.startswith("/postagem-video/"):
        return h.send_file(_dentro(VIDEOS, path[len("/postagem-video/"):]))
    if path.startswith("/postagem-fonte/"):
        achado = _fonte(path[len("/postagem-fonte/"):])
        return h.send_file(achado[1] if achado else None, "video/mp4")
    h.send_error(404)


def post(h, path: str) -> None:
    if path == "/api/postagem/upload-foto-pagina":
        pid = re.sub(r"[^a-zA-Z0-9_-]", "", unquote(h.headers.get("X-Page-Id", "")))
        name = Path(unquote(h.headers.get("X-Filename", "foto.jpg"))).name
        if not pid or not _pagina(pid):
            return h.send_json({"ok": False, "msg": "Pagina nao encontrada."}, 400)
        if Path(name).suffix.lower() not in IMAGE_EXTS:
            return h.send_json({"ok": False, "msg": "Use JPG, PNG, WEBP ou JFIF."}, 400)
        FOTOS_PAGINAS.mkdir(parents=True, exist_ok=True)
        tmp = FOTOS_PAGINAS / f"_up_{pid}_{time.time_ns()}{Path(name).suffix.lower()}"
        if not h.save_upload(tmp):
            tmp.unlink(missing_ok=True)
            return h.send_json({"ok": False, "msg": "Arquivo vazio ou grande demais."}, 400)
        dest = FOTOS_PAGINAS / f"{pid}.jpg"
        if tmp.suffix.lower() in (".jpg", ".jpeg"):
            shutil.move(str(tmp), str(dest))
        else:
            res = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(tmp),
                                  "-frames:v", "1", "-q:v", "2", str(dest)],
                                 capture_output=True, creationflags=NO_WINDOW)
            tmp.unlink(missing_ok=True)
            if res.returncode != 0 or not dest.is_file():
                return h.send_json({"ok": False, "msg": "Nao consegui converter a imagem para JPG."}, 400)
        return h.send_json({"ok": True, "msg": "Foto pronta para enviar na Pagina.", "pid": pid})
    if path == "/api/postagem/upload":
        name = Path(unquote(h.headers.get("X-Filename", "video.mp4"))).name
        if Path(name).suffix.lower() not in VIDEO_EXTS:
            return h.send_json({"ok": False, "msg": "Use MP4, MOV, M4V ou WEBM."}, 400)
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = DIR / f"_upload_{time.time_ns()}{Path(name).suffix.lower()}"
        if not h.save_upload(tmp):
            tmp.unlink(missing_ok=True)
            return h.send_json({"ok": False, "msg": "Arquivo vazio ou grande demais (max 400 MB)"}, 400)
        adicionar(tmp, "upload", name, mover=True)
        return h.send_json({"ok": True, "msg": f"'{name}' entrou na fila."})
    acao = ACOES.get(path[len("/api/postagem/"):])
    if not acao and path != "/api/postagem/roleta-db":
        return h.send_error(404)
    length = int(h.headers.get("Content-Length") or 0)
    body = json.loads(h.rfile.read(length) or b"{}")
    if path == "/api/postagem/roleta-db":
        try:
            return h.send_json({"ok": True, **roleta_gravar(body)})
        except Recusa as e:
            return h.send_json({"ok": False, "msg": str(e)}, 400)
    try:
        return h.send_json({"ok": True, "msg": acao(body)})
    except Recusa as e:
        return h.send_json({"ok": False, "msg": str(e)}, 400)
    except GraphError as e:
        return h.send_json({"ok": False, "msg": f"A Meta respondeu: {e}"}, 502)
