# Poe GraphQL Sender — WS/SSE Sniffer & 1:1 Replay  
**Desenvolvido por scrowzer**

> Sniffa e reproduz (replay) o tráfego do Poe via GraphQL, escuta respostas em tempo real por **WebSocket ou SSE**, e imprime apenas os *chunks* do **bot** (não ecoa a sua mensagem).  
> Funciona sem injetar nada no backend: a automação só **captura** o que o navegador faria e **reproduz** do seu lado com os mesmos cookies/headers.

---

## 🇧🇷 Português

### ⚠️ Aviso legal
- Este código é para **fins educacionais**. Antes de usar, verifique **Termos de Serviço** do Poe e as leis aplicáveis na sua jurisdição.  
- Você é o único responsável pelo uso. **Não** abuse, não burle limitação de uso, e **não** colete dados de terceiros sem consentimento.

---

## Visão geral

**Poe GraphQL sender** abre a página do bot no navegador (Playwright) já **logado** com seus cookies, intercepta:

1) `sendMessageMutation` → captura **headers + body** e **finge** o 200 (não envia pelo browser).  
2) `subscriptionsMutation` → captura **headers + body** e **deixa passar**.  
3) URL do canal de **stream**: `…/updates?channel=…` (pode ser **WS** ou **SSE**).  
4) (Opcional) **Frames de handshake** que o cliente envia no WS (via hook em `WebSocket.prototype.send`).

Depois, a app faz o **replay 1:1** com `requests` (GraphQL) usando os **mesmos cookies/headers** capturados, obtém o `chatId` e conecta direto no mesmo canal sniffado (**sem** depender do Playwright para stream).  
O listener decodifica envelopes do servidor (algumas instâncias enviam `{"messages":["…json…"]}`) e imprime **somente** os pedaços da **resposta do bot**; mensagens do **usuário** são ignoradas.

---

## Principais recursos

- 🧠 **Replay fiel**: reutiliza cookies/headers (`poe-formkey`, `poe-revision`, `poe-tag-id`, `poe-tchannel`, `Cookie`) do seu próprio navegador.  
- 🔎 **Sniffer WS/SSE**: captura conexões abertas por **page, iframes ou workers**.  
- 🔄 **SSE fallback**: se não houver WS, escuta via **EventSource**.  
- 🧰 **Compatível com envelopes**: decodifica `messages[]` com `message_type`.  
- 🧵 **Filtro de autor**: só imprime **mensagens do bot** (heurísticas robustas).  
- 🧯 **Diagnóstico e timeouts**: logs úteis + travas de tempo para não “pendurar”.  
- 🧪 **Handshake opcional**: reenvia frames que o cliente nativo mandou no WS (quando existirem).  
- 🧷 **Playwright mantido vivo**: evita perder assinatura do canal durante o stream.

---

## Requisitos

- **Python 3.9+** (recomendado 3.10/3.11)  
- **Chromium** via Playwright  
- Pacotes Python:
  - `playwright`
  - `requests`
  - `websockets>=11,<13`  ← **atenção ao pacote certo** (`websockets`, no plural)
  - (Opcional) `aiohttp` se quiser WS alternativo

Instalação:

```bash
python -m pip install -U "websockets>=11,<13" requests playwright
python -m playwright install chromium
```

> Se tiver instalado **websocket-client** (singular) ou **websocket** por engano, remova:
> ```bash
> python -m pip uninstall -y websocket websocket-client
> ```

---

## Configuração (cookies)

Edite no topo do arquivo:

- `P_B_COOKIE` (**obrigatório**): cookie de sessão `p-b` do Poe.  
- `P_LAT_COOKIE`, `CF_CLEARANCE`, `__cf_bm`, `p-sb` (**opcionais**): úteis se o site exigir Cloudflare/variações de sessão.

> **Dica**: copie os cookies do navegador para estes campos. O domínio deve ser `poe.com`.

---

## Como funciona (deep dive)

1) **Bootstrap (Playwright)**
   - Abre `https://poe.com/<BOT_NAME>` com cookies carregados.
   - Injeta _init scripts_ que:
     - interceptam `new WebSocket(url)` e `new EventSource(url)`;
     - **guardam** as últimas URLs (`__POE_LAST_WS_URL__`, `__POE_LAST_ES_URL__`);
     - envolvem `WebSocket.prototype.send` para **capturar** frames enviados pelo cliente.
   - Roteia `**/api/gql_POST` no **context** (pega também chamadas de workers) e:
     - `sendMessageMutation`: **captura**, **não envia** (fulfill 200).
     - `subscriptionsMutation`: **captura** e **deixa passar**.

2) **Replays (requests)**
   - `subscriptionsMutation` (opcional) → registra no canal, usando **mesmos headers**.  
   - `sendMessageMutation` → cria a mensagem e extrai o `chatId`.

3) **Stream (WS/SSE)**
   - Conecta exatamente na URL sniffada `…/updates?channel=…`.
   - Envia, se existirem, **frames iniciais** capturados do cliente (handshake).  
   - Lê **envelopes** (quando vierem), normaliza `subscriptionUpdate` e filtra nós:  
     `messageTextUpdated`, `messageAdded`, `messageUpdated`, `messageCreated`, `messageStateUpdated`.  
   - **Mostra apenas o texto do bot**, concatenando os *chunks* até `state=complete`.

4) **Playwright vivo até o fim**  
   - Só encerra `context/browser` depois que o stream terminar.

---

## Execução

1) Ajuste `P_B_COOKIE` e, se necessário, os demais cookies.  
2) Ajuste `BOT_NAME` (slug) e `PROMPT` (mensagem).  
3) Rode:

```bash
python poe.py
```

**Exemplo de saída:**

```
Registrando subscriptions via subscriptionsMutation (replay 1:1)…
Enviando a pergunta via GraphQL com payload/headers CAPTURADOS…
Chat criado! ID: 1249xxxxxx
Conectando via WebSocket: wss://tch….poe.com/up/.../updates?channel=poe-chan...
[WS] Conectando…
[WS] Conectado. Enviando frames de handshake do cliente (se houver)…
[WS] Aguardando eventos…
Oi! Sou o <bot> do Poe… (chunks)
--- Resposta Completa (WS) ---

RESPOSTA FINAL:
Oi! Sou o <bot> do Poe…
```

---

## Estrutura do projeto

```
poe.py                 # script principal (playwright + gql replay + ws/sse listener)
README.md              # este arquivo
```

---

## Troubleshooting

### 1) `BaseEventLoop.create_connection() got an unexpected keyword argument 'extra_headers'`
Você instalou o pacote errado (`websocket-client`) ou versão antiquada do `websockets`.

- Remova conflitos:
  ```bash
  python -m pip uninstall -y websocket websocket-client
  ```
- Instale o certo (plural) e recente:
  ```bash
  python -m pip install -U "websockets>=11,<13"
  ```

### 2) Conecta, mas não chega nada (WS mudo)
Causas comuns:
- **Playwright fechado cedo** → o canal perde a assinatura.  
  ✔️ **Solução**: não fechar o Playwright até o fim (já feito neste código).

- `poe-tchannel` divergente entre navegador e replay.  
  ✔️ **Solução**: forçar `session.headers["poe-tchannel"]` = o capturado no navegador (já feito).

- O Poe usa **SSE** em vez de WS.  
  ✔️ **Solução**: o sniffer captura EventSource; há fallback de **SSE**.

### 3) “mtype=unknown” no log
Alguns servidores enviam envelopes:
```json
{"min_seq":..., "messages":["{\"message_type\":\"subscriptionUpdate\",\"payload\":{...}}"]}
```
✔️ **Solução**: o listener atual já desenvelopa `messages[]` e lê `message_type`.

### 4) Só aparece a minha frase; não a resposta do bot
Você estava imprimindo eventos do usuário (eco).  
✔️ **Solução**: o código agora filtra apenas **mensagens do bot**.

### 5) Cloudflare bloqueando
Use `CF_CLEARANCE` e `__cf_bm` atuais do seu navegador. Cookies desatualizados podem falhar.

### 6) “Não capturei WS nem SSE”
Algumas páginas atrasam a conexão. Tente:
- Aumentar a janela de espera no bootstrap;  
- Garantir que o prompt foi digitado e **Enter** pressionado.

---

## Dicas/Notas

- **Headers capturados**: `poe-formkey`, `poe-revision`, `poe-tchannel`, `poe-tag-id`, `Cookie`.  
- **Não** reescreva o payload: reenvie **byte a byte** (`send_raw`, `subs_raw`).  
- O filtro do bot usa heurísticas (autor/handle, `__typename`, `type/role`, presença de `bot{handle}`, `suggestedReplies}`, estados de geração etc.).

---

## Roadmap (sugestões)

- CLI com subcomandos (`send`, `stream`, `sse-only`, `debug`).  
- Exportar em **NDJSON**/Markdown.  
- Modo “headful” para inspeção visual.  
- Suporte explícito a **aiohttp** como cliente WS alternativo.

---

## Contribuindo

1) Faça um fork e crie uma branch: `feat/minha-feature`.  
2) Siga PEP8 e mantenha o estilo atual.  
3) Abra um PR com descrição clara do que foi feito.

---

## Licença



```
MIT License
Copyright (c) …

Permission is hereby granted, free of charge, to any person obtaining a copy…
```

---

## Créditos

**Desenvolvido por scrowzer.**

---

---

## 🇺🇸 English

### ⚠️ Legal notice
- This code is for **educational purposes**. Check the Poe **Terms of Service** and applicable laws before using it.  
- You are solely responsible for usage. Do **not** abuse it, bypass usage limits, or collect third-party data without consent.

---

## Overview

**Poe GraphQL sender** opens the bot page in a logged-in Playwright browser and intercepts:

1) `sendMessageMutation` → captures **headers + body** and **fakes** a 200 (so the browser doesn’t send it).  
2) `subscriptionsMutation` → captures **headers + body** and **lets it pass**.  
3) The real-time channel URL `…/updates?channel=…` (could be **WS** or **SSE**).  
4) (Optional) Client-side **WebSocket handshake frames** via a hook on `WebSocket.prototype.send`.

Then it **replays** those GraphQL requests with `requests`, obtains the `chatId`, and connects directly to the sniffed stream (WS/SSE).  
The listener decodes server envelopes (some instances send `{"messages":["…json…"]}`) and prints **only** the **bot** chunks; user messages are ignored.

---

## Key features

- 🧠 **1:1 replay** using your own cookies/headers (`poe-formkey`, `poe-revision`, `poe-tag-id`, `poe-tchannel`, `Cookie`).  
- 🔎 **WS/SSE sniffer** across page/frames/workers.  
- 🔄 **SSE fallback** when no WS is used.  
- 🧰 **Envelope-aware** stream decoder (`messages[]` + `message_type`).  
- 🧵 **Bot-only filter**: prints assistant chunks only.  
- 🧯 **Diagnostics + timeouts** to avoid hanging.  
- 🧪 **Optional handshake replay** for WS frames the native client sent.  
- 🧷 **Keeps Playwright alive** until streaming is done.

---

## Requirements

- **Python 3.9+**  
- **Chromium** via Playwright  
- Python packages:
  - `playwright`
  - `requests`
  - `websockets>=11,<13`
  - (Optional) `aiohttp` if you prefer that WS client

Install:

```bash
python -m pip install -U "websockets>=11,<13" requests playwright
python -m playwright install chromium
```

> If you accidentally installed **websocket-client** or **websocket** (singular), uninstall them:
> ```bash
> python -m pip uninstall -y websocket websocket-client
> ```

---

## Configuration (cookies)

Edit at the top:

- `P_B_COOKIE` (**required**): your Poe `p-b` session cookie.  
- `P_LAT_COOKIE`, `CF_CLEARANCE`, `__cf_bm`, `p-sb` (**optional**): helpful when Cloudflare/session layers are active.

---

## How it works (deep dive)

1) **Bootstrap (Playwright)**
   - Opens `https://poe.com/<BOT_NAME>` with your cookies.
   - Init scripts:
     - intercept `new WebSocket(url)` and `new EventSource(url)` to **record** URLs;
     - wrap `WebSocket.prototype.send` to **capture** client-sent frames.
   - Routes `**/api/gql_POST` at the **context** level:
     - `sendMessageMutation`: **capture** and **fulfill 200**.
     - `subscriptionsMutation`: **capture** and **continue**.

2) **Replays (requests)**
   - (Optional) `subscriptionsMutation` → registers on the channel with the **same headers**.  
   - `sendMessageMutation` → creates the message and returns the `chatId`.

3) **Stream (WS/SSE)**
   - Connects to the exact sniffed `updates?channel=…` URL.
   - Sends captured **handshake frames** if any.
   - Decodes **envelopes** (when present), normalizes `subscriptionUpdate`, extracts nodes, and prints **only bot chunks** until `state=complete`.

4) **Playwright kept alive**  
   - Close `context/browser` only after streaming ends.

---

## Run

1) Fill `P_B_COOKIE` (and optional cookies).  
2) Set `BOT_NAME` and `PROMPT`.  
3) Run:

```bash
python poe.py
```

**Sample output:**

```
Registering subscriptions via subscriptionsMutation (1:1 replay)…
Sending question via GraphQL with CAPTURED payload/headers…
Chat created! ID: 1249xxxxxx
Connecting via WebSocket: wss://tch….poe.com/up/.../updates?channel=poe-chan...
[WS] Connecting…
[WS] Connected. Sending client handshake frames (if any)…
[WS] Waiting for events…
Hi! I’m the Poe <bot>… (chunks)
--- Resposta Completa (WS) ---

FINAL ANSWER:
Hi! I’m the Poe <bot>…
```

---

## Project structure

```
poe.py        # main script (playwright + gql replay + ws/sse listener)
README.md     # this file
```

---

## Troubleshooting

- **`BaseEventLoop.create_connection() ... 'extra_headers'`** → wrong package (`websocket-client`) or outdated.  
  → Uninstall conflicting libs; install `websockets>=11,<13`.

- **WS connects but no events** → channel unsubscribed (closed Playwright too early) or mismatched `poe-tchannel`.  
  → Keep Playwright alive; force the captured `poe-tchannel` in all replays and WS headers.

- **`mtype=unknown` logs** → server envelope `{"messages":["…json…"]}`.  
  → Current listener already unwraps and handles `message_type`.

- **Only your own text shows up** → user echo.  
  → Current code filters **bot** messages only.

- **Cloudflare challenges** → copy fresh `CF_CLEARANCE` and `__cf_bm`.

---

## Roadmap

- CLI subcommands; structured outputs (NDJSON/Markdown); headful debug; optional `aiohttp` WS client.

---

## Contributing

1) Fork + feature branch: `feat/your-feature`.  
2) Follow PEP8 and current style.  
3) Open a PR describing your changes.

---

## License

MIT

---

## Credits

**Developed by scrowzer.**
