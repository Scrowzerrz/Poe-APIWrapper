# -*- coding: utf-8 -*-
# Poe GraphQL sender — Multi-conta (p-b) + troca dinâmica + chat CLI + modelo configurável
#
# Recursos:
# - Várias contas (cookies p-b) com fallback automático quando crédito/acesso acaba.
# - cf_clearance e __cf_bm FIXOS (compartilhados entre contas).
# - Chat via CLI (/model <slug>, /switch, /quit).
# - Captura e replay 1:1 (sendMessageMutation/subscriptionsMutation) + sniffer WS/SSE.
# - Decodifica envelopes WS {min_seq, messages:["<json>", ...]} e filtra apenas a resposta do BOT.
#
# Observação: este script usa Playwright e websockets. Instale dependências compatíveis:
#   pip install "playwright>=1.41" "websockets>=11,<13" requests
#   playwright install chromium
#
import asyncio
import json
import sys
from typing import Optional, Dict, Any, List, Tuple

import requests
import websockets
from playwright.async_api import async_playwright

# ==============================================================================
# 0) CONFIGURAÇÕES — COOKIES E MODELO
# ==============================================================================

# LISTA de contas. Cada item precisa de pelo menos "p_b".
# Opcionalmente pode ter "p_lat" e/ou "p_sb".
ACCOUNTS: List[Dict[str, str]] = [
    {
        "label": "acc1",
        "p_b": "",
        "p_lat": "",
        "p_sb": "",
    },
    # Adicione outras contas aqui...
    # {"label": "acc2", "p_b": "...", "p_lat": "", "p_sb": ""},
]

# FIXOS para TODAS as contas (não precisa um por conta)
CF_CLEARANCE = ""   # Cloudflare
CF_BM        = ""   # __cf_bm

# Modelo padrão (slug do Poe visível na URL). Pode trocar via comando /model <slug>.
DEFAULT_MODEL_SLUG = "GPT-5"

POE_URL_ROOT = "https://poe.com"
POE_API_URL  = f"{POE_URL_ROOT}/api/gql_POST"

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0",
    "Accept": "*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Origin": POE_URL_ROOT,
    # Referer será setado por mensagem, de acordo com o modelo
    "Sec-Ch-Ua": '"Not;A=Brand";v="99", "Microsoft Edge";v="139", "Chromium";v="139"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Content-Type": "application/json",
    "poegraphql": "0",
}

# ==============================================================================
# 1) GERENCIADOR DE CONTAS (multi p-b com fallback)
# ==============================================================================

class AccountPool:
    def __init__(self, accounts: List[Dict[str, str]]):
        if not accounts:
            raise RuntimeError("Nenhuma conta configurada em ACCOUNTS.")
        # Normaliza labels
        for i, a in enumerate(accounts):
            a.setdefault("label", f"acc{i+1}")
            a.setdefault("p_lat", "")
            a.setdefault("p_sb", "")
        self.accounts = accounts
        self.idx = 0

    def current(self) -> Dict[str, str]:
        return self.accounts[self.idx]

    def rotate(self) -> Dict[str, str]:
        self.idx = (self.idx + 1) % len(self.accounts)
        return self.current()

    def label(self) -> str:
        return self.current().get("label", f"acc{self.idx+1}")

    def __len__(self):
        return len(self.accounts)

def _apply_cookies_to_session(session: requests.Session, acc: Dict[str, str]):
    """Aplica cookies da conta ao session; cf_clearance/__cf_bm são compartilhados e fixos."""
    # Limpa cookies relevantes previamente (para evitar mistura acidental entre contas)
    for key in ["p-b", "p-lat", "p-sb", "cf_clearance", "__cf_bm"]:
        try:
            session.cookies.clear(domain="poe.com", path="/", name=key)
        except Exception:
            pass

    session.cookies.set("p-b", acc.get("p_b", ""), domain="poe.com", path="/")
    if acc.get("p_lat"):
        session.cookies.set("p-lat", acc["p_lat"], domain="poe.com", path="/")
    if acc.get("p_sb"):
        session.cookies.set("p-sb", acc["p_sb"], domain="poe.com", path="/")
    if CF_CLEARANCE:
        session.cookies.set("cf_clearance", CF_CLEARANCE, domain="poe.com", path="/")
    if CF_BM:
        session.cookies.set("__cf_bm", CF_BM, domain="poe.com", path="/")

def _session_new() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    return s

# ==============================================================================
# 2) HELPERS
# ==============================================================================

def cookie_header_from_jar(jar: requests.cookies.RequestsCookieJar) -> str:
    return "; ".join(f"{c.name}={c.value}" for c in jar)

def parse_cookie_header_and_set(session: requests.Session, cookie_header: str):
    if not cookie_header:
        return
    parts = [p.strip() for p in cookie_header.split(";") if "=" in p]
    for part in parts:
        try:
            k, v = part.split("=", 1)
            session.cookies.set(k.strip(), v.strip(), domain="poe.com", path="/")
        except Exception:
            continue

def extract_text_pieces(event: Any) -> List[str]:
    out: List[str] = []
    if isinstance(event, dict):
        for k in ("text_new", "response", "delta", "text", "partial_response"):
            v = event.get(k)
            if isinstance(v, str) and v:
                out.append(v)
        for v in event.values():
            if isinstance(v, (dict, list)):
                out.extend(extract_text_pieces(v))
    elif isinstance(event, list):
        for x in event:
            out.extend(extract_text_pieces(x))
    return out

def looks_complete(node: Dict[str, Any]) -> bool:
    state = (node.get("state") or node.get("message_state") or "").lower()
    return state in {"complete", "completed", "stopped", "aborted"}

def is_bot_message(msg: Dict[str, Any], bot_slug: str) -> bool:
    """Heurística robusta p/ diferenciar bot vs usuário."""
    # 1) author
    a = msg.get("author")
    if isinstance(a, dict):
        handle = str(a.get("handle") or "").lower()
        if handle and handle == str(bot_slug).lower():
            return True
        if a.get("__typename") in ("Bot", "PoeBot", "DefaultBot"):
            return True
        t = str(a.get("type") or a.get("role") or "").lower()
        if t in ("bot", "assistant", "ai"):
            return True
        if a.get("isBot") is True:
            return True

    # 2) nó "bot"
    b = msg.get("bot")
    if isinstance(b, dict):
        h = str(b.get("handle") or "").lower()
        if h and h == str(bot_slug).lower():
            return True
        return True

    # 3) tipos/flags no próprio message
    mt = str(msg.get("messageType") or msg.get("type") or "").lower()
    if mt in ("bot", "assistant", "ai"):
        return True

    # 4) sinais típicos de resposta do modelo
    if "suggestedReplies" in msg:
        return True
    if ("text_new" in msg or "partial_response" in msg or "delta" in msg):
        st = str(msg.get("state") or "").lower()
        if st in ("incomplete", "generating", "in_progress", "streaming", "pending", ""):
            return True

    return False

def _has_limit_error_from_gql_json(obj: Dict[str, Any]) -> bool:
    """Detecta mensagens de erro de limite/créditos no JSON de resposta GraphQL."""
    try:
        errs = obj.get("errors") or []
        for e in errs:
            m = str(e.get("message") or "").lower()
            if any(x in m for x in ("limit", "quota", "credit", "points", "rate")):
                return True
    except Exception:
        pass
    return False

# ==============================================================================
# 3) BOOTSTRAP (Playwright) — captura payloads/headers/WS URL
# ==============================================================================

async def get_bootstrap(bot_name: str, cookie_dict: Dict[str, str], prompt_for_capture: str) -> Tuple[Dict[str, Optional[str]], Dict[str, Any]]:
    """Abre a página logada, intercepta send/subs, sniffa WS/SSE e captura frames enviados pelo cliente. NÃO fecha o Playwright aqui."""
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context()

    # Cookies de sessão
    add = []
    for name, v in (("p-b", cookie_dict.get("p-b")),
                    ("p-lat", cookie_dict.get("p-lat")),
                    ("p-sb", cookie_dict.get("p-sb")),
                    ("cf_clearance", cookie_dict.get("cf_clearance")),
                    ("__cf_bm", cookie_dict.get("__cf_bm"))):
        if v:
            add.append({"name": name, "value": v, "domain": "poe.com", "path": "/", "secure": True})
    if add:
        await context.add_cookies(add)

    # Hooks JS
    await context.add_init_script("""
(() => {
  const OW = window.WebSocket;
  if (OW) {
    window.WebSocket = new Proxy(OW, {
      construct(target, args) {
        try { window.__POE_LAST_WS_URL__ = String(args?.[0] ?? ''); } catch(_) {}
        const sock = new target(...args);
        const _send = sock.send;
        try {
          sock.send = function(data) {
            try {
              window.__POE_WS_SENT__ = window.__POE_WS_SENT__ || [];
              let payload = data;
              if (data instanceof ArrayBuffer) payload = "[binary:" + String(data.byteLength||0) + "]";
              window.__POE_WS_SENT__.push(String(payload));
            } catch (e) {}
            return _send.call(this, data);
          };
        } catch (e) {}
        return sock;
      }
    });
  }
  const OE = window.EventSource;
  if (OE) {
    window.EventSource = new Proxy(OE, {
      construct(target, args) {
        try { window.__POE_LAST_ES_URL__ = String(args?.[0] ?? ''); } catch(_) {}
        return new target(...args);
      }
    });
  }
})();
""")

    page = await context.new_page()

    captured: Dict[str, Optional[str]] = {
        # sendMessageMutation
        "send_payload_raw": None,
        "send_payload_dict": None,
        "send_req_headers": {},
        "send_cookie_header": None,
        # subscriptionsMutation
        "subs_payload_raw": None,
        "subs_payload_dict": None,
        "subs_req_headers": {},
        "subs_cookie_header": None,
        # metas
        "formkey_hdr": None,
        "revision_hdr": None,
        "tag_id_hdr": None,
        "tchannel_hdr": None,
        # stream
        "ws_url": None,
        "es_url": None,
        # frames
        "ws_client_frames": [],
    }

    def maybe_set_updates_url(u: str):
        if not u:
            return
        if ("updates" in u) and ("channel=" in u):
            if u.startswith("ws"):
                captured["ws_url"] = u
            else:
                captured["es_url"] = u

    context.on("websocket", lambda ws: maybe_set_updates_url((ws.url or "")))

    def on_req(req):
        try:
            if getattr(req, "resource_type", "") == "eventsource":
                maybe_set_updates_url(req.url)
            else:
                if "updates" in (req.url or "") and "channel=" in (req.url or ""):
                    maybe_set_updates_url(req.url)
        except Exception:
            pass

    context.on("request", on_req)
    page.on("request", on_req)

    async def route_handler(route, request):
        try:
            if request.url.endswith("/api/gql_POST"):
                hdrs = {k.lower(): v for k, v in request.headers.items()}
                body = request.post_data or ""
                qn = hdrs.get("poe-queryname", "")

                if "poe-formkey" in hdrs:  captured["formkey_hdr"]  = hdrs["poe-formkey"]
                if "poe-revision" in hdrs: captured["revision_hdr"] = hdrs["poe-revision"]
                if "poe-tchannel" in hdrs: captured["tchannel_hdr"] = hdrs["poe-tchannel"]
                if "poe-tag-id" in hdrs:   captured["tag_id_hdr"]   = hdrs["poe-tag-id"]

                if qn == "sendMessageMutation" or "sendMessageMutation" in (body or ""):
                    captured["send_req_headers"] = {**(captured["send_req_headers"] or {}), **hdrs}
                    captured["send_payload_raw"] = body
                    if "cookie" in hdrs: captured["send_cookie_header"] = hdrs["cookie"]
                    try:
                        captured["send_payload_dict"] = json.loads(body)
                    except Exception:
                        pass
                    await route.fulfill(status=200, content_type="application/json", body='{"data":{"messageCreate":null}}')
                    return

                if qn == "subscriptionsMutation" or '"queryName":"subscriptionsMutation"' in (body or ""):
                    captured["subs_req_headers"] = {**(captured["subs_req_headers"] or {}), **hdrs}
                    captured["subs_payload_raw"] = body
                    if "cookie" in hdrs: captured["subs_cookie_header"] = hdrs["cookie"]
                    try:
                        captured["subs_payload_dict"] = json.loads(body)
                    except Exception:
                        pass
                    await route.continue_()
                    return
        except Exception:
            pass
        await route.continue_()

    await context.route("**/api/gql_POST", route_handler)

    await page.goto(f"{POE_URL_ROOT}/{bot_name}", wait_until="domcontentloaded")

    # Digita o prompt
    try:
        locator = page.locator("textarea")
        if await locator.count() > 0:
            ta = locator.first
            await ta.click()
            await ta.fill(prompt_for_capture)
        else:
            ed = page.locator('div[contenteditable="true"]').first
            await ed.click()
            await ed.fill(prompt_for_capture)
        await page.keyboard.press("Enter")
    except Exception:
        pass

    # Aguarda capturas mínimas
    for _ in range(150):
        got_send = bool(captured["send_payload_raw"])
        got_subs = bool(captured["subs_payload_raw"])
        got_stream = bool(captured["ws_url"] or captured["es_url"])
        if got_send and (got_subs or got_stream):
            break
        await page.wait_for_timeout(200)

    # Hooks JS + frames enviados pelo cliente
    try:
        last = await page.evaluate("""({
          ws: window.__POE_LAST_WS_URL__||null,
          es: window.__POE_LAST_ES_URL__||null,
          frames: (window.__POE_WS_SENT__||[]).slice(-10)
        })""")
        if last:
            if not (captured["ws_url"] or captured["es_url"]):
                if last.get("ws"): maybe_set_updates_url(last.get("ws"))
                if last.get("es"): maybe_set_updates_url(last.get("es"))
            frames = last.get("frames") or []
            captured["ws_client_frames"] = [f for f in frames if isinstance(f, str) and not f.startswith("[")]
    except Exception:
        pass

    info = {
        "send_payload_raw": captured["send_payload_raw"],
        "send_payload_dict": captured["send_payload_dict"],
        "send_req_headers": captured["send_req_headers"],
        "send_cookie_header": captured["send_cookie_header"],
        "subs_payload_raw": captured["subs_payload_raw"],
        "subs_payload_dict": captured["subs_payload_dict"],
        "subs_req_headers": captured["subs_req_headers"],
        "subs_cookie_header": captured["subs_cookie_header"],
        "formkey": captured["formkey_hdr"],
        "revision": captured["revision_hdr"],
        "tag_id": captured["tag_id_hdr"],
        "tchannel_hdr": captured["tchannel_hdr"],
        "ws_url": captured["ws_url"],
        "es_url": captured["es_url"],
        "ws_client_frames": captured["ws_client_frames"],
    }
    handles = {"p": p, "browser": browser, "context": context, "page": page}
    if not info["send_payload_raw"]:
        await cleanup_playwright(handles)
        raise RuntimeError("Falha: não capturei a sendMessageMutation (body RAW).")
    return info, handles

async def cleanup_playwright(handles: Dict[str, Any]):
    try:
        if handles.get("context"):
            await handles["context"].close()
    except Exception:
        pass
    try:
        if handles.get("browser"):
            await handles["browser"].close()
    except Exception:
        pass
    try:
        if handles.get("p"):
            await handles["p"].stop()
    except Exception:
        pass

# ==============================================================================
# 4) LISTENERS (WS/SSE) — retornam (texto, flags)
# ==============================================================================

def _scan_point_limit(inner_frame: Dict[str, Any]) -> bool:
    """Detecta sinais de 'messagePointLimitUpdated' em frames WS."""
    try:
        pl = inner_frame.get("payload") or {}
        uid = pl.get("unique_id") or ""
        if isinstance(uid, str) and "messagePointLimitUpdated" in uid:
            return True
    except Exception:
        pass
    return False

async def listen_via_websocket_url(ws_url: str,
                                   session: requests.Session,
                                   target_chat_id: Optional[str],
                                   bot_slug: str,
                                   meta_headers: Dict[str, str],
                                   initial_client_frames: List[str],
                                   timeout_seconds: int = 120) -> Tuple[str, Dict[str, bool]]:
    """Conecta no ws_url, envia frames iniciais (se houver), desenvelopa 'messages[]' e ESCUTA APENAS O BOT."""
    flags = {"point_limit": False}
    def _iter_inner_frames(packet: Any):
        # packet pode ser str/dict; envelope com "messages": [ "<json>", ... ]
        try:
            if isinstance(packet, (bytes, bytearray)):
                packet = packet.decode("utf-8", "ignore")
            if isinstance(packet, str):
                packet = json.loads(packet)
            if not isinstance(packet, dict):
                return
        except Exception:
            return

        msgs = packet.get("messages")
        if isinstance(msgs, list) and msgs:
            for m in msgs:
                try:
                    if isinstance(m, (bytes, bytearray)):
                        m = m.decode("utf-8", "ignore")
                    if isinstance(m, str):
                        m = json.loads(m)
                    if isinstance(m, dict):
                        yield m
                except Exception:
                    continue
        else:
            yield packet

    def _iter_subscription_updates(frame: Dict[str, Any]):
        mtype = frame.get("mtype") or frame.get("message_type") or ""
        if mtype != "subscriptionUpdate":
            return
        inner = frame.get("payload") or {}
        if isinstance(inner.get("payloads"), list):
            for pl in inner["payloads"]:
                yield pl.get("data") or {}
            return
        if isinstance(inner.get("data"), dict):
            yield inner["data"]
            return
        inner2 = inner.get("payload")
        if isinstance(inner2, dict) and isinstance(inner2.get("data"), dict):
            yield inner2["data"]
            return

    def _extract_nodes(d: Dict[str, Any]) -> List[Dict[str, Any]]:
        nodes = []
        for key in ("messageTextUpdated", "messageAdded", "messageUpdated", "messageCreated", "messageStateUpdated"):
            node = d.get(key)
            if isinstance(node, dict):
                nodes.append(node)
        if not nodes and isinstance(d, dict):
            for v in d.values():
                if isinstance(v, dict) and ("text_new" in v or "message" in v):
                    nodes.append(v)
        return nodes

    ws_headers = {
        "Origin": "https://poe.com",
        "User-Agent": BASE_HEADERS["User-Agent"],
        "Cookie": cookie_header_from_jar(session.cookies),
        "Accept-Language": BASE_HEADERS.get("Accept-Language", "en-US,en;q=0.9"),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    for k, v in (meta_headers or {}).items():
        if v:
            ws_headers[k] = v
    extra = list(ws_headers.items())

    full = ""
    async with websockets.connect(ws_url, extra_headers=extra, max_size=None) as ws:
        for f in initial_client_frames or []:
            try:
                await ws.send(f)
            except Exception:
                pass

        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break

            try:
                candidates = list(_iter_inner_frames(message))
            except Exception:
                candidates = []

            got_update = False
            for inner in candidates:
                # marca limite de pontos se aparecer
                if _scan_point_limit(inner):
                    flags["point_limit"] = True

                mtype = inner.get("mtype") or inner.get("message_type") or "unknown"
                if mtype != "subscriptionUpdate":
                    continue
                for data_obj in _iter_subscription_updates(inner):
                    got_update = True
                    for node in _extract_nodes(data_obj):
                        msg = node.get("message") if isinstance(node.get("message"), dict) else node
                        chat_id_here = str(msg.get("chatId") or msg.get("chat_id") or "")
                        if target_chat_id and chat_id_here and str(target_chat_id) != chat_id_here:
                            continue
                        # Apenas bot
                        if not is_bot_message(msg, bot_slug):
                            continue
                        pieces = extract_text_pieces(msg)
                        if pieces:
                            chunk = "".join(pieces)
                            print(chunk, end="", flush=True)
                            full += chunk
                        if looks_complete(msg) or looks_complete(node):
                            return full, flags
            if got_update:
                deadline = asyncio.get_event_loop().time() + timeout_seconds
    return full, flags

def _build_stream_headers(session: requests.Session, bot_slug: str) -> Dict[str, str]:
    return {
        "Origin": "https://poe.com",
        "Referer": f"{POE_URL_ROOT}/{bot_slug}",
        "User-Agent": BASE_HEADERS["User-Agent"],
        "Accept-Language": BASE_HEADERS.get("Accept-Language", "en-US,en;q=0.9"),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Cookie": cookie_header_from_jar(session.cookies),
    }

def _matches_chat_and_chunk(payload: Dict[str, Any], target_chat_id: Optional[str], bot_slug: str) -> Tuple[bool, str, Dict[str, Any]]:
    def _extract_nodes(d: Dict[str, Any]) -> List[Dict[str, Any]]:
        nodes = []
        for key in ("messageTextUpdated","messageAdded","messageUpdated","messageCreated","messageStateUpdated"):
            node = d.get(key)
            if isinstance(node, dict):
                nodes.append(node)
        if not nodes and isinstance(d, dict):
            for v in d.values():
                if isinstance(v, dict) and ("text_new" in v or "message" in v):
                    nodes.append(v)
        return nodes

    try:
        mtype = payload.get("mtype") or payload.get("message_type")
        if mtype != "subscriptionUpdate":
            return False, "", {}
        inner = payload.get("payload") or {}
        datas: List[Dict[str, Any]] = []
        if isinstance(inner.get("payloads"), list):
            datas = [pl.get("data") or {} for pl in inner["payloads"]]
        elif isinstance(inner.get("data"), dict):
            datas = [inner["data"]]
        elif isinstance(inner.get("payload"), dict) and isinstance(inner["payload"].get("data"), dict):
            datas = [inner["payload"]["data"]]
        for d in datas:
            for node in _extract_nodes(d):
                msg = node.get("message") if isinstance(node.get("message"), dict) else node
                chat_id_here = str(msg.get("chatId") or msg.get("chat_id") or "")
                if target_chat_id and chat_id_here and str(target_chat_id) != chat_id_here:
                    continue
                if not is_bot_message(msg, bot_slug):
                    continue
                pieces = extract_text_pieces(msg)
                return True, ("".join(pieces) if pieces else ""), msg
    except Exception:
        pass
    return False, "", {}

async def listen_via_sse_url(es_url: str, session: requests.Session, target_chat_id: Optional[str], bot_slug: str, timeout_seconds: int = 120) -> Tuple[str, Dict[str, bool]]:
    flags = {"point_limit": False}
    headers = _build_stream_headers(session, bot_slug)
    headers["Accept"] = "text/event-stream"
    full = ""
    with session.get(es_url, headers=headers, stream=True) as r:
        r.raise_for_status()
        import time
        end_at = time.time() + timeout_seconds
        for raw in r.iter_lines(decode_unicode=True):
            if time.time() > end_at:
                break
            if not raw or raw.startswith(":"):
                continue
            if not raw.startswith("data:"):
                continue
            line = raw[5:].strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            # checa limite (fallback simples)
            if "messagePointLimitUpdated" in line:
                flags["point_limit"] = True
            ok, chunk, msg = _matches_chat_and_chunk(payload, target_chat_id, bot_slug)
            if ok and chunk:
                print(chunk, end="", flush=True)
                full += chunk
            if msg and looks_complete(msg):
                break
    return full, flags

# ==============================================================================
# 5) PIPELINE DE UMA MENSAGEM (com troca dinâmica de conta em caso de limite/erro)
# ==============================================================================

async def run_single_message_with_account(acc: Dict[str, str], bot_slug: str, user_text: str) -> Tuple[str, Dict[str, bool], Optional[str]]:
    """
    Executa 1 mensagem com a conta 'acc'.
    Retorna (answer_text, flags, error_str).
      flags: {"point_limit": bool}
      error_str: uma string se houve erro fatal (auth/HTTP/GraphQL), None se ok.
    """
    session = _session_new()
    _apply_cookies_to_session(session, acc)
    # Headers dependem do modelo (referer)
    session.headers["Referer"] = f"{POE_URL_ROOT}/{bot_slug}"

    info = None
    handles = None
    try:
        # Bootstrap com os COOKIES desta conta
        cookie_dict = {
            "p-b": acc.get("p_b", ""),
            "p-lat": acc.get("p_lat", ""),
            "p-sb": acc.get("p_sb", ""),
            "cf_clearance": CF_CLEARANCE,
            "__cf_bm": CF_BM,
        }
        info, handles = await get_bootstrap(bot_slug, cookie_dict, user_text)

        # Enforce poe-tchannel (do navegador) em todas as chamadas
        tch = info.get("tchannel_hdr")
        if tch:
            session.headers["poe-tchannel"] = tch

        # (Opcional) subscriptionsMutation replay
        subs_raw   = info.get("subs_payload_raw")
        subs_hdrs  = {k.lower(): v for k, v in (info.get("subs_req_headers") or {}).items()}
        subs_cookie_hdr = info.get("subs_cookie_header") or ""
        if subs_cookie_hdr:
            parse_cookie_header_and_set(session, subs_cookie_hdr)
        if subs_raw:
            blocked = {"content-length", "host", "connection", "accept-encoding"}
            gql_headers_subs = {k: v for k, v in subs_hdrs.items() if k not in blocked}
            gql_headers_subs["content-type"] = "application/json"
            gql_headers_subs["origin"]  = POE_URL_ROOT
            gql_headers_subs["referer"] = f"{POE_URL_ROOT}/{bot_slug}"
            resp_subs = session.post(POE_API_URL, data=subs_raw, headers=gql_headers_subs)
            if resp_subs.status_code in (401, 403):
                return "", {"point_limit": False}, f"auth_{resp_subs.status_code}"
            # Não sobrescrever poe-tchannel aqui
            resp_subs.raise_for_status()

        # SEND MESSAGE replay
        send_raw   = info.get("send_payload_raw")
        send_hdrs  = {k.lower(): v for k, v in (info.get("send_req_headers") or {}).items()}
        send_cookie_hdr = info.get("send_cookie_header") or ""
        if send_cookie_hdr:
            parse_cookie_header_and_set(session, send_cookie_hdr)
        if not send_raw:
            return "", {"point_limit": False}, "no_send_payload"

        blocked = {"content-length", "host", "connection", "accept-encoding"}
        gql_headers_send = {k: v for k, v in send_hdrs.items() if k not in blocked}
        gql_headers_send["content-type"] = "application/json"
        gql_headers_send["origin"]  = POE_URL_ROOT
        gql_headers_send["referer"] = f"{POE_URL_ROOT}/{bot_slug}"
        if "poe-tchannel" in session.headers:
            gql_headers_send["poe-tchannel"] = session.headers["poe-tchannel"]

        resp = session.post(POE_API_URL, data=send_raw, headers=gql_headers_send)
        if resp.status_code in (401, 403):
            return "", {"point_limit": False}, f"auth_{resp.status_code}"
        resp.raise_for_status()
        data = resp.json()

        # Checa erros GraphQL (limite/credit/quota)
        if _has_limit_error_from_gql_json(data):
            return "", {"point_limit": True}, None

        root = data.get("data", {}) if isinstance(data, dict) else {}
        chat = None
        if "messageCreate" in root:
            chat = (root.get("messageCreate") or {}).get("chat") or {}
        elif "messageEdgeCreate" in root:
            chat = (root.get("messageEdgeCreate") or {}).get("chat") or {}
        if not chat:
            return "", {"point_limit": False}, "no_chat_in_response"

        chat_id = chat.get("chatId") or chat.get("id")
        if not chat_id:
            return "", {"point_limit": False}, "no_chat_id"

        # STREAM (WS preferencial)
        ws_url = info.get("ws_url") or ""
        es_url = info.get("es_url") or ""
        if (not es_url) and ws_url.startswith("wss://"):
            es_url = "https://" + ws_url[len("wss://"):]

        meta_headers = {}
        if tch:                 meta_headers["poe-tchannel"] = tch
        if info.get("formkey"): meta_headers["poe-formkey"]  = info.get("formkey")
        if info.get("revision"):meta_headers["poe-revision"] = info.get("revision")
        if info.get("tag_id"):  meta_headers["poe-tag-id"]   = info.get("tag_id")

        final_answer = ""
        flags = {"point_limit": False}
        if ws_url:
            print(f"Conectando via WebSocket: {ws_url}")
            ans, flags = await listen_via_websocket_url(
                ws_url,
                session,
                str(chat_id),
                bot_slug=bot_slug,
                meta_headers=meta_headers,
                initial_client_frames=info.get("ws_client_frames") or [],
                timeout_seconds=120,
            )
            final_answer = ans
            if (not final_answer) and es_url:
                print("\n[WS] Sem payload do bot. Tentando SSE…")
                ans2, flags2 = await listen_via_sse_url(es_url, session, str(chat_id), bot_slug=bot_slug, timeout_seconds=120)
                final_answer = ans2
                flags["point_limit"] = flags["point_limit"] or flags2.get("point_limit", False)
        else:
            print(f"Conectando via SSE: {es_url}")
            ans2, flags2 = await listen_via_sse_url(es_url, session, str(chat_id), bot_slug=bot_slug, timeout_seconds=120)
            final_answer = ans2
            flags["point_limit"] = flags2.get("point_limit", False)

        return final_answer, flags, None

    except requests.HTTPError as e:
        body = getattr(e.response, "text", "")[:500]
        return "", {"point_limit": False}, f"http_error:{getattr(e.response,'status_code',0)}:{body}"
    except Exception as e:
        return "", {"point_limit": False}, f"exc:{e}"
    finally:
        if handles:
            await cleanup_playwright(handles)

async def run_message_with_rotation(pool: AccountPool, bot_slug: str, user_text: str) -> str:
    """
    Envia uma mensagem, alternando entre contas se necessário (limite/erro).
    Retorna o texto final (ou vazio se todas falharem).
    """
    tried = 0
    while tried < len(pool):
        acc = pool.current()
        print(f"\n[Conta] Usando: {pool.label()}")
        answer, flags, err = await run_single_message_with_account(acc, bot_slug, user_text)
        if answer:
            return answer
        # sem resposta — avaliar motivo
        if flags.get("point_limit") or (err and ("limit" in err or "credit" in err or "quota" in err or "points" in err)):
            print("[Conta] Limite/créditos atingidos. Trocando de conta…")
            pool.rotate()
            tried += 1
            continue
        if err and err.startswith("auth_"):
            print(f"[Conta] Erro de autenticação ({err}). Trocando de conta…")
            pool.rotate()
            tried += 1
            continue
        if err and err.startswith("http_error:"):
            print(f"[Conta] HTTP error ({err}). Trocando de conta…")
            pool.rotate()
            tried += 1
            continue
        if not answer:
            # tentaremos próxima conta mesmo sem erro claro
            print(f"[Conta] Sem resposta. Trocando de conta… ({err or 'motivo desconhecido'})")
            pool.rotate()
            tried += 1
            continue
    print("[Falha] Nenhuma conta conseguiu responder.")
    return ""

# ==============================================================================
# 6) CLI — conversa interativa
# ==============================================================================

async def chat_cli():
    pool = AccountPool(ACCOUNTS)
    bot_slug = DEFAULT_MODEL_SLUG

    print("=== Poe CLI ===")
    print("Comandos: /model <slug>  |  /switch  |  /quit")
    print(f"Modelo atual: {bot_slug}  |  Conta: {pool.label()}  |  Total contas: {len(pool)}")
    while True:
        try:
            user = input("\nVocê> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nEncerrando.")
            break
        if not user:
            continue
        if user.lower() in ("/quit", "/exit"):
            print("Tchau!")
            break
        if user.startswith("/model "):
            new_slug = user.split(" ", 1)[1].strip()
            if new_slug:
                bot_slug = new_slug
                print(f"[OK] Modelo trocado para: {bot_slug}")
            else:
                print("[ERRO] Informe um slug: /model <slug>")
            continue
        if user.startswith("/switch"):
            pool.rotate()
            print(f"[OK] Trocado para conta: {pool.label()}")
            continue

        # Envia mensagem (com rotação automática de conta quando necessário)
        print(f"Bot({bot_slug})> ", end="", flush=True)
        _ = await run_message_with_rotation(pool, bot_slug, user)
        print("")  # nova linha ao final do streaming

# ==============================================================================
# 7) MAIN
# ==============================================================================

if __name__ == "__main__":
    if not ACCOUNTS or not ACCOUNTS[0].get("p_b"):
        print("Configure pelo menos um cookie p-b em ACCOUNTS[0]['p_b'].")
        sys.exit(1)
    try:
        asyncio.run(chat_cli())
    except RuntimeError as e:
        # fallback para event loop já existente (ex. no IPython)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(chat_cli())