FROM python:3.12-slim

WORKDIR /app
COPY broker.py ai_assess.py envelope_location.py startup.py escalator.py /app/

# broker.py, ai_assess.py e startup.py sono stdlib; envelope_location.py (usato da
# startup.py per le fasi 1-2, e da escalator.py per il lookup
# devices-in-area) usa `requests` per parlare con le ENVELOPE Devices
# Location APIs. escalator.py stesso è stdlib (urllib) per la chiamata
# al Risk Escalation Service.
RUN pip install --no-cache-dir "requests>=2.31,<3"

# Porte di default (sovrascrivibili dagli argomenti a runtime):
#   30490/udp  ingest traffico utente (frame geo JSON)
#   30500/tcp  server per i client Risk Escalator (stream NDJSON)
#   8080/tcp   callback di ENVELOPE per la subscription sull'AoI (fase 1);
#              va raggiungibile da ENVELOPE, vedi --notify-host in startup.py
EXPOSE 30490/udp 30500/tcp 8080/tcp

ENTRYPOINT ["python3", "-u", "startup.py"]
# Gli argomenti si passano come command: quelli di startup.py (--aoi-lat/
# --aoi-lon/--aoi-radius/--notify-host/--skip-device-detect/...) più, non
# riconosciuti, quelli di broker.py (lista IMSI, porte, URL) che vengono
# inoltrati invariati quando parte la fase 3:
#   docker run ... guardtwin-broker:latest \
#       --aoi-lat 45.064924 --aoi-lon 7.659707 --aoi-radius 150 \
#       --notify-host <ip-host-raggiungibile-da-envelope> \
#       --imsi <lista> --listen-port 30490 --serve-port 30500 ...
#
# escalator.py usa la stessa immagine ma con entrypoint diverso (vedi il
# servizio escalator in docker-compose.yml, che sovrascrive `entrypoint`).
CMD ["--help"]
