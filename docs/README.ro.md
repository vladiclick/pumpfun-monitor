# PUMP MONITOR

Monitorizare în timp real a lansărilor de token-uri pe **pump.fun** (Solana), cu
pipeline anti-rug fail-fast și dashboard live în stil terminal.

**Costă 0 lei.** Nicio dependență, niciun API și niciun serviciu din proiect nu
cere card sau nu se transformă în abonament. Totul rulează local.

```
┌──────────────┐   logsSubscribe    ┌─────────────┐   HTTP ingest   ┌──────────┐   WebSocket   ┌───────────┐
│  Solana RPC  │ ─────────────────▶ │  bot (proc) │ ──────────────▶ │ FastAPI  │ ────────────▶ │ dashboard │
└──────────────┘                    └─────────────┘                 │ + SQLite │               └───────────┘
                                    (una per bot_id)                └──────────┘
```

---

## 1. Ce face, pe scurt

1. Se abonează pe WebSocket la logurile programului pump.fun
   (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`).
2. Detectează instrucțiunea de creare (`Create` / `CreateV2`) și extrage
   **direct din loguri** mint-ul, numele și simbolul — fără niciun apel RPC.
3. Rulează un pipeline anti-rug **fail-fast** (se oprește la primul eșec).
4. Trimite fiecare eveniment către FastAPI, care îl salvează în SQLite și îl
   împinge live către dashboard.

### Pipeline anti-rug — ordinea contează

| # | Etapă | Sursă | Cost | Timp tipic |
|---|-------|-------|------|-----------|
| a | **Mint Authority + Freeze Authority** | `getAccountInfo` on-chain | gratis, fără limită externă | 40–300 ms |
| b | **Lichiditate** | DexScreener | gratis, fără cheie | 0.2 s sau timeout configurat |
| c | **RugCheck** (a doua opinie) | api.rugcheck.xyz | gratis, fără cheie | 0.3–1 s |

Ordinea nu e întâmplătoare: verificarea on-chain e cea mai rapidă și cea mai
sigură, deci elimină gratuit majoritatea gunoiului înainte să atingem API-uri
externe cu rate limit. Dacă etapa (a) pică, (b) și (c) **nu se mai apelează deloc**.

### Verdicte

| Verdict | Ce înseamnă |
|---------|-------------|
| `PASS` | a trecut toate etapele active |
| `FAIL` | respins de un filtru (autoritate activă, lichiditate sub prag, risc RugCheck) |
| `PENDING` | token prea nou — nu există încă pereche indexată pe DexScreener |
| `ERROR` | nu s-a putut decide (RPC indisponibil, coadă aglomerată, parse eșuat) |

`ERROR` nu e același lucru cu `FAIL`: un token nu e acuzat pentru că nodul RPC
a avut o problemă.

---

## 2. Cerințe

- **Python 3.11+** (testat pe 3.11; `solders` are wheel-uri până la 3.13)
- **Node 18+** (testat pe Node 22)
- Conexiune la internet. Atât.

---

## 3. Instalare

```bash
cd pumpfun-monitor

# Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # editează INGEST_TOKEN
cd ..

# Frontend
cd frontend
npm install
cd ..
```

---

## 4. Pornire

### Varianta rapidă

```bash
./start.sh
```

Pornește backend-ul pe `:8000` și dashboard-ul pe `:5173`. `Ctrl+C` le oprește pe amândouă.

### Manual (două terminale)

```bash
# Terminal 1 — backend
cd backend
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 — dashboard
cd frontend
npm run dev
```

Deschide **http://127.0.0.1:5173**.

### Un singur port (fără Vite)

Dacă faci build de frontend, backend-ul îl servește singur pe `:8000`:

```bash
cd frontend && npm run build
# apoi doar backend-ul; dashboardul e pe http://127.0.0.1:8000
```

### Bot separat, fără dashboard

```bash
cd backend
.venv/bin/python -m bot.runner --config configs/rapid.json
```

---

## 5. Configurare

### `.env` (global)

| Variabilă | Implicit | Rol |
|-----------|----------|-----|
| `SOLANA_RPC_HTTP` | `https://api.mainnet-beta.solana.com` | endpoint pentru `getAccountInfo` / `getTransaction` |
| `SOLANA_RPC_WS` | dedus din HTTP | endpoint `logsSubscribe`; setează-l explicit dacă providerul are host separat |
| `BACKEND_URL` | `http://127.0.0.1:8000` | unde trimit boții evenimentele |
| `INGEST_TOKEN` | — | secret partajat bot ↔ backend. **Schimbă-l** înainte să expui public |
| `DB_PATH` | `data/events.db` | fișierul SQLite |
| `LOG_LEVEL` | `INFO` | `DEBUG` arată și retry-urile RPC |
| `DASHBOARD_TOKEN` | gol | gol = dashboard deschis (doar local). Setat = REST-ul și WebSocket-ul îl cer. **Obligatoriu înainte de expunere publică** |
| `CORS_ORIGINS` | `*` | restrânge-l dacă expui public |
| `TELEGRAM_BOT_TOKEN` | gol | token de bot Telegram (de la @BotFather); gol = notificările sunt complet dezactivate |
| `TELEGRAM_CHAT_ID` | gol | chatul unde ajung notificările; dacă lipsește, botul îl descoperă singur la pornire, îl afișează în log și se oprește — detalii mai jos |

#### RPC public vs. Helius free

RPC-ul public `api.mainnet-beta.solana.com` funcționează, dar are rate limit
agresiv și noduri desincronizate. Pentru rulare serioasă, **Helius free tier**
(fără card) e semnificativ mai bun:

```env
SOLANA_RPC_HTTP=https://mainnet.helius-rpc.com/?api-key=CHEIA_TA
SOLANA_RPC_WS=wss://mainnet.helius-rpc.com/?api-key=CHEIA_TA
```

Cheia din URL e mascată automat în loguri și în dashboard.

#### Notificări Telegram

Opțional — dacă `TELEGRAM_BOT_TOKEN` rămâne gol, funcția e complet
dezactivată, fără erori, nimic nu se schimbă. Setup, pas cu pas:

1. Deschide Telegram, caută `@BotFather` și trimite-i `/newbot`. Alege un
   nume și un username care se termină în `bot`. BotFather răspunde cu tokenul.
2. Pune tokenul în `backend/.env`:
   ```env
   TELEGRAM_BOT_TOKEN=TOKENUL_TAU
   ```
3. **Trimite cel puțin un mesaj botului tău** (deschide-i chatul din Telegram
   și scrie orice, de exemplu `salut`). E obligatoriu: Telegram nu lasă un bot
   să scrie primul cuiva care nu i-a scris niciodată, iar `getUpdates`
   întoarce doar chat-urile din care botul a primit deja ceva. Ăsta e pasul pe
   care toată lumea îl uită.
4. Pornește botul normal. Cum `TELEGRAM_CHAT_ID` e încă gol, botul caută
   singur chat id-ul, îl afișează în log și se oprește:
   ```
   14:32:07 | INFO | bot.runner | TELEGRAM_BOT_TOKEN e setat, dar TELEGRAM_CHAT_ID lipseste. Il caut...

   ==========================================================================
     Am gasit chat-urile de mai jos. Copiaza id-ul in backend/.env:

         TELEGRAM_CHAT_ID=987654321          <- vladi (private)
         TELEGRAM_CHAT_ID=-1001234567890     <- Alerte memecoin (supergroup)

     Apoi porneste botul din nou. Pana atunci nu trimit nimic.
   ==========================================================================
   ```
   Dacă botul nu a primit încă niciun mesaj, în locul listei apare o casetă
   care îți spune exact asta și te trimite înapoi la pasul 3.
5. Copiază id-ul afișat în `.env` ca `TELEGRAM_CHAT_ID=...` și pornește botul
   din nou. Gata.

Notificarea se trimite doar pentru verdictul `PASS` — niciodată pentru `FAIL`,
`PENDING` sau `ERROR`. Mesajul conține numele și simbolul tokenului, adresa
completă a mint-ului, un link pump.fun, timpul până la verdict și `bot_id`-ul
instanței care l-a găsit. Trimiterea e fire-and-forget cu timeout, exact ca
apelurile către DexScreener și RugCheck, așa că o pană sau un rate limit pe
Telegram nu poate încetini sau bloca botul.

Pentru un grup: adaugă botul în grup, scrie un mesaj acolo, iar pasul 4 de mai
sus îți arată id-ul grupului (un număr negativ).

### `configs/*.json` (una per instanță)

Fiecare fișier din `backend/configs/` = o instanță cu `bot_id` unic și praguri
proprii. Cele livrate:

| Fișier | Profil |
|--------|--------|
| `elita.json` | **singura care trimite pe Telegram.** Cere tracțiune reală (volum + fdv) după ~200 s. Lasă să treacă ~1 din 20 |
| `rapid.json` | doar filtrul on-chain — verdict în **47–300 ms**, zero API-uri externe |
| `echilibrat.json` | on-chain + lichiditate + RugCheck — **~2.2 s**, respinge ~25% |
| `strict.json` | lichiditate reală ≥ $5000 și RugCheck fail-closed — cel mai lent, cel mai selectiv |

Opțiuni importante:

| Cheie | Implicit | Ce face |
|-------|----------|---------|
| `require_mint_authority_revoked` | `true` | respinge token-urile unde creatorul mai poate emite |
| `require_freeze_authority_revoked` | `true` | respinge token-urile care pot fi înghețate |
| `authority_retries` / `authority_retry_delay` | `5` / `0.4` | contul de mint apare cu câteva sute de ms întârziere — reîncercăm |
| `authority_commitment` | `processed` | `confirmed` e mai sigur, dar pierzi ~1 s |
| `check_liquidity` | `true` | activează etapa DexScreener |
| `min_liquidity_usd` | `1000` | pragul de lichiditate |
| `min_volume_h24_usd` | `0` | volum minim de tranzacționare (`0` = oprit). **Cel mai bun filtru de calitate** — vezi nota de mai jos |
| `min_fdv_usd` | `0` | capitalizare minimă (`0` = oprit) |
| `telegram_notify` | `true` | trimite alerte Telegram pentru această instanță |
| `liquidity_wait_seconds` | `8` | cât așteptăm indexarea pe DexScreener |
| `liquidity_poll_interval` | `4` | la ce interval reinterogăm în fereastra de mai sus |
| `allow_missing_liquidity` | `true` | vezi nota de mai jos — cu `false` aproape orice lansare nouă iese `PENDING` |
| `check_rugcheck` | `true` | activează a doua opinie |
| `max_rugcheck_score` | `2500` | scor mai mare = mai riscant |
| `rugcheck_fail_open` | `true` | dacă RugCheck e down: `true` trece, `false` respinge |
| `rugcheck_wait_seconds` | `0` | cât așteptăm ca RugCheck să indexeze tokenul (`0` = o singură încercare) |
| `rugcheck_poll_interval` | `3` | la ce interval reinterogăm în fereastra de mai sus |
| `max_concurrent_checks` | `24` | verificări în paralel (muncă I/O-bound) |
| `rpc_min_interval` | `0.5` | pauză minimă între apeluri RPC. **Per bot** — N boți pe același RPC = N× încărcarea |
| `rpc_max_retries` | `3` | reîncercări per apel RPC |
| `http_timeout` | `10` | timeout pentru RPC și API-uri externe |
| `ws_backoff_initial` / `ws_backoff_max` | `1` / `60` | reconectare WebSocket, în secunde |
| `ws_ping_interval` / `ws_ping_timeout` | `20` / `20` | detectarea conexiunilor moarte |
| `rpc_http` / `rpc_ws` | din `.env` | endpoint RPC doar pentru această instanță |
| `label`, `notes`, `tags` | — | metadate pentru dashboard |

Valorile din tabel sunt cele din cod (`bot/config.py`), adică ce primești dacă
omiți cheia din JSON. Profilurile livrate suprascriu unele dintre ele.

> **Despre tracțiune (volum și fdv).** Pentru un token pump.fun de câteva
> secunde, scorul RugCheck nu mai separă nimic: măsurat pe 25 de token-uri
> trecute, **toate** aveau scor 1 și zero semnale de risc. Singurul lucru care
> chiar diferențiază e dacă cineva îl tranzacționează. Cifre reale, la ~3 minute
> după lansare: mediana volumului e **$26**, dar lansările care contează fac
> $5.000–$40.000. De aici `min_volume_h24_usd`.
>
> DexScreener indexează perechea abia după ~60–70 s, deci pragurile astea cer
> `liquidity_wait_seconds` de cel puțin ~100 s. Un token sub prag **nu e respins
> imediat** — se reinterogează până expiră fereastra, ca să aibă timp să adune
> volum; cele care reușesc ies mai devreme, cele care nu, raportează
> `(dupa Ns)`.

> **Despre RugCheck.** Pentru token-uri de câteva secunde, RugCheck răspunde
> `400 {"error":"unable to generate report"}` — pur și simplu nu a apucat să le
> indexeze. Botul tratează asta ca *raport inexistent*, nu ca API picat, iar
> `rugcheck_wait_seconds` spune cât așteptăm să apară raportul. Cu `0`, a doua
> opinie nu contribuie deloc la lansările foarte proaspete.

> **Despre lichiditate.** DexScreener indexează perechea pump.fun aproape
> instantaneu, dar cât timp tokenul e pe bonding curve răspunsul **nu conține
> deloc** câmpul `liquidity` — nu îl raportează ca zero, pur și simplu lipsește.
> Botul tratează asta ca *necunoscut*, nu ca `$0`; `allow_missing_liquidity`
> decide dacă un token cu lichiditate necunoscută trece mai departe la RugCheck
> (`true`) sau iese `PENDING` (`false`).

Adaugi un bot nou copiind un JSON și schimbând `bot_id`. Apare în dashboard
după restartul backend-ului (sau prin `POST /api/instances`).

---

## 6. API

| Metodă | Rută | Rol |
|--------|------|-----|
| `GET` | `/api/health` | stare server |
| `GET` | `/api/instances` | instanțe + statistici |
| `POST` | `/api/instances` | creează/actualizează configul unui bot |
| `POST` | `/api/instances/{id}/start` | pornește instanța (proces separat) |
| `POST` | `/api/instances/{id}/stop` | oprire curată (SIGTERM, apoi SIGKILL) |
| `POST` | `/api/instances/{id}/restart` | repornire |
| `DELETE` | `/api/instances/{id}` | oprește și șterge configul |
| `GET` | `/api/events` | istoric; filtre `bot_id`, `type`, `verdict`, `search`, `limit`, `before_ts` |
| `GET` | `/api/stats` | agregate; opțional `?bot_id=` |
| `POST` | `/api/ingest` | folosit de boți (necesită header `x-ingest-token`) |
| `WS` | `/ws` | flux live către dashboard |

Documentație interactivă: **http://127.0.0.1:8000/docs**

---

## 7. Expunere publică, gratis

### Cloudflare Tunnel (recomandat)

Fără cont, fără card, fără port forwarding:

```bash
brew install cloudflared          # sau: https://github.com/cloudflare/cloudflared/releases

cd frontend && npm run build      # ca backend-ul să servească și dashboardul
cloudflared tunnel --url http://localhost:8000
```

Primești un URL `https://ceva-random.trycloudflare.com` care merge imediat,
inclusiv WebSocket-ul. Este temporar (durează cât ține procesul).

Pentru un subdomeniu stabil ai nevoie de un cont Cloudflare gratuit și un domeniu:

```bash
cloudflared tunnel login
cloudflared tunnel create pumpmonitor
cloudflared tunnel route dns pumpmonitor monitor.domeniul-tau.com
cloudflared tunnel run --url http://localhost:8000 pumpmonitor
```

**Înainte să expui public:**

1. Schimbă `INGEST_TOKEN` cu ceva aleator (`openssl rand -hex 24`).
2. Setează `DASHBOARD_TOKEN` — fără el, oricine are linkul poate porni și opri boți:
   ```bash
   echo "DASHBOARD_TOKEN=$(openssl rand -hex 24)" >> backend/.env
   ```
   Deschizi dashboardul o singură dată cu `https://.../?token=VALOAREA`. Tokenul
   e reținut în `sessionStorage` și șters din bara de adrese, ca să nu rămână în
   istoric sau în capturi de ecran.
3. Setează `CORS_ORIGINS` la domeniul tău, nu `*`.
4. Pentru control de acces per-utilizator (nu doar un secret partajat), pune
   Cloudflare Access în față — e gratuit până la 50 de utilizatori.

### Alternative gratuite

- `ssh -R 80:localhost:8000 serveo.net` — zero instalare
- `npx localtunnel --port 8000`

---

## 8. Teste

```bash
cd backend

# 220 de teste, fără rețea
.venv/bin/python -m unittest discover -s tests -t .

# Test live pe mainnet (necesită internet)
.venv/bin/python -m tools.smoke_live --seconds 45 --max-tokens 5
.venv/bin/python -m tools.smoke_live --seconds 60 --max-tokens 5 --full   # și DexScreener + RugCheck
```

---

## 9. Rezistență (ce se întâmplă când ceva pică)

| Situație | Comportament |
|----------|--------------|
| WebSocket-ul cade | reconectare cu backoff exponențial + jitter, plafonat la 60 s |
| Nodul nu răspunde la ping | conexiunea e considerată moartă și se reface |
| RPC dă 429 / 5xx | retry cu backoff; verdictul devine `ERROR`, nu `FAIL` |
| Backend-ul e oprit | boții pun evenimentele într-o coadă și le retrimit când revine |
| Coada crește prea mult | detecțiile mai vechi de 25 s sunt aruncate, cu contor vizibil |
| RPC-ul e prea lent | pipeline-ul are deadline; se oprește și raportează `ERROR`/`deadline` în loc să irosească apeluri |
| Botul crapă | managerul îl repornește automat; max 5 reporniri / 60 s, apoi se oprește |
| DexScreener/RugCheck sunt jos | `rugcheck_fail_open` decide; DexScreener dă `PENDING` |
| Telegram e indisponibil sau dă rate limit | mesajul e reîncercat de câteva ori, apoi abandonat; botul nu e afectat |
| SQLite dă eroare la scriere | se loghează, dar fluxul live nu se oprește |
| Un API răspunde cu date corupte | `httpx.HTTPError` prins integral (inclusiv erori de decodare); firul de trimitere a evenimentelor nu poate muri |
| Serverul e ucis brutal | la repornire detectează boții rămași orfani și refuză să pornească un al doilea proces cu același `bot_id` |
| Nodul RPC întoarce un mint incomplet | verdict `ERROR`, niciodată `PASS` — lipsa unui câmp nu e tratată ca „autoritate revocată" |
| Dashboard rămas în aer (laptop adormit) | conexiunea e închisă după 75 s de inactivitate, resursele eliberate |
| Baza crește | curățare automată la 6 ore, se păstrează 14 zile |

Logurile fiecărui bot: `backend/data/logs/<bot_id>.log` (rotative, 5 × 2 MB).

---

## 10. Performanță — cifre măsurate

Toate valorile de mai jos sunt măsurate pe această mașină, pe mainnet, nu estimate.

| Măsurătoare | Valoare | Cum |
|---|---|---|
| Latență detecție→verdict, doar on-chain (`rapid`) | **37–550 ms** | rulare live, 19 token-uri |
| Latență detecție→verdict, pipeline complet (`echilibrat`) | **2.1–2.7 s** | 188 de verdicte live |
| Rată de respingere, pipeline complet | **25%** (47/188) | motive reale de la RugCheck |
| Paralelism | **10× accelerare** | 10 token-uri × 2 s întârziere RPC → 2.00 s, nu 20 s |
| Blocarea event loop-ului sub sarcină | **max 3 ms** | 169 de eșantioane la 10 ms, sub încărcare completă |
| Volum WebSocket | **1416 mesaje/s** | programul pump.fun, măsurat 60 s |
| Cost CPU cale fierbinte | **0.2% dintr-un nucleu** | 1.6 µs/mesaj (prefiltru înainte de `json.loads`) |
| Rată reală de lansări | **0.85 token/s** | 51 de creări în 60 s |
| Plafon de debit per instanță | **2.0 token/s** | impus de `rpc_min_interval=0.5` |
| Marjă | **2.4×** | plafon ÷ rată reală |
| Oprire pe conexiune liniștită | **0.00 s** | era până la 60 s (timeout de `recv`) |

Latența e măsurată cu `time.monotonic()` din clipa în care linia de log ajunge pe
WebSocket până la verdictul final — include tot, inclusiv timpul petrecut în coadă.
Nu e o estimare a apelului de rețea.

**De ce nu poate fi mai rapid decât atât.** Un bot cu infrastructură plătită
rulează pe un nod Solana propriu, colocat lângă validatori, cu acces la mempool
(Jito/bloXroute) și vede tranzacția *înainte* să fie confirmată. Aici plecăm de la
`logsSubscribe` pe un RPC public partajat: primim logul după ce blocul e procesat,
plus latența de rețea până la nod. Diferența e de ordinul sutelor de milisecunde
și **nu se închide prin cod mai bun** — ține de unde stă mașina și pe ce nod
vorbește. Ce se poate face gratuit: Helius free tier scade latența și elimină
429-urile; restul e infrastructură plătită.

---

## 11. Probleme frecvente

**Toate token-urile ies `PENDING`** — normal cu `allow_missing_liquidity: false`.
Token-urile proaspete de pe pump.fun n-au încă pereche pe DexScreener, banii
sunt în bonding curve. Fie pui `true`, fie crești `liquidity_wait_seconds`.

**Latența crește constant, apar `ERROR: HTTP 429`** — RPC-ul e gâtuirea, nu botul.
`rpc_min_interval` este **per instanță**, iar rate limit-ul furnizorului este per IP:
trei boți cu `0.5` înseamnă ~6 cereri/secundă din același IP, peste ce tolerează
RPC-ul public. Soluții, în ordinea eficienței:

1. treci pe Helius free tier (fără card) — rezolvă complet problema;
2. rulează un singur bot pe RPC-ul public;
3. crește `rpc_min_interval` la `1.0`+.

Pipeline-ul are un *deadline*: dacă RPC-ul e prea lent, verdictul devine `ERROR`
cu etapa `deadline` în loc să producă rezultate de zeci de secunde. Latența afișată
rămâne astfel o măsurătoare onestă, nu una mascată.

**`contul de mint nu exista on-chain`** — nodul e în urmă. Crește
`authority_retries` sau schimbă providerul.

**Dashboardul arată `DECONECTAT`** — backend-ul nu rulează sau proxy-ul Vite nu
ajunge la `:8000`. Verifică `curl http://127.0.0.1:8000/api/health`.

**`Ingest respins (401)`** — `INGEST_TOKEN` din `.env` nu se potrivește. Repornește
și backend-ul, și boții după ce îl schimbi.

**Botul Telegram nu afișează niciun chat id, doar se oprește** — `getUpdates`
nu are ce să întoarcă pentru că botul n-a primit încă niciun mesaj. Deschide
chatul cu botul tău în Telegram, scrie-i ceva (de exemplu `salut`), apoi
pornește botul din nou.

---

## 12. Structură

```
pumpfun-monitor/
├── start.sh                  pornește tot
├── backend/
│   ├── app/                  serverul FastAPI
│   │   ├── main.py           rute REST + WebSocket + ingest
│   │   ├── manager.py        pornire/oprire boți ca procese separate
│   │   ├── hub.py            broadcast către dashboard-uri
│   │   ├── db.py             SQLite (WAL, agregate, curățare)
│   │   └── models.py         validare Pydantic
│   ├── bot/                  listener-ul
│   │   ├── runner.py         orchestrator per instanță
│   │   ├── listener.py       logsSubscribe + reconectare
│   │   ├── parser.py         extragere mint/nume/simbol
│   │   ├── checks.py         pipeline anti-rug fail-fast
│   │   ├── rpc.py            JSON-RPC cu rate limit și retry
│   │   ├── emitter.py        coadă de evenimente către backend
│   │   ├── telegram.py       notificări Telegram la verdict PASS (opțional)
│   │   ├── config.py         config per instanță
│   │   ├── settings.py       încărcarea .env + mascarea secretelor
│   │   ├── events.py         schema evenimentelor
│   │   ├── logging_setup.py  logging structurat, rotativ
│   │   └── solana_util.py    base58, borsh, discriminatori Anchor
│   ├── configs/              o instanță = un JSON
│   ├── tests/                220 de teste unitare
│   └── tools/smoke_live.py   test live pe mainnet
└── frontend/
    └── src/
        ├── App.jsx
        ├── components/       TopBar, MetricsBar, InstancePanel, FeedTable, DetailPanel
        ├── hooks/useBackend.js
        └── styles/index.css
```

---

## 13. Avertisment

Instrument de **monitorizare și cercetare**. Filtrele reduc riscul, nu îl elimină:
un token poate trece toate verificările și tot să fie rug pull. Nu execută
tranzacții și nu dă sfaturi de investiție.
