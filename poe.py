
import asyncio
import json
from typing import Optional, Dict, Any, List, Tuple

import requests
import websockets
from playwright.async_api import async_playwright

# ==============================================================================
# 0) CONFIGURATIONS AND COOKIES
# ==============================================================================
P_B_COOKIE   = ""   # obrigatório (cookie de sessão p-b)
P_LAT_COOKIE = ""                                # opcional
CF_CLEARANCE = ""                                # opcional (Cloudflare)
CF_BM        = ""                                # opcional
P_SB         = ""                                # opcional

BOT_NAME = "GPT-5"  # slug/handle visível na URL
PROMPT   = "Hello World"  # mensagem a enviar

POE_URL_ROOT = "https://poe.com"
POE_API_URL  = f"{POE_URL_ROOT}/api/gql_POST"

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0",
    "Accept": "*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Origin": POE_URL_ROOT,
    "Referer": f"{POE_URL_ROOT}/{BOT_NAME}",
    "Sec-Ch-Ua": '"Not;A=Brand";v="99", "Microsoft Edge";v="139", "Chromium";v="139"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Content-Type": "application/json",
    "poegraphql": "0",
}

# ============================== UTILS =========================================

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

# ============================== PARSERS ========================================

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

def is_bot_message(msg: Dict[str, Any]) -> bool:
    """Heurística robusta p/ diferenciar bot vs usuário."""
    # 1) author
    a = msg.get("author")
    if isinstance(a, dict):
        handle = str(a.get("handle") or "").lower()
        if handle and handle == str(BOT_NAME).lower():
            return True
        if a.get("__typename") in ("Bot", "PoeBot", "DefaultBot"):
            return True
        t = str(a.get("type") or a.get("role") or "").lower()
        if t in ("bot", "assistant", "ai"):
            return True
        if a.get("isBot") is True:
            return True

    # 2) "bot" 
    b = msg.get("bot")
    if isinstance(b, dict):
        h = str(b.get("handle") or "").lower()
        if h and h == str(BOT_NAME).lower():
            return True
        
        return True

    
    mt = str(msg.get("messageType") or msg.get("type") or "").lower()
    if mt in ("bot", "assistant", "ai"):
        return True

    
    if "suggestedReplies" in msg:
        return True
    if ("text_new" in msg or "partial_response" in msg or "delta" in msg):
        st = str(msg.get("state") or "").lower()
        if st in ("incomplete", "generating", "in_progress", "streaming", "pending", ""):
            return True

    return False

# ============================== BOOTSTRAP ======================================

async def get_bootstrap(bot_name: str, cookie_dict: Dict[str, str], prompt_for_capture: str) -> Tuple[Dict[str, Optional[str]], Dict[str, Any]]:
    """Abre a página logada, intercepta send/subs, sniffa WS/SSE e captura frames enviados pelo cliente. NÃO fecha o Playwright aqui."""
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context()

    # session cookies
    add = []
    for name in ("p-b", "p-lat", "cf_clearance", "__cf_bm", "p-sb"):
        val = cookie_dict.get(name)
        if val:
            add.append({"name": name, "value": val, "domain": "poe.com", "path": "/", "secure": True})
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

    # typing the prompt
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

    # waiting for the capture
    for _ in range(120):
        got_send = bool(captured["send_payload_raw"])
        got_subs = bool(captured["subs_payload_raw"])
        got_stream = bool(captured["ws_url"] or captured["es_url"])
        if got_send and (got_subs or got_stream):
            break
        await page.wait_for_timeout(200)

    # Hooks JS + frames sent by the client
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

# ============================== STREAM LISTENERS ================================

async def listen_via_websocket_url(ws_url: str,
                                   session: requests.Session,
                                   target_chat_id: Optional[str],
                                   meta_headers: Dict[str, str],
                                   initial_client_frames: List[str],
                                   timeout_seconds: int = 120) -> str:
    """Conecta no ws_url, envia frames iniciais (se houver), desenvelopa 'messages[]' e ESCUTA APENAS O BOT."""
    def _iter_inner_frames(packet: Any):
        # packet can be str/dict; send with "messages": [ "<json>", ... ]
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
    print("[WS] Conectando…")
    async with websockets.connect(ws_url, extra_headers=extra, max_size=None) as ws:
        print("[WS] Conectado. Enviando frames de handshake do cliente (se houver)…")
        for f in initial_client_frames or []:
            try:
                await ws.send(f)
                print(f"[WS] >> {f[:80] + ('…' if len(f) > 80 else '')}")
            except Exception:
                pass

        print("[WS] Aguardando eventos…")
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                print("\n[WS] Timeout sem eventos. Verifique tchannel/channel.")
                break

            try:
                message = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                print("\n[WS] Timeout sem eventos. Verifique tchannel/channel.")
                break

            got_update = False
            # unwrap and process
            try:
                candidates = list(_iter_inner_frames(message))
            except Exception:
                candidates = []
            for inner in candidates:
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

                        # >>>>>>> filter: only bot messages
                        if not is_bot_message(msg):
                            # first time, log light (key to identify author)
                            # (not too verbose)
                            continue
                        # <<<<<<<

                        pieces = extract_text_pieces(msg)
                        if pieces:
                            chunk = "".join(pieces)
                            print(chunk, end="", flush=True)
                            full += chunk

                        if looks_complete(msg) or looks_complete(node):
                            print("\n--- Resposta Completa (WS) ---")
                            return full

            if got_update:
                deadline = asyncio.get_event_loop().time() + timeout_seconds

    return full

def _build_stream_headers(session: requests.Session) -> Dict[str, str]:
    return {
        "Origin": "https://poe.com",
        "Referer": f"{POE_URL_ROOT}/{BOT_NAME}",
        "User-Agent": BASE_HEADERS["User-Agent"],
        "Accept-Language": BASE_HEADERS.get("Accept-Language", "en-US,en;q=0.9"),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Cookie": cookie_header_from_jar(session.cookies),
    }

def _matches_chat_and_chunk(payload: Dict[str, Any], target_chat_id: Optional[str]) -> Tuple[bool, str, Dict[str, Any]]:
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
                if not is_bot_message(msg):
                    continue
                pieces = extract_text_pieces(msg)
                return True, ("".join(pieces) if pieces else ""), msg
    except Exception:
        pass
    return False, "", {}

async def listen_via_sse_url(es_url: str, session: requests.Session, target_chat_id: Optional[str], timeout_seconds: int = 120) -> str:
    """Conecta via SSE no es_url e ESCUTA (apenas BOT)."""
    headers = _build_stream_headers(session)
    headers["Accept"] = "text/event-stream"
    full = ""
    with session.get(es_url, headers=headers, stream=True) as r:
        r.raise_for_status()
        import time
        end_at = time.time() + timeout_seconds
        for raw in r.iter_lines(decode_unicode=True):
            if time.time() > end_at:
                print("\n[SSE] Timeout sem eventos. Verifique tchannel/channel.")
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
            ok, chunk, msg = _matches_chat_and_chunk(payload, target_chat_id)
            if ok and chunk:
                print(chunk, end="", flush=True)
                full += chunk
            if msg and looks_complete(msg):
                print("\n--- Resposta Completa (SSE) ---")
                break
    return full

# ============================== DIAGNOSTICS ====================================

def summarize_payload(payload_base: Dict[str, Any]) -> str:
    vars_obj = (payload_base or {}).get("variables") or {}
    exts_obj = (payload_base or {}).get("extensions") or {}
    qn = (payload_base or {}).get("queryName")
    sdid = vars_obj.get("sdid")
    bot  = vars_obj.get("bot")
    h    = exts_obj.get("hash")
    return f"qn:{qn} bot:{str(bot)[:12]}… sdid:{str(sdid)[:12]}… ext_hash:{str(h)[:8]}…"

# ================================= MAIN ========================================

async def main():
    session = requests.Session()
    session.headers.update(BASE_HEADERS)

    # Base cookies
    session.cookies.update({"p-b": P_B_COOKIE})
    if P_LAT_COOKIE:
        session.cookies.set("p-lat", P_LAT_COOKIE, domain="poe.com", path="/")
    if CF_CLEARANCE:
        session.cookies.set("cf_clearance", CF_CLEARANCE, domain="poe.com", path="/")
    if CF_BM:
        session.cookies.set("__cf_bm", CF_BM, domain="poe.com", path="/")
    if P_SB:
        session.cookies.set("p-sb", P_SB, domain="poe.com", path="/")

    info = None
    handles = None
    try:
        info, handles = await get_bootstrap(
            BOT_NAME,
            {"p-b": P_B_COOKIE, "p-lat": P_LAT_COOKIE, "cf_clearance": CF_CLEARANCE, "__cf_bm": CF_BM, "p-sb": P_SB},
            PROMPT,
        )

        # Always use the tchannel from the browser
        tch = info.get("tchannel_hdr")
        if tch:
            session.headers["poe-tchannel"] = tch

        # (Optional) subscriptionsMutation replay
        subs_raw   = info.get("subs_payload_raw")
        subs_hdrs  = {k.lower(): v for k, v in (info.get("subs_req_headers") or {}).items()}
        subs_cookie_hdr = info.get("subs_cookie_header") or ""
        if subs_cookie_hdr:
            parse_cookie_header_and_set(session, subs_cookie_hdr)
        if subs_raw:
            print("Registrando subscriptions via subscriptionsMutation (replay 1:1)…")
            blocked = {"content-length", "host", "connection", "accept-encoding"}
            gql_headers_subs = {k: v for k, v in subs_hdrs.items() if k not in blocked}
            gql_headers_subs["content-type"] = "application/json"
            gql_headers_subs["origin"]  = POE_URL_ROOT
            gql_headers_subs["referer"] = f"{POE_URL_ROOT}/{BOT_NAME}"
            resp_subs = session.post(POE_API_URL, data=subs_raw, headers=gql_headers_subs)
            resp_subs.raise_for_status()
        else:
            print("Aviso: não capturei subscriptionsMutation; seguirei com stream apenas (pode já estar registrado).")

        # =========== SEND MESSAGE (replay 1:1) ===========
        send_raw   = info.get("send_payload_raw")
        send_hdrs  = {k.lower(): v for k, v in (info.get("send_req_headers") or {}).items()}
        send_cookie_hdr = info.get("send_cookie_header") or ""
        if send_cookie_hdr:
            parse_cookie_header_and_set(session, send_cookie_hdr)
        if not send_raw:
            raise RuntimeError("Não capturei a sendMessageMutation (body RAW).")

        print("Enviando a pergunta via GraphQL com payload/headers CAPTURADOS…")
        blocked = {"content-length", "host", "connection", "accept-encoding"}
        gql_headers_send = {k: v for k, v in send_hdrs.items() if k not in blocked}
        gql_headers_send["content-type"] = "application/json"
        gql_headers_send["origin"]  = POE_URL_ROOT
        gql_headers_send["referer"] = f"{POE_URL_ROOT}/{BOT_NAME}"
        if "poe-tchannel" in session.headers:
            gql_headers_send["poe-tchannel"] = session.headers["poe-tchannel"]
        resp = session.post(POE_API_URL, data=send_raw, headers=gql_headers_send)
        resp.raise_for_status()
        data = resp.json()

        root = data.get("data", {}) if isinstance(data, dict) else {}
        chat = None
        if "messageCreate" in root:
            chat = (root.get("messageCreate") or {}).get("chat") or {}
        elif "messageEdgeCreate" in root:
            chat = (root.get("messageEdgeCreate") or {}).get("chat") or {}
        if not chat:
            raise RuntimeError("Resposta sem objeto 'chat' esperado (messageCreate/messageEdgeCreate).")

        chat_id = chat.get("chatId") or chat.get("id")
        if not chat_id:
            raise RuntimeError("Não consegui obter o chatId.")
        print(f"Chat criado! ID: {chat_id}")

        # =========== STREAM LISTEN ===========
        ws_url = info.get("ws_url") or ""
        es_url = info.get("es_url") or ""
        if (not es_url) and ws_url.startswith("wss://"):
            es_url = "https://" + ws_url[len("wss://"):]
        if not (ws_url or es_url):
            raise RuntimeError("Não capturei WS *nem* SSE (updates?channel=…). Revise os hooks.")

        meta_headers = {}
        if tch:                 meta_headers["poe-tchannel"] = tch
        if info.get("formkey"): meta_headers["poe-formkey"]  = info.get("formkey")
        if info.get("revision"):meta_headers["poe-revision"] = info.get("revision")
        if info.get("tag_id"):  meta_headers["poe-tag-id"]   = info.get("tag_id")

        if ws_url:
            print(f"Conectando via WebSocket: {ws_url}")
            final_answer = await listen_via_websocket_url(
                ws_url,
                session,
                str(chat_id),
                meta_headers=meta_headers,
                initial_client_frames=info.get("ws_client_frames") or [],
                timeout_seconds=120,
            )
            if not final_answer and es_url:
                print("\n[WS] Sem payload do bot. Tentando SSE…")
                final_answer = await listen_via_sse_url(es_url, session, str(chat_id), timeout_seconds=120)
        else:
            print(f"Conectando via SSE: {es_url}")
            final_answer = await listen_via_sse_url(es_url, session, str(chat_id), timeout_seconds=120)

        print("\nRESPOSTA FINAL:")
        print(final_answer if final_answer else "(vazio)")

    except requests.HTTPError as e:
        body = getattr(e.response, "text", "")[:2000]
        print(f"HTTPError: {e}\nBody: {body}")
    except Exception as e:
        print(f"Erro: {e}")
    finally:
        if handles:
            await cleanup_playwright(handles)

if __name__ == "__main__":
    asyncio.run(main())