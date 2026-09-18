# smi-sensor-fusion

Broker di sensor fusion per il digital twin, dockerizzato: dai frame
radar georeferenziati della bicicletta "senziente" (DROPMO geo bridge,
UDP JSON) e dallo stato del canale 5G di una **lista di IMSI** (score
0–10 dal metrics server, risoluzione IMSI→ranUeNgapID via AMF) calcola
un **grado di rischio incidenti 0–10** e lo serve ai client digital
twin via TCP (record `smi.risk`; la qualità del canale qualifica
l'affidabilità del dato radar: un link degradato amplifica il rischio
rilevato, ma da solo non ne crea). Un quarto contributo, calcolato in
modo **asincrono** da un LLM (vLLM) a partire dalla geometria statica del
sito (edifici, strade), aggiunge un fattore di rischio ambientale
(`dt_ai_env_risk`) quando rileva pericoli che il radar da solo non vede
(es. un angolo cieco dietro un edificio) — vedi `DATA_MODEL.md`.

File:

- `startup.py` — orchestratore del workflow completo, entrypoint
  dell'immagine Docker: fase 1 (AoI su ENVELOPE Devices Location APIs),
  fase 2 (attesa del primo frame radar sul `--listen-port` di
  `broker.py`, bypassabile), poi lancia `broker.py` (fase 3) passandogli
  anche l'AoI di fase 1, oltre agli argomenti extra dati sulla riga di
  comando.
- `envelope_location.py` — client delle ENVELOPE Devices Location APIs
  (Devices-in-Area): health check, subscription su un'area circolare,
  ricezione notifiche di callback, query dei device correntemente
  nell'area (quest'ultima non usata dal workflow di `startup.py`, resta
  disponibile come funzione di libreria). Usato da `startup.py`
  (richiede `requests`).
- `broker.py` — il broker (solo stdlib Python ≥ 3.8): oltre alla fusione
  radar+canale+LLM già c'era, scarta i frame fuori dall'AoI (se
  `--aoi-lat`/`--aoi-lon` sono dati) e mette in pausa le chiamate LLM se
  il radar smette di mandare frame (vedi "Comportamento" sotto).
- `Dockerfile`, `docker-compose.yml` — immagine e servizi; la stessa
  immagine impacchetta `startup.py` + `envelope_location.py` +
  `broker.py` (fasi 1-2-3, servizio `broker`) e `dt_client.py` (servizio
  `dt-client`, entrypoint diverso), così tutta la pipeline (AoI →
  subscription → attesa device → risk fusion+LLM → invio al Risk
  Escalation Service) parte insieme con un solo `docker compose up`.
- `amf_proxy.py` — forwarder TCP da eseguire su gnb-test (10.211.1.140):
  espone l'API AMF (172.24.254.140:8080, raggiungibile solo da gnb-test)
  sulla porta 18080 lato 10.211.0.0/16.
- `dt_client.py` — client TCP per `--serve-port`: legge i record
  `smi.risk`, deriva `riskLevel` (1-5) da `risk_score` e, a ogni
  cambio di livello, invia un risk-event al Risk Escalation Service
  (`POST {url}/api/v1/risk-events`, contratto INSOFTDEV/SPEC-001 v2.2)
  — l'entità consumer che sostituisce "il digital twin". `devices[]`
  viene popolato interrogando le ENVELOPE Devices Location APIs
  (`envelope_location.ipv4_addresses_in`) per gli IPv4 visti nell'AoI
  negli ultimi `--devices-max-age-s` secondi (default 60): il rischio
  vale per chi è fisicamente nell'area, non solo per la bici. Se
  `--risk-escalation-url` non è dato, resta un semplice consumer che
  stampa/logga (comportamento originale, utile per debug manuale).
- `fetch_geometry.py` — tool ausiliario offline: scarica edifici/strade/
  amenity attorno a un punto da OpenStreetMap e scrive `geometry.geojson`.
  Non viene eseguito da `broker.py`; va lanciato a mano, una volta per
  sito (o quando serve aggiornare il rilievo).
- `geometry.geojson` — geometria statica del sito reale, generata con
  `fetch_geometry.py`.
- `test_replay.py` — replay di test dai log `smi.risk` storici in
  `/home/guardtwin/logs`, con mock AMF/metrics.
- `test_replay_dropmo.py` — replay di test da oggetti radar DROPMO v1.1
  reali (`objects_yaw.csv` di una sessione in `/home/guardtwin/DROPMO/data/`),
  con mock AMF/metrics a punteggio costante o casuale. Vedi "Test con dati
  radar reali (DROPMO)" sotto.
- `DATA_MODEL.md` — modello dati e di rischio `smi.risk` v3 servito al DT.

## Porte

| Porta | Proto | Direzione | Uso |
|---|---|---|---|
| `--notify-port` (8080, startup.py) | HTTP | in | callback di ENVELOPE per la subscription sull'AoI (fase 1) |
| `--listen-port` (30490) | UDP | in | traffico utente: frame geo JSON (fase 2: il primo frame identifica il nodo di sensing; fase 3: ingest continuo) |
| `--serve-port` (30500) | TCP | in | i client DT si connettono qui e ricevono i record di rischio NDJSON |
| `--llm-url` → servizio `vllm` (8000) | HTTP | in/out | endpoint OpenAI-compatible di vLLM per il rischio ambientale |

`--listen-port` e `--serve-port` sono selezionabili; nel compose si
cambiano con le variabili `UDP_INGEST_PORT` e `DT_SERVE_PORT`.

## Avvio completo (fasi 1-2-3)

Fuori da Docker, a mano sull'host:

    python3 startup.py \
        --aoi-lat 45.064924 --aoi-lon 7.659707 --aoi-radius 150 \
        --imsi 001010000167802 --listen-port 30490 --serve-port 30500 \
        --resolver-url http://10.211.1.140:18080 \
        --metrics-url http://10.211.1.140:8080 --gnb-id f01 \
        --llm-url http://<host-vllm>:8000 --geometry-file geometry.geojson

`startup.py` riconosce solo i propri argomenti (`--aoi-*`,
`--skip-device-detect`, `--detect-timeout-s`, `--notify-*`,
`--broker-py`); tutto il resto (`--imsi`, `--serve-port`, `--llm-url`,
...) viene passato invariato a `broker.py` quando parte la fase 3.

Fase 2 non interroga più ENVELOPE per sapere quale dispositivo è la
bici: si limita a legare essa stessa la porta UDP di ingest di
`broker.py` (lo stesso `--listen-port`/`--listen-host` passato per la
fase 3, letto dagli argomenti forwardati) e aspettare il primo frame
radar. L'IP sorgente di quel frame è il nodo di sensing trovato; la
socket viene chiusa subito dopo così `broker.py` può legare la stessa
porta per la fase 3 (quel primo frame viene quindi consumato qui e non
arriva a `broker.py`; eventuali frame nella breve finestra tra la
chiusura e il bind di `broker.py` si perdono, come qualunque pacchetto
UDP mandato quando nessuno è ancora in ascolto). Con
`--skip-device-detect` questa fase viene saltata e si passa
direttamente alla fase 3.

### Log verbose (fasi 1-2)

Con `python3 startup.py -v ...` (o `VERBOSE=1` in Docker, vedi sotto) si
vedono a livello DEBUG: ogni chiamata alle ENVELOPE Devices Location
APIs (`/health`, `/subscriptions`) con corpo della richiesta e
riassunto della risposta, e -- a livello INFO -- il frame radar che
sblocca la fase 2 e ogni notifica di callback ricevuta da ENVELOPE
sulla subscription. Le librerie `requests`/`urllib3` aggiungono in più,
sempre a DEBUG, la riga HTTP grezza (metodo, path, status, byte).

## Avvio con Docker

Il container esegue `startup.py`: fasi 1-2-3 tutte dentro, non solo la
fase 3. Serve quindi impostare `NOTIFY_HOST` (o `--notify-host` nel
`docker run` a mano) all'IP di questo host **così come raggiungibile da
ENVELOPE (192.168.0.38)** -- non viene auto-rilevato dentro il
container, e senza il valore giusto la subscription della fase 1 non
riceverebbe mai le notifiche. Il compose fallisce esplicitamente
all'avvio se non è impostato, apposta.

Su gnb-test (una volta, prerequisito per la risoluzione IMSI):

    nohup python3 amf_proxy.py >/tmp/amf_proxy.log 2>&1 &

Su envelope-edgeserver-1 (vedi ["vLLM e geometria del
sito"](#vllm-e-geometria-del-sito) sotto per generare `geometry.geojson`
la prima volta):

    cd ~/guardtwin-edge
    NOTIFY_HOST=<ip di questo host visto da ENVELOPE> \
    RISK_ESCALATION_URL=<base URL del Risk Escalation Service> \
    EVALUATOR_BEARER_TOKEN=<token emesso all'evaluator> \
    docker compose up -d --build

(oppure esportate in `.env`/nell'ambiente della shell; le altre
variabili di fase 1-2 -- `AOI_LAT`, `AOI_LON`, `AOI_RADIUS`,
`NOTIFY_PORT`, `AOI_ID` -- hanno un default sensato per questo sito e di
solito non vanno toccate; `RISK_ESCALATION_URL`/`EVALUATOR_BEARER_TOKEN`
sono invece obbligatorie, senza default, per lo stesso motivo di
`NOTIFY_HOST`: sono specifiche del deployment e il compose fallisce
esplicitamente se mancano — vedi anche "Risk Escalation Service" sotto).
Per saltare la fase 2 (device detect), basta un `SKIP_DEVICE_DETECT` non
vuoto; per il log verbose delle fasi 1-2, un `VERBOSE` non vuoto (i log
si vedono con `docker compose logs -f broker`/`dt-client`, essendo i
servizi avviati in background con `-d`):

    NOTIFY_HOST=<ip> VERBOSE=1 RISK_ESCALATION_URL=<url> EVALUATOR_BEARER_TOKEN=<token> \
    docker compose up -d --build
    docker compose logs -f broker dt-client

oppure a mano, scegliendo lista IMSI e porte:

    docker build -t guardtwin-broker:latest .
    docker run -d --name guardtwin-broker --restart unless-stopped \
        -p 30490:30490/udp -p 30500:30500/tcp -p 8080:8080/tcp \
        -v ./geometry.geojson:/app/geometry.geojson:ro \
        guardtwin-broker:latest \
        --aoi-lat 45.064924 --aoi-lon 7.659707 --aoi-radius 150 \
        --notify-host <ip di questo host visto da ENVELOPE> \
        --imsi 001010000167808,001010000167802 \
        --listen-port 30490 --serve-port 30500 \
        --resolver-url http://10.211.1.140:18080 \
        --metrics-url http://10.211.1.140:8080 --gnb-id f01 \
        --llm-url http://<host-vllm>:8000 --geometry-file /app/geometry.geojson

(aggiungere `--skip-device-detect` prima degli argomenti di `broker.py`
per saltare la fase 2 e andare diretti alla supervisione)

`dt_client.py`, a mano, stessa immagine ma entrypoint diverso:

    docker run -d --name guardtwin-dt-client --restart unless-stopped \
        --entrypoint python3 guardtwin-broker:latest \
        -u dt_client.py --host <ip/hostname del broker> --port 30500 \
        --aoi-lat 45.064924 --aoi-lon 7.659707 --aoi-radius 150 \
        --risk-escalation-url <base URL del Risk Escalation Service> \
        --evaluator-token <token evaluator>

(via `docker compose`, il servizio `dt-client` fa lo stesso e si trova
già sulla stessa rete Docker del broker, raggiungibile come `broker`)

(il container `vllm` va avviato a parte, `docker compose up -d vllm`,
oppure con `docker run` puntando `--llm-url` a un endpoint OpenAI-
compatible già in esecuzione altrove; omettendo `--llm-url`/
`--geometry-file` il broker funziona comunque, con `dt_ai_env_risk`
sempre a 0)

Nota: gli URL puntano a 10.211.1.140 (gnb-test) perché l'edge non ha
rotte verso 172.24.254.0/24; il metrics server è la porta pubblicata
8080, l'AMF passa dal proxy 18080.

## Client digital twin

Basta una connessione TCP: ogni riga è un record `smi.risk`.

    nc 10.211.1.250 30500            # oppure, in python:
    # for line in socket.create_connection(("10.211.1.250", 30500)).makefile():
    #     frame = json.loads(line)

Più client contemporanei sono supportati; un client lento che blocca il
socket viene scollegato.

`dt_client.py` è il consumer che sostituisce "il digital twin" — vedi
"Risk Escalation Service" sotto per cosa fa. Con `--log-file` appende
ogni record ricevuto, così com'è arrivato (una riga NDJSON ciascuno), a
un file — utile per rivedere in differita `env.reasoning`/`env.score`
quando si mette a punto il prompt dell'LLM (equivalente, lato consumer,
a `--jsonl-out` di `broker.py`):

    python3 dt_client.py --host 127.0.0.1 --port 30500 \
        --aoi-lat 45.064924 --aoi-lon 7.659707 --aoi-radius 150 \
        --risk-escalation-url http://<risk-escalation-service>/api/v1 \
        --evaluator-token <token> \
        --log-file dt_client.log

Senza `--risk-escalation-url` resta un client di sola lettura (stampa
ogni record con `risk_level` derivato, nessuna chiamata esterna) — comodo
per debug manuale senza toccare il resto della pipeline.

## Risk Escalation Service

`dt_client.py`, una volta connesso al flusso `smi.risk` del broker,
implementa il lato client del contratto SPEC-001 v2.2 (INSOFTDEV) verso
il Risk Escalation Service — il middleware che traduce il rischio in
QoS di rete via CAMARA Quality-on-Demand. Non a ogni record: solo
quando il `riskLevel` derivato da `risk_score` cambia rispetto
all'ultimo inviato, per non martellare il servizio al ritmo del radar
(~10 Hz) né generare escalate/de-escalate ridondanti.

- **`riskLevel`** (1–5, richiesto intero dal contratto): mappato da
  `risk_score` (0–10, continuo) con lo stesso arrotondamento half-up già
  usato altrove nel progetto, clampato a `[1, 5]` (il contratto rifiuta
  `0` con `400`).
- **`devices[]`**: non l'IP della singola bici (nessuna mappatura
  IMSI→IP live esiste oggi nel progetto — l'AMF risolve solo
  IMSI→`ranUeNgapID`), ma la lista degli IPv4 che ENVELOPE riporta
  attualmente nell'AoI (`envelope_location.ipv4_addresses_in`, finestra
  `--devices-max-age-s`, default 60s) — il rischio si applica a chi è
  fisicamente nell'area in quel momento. Se la lista è vuota (nessun
  device visto di recente), l'invio viene saltato e ritentato al
  prossimo cambio di `riskLevel`, senza fermare lo stream `smi.risk`.
- **`occurredAt`**: dal `ts_us` del record (istante di valutazione del
  broker), non dall'orologio di `dt_client.py`.
- **`eventId`/idempotenza**: un UUID per evento, riusato sui retry dello
  stesso invio (backoff `--risk-escalation-backoff-s` × tentativo, fino
  a `--risk-escalation-retries`); un nuovo evento (cambio di livello) ha
  sempre un `eventId` nuovo. `400`/`401` non vengono ritentati (errore
  del client, un retry identico non risolverebbe nulla).
- Un fallimento della query ENVELOPE devices-in-area (servizio giù,
  rete) viene loggato e trattato come "nessun device trovato" — non
  fa cadere né riconnettere la connessione TCP al broker, che è un
  problema indipendente.
- `--evaluator-token` è il bearer `EVALUATOR_BEARER_TOKEN` del
  contratto; `--aoi-id` è opzionale, solo per raggruppamento nella
  dashboard del middleware (mai letto dalla logica di decisione, per
  contratto).

Vedi `python3 dt_client.py --help` per tutte le opzioni.

## Comportamento

Lo score di ogni IMSI è interrogato ogni `--score-period` s (default 1);
le IMSI sono ri-risolte ogni `--resolve-period` s (default 60), così il
broker segue registrazioni/deregistrazioni degli UE. Se AMF o metrics
non rispondono il flusso radar non si interrompe: cambia solo
`net.ues[].state` (`unresolved` / `no_score` / `stale`).

Il rischio ambientale (`dt_ai_env_risk`) è calcolato da vLLM in modo
**continuo mentre arrivano frame radar freschi**: ogni chiamata parte
non appena la precedente ha risposto (nessun periodo fisso — è la
chiamata HTTP stessa, bloccante, a scandire il ritmo del loop). Se non
arriva più nessun frame radar (dropper node fermo/irraggiungibile) per
più di `--radar-idle-s` s (default 5), le chiamate all'LLM si mettono in
pausa (`env-risk: no radar frame in >Ns, pausing LLM calls...`) invece
di continuare a interrogarlo all'infinito con un contesto ormai
congelato; riprendono da sole non appena arriva un frame fresco
(`env-risk: radar frames resumed, LLM calls resumed`). Ogni chiamata
effettivamente fatta logga quanto ci ha messo il modello a rispondere
(`env-risk: LLM call took N.NNs`), utile per capire la latenza reale del
prompt. Se una chiamata fallisce (vLLM ancora in caricamento,
irraggiungibile, ecc.) il retry è ritardato di `--llm-retry-s` s
(default 1) per non martellare un server già in difficoltà. In ogni
caso, se il risultato più recente disponibile ha più di `--llm-stale-s`
s (default 12) o non ce n'è ancora uno (avvio, o `--llm-disable`), il
contributo usato da `assess()` è `0` — il flusso radar non si interrompe
mai (`env.state`: `ok` / `stale` / `no_result`).

Se vengono dati `--aoi-lat`/`--aoi-lon` (`startup.py` li passa sempre a
`broker.py`, con lo stesso valore usato per la subscription ENVELOPE di
fase 1 — vedi sopra), ogni frame la cui posizione ego cade fuori da
`--aoi-radius` metri dal centro viene scartato **prima** della fusione
(non entra né nel calcolo del rischio né nello stream verso i client
DT), con un log `WARNING dropping frame seq=... outside the AoI`. Un
frame senza fix GPS (`ego.lat`/`ego.lon` assenti) non viene filtrato,
perché non c'è una posizione su cui decidere. Il contatore dei frame
scartati per questo motivo è nel riepilogo finale (`done: N frames
fused, N bad datagrams, N dropped (outside AoI)`).

## vLLM e geometria del sito

Il servizio `vllm` nel compose usa la GPU locale (reservation NVIDIA via
Docker Compose) e serve un modello instruct (`LLM_MODEL`, default
`Qwen/Qwen2.5-7B-Instruct`) su `LLM_PORT` (default 8000). Al primo avvio
scarica i pesi da Hugging Face (richiede accesso a internet) in un volume
persistente (`vllm-cache`).

La geometria statica (edifici, strade, amenity) attorno al sito va
fornita come GeoJSON in `GEOMETRY_FILE_HOST` (default `./geometry.geojson`,
montato in sola lettura nel broker); è opzionale — se assente il rischio
ambientale funziona comunque, senza contesto geometrico.

`broker.py` non la scarica più da solo a ogni avvio (era stato provato:
il server pubblico Overpass risponde 504 abbastanza spesso sotto carico
da non valerne la pena per un dato che comunque non cambia a runtime).
Per generarla o aggiornarla si usa `fetch_geometry.py`, a mano, una
tantum:

    python3 fetch_geometry.py --lat 45.064924 --lon 7.659707 \
        --radius-m 300 --out geometry.geojson

`--lat`/`--lon` sono il centro dell'area (nota: separati e nominati,
apposta, per non dover ricordare in che ordine vanno — a differenza dei
campi `pos` nei log `smi.risk`, che sono `[lat, lon]`); `--radius-m` è
il mezzo lato in metri del quadrato scaricato (lato totale = 2x questo
valore, deve coprire l'area in cui la bicicletta si muove). Riprova
automaticamente fino a `--attempts` volte (default 5) se Overpass
risponde 504 — capita spesso, è transitorio, un tentativo successivo va
spesso a buon fine; non serve nessun token/API key, è pubblica e
anonima. `--overpass-url` punta a un mirror/istanza propria se il
server pubblico continua a dare problemi.

Variabili `.env` aggiuntive: `LLM_MODEL`, `LLM_PORT`, `LLM_RETRY_S`,
`LLM_STALE_S`, `LLM_HTTP_TIMEOUT`, `LLM_MAX_MODEL_LEN`,
`LLM_GPU_MEM_UTIL`, `GEOMETRY_FILE_HOST`.

Per il servizio `dt-client` (vedi "Risk Escalation Service" sopra):
`RISK_ESCALATION_URL`, `EVALUATOR_BEARER_TOKEN` (obbligatorie, nessun
default) e `AOI_ID` (opzionale).

## Test rapido senza sorgente radar

    # rigioca un file jsonl come stream UDP verso il broker
    python3 - <<'EOF'
    import socket, time
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for line in open('geo_run.jsonl'):
        s.sendto(line.strip().encode(), ('127.0.0.1', 30490))
        time.sleep(0.1)
    EOF

Per un test end-to-end più realistico (radar + AMF + metrics + LLM) è
incluso `test_replay.py`, che rigioca i log `smi.risk` storici in
`/home/guardtwin/logs` (ricostruendo frame radar approssimati dai
`threats[]` registrati) e simula AMF/metrics con gli score reali
osservati. Vedi `python3 test_replay.py --help`.

## Test con dati radar reali (DROPMO)

`test_replay_dropmo.py` rigioca oggetti radar **veri** (non ricostruiti)
da una sessione DROPMO v1.1 già decodificata in CSV da
`DROPMO/tools/convert_session.py` (default: `objects_yaw.csv` della
sessione `2026-09-11_test`, 12.653 frame, ~1.700 oggetti con yaw
confermato). Utile per validare fusione, risk model e boost yaw contro
forme radar reali invece che sintetiche.

Un limite importante, non aggirabile con i dati disponibili: i frame
DROPMO sono puramente radar-relativi (x/y in metri dal radar, nessun
GPS) — il georeferenziamento in produzione lo fa l'OBU UNIMORE (che ha
l'antenna GPS), non DROPMO. Le due sessioni registrate in
`DROPMO/data/` sono entrambe da banco, bici ferma per tutta la durata:
non c'è quindi nemmeno un log GPS reale da riagganciare. Lo script
simula: bici ferma a un punto fisso (`--ego-lat`/`--ego-lon`, default
la posizione reale della sessione, 45.0649195,7.659724166666667) con
un heading fisso e arbitrario (`--heading-deg`, la bici non ha mai
girato), e proietta le posizioni radar-relative attorno a
quell'ancora — le lat/lon risultanti servono a esercitare la pipeline,
non sono geograficamente significative. Lo yaw di ogni oggetto viene
convertito in heading assoluto con le funzioni ufficiali di
`dropmo.yaw` (`sensor_to_vehicle_yaw`, `vehicle_to_enu_yaw`,
`enu_yaw_to_heading_deg`), componendo mount + lo stesso heading fisso,
così resta coerente con la posizione proiettata.

Come `test_replay.py`, simula anche AMF/metrics, ma qui lo stato di
rete non ha un log storico da riusare: `--metrics-mode constant`
(default, punteggio fisso via `--metrics-score`) o `--metrics-mode
random` (punteggio casuale a ogni poll tra `--metrics-random-min` e
`--metrics-random-max`).

    python3 test_replay_dropmo.py --amf-port 18080 --metrics-port 18081 &
    python3 broker.py --imsi 001010000167806 \
        --listen-port 30491 --serve-port 30500 \
        --resolver-url http://127.0.0.1:18080 \
        --metrics-url http://127.0.0.1:18081 --gnb-id f01 \
        --aoi-lat 45.0649195 --aoi-lon 7.659724166666667 --aoi-radius 150 \
        --stdout -v

Vedi `python3 test_replay_dropmo.py --help` per tutte le opzioni
(inclusa `--dropmo-path` se il checkout di DROPMO non è in
`/home/guardtwin/DROPMO`).
